"""
Network.

Live state
GET  /api/network/interfaces       — every interface: kind, state, MAC, addresses, MTU, speed/duplex, driver, counters
GET  /api/network/status           — small summary used by the dashboard tile (name -> ip, up, rx_mb, tx_mb)
GET  /api/network/dns              — resolvers and search domains in use, hostname
GET  /api/network/routes           — routing table (IPv4 + IPv6); static routes added here are flagged
GET  /api/network/listening        — what is listening on which port, and which process owns it

Configuration (network.cfg, applied by helix-apply-network as systemd-networkd units)
GET  /api/network/config
PUT  /api/network/config           — validate, write, apply; with safe=true (default) it reverts by itself unless confirmed
GET  /api/network/pending          — is a change waiting to be confirmed, and for how long
POST /api/network/confirm | revert
POST /api/network/routes           — add a static route (kept across reboots);  DELETE /api/network/routes removes it

Diagnostics
POST /api/network/diag/ping | dns | trace

Avahi (mDNS):  GET /api/network/avahi, POST /api/network/avahi/enable|disable
"""

import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from config import HELIX_CONFIG, STATE_DIR
from services import privileged

NETWORK_CFG = os.path.join(HELIX_CONFIG, "network.cfg")
ROUTES_CFG = os.path.join(HELIX_CONFIG, "network-routes.cfg")
PENDING_FILE = os.path.join(STATE_DIR, "net_pending.json")
BACKUP_CFG = os.path.join(STATE_DIR, "network.cfg.prev")
CONFIRM_SECS = int(os.getenv("NET_CONFIRM_SECS", "90"))     # env override is for tests

# In the image the helper lives in /usr/local/sbin; a source checkout keeps it in <repo>/network.
_HERE = os.path.dirname(os.path.abspath(__file__))
NETWORK_SBIN = os.getenv("NETWORK_SBIN") or next(
    (d for d in ("/usr/local/sbin", os.path.join(_HERE, "..", "..", "..", "network"))
     if os.path.exists(os.path.join(d, "helix-apply-network"))), "/usr/local/sbin")

router = APIRouter(prefix="/api/network", tags=["network"])

_HIDDEN_IF = re.compile(r"^(veth|docker0|br-[0-9a-f]{12}|vnet|virbr|shim)")


# ── helpers ──────────────────────────────────────────────────────────────────

def _run(cmd, timeout=8) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 127, "", str(e))


def _ipj(*args):
    r = _run(["ip", "-j", *args])
    try:
        return json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return []


def _sysread(name: str, leaf: str) -> str:
    try:
        return Path(f"/sys/class/net/{name}/{leaf}").read_text().strip()
    except OSError:
        return ""


def _physical_nics() -> list[str]:
    return sorted(n for n in (os.listdir("/sys/class/net") if os.path.isdir("/sys/class/net") else [])
                  if os.path.exists(f"/sys/class/net/{n}/device") and not _sysread(n, "master"))


# ── live interface data ──────────────────────────────────────────────────────

