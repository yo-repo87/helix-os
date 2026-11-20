"""Firewall API (nftables table `inet helix`; engine in services/firewall.py, root helper helix-firewall).

GET  /api/firewall/status      saved config, pending change + undo deadline, what is active
GET  /api/firewall/audit       every listening port: what it is, who can reach it, warnings; + suggested rules
GET  /api/firewall/log         recent blocked / would-be-blocked connections and the top sources
PUT  /api/firewall/config      apply a new config. Enforcing changes are undone after 90 s unless confirmed
POST /api/firewall/confirm     keep the pending change
POST /api/firewall/revert      undo the pending change now
POST /api/firewall/off         turn the firewall off
"""
import ipaddress
import json
import os
import shutil
import subprocess
import time
from collections import Counter

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from config import HELIX_CONFIG, STATE_DIR
from services import firewall as fw

router = APIRouter(prefix="/api/firewall", tags=["firewall"])

SAVED = os.path.join(HELIX_CONFIG, "firewall.json")
PENDING = os.path.join(STATE_DIR, "firewall.pending.json")
CONFIRM_SECS = int(os.environ.get("HELIX_FW_CONFIRM_SECS", "90"))
HELPER = os.environ.get("HELIX_FIREWALL_HELPER", "/usr/local/sbin/helix-firewall")
if not os.path.exists(HELPER):
    _repo = os.path.join(os.path.dirname(__file__), "..", "..", "..", "network", "helix-firewall")
    if os.path.exists(_repo):
        HELPER = os.path.abspath(_repo)


def _helper(*args, timeout=60) -> subprocess.CompletedProcess:
    cmd = ["sudo", "-n", "env", f"HELIX_CONFIG={HELIX_CONFIG}", f"STATE_DIR={STATE_DIR}",
           f"HELIX_BACKEND={os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))}",
           *([f"HELIX_NFT={os.environ['HELIX_NFT']}"] if os.environ.get("HELIX_NFT") else []),   # tests: stand-in nft
           "python3", HELPER, *args]
    if os.environ.get("HELIX_FW_DIRECT"):                 # tests: no sudo
        cmd = cmd[cmd.index("python3"):]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def _err(r) -> str:
    return (r.stderr or r.stdout or "").strip().splitlines()[-1] if (r.stderr or r.stdout) else "failed"


def _saved() -> dict:
    try:
        return fw.clean_config(json.load(open(SAVED)))
    except (OSError, ValueError):
        return fw.default_config()


def _pending():
    try:
        return fw.clean_config(json.load(open(PENDING)))
    except (OSError, ValueError):
        return None


def _available() -> dict:
    nft = os.environ.get("HELIX_NFT") or shutil.which("nft") or os.path.exists("/usr/sbin/nft")
    return {"available": bool(nft) and os.path.exists(HELPER),
            "reason": "" if nft else "nftables is not installed on this system (update Helix OS)."}


@router.get("/status")
def status():
    r = _helper("status", timeout=20)
    try:
        active = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        active = {"table": False, "active_mode": "unknown", "pending": False, "deadline": None, "error": _err(r)}
    return {**_available(), "config": _saved(), "pending": _pending(), "active": active,
            "confirm_secs": CONFIRM_SECS, "mgmt_ports": list(fw.MGMT_TCP), "default_lan": fw.DEFAULT_LAN}


# ── what is listening ─────────────────────────────────────────────────────────

def _listeners() -> list[dict]:
    try:
        r = subprocess.run(["sudo", "-n", "ss", "-Htulnp"], capture_output=True, text=True, timeout=15)
        text = r.stdout if r.returncode == 0 else subprocess.run(["ss", "-Htuln"], capture_output=True,
                                                                  text=True, timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return fw.parse_ss(text)


def _containers() -> dict:
    try:
        r = subprocess.run(["sudo", "-n", "docker", "ps", "--format", "{{.Names}}\t{{.Ports}}"],
                           capture_output=True, text=True, timeout=15)
        return fw.parse_docker_ports(r.stdout) if r.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired):
        return {}


@router.get("/audit")
def audit():
    cfg = _pending() or _saved()
    listeners, containers = _listeners(), _containers()
    rows = fw.audit(cfg, listeners, containers)
    return {"mode": cfg["mode"], "rows": rows, "suggested": fw.learn_rules(cfg, listeners, containers),
            "uncovered": sum(1 for x in rows if not x["covered"])}


