"""Helix firewall: config model, nftables ruleset generator, learn mode and exposure audit. Pure logic.

Design (CLAUDE.md §6a):
- One nftables table Helix owns, `inet helix`. Docker's and PIA's iptables(-nft) rules are never touched; our
  base chains run just before theirs (priority filter - 10). An accept here does not stop their chains; a drop
  here is final.
- input  = connections to this machine (incl. host-network containers).
- forward, only towards Docker bridges = published container ports. Docker DNATs in prerouting, so the port
  the client asked for is `ct original proto-dst`. Container->container and container->outside are untouched.
- Modes: off (no table), learn (nothing blocked; would-be blocks are logged), enforce (default deny inbound).
- Lockout protection: SSH and the web UI (22, 80, 443, 8443) are always reachable from LAN and VPN; applying
  an enforcing config is confirm-or-revert (root helper `helix-firewall`); `helix-firewall off` on the console.

The root helper (network/helix-firewall) imports this module to build the same ruleset at boot and on apply.
"""
import ipaddress
import json
import re
import time

VERSION = 1
MODES = ("off", "learn", "enforce")
SOURCES = ("lan", "vpn", "lan_vpn", "any", "custom")
PROTOS = ("tcp", "udp", "both")
LOG_PREFIX = {"enforce": "helix-fw-drop: ", "learn": "helix-fw-learn: "}

# Private, link-local, CGNAT (Tailscale) and IPv6 ULA/link-local: what "LAN" means unless the user edits it.
DEFAULT_LAN = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "fc00::/7", "fe80::/10"]
VPN_IFACES = ("wg*", "tailscale0")
VPN_NETS = ["100.64.0.0/10"]                      # Tailscale addresses
DOCKER_IFACES = ("docker0", "br-*", "br_*")       # Docker bridges (the VM bridge br0 does not match)

# Always open from LAN + VPN: you can never lock yourself out of SSH or the web UI.
MGMT_TCP = (22, 80, 443, 8443)

# Ports worth a warning when reachable from more than this machine.
SENSITIVE = {
    21: "FTP (unencrypted)", 23: "Telnet (unencrypted)", 111: "rpcbind", 445: "SMB file shares",
    2049: "NFS file shares", 2375: "Docker API without TLS (full control of the server)", 2376: "Docker API",
    3306: "MySQL/MariaDB database", 3389: "Remote Desktop", 5432: "PostgreSQL database", 5900: "VNC",
    5984: "CouchDB database", 6379: "Redis database", 8086: "InfluxDB database", 9200: "Elasticsearch",
    11211: "Memcached", 27017: "MongoDB database", 5050: "Checkmk", 8082: "Helix backend (bypasses nginx)",
    3781: "Nginx Proxy Manager API", 81: "Nginx Proxy Manager admin",
}

# Names for ports whose socket belongs to the kernel or to systemd socket activation (ss shows no useful process).
WELL_KNOWN = {
    21: "FTP", 22: "SSH", 53: "DNS", 67: "DHCP", 80: "Web UI (HTTP)", 81: "Nginx Proxy Manager admin", 111: "rpcbind (NFS)",
    137: "NetBIOS (SMB)", 138: "NetBIOS (SMB)", 139: "SMB", 443: "Web UI (HTTPS)", 445: "SMB", 1900: "SSDP discovery",
    2049: "NFS", 3702: "WS-Discovery (SMB)", 5353: "mDNS (Avahi)", 5355: "LLMNR", 6556: "Checkmk agent",
    8443: "Web UI (fallback HTTPS)", 41641: "Tailscale", 51820: "WireGuard",
}
SOCKET_OWNERS = ("systemd", "")                   # process names that say nothing about the service

# ── extras (all applied only in enforce mode) ──
DEFAULT_BLOCKLISTS = [
    {"id": "firehol1", "name": "FireHOL level 1 (attackers, botnets, bogons)",
     "url": "https://iplists.firehol.org/files/firehol_level1.netset", "enabled": False},
    {"id": "spamhaus_drop", "name": "Spamhaus DROP (hijacked networks)",
     "url": "https://www.spamhaus.org/drop/drop.txt", "enabled": False},
    {"id": "spamhaus_dropv6", "name": "Spamhaus DROPv6",
     "url": "https://www.spamhaus.org/drop/dropv6.txt", "enabled": False},
]
# Free per-country address lists (aggregated). {cc} is the lower-case ISO 3166 code.
GEO_URLS = ["https://www.ipdeny.com/ipblocks/data/aggregated/{cc}-aggregated.zone",
            "https://www.ipdeny.com/ipv6/ipaddresses/aggregated/{cc}-aggregated.zone"]