@router.get("/interfaces")
def interfaces():
    links = {l["ifname"]: l for l in _ipj("-s", "link")}
    addrs = {a["ifname"]: a for a in _ipj("addr")}
    default_dev = next((r.get("dev") for r in _ipj("route") if r.get("dst") == "default"), None)
    out = []
    for name, l in links.items():
        if name == "lo":
            continue
        a = addrs.get(name, {})
        info = l.get("linkinfo") or {}
        phys = os.path.exists(f"/sys/class/net/{name}/device")
        kind = info.get("info_kind") or ("physical" if phys else "virtual")
        if name.startswith("tailscale"):
            kind = "tailscale"
        st = l.get("stats64") or {}
        speed = _sysread(name, "speed")
        v4 = [f"{x['local']}/{x['prefixlen']}" for x in a.get("addr_info", []) if x.get("family") == "inet"]
        v6 = [f"{x['local']}/{x['prefixlen']}" for x in a.get("addr_info", [])
              if x.get("family") == "inet6" and x.get("scope") != "link"]
        drv = ""
        try:
            drv = os.path.basename(os.readlink(f"/sys/class/net/{name}/device/driver"))
        except OSError:
            pass
        out.append({
            "name": name, "kind": kind, "physical": phys, "hidden": bool(_HIDDEN_IF.match(name)),
            "up": "UP" in (l.get("flags") or []) and l.get("operstate") in ("UP", "UNKNOWN"),
            "carrier": _sysread(name, "carrier") == "1", "state": l.get("operstate", ""),
            "mac": l.get("address", ""), "mtu": l.get("mtu"), "master": l.get("master", ""),
            "ipv4": v4, "ipv6": v6,
            "speed_mbps": int(speed) if speed.lstrip("-").isdigit() and int(speed) > 0 else None,
            "duplex": _sysread(name, "duplex") or None, "driver": drv,
            "rx_bytes": (st.get("rx") or {}).get("bytes", 0), "tx_bytes": (st.get("tx") or {}).get("bytes", 0),
            "rx_errors": (st.get("rx") or {}).get("errors", 0), "tx_errors": (st.get("tx") or {}).get("errors", 0),
            "rx_dropped": (st.get("rx") or {}).get("dropped", 0), "tx_dropped": (st.get("tx") or {}).get("dropped", 0),
            "default_route": name == default_dev,
        })
    order = {"physical": 0, "bond": 1, "bridge": 2, "vlan": 3}
    out.sort(key=lambda i: (i["hidden"], order.get(i["kind"], 9), i["name"]))
    return {"interfaces": out, "primary": default_dev, "time": time.time(), "physical": _physical_nics()}


@router.get("/status")
def network_status():
    """name -> {ip, up, rx_mb, tx_mb}: the small shape the dashboard tile reads."""
    import psutil
    ifaces = {}
    counters = psutil.net_io_counters(pernic=True)
    addrs = psutil.net_if_addrs()
    stats_ = psutil.net_if_stats()
    for name, cnt in counters.items():
        if name == "lo":
            continue
        ip = next((a.address for a in addrs.get(name, []) if a.family.name == "AF_INET"), None)
        ifaces[name] = {"ip": ip, "up": stats_[name].isup if name in stats_ else False,
                        "rx_mb": round(cnt.bytes_recv / 1e6, 1), "tx_mb": round(cnt.bytes_sent / 1e6, 1)}
    gw = next((r.get("gateway") for r in _ipj("route") if r.get("dst") == "default"), None)
    return {"interfaces": ifaces, "routes": {"gateway": gw}}


@router.get("/dns")
def dns_info():
    servers, domains = [], []
    r = _run(["resolvectl", "dns"])
    if r.returncode == 0:
        servers = list(dict.fromkeys(re.findall(r"(?<![\w.:])((?:\d{1,3}\.){3}\d{1,3}|[0-9a-fA-F:]{3,39})(?![\w.:])",
                                                re.sub(r"^.*?:\s*", "", r.stdout, flags=re.M))))
    r = _run(["resolvectl", "domain"])
    if r.returncode == 0:
        domains = list(dict.fromkeys(w for w in re.sub(r"^.*?:\s*", "", r.stdout, flags=re.M).split() if "." in w or w == "~."))
    if not servers:
        try:
            servers = [m for m in re.findall(r"^nameserver\s+(\S+)", Path("/etc/resolv.conf").read_text(), re.M)
                       if not m.startswith("127.")]
        except OSError:
            pass
    return {"servers": servers, "domains": domains, "hostname": socket.gethostname(),
            "fqdn": socket.getfqdn()}


# ── routes ───────────────────────────────────────────────────────────────────