# ── blocked-connection log ────────────────────────────────────────────────────

@router.get("/log")
def blocked_log(limit: int = 200, hours: int = 24):
    limit = max(1, min(limit, 2000))
    hours = max(1, min(hours, 24 * 7))
    try:
        r = subprocess.run(["sudo", "-n", "journalctl", "-k", "--no-pager", "-o", "short-unix",
                            "--since", f"-{hours}h", "-g", "helix-fw-"], capture_output=True, text=True, timeout=30)
        lines = r.stdout.splitlines()
    except (OSError, subprocess.TimeoutExpired):
        lines = []
    events = fw.parse_log(lines)
    top = Counter((e["src"], e["kind"]) for e in events if e["src"])
    ports = Counter((e["proto"], e["dport"]) for e in events if e["dport"])
    return {"events": events[-limit:][::-1], "total": len(events),
            "top_sources": [{"src": s, "kind": k, "count": c} for (s, k), c in top.most_common(15)],
            "top_ports": [{"proto": p, "port": d, "count": c} for (p, d), c in ports.most_common(15)]}


# ── changes ───────────────────────────────────────────────────────────────────

class ConfigReq(BaseModel):
    config: dict
    accept_client_lockout: bool = False


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")) or ""


def _client_keeps_access(cfg: dict, ip: str) -> bool:
    """Your browser must still reach the web UI after an enforcing change."""
    if cfg["mode"] != "enforce" or not ip:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    if addr.is_loopback:                                   # through NPM, the Cloudflare tunnel, or this machine
        return True
    nets = [ipaddress.ip_network(n) for n in cfg["lan_networks"] + fw.VPN_NETS]
    if any(addr in n for n in nets if n.version == addr.version):
        return True
    for r in cfg["rules"]:
        if r["enabled"] and r["proto"] in ("tcp", "both") and any(lo <= p <= hi for p in (80, 443)
                                                               for lo, hi in fw.parse_ports(r["ports"])):
            if r["source"] == "any":
                return True
            if r["source"] == "custom" and any(addr in ipaddress.ip_network(c) for c in r["cidrs"]
                                               if ipaddress.ip_network(c).version == addr.version):
                return True
    return False


@router.put("/config")
def put_config(req: ConfigReq, request: Request):
    if not _available()["available"]:
        raise HTTPException(503, _available()["reason"] or "Firewall helper missing.")
    try:
        cfg = fw.clean_config(req.config)
    except ValueError as e:
        raise HTTPException(400, str(e))
    ip = _client_ip(request)
    if not req.accept_client_lockout and not _client_keeps_access(cfg, ip):
        raise HTTPException(409, f"Your address {ip} is not in the LAN networks and no rule lets it reach the web UI, "
                                 "so you would be locked out. Add it as a LAN network or allow ports 80/443 from it.")
    prev = _saved()
    if cfg["mode"] == "learn" and prev["mode"] != "learn" and not cfg.get("learn_since"):
        cfg["learn_since"] = int(time.time())
    # Only a change that can block anything needs the safety net; learn/off never block.
    risky = cfg["mode"] == "enforce"
    try:
        with open(PENDING + ".tmp", "w") as f:
            f.write(fw.dumps(cfg))
        os.replace(PENDING + ".tmp", PENDING)
    except OSError as e:
        raise HTTPException(500, f"Could not stage the change: {e}")
    r = _helper("apply", *(["--revert-after", str(CONFIRM_SECS)] if risky else []), timeout=60)
    if r.returncode != 0:
        try:
            os.unlink(PENDING)
        except OSError:
            pass
        raise HTTPException(400, _err(r))
    return {"ok": True, "needs_confirm": risky, "confirm_secs": CONFIRM_SECS if risky else 0, "mode": cfg["mode"]}


@router.post("/confirm")
def confirm():
    r = _helper("confirm")
    if r.returncode != 0:
        raise HTTPException(409, _err(r))
    return {"ok": True}


@router.post("/revert")
def revert():
    r = _helper("revert")
    if r.returncode != 0:
        raise HTTPException(500, _err(r))
    return {"ok": True}


@router.post("/off")
def turn_off():
    r = _helper("off")
    if r.returncode != 0:
        raise HTTPException(500, _err(r))
    return {"ok": True}
