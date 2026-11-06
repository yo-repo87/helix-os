"""
GPU monitoring and overclocking.

GET  /api/gpu/                      — list GPUs with current stats
GET  /api/gpu/docker                — containers with GPU device bindings
GET  /api/gpu/overclock/            — OC state + limits for all GPUs
POST /api/gpu/overclock/{idx}/power — set power limit (NVIDIA/AMD)
POST /api/gpu/overclock/{idx}/clocks— lock/set clock speeds (NVIDIA/Intel)
POST /api/gpu/overclock/{idx}/fan   — set fan speed (NVIDIA/AMD)
POST /api/gpu/overclock/{idx}/perf  — set performance level (AMD)
POST /api/gpu/overclock/{idx}/reset — reset all OC settings to defaults
"""

import glob
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/gpu", tags=["gpu"])


def _run(*args, timeout: int = 8) -> tuple[int, str, str]:
    try:
        r = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 1, "", ""


def _float(s: str) -> float | None:
    try:
        return float(str(s).strip().split()[0])
    except Exception:
        return None


def _int(s: str) -> int | None:
    try:
        return int(str(s).strip().split()[0])
    except Exception:
        return None


# ── NVIDIA ────────────────────────────────────────────────────────────────────

def _nvidia_gpus() -> list[dict]:
    rc, out, _ = _run(
        "nvidia-smi",
        "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,"
        "temperature.gpu,power.draw,driver_version",
        "--format=csv,noheader,nounits",
    )
    if rc != 0 or not out.strip():
        return []

    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7:
            continue
        idx, name, util, mem_used, mem_total, temp, power, *rest = parts
        driver = rest[0] if rest else ""
        gpus.append({
            "index":      _int(idx),
            "vendor":     "nvidia",
            "name":       name,
            "util_pct":   _float(util),
            "mem_used_mb":_float(mem_used),
            "mem_total_mb":_float(mem_total),
            "mem_pct":    round(_float(mem_used) / _float(mem_total) * 100, 1)
                          if _float(mem_used) and _float(mem_total) else None,
            "temp_c":     _float(temp),
            "power_w":    _float(power) if power not in ("N/A", "[N/A]", "") else None,
            "driver":     driver,
        })
    return gpus


# ── AMD ───────────────────────────────────────────────────────────────────────

def _amd_sysfs_read(card_path: str, attr: str) -> str:
    try:
        return Path(f"{card_path}/{attr}").read_text().strip()
    except (OSError, FileNotFoundError):
        return ""


def _amd_gpus() -> list[dict]:
    # Try rocm-smi first
    rc, out, _ = _run("rocm-smi", "--showuse", "--showmemuse", "--showtemp",
                      "--showpower", "--json")
    if rc == 0 and out.strip():
        try:
            data = json.loads(out)
            gpus = []
            for key, card in data.items():
                if not key.lower().startswith("card"):
                    continue
                idx = int(key.replace("card", "").strip()) if key.lower() != "card" else 0
                util = _float(card.get("GPU use (%)", card.get("GPU Use (%)", "")))
                mem_pct = _float(card.get("GPU memory use (%)", card.get("GPU Memory Use (%)", "")))
                temp = _float(card.get("Temperature (Sensor junction) (C)",
                                       card.get("Temperature (Sensor edge) (C)", "")))
                power = _float(card.get("Average Graphics Package Power (W)",
                                        card.get("Current Socket Graphics Package Power (W)", "")))
                gpus.append({
                    "index":       idx,
                    "vendor":      "amd",
                    "name":        card.get("Card series", f"AMD GPU card{idx}"),
                    "util_pct":    util,
                    "mem_used_mb": None,
                    "mem_total_mb":None,
                    "mem_pct":     mem_pct,
                    "temp_c":      temp,
                    "power_w":     power,
                    "driver":      card.get("Driver version", ""),
                })
            if gpus:
                return gpus
        except (json.JSONDecodeError, KeyError, ValueError):
            pass

    # Fallback: sysfs
    gpus = []
    for i, card_dev in enumerate(sorted(glob.glob("/sys/class/drm/card*/device"))):
        card_path = card_dev

        # Only AMD cards have gpu_busy_percent
        busy = _amd_sysfs_read(card_path, "gpu_busy_percent")
        if not busy:
            continue

        mem_used  = _amd_sysfs_read(card_path, "mem_info_vram_used")
        mem_total = _amd_sysfs_read(card_path, "mem_info_vram_total")
        temp_raw  = _amd_sysfs_read(card_path, "hwmon/hwmon0/temp1_input")

        mem_used_mb  = _int(mem_used)  // 1024 // 1024 if _int(mem_used)  else None
        mem_total_mb = _int(mem_total) // 1024 // 1024 if _int(mem_total) else None
        mem_pct = round(mem_used_mb / mem_total_mb * 100, 1) if mem_used_mb and mem_total_mb else None
        temp_c  = round(_float(temp_raw) / 1000, 1) if temp_raw else None

        name = _amd_sysfs_read(card_path, "product_name") or f"AMD GPU card{i}"

        gpus.append({
            "index":       i,
            "vendor":      "amd",
            "name":        name,
            "util_pct":    _float(busy),
            "mem_used_mb": mem_used_mb,
            "mem_total_mb":mem_total_mb,
            "mem_pct":     mem_pct,
            "temp_c":      temp_c,
            "power_w":     None,
            "driver":      "amdgpu",
        })
    return gpus