LIST_HOSTS = ("iplists.firehol.org", "www.spamhaus.org", "www.ipdeny.com")
LIST_CAP = 300_000                                # entries per list (FireHOL level 1 is ~4-5k after merging)
EGRESS = {"internet": "Internet (normal)", "lan": "LAN only, no internet", "none": "Nothing outside Docker",
          "vpn": "Only through the PIA VPN (kill switch)"}
PIA_IFACE = "pia0"
BAN_SOURCES = ("ssh", "webui", "ftp", "smb")
CC_RE = re.compile(r"^[A-Z]{2}$")
CONTAINER_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")


def default_bans() -> dict:
    return {"enabled": True, "max_retries": 5, "window_min": 10, "ban_min": 60, "ban_lan": False,
            "never": [], "sources": list(BAN_SOURCES)}


NOTIFY_EVENTS = {"bans": "An address was banned", "reverted": "Protection was undone (Keep not pressed)",
                 "lists": "A blocklist or country list failed to update",
                 "new_service": "A new service is listening without a rule"}


def default_notify() -> dict:
    return {k: True for k in NOTIFY_EVENTS}


def default_limits() -> dict:
    return {"syn_flood": True, "syn_per_sec": 50, "ping_per_sec": 10}


NAME_RE = re.compile(r"^[\w .:/()+@&'-]{1,60}$")


def default_config() -> dict:
    return {"version": VERSION, "mode": "off", "lan_networks": list(DEFAULT_LAN), "rules": [],
            "log_blocked": True, "learn_since": None, "bans": default_bans(),
            "blocklists": [dict(b) for b in DEFAULT_BLOCKLISTS], "geo": {"mode": "off", "countries": []},
            "limits": default_limits(), "egress": {}, "notify": default_notify()}


# ── validation ────────────────────────────────────────────────────────────────

def parse_ports(text) -> list[tuple[int, int]]:
    """'80, 443, 8000-8010' -> [(80, 80), (443, 443), (8000, 8010)]. Raises ValueError with a sentence."""
    text = str(text or "").strip()
    if not text:
        raise ValueError("Enter at least one port, e.g. 443 or 8000-8010.")
    out = []
    for part in [p.strip() for p in text.split(",") if p.strip()]:
        m = re.fullmatch(r"(\d{1,5})(?:\s*-\s*(\d{1,5}))?", part)
        if not m:
            raise ValueError(f"'{part}' is not a port or a range like 8000-8010.")
        lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        if not (1 <= lo <= 65535 and 1 <= hi <= 65535):
            raise ValueError(f"Ports are 1 to 65535 ('{part}').")
        if hi < lo:
            raise ValueError(f"In '{part}' the first port must be the smaller one.")
        out.append((lo, hi))
    if len(out) > 64:
        raise ValueError("At most 64 ports or ranges per rule.")
    return sorted(set(out))


def ports_text(ranges) -> str:
    return ", ".join(str(lo) if lo == hi else f"{lo}-{hi}" for lo, hi in ranges)


def _networks(items, label) -> list[str]:
    out = []
    for raw in items or []:
        raw = str(raw).strip()
        if not raw:
            continue
        try:
            net = ipaddress.ip_network(raw, strict=False)
        except ValueError:
            raise ValueError(f"{label}: '{raw}' is not an IP address or network (e.g. 192.168.1.0/24).")
        out.append(str(net))
    if len(out) > 256:
        raise ValueError(f"{label}: at most 256 entries.")
    return sorted(set(out), key=lambda n: (ipaddress.ip_network(n).version, n))


def clean_rule(r: dict, idx: int = 0) -> dict:
    if not isinstance(r, dict):
        raise ValueError("Each rule must be an object.")
    name = str(r.get("name") or "").strip() or f"Rule {idx + 1}"
    if not NAME_RE.match(name):
        raise ValueError(f"Rule name '{name[:40]}' has characters that are not allowed (letters, digits, spaces, . : / ( ) + @ & ' - _).")
    proto = str(r.get("proto") or "tcp")
    if proto not in PROTOS:
        raise ValueError(f"{name}: protocol must be tcp, udp or both.")
    source = str(r.get("source") or "lan")
    if source not in SOURCES:
        raise ValueError(f"{name}: 'who can connect' must be one of {', '.join(SOURCES)}.")
    try:
        ranges = parse_ports(r.get("ports"))
    except ValueError as e:
        raise ValueError(f"{name}: {e}")
    cidrs = _networks(r.get("cidrs"), name) if source == "custom" else []
    if source == "custom" and not cidrs:
        raise ValueError(f"{name}: add at least one address or network for 'specific addresses'.")
    try:
        rate = int(r.get("rate_per_min") or 0)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: the connection limit must be a whole number per minute.")
    if not 0 <= rate <= 10000:
        raise ValueError(f"{name}: the connection limit must be 0 (none) to 10000 per minute.")
    rid = str(r.get("id") or f"r{idx + 1}")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", rid):
        rid = f"r{idx + 1}"
    return {"id": rid, "name": name, "enabled": bool(r.get("enabled", True)), "proto": proto,
            "ports": ports_text(ranges), "source": source, "cidrs": cidrs,
            "note": str(r.get("note") or "")[:200], "learned": bool(r.get("learned")), "rate_per_min": rate}


