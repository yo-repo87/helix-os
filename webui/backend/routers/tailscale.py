"""
Tailscale integration.

GET  /api/tailscale/status         — connection state, our tailnet IP, peers, and the login link when sign-in is needed
POST /api/tailscale/up             — connect (optionally with an auth key / flags); returns login_url when it needs a browser sign-in
POST /api/tailscale/down           — disconnect
POST /api/tailscale/logout         — remove this node from the tailnet
GET  /api/tailscale/devices        — peer list

tailscaled ships in the image and is enabled at boot (build/hooks/03-helix-install.sh); its node key lives on
persistent storage so the machine keeps its identity across reboots. The backend runs as the unprivileged
`helix` user, so every tailscale command goes through sudo (services/privileged.py).
"""

import json
import re
import shutil

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services import privileged

router = APIRouter(prefix="/api/tailscale", tags=["tailscale"])

_KEY_FILE = "/run/helix-ts-authkey"
_AUTHKEY_RE = re.compile(r"^tskey-[A-Za-z0-9_-]{8,200}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def _run(*args, timeout: int = 15) -> tuple[int, str, str]:
    r = privileged.run(list(args), timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def _tailscale_available() -> bool:
    return shutil.which("tailscale") is not None


def _status_json() -> tuple[dict, str]:
    """(parsed `tailscale status --json`, error text). Empty dict when tailscaled is not answering."""
    rc, out, err = _run("tailscale", "status", "--json")
    if not out.strip():
        return {}, (err.strip() or "tailscaled is not running")
    try:
        return json.loads(out), ""
    except json.JSONDecodeError:
        return {}, "unreadable status from tailscaled"


# ── Status ────────────────────────────────────────────────────────────────────

@router.get("/status")
def get_status():
    if not _tailscale_available():
        return {"available": False, "connected": False}

    data, err = _status_json()
    if not data:
        return {"available": True, "connected": False, "backend_state": "DaemonDown", "error": err}

    backend = data.get("BackendState", "NoState")
    self_node = data.get("Self") or {}

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
        "available":     True,
        "connected":     backend == "Running",
        "backend_state": backend,
        "auth_url":      data.get("AuthURL", "") if backend == "NeedsLogin" else "",
        "hostname":      self_node.get("HostName", ""),
        "dns_name":      self_node.get("DNSName", ""),
        "ip":            (self_node.get("TailscaleIPs") or [""])[0],
        "tailnet":       (data.get("CurrentTailnet") or {}).get("Name") or data.get("MagicDNSSuffix", ""),
        "peers":         peers,
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
    if not _tailscale_available():
        raise HTTPException(503, "tailscale is not installed")
    if req.auth_key and not _AUTHKEY_RE.match(req.auth_key):
        raise HTTPException(400, "That does not look like a Tailscale auth key (it should start with tskey-)")
    if req.hostname and not _HOST_RE.match(req.hostname):
        raise HTTPException(400, "Hostname can only contain letters, digits and '-'")
    if req.exit_node and not re.match(r"^[A-Za-z0-9.:_-]+$", req.exit_node):
        raise HTTPException(400, "Invalid exit node")

    # --timeout: without it `tailscale up` waits for the browser sign-in forever
    cmd = ["tailscale", "up", "--reset", "--timeout=8s"]
    if req.accept_routes:
        cmd.append("--accept-routes")
    if not req.accept_dns:
        cmd.append("--accept-dns=false")
    if req.advertise_exit:
        cmd.append("--advertise-exit-node")
    if req.hostname:
        cmd.append(f"--hostname={req.hostname}")
    if req.exit_node:
        cmd.append(f"--exit-node={req.exit_node}")
    if req.shields_up:
        cmd.append("--shields-up")

    wrote_key = False
    try:
        if req.auth_key:
            # a file, never argv: command lines are visible to every local user in `ps`
            r = privileged.run(["sh", "-c", f"umask 077; cat > {_KEY_FILE}"], input=req.auth_key)
            if r.returncode != 0:
                raise HTTPException(500, "Could not stage the auth key")
            wrote_key = True
            cmd.append(f"--auth-key=file:{_KEY_FILE}")
        rc, out, err = _run(*cmd, timeout=45)
    finally:
        if wrote_key:
            privileged.run(["rm", "-f", _KEY_FILE])

    data, _ = _status_json()
    if data.get("BackendState") == "Running":
        return {"ok": True, "message": "Tailscale connected"}
    if data.get("BackendState") == "NeedsLogin" and data.get("AuthURL"):
        return {"ok": False, "login_url": data["AuthURL"]}
    for line in (out + err).splitlines():               # older clients only print the URL
        if "https://" in line:
            return {"ok": False, "login_url": line.strip()}
    if rc != 0:
        raise HTTPException(500, (err.strip() or out.strip() or "tailscale up failed").splitlines()[-1])
    return {"ok": False, "message": "Tailscale is still starting; check again in a moment"}


@router.post("/down")
def tailscale_down():
    if not _tailscale_available():
        raise HTTPException(503, "tailscale is not installed")
    rc, _, err = _run("tailscale", "down")
    if rc != 0:
        raise HTTPException(500, err.strip() or "tailscale down failed")
    return {"ok": True}


@router.post("/logout")
def tailscale_logout():
    if not _tailscale_available():
        raise HTTPException(503, "tailscale is not installed")
    rc, _, err = _run("tailscale", "logout")
    if rc != 0:
        raise HTTPException(500, err.strip() or "tailscale logout failed")
    return {"ok": True, "message": "Logged out of Tailscale"}