# ── Intel ─────────────────────────────────────────────────────────────────────

def _intel_gpu_name(card_index: int) -> str:
    """Try lspci to get a real GPU name for Intel card at given DRM index."""
    # Walk the DRM card's PCI symlink to find the PCI address
    pci_link = f"/sys/class/drm/card{card_index}/device"
    try:
        pci_addr = Path(pci_link).resolve().name  # e.g. "0000:00:02.0"
    except Exception:
        pci_addr = None

    if pci_addr:
        rc, out, _ = _run("lspci", "-s", pci_addr, "-vmm")
        if rc == 0:
            for line in out.splitlines():
                if line.startswith("Device:"):
                    name = line.split(":", 1)[1].strip()
                    if name:
                        return name

    # Fallback: scan lspci output for VGA/Display entries mentioning Intel
    rc, out, _ = _run("lspci")
    if rc == 0:
        for line in out.splitlines():
            if ("VGA" in line or "Display" in line or "3D" in line) and "Intel" in line:
                # Strip PCI address and class prefix
                parts = line.split(":", 2)
                if len(parts) >= 3:
                    return parts[2].strip()

    return f"Intel GPU {card_index}"


def _intel_gpus() -> list[dict]:
    gpus = []
    # Match each renderD node to a card by looking at DRM device links
    render_nodes = sorted(glob.glob("/dev/dri/renderD*"))
    if not render_nodes:
        return []

    # Build map: card_index → render_node via /sys/class/drm/cardN/device/drm/renderD*
    card_render_map: dict[int, str] = {}
    for card_dir in sorted(glob.glob("/sys/class/drm/card*/device")):
        card_name = Path(card_dir).parent.name  # "card0", "card1", ...
        try:
            card_idx = int(card_name.replace("card", ""))
        except ValueError:
            continue
        # Check it's an Intel card (vendor id 0x8086)
        vendor_id = ""
        try:
            vendor_id = Path(f"{card_dir}/vendor").read_text().strip()
        except OSError:
            pass
        if vendor_id != "0x8086":
            continue
        # Find associated render node
        render_links = glob.glob(f"{card_dir}/drm/renderD*")
        if render_links:
            card_render_map[card_idx] = f"/dev/dri/{Path(render_links[0]).name}"
        else:
            # fallback: assume renderD128 + card_idx offset
            if card_idx < len(render_nodes):
                card_render_map[card_idx] = render_nodes[card_idx]

    if not card_render_map:
        return []

    # Try intel_gpu_top once for all cards (single JSON output)
    top_data: dict = {}
    rc, out, _ = _run("intel_gpu_top", "-J", "-s", "250", "-o", "-", timeout=2)
    if rc == 0 and out.strip():
        try:
            for line in out.strip().splitlines():
                if line.strip().startswith("{"):
                    top_data = json.loads(line)
                    break
        except Exception:
            pass

    for card_idx, render in sorted(card_render_map.items()):
        card_sys = f"/sys/class/drm/card{card_idx}"

        freq = None
        for freq_path in [f"{card_sys}/gt_cur_freq_mhz", f"{card_sys}/gt/gt0/rps_cur_freq_mhz"]:
            try:
                freq = int(Path(freq_path).read_text().strip())
                break
            except Exception:
                pass

        # Utilization from intel_gpu_top
        util = None
        if top_data:
            engines = top_data.get("engines", {})
            render_eng = engines.get("Render/3D/0", engines.get("Render/3D", {}))
            util = _float(render_eng.get("busy", ""))

        # Temperature from hwmon
        temp_c = None
        for hwmon in glob.glob(f"{card_sys}/device/hwmon/hwmon*/temp1_input"):
            try:
                temp_c = round(float(Path(hwmon).read_text().strip()) / 1000, 1)
                break
            except Exception:
                pass

        gpus.append({
            "index":       card_idx,
            "vendor":      "intel",
            "name":        _intel_gpu_name(card_idx),
            "util_pct":    util,
            "mem_used_mb": None,
            "mem_total_mb":None,
            "mem_pct":     None,
            "temp_c":      temp_c,
            "power_w":     None,
            "driver":      "i915",
            "freq_mhz":    freq,
            "render_node": render,
        })
    return gpus