def clean_config(cfg: dict) -> dict:
    if not isinstance(cfg, dict):
        raise ValueError("Firewall settings must be an object.")
    mode = str(cfg.get("mode") or "off")
    if mode not in MODES:
        raise ValueError("Mode must be off, learn or enforce.")
    rules_in = cfg.get("rules") or []
    if not isinstance(rules_in, list) or len(rules_in) > 500:
        raise ValueError("Rules must be a list of at most 500.")
    rules, seen = [], set()
    for i, r in enumerate(rules_in):
        c = clean_rule(r, i)
        while c["id"] in seen:
            c["id"] = f"{c['id']}x"
        seen.add(c["id"])
        rules.append(c)
    lan = _networks(cfg.get("lan_networks") if cfg.get("lan_networks") is not None else DEFAULT_LAN, "LAN networks")
    if not lan:
        raise ValueError("LAN networks cannot be empty (SSH and the web UI are always allowed from them).")
    ls = cfg.get("learn_since")
    return {"version": VERSION, "mode": mode, "lan_networks": lan, "rules": rules,
            "log_blocked": bool(cfg.get("log_blocked", True)),
            "learn_since": int(ls) if isinstance(ls, (int, float)) else None,
            "bans": _clean_bans(cfg.get("bans")), "blocklists": _clean_blocklists(cfg.get("blocklists")),
            "geo": _clean_geo(cfg.get("geo")), "limits": _clean_limits(cfg.get("limits")),
            "egress": _clean_egress(cfg.get("egress")), "notify": _clean_notify(cfg.get("notify"))}


def _clean_notify(n) -> dict:
    d = default_notify()
    if n is None:
        return d
    if not isinstance(n, dict):
        raise ValueError("Notification settings must be an object.")
    return {k: bool(n.get(k, d[k])) for k in NOTIFY_EVENTS}


def _int(v, lo, hi, label):
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a whole number.")
    if not lo <= n <= hi:
        raise ValueError(f"{label} must be between {lo} and {hi}.")
    return n


def _clean_bans(b) -> dict:
    d = default_bans()
    if b is None:
        return d
    if not isinstance(b, dict):
        raise ValueError("Ban settings must be an object.")
    src = [x for x in (b.get("sources") if b.get("sources") is not None else d["sources"]) if x in BAN_SOURCES]
    return {"enabled": bool(b.get("enabled", d["enabled"])),
            "max_retries": _int(b.get("max_retries", d["max_retries"]), 2, 100, "Failed logins before a ban"),
            "window_min": _int(b.get("window_min", d["window_min"]), 1, 1440, "Counting window (minutes)"),
            "ban_min": _int(b.get("ban_min", d["ban_min"]), 1, 525600, "Ban length (minutes)"),
            "ban_lan": bool(b.get("ban_lan", False)),
            "never": _networks(b.get("never"), "Never ban"), "sources": src}


def _clean_blocklists(lists) -> list[dict]:
    if lists is None:
        return [dict(b) for b in DEFAULT_BLOCKLISTS]
    if not isinstance(lists, list) or len(lists) > 20:
        raise ValueError("At most 20 blocklists.")
    out, seen = [], set()
    for b in lists:
        if not isinstance(b, dict):
            raise ValueError("Each blocklist must be an object.")
        bid = str(b.get("id") or "")
        if not re.fullmatch(r"[a-z0-9_]{1,32}", bid) or bid in seen:
            raise ValueError(f"Blocklist id '{bid[:32]}' is not valid or used twice.")
        url = str(b.get("url") or "").strip()
        if not re.fullmatch(r"https://[A-Za-z0-9.-]+(/[A-Za-z0-9._~%/+-]*)?", url):
            raise ValueError(f"Blocklist '{bid}': the address must be a plain https:// URL.")
        name = str(b.get("name") or bid)[:80]
        seen.add(bid)
        out.append({"id": bid, "name": name, "url": url, "enabled": bool(b.get("enabled"))})
    return out


