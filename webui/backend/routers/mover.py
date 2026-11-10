"""
Cache mover — moves files from a cache drive onto the array.

The work is done by array/sbin/helix-mover, which follows Unraid's mover rules:
skip open files, skip the docker data-root, do not overwrite a file that is
already on the array, and do not delete a source unless the copy succeeded.

GET  /api/mover/status   — running state, last run time, stats
POST /api/mover/start    — start mover. ?dry_run=true plans without copying
                           and does not change the saved config.
POST /api/mover/stop     — stop the running mover
GET  /api/mover/stream   — SSE stream of live output
GET  /api/mover/config   — get mover settings
PUT  /api/mover/config   — update mover settings (cache_path, share_path, dry_run)
"""

import asyncio
import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from config import ARRAY_SBIN, HELIX_CONFIG, STATE_DIR

router = APIRouter(prefix="/api/mover", tags=["mover"])

MOVER_STATE_FILE = os.path.join(STATE_DIR, "mover_state.json")
MOVER_CFG_FILE   = os.path.join(HELIX_CONFIG, "mover.cfg")

# Cache drives mount at /mnt/cache1, /mnt/cache2, … (see helix-array-start).
CACHE_PATH   = "/mnt/cache1"
USER_SHARE   = "/mnt/user"

_mover_proc: subprocess.Popen | None = None
_mover_lock = threading.Lock()
_mover_output: list[str] = []
_mover_running = False
_mover_cancel = False


def _script_path() -> str:
    """Prefer the installed helper, then the copy in this repo (dev tree)."""
    installed = os.path.join(ARRAY_SBIN, "helix-mover")
    if os.path.isfile(installed):
        return installed
    here = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "array", "sbin", "helix-mover"))
    return here


def _mover_cmd(cache: str, dest: str, dry: bool) -> list[str]:
    cmd = ["/bin/bash", _script_path(), "--cache", cache, "--dest", dest]
    if dry:
        cmd.append("--dry-run")
    # The open-file check reads every process's /proc entries, and the copy
    # must be allowed to preserve ownership. Both need root. HELIX_MOVER_DIRECT
    # skips sudo for a same-user test tree.
    if os.geteuid() != 0 and os.environ.get("HELIX_MOVER_DIRECT") != "1":
        cmd = ["sudo", "-n", "env", f"HELIX_CONFIG={HELIX_CONFIG}", *cmd]
    return cmd