# ── Docker GPU assignments ────────────────────────────────────────────────────

def _docker_gpu_containers() -> list[dict]:
    rc, out, _ = _run("docker", "ps", "--format", "{{json .}}")
    if rc != 0:
        return []

    result = []
    for line in out.strip().splitlines():
        try:
            c = json.loads(line)
        except Exception:
            continue

        cid = c.get("ID", "")[:12]
        name = c.get("Names", "")

        rc2, inspect_out, _ = _run("docker", "inspect", "--format",
                                   "{{json .HostConfig.Devices}}", cid)
        if rc2 != 0:
            continue
        try:
            devices = json.loads(inspect_out.strip() or "null") or []
        except Exception:
            continue

        gpu_devs = [d.get("PathOnHost", "") for d in devices
                    if isinstance(d, dict) and
                    any(x in d.get("PathOnHost", "") for x in ("/dev/dri", "/dev/nvidia"))]
        if gpu_devs:
            result.append({"id": cid, "name": name, "devices": gpu_devs})

    # Also check --gpus all via NVIDIA runtime
    rc3, rt_out, _ = _run("docker", "ps", "--format", "{{json .}}",
                          "--filter", "label=NVIDIA_VISIBLE_DEVICES")
    # Combine (best-effort)
    return result


# ── Overclock helpers ─────────────────────────────────────────────────────────