def _load_static_routes() -> list[dict]:
    out = []
    try:
        for line in Path(ROUTES_CFG).read_text().splitlines():
            p = line.split("#")[0].split()
            if len(p) >= 2:
                out.append({"dest": p[0], "gateway": p[1], "metric": int(p[2]) if len(p) > 2 and p[2].isdigit() else 0})
    except OSError:
        pass
    return out


def _save_static_routes(routes: list[dict]) -> None:
    body = "# Static routes added in the web UI: DEST GATEWAY [METRIC]\n" + "".join(
        f"{r['dest']} {r['gateway']}" + (f" {r['metric']}" if r.get("metric") else "") + "\n" for r in routes)
    Path(ROUTES_CFG).write_text(body)


@router.get("/routes")
def routes():
    static = {(r["dest"], r["gateway"]) for r in _load_static_routes()}
    out = []
    for fam, args in (("IPv4", ["route"]), ("IPv6", ["-6", "route"])):
        for r in _ipj(*args):
            dst = r.get("dst", "")
            if dst.startswith("fe80") or r.get("type") in ("local", "broadcast", "multicast"):
                continue
            out.append({"family": fam, "dest": dst, "gateway": r.get("gateway", ""), "dev": r.get("dev", ""),
                        "metric": r.get("metric", 0), "protocol": r.get("protocol", ""), "scope": r.get("scope", ""),
                        "src": r.get("prefsrc", ""), "static": (dst, r.get("gateway", "")) in static})
    return {"routes": out}


class RouteRequest(BaseModel):
    dest: str
    gateway: str
    metric: int = 0


def _check_route(req: RouteRequest) -> tuple[str, str]:
    try:
        dest = str(ipaddress.ip_network(req.dest.strip(), strict=False))
        gw = str(ipaddress.ip_address(req.gateway.strip()))
    except ValueError as e:
        raise HTTPException(400, f"Not a valid route: {e}")
    if ipaddress.ip_network(dest).version != ipaddress.ip_address(gw).version:
        raise HTTPException(400, "The destination and the gateway must both be IPv4 or both IPv6")
    if not 0 <= req.metric <= 9999:
        raise HTTPException(400, "Metric must be between 0 and 9999")
    return dest, gw


@router.post("/routes")
def add_route(req: RouteRequest):
    dest, gw = _check_route(req)
    cmd = ["ip", "route", "replace", dest, "via", gw] + (["metric", str(req.metric)] if req.metric else [])
    if ipaddress.ip_network(dest).version == 6:
        cmd.insert(1, "-6")
    r = privileged.run(cmd)
    if r.returncode != 0:
        raise HTTPException(400, (r.stderr or "The system rejected the route").strip())
    rs = [x for x in _load_static_routes() if (x["dest"], x["gateway"]) != (dest, gw)]
    rs.append({"dest": dest, "gateway": gw, "metric": req.metric})
    _save_static_routes(rs)
    return {"ok": True}


@router.delete("/routes")
def delete_route(req: RouteRequest):
    dest, gw = _check_route(RouteRequest(dest=req.dest, gateway=req.gateway, metric=req.metric))
    cmd = ["ip", "route", "del", dest, "via", gw]
    if ipaddress.ip_network(dest).version == 6:
        cmd.insert(1, "-6")
    privileged.run(cmd)
    _save_static_routes([x for x in _load_static_routes() if (x["dest"], x["gateway"]) != (dest, gw)])
    return {"ok": True}


# ── listening ports ──────────────────────────────────────────────────────────

