"""
CPU Overclock router — profiles, apply, status, reset.

GET  /api/cpu-oc/status       — current CPU state (vendor, model, governor, turbo, freq)
GET  /api/cpu-oc/profiles     — all profiles (filtered to vendor-compatible + generic)
POST /api/cpu-oc/apply        — apply a named profile by id
POST /api/cpu-oc/custom       — apply arbitrary settings
POST /api/cpu-oc/reset        — restore schedutil + boost + stock power limits
GET  /api/cpu-oc/live         — SSE: per-core freq + overall temp every 2 s
"""

import asyncio
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

router = APIRouter(prefix="/api/cpu-oc", tags=["cpu-oc"])

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
_PROFILES_FILE = os.path.join(_DATA_DIR, "cpu_profiles.json")


# ── CPU detection ─────────────────────────────────────────────────────────────

def _cpuinfo() -> dict:
    """Parse /proc/cpuinfo for first physical CPU."""
    info = {"model": "", "vendor": "", "family": 0, "model_num": 0, "stepping": 0, "cores": 1}
    try:
        text = Path("/proc/cpuinfo").read_text()
        for line in text.splitlines():
            if ":" not in line:
                continue
            k, _, v = line.partition(":")
            k, v = k.strip(), v.strip()
            if k == "model name" and not info["model"]:
                info["model"] = v
            elif k == "vendor_id" and not info["vendor"]:
                info["vendor"] = v.lower()
            elif k == "cpu family" and not info["family"]:
                info["family"] = int(v)
            elif k == "model" and not info["model_num"]:
                info["model_num"] = int(v)
            elif k == "stepping" and not info["stepping"]:
                info["stepping"] = int(v)
        # Count cores
        info["cores"] = text.count("processor\t:")
    except Exception:
        pass
    return info


def _detect_generation(info: dict) -> str:
    """Map CPU info to a generation tag used in profiles."""
    model = info["model"].lower()
    vendor = info["vendor"]
    fam = info["family"]
    mod = info["model_num"]

    if "amd" in vendor or "authenticamd" in vendor:
        # Threadripper / EPYC — check name before generic Ryzen/CPUID path
        if "threadripper" in model:
            if fam == 25:
                if mod >= 0x18:   # Zen 4 (Storm Peak / TRX50)
                    return "threadripper_zen4"
                return "threadripper_zen3"  # Zen 3 (Chagall)
            if fam == 23:
                if mod >= 0x31:   # Zen 2 (Castle Peak / TRX40)
                    return "threadripper_zen2"
                return "threadripper_zen"   # Zen / Zen+ (TR4)
        if "epyc" in model:
            if fam == 25:
                if mod >= 0x11:   # Genoa 9004 / Bergamo 8004 (Zen 4/4c)
                    return "epyc_zen4"
                return "epyc_zen3"          # Milan 7003 (Zen 3)
            if fam == 23:
                if mod >= 0x31:   # Rome 7002 (Zen 2)
                    return "epyc_zen2"
                return "epyc_zen1"          # Naples 7001 (Zen 1)
        # Regular Ryzen — CPUID family/model
        if fam == 25:
            if mod >= 0x61:   # Raphael (Ryzen 7000 desktop, Zen 4)
                return "zen4"
            return "zen3"
        if fam == 23:
            if mod >= 0x71:
                return "zen2"
            if mod >= 0x18:
                return "zen+"
            return "zen"
        if fam == 21:
            if mod >= 2:
                return "piledriver"
            return "bulldozer"
        # String-based fallback for Ryzen
        if "ryzen" in model:
            m = re.search(r'ryzen\s+\d+\s+(\d{4})', model)
            if m:
                num = int(m.group(1))
                if num >= 7000: return "zen4"
                if num >= 5000: return "zen3"
                if num >= 3000: return "zen2"
                if num >= 2000: return "zen+"
                return "zen"
        if "fx" in model:
            return "piledriver"

    elif "intel" in vendor or "genuineintel" in vendor:
        # Xeon / HEDT — check name before generic Core path
        if "xeon w" in model:
            return "xeon_w"
        if "xeon" in model:
            return "xeon_scalable"
        if re.search(r'core\s+i\d-\d+x\b', model):
            return "skylake_x"

        # Regular Core — generation from name string
        m = re.search(r'(\d+)th gen', model)
        if m:
            gen = int(m.group(1))
            if gen >= 13: return "raptor_lake"
            if gen == 12: return "alder_lake"
            if gen == 11: return "rocket_lake"
            if gen == 10: return "comet_lake"
            if gen == 8 or gen == 9: return "coffee_lake"
            if gen == 6 or gen == 7: return "skylake"
            if gen == 4 or gen == 5: return "haswell"
            if gen == 3: return "ivy_bridge"
            if gen == 2: return "sandy_bridge"
        m = re.search(r'core.*i\d-(\d+)', model)
        if m:
            num = int(m.group(1))
            gen = num // 1000
            if gen >= 13: return "raptor_lake"
            if gen == 12: return "alder_lake"
            if gen == 11: return "rocket_lake"
            if gen == 10: return "comet_lake"
            if gen == 8 or gen == 9: return "coffee_lake"
            if gen == 6 or gen == 7: return "skylake"
            if gen == 4 or gen == 5: return "haswell"
            if gen == 3: return "ivy_bridge"
            if gen == 2: return "sandy_bridge"
        # CPUID-based Intel detection
        if fam == 6:
            if mod >= 0xB7: return "raptor_lake"
            if mod >= 0x97: return "alder_lake"
            if mod == 0xA7: return "rocket_lake"
            if mod >= 0xA5: return "comet_lake"
            if mod >= 0x9E: return "coffee_lake"
            if mod == 0x55: return "skylake_x"   # Skylake-X / Xeon SP (name check above wins for Xeon)
            if mod >= 0x5E: return "skylake"
            if mod >= 0x3C: return "haswell"
            if mod >= 0x3A: return "ivy_bridge"
            if mod >= 0x2A: return "sandy_bridge"
    return "unknown"


