"""
Checkmk integration via Livestatus (docker exec → unix socket).

GET /api/checkmk/available   — is Checkmk reachable?
GET /api/checkmk/status      — overall site health (services OK/WARN/CRIT counts)
GET /api/checkmk/hosts       — all monitored hosts
GET /api/checkmk/services    — all service states
GET /api/checkmk/containers  — Docker container service states
"""

import subprocess
from typing import Optional

from fastapi import APIRouter

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
