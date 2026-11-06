"""
OS update management (Debian/Ubuntu apt).

GET  /api/updates/check    — list upgradable packages
POST /api/updates/apply    — run apt-get upgrade (non-interactive)
GET  /api/updates/stream   — SSE stream of apt output during apply
GET  /api/updates/status   — last check/apply result
"""

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from config import STATE_DIR

router = APIRouter(prefix="/api/updates", tags=["updates"])

STATE_FILE = os.path.join(STATE_DIR, "updates_state.json")

_upgrade_proc: subprocess.Popen | None = None
_upgrade_output: list[str] = []


def _load_state() -> dict:
    try:
        return json.loads(Path(STATE_FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"last_check": None, "upgradable": [], "last_apply": None, "last_status": "unknown"}


def _save_state(s: dict):
    Path(STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(STATE_FILE).write_text(json.dumps(s))


def _run_check() -> list[dict]:
    """Run apt-get update + list --upgradable."""
    subprocess.run(
        ["sudo", "-n", "apt-get", "update", "-qq"],
        capture_output=True, timeout=120)
    r = subprocess.run(
        ["apt", "list", "--upgradable", "--quiet"],
        capture_output=True, text=True, timeout=30)
    packages = []
    for line in r.stdout.splitlines():
        if "/" in line and "[" in line:
            # e.g. "curl/stable 7.88.1-10+deb12u7 amd64 [upgradable from: 7.88.1-10+deb12u5]"
            parts = line.split()
            name = parts[0].split("/")[0]
            new_ver = parts[1] if len(parts) > 1 else ""
            old_ver = ""
            if "upgradable from:" in line:
                old_ver = line.split("upgradable from:")[-1].rstrip("]").strip()
            packages.append({"name": name, "new_version": new_ver, "old_version": old_ver})
    return packages


# ── Endpoints ─────────────────────────────────────────────────────

@router.get("/check")
def check_updates():
    packages = _run_check()
    state = _load_state()
    state["last_check"] = int(time.time())
    state["upgradable"] = packages
    _save_state(state)
    return {"count": len(packages), "packages": packages, "checked_at": state["last_check"]}


@router.post("/apply")
def apply_updates():
    global _upgrade_proc, _upgrade_output
    if _upgrade_proc and _upgrade_proc.poll() is None:
        return {"ok": False, "message": "Upgrade already running"}
    _upgrade_output = []

    def _run():
        global _upgrade_proc
        env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
        _upgrade_proc = subprocess.Popen(
            ["sudo", "-n", "apt-get", "upgrade", "-y", "-q"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=env)
        for line in _upgrade_proc.stdout or []:
            _upgrade_output.append(line.rstrip())
            if len(_upgrade_output) > 1000:
                _upgrade_output = _upgrade_output[-800:]
        _upgrade_proc.wait()
        rc = _upgrade_proc.returncode
        state = _load_state()
        state["last_apply"] = int(time.time())
        state["last_status"] = "ok" if rc == 0 else f"failed (rc={rc})"
        _save_state(state)

    import threading
    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "message": "Upgrade started — stream /api/updates/stream"}


@router.get("/stream")
async def stream_updates():
    async def _gen():
        seen = 0
        while True:
            lines = _upgrade_output[seen:]
            for line in lines:
                yield f"data: {line}\n\n"
            seen += len(lines)
            running = _upgrade_proc is not None and _upgrade_proc.poll() is None
            if not running and seen >= len(_upgrade_output):
                yield "data: DONE\n\n"
                break
            await asyncio.sleep(0.5)
    return StreamingResponse(_gen(), media_type="text/event-stream")


@router.get("/status")
def get_status():
    state = _load_state()
    running = _upgrade_proc is not None and _upgrade_proc.poll() is None
    return {**state, "running": running, "recent_output": _upgrade_output[-30:]}