def _clean_geo(g) -> dict:
    if g is None:
        return {"mode": "off", "countries": []}
    if not isinstance(g, dict):
        raise ValueError("Country settings must be an object.")
    mode = str(g.get("mode") or "off")
    if mode not in ("off", "block", "allow"):
        raise ValueError("Country mode must be off, block or allow.")
    cc = sorted({str(c).strip().upper() for c in (g.get("countries") or []) if str(c).strip()})
    bad = [c for c in cc if not CC_RE.match(c)]
    if bad:
        raise ValueError(f"Not a two-letter country code: {', '.join(bad[:5])}.")
    if len(cc) > 250:
        raise ValueError("Too many countries.")
    if mode != "off" and not cc:
        raise ValueError("Pick at least one country, or turn country filtering off.")
    return {"mode": mode, "countries": cc}


def _clean_limits(l) -> dict:
    d = default_limits()
    if l is None:
        return d
    if not isinstance(l, dict):
        raise ValueError("Limit settings must be an object.")
    return {"syn_flood": bool(l.get("syn_flood", d["syn_flood"])),
            "syn_per_sec": _int(l.get("syn_per_sec", d["syn_per_sec"]), 5, 100000, "New connections per second per address"),
            "ping_per_sec": _int(l.get("ping_per_sec", d["ping_per_sec"]), 0, 10000, "Pings per second")}


def _clean_egress(e) -> dict:
    if not e:
        return {}
    if not isinstance(e, dict) or len(e) > 500:
        raise ValueError("Outbound rules must be an object of at most 500 containers.")
    out = {}
    for name, pol in e.items():
        if not CONTAINER_RE.match(str(name)):
            raise ValueError(f"'{str(name)[:40]}' is not a container name.")
        if pol not in EGRESS:
            raise ValueError(f"{name}: outbound must be one of {', '.join(EGRESS)}.")
        if pol != "internet":
            out[str(name)] = pol
    return out


# ── ruleset ───────────────────────────────────────────────────────────────────

def _split(nets) -> tuple[list[str], list[str]]:
    v4 = [n for n in nets if ipaddress.ip_network(n).version == 4]
    v6 = [n for n in nets if ipaddress.ip_network(n).version == 6]
    return v4, v6


def _set(items) -> str:
    return "{ " + ", ".join(items) + " }"


def _l4(proto: str) -> str:
    return "meta l4proto { tcp, udp }" if proto == "both" else f"meta l4proto {proto}"


def _source_matches(rule: dict) -> list[str]:
    """nft match fragments (one rule line each) for who may connect."""
    src = rule["source"]
    out = []
    if src in ("lan", "lan_vpn"):
        out += ["ip saddr @lan4", "ip6 saddr @lan6"]
    if src in ("vpn", "lan_vpn"):
        out += [f'iifname "{i}"' for i in VPN_IFACES] + ["ip saddr @vpn4"]
    if src == "any":
        out.append("")
    if src == "custom":
        v4, v6 = _split(rule["cidrs"])
        if v4:
            out.append("ip saddr " + _set(v4))
        if v6:
            out.append("ip6 saddr " + _set(v6))
    return out


def _rule_lines(rule: dict, port_expr: str, idx: int = 0) -> list[str]:
    ports = _set([p.replace(" ", "") for p in rule["ports"].split(",")])
    comment = re.sub(r'["\\]', "", rule["name"])[:60]
    out = []
    if rule.get("rate_per_min"):
        # Per source address: more new connections than this per minute to these ports are dropped.
        n = rule["rate_per_min"]
        for fam, key in (("ipv4", "ip"), ("ipv6", "ip6")):
            out.append(f"meta nfproto {fam} {_l4(rule['proto'])} {port_expr} {ports} ct state new "
                       f"add @rl{idx}_{key} {{ {key} saddr limit rate over {n}/minute burst {n} packets }} drop")
    out += [f'{src} {_l4(rule["proto"])} {port_expr} {ports} accept comment "{comment}"'.replace("  ", " ").strip()
            for src in _source_matches(rule)]
    return out


def _elements(items) -> str:
    return ("elements = " + _set(items) + "; ") if items else ""


