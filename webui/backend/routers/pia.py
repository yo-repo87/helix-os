"""
Private Internet Access VPN integration.

Uses PIA's public WireGuard API to establish a tunnel on interface pia0,
then routes selected Docker containers through it via a dedicated
helix_pia Docker bridge network + iptables masquerade rules.

Endpoints:
  GET  /api/pia/status
  GET  /api/pia/regions
  GET  /api/pia/settings
  PUT  /api/pia/settings
  POST /api/pia/connect
  POST /api/pia/disconnect
  GET  /api/pia/containers
  POST /api/pia/containers/{id}/route
  DELETE /api/pia/containers/{id}/route
"""

import json
import os
import ssl
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/pia", tags=["pia"])

# Shared paths (persistent /boot/config + the writable state dir). These two used to
# define their own /etc/helix and /var/lib/helix, which the unprivileged backend cannot write.
from config import HELIX_CONFIG, STATE_DIR

PIA_CFG           = os.path.join(HELIX_CONFIG, "pia.cfg")
PIA_STATE_FILE    = os.path.join(STATE_DIR,   "pia_state.json")
PIA_REGIONS_CACHE = os.path.join(STATE_DIR,   "pia_regions.json")

PIA_IFACE      = "pia0"
PIA_WG_CONF    = f"/etc/wireguard/{PIA_IFACE}.conf"
PIA_DOCKER_NET = "helix_pia"
PIA_SUBNET     = "172.28.0.0/16"
PIA_TABLE      = 51821   # routing table / rule priority for the container bridge
PIA_BR         = "br_pia"

PIA_TOKEN_URL   = "https://www.privateinternetaccess.com/gtoken/generateToken"
PIA_SERVER_LIST = "https://serverlist.piaservers.net/vpninfo/servers/v6"
REGIONS_TTL     = 3600  # seconds

_IS_ROOT = os.geteuid() == 0


def _prun(*args, timeout: int = 30) -> tuple[int, str, str]:
    """Run a privileged command. The backend is the unprivileged helix user, which
    has passwordless sudo, so no root-only gate is needed."""
    return _run(*(args if _IS_ROOT else ("sudo", "-n", *args)), timeout=timeout)