def _sudo_run(*args, timeout: int = 10) -> tuple[int, str, str]:
    """Run a command with sudo; falls back gracefully if sudo is not available."""
    try:
        r = subprocess.run(["sudo", "-n"] + list(args),
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 1, "", "sudo not available or timed out"


def _sysfs_read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _sysfs_write(path: str, value: str) -> tuple[bool, str]:
    rc, _, err = _sudo_run("tee", path, timeout=5)
    if rc != 0:
        # Also try direct write (works if running as root)
        try:
            Path(path).write_text(value + "\n")
            return True, ""
        except OSError as e:
            return False, str(e)
    return True, ""


# ── NVIDIA OC ────────────────────────────────────────────────────────────────

def _nvidia_oc_state(index: int) -> dict:
    """Get current OC state and limits for one NVIDIA GPU."""
    # Power info
    rc, out, _ = _run("nvidia-smi", "-i", str(index), "-q", "-d", "POWER")
    pwr = {}
    if rc == 0:
        for line in out.splitlines():
            line = line.strip()
            if "Current Power Limit" in line:
                pwr["current_w"] = _float(line.split(":")[-1].strip().split()[0])
            elif "Default Power Limit" in line:
                pwr["default_w"] = _float(line.split(":")[-1].strip().split()[0])
            elif "Min Power Limit" in line:
                pwr["min_w"] = _float(line.split(":")[-1].strip().split()[0])
            elif "Max Power Limit" in line:
                pwr["max_w"] = _float(line.split(":")[-1].strip().split()[0])

    # Clock info
    rc2, out2, _ = _run("nvidia-smi", "-i", str(index),
                         "--query-gpu=clocks.current.sm,clocks.current.memory,"
                         "clocks.max.sm,clocks.max.memory",
                         "--format=csv,noheader,nounits")
    clocks = {}
    if rc2 == 0 and out2.strip():
        parts = [p.strip() for p in out2.strip().split(",")]
        if len(parts) >= 4:
            clocks = {
                "gpu_cur_mhz":  _int(parts[0]),
                "mem_cur_mhz":  _int(parts[1]),
                "gpu_max_mhz":  _int(parts[2]),
                "mem_max_mhz":  _int(parts[3]),
            }

    # Fan (if supported)
    rc3, out3, _ = _run("nvidia-smi", "-i", str(index),
                         "--query-gpu=fan.speed", "--format=csv,noheader,nounits")
    fan_pct = _int(out3.strip()) if rc3 == 0 and out3.strip() not in ("[N/A]", "N/A", "") else None

    # Persistence mode
    rc4, out4, _ = _run("nvidia-smi", "-i", str(index),
                         "--query-gpu=persistence_mode", "--format=csv,noheader")
    persistence = out4.strip().lower() == "enabled" if rc4 == 0 else None

    return {
        "vendor": "nvidia",
        "index": index,
        "supported": True,
        "power": pwr,
        "clocks": clocks,
        "fan_pct": fan_pct,
        "persistence": persistence,
        "note": "Power and clock changes require root/sudo.",
    }


def _nvidia_set_power(index: int, watts: float) -> dict:
    rc, out, err = _sudo_run("nvidia-smi", "-i", str(index), "-pl", str(int(watts)))
    return {"ok": rc == 0, "output": (out or err).strip()}


def _nvidia_set_clocks(index: int, gpu_min: int, gpu_max: int) -> dict:
    rc, out, err = _sudo_run("nvidia-smi", "-i", str(index),
                              f"--lock-gpu-clocks={gpu_min},{gpu_max}")
    return {"ok": rc == 0, "output": (out or err).strip()}


def _nvidia_set_fan(index: int, pct: int | None) -> dict:
    # Fan control via nvidia-smi (supported on some GPUs/drivers)
    if pct is None:
        rc, out, err = _sudo_run("nvidia-smi", "-i", str(index), "--auto-boost-default=1")
    else:
        # Fan speed setting requires nvidia-settings with X, or specific driver support
        # Try nvidia-smi auto-boost disable first
        _sudo_run("nvidia-smi", "-i", str(index), "--auto-boost-default=0")
        rc, out, err = _sudo_run("nvidia-smi", "-i", str(index), f"--fan-speed={pct}")
    return {"ok": rc == 0, "output": (out or err).strip()}


def _nvidia_reset(index: int) -> dict:
    results = []
    rc1, o1, e1 = _sudo_run("nvidia-smi", "-i", str(index), "--reset-gpu-clocks")
    results.append(f"clocks: {'ok' if rc1==0 else (e1 or o1).strip()}")
    # Reset power to default
    state = _nvidia_oc_state(index)
    default_w = state.get("power", {}).get("default_w")
    if default_w:
        rc2, o2, e2 = _sudo_run("nvidia-smi", "-i", str(index), "-pl", str(int(default_w)))
        results.append(f"power: {'ok' if rc2==0 else (e2 or o2).strip()}")
    return {"ok": rc1 == 0, "output": "; ".join(results)}


# ── AMD OC ───────────────────────────────────────────────────────────────────

def _amd_oc_state(index: int) -> dict:
    """Get current OC state for AMD GPU at DRM card index."""
    # Performance level
    perf_path = f"/sys/class/drm/card{index}/device/power_dpm_force_performance_level"
    perf_level = _sysfs_read(perf_path) or "unknown"

    # Power cap
    cap_cur  = _sysfs_read(f"/sys/class/drm/card{index}/device/hwmon/hwmon0/power1_cap")
    cap_min  = _sysfs_read(f"/sys/class/drm/card{index}/device/hwmon/hwmon0/power1_cap_min")
    cap_max  = _sysfs_read(f"/sys/class/drm/card{index}/device/hwmon/hwmon0/power1_cap_max")
    cap_def  = _sysfs_read(f"/sys/class/drm/card{index}/device/hwmon/hwmon0/power1_cap_default")

    def uw_to_w(v: str | None) -> float | None:
        return round(int(v) / 1_000_000, 1) if v and v.isdigit() else None

    # Fan
    fan_input  = _sysfs_read(f"/sys/class/drm/card{index}/device/hwmon/hwmon0/fan1_input")
    fan_target = _sysfs_read(f"/sys/class/drm/card{index}/device/hwmon/hwmon0/fan1_target")
    pwm1       = _sysfs_read(f"/sys/class/drm/card{index}/device/hwmon/hwmon0/pwm1")
    pwm1_en    = _sysfs_read(f"/sys/class/drm/card{index}/device/hwmon/hwmon0/pwm1_enable")
    fan_pct = round(int(pwm1) / 255 * 100) if pwm1 and pwm1.isdigit() else None

    # Clock info from rocm-smi
    rc, out, _ = _run("rocm-smi", "-d", str(index), "--showclkfrq")
    clocks: dict = {}
    if rc == 0:
        section = ""
        for line in out.splitlines():
            if "GPU clocks" in line or "GFX Clocks" in line:
                section = "gpu"
            elif "Mem clocks" in line or "MEM Clocks" in line:
                section = "mem"
            elif "*" in line and section:
                m = re.search(r"(\d+)Mhz", line, re.I)
                if m:
                    clocks[f"{section}_cur_mhz"] = int(m.group(1))

    # Power overdrive limit
    rc2, out2, _ = _run("rocm-smi", "-d", str(index), "--showpoweroverdrive")
    od_pct = None
    if rc2 == 0:
        m = re.search(r"(\d+)%", out2)
        if m:
            od_pct = int(m.group(1))

    return {
        "vendor": "amd",
        "index": index,
        "supported": True,
        "perf_level": perf_level,
        "power": {
            "current_w": uw_to_w(cap_cur),
            "min_w":     uw_to_w(cap_min),
            "max_w":     uw_to_w(cap_max),
            "default_w": uw_to_w(cap_def),
            "od_pct":    od_pct,
        },
        "clocks": clocks,
        "fan_pct": fan_pct,
        "fan_rpm": _int(fan_input) if fan_input else None,
        "pwm1_enable": pwm1_en,
        "note": "Changes written via sysfs hwmon — requires root/sudo.",
    }


def _amd_set_power(index: int, watts: float) -> dict:
    uw = int(watts * 1_000_000)
    path = f"/sys/class/drm/card{index}/device/hwmon/hwmon0/power1_cap"
    ok, err = _sysfs_write(path, str(uw))
    if not ok:
        # Fallback: rocm-smi overdrive
        rc, out, e = _sudo_run("rocm-smi", "-d", str(index), "--setpoweroverdrive", "0")
        return {"ok": rc == 0, "output": (out or e).strip()}
    return {"ok": ok, "output": err or f"Set to {watts}W"}


def _amd_set_perf(index: int, level: str) -> dict:
    path = f"/sys/class/drm/card{index}/device/power_dpm_force_performance_level"
    ok, err = _sysfs_write(path, level)
    return {"ok": ok, "output": err or f"perf_level={level}"}


def _amd_set_fan(index: int, pct: int | None) -> dict:
    enable_path = f"/sys/class/drm/card{index}/device/hwmon/hwmon0/pwm1_enable"
    pwm_path    = f"/sys/class/drm/card{index}/device/hwmon/hwmon0/pwm1"
    if pct is None:
        ok, err = _sysfs_write(enable_path, "2")  # 2 = automatic
        return {"ok": ok, "output": err or "Fan set to auto"}
    _sysfs_write(enable_path, "1")  # 1 = manual
    pwm = max(0, min(255, int(pct / 100 * 255)))
    ok, err = _sysfs_write(pwm_path, str(pwm))
    return {"ok": ok, "output": err or f"Fan set to {pct}%"}


def _amd_reset(index: int) -> dict:
    results = []
    # Reset performance level to auto
    ok1, _ = _sysfs_write(
        f"/sys/class/drm/card{index}/device/power_dpm_force_performance_level", "auto")
    results.append(f"perf_level: {'ok' if ok1 else 'failed'}")
    # Reset fan to auto
    ok2, _ = _sysfs_write(
        f"/sys/class/drm/card{index}/device/hwmon/hwmon0/pwm1_enable", "2")
    results.append(f"fan: {'ok' if ok2 else 'failed'}")
    # Reset power cap to default
    cap_def = _sysfs_read(f"/sys/class/drm/card{index}/device/hwmon/hwmon0/power1_cap_default")
    if cap_def:
        ok3, _ = _sysfs_write(
            f"/sys/class/drm/card{index}/device/hwmon/hwmon0/power1_cap", cap_def)
        results.append(f"power: {'ok' if ok3 else 'failed'}")
    return {"ok": ok1 or ok2, "output": "; ".join(results)}


# ── Intel OC ─────────────────────────────────────────────────────────────────

def _intel_oc_state(index: int) -> dict:
    card = f"/sys/class/drm/card{index}"
    rp0  = _sysfs_read(f"{card}/gt_RP0_freq_mhz")   # max boost
    rp1  = _sysfs_read(f"{card}/gt_RP1_freq_mhz")   # efficient max
    rpn  = _sysfs_read(f"{card}/gt_RPn_freq_mhz")   # min
    cur  = _sysfs_read(f"{card}/gt_cur_freq_mhz")
    mn   = _sysfs_read(f"{card}/gt_min_freq_mhz")
    mx   = _sysfs_read(f"{card}/gt_max_freq_mhz")
    boost= _sysfs_read(f"{card}/gt_boost_freq_mhz")

    return {
        "vendor": "intel",
        "index": index,
        "supported": bool(mn and mx),
        "clocks": {
            "cur_mhz":   _int(cur)   if cur   else None,
            "min_mhz":   _int(mn)    if mn    else None,
            "max_mhz":   _int(mx)    if mx    else None,
            "boost_mhz": _int(boost) if boost else None,
            "rp0_mhz":   _int(rp0)  if rp0   else None,
            "rp1_mhz":   _int(rp1)  if rp1   else None,
            "rpn_mhz":   _int(rpn)  if rpn   else None,
        },
        "power": {},
        "fan_pct": None,
        "note": "Intel GPU frequency control via sysfs gt_min/max_freq_mhz — requires root.",
    }


def _intel_set_clocks(index: int, min_mhz: int, max_mhz: int) -> dict:
    card = f"/sys/class/drm/card{index}"
    ok1, e1 = _sysfs_write(f"{card}/gt_min_freq_mhz", str(min_mhz))
    ok2, e2 = _sysfs_write(f"{card}/gt_max_freq_mhz", str(max_mhz))
    return {"ok": ok1 and ok2, "output": (e1 or e2 or f"{min_mhz}-{max_mhz} MHz")}


def _intel_reset(index: int) -> dict:
    state = _intel_oc_state(index)
    rpn = state["clocks"].get("rpn_mhz")
    rp0 = state["clocks"].get("rp0_mhz")
    if rpn and rp0:
        return _intel_set_clocks(index, rpn, rp0)
    return {"ok": False, "output": "Could not determine default freq range"}


# ── Aggregate OC state ────────────────────────────────────────────────────────

def _oc_state_all() -> list[dict]:
    states = []
    for g in _nvidia_gpus():
        states.append(_nvidia_oc_state(g["index"]))
    for g in _amd_gpus():
        idx = g["index"]
        try:
            states.append(_amd_oc_state(idx))
        except Exception:
            states.append({"vendor": "amd", "index": idx, "supported": False})
    for g in _intel_gpus():
        states.append(_intel_oc_state(g["index"]))
    return states


# ── Docker args helpers ───────────────────────────────────────────────────────

def _docker_args_for_gpu(gpu: dict) -> dict:
    """Return the Docker run flags needed to pass a GPU into a container."""
    vendor = gpu.get("vendor", "")
    if vendor == "nvidia":
        return {
            "runtime": "nvidia",
            "devices": [],
            "groups": [],
            "env": ["NVIDIA_VISIBLE_DEVICES=all", "NVIDIA_DRIVER_CAPABILITIES=compute,utility"],
            "flag_summary": "--gpus all",
            "notes": "Requires nvidia-container-toolkit on the host.",
        }
    if vendor == "amd":
        devices = ["/dev/kfd"]
        # Add the specific render node for this card if known
        for card_path in glob.glob(f"/sys/class/drm/card*/device/drm/renderD*"):
            devices.append(f"/dev/dri/{Path(card_path).name}")
        if not any("/dev/dri/" in d for d in devices):
            devices.append("/dev/dri/renderD128")  # fallback
        return {
            "runtime": None,
            "devices": devices,
            "groups": ["render", "video"],
            "env": ["HSA_OVERRIDE_GFX_VERSION="],
            "flag_summary": " ".join(f"--device={d}" for d in devices) +
                            " --group-add render --group-add video",
            "notes": "Requires ROCm user-space libs on host (run gpu/setup-ai-gpu.sh --amd).",
        }
    if vendor == "intel":
        render = gpu.get("render_node", "/dev/dri/renderD128")
        return {
            "runtime": None,
            "devices": [render],
            "groups": ["render"],
            "env": [],
            "flag_summary": f"--device={render} --group-add render",
            "notes": "Requires intel-level-zero-gpu on host (run gpu/setup-ai-gpu.sh --intel).",
        }
    return {"runtime": None, "devices": [], "groups": [], "env": [], "flag_summary": "", "notes": ""}


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/")
def list_gpus():
    gpus: list[dict] = []
    gpus.extend(_nvidia_gpus())
    gpus.extend(_amd_gpus())
    gpus.extend(_intel_gpus())
    # Attach docker args to each GPU entry
    for g in gpus:
        g["docker_args"] = _docker_args_for_gpu(g)
    return {"available": bool(gpus), "gpus": gpus}


@router.get("/docker")
def gpu_docker_containers():
    return _docker_gpu_containers()


# ── Overclock Pydantic models ──────────────────────────────────────────────────

class PowerReq(BaseModel):
    watts: float

class ClocksReq(BaseModel):
    min_mhz: int
    max_mhz: int

class FanReq(BaseModel):
    pct: int | None = None  # None = auto

class PerfReq(BaseModel):
    level: str  # auto | low | high | manual


# ── Overclock endpoints ────────────────────────────────────────────────────────

@router.get("/overclock/")
def oc_state():
    return _oc_state_all()


def _resolve_gpu(idx: int) -> dict:
    """Find a GPU by its index from the full GPU list."""
    all_gpus: list[dict] = []
    all_gpus.extend(_nvidia_gpus())
    all_gpus.extend(_amd_gpus())
    all_gpus.extend(_intel_gpus())
    for g in all_gpus:
        if g.get("index") == idx:
            return g
    raise HTTPException(status_code=404, detail=f"GPU index {idx} not found")


@router.post("/overclock/{idx}/power")
def oc_set_power(idx: int, req: PowerReq):
    g = _resolve_gpu(idx)
    if g["vendor"] == "nvidia":
        return _nvidia_set_power(idx, req.watts)
    if g["vendor"] == "amd":
        return _amd_set_power(idx, req.watts)
    raise HTTPException(status_code=400, detail="Power limit not supported for this GPU vendor")


@router.post("/overclock/{idx}/clocks")
def oc_set_clocks(idx: int, req: ClocksReq):
    g = _resolve_gpu(idx)
    if g["vendor"] == "nvidia":
        return _nvidia_set_clocks(idx, req.min_mhz, req.max_mhz)
    if g["vendor"] == "intel":
        return _intel_set_clocks(idx, req.min_mhz, req.max_mhz)
    raise HTTPException(status_code=400, detail="Clock locking not supported via this endpoint for AMD (use /perf)")


@router.post("/overclock/{idx}/fan")
def oc_set_fan(idx: int, req: FanReq):
    g = _resolve_gpu(idx)
    if g["vendor"] == "nvidia":
        return _nvidia_set_fan(idx, req.pct)
    if g["vendor"] == "amd":
        return _amd_set_fan(idx, req.pct)
    raise HTTPException(status_code=400, detail="Fan control not supported for Intel GPU")


@router.post("/overclock/{idx}/perf")
def oc_set_perf(idx: int, req: PerfReq):
    g = _resolve_gpu(idx)
    if g["vendor"] != "amd":
        raise HTTPException(status_code=400, detail="Performance level only applies to AMD GPUs")
    allowed = {"auto", "low", "high", "manual"}
    if req.level not in allowed:
        raise HTTPException(status_code=400, detail=f"level must be one of {allowed}")
    return _amd_set_perf(idx, req.level)


@router.post("/overclock/{idx}/reset")
def oc_reset(idx: int):
    g = _resolve_gpu(idx)
    if g["vendor"] == "nvidia":
        return _nvidia_reset(idx)
    if g["vendor"] == "amd":
        return _amd_reset(idx)
    if g["vendor"] == "intel":
        return _intel_reset(idx)
    raise HTTPException(status_code=400, detail="Unknown vendor")


# ── GPU Profiles ──────────────────────────────────────────────────────────────

_GPU_PROFILES_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "gpu_profiles.json")