@router.get("/listening")
def listening():
    r = privileged.run(["ss", "-H", "-tulnp"])
    if r.returncode != 0 and not r.stdout:
        raise HTTPException(500, (r.stderr or "ss failed").strip())
    rows = []
    for line in r.stdout.splitlines():
        p = line.split()
        if len(p) < 5:
            continue
        local = p[4]
        addr, _, port = local.rpartition(":")
        addr = addr.strip("[]")
        m = re.search(r'\(\("([^"]+)",pid=(\d+)', line)
        rows.append({"proto": p[0], "address": addr, "port": int(port) if port.isdigit() else 0,
                     "process": m.group(1) if m else "", "pid": int(m.group(2)) if m else None,
                     "scope": "this machine only" if addr.startswith("127.") or addr == "::1"
                     else "every interface" if addr in ("0.0.0.0", "::", "*") else addr})
    rows.sort(key=lambda x: (x["port"], x["proto"]))
    return {"listening": rows}


# ── config ───────────────────────────────────────────────────────────────────

def _parse_cfg() -> dict:
    defaults = {
        "ifname": "", "use_dhcp": "yes", "ipaddr": "", "netmask": "255.255.255.0",
        "gateway": "", "dhcp_hostname": "", "dns_server1": "", "dns_server2": "",
        "domain_name": "", "mtu": "0", "bonding": "no", "bond_name": "bond0",
        "bond_members": "", "bonding_mode": "1", "bonding_miimon": "100",
        "bridging": "no", "bridge_name": "br0", "vlan_id": "0", "use_dhcp6": "auto",
    }
    try:
        content = Path(NETWORK_CFG).read_text()
    except FileNotFoundError:
        return defaults
    result = dict(defaults)
    in_section = False
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line == "[network]":
            in_section = True
            continue
        if line.startswith("[") and in_section:
            break
        if in_section and "=" in line:
            k, _, v = line.partition("=")
            k = k.strip().lower()
            v = v.strip().strip('"')
            if k in result:
                result[k] = v
    return result


def _write_cfg(fields: dict) -> None:
    """Merge fields into network.cfg, preserving comments. Values were validated, so none can hold a newline."""
    try:
        content = Path(NETWORK_CFG).read_text()
    except FileNotFoundError:
        content = "[network]\n"
    for key, val in fields.items():
        upper = key.upper()
        new_line = f"{upper}={val}"
        pattern = rf"(?m)^({upper}\s*=.*)$"
        if re.search(pattern, content, re.IGNORECASE):
            content = re.sub(pattern, lambda _m: new_line, content, flags=re.IGNORECASE)
        else:
            content = re.sub(r"(\[network\][^\[]*)", lambda m: m.group(1).rstrip("\n") + "\n" + new_line + "\n", content, count=1)
    Path(NETWORK_CFG).write_text(content)


def _apply() -> dict:
    script = os.path.join(NETWORK_SBIN, "helix-apply-network")
    if not os.path.exists(script):
        return {"ok": False, "error": f"Script not found: {script}"}
    r = privileged.run(["env", f"HELIX_CONFIG={HELIX_CONFIG}", "/bin/bash", script], timeout=40)
    return {"ok": r.returncode == 0, "stdout": r.stdout, "stderr": r.stderr}


@router.get("/config")
def get_config():
    return _parse_cfg()


class NetworkUpdate(BaseModel):
    use_dhcp:       str | None = None
    ipaddr:         str | None = None
    netmask:        str | None = None
    gateway:        str | None = None
    dhcp_hostname:  str | None = None
    dns_server1:    str | None = None
    dns_server2:    str | None = None
    domain_name:    str | None = None
    mtu:            str | None = None
    ifname:         str | None = None
    bonding:        str | None = None
    bond_name:      str | None = None
    bond_members:   str | None = None
    bonding_mode:   str | None = None
    bonding_miimon: str | None = None
    bridging:       str | None = None
    bridge_name:    str | None = None
    vlan_id:        str | None = None
    use_dhcp6:      str | None = None
    apply:          bool = True
    safe:           bool = True      # revert by itself unless confirmed within CONFIRM_SECS


