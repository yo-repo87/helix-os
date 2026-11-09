"""
WireGuard VPN management.

Interfaces are stored as /etc/wireguard/wgN.conf (standard wg-quick format).
Peer metadata (names, descriptions) lives in HELIX_CONFIG/wireguard/peers.json
since the conf format has no structured comment convention.

GET  /api/wireguard/interfaces
POST /api/wireguard/interfaces
GET  /api/wireguard/interfaces/{iface}
PUT  /api/wireguard/interfaces/{iface}
DELETE /api/wireguard/interfaces/{iface}
POST /api/wireguard/interfaces/{iface}/up
POST /api/wireguard/interfaces/{iface}/down
GET  /api/wireguard/interfaces/{iface}/peers
POST /api/wireguard/interfaces/{iface}/peers
DELETE /api/wireguard/interfaces/{iface}/peers/{pubkey_b64}
GET  /api/wireguard/interfaces/{iface}/peers/{pubkey_b64}/config
POST /api/wireguard/keygen
"""

import base64
import json
import os
import re
import subprocess
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import HELIX_CONFIG

router = APIRouter(prefix="/api/wireguard", tags=["wireguard"])

# Interface configs live in the persistent config dir (wg-quick accepts a full path), so
# they survive reboots; /etc/wireguard is in RAM.
META_DIR    = Path(HELIX_CONFIG) / "wireguard"
WG_DIR      = META_DIR
_IS_ROOT    = os.geteuid() == 0


def _require_root():
    """No-op: privileged commands go through sudo in _run() (the backend is not root)."""
    return


# ── Helpers ───────────────────────────────────────────────────────────────────

_NEEDS_ROOT = {("wg", "show"), ("wg", "set"), ("wg", "syncconf")}


def _run(*args, input: str | None = None, timeout: int = 10) -> tuple[int, str, str]:
    cmd = list(args)
    if not _IS_ROOT and (cmd[0] == "wg-quick" or tuple(cmd[:2]) in _NEEDS_ROOT):
        cmd = ["sudo", "-n", *cmd]
    r = subprocess.run(cmd, capture_output=True, text=True, input=input, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def _syncconf(iface: str, path: Path) -> None:
    """Apply a conf to a running interface. `wg syncconf` rejects wg-quick keys
    (Address, DNS, ...), so strip them first."""
    rc, stripped, _ = _run("wg-quick", "strip", str(path))
    if rc == 0:
        _run("wg", "syncconf", iface, "/dev/stdin", input=stripped)


def _iface_path(iface: str) -> Path:
    if not re.fullmatch(r'wg\d+', iface):
        raise HTTPException(400, "Interface name must match wgN (e.g. wg0)")
    return WG_DIR / f"{iface}.conf"


def _meta_path(iface: str) -> Path:
    META_DIR.mkdir(parents=True, exist_ok=True)
    return META_DIR / f"{iface}_peers.json"


def _load_meta(iface: str) -> dict:
    p = _meta_path(iface)
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_meta(iface: str, meta: dict) -> None:
    _meta_path(iface).write_text(json.dumps(meta, indent=2))


def _pubkey_safe(pubkey: str) -> str:
    return base64.urlsafe_b64encode(base64.b64decode(pubkey)).decode()


def _pubkey_from_safe(safe: str) -> str:
    return base64.b64encode(base64.urlsafe_b64decode(safe)).decode()


# ── WireGuard conf parser ─────────────────────────────────────────────────────

def _parse_conf(text: str) -> dict:
    """Parse wg-quick conf into {interface:{}, peers:[]}."""
    result = {"interface": {}, "peers": []}
    section = None
    cur: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line == "[Interface]":
            section = "interface"
            cur = result["interface"]
        elif line == "[Peer]":
            cur = {}
            result["peers"].append(cur)
            section = "peer"
        elif "=" in line:
            k, _, v = line.partition("=")
            cur[k.strip()] = v.strip()
    return result


def _build_conf(iface_cfg: dict, peers: list[dict]) -> str:
    lines = ["[Interface]"]
    for k in ("Address", "ListenPort", "PrivateKey", "DNS", "PostUp", "PostDown", "MTU"):
        if k in iface_cfg and iface_cfg[k]:
            lines.append(f"{k} = {iface_cfg[k]}")
    for peer in peers:
        lines.append("")
        lines.append("[Peer]")
        for k in ("PublicKey", "PresharedKey", "AllowedIPs", "Endpoint", "PersistentKeepalive"):
            if k in peer and peer[k]:
                lines.append(f"{k} = {peer[k]}")
    return "\n".join(lines) + "\n"


# ── Live status ───────────────────────────────────────────────────────────────

def _live_status(iface: str) -> dict:
    """Get live rx/tx and handshake times from `wg show`."""
    rc, out, _ = _run("wg", "show", iface, "dump")
    if rc != 0:
        return {}
    peers: dict = {}
    lines = out.strip().splitlines()
    # First line is interface (private_key, public_key, listen_port, fwmark)
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) >= 7:
            pubkey      = parts[0]
            last_shake  = int(parts[4]) if parts[4].isdigit() else 0
            rx          = int(parts[5]) if parts[5].isdigit() else 0
            tx          = int(parts[6]) if parts[6].isdigit() else 0
            peers[pubkey] = {"last_handshake": last_shake, "rx_bytes": rx, "tx_bytes": tx}
    return peers


