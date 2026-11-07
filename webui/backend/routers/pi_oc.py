"""
Raspberry Pi Overclock router — firmware (config.txt) based OC for Pi boards.

Unlike x86 CPU/GPU OC (sysfs/MSR/RAPL — see cpu_oc.py / gpu.py), the Pi is
overclocked through the firmware config.txt: CPU (arm_freq / over_voltage or
over_voltage_delta), GPU (gpu_freq / v3d_freq / core_freq / h264_freq /
isp_freq), and — on Pi 1-3 only — memory (sdram_freq / sdram_over_voltage).
Changes are written to a managed block in config.txt and take effect on reboot.

GET  /api/pi-oc/status    — board model, SoC, current config.txt OC + live freq/temp
GET  /api/pi-oc/profiles  — OC profiles applicable to the detected board
POST /api/pi-oc/apply     — write a named profile to config.txt (reboot to apply)
POST /api/pi-oc/custom    — write arbitrary arm_freq/gpu_freq/voltage/sdram values
POST /api/pi-oc/reset     — remove the Helix OC block from config.txt (back to stock)
GET  /api/pi-oc/live      — SSE: ARM freq + SoC temp + throttled flags every 2 s
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

router = APIRouter(prefix="/api/pi-oc", tags=["pi-oc"])

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
_PROFILES_FILE = os.path.join(_DATA_DIR, "pi_oc_profiles.json")

# Boot config location (Pi firmware reads config.txt from the boot partition).
_BOOT_DIR = os.environ.get("HELIX_BOOT", "/boot")
_CONFIG_TXT = os.path.join(_BOOT_DIR, "config.txt")

_BLOCK_START = "# >>> helix-oc >>>"
_BLOCK_END = "# <<< helix-oc <<<"

# Fields each key maps to in config.txt; also the set the frontend renders.
_CPU_KEYS = ("arm_freq", "over_voltage", "over_voltage_delta", "force_turbo", "arm_freq_min")
_GPU_KEYS = ("gpu_freq", "core_freq", "v3d_freq", "h264_freq", "isp_freq", "gpu_mem", "gpu_freq_min")
_MEM_KEYS = ("sdram_freq", "sdram_over_voltage", "sdram_schmoo")
_MISC_KEYS = ("temp_limit", "initial_turbo", "over_voltage_sdram")


# ── Board detection ─────────────────────────────────────────────────────────────

def _read_dt(name: str) -> str:
    try:
        return Path(f"/proc/device-tree/{name}").read_text().replace("\x00", "").strip()
    except Exception:
        return ""


def _detect_board() -> dict:
    """Identify the Pi board + SoC. soc is the profile-matching key."""
    model = _read_dt("model")                       # e.g. "Raspberry Pi 4 Model B Rev 1.4"
    compat = _read_dt("compatible")                 # e.g. "raspberrypi,4-model-b\x00brcm,bcm2712"
    cpuinfo = ""
    try:
        cpuinfo = Path("/proc/cpuinfo").read_text()
    except Exception:
        pass

    blob = f"{model} {compat} {cpuinfo}".lower()
    is_pi = "raspberry pi" in model.lower() or "bcm27" in blob or "raspberrypi" in compat.lower()

    # Map SoC → family key used by the profile set.
    soc = ""
    codename = ""
    if "bcm2712" in blob:
        soc, codename = "bcm2712", "Pi 5"
    elif "bcm2711" in blob:
        soc, codename = "bcm2711", "Pi 4 / CM4 / Pi 400"
    elif "bcm2837" in blob:
        soc, codename = "bcm2837", "Pi 3 / Zero 2 W"
    elif "bcm2710" in blob:
        soc, codename = "bcm2710", "Pi 3 / Zero 2 W"
    elif "bcm2836" in blob:
        soc, codename = "bcm2836", "Pi 2"
    elif "bcm2835" in blob:
        soc, codename = "bcm2835", "Pi 1 / Zero"

    return {"is_pi": bool(is_pi and soc), "model": model or "Unknown", "soc": soc, "codename": codename}


# ── config.txt read / write ─────────────────────────────────────────────────────

def _parse_managed_block(text: str) -> dict:
    """Extract key=value pairs from the Helix-managed block, if present."""
    out = {}
    inside = False
    for line in text.splitlines():
        s = line.strip()
        if s == _BLOCK_START:
            inside = True
            continue
        if s == _BLOCK_END:
            inside = False
            continue
        if inside and "=" in s and not s.startswith("#"):
            k, _, v = s.partition("=")
            out[k.strip()] = v.strip()
    return out


def _current_oc() -> dict:
    """Current Helix-managed OC settings from config.txt (empty = stock)."""
    try:
        return _parse_managed_block(Path(_CONFIG_TXT).read_text())
    except Exception:
        return {}


def _write_managed_block(settings: dict) -> list[str]:
    """Replace (or remove, if empty) the Helix-managed block in config.txt."""
    errors = []
    try:
        orig = Path(_CONFIG_TXT).read_text() if os.path.exists(_CONFIG_TXT) else ""
    except Exception as e:
        return [f"Cannot read {_CONFIG_TXT}: {e}"]

    # Strip any existing managed block.
    lines = orig.splitlines()
    kept, inside = [], False
    for line in lines:
        s = line.strip()
        if s == _BLOCK_START:
            inside = True
            continue
        if s == _BLOCK_END:
            inside = False
            continue
        if not inside:
            kept.append(line)

    # Trim trailing blank lines, then append the new block (if any settings).
    while kept and kept[-1].strip() == "":
        kept.pop()
    body = "\n".join(kept)

    if settings:
        block = [_BLOCK_START, "# Managed by Helix OS — edit via the web UI (Hardware → Pi Overclock)"]
        for k, v in settings.items():
            block.append(f"{k}={v}")
        block.append(_BLOCK_END)
        new_text = (body + "\n\n" + "\n".join(block) + "\n") if body else ("\n".join(block) + "\n")
    else:
        new_text = body + "\n" if body else ""

    try:
        # Back up once, then write atomically.
        if os.path.exists(_CONFIG_TXT) and not os.path.exists(_CONFIG_TXT + ".helix-bak"):
            shutil.copy2(_CONFIG_TXT, _CONFIG_TXT + ".helix-bak")
        tmp = _CONFIG_TXT + ".tmp"
        Path(tmp).write_text(new_text)
        os.replace(tmp, _CONFIG_TXT)
    except PermissionError:
        # The backend runs as the unprivileged 'helix' user and /boot (FAT) is
        # usually root-owned. 'helix' has passwordless sudo, so retry via sudo.
        err = _sudo_write(new_text)
        if err:
            errors.append(err)
    except Exception as e:
        errors.append(f"Write failed: {e}")
    return errors


def _sudo_write(new_text: str) -> str:
    """Fallback write of config.txt via sudo (helix has NOPASSWD sudo)."""
    import tempfile
    tmp = ""
    try:
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".helix-config") as tf:
            tf.write(new_text)
            tmp = tf.name
        if os.path.exists(_CONFIG_TXT):
            subprocess.run(["sudo", "-n", "cp", "-n", _CONFIG_TXT, _CONFIG_TXT + ".helix-bak"],
                           capture_output=True, text=True, timeout=10)
        r = subprocess.run(["sudo", "-n", "cp", tmp, _CONFIG_TXT],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return (f"Permission denied writing {_CONFIG_TXT} "
                    f"(backend is unprivileged and sudo failed: {r.stderr.strip() or 'no passwordless sudo'})")
        return ""
    except Exception as e:
        return f"sudo write failed: {e}"
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except Exception:
                pass


# ── Live readings ───────────────────────────────────────────────────────────────

def _sysfs(path: str, default: str = "") -> str:
    try:
        return Path(path).read_text().strip()
    except Exception:
        return default


def _arm_freq_mhz() -> dict:
    cur = int(_sysfs("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq", "0") or 0) // 1000
    mn = int(_sysfs("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_min_freq", "0") or 0) // 1000
    mx = int(_sysfs("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq", "0") or 0) // 1000
    return {"cur_mhz": cur, "min_mhz": mn, "max_mhz": mx}


def _soc_temp_c() -> Optional[float]:
    v = _sysfs("/sys/class/thermal/thermal_zone0/temp", "")
    if v.lstrip("-").isdigit():
        return int(v) / 1000.0
    return None


def _vcgencmd(*args) -> str:
    try:
        r = subprocess.run(["vcgencmd", *args], capture_output=True, text=True, timeout=3)
        return r.stdout.strip()
    except Exception:
        return ""


def _throttled() -> dict:
    """Decode vcgencmd get_throttled bit flags (if vcgencmd is available)."""
    out = _vcgencmd("get_throttled")  # throttled=0x0
    m = re.search(r"0x([0-9a-fA-F]+)", out)
    if not m:
        return {}
    bits = int(m.group(1), 16)
    return {
        "under_voltage_now": bool(bits & 0x1),
        "freq_capped_now": bool(bits & 0x2),
        "throttled_now": bool(bits & 0x4),
        "under_voltage_occurred": bool(bits & 0x10000),
        "freq_capped_occurred": bool(bits & 0x20000),
        "throttled_occurred": bool(bits & 0x40000),
        "raw": f"0x{bits:x}",
    }


# ── Profiles ──────────────────────────────────────────────────────────────────

def _load_profiles() -> list[dict]:
    try:
        return json.loads(Path(_PROFILES_FILE).read_text())
    except Exception:
        return []


def _profiles_for(soc: str) -> list[dict]:
    return [p for p in _load_profiles() if soc in p.get("socs", []) or p.get("socs") == ["any"]]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/status")
def status():
    board = _detect_board()
    return {
        **board,
        "config_txt": _CONFIG_TXT,
        "current_oc": _current_oc(),
        "arm_freq": _arm_freq_mhz(),
        "temp_c": _soc_temp_c(),
        "throttled": _throttled(),
        "reboot_required": False,
    }


@router.get("/profiles")
def profiles():
    board = _detect_board()
    return {
        "soc": board["soc"],
        "codename": board["codename"],
        "profiles": _profiles_for(board["soc"]) if board["is_pi"] else [],
    }


def _validate(settings: dict, soc: str) -> list[str]:
    """Clamp/guard obviously unsafe values; returns warnings (non-fatal)."""
    warns = []
    try:
        if "arm_freq" in settings:
            af = int(settings["arm_freq"])
            caps = {"bcm2712": 3200, "bcm2711": 2200, "bcm2837": 1600, "bcm2710": 1400, "bcm2836": 1200, "bcm2835": 1200}
            cap = caps.get(soc, 2200)
            if af > cap:
                warns.append(f"arm_freq {af} exceeds the practical cap for {soc} ({cap} MHz) — kept, but may not boot.")
        if "over_voltage" in settings and int(settings["over_voltage"]) > 6 and str(settings.get("force_turbo", "0")) != "1":
            warns.append("over_voltage > 6 requires force_turbo=1 to take effect.")
    except Exception:
        pass
    return warns


class ApplyReq(BaseModel):
    profile_id: str


@router.post("/apply")
def apply_profile(req: ApplyReq):
    board = _detect_board()
    if not board["is_pi"]:
        raise HTTPException(400, "Not a Raspberry Pi — config.txt overclocking is unavailable.")
    profile = next((p for p in _profiles_for(board["soc"]) if p["id"] == req.profile_id), None)
    if not profile:
        raise HTTPException(404, f"Profile '{req.profile_id}' not applicable to {board['soc']}")

    settings = dict(profile.get("settings", {}))
    warns = _validate(settings, board["soc"])
    errors = _write_managed_block(settings)
    return {
        "ok": len(errors) == 0,
        "profile": profile["name"],
        "applied": settings,
        "warnings": errors + warns,
        "reboot_required": len(errors) == 0,
        "message": "Settings written to config.txt — reboot to apply." if not errors else "Failed to write config.txt.",
    }


class CustomReq(BaseModel):
    arm_freq: Optional[int] = None
    over_voltage: Optional[int] = None
    over_voltage_delta: Optional[int] = None     # Pi 5 (microvolts)
    force_turbo: Optional[bool] = None
    gpu_freq: Optional[int] = None
    v3d_freq: Optional[int] = None
    core_freq: Optional[int] = None
    gpu_mem: Optional[int] = None
    sdram_freq: Optional[int] = None              # Pi 1-3 only
    sdram_over_voltage: Optional[int] = None
    temp_limit: Optional[int] = None


@router.post("/custom")
def apply_custom(req: CustomReq):
    board = _detect_board()
    if not board["is_pi"]:
        raise HTTPException(400, "Not a Raspberry Pi — config.txt overclocking is unavailable.")

    # Start from the current managed block so partial edits merge, not clobber.
    settings = _current_oc()
    for field, value in req.dict(exclude_none=True).items():
        if isinstance(value, bool):
            settings[field] = "1" if value else "0"
        else:
            settings[field] = str(value)

    warns = _validate(settings, board["soc"])
    errors = _write_managed_block(settings)
    return {
        "ok": len(errors) == 0,
        "applied": settings,
        "warnings": errors + warns,
        "reboot_required": len(errors) == 0,
        "message": "Settings written to config.txt — reboot to apply." if not errors else "Failed to write config.txt.",
    }


@router.post("/reset")
def reset():
    board = _detect_board()
    if not board["is_pi"]:
        raise HTTPException(400, "Not a Raspberry Pi — nothing to reset.")
    errors = _write_managed_block({})
    return {
        "ok": len(errors) == 0,
        "applied": {},
        "warnings": errors,
        "reboot_required": len(errors) == 0,
        "message": "Overclock removed from config.txt — reboot to return to stock." if not errors else "Failed to write config.txt.",
    }


@router.get("/live")
async def live():
    async def _gen():
        while True:
            try:
                payload = json.dumps({
                    "ts": time.time(),
                    "arm_freq": _arm_freq_mhz(),
                    "temp_c": _soc_temp_c(),
                    "throttled": _throttled(),
                })
                yield f"data: {payload}\n\n"
            except Exception as e:
                yield f"data: {{\"error\": \"{e}\"}}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
