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

NAME_RE = re.compile(r"^[\w .:/()+@&'-]{1,60}$")


def default_config() -> dict:
    return {"version": VERSION, "mode": "off", "lan_networks": list(DEFAULT_LAN), "rules": [],
            "log_blocked": True, "learn_since": None}


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
    rid = str(r.get("id") or f"r{idx + 1}")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", rid):
        rid = f"r{idx + 1}"
    return {"id": rid, "name": name, "enabled": bool(r.get("enabled", True)), "proto": proto,
            "ports": ports_text(ranges), "source": source, "cidrs": cidrs,
            "note": str(r.get("note") or "")[:200], "learned": bool(r.get("learned"))}


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
            "learn_since": int(ls) if isinstance(ls, (int, float)) else None}


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


def _rule_lines(rule: dict, port_expr: str) -> list[str]:
    ports = _set([p.replace(" ", "") for p in rule["ports"].split(",")])
    comment = re.sub(r'["\\]', "", rule["name"])[:60]
    return [f'{src} {_l4(rule["proto"])} {port_expr} {ports} accept comment "{comment}"'.replace("  ", " ").strip()
            for src in _source_matches(rule)]


def generate(cfg: dict) -> str:
    """The full nft script (atomic: create, delete, recreate). Empty string for mode off."""
    cfg = clean_config(cfg)
    if cfg["mode"] == "off":
        return ""
    lan4, lan6 = _split(cfg["lan_networks"])
    mode = cfg["mode"]
    final = "drop" if mode == "enforce" else "accept"
    log = ""
    if cfg["log_blocked"]:
        log = f'limit rate 10/second burst 30 packets log prefix "{LOG_PREFIX[mode]}" level info'
    mgmt = _set(str(p) for p in MGMT_TCP)
    rules = [r for r in cfg["rules"] if r["enabled"]]

    L = ["table inet helix {}", "delete table inet helix", "table inet helix {"]
    L.append(f"  set lan4 {{ type ipv4_addr; flags interval; {('elements = ' + _set(lan4) + '; ') if lan4 else ''}}}")
    L.append(f"  set lan6 {{ type ipv6_addr; flags interval; {('elements = ' + _set(lan6) + '; ') if lan6 else ''}}}")
    L.append(f"  set vpn4 {{ type ipv4_addr; flags interval; elements = {_set(VPN_NETS)}; }}")

    # ── input: this machine ──
    L += ["  chain input {", "    type filter hook input priority filter - 10; policy accept;",
          '    iif "lo" accept',
          "    ct state established,related accept",
          "    ct state invalid drop",
          "    meta l4proto ipv6-icmp accept",                         # neighbour discovery, router adverts
          "    ip protocol icmp icmp type { echo-request, destination-unreachable, time-exceeded } accept",
          "    udp sport 67 udp dport 68 accept",                       # DHCPv4 replies
          "    udp sport 547 udp dport 546 accept",                     # DHCPv6 replies
          "    ip saddr @lan4 fib daddr type { broadcast, multicast } accept",   # discovery (mDNS, NetBIOS, SSDP, WSD)
          "    ip6 saddr @lan6 fib daddr type multicast accept",
          "    # SSH and the web UI: always from LAN and VPN (lockout protection)"]
    for src in ["ip saddr @lan4", "ip6 saddr @lan6", "ip saddr @vpn4"] + [f'iifname "{i}"' for i in VPN_IFACES]:
        L.append(f'    {src} tcp dport {mgmt} accept comment "Helix management"')
    for r in rules:
        L += ["    " + x for x in _rule_lines(r, "th dport")]
    if log:
        L.append(f"    {log}")
    L += [f"    {final}", "  }"]

    # ── forward: only new connections from outside into Docker bridges (published container ports) ──
    L += ["  chain forward {", "    type filter hook forward priority filter - 10; policy accept;",
          "    ct state established,related accept"]
    L += [f'    oifname "{i}" jump to_containers' for i in DOCKER_IFACES]
    L += ["  }", "  chain to_containers {"]
    L += [f'    iifname "{i}" accept' for i in DOCKER_IFACES]        # container to container
    L += ["    ct status dnat jump published", "  }", "  chain published {"]
    for r in rules:
        L += ["    " + x for x in _rule_lines(r, "ct original proto-dst")]
    if log:
        L.append(f"    {log}")
    L += [f"    {final}", "  }", "}"]
    return "\n".join(L) + "\n"


# ── listening sockets, learn mode, exposure audit ────────────────────────────

def parse_ss(text: str) -> list[dict]:
    """`ss -Htulnp` -> [{proto, addr, port, process}] (loopback-only ones included, flagged)."""
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
        name = owner or li["process"] or f"Port {li['port']}"
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


def dumps(cfg: dict) -> str:
    return json.dumps(clean_config(cfg), indent=2) + "\n"