def _iface_up(iface: str) -> bool:
    rc, out, _ = _run("ip", "link", "show", iface)
    return rc == 0 and "UP" in out


# ── Keypair generation ────────────────────────────────────────────────────────

def _genkey() -> tuple[str, str]:
    rc, priv, _ = _run("wg", "genkey")
    if rc != 0:
        raise HTTPException(500, "wg genkey failed")
    priv = priv.strip()
    rc, pub, _ = _run("wg", "pubkey", input=priv)
    if rc != 0:
        raise HTTPException(500, "wg pubkey failed")
    return priv.strip(), pub.strip()


@router.post("/keygen")
def keygen():
    _require_root()
    priv, pub = _genkey()
    return {"private_key": priv, "public_key": pub}


# ── Interface endpoints ───────────────────────────────────────────────────────

@router.get("/interfaces")
def list_interfaces():
    if not WG_DIR.exists():
        return []
    result = []
    for f in sorted(WG_DIR.glob("wg*.conf")):
        iface = f.stem
        up    = _iface_up(iface)
        try:
            parsed = _parse_conf(f.read_text())
        except Exception:
            parsed = {"interface": {}, "peers": []}
        cfg = parsed["interface"]
        result.append({
            "name":        iface,
            "address":     cfg.get("Address", ""),
            "listen_port": cfg.get("ListenPort", "51820"),
            "up":          up,
            "peer_count":  len(parsed["peers"]),
        })
    return result


@router.get("/interfaces/{iface}")
def get_interface(iface: str):
    p = _iface_path(iface)
    if not p.exists():
        raise HTTPException(404, f"Interface {iface} not found")
    parsed  = _parse_conf(p.read_text())
    cfg     = parsed["interface"]
    meta    = _load_meta(iface)
    live    = _live_status(iface) if _iface_up(iface) else {}

    # Derive public key from private
    pub_key = ""
    if cfg.get("PrivateKey"):
        _, pub_out, _ = _run("wg", "pubkey", input=cfg["PrivateKey"])
        pub_key = pub_out.strip()

    peers = []
    for peer in parsed["peers"]:
        pk   = peer.get("PublicKey", "")
        pmeta = meta.get(pk, {})
        plive = live.get(pk, {})
        peers.append({
            "public_key":        pk,
            "public_key_safe":   _pubkey_safe(pk) if pk else "",
            "allowed_ips":       peer.get("AllowedIPs", ""),
            "endpoint":          peer.get("Endpoint", ""),
            "keepalive":         peer.get("PersistentKeepalive", ""),
            "name":              pmeta.get("name", ""),
            "description":       pmeta.get("description", ""),
            "last_handshake":    plive.get("last_handshake", 0),
            "rx_bytes":          plive.get("rx_bytes", 0),
            "tx_bytes":          plive.get("tx_bytes", 0),
        })

    return {
        "name":        iface,
        "address":     cfg.get("Address", ""),
        "listen_port": cfg.get("ListenPort", "51820"),
        "dns":         cfg.get("DNS", ""),
        "mtu":         cfg.get("MTU", ""),
        "public_key":  pub_key,
        "up":          _iface_up(iface),
        "peers":       peers,
    }


class InterfaceCreate(BaseModel):
    name:        str = "wg0"
    address:     str = "10.0.0.1/24"
    listen_port: str = "51820"
    dns:         str = "1.1.1.1"
    mtu:         str = ""


