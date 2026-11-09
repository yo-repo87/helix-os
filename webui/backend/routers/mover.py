"""
Cache mover — moves files from /mnt/cache1 to the array (/mnt/disk* via /mnt/user).

GET  /api/mover/status   — running state, last run time, stats
POST /api/mover/start    — start mover
POST /api/mover/stop     — kill running mover
GET  /api/mover/stream   — SSE stream of live rsync output
GET  /api/mover/config   — get mover settings
PUT  /api/mover/config   — update mover settings (cache_path, dry_run)
"""

import asyncio
import json
import os
import subprocess
import threading
import time
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from config import HELIX_CONFIG, STATE_DIR

router = APIRouter(prefix="/api/mover", tags=["mover"])

MOVER_STATE_FILE = os.path.join(STATE_DIR, "mover_state.json")
MOVER_CFG_FILE   = os.path.join(HELIX_CONFIG, "mover.cfg")

# Cache drives mount at /mnt/cache1, /mnt/cache2, … (see helix-array-start).
CACHE_PATH   = "/mnt/cache1"
USER_SHARE   = "/mnt/user"
DISK_PATTERN = "/mnt/disk"

_mover_proc: subprocess.Popen | None = None
_mover_lock = threading.Lock()
_mover_output: list[str] = []


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


def _save_state(state: dict):
    try:
        Path(MOVER_STATE_FILE).write_text(json.dumps(state))
    except OSError:
        pass


def _load_state() -> dict:
    try:
        return json.loads(Path(MOVER_STATE_FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"last_run": None, "last_duration_s": None, "last_moved": 0,
                "last_status": "never", "running": False}


def _do_move(cfg: dict):
    global _mover_proc, _mover_output
    cache = cfg["cache_path"]
    dest  = cfg["share_path"]
    dry   = cfg.get("dry_run", False)

    if not Path(cache).is_dir():
        _save_state({**_load_state(), "running": False, "last_status": f"error: {cache} not found"})
        return

    _mover_output = []
    state = _load_state()
    state["running"] = True
    state["last_status"] = "running"
    _save_state(state)

    start = time.time()
    moved = 0

    # Build rsync command per top-level subdirectory (share) in cache
    shares = [p for p in Path(cache).iterdir() if p.is_dir()]
    if not shares:
        shares = [Path(cache)]

    for share_dir in shares:
        share_name = share_dir.name
        dst = Path(dest) / share_name
        dst.mkdir(parents=True, exist_ok=True)

        cmd = ["rsync", "-avX", "--progress",
               "--remove-source-files",
               str(share_dir) + "/",
               str(dst) + "/"]
        if dry:
            cmd.insert(1, "--dry-run")

        with _mover_lock:
            _mover_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, text=True)
        try:
            for line in _mover_proc.stdout or []:
                line = line.rstrip()
                _mover_output.append(line)
                if _mover_output[-1:] and len(_mover_output) > 500:
                    _mover_output = _mover_output[-400:]
                # Count transferred files
                if line and not line.startswith(" ") and "/" not in line and line != "":
                    moved += 1
            _mover_proc.wait()
        except Exception as e:
            _mover_output.append(f"ERROR: {e}")

    duration = round(time.time() - start, 1)
    _save_state({"running": False, "last_run": int(time.time()),
                 "last_duration_s": duration, "last_moved": moved,
                 "last_status": "done" if not dry else "dry-run complete"})
    with _mover_lock:
        _mover_proc = None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/status")
def mover_status():
    global _mover_proc
    state = _load_state()
    running = _mover_proc is not None and _mover_proc.poll() is None
    state["running"] = running
    state["recent_lines"] = _mover_output[-30:]
    return state


@router.post("/start")
def mover_start():
    global _mover_proc
    if _mover_proc and _mover_proc.poll() is None:
        return {"ok": False, "message": "Mover already running"}
    cfg = _load_cfg()
    t = threading.Thread(target=_do_move, args=(cfg,), daemon=True)
    t.start()
    return {"ok": True, "message": "Mover started", "dry_run": cfg.get("dry_run", False)}


@router.post("/stop")
def mover_stop():
    global _mover_proc
    with _mover_lock:
        if _mover_proc and _mover_proc.poll() is None:
            _mover_proc.terminate()
            _save_state({**_load_state(), "running": False, "last_status": "stopped"})
            return {"ok": True, "message": "Mover stopped"}
    return {"ok": False, "message": "Mover not running"}


@router.get("/stream")
async def mover_stream():
    """SSE stream of live mover output."""
    async def _gen():
        seen = 0
        while True:
            lines = _mover_output[seen:]
            for line in lines:
                yield f"data: {line}\n\n"
            seen += len(lines)
            running = _mover_proc is not None and _mover_proc.poll() is None
            if not running and seen >= len(_mover_output):
                yield "data: DONE\n\n"
                break
            await asyncio.sleep(0.5)
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
    Path(MOVER_CFG_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(MOVER_CFG_FILE).write_text(
        f"cache_path={cfg.cache_path}\n"
        f"share_path={cfg.share_path}\n"
        f"dry_run={'true' if cfg.dry_run else 'false'}\n"
    )
    return {"ok": True}