# ── Current state ─────────────────────────────────────────────────────────────

def _num_cpus() -> int:
    r = subprocess.run(["nproc"], capture_output=True, text=True)
    try:
        return int(r.stdout.strip())
    except Exception:
        return 1


def _sysfs_read(path: str, default: str = "") -> str:
    try:
        return Path(path).read_text().strip()
    except Exception:
        return default


def _get_governor() -> str:
    return _sysfs_read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor", "unknown")


def _get_turbo(vendor: str) -> bool:
    if "amd" in vendor or "authenticamd" in vendor:
        v = _sysfs_read("/sys/devices/system/cpu/cpufreq/boost", "1")
        return v == "1"
    else:
        v = _sysfs_read("/sys/devices/system/cpu/intel_pstate/no_turbo", "0")
        return v == "0"


def _get_freq_mhz() -> dict:
    """Return min/max/current freq in MHz from sysfs."""
    try:
        cur = int(_sysfs_read(
            "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq", "0")) // 1000
        mn = int(_sysfs_read(
            "/sys/devices/system/cpu/cpu0/cpufreq/scaling_min_freq", "0")) // 1000
        mx = int(_sysfs_read(
            "/sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq", "0")) // 1000
        hw_max = int(_sysfs_read(
            "/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq", "0")) // 1000
        return {"cur_mhz": cur, "min_mhz": mn, "max_mhz": mx, "hw_max_mhz": hw_max}
    except Exception:
        return {"cur_mhz": 0, "min_mhz": 0, "max_mhz": 0, "hw_max_mhz": 0}


def _get_power_limit_w() -> Optional[float]:
    """Read Intel RAPL PL1 (or return None if unavailable)."""
    for path in [
        "/sys/class/powercap/intel-rapl/intel-rapl:0/constraint_0_power_limit_uw",
        "/sys/devices/virtual/powercap/intel-rapl/intel-rapl:0/constraint_0_power_limit_uw",
    ]:
        v = _sysfs_read(path, "")
        if v:
            try:
                return int(v) / 1_000_000
            except Exception:
                pass
    return None


