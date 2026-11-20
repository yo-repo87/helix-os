"""Firewall API (nftables table `inet helix`; engine in services/firewall.py, root helper helix-firewall).

GET  /api/firewall/status      saved config, pending change + undo deadline, what is active
GET  /api/firewall/audit       every listening port: what it is, who can reach it, warnings; + suggested rules
GET  /api/firewall/log         recent blocked / would-be-blocked connections and the top sources
PUT  /api/firewall/config      apply a new config. Enforcing changes are undone after 90 s unless confirmed
POST /api/firewall/confirm     keep the pending change
POST /api/firewall/revert      undo the pending change now
POST /api/firewall/off         turn the firewall off
GET  /api/firewall/bans        banned addresses;  POST /bans/ban {ip, minutes}  ·  POST /bans/unban {ip}
POST /api/firewall/lists/update   download blocklists + country lists now (background);  GET /lists/status
GET  /api/firewall/containers  containers for per-container outbound rules
GET  /api/firewall/summary     small status for the dashboard tile
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

def _ephemeral() -> tuple[int, int]:
    try:
        lo, hi = open("/proc/sys/net/ipv4/ip_local_port_range").read().split()
        return int(lo), int(hi)
    except (OSError, ValueError):
        return 32768, 60999


def _listeners(containers: dict | None = None) -> list[dict]:
    try:
        r = subprocess.run(["sudo", "-n", "ss", "-Htulnp"], capture_output=True, text=True, timeout=15)
        text = r.stdout if r.returncode == 0 else subprocess.run(["ss", "-Htuln"], capture_output=True,
                                                                  text=True, timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return fw.parse_ss(text, _ephemeral(), frozenset(containers or {}))


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
    containers = _containers()
    listeners = _listeners(containers)
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


# ── bans ──────────────────────────────────────────────────────────────────────

BANS = os.path.join(HELIX_CONFIG, "firewall-bans.json")


@router.get("/bans")
def bans():
    try:
        b = json.load(open(BANS))
    except (OSError, ValueError):
        b = {}
    now = time.time()
    rows = [{"ip": ip, "until": int(v.get("until", 0)), "since": int(v.get("since", 0)), "reason": v.get("reason", "")}
            for ip, v in b.items() if isinstance(v, dict) and v.get("until", 0) > now]
    return {"bans": sorted(rows, key=lambda r: -r["since"])}


class BanReq(BaseModel):
    ip: str
    minutes: int = 60


@router.post("/bans/ban")
def ban(req: BanReq):
    try:
        ipaddress.ip_address(req.ip)
    except ValueError:
        raise HTTPException(400, f"'{req.ip}' is not an IP address.")
    if not 1 <= req.minutes <= 525600:
        raise HTTPException(400, "Ban length must be 1 minute to 1 year.")
    r = _helper("ban", req.ip, str(req.minutes), "banned by hand")
    if r.returncode != 0:
        raise HTTPException(400, _err(r))
    return {"ok": True}


class UnbanReq(BaseModel):
    ip: str


@router.post("/bans/unban")
def unban(req: UnbanReq):
    try:
        ipaddress.ip_address(req.ip)
    except ValueError:
        raise HTTPException(400, f"'{req.ip}' is not an IP address.")
    r = _helper("unban", req.ip)
    if r.returncode != 0:
        raise HTTPException(500, _err(r))
    return {"ok": True}


# ── blocklists / countries ────────────────────────────────────────────────────

LISTS_STATUS = os.path.join(STATE_DIR, "firewall-lists-status.json")


@router.post("/lists/update")
def lists_update():
    """Downloads can take a while: run the helper in its own unit (a backend restart must not kill it)."""
    env = [f"--setenv={k}={v}" for k, v in (("HELIX_CONFIG", HELIX_CONFIG), ("STATE_DIR", STATE_DIR),
                                             ("HELIX_BACKEND", os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))),
                                             ("HELIX_NFT", os.environ.get("HELIX_NFT", ""))) if v]
    subprocess.run(["sudo", "-n", "systemctl", "reset-failed", "helix-firewall-lists-now.service"], capture_output=True)
    try:
        r = subprocess.run(["sudo", "-n", "systemd-run", "--unit=helix-firewall-lists-now", "--collect", *env,
                            "python3", HELPER, "update-lists"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HTTPException(500, str(e))
    if r.returncode != 0:
        raise HTTPException(409, "An update is already running." if "already" in r.stderr else _err(r))
    return {"ok": True}


@router.get("/lists/status")
def lists_status():
    try:
        st = json.load(open(LISTS_STATUS))
    except (OSError, ValueError):
        st = {}
    running = subprocess.run(["systemctl", "is-active", "--quiet", "helix-firewall-lists-now.service"]).returncode == 0
    return {**st, "running": running}


# ── containers (per-container outbound rules) ────────────────────────────────

@router.get("/containers")
def containers():
    try:
        r = subprocess.run(["sudo", "-n", "docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}\t{{.Networks}}"],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False, "containers": []}
    if r.returncode != 0:
        return {"available": False, "containers": []}
    out = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            nets = parts[2].split(",")
            out.append({"name": parts[0], "state": parts[1], "networks": nets,
                        "supported": not any(n in ("host", "none") for n in nets)})
    return {"available": True, "containers": sorted(out, key=lambda c: c["name"].lower())}


@router.get("/summary")
def summary():
    """Dashboard tile: mode, rules, blocked today, active bans."""
    cfg = _pending() or _saved()
    try:
        r = subprocess.run(["sudo", "-n", "journalctl", "-k", "--no-pager", "-o", "cat", "--since", "today",
                            "-g", "helix-fw-drop: "], capture_output=True, text=True, timeout=15)
        blocked = len([x for x in r.stdout.splitlines() if "helix-fw-drop:" in x])
    except (OSError, subprocess.TimeoutExpired):
        blocked = None
    return {"mode": cfg["mode"], "rules": len([x for x in cfg["rules"] if x["enabled"]]),
            "blocked_today": blocked, "bans": len(bans()["bans"]), "available": _available()["available"]}
