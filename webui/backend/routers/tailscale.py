"""
Tailscale integration.

GET  /api/tailscale/status         — full tailscale status (connected, IP, peers)
POST /api/tailscale/up             — tailscale up (optionally with auth key / flags)
POST /api/tailscale/down           — tailscale down
POST /api/tailscale/logout         — tailscale logout (removes node from tailnet)
GET  /api/tailscale/devices        — peer device list from status JSON

Tailscale daemon must already be installed (tailscaled running as a systemd service).
All write ops require root.
"""

import json
import subprocess

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/tailscale", tags=["tailscale"])

_IS_ROOT = __import__("os").geteuid() == 0


def _require_root():
    if not _IS_ROOT:
        raise HTTPException(503, "Tailscale management requires the backend to run as root")


def _run(*args, timeout: int = 15) -> tuple[int, str, str]:
    r = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def _tailscale_available() -> bool:
    rc, _, _ = _run("which", "tailscale")
    return rc == 0


# ── Status ────────────────────────────────────────────────────────────────────

@router.get("/status")
def get_status():
    if not _tailscale_available():
        return {"available": False, "connected": False}

    rc, out, _ = _run("tailscale", "status", "--json")
    if rc != 0 or not out.strip():
        return {"available": True, "connected": False}

    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {"available": True, "connected": False}

    backend = data.get("BackendState", "NoState")
    self_node = data.get("Self", {})

    # Extract device list from Peer map
    peers = []
    for _, peer in (data.get("Peer") or {}).items():
        peers.append({
            "hostname":   peer.get("HostName", ""),
            "dns_name":   peer.get("DNSName", ""),
            "ip":         (peer.get("TailscaleIPs") or [""])[0],
            "os":         peer.get("OS", ""),
            "online":     peer.get("Online", False),
            "exit_node":  peer.get("ExitNode", False),
            "exit_node_option": peer.get("ExitNodeOption", False),
        })

    return {
        "available":    True,
        "connected":    backend in ("Running", "NeedsLogin") and self_node.get("Online", False),
        "backend_state": backend,
        "hostname":    self_node.get("HostName", ""),
        "dns_name":    self_node.get("DNSName", ""),
        "ip":          (self_node.get("TailscaleIPs") or [""])[0],
        "tailnet":     data.get("MagicDNSSuffix", ""),
        "peers":       peers,
    }


@router.get("/devices")
def list_devices():
    return get_status().get("peers", [])


# ── Control ───────────────────────────────────────────────────────────────────

class TailscaleUpRequest(BaseModel):
    auth_key:       str = ""       # one-time auth key for new login
    accept_routes:  bool = True
    accept_dns:     bool = True
    advertise_exit: bool = False   # advertise this node as exit node
    hostname:       str = ""       # override hostname in tailnet
    exit_node:      str = ""       # use this peer as exit node (IP or hostname)
    shields_up:     bool = False   # block incoming connections


@router.post("/up")
def tailscale_up(req: TailscaleUpRequest):
    _require_root()
    if not _tailscale_available():
        raise HTTPException(503, "tailscale is not installed")

    cmd = ["tailscale", "up", "--reset"]
    if req.auth_key:
        cmd += ["--authkey", req.auth_key]
    if req.accept_routes:
        cmd.append("--accept-routes")
    if not req.accept_dns:
        cmd.append("--accept-dns=false")
    if req.advertise_exit:
        cmd.append("--advertise-exit-node")
    if req.hostname:
        cmd += ["--hostname", req.hostname]
    if req.exit_node:
        cmd += ["--exit-node", req.exit_node]
    if req.shields_up:
        cmd.append("--shields-up")

    rc, out, err = _run(*cmd, timeout=30)
    if rc != 0:
        # tailscale up sometimes returns a login URL — surface it
        for line in (out + err).splitlines():
            if "https://" in line:
                return {"ok": False, "login_url": line.strip()}
        raise HTTPException(500, err.strip() or "tailscale up failed")
    return {"ok": True, "message": "Tailscale connected"}


@router.post("/down")
def tailscale_down():
    _require_root()
    if not _tailscale_available():
        raise HTTPException(503, "tailscale is not installed")
    rc, _, err = _run("tailscale", "down")
    if rc != 0:
        raise HTTPException(500, err.strip())
    return {"ok": True}


@router.post("/logout")
def tailscale_logout():
    _require_root()
    if not _tailscale_available():
        raise HTTPException(503, "tailscale is not installed")
    rc, _, err = _run("tailscale", "logout")
    if rc != 0:
        raise HTTPException(500, err.strip())
    return {"ok": True, "message": "Logged out of Tailscale"}