def generate(cfg: dict, data: dict | None = None) -> str:
    """The full nft script (atomic: create, delete, recreate). Empty string for mode off.

    `data` (gathered by the root helper; all optional): bans {ip: seconds_left}, lists {id: [cidr]},
    geo {cc: [cidr]}, egress_ips {policy: [container ipv4]}. Extras are applied in enforce mode only."""
    cfg = clean_config(cfg)
    data = data or {}
    if cfg["mode"] == "off":
        return ""
    lan4, lan6 = _split(cfg["lan_networks"])
    mode = cfg["mode"]
    enforce = mode == "enforce"
    final = "drop" if enforce else "accept"
    log = ""
    if cfg["log_blocked"]:
        log = f'limit rate 10/second burst 30 packets log prefix "{LOG_PREFIX[mode]}" level info'
    mgmt = _set(str(p) for p in MGMT_TCP)
    rules = [r for r in cfg["rules"] if r["enabled"]]
    lim = cfg["limits"]

    # extras (enforce only)
    bans = {}
    if enforce and cfg["bans"]["enabled"]:
        bans = {ip: int(left) for ip, left in (data.get("bans") or {}).items() if int(left) > 0 and _is_ip(ip)}
    ban4 = [f"{ip} timeout {t}s" for ip, t in sorted(bans.items()) if ipaddress.ip_address(ip).version == 4]
    ban6 = [f"{ip} timeout {t}s" for ip, t in sorted(bans.items()) if ipaddress.ip_address(ip).version == 6]
    blk = []
    if enforce:
        for b in cfg["blocklists"]:
            if b["enabled"]:
                blk += (data.get("lists") or {}).get(b["id"]) or []
    blk4, blk6 = _split(sorted(set(blk)))
    geo_on = enforce and cfg["geo"]["mode"] != "off"
    geo = []
    if geo_on:
        for cc in cfg["geo"]["countries"]:
            geo += (data.get("geo") or {}).get(cc) or []
    geo4, geo6 = _split(sorted(set(geo)))
    eg = {k: [ip for ip in v if _is_ip(ip) and ipaddress.ip_address(ip).version == 4]
          for k, v in (data.get("egress_ips") or {}).items()} if enforce else {}

    L = ["table inet helix {}", "delete table inet helix", "table inet helix {"]
    L.append(f"  set lan4 {{ type ipv4_addr; flags interval; {_elements(lan4)}}}")
    L.append(f"  set lan6 {{ type ipv6_addr; flags interval; {_elements(lan6)}}}")
    L.append(f"  set vpn4 {{ type ipv4_addr; flags interval; elements = {_set(VPN_NETS)}; }}")
    # bans: elements carry their own timeout; the watcher adds more at run time
    L.append(f"  set banned4 {{ type ipv4_addr; flags timeout; {_elements(ban4)}}}")
    L.append(f"  set banned6 {{ type ipv6_addr; flags timeout; {_elements(ban6)}}}")
    L.append(f"  set block4 {{ type ipv4_addr; flags interval; auto-merge; {_elements(blk4)}}}")
    L.append(f"  set block6 {{ type ipv6_addr; flags interval; auto-merge; {_elements(blk6)}}}")
    L.append(f"  set geo4 {{ type ipv4_addr; flags interval; auto-merge; {_elements(geo4)}}}")
    L.append(f"  set geo6 {{ type ipv6_addr; flags interval; auto-merge; {_elements(geo6)}}}")
    for pol in ("lan", "none", "vpn"):
        L.append(f"  set eg_{pol}4 {{ type ipv4_addr; {_elements(eg.get(pol, []))}}}")
    L.append("  set syn4 { type ipv4_addr; flags dynamic, timeout; timeout 10s; }")
    L.append("  set syn6 { type ipv6_addr; flags dynamic, timeout; timeout 10s; }")
    for i, r in enumerate(rules):
        if r.get("rate_per_min"):
            L.append(f"  set rl{i}_ip {{ type ipv4_addr; flags dynamic, timeout; timeout 2m; }}")
            L.append(f"  set rl{i}_ip6 {{ type ipv6_addr; flags dynamic, timeout; timeout 2m; }}")

    def threats(indent="    "):
        """Inbound from outside: bans, blocklists, countries, SYN flood. LAN/VPN are exempt from lists/countries."""
        out = []
        if not enforce:
            return out
        out += [f"{indent}ip saddr @banned4 drop", f"{indent}ip6 saddr @banned6 drop"]
        out += [f"{indent}ip saddr @block4 drop", f"{indent}ip6 saddr @block6 drop"]
        if geo_on and cfg["geo"]["mode"] == "block":
            out += [f"{indent}ip saddr @geo4 ip saddr != @lan4 ip saddr != @vpn4 drop",
                    f"{indent}ip6 saddr @geo6 ip6 saddr != @lan6 drop"]
        if geo_on and cfg["geo"]["mode"] == "allow":
            out += [f"{indent}ip saddr != @geo4 ip saddr != @lan4 ip saddr != @vpn4 drop",
                    f"{indent}ip6 saddr != @geo6 ip6 saddr != @lan6 drop"]
        if lim["syn_flood"]:
            n = lim["syn_per_sec"]
            out += [f"{indent}tcp flags & (fin|syn|rst|ack) == syn ip saddr != @lan4 "
                    f"add @syn4 {{ ip saddr limit rate over {n}/second burst {n * 2} packets }} drop",
                    f"{indent}tcp flags & (fin|syn|rst|ack) == syn ip6 saddr != @lan6 "
                    f"add @syn6 {{ ip6 saddr limit rate over {n}/second burst {n * 2} packets }} drop"]
        return out

    # ── input: this machine ──
    L += ["  chain input {", "    type filter hook input priority filter - 10; policy accept;",
          '    iif "lo" accept']
    if enforce:                                      # banned addresses lose their open connections too
        L += ["    ip saddr @banned4 drop", "    ip6 saddr @banned6 drop"]
    L += ["    ct state established,related accept",
          "    ct state invalid drop"]
    L += [x for x in threats() if "banned" not in x]
    # Numbers / built-in keywords only: protocol *names* come from /etc/protocols, which a minimal system
    # may not have, and then nft rejects the whole ruleset.
    L.append("    meta l4proto 58 accept")                                 # ICMPv6: neighbour discovery, router adverts
    if enforce and lim["ping_per_sec"]:
        p = lim["ping_per_sec"]
        L.append(f"    icmp type echo-request limit rate over {p}/second burst {p * 2} packets drop")
    L += ["    icmp type { echo-request, destination-unreachable, time-exceeded } accept",
          "    udp sport 67 udp dport 68 accept",                       # DHCPv4 replies
          "    udp sport 547 udp dport 546 accept",                     # DHCPv6 replies
          "    ip saddr @lan4 fib daddr type { broadcast, multicast } accept",   # discovery (mDNS, NetBIOS, SSDP, WSD)
          "    ip6 saddr @lan6 fib daddr type multicast accept",
          "    # SSH and the web UI: always from LAN and VPN (lockout protection)"]
    for src in ["ip saddr @lan4", "ip6 saddr @lan6", "ip saddr @vpn4"] + [f'iifname "{i}"' for i in VPN_IFACES]:
        L.append(f'    {src} tcp dport {mgmt} accept comment "Helix management"')
    for i, r in enumerate(rules):
        L += ["    " + x for x in _rule_lines(r, "th dport", i)]
    if log:
        L.append(f"    {log}")
    L += [f"    {final}", "  }"]

    # ── forward ──
    L += ["  chain forward {", "    type filter hook forward priority filter - 10; policy accept;",
          "    ct state established,related accept"]
    L += [f'    iifname "{i}" jump from_containers' for i in DOCKER_IFACES]
    L += [f'    oifname "{i}" jump to_containers' for i in DOCKER_IFACES]
    L += ["  }", "  chain from_containers {"]
    L += [f'    oifname "{i}" return' for i in DOCKER_IFACES]         # container to container: not outbound
    L += ["    ip saddr @eg_none4 drop",
          "    ip saddr @eg_lan4 ip daddr @lan4 return", "    ip saddr @eg_lan4 drop",
          f'    ip saddr @eg_vpn4 oifname "{PIA_IFACE}" return', "    ip saddr @eg_vpn4 ip daddr @lan4 return",
          "    ip saddr @eg_vpn4 drop",
          "  }", "  chain to_containers {"]
    L += [f'    iifname "{i}" accept' for i in DOCKER_IFACES]        # container to container
    L += ["    ct status dnat jump published", "  }", "  chain published {"]
    L += threats()
    for i, r in enumerate(rules):
        L += ["    " + x for x in _rule_lines(r, "ct original proto-dst", i)]
    if log:
        L.append(f"    {log}")
    L += [f"    {final}", "  }", "}"]
    return "\n".join(L) + "\n"