def _get_cpu_temp() -> Optional[float]:
    """Try hwmon for CPU package temp."""
    # Try coretemp / k10temp
    for hwmon_dir in sorted(Path("/sys/class/hwmon").iterdir()):
        name = _sysfs_read(str(hwmon_dir / "name"), "")
        if name in ("coretemp", "k10temp", "zenpower"):
            # Read temp1_input (package / Tdie)
            for temp_file in sorted(hwmon_dir.glob("temp*_input")):
                label_file = str(temp_file).replace("_input", "_label")
                label = _sysfs_read(label_file, "")
                if label in ("Package id 0", "Tdie", "Tccd1", "") or not label:
                    try:
                        return int(temp_file.read_text().strip()) / 1000
                    except Exception:
                        pass
    return None


def _get_intel_pstate() -> dict:
    base = "/sys/devices/system/cpu/intel_pstate"
    return {
        "max_perf_pct": int(_sysfs_read(f"{base}/max_perf_pct", "100")),
        "min_perf_pct": int(_sysfs_read(f"{base}/min_perf_pct", "10")),
        "no_turbo": _sysfs_read(f"{base}/no_turbo", "0") == "1",
        "status": _sysfs_read(f"{base}/status", ""),
    }


# ── Write helpers ─────────────────────────────────────────────────────────────

def _write_all_cpus(relpath: str, value: str) -> list[str]:
    """Write value to all CPU sysfs paths matching the pattern."""
    errors = []
    for cpu_dir in sorted(Path("/sys/devices/system/cpu").glob("cpu[0-9]*")):
        p = cpu_dir / relpath
        if p.exists():
            try:
                p.write_text(value)
            except PermissionError:
                errors.append(f"Permission denied: {p}")
            except Exception as e:
                errors.append(str(e))
    return errors


def _apply_governor(governor: str) -> list[str]:
    errors = _write_all_cpus("cpufreq/scaling_governor", governor)
    if errors:
        # Try cpupower as fallback
        r = subprocess.run(
            ["cpupower", "frequency-set", "-g", governor],
            capture_output=True, text=True
        )
        if r.returncode != 0:
            return [r.stderr.strip() or r.stdout.strip()]
        return []
    return []


def _apply_turbo(enable: bool, vendor: str) -> list[str]:
    if "amd" in vendor or "authenticamd" in vendor:
        p = Path("/sys/devices/system/cpu/cpufreq/boost")
        if p.exists():
            try:
                p.write_text("1" if enable else "0")
                return []
            except PermissionError:
                return ["Permission denied writing AMD boost sysfs (run backend as root or add sudoers rule)"]
    else:
        p = Path("/sys/devices/system/cpu/intel_pstate/no_turbo")
        if p.exists():
            try:
                p.write_text("0" if enable else "1")
                return []
            except PermissionError:
                return ["Permission denied writing Intel pstate (run backend as root or add sudoers rule)"]
    return []


def _apply_freq_pct(min_pct: int, max_pct: int) -> list[str]:
    """Set scaling min/max freq as a percentage of hw_max."""
    errors = []
    hw_max_str = _sysfs_read(
        "/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq", "0")
    hw_max = int(hw_max_str) if hw_max_str.isdigit() else 0
    if not hw_max:
        return ["Cannot read cpuinfo_max_freq"]

    min_khz = int(hw_max * max(1, min_pct) / 100)
    max_khz = int(hw_max * min(100, max_pct) / 100)

    errors += _write_all_cpus("cpufreq/scaling_max_freq", str(max_khz))
    errors += _write_all_cpus("cpufreq/scaling_min_freq", str(min_khz))
    return errors


def _apply_intel_pstate(max_pct: int, min_pct: int) -> list[str]:
    errors = []
    base = Path("/sys/devices/system/cpu/intel_pstate")
    for name, val in [("max_perf_pct", max_pct), ("min_perf_pct", min_pct)]:
        p = base / name
        if p.exists():
            try:
                p.write_text(str(val))
            except PermissionError:
                errors.append(f"Permission denied: {p}")
    return errors


