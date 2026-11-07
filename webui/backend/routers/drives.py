"""
Drives router — SMART health, spin-down, format, preclear, LED identify.
"""

import json
import os
import re
import subprocess
import threading
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from config import STATE_DIR
from services.drive_health import DRIVES_CACHE, _smartctl_json

router = APIRouter(prefix="/api/drives", tags=["drives"])

# Track running preclear jobs {device: {"pct": int, "done": bool, "error": str}}
_preclear_jobs: dict[str, dict] = {}


def _sudo(*args, timeout: int = 30) -> tuple[int, str, str]:
    try:
        r = subprocess.run(["sudo", "-n"] + list(args),
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


def _load_cache() -> list[dict]:
    try:
        return json.loads(Path(DRIVES_CACHE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


@router.get("/")
def list_drives():
    """Return all drives with current SMART health (from 60-s cache)."""
    drives = _load_cache()
    # Strip the full attributes dict from the list view — keep it for /smart
    return [{k: v for k, v in d.items() if k != "attributes"} for d in drives]


def _assigned_map() -> dict:
    """device -> (role, section) from disks.cfg (what's already in the array)."""
    from config import DISKS_CFG
    out = {}
    try:
        text = Path(DISKS_CFG).read_text()
    except Exception:
        return out
    section = None
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r'^\[(\w+)\]$', line)
        if m:
            section = m.group(1)
            continue
        if section and line.startswith("device="):
            dev = line.split("=", 1)[1].strip()
            if section.startswith("parity"):
                role = "parity"
            elif section.startswith(("cache", "pool")):
                role = "cache"
            else:
                role = "data"
            out[dev] = (role, section)
    return out


def _boot_disk() -> str:
    """Parent disk backing /boot (the HELIXBOOT medium) — never assignable."""
    try:
        src = subprocess.run(["findmnt", "-n", "-o", "SOURCE", "--target", "/boot"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        if not src or not src.startswith("/dev/"):
            return ""
        pk = subprocess.run(["lsblk", "-no", "PKNAME", src],
                            capture_output=True, text=True, timeout=5).stdout.strip().splitlines()
        return f"/dev/{pk[0].strip()}" if pk and pk[0].strip() else src
    except Exception:
        return ""


@router.get("/available")
def available_drives():
    """Every physical disk on the machine (via lsblk), flagged assigned vs free.

    This is what the drive-assignment UI lists — unlike '/', which only shows
    disks already written into the array config. The boot medium is flagged so
    the UI won't offer it for the array.
    """
    assigned = _assigned_map()
    boot = _boot_disk()
    disks = []
    try:
        r = subprocess.run(
            ["lsblk", "-J", "-b", "-o",
             "NAME,PATH,SIZE,TYPE,MODEL,SERIAL,ROTA,TRAN,MOUNTPOINT,FSTYPE,VENDOR"],
            capture_output=True, text=True, timeout=15)
        data = json.loads(r.stdout or "{}")
    except Exception as e:
        return {"disks": [], "error": str(e), "boot_disk": boot}

    for d in data.get("blockdevices", []):
        if d.get("type") != "disk":
            continue  # skip partitions, loop, rom/CD, lvm
        name = d.get("name") or ""
        # Skip pseudo/virtual/empty devices (floppy, loop, ram, zram, md, dm, sr).
        if (d.get("size") or 0) < 1_000_000 or name.startswith(
                ("fd", "sr", "loop", "ram", "zram", "md", "dm-", "nbd")):
            continue
        path = d.get("path") or f"/dev/{name}"
        role, aname = assigned.get(path, ("", ""))
        rota = d.get("rota")
        disks.append({
            "device": path,
            "size_gb": round((d.get("size") or 0) / 1e9, 2),
            "model": (d.get("model") or "").strip(),
            "serial": (d.get("serial") or "").strip(),
            "kind": "HDD" if rota in (True, 1, "1") else "SSD",
            "tran": d.get("tran") or "",
            "fstype": d.get("fstype") or "",
            "mountpoint": d.get("mountpoint") or "",
            "assigned": bool(role),
            "role": role,
            "array_name": aname,
            "is_boot": (path == boot),
            "partitions": [c.get("name") for c in (d.get("children") or [])],
        })
    return {"disks": disks, "boot_disk": boot}


@router.get("/{name}/smart")
def drive_smart(name: str):
    """Full SMART attribute table for one drive."""
    drives = _load_cache()
    for d in drives:
        if d["name"] == name:
            return d
    raise HTTPException(404, f"Drive '{name}' not in config or not yet polled")


@router.post("/{name}/test")
def drive_test(name: str, test_type: str = "short", background_tasks: BackgroundTasks = None):
    """Trigger a SMART self-test (short or long)."""
    if test_type not in ("short", "long", "conveyance"):
        raise HTTPException(400, "test_type must be short, long, or conveyance")

    drives = _load_cache()
    device = None
    for d in drives:
        if d["name"] == name:
            device = d.get("device")
            break

    if not device:
        raise HTTPException(404, f"Drive '{name}' not found")

    def _run_test():
        subprocess.run(
            ["smartctl", "-t", test_type, device],
            capture_output=True, timeout=10
        )

    background_tasks.add_task(_run_test)
    return {"ok": True, "message": f"SMART {test_type} test started on {device}"}


@router.post("/refresh")
async def refresh_drives():
    """Force an immediate poll (runs synchronously, may take several seconds)."""
    from services.drive_health import _poll_all, _write_cache
    import asyncio
    data = await asyncio.get_event_loop().run_in_executor(None, _poll_all)
    _write_cache(data)
    return {"ok": True, "count": len(data)}


def _device_for(name: str) -> str:
    """Look up /dev/sdX for a named drive from the SMART cache."""
    drives = _load_cache()
    for d in drives:
        if d["name"] == name:
            dev = d.get("device", "")
            if dev:
                return dev
    # Fallback: check common block devices
    for candidate in [f"/dev/{name}", f"/dev/disk/by-label/{name}"]:
        if Path(candidate).exists():
            return candidate
    raise HTTPException(404, f"Device for '{name}' not found")


# ── Spin-down / spin-state ────────────────────────────────────────────────────

@router.post("/{name}/spindown")
def spindown_drive(name: str):
    """Immediately spin down a drive (hdparm -y)."""
    device = _device_for(name)
    rc, out, err = _sudo("hdparm", "-y", device)
    return {"ok": rc == 0, "device": device, "output": (out or err).strip()}


@router.post("/{name}/spinup")
def spinup_drive(name: str):
    """Wake a spun-down drive by reading a sector."""
    device = _device_for(name)
    rc, out, err = _sudo("hdparm", "-C", device)
    # Actually wake it by doing a read
    subprocess.Popen(["dd", "if=" + device, "of=/dev/null", "bs=512", "count=1"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ok": True, "device": device, "output": out.strip()}


@router.get("/{name}/spinstate")
def spinstate(name: str):
    """Return drive power state: active/idle, standby, sleeping."""
    device = _device_for(name)
    rc, out, err = _sudo("hdparm", "-C", device)
    state = "unknown"
    for line in (out + err).splitlines():
        if "standby" in line.lower():
            state = "standby"
        elif "sleeping" in line.lower():
            state = "sleep"
        elif "active" in line.lower() or "idle" in line.lower():
            state = "active"
    return {"device": device, "state": state, "raw": (out or err).strip()}


# ── LED identify ──────────────────────────────────────────────────────────────

@router.post("/{name}/identify")
def identify_drive(name: str, seconds: int = 10):
    """Blink drive LED (tries ledctl, falls back to hdparm identity display)."""
    device = _device_for(name)
    # Try ledctl (SAS/SCSI enclosures with IBPI)
    rc, out, err = _sudo("ledctl", f"locate={device}")
    if rc == 0:
        threading.Timer(seconds, lambda: _sudo("ledctl", f"locate_off={device}")).start()
        return {"ok": True, "method": "ledctl", "device": device}
    # Fallback: return drive identity info to help user locate it manually
    rc2, out2, _ = _sudo("hdparm", "-I", device)
    model = ""
    serial = ""
    for line in out2.splitlines():
        if "Model Number" in line:
            model = line.split(":", 1)[-1].strip()
        elif "Serial Number" in line:
            serial = line.split(":", 1)[-1].strip()
    return {"ok": False, "method": "info_only", "device": device,
            "model": model, "serial": serial,
            "note": "ledctl not available; use model/serial to locate drive physically"}


# ── Format ────────────────────────────────────────────────────────────────────

class FormatReq(BaseModel):
    fs: str = "xfs"       # xfs | btrfs | ext4
    label: str = ""
    force: bool = False


@router.post("/{name}/format")
def format_drive(name: str, req: FormatReq):
    """Format a drive. DESTRUCTIVE — drive must not be mounted."""
    device = _device_for(name)
    if req.fs not in ("xfs", "btrfs", "ext4"):
        raise HTTPException(400, "fs must be xfs, btrfs, or ext4")

    # Safety: refuse if mounted
    rc, out, _ = subprocess.run(["findmnt", "-n", device],
                                  capture_output=True, text=True).returncode, \
                 *subprocess.run(["findmnt", "-n", device],
                                  capture_output=True, text=True).stdout.splitlines()[:1], ""
    if rc == 0 and out:
        raise HTTPException(409, f"{device} is currently mounted — unmount first")

    label_args: list[str] = []
    if req.label:
        label_args = ["-L", req.label] if req.fs in ("xfs", "ext4") else ["-L", req.label]

    if req.fs == "xfs":
        cmd = ["mkfs.xfs"] + (["-f"] if req.force else []) + label_args + [device]
    elif req.fs == "btrfs":
        cmd = ["mkfs.btrfs"] + (["-f"] if req.force else []) + label_args + [device]
    else:
        cmd = ["mkfs.ext4"] + label_args + [device]

    rc, out, err = _sudo(*cmd, timeout=120)
    return {"ok": rc == 0, "device": device, "fs": req.fs,
            "output": (out or err).strip()[-500:]}


# ── Pre-clear ────────────────────────────────────────────────────────────────

def _run_preclear(device: str):
    """Run dd zero-fill in background, tracking progress."""
    _preclear_jobs[device] = {"pct": 0, "done": False, "error": ""}
    try:
        # Get drive size
        rc, out, _ = subprocess.run(
            ["blockdev", "--getsize64", device],
            capture_output=True, text=True).returncode, \
            *subprocess.run(["blockdev", "--getsize64", device],
                             capture_output=True, text=True).stdout.strip().split()[:1], ""
        total_bytes = int(out) if out.isdigit() else 0

        proc = subprocess.Popen(
            ["dd", f"if=/dev/zero", f"of={device}", "bs=4M", "status=progress"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        for line in proc.stdout or []:
            m = re.search(r"(\d+)\s+bytes", line)
            if m and total_bytes:
                pct = min(99, int(int(m.group(1)) / total_bytes * 100))
                _preclear_jobs[device]["pct"] = pct
        proc.wait()
        _preclear_jobs[device]["pct"] = 100
        _preclear_jobs[device]["done"] = True
    except Exception as e:
        _preclear_jobs[device]["error"] = str(e)
        _preclear_jobs[device]["done"] = True


@router.post("/{name}/preclear")
def preclear_drive(name: str):
    """Zero-fill a drive (pre-clear). Runs in background — poll /preclear/status."""
    device = _device_for(name)
    if device in _preclear_jobs and not _preclear_jobs[device].get("done"):
        raise HTTPException(409, "Preclear already running on this device")
    t = threading.Thread(target=_run_preclear, args=(device,), daemon=True)
    t.start()
    return {"ok": True, "device": device, "message": "Preclear started in background"}


@router.get("/{name}/preclear/status")
def preclear_status(name: str):
    device = _device_for(name)
    job = _preclear_jobs.get(device)
    if not job:
        return {"running": False, "pct": 0, "done": False}
    return {"running": not job["done"], "pct": job["pct"],
            "done": job["done"], "error": job.get("error", "")}


# ── Spin-down timer ───────────────────────────────────────────────

class SpindownTimerReq(BaseModel):
    value: int  # 0=disabled; 1-240=5s increments; 241-251=30min increments; see hdparm(8)


@router.post("/{name}/spindown-timer")
def set_spindown_timer(name: str, req: SpindownTimerReq):
    """Set auto-spindown via hdparm -S (0 = disabled)."""
    device = _device_for(name)
    rc, out, err = _sudo("hdparm", "-S", str(req.value), device)
    return {"ok": rc == 0, "device": device, "value": req.value,
            "output": (out or err).strip()}


# ── Drive health history ───────────────────────────────────────────

@router.get("/{name}/history")
def drive_history(name: str):
    """Return SMART attribute time-series (hourly, up to 7 days)."""
    from services.drive_history import get_history
    return get_history(name)