def _is_ip(x: str) -> bool:
    try:
        ipaddress.ip_address(x)
        return True
    except ValueError:
        return False


# ── listening sockets, learn mode, exposure audit ────────────────────────────

def parse_ss(text: str, ephemeral=(32768, 60999), published=frozenset()) -> list[dict]:
    """`ss -Htulnp` -> [{proto, addr, port, process}] (loopback-only ones included, flagged).

    `ss -u` lists every unconnected UDP socket, including client sockets that happen to sit on a random port in the
    kernel's ephemeral range; those are not services and are skipped (unless Docker publishes that port)."""
    out, seen = [], set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        proto = parts[0].lower()
        if proto not in ("tcp", "udp"):
            continue
        local = parts[4]
        m = re.match(r"^\[?(.*?)\]?(?:%[\w.-]+)?:(\d+|\*)$", local)
        if not m or m.group(2) == "*":
            continue
        addr, port = m.group(1), int(m.group(2))
        proc = ""
        pm = re.search(r'users:\(\("([^"]+)"', line)
        if pm:
            proc = pm.group(1)
        # Client sockets always belong to a process; kernel listeners (WireGuard, NFS) have none and are services.
        if (proto == "udp" and proc and ephemeral[0] <= port <= ephemeral[1]
                and (proto, port) not in published and port not in WELL_KNOWN):
            continue
        loopback = addr in ("127.0.0.1", "::1") or addr.startswith("127.") or addr == "localhost"
        key = (proto, port, loopback)
        if key in seen:
            continue
        seen.add(key)
        out.append({"proto": proto, "addr": addr or "*", "port": port, "process": proc, "loopback": loopback})
    return sorted(out, key=lambda x: (x["port"], x["proto"]))