def _apply_power_limit(pct: int, vendor: str) -> list[str]:
    """Apply power limit as percentage of TDP via Intel RAPL or AMD SMU."""
    errors = []

    if "amd" not in vendor and "authenticamd" not in vendor:
        # Intel RAPL via powercap
        rapl_base_paths = [
            "/sys/class/powercap/intel-rapl/intel-rapl:0",
            "/sys/devices/virtual/powercap/intel-rapl/intel-rapl:0",
        ]
        for base in rapl_base_paths:
            tdp_file = f"{base}/constraint_0_max_power_uw"
            limit_file = f"{base}/constraint_0_power_limit_uw"
            tdp_str = _sysfs_read(tdp_file, "")
            if tdp_str:
                try:
                    tdp_uw = int(tdp_str)
                    new_limit = int(tdp_uw * pct / 100)
                    Path(limit_file).write_text(str(new_limit))
                    return []
                except Exception as e:
                    errors.append(str(e))
        # Fallback: try to read design limit from energy_uj max
        errors.append("Intel RAPL power limit: could not apply (sysfs path not found or permission denied)")
    else:
        # AMD: power limit via ryzenadj if available, otherwise note it
        r = subprocess.run(["which", "ryzenadj"], capture_output=True)
        if r.returncode == 0 and pct != 100:
            # Read current TDP estimate from /sys
            info_file = "/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq"
            # We can't easily get TDP, so skip ryzenadj here — it needs explicit watt values
            pass
        # For AMD, power limit is primarily applied via boost state + governor
        # Full PBO control requires ryzenadj with explicit watt values — flag this
        if pct != 100:
            errors.append(
                "AMD extended power limits (PBO): install ryzenadj and set TDP manually for full control. "
                "Governor + boost state applied successfully."
            )
    return errors


def _apply_c_states(disable: bool) -> list[str]:
    """Disable/enable CPU C-states via /dev/cpu_dma_latency or cpupower."""
    if not disable:
        # Re-enable: just remove the latency constraint (it's a file descriptor hold)
        # We can't truly "re-enable" via latency file without tracking the fd, so use cpupower
        r = subprocess.run(
            ["cpupower", "idle-set", "--enable-all"],
            capture_output=True, text=True
        )
        if r.returncode != 0:
            return [f"cpupower idle-set --enable-all failed: {r.stderr.strip()}"]
        return []
    else:
        r = subprocess.run(
            ["cpupower", "idle-set", "--disable-by-latency", "1"],
            capture_output=True, text=True
        )
        if r.returncode != 0:
            return [f"cpupower idle-set failed: {r.stderr.strip()}"]
        return []


# ── Profiles ──────────────────────────────────────────────────────────────────

def _load_profiles() -> list[dict]:
    try:
        return json.loads(Path(_PROFILES_FILE).read_text())
    except Exception:
        return []


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/status")
def get_status():
    info = _cpuinfo()
    gen = _detect_generation(info)
    vendor = info["vendor"]
    freqs = _get_freq_mhz()
    temp = _get_cpu_temp()
    pl = _get_power_limit_w()
    pstate = _get_intel_pstate()

    return {
        "model": info["model"],
        "vendor": vendor,
        "generation": gen,
        "cores": info["cores"],
        "governor": _get_governor(),
        "turbo": _get_turbo(vendor),
        "freq": freqs,
        "temp_c": temp,
        "power_limit_w": pl,
        "intel_pstate": pstate,
        "available_governors": _available_governors(),
    }


def _available_governors() -> list[str]:
    p = "/sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors"
    s = _sysfs_read(p, "")
    if s:
        return s.split()
    return ["performance", "powersave", "schedutil", "ondemand", "conservative"]


@router.get("/profiles")
def get_profiles():
    info = _cpuinfo()
    gen = _detect_generation(info)
    vendor = info["vendor"]
    model = info["model"]

    all_profiles = _load_profiles()
    # Return profiles that match detected vendor/generation, plus generics
    matched = []
    generic = []
    for p in all_profiles:
        if p.get("vendor") == "any":
            generic.append(p)
            continue
        if _profile_matches(p, vendor, gen, model):
            matched.append(p)

    return {
        "detected_generation": gen,
        "detected_vendor": vendor,
        "matched_profiles": matched,
        "generic_profiles": generic,
        "all_profiles": all_profiles,
    }