@router.post("/interfaces")
def create_interface(req: InterfaceCreate):
    _require_root()
    p = _iface_path(req.name)
    if p.exists():
        raise HTTPException(409, f"Interface {req.name} already exists")
    WG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    priv, _ = _genkey()
    cfg = {
        "Address":    req.address,
        "ListenPort": req.listen_port,
        "PrivateKey": priv,
    }
    if req.dns:
        cfg["DNS"] = req.dns
    if req.mtu:
        cfg["MTU"] = req.mtu
    p.write_text(_build_conf(cfg, []))
    try:
        p.chmod(0o600)         # FAT ignores modes; harmless there
    except OSError:
        pass
    return get_interface(req.name)


class InterfaceUpdate(BaseModel):
    address:     str | None = None
    listen_port: str | None = None
    dns:         str | None = None
    mtu:         str | None = None


@router.put("/interfaces/{iface}")
def update_interface(iface: str, req: InterfaceUpdate):
    _require_root()
    p = _iface_path(iface)
    if not p.exists():
        raise HTTPException(404)
    parsed = _parse_conf(p.read_text())
    cfg = parsed["interface"]
    if req.address     is not None: cfg["Address"]    = req.address
    if req.listen_port is not None: cfg["ListenPort"] = req.listen_port
    if req.dns         is not None: cfg["DNS"]        = req.dns
    if req.mtu         is not None: cfg["MTU"]        = req.mtu
    p.write_text(_build_conf(cfg, parsed["peers"]))
    # Re-sync if up
    if _iface_up(iface):
        _syncconf(iface, p)
    return get_interface(iface)


@router.delete("/interfaces/{iface}")
def delete_interface(iface: str):
    _require_root()
    p = _iface_path(iface)
    if not p.exists():
        raise HTTPException(404)
    if _iface_up(iface):
        _run("wg-quick", "down", str(_iface_path(iface)))
    p.unlink()
    _meta_path(iface).unlink(missing_ok=True)
    return {"ok": True}


@router.post("/interfaces/{iface}/up")
def iface_up(iface: str):
    _require_root()
    if not _iface_path(iface).exists():
        raise HTTPException(404)
    rc, _, err = _run("wg-quick", "up", str(_iface_path(iface)), timeout=15)
    if rc != 0:
        raise HTTPException(500, err.strip())
    return {"ok": True, "up": True}


@router.post("/interfaces/{iface}/down")
def iface_down(iface: str):
    _require_root()
    if not _iface_path(iface).exists():
        raise HTTPException(404)
    rc, _, err = _run("wg-quick", "down", str(_iface_path(iface)), timeout=15)
    if rc != 0:
        raise HTTPException(500, err.strip())
    return {"ok": True, "up": False}


# ── Peer endpoints ────────────────────────────────────────────────────────────

@router.get("/interfaces/{iface}/peers")
def list_peers(iface: str):
    return get_interface(iface)["peers"]


class PeerCreate(BaseModel):
    name:        str = ""
    description: str = ""
    allowed_ips: str = ""       # e.g. "10.0.0.2/32" — auto-assigned if empty
    keepalive:   str = "25"
    dns:         str = ""       # override DNS for client config
    server_endpoint: str = ""   # host:port for client config