def parse_docker_ports(text: str) -> dict:
    """`docker ps --format '{{.Names}}\\t{{.Ports}}'` -> {(proto, host_port): container}."""
    out = {}
    for line in text.splitlines():
        if "\t" not in line:
            continue
        name, ports = line.split("\t", 1)
        for m in re.finditer(r"(?:[\d.]+|\[?::\]?):(\d+)(?:-(\d+))?->\d+(?:-\d+)?/(tcp|udp)", ports):
            lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
            for p in range(lo, min(hi, lo + 100) + 1):
                out[(m.group(3), p)] = name
    return out


def _covered(cfg: dict, proto: str, port: int) -> list[dict]:
    hits = []
    for r in cfg["rules"]:
        if not r["enabled"] or (r["proto"] != "both" and r["proto"] != proto):
            continue
        if any(lo <= port <= hi for lo, hi in parse_ports(r["ports"])):
            hits.append(r)
    return hits


def reach(cfg: dict, proto: str, port: int, loopback: bool = False) -> str:
    """Who can connect: 'this machine' | 'nobody' | 'LAN' | 'VPN' | 'LAN and VPN' | 'specific addresses' | 'anyone'."""
    if loopback:
        return "this machine"
    if cfg["mode"] in ("off", "learn"):
        return "anyone"
    who = set()
    if proto == "tcp" and port in MGMT_TCP:
        who |= {"lan", "vpn"}
    for r in _covered(cfg, proto, port):
        who |= {"lan_vpn": {"lan", "vpn"}}.get(r["source"], {r["source"]})
    if "any" in who:
        return "anyone"
    if {"lan", "vpn"} <= who:
        return "LAN and VPN"
    if "lan" in who:
        return "LAN"
    if "vpn" in who:
        return "VPN"
    if "custom" in who:
        return "specific addresses"
    return "nobody"


def audit(cfg: dict, listeners: list[dict], containers: dict) -> list[dict]:
    """One row per listening port: what it is, who can reach it, and a warning when that is risky."""
    cfg = clean_config(cfg)
    rows = []
    for li in listeners:
        owner = containers.get((li["proto"], li["port"])) or ""
        who = reach(cfg, li["proto"], li["port"], li["loopback"])
        warn = ""
        sens = SENSITIVE.get(li["port"])
        if sens and who == "anyone":
            warn = f"{sens} is reachable by anyone who can reach this machine."
        elif sens and who not in ("this machine", "nobody") and li["port"] in (2375, 8082, 3781):
            warn = f"{sens} should not be reachable from other machines."
        covered = bool(_covered(cfg, li["proto"], li["port"])) or (li["proto"] == "tcp" and li["port"] in MGMT_TCP)
        if li["process"] in SOCKET_OWNERS and li["port"] in WELL_KNOWN:
            li = {**li, "process": WELL_KNOWN[li["port"]]}
        rows.append({**li, "container": owner, "reach": who, "warn": warn, "covered": covered or li["loopback"],
                     "sensitive": sens or ""})
    return rows


def learn_rules(cfg: dict, listeners: list[dict], containers: dict) -> list[dict]:
    """LAN-scoped allow rules for everything listening that no rule covers yet (one per name, ports merged)."""
    cfg = clean_config(cfg)
    by_name: dict[tuple[str, str], list[int]] = {}
    for li in listeners:
        if li["loopback"] or (li["proto"] == "tcp" and li["port"] in MGMT_TCP):
            continue
        if _covered(cfg, li["proto"], li["port"]):
            continue
        owner = containers.get((li["proto"], li["port"]))
        proc = li["process"] if li["process"] not in SOCKET_OWNERS else ""
        name = owner or WELL_KNOWN.get(li["port"]) or proc or f"Port {li['port']}"
        if li["process"] in ("docker-proxy",) and not owner:
            name = f"Container port {li['port']}"
        by_name.setdefault((name, li["proto"]), []).append(li["port"])
    merged: dict[str, dict] = {}
    for (name, proto), ports in sorted(by_name.items()):
        key = name
        if key in merged and merged[key]["ports_set"] == set(ports):
            merged[key]["proto"] = "both"
            continue
        if key in merged:
            key = f"{name} ({proto})"
        merged[key] = {"name": key[:60], "proto": proto, "ports_set": set(ports)}
    out = []
    for i, m in enumerate(merged.values()):
        out.append(clean_rule({"id": f"l{int(time.time()) % 100000}{i}", "name": m["name"], "proto": m["proto"],
                               "ports": ", ".join(str(p) for p in sorted(m["ports_set"])),
                               "source": "lan", "learned": True,
                               "note": "Created from what was listening when you reviewed learn mode."}, i))
    return out


