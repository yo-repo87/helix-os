"""
Checkmk integration via Livestatus (docker exec → unix socket).

GET /api/checkmk/available   — is Checkmk reachable?
GET /api/checkmk/status      — overall site health (services OK/WARN/CRIT counts)
GET /api/checkmk/hosts       — all monitored hosts
GET /api/checkmk/services    — all service states
GET /api/checkmk/containers  — Docker container service states
"""

import json
import os
import subprocess
import threading
import time
from typing import Optional

from fastapi import APIRouter, HTTPException

from config import HELIX_CONFIG, STATE_DIR

router = APIRouter(prefix="/api/checkmk", tags=["checkmk"])

CONTAINER   = "checkmk"
SITE        = "cmk"
LIVE_SOCKET = f"/omd/sites/{SITE}/tmp/run/live"
UNIXCAT     = "/omd/sites/{site}/bin/unixcat"


def _livestatus(query: str) -> list[list]:
    """Run a Livestatus query via docker exec and parse semicolon-separated rows."""
    try:
        # Append newline to terminate query
        payload = query.strip() + "\n\n"
        result = subprocess.run(
            ["docker", "exec", "-i", CONTAINER,
             "su", "-", SITE, "-c",
             f"echo -e {_sh_escape(payload)} | unixcat ~/tmp/run/live"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
        rows = []
        for line in result.stdout.strip().splitlines():
            if line:
                rows.append(line.split(";"))
        return rows
    except Exception:
        return []


def _sh_escape(s: str) -> str:
    """Escape a string for use in bash -c 'echo -e ...'."""
    # Replace newlines with \n literal for echo -e, then single-quote
    escaped = s.replace("\\", "\\\\").replace("'", "'\\''").replace("\n", "\\n")
    return f"'{escaped}'"


def _query_dicts(query: str, columns: list[str]) -> list[dict]:
    rows = _livestatus(query)
    return [dict(zip(columns, row)) for row in rows]


# ── Endpoints ─────────────────────────────────────────────────────

@router.get("/available")
def checkmk_available():
    rows = _livestatus("GET status\nColumns: program_start\n")
    return {"available": bool(rows)}


@router.get("/status")
def checkmk_status():
    # Basic counts from status table
    status_rows = _query_dicts(
        "GET status\nColumns: num_hosts num_services\n",
        ["num_hosts", "num_services"]
    )
    if not status_rows:
        return {"available": False}
    s = status_rows[0]

    def _int(v) -> int:
        try:
            return int(v)
        except (ValueError, TypeError):
            return 0

    # Count services by state
    svc_rows = _query_dicts("GET services\nColumns: state\n", ["state"])
    ok = warn = crit = unknown = 0
    for row in svc_rows:
        st = _int(row.get("state", 0))
        if st == 0:   ok += 1
        elif st == 1: warn += 1
        elif st == 2: crit += 1
        else:         unknown += 1

    # Count hosts by state
    host_rows = _query_dicts("GET hosts\nColumns: state\n", ["state"])
    hosts_up = hosts_down = 0
    for row in host_rows:
        if _int(row.get("state", 0)) == 0: hosts_up += 1
        else:                               hosts_down += 1

    return {
        "available": True,
        "services": {
            "total":   _int(s.get("num_services", 0)),
            "ok":      ok,
            "warn":    warn,
            "crit":    crit,
            "unknown": unknown,
        },
        "hosts": {
            "total": _int(s.get("num_hosts", 0)),
            "up":    hosts_up,
            "down":  hosts_down,
        },
    }


@router.get("/hosts")
def checkmk_hosts():
    cols = ["name", "alias", "state", "num_services", "num_services_ok",
            "num_services_crit", "last_check"]
    return _query_dicts(
        "GET hosts\nColumns: " + " ".join(cols) + "\n",
        cols
    )


@router.get("/services")
def checkmk_services(host: Optional[str] = None, state: Optional[int] = None):
    cols = ["host_name", "description", "state", "plugin_output", "last_check"]
    q = "GET services\nColumns: " + " ".join(cols) + "\n"
    if host:
        q += f"Filter: host_name = {host}\n"
    if state is not None:
        q += f"Filter: state = {state}\n"
    return _query_dicts(q, cols)


@router.get("/containers")
def checkmk_containers():
    cols = ["host_name", "description", "state", "plugin_output"]
    services = _query_dicts(
        "GET services\nColumns: " + " ".join(cols) + "\nFilter: description ~ Docker\n",
        cols
    )
    host_cols = ["name", "state", "num_services_crit", "last_check"]
    hosts = _query_dicts(
        "GET hosts\nColumns: " + " ".join(host_cols) + "\n",
        host_cols
    )
    return {"services": services, "hosts": hosts}


# ── Setup / control (the Monitoring page) ─────────────────────────
# helix-monitor (root, installed by the image) does the work; this only starts it and reports on it.
MONITOR = os.getenv("HELIX_MONITOR", "/usr/local/sbin/helix-monitor")
CREDS = os.path.join(HELIX_CONFIG, "checkmk", "credentials.json")
_job = {"state": "idle", "log": [], "started": 0.0}
_job_lock = threading.Lock()


def _monitor(*args, timeout=60):
    env = ["env", f"HELIX_CONFIG={HELIX_CONFIG}", f"STATE_DIR={STATE_DIR}"]
    return subprocess.run(["sudo", "-n", *env, "python3", MONITOR, *args], capture_output=True, text=True, timeout=timeout)


@router.get("/setup")
def setup_status():
    if not os.path.exists(MONITOR):
        return {"installed": False, "job": _job_view()}
    try:
        r = _monitor("status", timeout=30)
        d = json.loads(r.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        d = {"enabled": False, "container": "unknown", "ready": False}
    d["installed"] = True
    d["job"] = _job_view()
    return d


def _job_view():
    return {"state": _job["state"], "log": _job["log"][-12:], "started": _job["started"]}


def _run_job(args):
    try:
        env = ["env", f"HELIX_CONFIG={HELIX_CONFIG}", f"STATE_DIR={STATE_DIR}"]
        p = subprocess.Popen(["sudo", "-n", *env, "python3", MONITOR, *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in p.stdout:
            _job["log"].append(line.rstrip())
        p.wait()
        _job["state"] = "done" if p.returncode == 0 else "failed"
    except Exception as e:                                  # noqa: BLE001 - shown to the user
        _job["log"].append(str(e)); _job["state"] = "failed"


@router.post("/enable")
def setup_enable():
    if not os.path.exists(MONITOR):
        raise HTTPException(503, "Monitoring is not part of this installation. Update Helix OS.")
    with _job_lock:
        if _job["state"] == "running":
            raise HTTPException(409, "Setup is already running")
        _job.update(state="running", log=[], started=time.time())
    threading.Thread(target=_run_job, args=(["enable"],), daemon=True).start()
    return {"ok": True}


@router.post("/disable")
def setup_disable():
    r = _monitor("disable", timeout=120)
    if r.returncode != 0:
        raise HTTPException(500, (r.stderr or r.stdout).strip()[-200:])
    return {"ok": True}


@router.get("/problems")
def problems():
    """What needs attention right now (cached by the alert loop, so the sidebar badge can ask often)."""
    try:
        d = json.load(open(os.path.join(STATE_DIR, "monitor_problems.json")))
    except (OSError, ValueError):
        return {"at": 0, "problems": [], "total": 0}
    if time.time() - d.get("at", 0) > 300:
        return {"at": d.get("at", 0), "problems": [], "total": 0, "stale": True}
    return d


@router.get("/alert-settings")
def alert_settings():
    from services import monitor_alerts
    return monitor_alerts.load_settings()


@router.put("/alert-settings")
def alert_settings_put(req: dict):
    from services import monitor_alerts
    return monitor_alerts.save_settings(req)


@router.get("/login")
def setup_login():
    """The Checkmk web login, for the "Show login" button. (The Helix API is unauthenticated today, see CLAUDE.md §29.4.)"""
    try:
        c = json.load(open(CREDS))
    except (OSError, ValueError):
        raise HTTPException(404, "Monitoring is not set up yet")
    return {"user": "cmkadmin", "password": c.get("admin_password", "")}