_HOSTLABEL = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_DOMAIN = re.compile(r"^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def _validate(merged: dict) -> dict:
    """Return the cleaned values, or raise HTTP 400 naming the field."""
    def bad(field, msg):
        raise HTTPException(400, f"{field}: {msg}")

    c = dict(merged)
    nics = set(_physical_nics())
    for k in ("use_dhcp", "bonding", "bridging"):
        if c[k] not in ("yes", "no"):
            bad(k, "must be yes or no")
    if c["use_dhcp6"] not in ("auto", "yes", "no"):
        bad("IPv6", "must be automatic, yes or no")
    if c["ifname"] and not (re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", c["ifname"]) and os.path.exists(f"/sys/class/net/{c['ifname']}")):
        bad("Interface", f"'{c['ifname']}' does not exist on this machine")

    # IPv4
    if c["use_dhcp"] == "no":
        try:
            ip = ipaddress.IPv4Address(c["ipaddr"])
        except ValueError:
            bad("IP address", "enter a valid IPv4 address such as 192.168.1.50")
        nm = c["netmask"].lstrip("/")
        try:
            net = ipaddress.IPv4Network(f"0.0.0.0/{nm}")
        except ValueError:
            bad("Netmask", "enter a mask like 255.255.255.0 or a prefix like 24")
        if not nm.isdigit() and str(net.netmask) != nm:
            bad("Netmask", "that is a wildcard mask; enter a netmask like 255.255.255.0 or a prefix like 24")
        c["netmask"] = str(net.netmask)
        if net.prefixlen == 0:
            bad("Netmask", "a prefix of 0 is not usable")
        if ip.is_multicast or ip.is_loopback or ip.is_unspecified:
            bad("IP address", "that address cannot be used for a machine")
        iface = ipaddress.IPv4Interface(f"{ip}/{net.prefixlen}")
        if c["gateway"]:
            try:
                gw = ipaddress.IPv4Address(c["gateway"])
            except ValueError:
                bad("Gateway", "enter a valid IPv4 address")
            if gw not in iface.network:
                bad("Gateway", f"{gw} is not inside {iface.network}, so this machine could not reach it")
            if gw == ip:
                bad("Gateway", "cannot be this machine's own address")
        if iface.network.prefixlen < 31 and ip in (iface.network.network_address, iface.network.broadcast_address):
            bad("IP address", "that is the network or broadcast address")
    else:
        c["ipaddr"] = c["gateway"] = ""          # DHCP ignores them; do not leave stale values around

    for k, label in (("dns_server1", "DNS server 1"), ("dns_server2", "DNS server 2")):
        if c[k]:
            try:
                ipaddress.ip_address(c[k])
            except ValueError:
                bad(label, "enter an IPv4 or IPv6 address")
    if c["dns_server2"] and not c["dns_server1"]:
        bad("DNS server 2", "set DNS server 1 first")
    if c["domain_name"] and not _DOMAIN.fullmatch(c["domain_name"]):
        bad("Domain", "can only contain letters, digits, '-' and '.'")
    if c["dhcp_hostname"] and not _HOSTLABEL.fullmatch(c["dhcp_hostname"]):
        bad("DHCP hostname", "can only contain letters, digits and '-'")
    if not c["mtu"].isdigit() or not (c["mtu"] == "0" or 576 <= int(c["mtu"]) <= 9216):
        bad("MTU", "use 0 (default) or a value between 576 and 9216")

    if c["bonding"] == "yes":
        members = c["bond_members"].split()
        if len(members) < 2:
            bad("Bonding", "choose at least two network ports to bond")
        if len(set(members)) != len(members):
            bad("Bonding", "a port is listed twice")
        missing = [m for m in members if m not in nics]
        if missing:
            bad("Bonding", f"not a physical network port here: {', '.join(missing)}")
        if not re.fullmatch(r"bond[0-9]{1,2}", c["bond_name"]):
            bad("Bond name", "use a name like bond0")
        if c["bonding_mode"] not in {"0", "1", "2", "3", "4", "5", "6"}:
            bad("Bond mode", "must be 0 to 6")
        if not c["bonding_miimon"].isdigit() or not 10 <= int(c["bonding_miimon"]) <= 10000:
            bad("Link monitoring", "must be between 10 and 10000 ms")
    else:
        c["bond_members"] = ""
    if c["bridging"] == "yes" and not re.fullmatch(r"br[0-9]{1,2}", c["bridge_name"]):
        bad("Bridge name", "use a name like br0")
    if not c["vlan_id"].isdigit() or not 0 <= int(c["vlan_id"]) <= 4094:
        bad("VLAN", "must be 0 (none) to 4094")
    return c


@router.put("/config")
def update_config(update: NetworkUpdate, background_tasks: BackgroundTasks):
    fields = {k: v for k, v in update.model_dump(exclude={"apply", "safe"}).items() if v is not None}
    if not fields:
        raise HTTPException(400, "No fields to update")
    merged = _validate({**_parse_cfg(), **{k: str(v).strip() for k, v in fields.items()}})
    fields = {k: merged[k] for k in set(fields) | {"ipaddr", "gateway", "netmask", "bond_members"} if k in merged}
    safe = update.safe and update.apply
    if safe:
        _arm_pending()
    try:
        _write_cfg(fields)
    except OSError as e:
        _clear_pending()
        raise HTTPException(500, f"Could not save the network settings: {e.strerror or e}")
    if update.apply:
        background_tasks.add_task(_apply_later)
    return {"ok": True, "updated": sorted(fields), "pending": safe, "confirm_within": CONFIRM_SECS if safe else 0}


def _apply_later() -> None:
    time.sleep(1.0)               # let the HTTP response reach the browser before the link may flap
    _apply()


@router.post("/apply")
def apply_network():
    result = _apply()
    if not result["ok"]:
        raise HTTPException(500, result.get("stderr") or result.get("error") or "Apply failed")
    return result


# ── safety net: revert unless confirmed ──────────────────────────────────────
_timer: threading.Timer | None = None
_plock = threading.Lock()


def _arm_pending() -> None:
    global _timer
    with _plock:
        try:
            if os.path.exists(NETWORK_CFG):
                shutil.copyfile(NETWORK_CFG, BACKUP_CFG)
                had = True
            else:
                had = False
            os.makedirs(STATE_DIR, exist_ok=True)
            Path(PENDING_FILE).write_text(json.dumps({"deadline": time.time() + CONFIRM_SECS, "had_cfg": had}))
        except OSError:
            return
        if _timer:
            _timer.cancel()
        _timer = threading.Timer(CONFIRM_SECS + 3, _auto_revert)
        _timer.daemon = True
        _timer.start()


def _pending() -> dict | None:
    try:
        return json.loads(Path(PENDING_FILE).read_text())
    except (OSError, ValueError):
        return None


def _clear_pending() -> None:
    global _timer
    with _plock:
        if _timer:
            _timer.cancel()
            _timer = None
        for f in (PENDING_FILE, BACKUP_CFG):
            try:
                os.unlink(f)
            except OSError:
                pass


def _do_revert() -> None:
    p = _pending()
    if not p:
        return
    try:
        if p.get("had_cfg") and os.path.exists(BACKUP_CFG):
            shutil.copyfile(BACKUP_CFG, NETWORK_CFG)
        elif not p.get("had_cfg"):
            os.unlink(NETWORK_CFG)
    except OSError:
        pass
    _clear_pending()
    _apply()


def _auto_revert() -> None:
    if _pending():
        _do_revert()


# the backend may have restarted while a change was waiting
_p = _pending()
if _p:
    if time.time() >= _p["deadline"]:
        threading.Thread(target=_do_revert, daemon=True).start()
    else:
        _timer = threading.Timer(_p["deadline"] - time.time() + 3, _auto_revert)
        _timer.daemon = True
        _timer.start()


@router.get("/pending")
def pending():
    p = _pending()
    if not p:
        return {"pending": False}
    return {"pending": True, "seconds_left": max(0, int(p["deadline"] - time.time()))}


@router.post("/confirm")
def confirm():
    if not _pending():
        return {"ok": True, "message": "Nothing to confirm"}
    _clear_pending()
    return {"ok": True}


@router.post("/revert")
def revert(background_tasks: BackgroundTasks):
    if not _pending():
        raise HTTPException(409, "There is no pending change to revert")
    background_tasks.add_task(_do_revert)
    return {"ok": True}


# ── diagnostics ──────────────────────────────────────────────────────────────
_HOST_RE = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*$")


def _host(h: str) -> str:
    h = (h or "").strip()
    if _HOST_RE.fullmatch(h):
        return h
    try:
        return str(ipaddress.ip_address(h))
    except ValueError:
        raise HTTPException(400, "Enter a host name or an IP address")


class DiagRequest(BaseModel):
    host: str
    count: int = 4


@router.post("/diag/ping")
def diag_ping(req: DiagRequest):
    host, n = _host(req.host), max(1, min(req.count, 10))
    r = _run(["ping", "-n", "-c", str(n), "-W", "2", host], timeout=n * 3 + 5)
    return {"ok": r.returncode == 0, "output": (r.stdout + r.stderr).strip()}


@router.post("/diag/trace")
def diag_trace(req: DiagRequest):
    host = _host(req.host)
    if not shutil.which("traceroute"):
        raise HTTPException(501, "traceroute is not installed on this system")
    r = _run(["traceroute", "-n", "-w", "2", "-q", "1", "-m", "20", host], timeout=60)
    return {"ok": r.returncode == 0, "output": (r.stdout + r.stderr).strip()}


class DnsRequest(BaseModel):
    name: str


@router.post("/diag/dns")
def diag_dns(req: DnsRequest):
    name = _host(req.name)
    t = time.time()
    try:
        found = sorted({a[4][0] for a in socket.getaddrinfo(name, None)})
        system = {"ok": True, "addresses": found, "ms": round((time.time() - t) * 1000)}
    except socket.gaierror as e:
        system = {"ok": False, "error": str(e), "ms": round((time.time() - t) * 1000)}
    per_server = []
    for srv in dns_info()["servers"][:4]:
        t = time.time()
        r = _run(["dig", f"@{srv}", name, "+short", "+time=2", "+tries=1"], timeout=8)
        per_server.append({"server": srv, "ok": r.returncode == 0 and bool(r.stdout.strip()),
                           "answer": r.stdout.strip().splitlines()[:4], "ms": round((time.time() - t) * 1000),
                           "error": "" if r.returncode == 0 else (r.stdout + r.stderr).strip()[:120]})
    return {"name": name, "system": system, "servers": per_server}


# ── Avahi / mDNS ─────────────────────────────────────────────────────────────

@router.get("/avahi")
def avahi_status():
    """Return avahi-daemon running state."""
    st = subprocess.run(["systemctl", "is-active", "avahi-daemon"], capture_output=True, text=True)
    active = st.stdout.strip() == "active"
    installed = subprocess.run(["which", "avahi-daemon"], capture_output=True).returncode == 0
    return {"installed": installed, "active": active}


@router.post("/avahi/enable")
def avahi_enable():
    r = subprocess.run(["sudo", "-n", "systemctl", "enable", "--now", "avahi-daemon"],
                       capture_output=True, text=True)
    return {"ok": r.returncode == 0, "output": (r.stdout + r.stderr).strip()}


@router.post("/avahi/disable")
def avahi_disable():
    r = subprocess.run(["sudo", "-n", "systemctl", "disable", "--now", "avahi-daemon"],
                       capture_output=True, text=True)
    return {"ok": r.returncode == 0, "output": (r.stdout + r.stderr).strip()}