def parse_log(lines: list[str]) -> list[dict]:
    """Kernel log lines from our `log prefix` -> [{ts, kind, src, dst, proto, dport, iface}]."""
    out = []
    for line in lines:
        kind = "blocked" if LOG_PREFIX["enforce"].strip() in line else "would block" if LOG_PREFIX["learn"].strip() in line else ""
        if not kind:
            continue
        f = dict(re.findall(r"\b(SRC|DST|PROTO|DPT|SPT|IN)=(\S*)", line))
        ts = None
        m = re.match(r"^\s*\[?(\d+(?:\.\d+)?)", line)
        if m:
            ts = float(m.group(1))
        out.append({"ts": ts, "kind": kind, "src": f.get("SRC", ""), "dst": f.get("DST", ""),
                    "proto": f.get("PROTO", "").lower(), "dport": int(f["DPT"]) if f.get("DPT", "").isdigit() else None,
                    "iface": f.get("IN", "")})
    return out


def parse_cidr_list(text: str, exclude: list[str], cap: int = LIST_CAP) -> list[str]:
    """A downloaded list (one address/network per line, `;`/`#` comments) -> networks, minus anything that
    overlaps `exclude` (your LAN and VPN: FireHOL level 1 lists private ranges, which would block your own LAN)."""
    ex = [ipaddress.ip_network(n) for n in exclude]
    out = []
    for line in text.splitlines():
        tok = re.split(r"[\s;#]", line.strip(), maxsplit=1)[0]
        if not tok:
            continue
        try:
            net = ipaddress.ip_network(tok, strict=False)
        except ValueError:
            continue
        if net.prefixlen == 0 or any(net.version == e.version and net.overlaps(e) for e in ex):
            continue
        out.append(str(net))
        if len(out) >= cap:
            break
    return sorted(set(out))


AUTH_PATTERNS = {
    "ssh": [re.compile(r"Failed (?:password|keyboard-interactive/pam) for (?:invalid user )?\S* ?from (\S+) port"),
            re.compile(r"Invalid user \S* ?from (\S+) port"),
            re.compile(r"maximum authentication attempts exceeded for .* from (\S+) port")],
    "webui": [re.compile(r"helix-auth: login failed user=\S* ip=(\S+)")],
    "ftp": [re.compile(r'FAIL LOGIN: Client "(?:::ffff:)?([^"]+)"')],
    "smb": [re.compile(r"status \[NT_STATUS_(?:WRONG_PASSWORD|NO_SUCH_USER|LOGON_FAILURE|ACCOUNT_LOCKED_OUT)\]"
                       r".*remote host \[ipv[46]:(.+):\d+\]")],
}


def parse_auth(source: str, line: str) -> str | None:
    """The client address of a failed login in a log line from `source`, or None."""
    for rx in AUTH_PATTERNS.get(source, []):
        m = rx.search(line)
        if m:
            ip = m.group(1).strip("[]")
            if ip.startswith("::ffff:"):
                ip = ip[7:]
            return ip if _is_ip(ip) else None
    return None


def bannable(cfg: dict, ip: str) -> bool:
    """Never ban this machine, the never-ban list, or (unless allowed) LAN/VPN addresses."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.is_loopback or a.is_unspecified:
        return False
    b = cfg["bans"]
    nets = [ipaddress.ip_network(n) for n in b["never"]]
    if not b["ban_lan"]:
        nets += [ipaddress.ip_network(n) for n in cfg["lan_networks"] + VPN_NETS]
    return not any(a in n for n in nets if n.version == a.version)


def new_uncovered(rows: list[dict], seen: dict, now: float) -> tuple[list[dict], dict]:
    """New-service detection for notifications. `rows` = audit(); `seen` = {"proto/port": {"first": t, "count": n,
    "told": bool}} from the last check. A port is reported once, after it has been seen uncovered in two checks in a
    row (a service restarting is not a new service). Returns (rows to report, new seen state)."""
    out, state = [], {}
    for r in rows:
        if r["loopback"] or r["covered"]:
            continue
        key = f"{r['proto']}/{r['port']}"
        prev = seen.get(key) or {"first": now, "count": 0, "told": False}
        cur = {"first": prev["first"], "count": prev["count"] + 1, "told": prev["told"]}
        if cur["count"] >= 2 and not cur["told"]:
            cur["told"] = True
            out.append(r)
        state[key] = cur
    return out, state


def dumps(cfg: dict) -> str:
    return json.dumps(clean_config(cfg), indent=2) + "\n"
