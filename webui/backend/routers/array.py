from fastapi import APIRouter, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import asyncio, glob, subprocess, os, json, re, tempfile, time
from config import HELIX_CONFIG, ARRAY_SBIN, ARRAY_STATE, DISKS_CFG, MNT_BASE, USER_SHARE, STATE_DIR
from services import array_state

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

def _usage(path: str) -> dict | None:
    """Numeric capacity of a mount (what `df` reports), for gauges. The status script only
    emits human strings like '91G' / '13T', which cannot be turned into a percentage."""
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    size = st.f_frsize * st.f_blocks
    used = size - st.f_frsize * st.f_bfree
    return {"size_bytes": size, "used_bytes": used, "free_bytes": st.f_frsize * st.f_bavail,
            "pct": round(100 * used / size, 1) if size else 0.0}


def _add_usage(d: dict) -> dict:
    us = d.get("user_share") or {}
    if us.get("mounted"):
        us.update(_usage(USER_SHARE) or {})
    for key in ("disks", "parity", "cache"):
        for e in d.get(key, []) or []:
            if e.get("mounted"):
                e.update(_usage(os.path.join(MNT_BASE, e["name"])) or {})
    return d


@router.get("/status")
def array_status():
    rc, out, err = _run_sbin("helix-array-status", "--json")
    if rc != 0:
        # Fallback: basic state only
        return {"array_state": _array_state(), "user_share": {}, "disks": [], "error": err}
    try:
        return _add_usage(json.loads(out))
    except json.JSONDecodeError:
        return {"array_state": _array_state(), "raw": out}

# ── Array Operation: what the panel shows, and what start/stop enforce ───────────────
TRANSITION_FILE = os.path.join(STATE_DIR, "array_transition")       # "starting" | "stopping"


def _transition() -> str | None:
    try:
        if time.time() - os.path.getmtime(TRANSITION_FILE) > 600:    # stale: a job that died
            return None
        return open(TRANSITION_FILE).read().strip() or None
    except OSError:
        return None


def _run_job(kind: str, script: str) -> None:
    """Run a start/stop script, keeping the 'starting'/'stopping' marker for its duration."""
    try:
        os.makedirs(os.path.dirname(TRANSITION_FILE), exist_ok=True)
        open(TRANSITION_FILE, "w").write(kind)
        _run_sbin(script)
    finally:
        try:
            os.unlink(TRANSITION_FILE)
        except OSError:
            pass


def _disk_roles() -> dict:
    """parity/data/cache entries from disks.cfg with their size and whether the device exists."""
    out = {"parity": [], "data": [], "cache": []}
    try:
        cfg = open(DISKS_CFG).read()
    except OSError:
        return out
    section = None
    for line in cfg.splitlines():
        line = line.strip()
        m = re.match(r'^\[(\w+)\]$', line)
        if m:
            section = m.group(1)
            continue
        if section and line.startswith("device="):
            dev = line.split("=", 1)[1].strip()
            role = "parity" if section.startswith("parity") else "cache" if section.startswith(("cache", "pool")) else "data"
            present = os.path.exists(dev)
            size_gb = 0.0
            if present:
                try:
                    with open(f"/sys/class/block/{os.path.basename(os.path.realpath(dev))}/size") as f:
                        size_gb = int(f.read()) * 512 / 1e9
                except (OSError, ValueError):
                    pass
            out[role].append({"name": section, "device": dev, "size_gb": size_gb, "present": present})
    return out


def _parity_elapsed() -> str | None:
    try:
        pid = subprocess.run(["pgrep", "-ox", "snapraid"], capture_output=True, text=True, timeout=3).stdout.split()
        if not pid:
            return None
        et = subprocess.run(["ps", "-o", "etimes=", "-p", pid[0]], capture_output=True, text=True, timeout=3).stdout.strip()
        n = int(et)
        return f"{n // 3600}:{n % 3600 // 60:02d}:{n % 60:02d}"
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _mover_running() -> bool:
    try:
        return bool(json.load(open(os.path.join(STATE_DIR, "mover_state.json"))).get("running"))
    except (OSError, ValueError):
        return False


def _parity_synced() -> bool:
    """True once a Parity Sync has completed at least once (SnapRAID leaves a content file on each data disk)."""
    import glob
    return any(os.path.exists(p) for p in glob.glob(os.path.join(MNT_BASE, "disk*", ".snapraid.content")))


def _containers_running() -> int:
    try:
        r = subprocess.run(["docker", "ps", "-q"], capture_output=True, text=True, timeout=4)
        return len(r.stdout.split()) if r.returncode == 0 else 0
    except (OSError, subprocess.TimeoutExpired):
        return 0


def _operation() -> dict:
    roles = _disk_roles()
    state = _array_state()
    return array_state.compute({
        "state": state, "transition": _transition(), **roles,
        "parity_running": _snapraid_running(), "parity_elapsed": _parity_elapsed(),
        "mover_running": _mover_running(), "synced": _parity_synced(),
        "containers_running": _containers_running() if state == "started" else 0,
    })


@router.get("/operation")
def array_operation():
    """Which array controls are valid right now, what each will do, and why any is unavailable."""
    return _operation()


@router.post("/start")
def array_start(background_tasks: BackgroundTasks, confirm: bool = False):
    if _array_state() == "started":
        return {"ok": True, "message": "Array already started"}
    row = _operation()["rows"][0]
    if row["button"] and not row["button"]["enabled"]:
        raise HTTPException(409, row["explain"])
    if row.get("confirm") and not confirm:
        raise HTTPException(409, f"Confirmation required: {row['confirm']}")
    background_tasks.add_task(_run_job, "starting", "helix-array-start")
    return {"ok": True, "message": "Array start initiated"}

@router.post("/stop")
def array_stop(background_tasks: BackgroundTasks):
    if _array_state() == "stopped":
        return {"ok": True, "message": "Array already stopped"}
    row = _operation()["rows"][0]
    if row["button"] and not row["button"]["enabled"]:
        raise HTTPException(409, row["explain"])
    background_tasks.add_task(_run_job, "stopping", "helix-array-stop")
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


AUTOSTART_CFG = os.path.join(HELIX_CONFIG, "array.cfg")


def _autostart_enabled() -> bool:
    try:
        return any(l.strip() == "autostart=yes" for l in open(AUTOSTART_CFG).read().splitlines())
    except OSError:
        return False


class AutostartReq(BaseModel):
    enabled: bool


@router.get("/autostart")
def get_autostart():
    return {"enabled": _autostart_enabled()}


@router.post("/autostart")
def set_autostart(req: AutostartReq):
    """"Start array on boot". Stored in the persistent config; read by helix-array-autostart."""
    try:
        os.makedirs(os.path.dirname(AUTOSTART_CFG), exist_ok=True)
        # keep any other lines a future version may add
        try:
            keep = [l for l in open(AUTOSTART_CFG).read().splitlines() if not l.startswith("autostart=")]
        except OSError:
            keep = []
        with open(AUTOSTART_CFG, "w") as f:
            f.write("\n".join(keep + [f"autostart={'yes' if req.enabled else 'no'}"]) + "\n")
    except OSError as e:
        return {"ok": False, "error": f"cannot write {AUTOSTART_CFG}: {e}"}
    return {"ok": True, "enabled": req.enabled}


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