def _load_cfg() -> dict:
    defaults = {"cache_path": CACHE_PATH, "share_path": USER_SHARE, "dry_run": False}
    try:
        for line in Path(MOVER_CFG_FILE).read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                defaults[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    defaults["dry_run"] = str(defaults.get("dry_run", "false")).lower() in ("true", "1", "yes")
    return defaults


def _check_path(path: str) -> str | None:
    """Reject a config path that is not an absolute path under /mnt."""
    if not path or "\n" in path or "\x00" in path:
        return "path is empty"
    if not path.startswith("/mnt/"):
        return "path must be under /mnt"
    parts = path.split("/")
    if ".." in parts:
        return "path must not contain .."
    return None


def _save_state(state: dict):
    try:
        Path(MOVER_STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
        Path(MOVER_STATE_FILE).write_text(json.dumps(state))
    except OSError:
        pass


def _load_state() -> dict:
    try:
        return json.loads(Path(MOVER_STATE_FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"last_run": None, "last_duration_s": None, "last_moved": 0,
                "last_status": "never", "running": False}


def _parse_result(line: str) -> tuple[int, int, int] | None:
    if not line.startswith("mover: RESULT "):
        return None
    vals = {"moved": 0, "in_use": 0, "exists": 0}
    for part in line.split()[2:]:
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        if k in vals:
            try:
                vals[k] = int(v)
            except ValueError:
                pass
    return vals["moved"], vals["in_use"], vals["exists"]


def _do_move(cfg: dict):
    global _mover_proc, _mover_output, _mover_running, _mover_cancel
    cache = cfg["cache_path"]
    dest  = cfg["share_path"]
    dry   = bool(cfg.get("dry_run", False))
    start = time.time()
    moved = in_use = exists = 0
    status = "done" if not dry else "dry-run complete"

    def finish(status_: str):
        global _mover_proc, _mover_running
        duration = round(time.time() - start, 1)
        _save_state({"running": False, "last_run": int(time.time()),
                     "last_duration_s": duration, "last_moved": moved,
                     "last_in_use": in_use, "last_exists": exists,
                     "last_status": status_})
        with _mover_lock:
            _mover_proc = None
            _mover_running = False

    if _check_path(cache) or _check_path(dest):
        _mover_output.append("mover: refuse: cache and array paths must be under /mnt")
        finish("error: bad path")
        return
    script = _script_path()
    if not os.path.isfile(script):
        _mover_output.append(f"mover: helper not found: {script}")
        finish("error: helper not found")
        return
    if _mover_cancel:
        finish("stopped")
        return

    _save_state({**_load_state(), "running": True, "last_status": "running"})
    cmd = _mover_cmd(cache, dest, dry)
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, start_new_session=True,
        )
    except OSError as e:
        _mover_output.append(f"mover: failed to start: {e}")
        finish("error: failed to start")
        return
    with _mover_lock:
        _mover_proc = proc
    try:
        for line in proc.stdout or []:
            line = line.rstrip("\n")
            if not line:
                continue
            _mover_output.append(line)
            if len(_mover_output) > 500:
                del _mover_output[:-400]
            parsed = _parse_result(line)
            if parsed:
                moved, in_use, exists = parsed
        rc = proc.wait()
        if _mover_cancel:
            status = "stopped"
        elif rc != 0:
            status = f"error (exit {rc})"
        elif dry:
            status = "dry-run complete"
        elif in_use or exists:
            status = f"done, {in_use} in use, {exists} already on array"
        else:
            status = "done"
    except Exception as e:
        _mover_output.append(f"mover: ERROR: {e}")
        status = "error"
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            pass
    finish(status)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/status")
def mover_status():
    state = _load_state()
    state["running"] = _mover_running
    state["recent_lines"] = _mover_output[-30:]
    return state


@router.post("/start")
def mover_start(dry_run: bool = False):
    """Start a move. dry_run is a query flag so a dry run does not rewrite mover.cfg."""
    global _mover_running, _mover_cancel, _mover_output
    with _mover_lock:
        if _mover_running:
            return {"ok": False, "message": "Mover already running"}
        cfg = _load_cfg()
        # A saved dry_run flag also counts, so the config field stays meaningful.
        # The button passes ?dry_run=true and does not write the flag itself.
        if dry_run or cfg.get("dry_run"):
            cfg = {**cfg, "dry_run": True}
        bad = _check_path(cfg["cache_path"]) or _check_path(cfg["share_path"])
        if bad:
            return {"ok": False, "message": bad}
        _mover_output.clear()
        _mover_cancel = False
        _mover_running = True
    threading.Thread(target=_do_move, args=(cfg,), daemon=True).start()
    return {"ok": True, "message": "Mover started", "dry_run": cfg.get("dry_run", False)}


@router.post("/stop")
def mover_stop():
    global _mover_cancel
    with _mover_lock:
        if not _mover_running:
            return {"ok": False, "message": "Mover not running"}
        _mover_cancel = True
        proc = _mover_proc
    if proc and proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            proc.terminate()
        return {"ok": True, "message": "Mover stopped"}
    return {"ok": True, "message": "Mover stopping"}


@router.get("/stream")
async def mover_stream():
    """SSE stream of live mover output."""
    async def _gen():
        seen = 0
        # The thread may not have printed anything yet. Wait until it finishes
        # or produces a line, so a fast client does not see DONE immediately.
        for _ in range(50):
            if _mover_output or not _mover_running:
                break
            await asyncio.sleep(0.05)
        while True:
            lines = _mover_output[seen:]
            for line in lines:
                yield f"data: {line}\n\n"
            seen += len(lines)
            if not _mover_running and seen >= len(_mover_output):
                yield "data: DONE\n\n"
                break
            await asyncio.sleep(0.4)
    return StreamingResponse(_gen(), media_type="text/event-stream")


class MoverCfg(BaseModel):
    cache_path: str = CACHE_PATH
    share_path: str = USER_SHARE
    dry_run: bool = False


@router.get("/config")
def mover_get_cfg():
    return _load_cfg()


@router.put("/config")
def mover_put_cfg(cfg: MoverCfg):
    bad = _check_path(cfg.cache_path) or _check_path(cfg.share_path)
    if bad:
        return {"ok": False, "error": bad}
    Path(MOVER_CFG_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(MOVER_CFG_FILE).write_text(
        f"cache_path={cfg.cache_path}\n"
        f"share_path={cfg.share_path}\n"
        f"dry_run={'true' if cfg.dry_run else 'false'}\n"
    )
    return {"ok": True}
