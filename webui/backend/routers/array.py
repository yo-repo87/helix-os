from fastapi import APIRouter, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import asyncio, glob, subprocess, os, json, re, tempfile
from config import HELIX_CONFIG, ARRAY_SBIN, ARRAY_STATE, DISKS_CFG, MNT_BASE, USER_SHARE

router = APIRouter(prefix="/api/array", tags=["array"])

def _sbin_cmd(script: str, *args) -> list:
    """Build a sudo invocation for an array helper. The helpers mount disks /
    start mergerfs and require root; the backend runs as the unprivileged 'helix'
    user (NOPASSWD sudo), so run them via sudo with HELIX_CONFIG passed through."""
    path = os.path.join(ARRAY_SBIN, script)
    return ["sudo", "-n", "env", f"HELIX_CONFIG={HELIX_CONFIG}", "/bin/bash", path, *args]


def _run_sbin(script: str, *args, env_override: dict | None = None) -> tuple[int, str, str]:
    cmd = ["sudo", "-n", "env", f"HELIX_CONFIG={HELIX_CONFIG}"]
    if env_override:
        cmd += [f"{k}={v}" for k, v in env_override.items()]
    cmd += ["/bin/bash", os.path.join(ARRAY_SBIN, script), *args]
    r = subprocess.run(cmd, capture_output=True, text=True)
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

def _snapraid_running() -> bool:
    """True while any snapraid process is alive (covers the POST-started sync,
    the SSE-started one, and anything started from a shell)."""
    try:
        return subprocess.run(["pgrep", "-x", "snapraid"],
                              capture_output=True, timeout=5).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False

@router.post("/parity-check")
def parity_check(scrub: bool = False, background_tasks: BackgroundTasks = None):
    if _snapraid_running():
        return {"ok": True, "message": "A parity operation is already running"}
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
            dev = os.path.realpath(line.split("=", 1)[1].strip())
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


# ── Disk assignment (writes disks.cfg, consumed by helix-array-start) ─────────

def _stable_path(dev: str) -> str:
    """A boot-stable name for a disk: /dev/sdX letters are assigned in probe order
    and change between boots (a reboot once turned the 32 GB cache disk into 'sdb'
    and a 14 TB data disk into 'sda'), which would silently swap the parity, data
    and cache roles. Prefer a serial-based /dev/disk/by-id link; wwn-* is the last
    resort and the plain device name the fallback."""
    real = os.path.realpath(dev)
    links = [l for l in glob.glob("/dev/disk/by-id/*")
             if "-part" not in l and os.path.realpath(l) == real]
    def rank(l):
        n = os.path.basename(l)
        return (n.startswith(("wwn-", "nvme-eui.", "nvme-uuid.")), not n.startswith(("ata-", "nvme-", "scsi-", "usb-")), n)
    return sorted(links, key=rank)[0] if links else dev


class AssignReq(BaseModel):
    parity: list[str] = []   # device paths, e.g. ["/dev/sdb"]
    data:   list[str] = []   # device paths for data disks
    cache:  list[str] = []   # device paths for cache/pool drives (fast write cache)
    fmt:    str = "xfs"      # filesystem for data disks
    cache_fmt: str = "btrfs" # filesystem for cache drives (btrfs supports pools)


def _write_disks_cfg(content: str) -> str:
    """Write disks.cfg; fall back to sudo since the backend is unprivileged and
    /boot is usually root-owned. Returns '' on success or an error string."""
    try:
        os.makedirs(os.path.dirname(DISKS_CFG), exist_ok=True)
        with open(DISKS_CFG, "w") as f:
            f.write(content)
        return ""
    except (PermissionError, OSError):
        try:
            with tempfile.NamedTemporaryFile("w", delete=False, suffix=".disks") as tf:
                tf.write(content)
                tmp = tf.name
            d = os.path.dirname(DISKS_CFG)
            subprocess.run(["sudo", "-n", "mkdir", "-p", d], capture_output=True, text=True, timeout=10)
            r = subprocess.run(["sudo", "-n", "cp", tmp, DISKS_CFG], capture_output=True, text=True, timeout=10)
            os.unlink(tmp)
            if r.returncode != 0:
                return (f"cannot write {DISKS_CFG}: {r.stderr.strip() or 'permission denied'} — "
                        "the boot medium is likely read-only (ISO/CD). Boot Helix from a writable "
                        "USB/disk to save array assignments.")
            return ""
        except Exception as e:
            return f"cannot write {DISKS_CFG}: {e}"


def _refresh_drive_cache() -> None:
    try:
        from services.drive_health import _poll_all, _write_cache
        _write_cache(_poll_all())
    except Exception:
        pass  # best effort; /api/drives/refresh still works


@router.post("/assign")
def assign_disks(req: AssignReq, background_tasks: BackgroundTasks):
    """Persist a parity/data disk assignment to disks.cfg (reboot-safe on a
    writable boot medium). Does not start the array — call /start after."""
    all_devs = req.parity + req.data + req.cache
    if len(set(all_devs)) != len(all_devs):
        raise HTTPException(400, "A device was assigned to more than one slot.")
    req.parity = [_stable_path(d) for d in req.parity]
    req.data   = [_stable_path(d) for d in req.data]
    req.cache  = [_stable_path(d) for d in req.cache]
    lines = ["# Helix OS — Disk Assignment (managed by the web UI)", ""]
    for i, dev in enumerate(req.parity, 1):
        lines += [f"[parity{i}]", f"device={dev}", ""]
    for i, dev in enumerate(req.data, 1):
        lines += [f"[disk{i}]", f"device={dev}", f"format={req.fmt}", ""]
    for i, dev in enumerate(req.cache, 1):
        lines += [f"[cache{i}]", f"device={dev}", f"format={req.cache_fmt}", ""]
    err = _write_disks_cfg("\n".join(lines) + "\n")
    if err:
        # Return 200 with ok=false (not a 500): BaseHTTPMiddleware can turn a
        # raised HTTPException into a generic "Internal Server Error", hiding the
        # real cause from the UI. The frontend shows .error.
        return {"ok": False, "error": err}
    background_tasks.add_task(_refresh_drive_cache)
    return {"ok": True, "parity": req.parity, "data": req.data,
            "cache": req.cache, "fmt": req.fmt, "cache_fmt": req.cache_fmt}


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

    if _snapraid_running():
        async def _busy():
            yield "data: ERROR:A parity operation is already running\n\n"
        return StreamingResponse(_busy(), media_type="text/event-stream")

    op = "scrub" if scrub else "sync"

    async def _generate():
        global _parity_proc
        args = _sbin_cmd("helix-parity-check", *(["--scrub"] if scrub else []))

        try:
            _parity_proc = await asyncio.create_subprocess_exec(
                *args,
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
        if os.path.isfile(script):
            cmd = _sbin_cmd("helix-parity-rebuild")
        else:
            # Fallback: direct snapraid fix (also needs root).
            cmd = ["sudo", "-n", "snapraid", "fix", "-e"]
        try:
            _rebuild_proc = await asyncio.create_subprocess_exec(
                *cmd,
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