def _profile_matches(profile: dict, vendor: str, gen: str, model: str) -> bool:
    pv = profile.get("vendor", "any")
    if pv == "any":
        return True
    # Vendor check
    is_amd = "amd" in vendor or "authenticamd" in vendor
    is_intel = "intel" in vendor or "genuineintel" in vendor
    if pv == "amd" and not is_amd:
        return False
    if pv == "intel" and not is_intel:
        return False
    # Generation check
    pgens = profile.get("generations", [])
    if pgens and gen not in pgens:
        # Check model name patterns as fallback
        patterns = profile.get("cpu_patterns", [])
        if patterns:
            return any(p.lower() in model.lower() for p in patterns)
        return False
    return True


class ApplyProfileReq(BaseModel):
    profile_id: str


@router.post("/apply")
def apply_profile(req: ApplyProfileReq):
    profiles = _load_profiles()
    profile = next((p for p in profiles if p["id"] == req.profile_id), None)
    if not profile:
        raise HTTPException(404, f"Profile '{req.profile_id}' not found")

    info = _cpuinfo()
    vendor = info["vendor"]
    errors = []
    applied = []

    # Governor
    gov = profile.get("governor", "schedutil")
    errs = _apply_governor(gov)
    if errs:
        errors += errs
    else:
        applied.append(f"Governor → {gov}")

    # Turbo
    turbo = profile.get("turbo", True)
    errs = _apply_turbo(turbo, vendor)
    if errs:
        errors += errs
    else:
        applied.append(f"Turbo boost → {'on' if turbo else 'off'}")

    # Frequency range
    min_pct = profile.get("min_freq_pct", 10)
    max_pct = profile.get("max_freq_pct", 100)

    # Intel pstate has its own interface
    is_intel = "intel" in vendor or "genuineintel" in vendor
    pstate_active = Path("/sys/devices/system/cpu/intel_pstate").exists()
    if is_intel and pstate_active:
        intel_max = profile.get("intel_max_perf", max_pct)
        intel_min = profile.get("intel_min_perf", min_pct)
        errs = _apply_intel_pstate(intel_max, intel_min)
        if errs:
            errors += errs
        else:
            applied.append(f"Intel P-state → {intel_min}%–{intel_max}%")
    else:
        errs = _apply_freq_pct(min_pct, max_pct)
        if errs:
            errors += errs
        else:
            applied.append(f"Frequency range → {min_pct}%–{max_pct}% of max")

    # Power limit
    pl_pct = profile.get("power_limit_pct", 100)
    if pl_pct != 100:
        errs = _apply_power_limit(pl_pct, vendor)
        if errs:
            errors += errs
        else:
            applied.append(f"Power limit → {pl_pct}% of TDP")
    else:
        applied.append("Power limit → stock (100%)")

    # C-states
    disable_c = profile.get("disable_c_states", False)
    if disable_c:
        errs = _apply_c_states(True)
        if errs:
            errors += errs
        else:
            applied.append("C-states → disabled (latency mode)")

    return {
        "ok": len(errors) == 0,
        "profile": profile["name"],
        "applied": applied,
        "warnings": errors,
    }


class CustomSettingsReq(BaseModel):
    governor: Optional[str] = None
    turbo: Optional[bool] = None
    min_freq_pct: Optional[int] = None
    max_freq_pct: Optional[int] = None
    power_limit_pct: Optional[int] = None
    disable_c_states: Optional[bool] = None
    intel_max_perf: Optional[int] = None
    intel_min_perf: Optional[int] = None