def _load_gpu_profiles() -> list[dict]:
    try:
        return json.loads(Path(_GPU_PROFILES_FILE).read_text())
    except Exception:
        return []


def _profile_matches_gpu(profile: dict, gpu: dict) -> bool:
    pv = profile.get("vendor", "any")
    if pv == "any":
        return True
    if pv != gpu.get("vendor", ""):
        return False
    patterns = profile.get("gpu_patterns", [])
    if not patterns:
        return True
    name = gpu.get("name", "").lower()
    return any(p.lower() in name for p in patterns)


@router.get("/profiles")
def get_gpu_profiles():
    """Return profiles filtered to detected GPUs, plus generics."""
    all_gpus: list[dict] = []
    all_gpus.extend(_nvidia_gpus())
    all_gpus.extend(_amd_gpus())
    all_gpus.extend(_intel_gpus())

    all_profiles = _load_gpu_profiles()

    result = []
    for gpu in all_gpus:
        matched = [p for p in all_profiles if _profile_matches_gpu(p, gpu)]
        generic = [p for p in all_profiles if p.get("vendor") == "any"]
        result.append({
            "gpu_index":   gpu["index"],
            "gpu_name":    gpu.get("name", ""),
            "gpu_vendor":  gpu.get("vendor", ""),
            "matched":     matched,
            "generic":     generic,
        })

    return {
        "gpus": result,
        "all_profiles": all_profiles,
    }


