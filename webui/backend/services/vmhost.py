"""What this machine can offer a VM: CPUs, memory, PCI and USB devices, networks, firmware, ISOs, settings.

Everything here is read-only except the settings file. No libvirt needed (the router adds libvirt-side facts).
"""
import glob
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from config import HELIX_CONFIG

SETTINGS_FILE = os.path.join(HELIX_CONFIG, "vm.cfg")
DEFAULT_SETTINGS = {
    "enabled": False,
    "storage": "/mnt/user/domains",      # default folder for new vDisks (a domains/<VM name>/ folder is made below it)
    "isos": "/mnt/user/isos",            # where install ISOs are looked for
    "virtio_iso": "",                    # path of the VirtIO drivers ISO once downloaded
    "default_network": "default",       # libvirt network or bridge new VMs use
    "shutdown_action": "stop",          # what happens to running VMs when the array stops: stop | hibernate
    "shutdown_timeout": 60,             # seconds a VM gets to shut down before it is forced off
    "usage_stats": True,
}
ACTIONS = ("stop", "hibernate")


def _run(cmd, timeout=8) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _read(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


# ── settings ─────────────────────────────────────────────────────────────────

def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    try:
        s.update({k: v for k, v in json.loads(Path(SETTINGS_FILE).read_text()).items() if k in DEFAULT_SETTINGS})
    except (OSError, ValueError):
        pass
    return s


def validate_settings(new: dict) -> dict:
    """Return cleaned values or raise ValueError naming the field."""
    s = {**load_settings(), **{k: v for k, v in new.items() if k in DEFAULT_SETTINGS}}
    for k in ("storage", "isos"):
        p = str(s[k]).rstrip("/") or "/"
        if not p.startswith("/mnt/") or ".." in p.split("/") or len(p) > 200:
            raise ValueError(f"{k}: use a folder under /mnt, for example /mnt/user/domains")
        s[k] = p
    if s["virtio_iso"] and (not str(s["virtio_iso"]).startswith("/") or ".." in str(s["virtio_iso"]).split("/")):
        raise ValueError("virtio_iso: must be a full path")
    if not re.match(r"^[A-Za-z0-9._:-]{1,32}$", str(s["default_network"])):
        raise ValueError("default_network: not a valid network name")
    if s["shutdown_action"] not in ACTIONS:
        raise ValueError("shutdown_action must be stop or hibernate")
    s["shutdown_timeout"] = int(s["shutdown_timeout"])
    if not 10 <= s["shutdown_timeout"] <= 600:
        raise ValueError("shutdown_timeout: between 10 and 600 seconds")
    s["enabled"] = bool(s["enabled"]); s["usage_stats"] = bool(s["usage_stats"])
    return s


def save_settings(s: dict) -> None:
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    tmp = SETTINGS_FILE + ".tmp"
    Path(tmp).write_text(json.dumps(s, indent=2))
    os.replace(tmp, SETTINGS_FILE)


# ── CPU, memory, virtualization ──────────────────────────────────────────────

def cpu_info() -> dict:
    flags = ""
    model = ""
    for line in _read("/proc/cpuinfo").splitlines():
        if line.startswith("flags") and not flags:
            flags = line
        if line.startswith("model name") and not model:
            model = line.split(":", 1)[1].strip()
    vendor = "amd" if ("svm" in flags.split() or "AMD" in model) else "intel" if "vmx" in flags.split() else ""
    cpus = []
    for d in sorted(glob.glob("/sys/devices/system/cpu/cpu[0-9]*"), key=lambda p: int(re.sub(r"\D", "", p))):
        n = int(re.sub(r"\D", "", os.path.basename(d)))
        if _read(f"{d}/online") == "0":
            continue
        cpus.append({"cpu": n, "core": _read(f"{d}/topology/core_id"), "package": _read(f"{d}/topology/physical_package_id"),
                     "siblings": _read(f"{d}/topology/thread_siblings_list")})
    # group hyper-threads together so the pinning grid shows one row per core
    cores: dict[tuple, list[int]] = {}
    for c in cpus:
        cores.setdefault((c["package"], c["core"]), []).append(c["cpu"])
    return {"model": model, "vendor": vendor, "threads": len(cpus), "cores": [sorted(v) for v in cores.values()],
            "hw_virtualization": "vmx" in flags.split() or "svm" in flags.split()}


def memory_total_mb() -> int:
    for line in _read("/proc/meminfo").splitlines():
        if line.startswith("MemTotal"):
            return int(line.split()[1]) // 1024
    return 0


def kvm_available() -> bool:
    return os.path.exists("/dev/kvm")


# ── PCI / USB ────────────────────────────────────────────────────────────────

PCI_CLASS = {"0300": "GPU", "0302": "GPU", "0380": "GPU", "0403": "Audio", "0401": "Audio", "0200": "Network", "0280": "Network",
             "0c03": "USB controller", "0106": "SATA controller", "0108": "NVMe", "0107": "SAS controller", "0104": "RAID controller",
             "0c80": "Serial bus", "0d11": "Bluetooth", "0b40": "Co-processor"}


def pci_devices() -> list[dict]:
    names: dict[str, str] = {}
    for line in _run(["lspci", "-D", "-mm"]).splitlines():
        m = re.match(r'^(\S+) "([^"]*)" "([^"]*)" "([^"]*)"', line)
        if m:
            names[m.group(1)] = f"{m.group(3)} {m.group(4)}".strip()
    groups: dict[str, str] = {}
    for p in glob.glob("/sys/kernel/iommu_groups/*/devices/*"):
        groups[os.path.basename(p)] = p.split("/")[4]
    out = []
    for d in sorted(glob.glob("/sys/bus/pci/devices/*")):
        addr = os.path.basename(d)
        cls = _read(f"{d}/class")[2:6]
        drv = os.path.basename(os.path.realpath(f"{d}/driver")) if os.path.exists(f"{d}/driver") else ""
        out.append({
            "address": addr, "name": names.get(addr, addr), "class": PCI_CLASS.get(cls, ""), "class_id": cls,
            "vendor": _read(f"{d}/vendor")[2:], "device": _read(f"{d}/device")[2:], "driver": drv,
            "iommu_group": groups.get(addr), "bound_to_vfio": drv == "vfio-pci",
            "is_bridge": cls.startswith("06"), "boot_vga": _read(f"{d}/boot_vga") == "1",
        })
    return out


def usb_devices() -> list[dict]:
    out = []
    for d in sorted(glob.glob("/sys/bus/usb/devices/*")):
        base = os.path.basename(d)
        if ":" in base or not os.path.exists(f"{d}/idVendor"):
            continue
        vid, pid = _read(f"{d}/idVendor"), _read(f"{d}/idProduct")
        if vid == "1d6b":                       # Linux Foundation root hubs
            continue
        if _read(f"{d}/bDeviceClass") == "09":  # hubs
            continue
        storage = any(_read(f"{i}/bInterfaceClass") == "08" for i in glob.glob(f"{d}/{base}:*"))
        out.append({"vendor": vid, "product": pid, "name": (_read(f"{d}/product") or "USB device"),
                    "manufacturer": _read(f"{d}/manufacturer"), "bus": _read(f"{d}/busnum"), "dev": _read(f"{d}/devnum"),
                    "storage": storage})
    return out


# ── firmware, networks, ISOs ─────────────────────────────────────────────────

def ovmf_files() -> dict:
    """{"ovmf": (code, vars), "ovmf-secure": (code, vars)} for the files that exist here."""
    d = "/usr/share/OVMF"
    pairs = {"ovmf": ("OVMF_CODE_4M.fd", "OVMF_VARS_4M.fd"), "ovmf-secure": ("OVMF_CODE_4M.secboot.fd", "OVMF_VARS_4M.ms.fd")}
    alt = {"ovmf": ("OVMF_CODE.fd", "OVMF_VARS.fd"), "ovmf-secure": ("OVMF_CODE.secboot.fd", "OVMF_VARS.ms.fd")}
    out = {}
    for k in pairs:
        for cand in (pairs[k], alt[k]):
            if all(os.path.exists(f"{d}/{f}") for f in cand):
                out[k] = (f"{d}/{cand[0]}", f"{d}/{cand[1]}")
                break
    return out


def bridges() -> list[str]:
    """Host bridges a VM can attach to (br0 and friends), not libvirt's own or Docker's."""
    try:
        links = json.loads(_run(["ip", "-j", "link"]) or "[]")
    except ValueError:
        return []
    out = []
    for l in links:
        if (l.get("linkinfo") or {}).get("info_kind") == "bridge":
            n = l["ifname"]
            if not (n.startswith(("virbr", "docker")) or re.match(r"^br-[0-9a-f]{12}$", n)):
                out.append(n)
    return sorted(out)


def list_isos(folder: str, limit: int = 300) -> list[dict]:
    out = []
    if not os.path.isdir(folder):
        return out
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".")][:50]
        for f in files:
            if f.lower().endswith((".iso", ".img")) and not f.startswith("."):
                p = os.path.join(root, f)
                try:
                    out.append({"path": p, "name": f, "size": os.path.getsize(p)})
                except OSError:
                    pass
        if len(out) >= limit:
            break
    return sorted(out, key=lambda x: x["name"].lower())


def fs_type(path: str) -> str:
    p = path
    while p and not os.path.exists(p):
        p = os.path.dirname(p)
    return _run(["stat", "-f", "-c", "%T", p or "/"]).strip()


def libvirt_installed() -> bool:
    return shutil.which("virsh") is not None
