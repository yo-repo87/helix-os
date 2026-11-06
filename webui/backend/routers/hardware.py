"""
Hardware profile — CPU info, PCI devices, memory modules, motherboard.

GET /api/hardware/           — full summary
GET /api/hardware/pci        — PCI device list
GET /api/hardware/cpu        — CPU details + governor
POST /api/hardware/governor  — set CPU frequency governor
"""

import glob
import os
import re
import subprocess
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/hardware", tags=["hardware"])


def _run(*args, timeout: int = 10) -> tuple[int, str]:
    try:
        r = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout + r.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 1, ""


def _sudo(*args, timeout: int = 10) -> tuple[int, str]:
    rc, out = _run("sudo", "-n", *args, timeout=timeout)
    return rc, out


def _sysfs(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


# ── CPU ───────────────────────────────────────────────────────────────────────

def _cpu_info() -> dict:
    # /proc/cpuinfo for model name
    model = ""
    cores_total = 0
    try:
        text = Path("/proc/cpuinfo").read_text()
        for line in text.splitlines():
            if "model name" in line.lower() and not model:
                model = line.split(":", 1)[-1].strip()
            if line.strip().startswith("processor"):
                cores_total += 1
    except OSError:
        pass

    # lscpu for topology
    rc, out = _run("lscpu")
    sockets = threads_per_core = cores_per_socket = 1
    arch = ""
    if rc == 0:
        for line in out.splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            k, v = k.strip().lower(), v.strip()
            if "socket(s)" in k:
                sockets = int(v) if v.isdigit() else 1
            elif "thread(s) per core" in k:
                threads_per_core = int(v) if v.isdigit() else 1
            elif "core(s) per socket" in k:
                cores_per_socket = int(v) if v.isdigit() else 1
            elif k == "architecture":
                arch = v

    phys_cores = sockets * cores_per_socket

    # Governor
    gov_path = "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"
    gov_avail_path = "/sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors"
    cur_gov = _sysfs(gov_path) or "unknown"
    avail_govs = _sysfs(gov_avail_path).split() or []

    # Frequencies
    cur_mhz_vals = []
    for p in sorted(glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq"))[:8]:
        v = _sysfs(p)
        if v.isdigit():
            cur_mhz_vals.append(round(int(v) / 1000))
    max_mhz = _sysfs("/sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq")
    min_mhz = _sysfs("/sys/devices/system/cpu/cpu0/cpufreq/scaling_min_freq")
    cpuinfo_max = _sysfs("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")

    return {
        "model":           model,
        "architecture":    arch,
        "sockets":         sockets,
        "physical_cores":  phys_cores,
        "logical_cores":   cores_total,
        "threads_per_core": threads_per_core,
        "governor":        cur_gov,
        "available_governors": avail_govs,
        "cur_mhz_per_core": cur_mhz_vals,
        "max_mhz":  round(int(max_mhz) / 1000) if max_mhz.isdigit() else None,
        "min_mhz":  round(int(min_mhz) / 1000) if min_mhz.isdigit() else None,
        "boost_mhz": round(int(cpuinfo_max) / 1000) if cpuinfo_max.isdigit() else None,
    }


# ── PCI ───────────────────────────────────────────────────────────────────────

def _pci_devices() -> list[dict]:
    rc, out = _run("lspci", "-vmm")
    if rc != 0:
        return []

    devices: list[dict] = []
    cur: dict = {}
    for line in out.splitlines():
        line = line.strip()
        if not line:
            if cur:
                devices.append(cur)
                cur = {}
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            cur[k.strip().lower()] = v.strip()
    if cur:
        devices.append(cur)

    result = []
    for d in devices:
        result.append({
            "slot":    d.get("slot", ""),
            "class":   d.get("class", ""),
            "vendor":  d.get("vendor", ""),
            "device":  d.get("device", ""),
            "svendor": d.get("svendor", ""),
            "sdevice": d.get("sdevice", ""),
            "driver":  d.get("driver", ""),
            "rev":     d.get("rev", ""),
        })
    return result


# ── Memory ────────────────────────────────────────────────────────────────────

def _memory_info() -> dict:
    # Basic from /proc/meminfo
    mem: dict = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            mem[k.strip()] = v.strip()
    except OSError:
        pass

    total_kb = int(mem.get("MemTotal", "0 kB").split()[0])
    free_kb  = int(mem.get("MemAvailable", "0 kB").split()[0])

    # dmidecode for DIMM slots (needs root)
    slots: list[dict] = []
    rc, out = _sudo("dmidecode", "-t", "memory", timeout=8)
    if rc == 0:
        dimm: dict = {}
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("Memory Device"):
                if dimm:
                    slots.append(dimm)
                dimm = {}
            elif ":" in line:
                k, v = line.split(":", 1)
                k, v = k.strip(), v.strip()
                if k in ("Size", "Type", "Speed", "Manufacturer", "Part Number",
                         "Locator", "Bank Locator"):
                    dimm[k] = v
        if dimm:
            slots.append(dimm)
        slots = [s for s in slots if s.get("Size", "No Module") not in ("No Module Installed", "")]

    return {
        "total_gb":     round(total_kb / 1024 / 1024, 2),
        "available_gb": round(free_kb  / 1024 / 1024, 2),
        "dimms":        slots,
    }


# ── Motherboard ───────────────────────────────────────────────────────────────

def _board_info() -> dict:
    info: dict = {}
    # sysfs DMI (no root needed)
    for key, path in [
        ("manufacturer", "/sys/class/dmi/id/board_vendor"),
        ("product",      "/sys/class/dmi/id/board_name"),
        ("version",      "/sys/class/dmi/id/board_version"),
        ("bios_vendor",  "/sys/class/dmi/id/bios_vendor"),
        ("bios_version", "/sys/class/dmi/id/bios_version"),
        ("bios_date",    "/sys/class/dmi/id/bios_date"),
        ("sys_vendor",   "/sys/class/dmi/id/sys_vendor"),
        ("sys_product",  "/sys/class/dmi/id/product_name"),
    ]:
        v = _sysfs(path)
        if v and v not in ("", "Unknown", "To be filled by O.E.M."):
            info[key] = v
    return info


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/")
def hardware_summary():
    return {
        "cpu":    _cpu_info(),
        "memory": _memory_info(),
        "board":  _board_info(),
    }


@router.get("/pci")
def pci_devices():
    return _pci_devices()


@router.get("/cpu")
def cpu_info():
    return _cpu_info()


class GovernorReq(BaseModel):
    governor: str


@router.post("/governor")
def set_governor(req: GovernorReq):
    """Set CPU frequency governor on all cores."""
    allowed_path = "/sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors"
    available = _sysfs(allowed_path).split()
    if available and req.governor not in available:
        raise HTTPException(400, f"Governor '{req.governor}' not available. Options: {available}")

    errors = []
    for path in glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor"):
        rc, out = _sudo("tee", path, timeout=5)
        if rc != 0:
            # Direct write (if running as root)
            try:
                Path(path).write_text(req.governor + "\n")
            except OSError as e:
                errors.append(str(e))

    # Fallback: cpupower
    if errors:
        rc2, out2 = _sudo("cpupower", "frequency-set", "-g", req.governor)
        if rc2 == 0:
            errors = []

    return {"ok": not errors, "governor": req.governor,
            "errors": errors[:3] if errors else []}