def _write_root_file(path: str, content: str, mode: str = "600") -> None:
    """Write a root-owned file (e.g. /etc/wireguard/pia0.conf) via sudo."""
    if _IS_ROOT:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(content)
        os.chmod(path, int(mode, 8))
        return
    rc, _, err = _prun("mkdir", "-p", os.path.dirname(path))
    r = subprocess.run(["sudo", "-n", "tee", path], input=content, capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        raise RuntimeError(f"cannot write {path}: {r.stderr.strip() or 'permission denied'}")
    _prun("chmod", mode, path)


def _run(*args, timeout: int = 30) -> tuple[int, str, str]:
    try:
        r = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


# ── Config helpers ────────────────────────────────────────────────────────────

def _load_cfg() -> dict:
    cfg: dict = {}
    try:
        for line in Path(PIA_CFG).read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return cfg


def _save_cfg(updates: dict):
    cfg = _load_cfg()
    cfg.update(updates)
    os.makedirs(os.path.dirname(PIA_CFG), exist_ok=True)
    Path(PIA_CFG).write_text("".join(f"{k}={v}\n" for k, v in cfg.items()))


def _load_state() -> dict:
    try:
        return json.loads(Path(PIA_STATE_FILE).read_text())
    except Exception:
        return {}


def _save_state(state: dict):
    os.makedirs(os.path.dirname(PIA_STATE_FILE) or ".", exist_ok=True)
    Path(PIA_STATE_FILE).write_text(json.dumps(state, indent=2))


# ── PIA API helpers ───────────────────────────────────────────────────────────

def _curl_quote(s: str) -> str:
    """Quote a value for a curl config file (-K): backslash and double quote."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _get_token(username: str, password: str) -> str:
    # PIA's token endpoint sits behind Cloudflare, which answers 403 to Python's
    # urllib/requests TLS fingerprint even with valid credentials (curl gets 200).
    # So shell out to curl, and pass the credentials on stdin so they never appear
    # in a process listing.
    cfg = f'user = "{_curl_quote(username)}:{_curl_quote(password)}"\n'
    try:
        r = subprocess.run(["curl", "-sS", "-m", "20", "-K", "-", PIA_TOKEN_URL],
                           input=cfg, capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"PIA token request failed: {e}")
    if r.returncode != 0:
        raise RuntimeError(f"PIA token request failed: {r.stderr.strip() or 'curl exit ' + str(r.returncode)}")
    try:
        result = json.loads(r.stdout)
    except ValueError:
        raise RuntimeError(f"PIA token request failed: unexpected reply {r.stdout[:80]!r}")
    if result.get("status") != "OK":
        raise RuntimeError(f"PIA login failed: {result.get('message', 'invalid credentials')}")
    return result["token"]


def _fetch_regions() -> list[dict]:
    cache = Path(PIA_REGIONS_CACHE)
    if cache.exists() and (time.time() - cache.stat().st_mtime) < REGIONS_TTL:
        try:
            return json.loads(cache.read_text())
        except Exception:
            pass

    try:
        with urllib.request.urlopen(PIA_SERVER_LIST, timeout=15) as resp:
            raw = resp.read().decode()
    except Exception as e:
        raise RuntimeError(f"Failed to fetch PIA server list: {e}")

    # Response = JSON + newline + RSA signature; grab only the JSON part
    data = json.loads(raw.split("\n")[0])

    regions = []
    for r in data.get("regions", []):
        wg_servers = r.get("servers", {}).get("wg", [])
        if not wg_servers:
            continue
        regions.append({
            "id":      r["id"],
            "name":    r["name"],
            "country": r.get("country", ""),
            "server":  wg_servers[0],  # {ip, cn}
        })

    regions.sort(key=lambda x: x["name"])
    os.makedirs(str(cache.parent), exist_ok=True)
    cache.write_text(json.dumps(regions))
    return regions


def _register_key(server_ip: str, token: str, pubkey: str) -> dict:
    params = urllib.parse.urlencode({"pt": token, "pubkey": pubkey})
    url = f"https://{server_ip}:1337/addKey?{params}"
    # PIA uses a self-signed cert with a specific CN — disable verification per PIA docs
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(url, context=ctx, timeout=15) as resp:
            result = json.loads(resp.read())
    except Exception as e:
        raise RuntimeError(f"PIA WireGuard key registration failed: {e}")
    if result.get("status") != "OK":
        raise RuntimeError(f"PIA addKey error: {result.get('message', 'unknown')}")
    return result


def _genkey() -> tuple[str, str]:
    rc, privkey, err = _run("wg", "genkey")
    if rc != 0:
        raise RuntimeError(f"wg genkey failed — is wireguard-tools installed? {err}")
    privkey = privkey.strip()
    result = subprocess.run(["wg", "pubkey"], input=privkey + "\n",
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError("wg pubkey failed")
    return privkey, result.stdout.strip()


def _build_wg_conf(privkey: str, peer_ip: str, dns: str,
                   server_key: str, server_ip: str, server_port: int) -> str:
    return (
        f"[Interface]\n"
        f"PrivateKey = {privkey}\n"
        f"Address = {peer_ip}/32\n"
        # Do NOT take over the host: no default-route hijack and no DNS rewrite.
        # Only traffic from the PIA Docker bridge subnet is routed through the tunnel.
        f"Table = off\n"
        f"PostUp = ip route replace default dev {PIA_IFACE} table {PIA_TABLE}; "
        f"ip rule add from {PIA_SUBNET} table {PIA_TABLE} priority {PIA_TABLE}\n"
        f"PostDown = ip rule del from {PIA_SUBNET} table {PIA_TABLE} priority {PIA_TABLE}; "
        f"ip route flush table {PIA_TABLE}\n\n"
        f"[Peer]\n"
        f"PublicKey = {server_key}\n"
        f"AllowedIPs = 0.0.0.0/0, ::/0\n"
        f"Endpoint = {server_ip}:{server_port}\n"
        f"PersistentKeepalive = 25\n"
    )


# ── Docker + iptables helpers ─────────────────────────────────────────────────

def _ensure_docker_net():
    rc, _, _ = _run("docker", "network", "inspect", PIA_DOCKER_NET)
    if rc == 0:
        return
    _run("docker", "network", "create",
         "--driver", "bridge",
         "--subnet", PIA_SUBNET,
         "--opt", f"com.docker.network.bridge.name={PIA_BR}",
         PIA_DOCKER_NET)


def _setup_iptables():
    _prun("sysctl", "-w", "net.ipv4.ip_forward=1")

    nat_check = ["iptables", "-t", "nat", "-C", "POSTROUTING",
                 "-s", PIA_SUBNET, "-o", PIA_IFACE, "-j", "MASQUERADE"]
    if _prun(*nat_check)[0] != 0:
        _prun("iptables", "-t", "nat", "-A", "POSTROUTING",
             "-s", PIA_SUBNET, "-o", PIA_IFACE, "-j", "MASQUERADE")

    for fwd in [
        ["-i", PIA_BR, "-o", PIA_IFACE, "-j", "ACCEPT"],
        ["-i", PIA_IFACE, "-o", PIA_BR, "-m", "state",
         "--state", "RELATED,ESTABLISHED", "-j", "ACCEPT"],
    ]:
        if _prun("iptables", "-C", "FORWARD", *fwd)[0] != 0:
            _prun("iptables", "-A", "FORWARD", *fwd)


def _teardown_iptables():
    for args in [
        ["-t", "nat", "-D", "POSTROUTING",
         "-s", PIA_SUBNET, "-o", PIA_IFACE, "-j", "MASQUERADE"],
        ["-D", "FORWARD",
         "-i", PIA_BR, "-o", PIA_IFACE, "-j", "ACCEPT"],
        ["-D", "FORWARD",
         "-i", PIA_IFACE, "-o", PIA_BR, "-m", "state",
         "--state", "RELATED,ESTABLISHED", "-j", "ACCEPT"],
    ]:
        _prun("iptables", *args)


def _routed_containers() -> list[dict]:
    rc, out, _ = _run("docker", "network", "inspect", PIA_DOCKER_NET,
                      "--format", "{{json .Containers}}")
    if rc != 0 or not out.strip() or out.strip() in ("null", "{}"):
        return []
    try:
        ctrs = json.loads(out.strip())
        return [
            {"id": cid[:12], "name": info.get("Name", ""), "ip": info.get("IPv4Address", "")}
            for cid, info in ctrs.items()
        ]
    except Exception:
        return []


def _all_containers() -> list[dict]:
    rc, out, _ = _run("docker", "ps", "--format", "{{json .}}")
    if rc != 0:
        return []
    result = []
    for line in out.strip().splitlines():
        try:
            c = json.loads(line)
            result.append({"id": c.get("ID", "")[:12], "name": c.get("Names", ""),
                           "image": c.get("Image", ""), "status": c.get("Status", "")})
        except Exception:
            pass
    return result


# ── API endpoints ─────────────────────────────────────────────────────────────

@router.get("/status")
def get_status():
    cfg = _load_cfg()
    state = _load_state()
    configured = bool(cfg.get("USERNAME") and cfg.get("PASSWORD"))

    rc, out, _ = _run("wg", "show", PIA_IFACE)
    connected = rc == 0 and bool(out.strip())

    return {
        "configured":  configured,
        "connected":   connected,
        "region":      state.get("region_name", ""),
        "region_id":   state.get("region_id", ""),
        "peer_ip":     state.get("peer_ip", ""),
        "containers":  _routed_containers() if connected else [],
    }


@router.get("/regions")
def list_regions():
    try:
        return _fetch_regions()
    except (RuntimeError, OSError, ValueError) as e:
        # URLError/ssl/timeout (OSError) and bad JSON (ValueError) mean the PIA
        # server list is unreachable, not that this backend is broken.
        raise HTTPException(503, f"PIA server list unavailable: {e}")


@router.get("/settings")
def get_settings():
    cfg = _load_cfg()
    return {
        "username":    cfg.get("USERNAME", ""),
        "password":    "••••••" if cfg.get("PASSWORD") else "",
        "region":      cfg.get("REGION", "us-newjersey"),
    }


class PiaSettings(BaseModel):
    username: str = ""
    password: str = ""
    region:   str = ""


@router.put("/settings")
def save_settings(s: PiaSettings):
    updates: dict = {}
    if s.username:
        updates["USERNAME"] = s.username
    if s.password and not s.password.startswith("•"):
        updates["PASSWORD"] = s.password
    if s.region:
        updates["REGION"] = s.region
    _save_cfg(updates)
    return {"ok": True}


class ConnectRequest(BaseModel):
    region: str = ""


@router.post("/connect")
def pia_connect(req: ConnectRequest):
    cfg = _load_cfg()
    username = cfg.get("USERNAME", "")
    password = cfg.get("PASSWORD", "")
    if not username or not password:
        raise HTTPException(400, "PIA credentials not configured — open Settings first.")

    region_id = req.region or cfg.get("REGION", "us-newjersey")

    try:
        regions = _fetch_regions()
        region = next((r for r in regions if r["id"] == region_id), None)
        if region is None:
            raise HTTPException(404, f"Region '{region_id}' not found")

        token = _get_token(username, password)
        privkey, pubkey = _genkey()
        peer = _register_key(region["server"]["ip"], token, pubkey)

        dns = (peer.get("dns_servers") or ["8.8.8.8"])[0]
        conf = _build_wg_conf(
            privkey=privkey,
            peer_ip=peer["peer_ip"],
            dns=dns,
            server_key=peer["server_key"],
            server_ip=peer["server_ip"],
            server_port=peer["server_port"],
        )

        _write_root_file(PIA_WG_CONF, conf, "600")

        _prun("wg-quick", "down", PIA_IFACE)   # idempotent
        rc, _, err = _prun("wg-quick", "up", PIA_IFACE)
        if rc != 0:
            raise RuntimeError(f"wg-quick up pia0 failed: {err.strip()}")

        _ensure_docker_net()
        _setup_iptables()

        _save_state({
            "region_id":   region_id,
            "region_name": region["name"],
            "peer_ip":     peer["peer_ip"],
            "server_ip":   peer["server_ip"],
        })

    except (RuntimeError, ValueError) as e:
        raise HTTPException(500, str(e))

    return {"ok": True, "region": region["name"], "peer_ip": peer["peer_ip"]}


@router.post("/disconnect")
def pia_disconnect():
    _teardown_iptables()
    _prun("wg-quick", "down", PIA_IFACE)
    _save_state({})
    return {"ok": True}


@router.get("/containers")
def list_pia_containers():
    return _routed_containers()


@router.get("/containers/available")
def available_containers():
    routed_ids = {c["id"] for c in _routed_containers()}
    return [c for c in _all_containers() if c["id"] not in routed_ids]


@router.post("/containers/{container_id}/route")
def route_container(container_id: str):
    state = _load_state()
    if not state.get("region_id"):
        raise HTTPException(409, "Connect to PIA first before routing containers.")
    _ensure_docker_net()
    rc, _, err = _run("docker", "network", "connect", PIA_DOCKER_NET, container_id)
    if rc != 0:
        raise HTTPException(500, err.strip())
    return {"ok": True}


@router.delete("/containers/{container_id}/route")
def unroute_container(container_id: str):
    rc, _, err = _run("docker", "network", "disconnect", PIA_DOCKER_NET, container_id)
    if rc != 0:
        raise HTTPException(500, err.strip())
    return {"ok": True}