@router.post("/custom")
def apply_custom(req: CustomSettingsReq):
    info = _cpuinfo()
    vendor = info["vendor"]
    errors = []
    applied = []

    if req.governor is not None:
        errs = _apply_governor(req.governor)
        (errors if errs else applied).extend(errs if errs else [f"Governor → {req.governor}"])

    if req.turbo is not None:
        errs = _apply_turbo(req.turbo, vendor)
        (errors if errs else applied).extend(errs if errs else [f"Turbo → {'on' if req.turbo else 'off'}"])

    is_intel = "intel" in vendor or "genuineintel" in vendor
    pstate_active = Path("/sys/devices/system/cpu/intel_pstate").exists()

    if req.intel_max_perf is not None or req.intel_min_perf is not None:
        if is_intel and pstate_active:
            cur = _get_intel_pstate()
            mx = req.intel_max_perf if req.intel_max_perf is not None else cur["max_perf_pct"]
            mn = req.intel_min_perf if req.intel_min_perf is not None else cur["min_perf_pct"]
            errs = _apply_intel_pstate(mx, mn)
            (errors if errs else applied).extend(errs if errs else [f"Intel P-state → {mn}%–{mx}%"])
    elif req.min_freq_pct is not None or req.max_freq_pct is not None:
        mn = req.min_freq_pct if req.min_freq_pct is not None else 10
        mx = req.max_freq_pct if req.max_freq_pct is not None else 100
        if is_intel and pstate_active:
            errs = _apply_intel_pstate(mx, mn)
        else:
            errs = _apply_freq_pct(mn, mx)
        (errors if errs else applied).extend(errs if errs else [f"Frequency → {mn}%–{mx}%"])

    if req.power_limit_pct is not None:
        errs = _apply_power_limit(req.power_limit_pct, vendor)
        (errors if errs else applied).extend(errs if errs else [f"Power limit → {req.power_limit_pct}%"])

    if req.disable_c_states is not None:
        errs = _apply_c_states(req.disable_c_states)
        (errors if errs else applied).extend(errs if errs else [f"C-states → {'disabled' if req.disable_c_states else 'enabled'}"])

    return {"ok": len(errors) == 0, "applied": applied, "warnings": errors}


@router.post("/reset")
def reset_to_defaults():
    """Restore schedutil governor, enable turbo, reset pstate, re-enable C-states."""
    info = _cpuinfo()
    vendor = info["vendor"]
    errors = []
    applied = []

    errs = _apply_governor("schedutil")
    (errors if errs else applied).extend(errs if errs else ["Governor → schedutil"])

    errs = _apply_turbo(True, vendor)
    (errors if errs else applied).extend(errs if errs else ["Turbo boost → on"])

    is_intel = "intel" in vendor or "genuineintel" in vendor
    pstate_active = Path("/sys/devices/system/cpu/intel_pstate").exists()
    if is_intel and pstate_active:
        errs = _apply_intel_pstate(100, 10)
        (errors if errs else applied).extend(errs if errs else ["Intel P-state → 10%–100%"])
    else:
        errs = _apply_freq_pct(10, 100)
        (errors if errs else applied).extend(errs if errs else ["Frequency range → 10%–100%"])

    errs = _apply_power_limit(100, vendor)
    # Power limit reset errors are non-fatal
    if errs:
        applied.append(f"Power limit → stock (note: {errs[0][:80]})")
    else:
        applied.append("Power limit → stock")

    errs = _apply_c_states(False)
    (errors if errs else applied).extend(errs if errs else ["C-states → enabled"])

    return {"ok": True, "applied": applied, "warnings": errors}


@router.get("/live")
async def live_cpu():
    """SSE stream of per-core frequencies and package temp every 2 seconds."""
    async def _gen():
        while True:
            try:
                # Read per-core current freq
                cores = []
                for cpu_dir in sorted(
                    Path("/sys/devices/system/cpu").glob("cpu[0-9]*"),
                    key=lambda p: int(p.name[3:])
                ):
                    freq_file = cpu_dir / "cpufreq" / "scaling_cur_freq"
                    if freq_file.exists():
                        try:
                            mhz = int(freq_file.read_text().strip()) // 1000
                            cores.append(mhz)
                        except Exception:
                            cores.append(0)
                temp = _get_cpu_temp()
                gov = _get_governor()
                payload = json.dumps({
                    "ts": time.time(),
                    "cores_mhz": cores,
                    "temp_c": temp,
                    "governor": gov,
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
