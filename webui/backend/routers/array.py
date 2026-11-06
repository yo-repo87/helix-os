from fastapi import APIRouter, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse
import asyncio, subprocess, os, json, re
from config import HELIX_CONFIG, ARRAY_SBIN, ARRAY_STATE, DISKS_CFG, MNT_BASE, USER_SHARE

router = APIRouter(prefix="/api/array", tags=["array"])

def _run_sbin(script: str, *args, env_override: dict | None = None) -> tuple[int, str, str]:
    env = os.environ.copy()
    env["HELIX_CONFIG"] = HELIX_CONFIG
    if env_override:
        env.update(env_override)
    path = os.path.join(ARRAY_SBIN, script)
    r = subprocess.run(["/bin/bash", path, *args], capture_output=True, text=True, env=env)
    return r.returncode, r.stdout, r.stderr

def _array_state() -> str:
    try:
        return open(ARRAY_STATE).readline().strip()
    except FileNotFoundError:
        return "stopped"

@router.get("/status")
def array_status():
    rc, out, err = _run_sbin("helix-array-status", "--json")
    if rc != 0:
        # Fallback: basic state only
        return {"array_state": _array_state(), "user_share": {}, "disks": [], "error": err}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"array_state": _array_state(), "raw": out}

@router.post("/start")
def array_start(background_tasks: BackgroundTasks):
    if _array_state() == "started":
        return {"ok": True, "message": "Array already started"}
    background_tasks.add_task(_run_sbin, "helix-array-start")
    return {"ok": True, "message": "Array start initiated"}

@router.post("/stop")
def array_stop(background_tasks: BackgroundTasks):
    if _array_state() == "stopped":
        return {"ok": True, "message": "Array already stopped"}
    background_tasks.add_task(_run_sbin, "helix-array-stop")
    return {"ok": True, "message": "Array stop initiated"}

@router.post("/parity-check")
def parity_check(scrub: bool = False, background_tasks: BackgroundTasks = None):
    if _array_state() != "started":
        raise HTTPException(400, "Array is not started")
    args = ["--scrub"] if scrub else []
    background_tasks.add_task(_run_sbin, "helix-parity-check", *args)
    return {"ok": True, "message": f"Parity {'sync+scrub' if scrub else 'sync'} initiated"}

@router.get("/drives")
def drives():
    """Per-drive info: size, usage, temp, SMART status."""
    disks = []
    try:
        cfg = open(DISKS_CFG).read()
    except FileNotFoundError:
        return []

    section = None
    dev = None
    for line in cfg.splitlines():
        line = line.strip()
        m = re.match(r'^\[(\w+)\]$', line)
        if m:
            section = m.group(1)
            dev = None
            continue
        if section and line.startswith("device="):
            dev = line.split("=", 1)[1].strip()
            if not section.startswith("disk"):
                continue
            mnt = f"{MNT_BASE}/{section}"
            info = {"name": section, "device": dev, "mountpoint": mnt}

            # disk usage
            try:
                st = os.statvfs(mnt)
                total = st.f_blocks * st.f_frsize
                free  = st.f_bfree  * st.f_frsize
                used  = total - free
                info.update({
                    "total_gb": round(total / 1e9, 2),
                    "used_gb":  round(used  / 1e9, 2),
                    "free_gb":  round(free  / 1e9, 2),
                    "use_pct":  round(used / total * 100, 1) if total else 0,
                    "mounted":  True,
                })
            except Exception:
                info["mounted"] = False

            # SMART temp
            try:
                r = subprocess.run(
                    ["smartctl", "-A", dev],
                    capture_output=True, text=True, timeout=5
                )
                m2 = re.search(r'Temperature_Celsius\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+(\d+)', r.stdout)
                info["temp_c"] = int(m2.group(1)) if m2 else None
                info["smart_ok"] = r.returncode == 0
            except Exception:
                info["temp_c"] = None
                info["smart_ok"] = None

            disks.append(info)
    return disks


# ── Parity check SSE streaming ────────────────────────────────────────────────

_parity_proc: asyncio.subprocess.Process | None = None

@router.get("/parity/stream")
async def parity_stream(scrub: bool = False):
    """SSE stream: live output from snapraid sync/scrub.
    Each event is one line of snapraid stdout.
    Final event data is 'DONE' or 'ERROR:<msg>'.
    """
    if _array_state() != "started":
        async def _not_started():
            yield "data: ERROR:Array is not started\n\n"
        return StreamingResponse(_not_started(), media_type="text/event-stream")

    op = "scrub" if scrub else "sync"

    async def _generate():
        global _parity_proc
        env = os.environ.copy()
        env["HELIX_CONFIG"] = HELIX_CONFIG
        script = os.path.join(ARRAY_SBIN, "helix-parity-check")
        args = ["/bin/bash", script] + (["--scrub"] if scrub else [])

        try:
            _parity_proc = await asyncio.create_subprocess_exec(
                *args, env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except Exception as e:
            yield f"data: ERROR:{e}\n\n"
            return

        async for raw in _parity_proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            # SSE: escape newlines in data value
            yield f"data: {line}\n\n"

        await _parity_proc.wait()
        rc = _parity_proc.returncode
        _parity_proc = None
        yield f"data: {'DONE' if rc == 0 else f'ERROR:exit {rc}'}\n\n"

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/parity/stop")
def parity_stop():
    global _parity_proc
    if _parity_proc is None:
        return {"ok": True, "message": "No parity operation running"}
    try:
        _parity_proc.terminate()
    except Exception:
        pass
    _parity_proc = None
    return {"ok": True, "message": "Parity operation stopped"}


# ── Drive rebuild (snapraid fix) ───────────────────────────────────

_rebuild_proc: asyncio.subprocess.Process | None = None


@router.get("/rebuild/stream")
async def rebuild_stream():
    """SSE stream: live output from snapraid fix (rebuild failed drive)."""
    async def _gen():
        global _rebuild_proc
        script = os.path.join(ARRAY_SBIN, "helix-parity-rebuild")
        if not os.path.isfile(script):
            # Fallback: direct snapraid fix
            script = None
        try:
            if script:
                _rebuild_proc = await asyncio.create_subprocess_exec(
                    script,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT)
            else:
                _rebuild_proc = await asyncio.create_subprocess_exec(
                    "snapraid", "fix", "-e",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT)
        except FileNotFoundError as e:
            yield f"data: ERROR: {e}\n\n"
            yield "data: DONE\n\n"
            return

        async for raw in _rebuild_proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            yield f"data: {line}\n\n"
        await _rebuild_proc.wait()
        rc = _rebuild_proc.returncode
        _rebuild_proc = None
        yield f"data: {'DONE' if rc == 0 else f'ERROR rc={rc}'}\n\n"

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/rebuild/stop")
def rebuild_stop():
    global _rebuild_proc
    if _rebuild_proc is None:
        return {"ok": True, "message": "No rebuild running"}
    try:
        _rebuild_proc.terminate()
    except Exception:
        pass
    _rebuild_proc = None
    return {"ok": True, "message": "Rebuild stopped"}
