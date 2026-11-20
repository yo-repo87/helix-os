"""Hand PCI devices (GPUs and their audio functions) to vfio-pci for VM passthrough, safely.

Two lessons from real hardware (CLAUDE.md §7):
- Unbinding a GPU from its graphics driver while anything has it open (Plex, a CUDA container,
  nvidia-persistenced) deadlocks inside the kernel: the writer and everything that later reads that
  device's sysfs hang in D state until reboot. So the live handover is refused while the device is in
  use; the saved binding then takes effect at the next boot instead.
- `options vfio-pci ids=VID:DID` binds EVERY card with that ID (two identical GPUs: both lost). Bindings
  are therefore per PCI address, saved in /boot/config/vfio-pci.cfg (`BIND=<addr> <addr> ...`) and applied
  at boot with driver_override by helix-vfio-bind, before udev loads any GPU driver.
"""
import os
import re
import subprocess
from pathlib import Path

CFG = os.environ.get("HELIX_VFIO_CFG", "/boot/config/vfio-pci.cfg")
SYSFS = os.environ.get("HELIX_SYSFS_PCI", "/sys/bus/pci/devices")
ADDR_RE = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")
# Drivers that can have a GPU open from userspace. Anything else (snd_hda_intel, xhci_hcd, no driver)
# is unbound without the in-use check.
GPU_DRIVERS = {"nvidia", "nouveau", "amdgpu", "radeon", "i915", "xe"}
STEP_TIMEOUT = 20


def valid_addr(addr: str) -> bool:
    return bool(ADDR_RE.match(addr or ""))


def driver(addr: str) -> str:
    try:
        return os.path.basename(os.readlink(f"{SYSFS}/{addr}/driver"))
    except OSError:
        return ""


# ── Saved bindings ─────────────────────────────────────────────────────────────

def saved() -> list[str]:
    try:
        text = Path(CFG).read_text()
    except OSError:
        return []
    out: list[str] = []
    for line in text.splitlines():
        if line.startswith("BIND="):
            out += [a for a in line[5:].split() if valid_addr(a) and a not in out]
    return out


def _write(addrs: list[str]) -> None:
    body = "# Helix OS: PCI devices handed to vfio-pci at boot (managed by the VM page)\n"
    if addrs:
        body += "BIND=" + " ".join(addrs) + "\n"
    tmp = CFG + ".tmp"
    try:
        Path(tmp).write_text(body)
        os.replace(tmp, CFG)
    except OSError:
        r = subprocess.run(["sudo", "-n", "tee", CFG], input=body, capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            raise OSError(f"could not write {CFG}: {r.stderr.strip()}")


def save(addrs: list[str]) -> list[str]:
    bad = [a for a in addrs if not valid_addr(a)]
    if bad:
        raise ValueError(f"not a PCI address: {', '.join(bad)}")
    cur = saved()
    _write(cur + [a for a in addrs if a not in cur])
    return saved()


def forget(addrs: list[str]) -> list[str]:
    _write([a for a in saved() if a not in addrs])
    return saved()


# ── Who is using a device ─────────────────────────────────────────────────────

def _device_files(addr: str, drv: str) -> list[str]:
    files: list[str] = []
    if drv == "nvidia":
        # nvidiactl / nvidia-uvm are shared by every NVIDIA GPU, so any user of them counts for all.
        files += sorted(str(p) for p in Path("/dev").glob("nvidia*") if not p.is_dir())
    for p in Path(f"{SYSFS}/{addr}/drm").glob("*"):
        if (Path("/dev/dri") / p.name).exists():
            files.append(f"/dev/dri/{p.name}")
    return files


def _fuser(files: list[str]) -> list[int]:
    if not files:
        return []
    try:
        r = subprocess.run(["sudo", "-n", "fuser", *files], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return [-1]  # unknown: treat as in use
    if r.returncode not in (0, 1):
        return [-1]
    return sorted({int(p) for p in re.findall(r"\b(\d+)\b", r.stdout)})


def users(addr: str, drv: str | None = None, fuser=_fuser) -> list[str]:
    """Processes that have this device open, as 'name (pid N)'. Empty = safe to unbind."""
    drv = driver(addr) if drv is None else drv
    if drv not in GPU_DRIVERS:
        return []
    out = []
    for pid in fuser(_device_files(addr, drv)):
        if pid < 0:
            out.append("(could not check which programs use it)")
            continue
        try:
            name = Path(f"/proc/{pid}/comm").read_text().strip()
        except OSError:
            name = "?"
        out.append(f"{name} (pid {pid})")
    return out


# ── Live handover ─────────────────────────────────────────────────────────────

def _root_write(path: str, value: str) -> bool:
    """One sysfs write as root, bounded by a timeout so a stuck driver cannot hang the caller."""
    try:
        r = subprocess.run(["sudo", "-n", "timeout", str(STEP_TIMEOUT), "tee", path], input=value + "\n",
                           capture_output=True, text=True, timeout=STEP_TIMEOUT + 5)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def handover(addrs: list[str]) -> dict:
    """Move each device to vfio-pci now if that is safe. Returns {moved, skipped: {addr: reason}}."""
    moved, skipped = [], {}
    # Refuse the whole group if any member is busy: passing half an IOMMU group is useless anyway.
    for a in addrs:
        u = users(a)
        if u:
            skipped = {x: f"in use by {', '.join(u)}" for x in addrs if driver(x) != "vfio-pci"}
            return {"moved": [], "skipped": skipped}
    subprocess.run(["sudo", "-n", "modprobe", "vfio-pci"], capture_output=True, timeout=30)
    for a in addrs:
        cur = driver(a)
        if cur == "vfio-pci":
            moved.append(a)
            continue
        ok = _root_write(f"{SYSFS}/{a}/driver_override", "vfio-pci")
        if ok and cur:
            ok = _root_write(f"{SYSFS}/{a}/driver/unbind", a)
        if ok:
            _root_write("/sys/bus/pci/drivers_probe", a)
        if driver(a) == "vfio-pci":
            moved.append(a)
        else:
            skipped[a] = "the kernel did not complete the switch; reboot to apply"
    return {"moved": moved, "skipped": skipped}


def release(addrs: list[str]) -> dict:
    """Give devices back to their normal driver (unbinding vfio-pci is always safe)."""
    back = []
    for a in addrs:
        _root_write(f"{SYSFS}/{a}/driver_override", "")
        if driver(a) == "vfio-pci":
            _root_write(f"{SYSFS}/{a}/driver/unbind", a)
        _root_write("/sys/bus/pci/drivers_probe", a)
        back.append({"addr": a, "driver": driver(a)})
    return {"devices": back}