class ProfileApplyReq(BaseModel):
    profile_id: str


@router.post("/overclock/{idx}/profile")
def oc_apply_profile(idx: int, req: ProfileApplyReq):
    """Apply a GPU OC profile by id to the GPU at index idx."""
    g = _resolve_gpu(idx)
    vendor = g["vendor"]

    all_profiles = _load_gpu_profiles()
    profile = next((p for p in all_profiles if p["id"] == req.profile_id), None)
    if not profile:
        raise HTTPException(404, f"Profile '{req.profile_id}' not found")

    applied = []
    warnings = []

    # ── NVIDIA ──────────────────────────────────────────────────────────────
    if vendor == "nvidia":
        state = _nvidia_oc_state(idx)
        pwr = state.get("power", {})

        # Power limit
        pl_pct = profile.get("power_limit_pct")
        if pl_pct is not None:
            default_w = pwr.get("default_w") or pwr.get("current_w")
            max_w     = pwr.get("max_w")
            if default_w:
                target_w = default_w * pl_pct / 100
                if max_w:
                    target_w = min(target_w, max_w)
                r = _nvidia_set_power(idx, target_w)
                if r["ok"]:
                    applied.append(f"Power → {target_w:.0f} W ({pl_pct}%)")
                else:
                    warnings.append(f"Power: {r['output']}")
            else:
                warnings.append("Power limit: could not read default TDP from nvidia-smi")

        # Clock lock (max_pct of hardware max)
        clock_max_pct = profile.get("clock_max_pct")
        if clock_max_pct is not None:
            clk = state.get("clocks", {})
            gpu_max = clk.get("gpu_max_mhz")
            if gpu_max:
                target_max = int(gpu_max * clock_max_pct / 100)
                target_min = max(300, target_max - 200)
                r = _nvidia_set_clocks(idx, target_min, target_max)
                if r["ok"]:
                    applied.append(f"Clocks → {target_min}–{target_max} MHz")
                else:
                    warnings.append(f"Clocks: {r['output']}")

        # Fan
        fan_pct = profile.get("fan_pct")
        r = _nvidia_set_fan(idx, fan_pct)
        if r["ok"]:
            applied.append(f"Fan → {'auto' if fan_pct is None else str(fan_pct)+'%'}")
        else:
            warnings.append(f"Fan: {r['output']}")

    # ── AMD ──────────────────────────────────────────────────────────────────
    elif vendor == "amd":
        state = _amd_oc_state(idx)

        # Performance level
        perf = profile.get("perf_level")
        if perf:
            r = _amd_set_perf(idx, perf)
            if r["ok"]:
                applied.append(f"Perf level → {perf}")
            else:
                warnings.append(f"Perf level: {r['output']}")

        # Power cap
        pl_pct = profile.get("power_limit_pct")
        if pl_pct is not None:
            pwr = state.get("power", {})
            default_w = pwr.get("default_w") or pwr.get("current_w")
            max_w     = pwr.get("max_w")
            if default_w:
                target_w = default_w * pl_pct / 100
                if max_w:
                    target_w = min(target_w, max_w)
                r = _amd_set_power(idx, target_w)
                if r["ok"]:
                    applied.append(f"Power cap → {target_w:.0f} W ({pl_pct}%)")
                else:
                    warnings.append(f"Power: {r['output']}")
            else:
                warnings.append("Power cap: could not read default from sysfs")

        # Fan
        fan_pct = profile.get("fan_pct")
        r = _amd_set_fan(idx, fan_pct)
        if r["ok"]:
            applied.append(f"Fan → {'auto' if fan_pct is None else str(fan_pct)+'%'}")
        else:
            warnings.append(f"Fan: {r['output']}")

    # ── Intel ────────────────────────────────────────────────────────────────
    elif vendor == "intel":
        state = _intel_oc_state(idx)
        clk = state.get("clocks", {})
        rp0 = clk.get("rp0_mhz") or clk.get("max_mhz")
        rpn = clk.get("rpn_mhz") or clk.get("min_mhz")

        freq_min_pct = profile.get("freq_min_pct", 10)
        freq_max_pct = profile.get("freq_max_pct", 100)

        if rp0:
            target_max = int(rp0 * freq_max_pct / 100)
            base       = rpn or 100
            target_min = int(base + (rp0 - base) * freq_min_pct / 100)
            r = _intel_set_clocks(idx, target_min, target_max)
            if r["ok"]:
                applied.append(f"Freq → {target_min}–{target_max} MHz")
            else:
                warnings.append(f"Freq: {r['output']}")
        else:
            warnings.append("Intel freq: could not read RP0 from sysfs")

    else:
        raise HTTPException(400, f"Unsupported vendor: {vendor}")

    return {
        "ok": len(warnings) == 0,
        "profile": profile["name"],
        "gpu": g.get("name", f"GPU {idx}"),
        "applied": applied,
        "warnings": warnings,
    }