@router.post("/interfaces/{iface}/peers")
def add_peer(iface: str, req: PeerCreate):
    _require_root()
    p = _iface_path(iface)
    if not p.exists():
        raise HTTPException(404)

    # Generate client keypair
    client_priv, client_pub = _genkey()

    # Auto-assign next available IP in the interface's subnet
    parsed    = _parse_conf(p.read_text())
    iface_cfg = parsed["interface"]
    allowed_ips = req.allowed_ips

    if not allowed_ips:
        # Find next free /32 in the interface subnet
        base_net = iface_cfg.get("Address", "10.0.0.1/24")
        base_ip  = base_net.split("/")[0]
        parts    = base_ip.split(".")
        used_ips = set()
        for peer in parsed["peers"]:
            for ip in peer.get("AllowedIPs", "").split(","):
                used_ips.add(ip.strip().split("/")[0])
        used_ips.add(base_ip)
        last = int(parts[3])
        for n in range(2, 255):
            candidate = f"{parts[0]}.{parts[1]}.{parts[2]}.{n}"
            if candidate not in used_ips:
                allowed_ips = f"{candidate}/32"
                last = n
                break
        if not allowed_ips:
            raise HTTPException(409, "No free IP addresses in subnet")

    client_ip = allowed_ips.split("/")[0]

    new_peer = {
        "PublicKey":           client_pub,
        "AllowedIPs":          allowed_ips,
    }
    if req.keepalive:
        new_peer["PersistentKeepalive"] = req.keepalive

    parsed["peers"].append(new_peer)
    p.write_text(_build_conf(iface_cfg, parsed["peers"]))

    # Update peer metadata
    meta = _load_meta(iface)
    meta[client_pub] = {
        "name":        req.name,
        "description": req.description,
        "private_key": client_priv,   # stored for client config download
        "allowed_ips": allowed_ips,
    }
    _save_meta(iface, meta)

    # Sync if interface is up
    if _iface_up(iface):
        _syncconf(iface, p)

    # Build client config
    _, server_pub_out, _ = _run("wg", "pubkey", input=iface_cfg.get("PrivateKey", ""))
    server_pub = server_pub_out.strip()
    endpoint   = req.server_endpoint or f"<server-ip>:{iface_cfg.get('ListenPort', '51820')}"
    client_dns = req.dns or iface_cfg.get("DNS", "1.1.1.1")

    client_conf = (
        f"[Interface]\n"
        f"Address = {client_ip}/32\n"
        f"PrivateKey = {client_priv}\n"
        f"DNS = {client_dns}\n"
        f"\n"
        f"[Peer]\n"
        f"PublicKey = {server_pub}\n"
        f"Endpoint = {endpoint}\n"
        f"AllowedIPs = 0.0.0.0/0, ::/0\n"
        f"PersistentKeepalive = {req.keepalive or '25'}\n"
    )

    return {
        "public_key":      client_pub,
        "public_key_safe": _pubkey_safe(client_pub),
        "allowed_ips":     allowed_ips,
        "name":            req.name,
        "client_config":   client_conf,
    }


@router.delete("/interfaces/{iface}/peers/{pubkey_safe}")
def remove_peer(iface: str, pubkey_safe: str):
    _require_root()
    p = _iface_path(iface)
    if not p.exists():
        raise HTTPException(404)
    pubkey   = _pubkey_from_safe(pubkey_safe)
    parsed   = _parse_conf(p.read_text())
    new_peers = [peer for peer in parsed["peers"] if peer.get("PublicKey") != pubkey]
    if len(new_peers) == len(parsed["peers"]):
        raise HTTPException(404, "Peer not found")
    parsed["peers"] = new_peers
    p.write_text(_build_conf(parsed["interface"], new_peers))
    # Remove from metadata
    meta = _load_meta(iface)
    meta.pop(pubkey, None)
    _save_meta(iface, meta)
    # Sync if up
    if _iface_up(iface):
        _syncconf(iface, p)
    return {"ok": True}


@router.get("/interfaces/{iface}/peers/{pubkey_safe}/config")
def get_peer_config(iface: str, pubkey_safe: str):
    p = _iface_path(iface)
    if not p.exists():
        raise HTTPException(404)
    pubkey = _pubkey_from_safe(pubkey_safe)
    meta   = _load_meta(iface)
    pmeta  = meta.get(pubkey)
    if not pmeta or not pmeta.get("private_key"):
        raise HTTPException(404, "Client config not available (key not managed by Helix OS)")

    parsed    = _parse_conf(p.read_text())
    iface_cfg = parsed["interface"]
    _, server_pub_out, _ = _run("wg", "pubkey", input=iface_cfg.get("PrivateKey", ""))
    server_pub = server_pub_out.strip()

    client_conf = (
        f"[Interface]\n"
        f"Address = {pmeta['allowed_ips']}\n"
        f"PrivateKey = {pmeta['private_key']}\n"
        f"DNS = {iface_cfg.get('DNS', '1.1.1.1')}\n"
        f"\n"
        f"[Peer]\n"
        f"PublicKey = {server_pub}\n"
        f"Endpoint = <server-ip>:{iface_cfg.get('ListenPort', '51820')}\n"
        f"AllowedIPs = 0.0.0.0/0, ::/0\n"
        f"PersistentKeepalive = 25\n"
    )
    return {"config": client_conf, "name": pmeta.get("name", "peer")}
