"""
VFIO GPU passthrough — query eligible GPUs and configure a VM for exclusive GPU access.

GET  /api/vfio/gpus                 — list GPUs with IOMMU group info
GET  /api/vfio/status               — VFIO kernel module + IOMMU status
POST /api/vfio/assign               — assign GPU to a VM (write vfio-pci binding + VM XML hostdev)
POST /api/vfio/unassign             — remove GPU from VM
GET  /api/vfio/vm/{name}/gpu        — which GPU (if any) is assigned to this VM
"""

import glob
import os
import re
import subprocess
from pathlib import Path
from xml.etree import ElementTree as ET

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/vfio", tags=["vfio"])

VFIO_IDS_FILE = "/etc/modprobe.d/vfio.conf"
GRUB_DEFAULT  = "/etc/default/grub"


def _run(*args, timeout: int = 10) -> tuple[int, str]:
    try:
        r = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout + r.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def _sudo(*args, timeout: int = 15) -> tuple[int, str]:
    return _run("sudo", "-n", *args, timeout=timeout)


def _sysfs(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def _iommu_groups() -> dict[str, list[str]]:
    """Return dict of {group_id: [pci_addr, ...]}"""
    groups: dict[str, list[str]] = {}
    for path in glob.glob("/sys/kernel/iommu_groups/*/devices/*"):
        parts = path.split("/")
        group = parts[4]
        pci_addr = parts[6]
        groups.setdefault(group, []).append(pci_addr)
    return groups


def _pci_info(addr: str) -> dict:
    """Get vendor/device name for a PCI address."""
    rc, out = _run("lspci", "-vmm", "-s", addr)
    info = {}
    for line in out.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            info[k.strip().lower()] = v.strip()
    return info


def _current_driver(addr: str) -> str:
    return _sysfs(f"/sys/bus/pci/devices/{addr}/driver").split("/")[-1] if _sysfs(f"/sys/bus/pci/devices/{addr}/driver") else ""


def _get_gpus() -> list[dict]:
    """Find GPUs (class 0x0300* = VGA/3D) with IOMMU group info."""
    groups = _iommu_groups()
    # Reverse map pci → group
    pci_to_group: dict[str, str] = {}
    for grp, addrs in groups.items():
        for a in addrs:
            pci_to_group[a] = grp

    gpus = []
    for path in glob.glob("/sys/bus/pci/devices/*"):
        addr = Path(path).name
        cls = _sysfs(f"{path}/class")
        if not (cls.startswith("0x0300") or cls.startswith("0x0302")):
            continue
        vendor_id = _sysfs(f"{path}/vendor").replace("0x", "")
        device_id = _sysfs(f"{path}/device").replace("0x", "")
        vendor_map = {"10de": "NVIDIA", "1002": "AMD", "8086": "Intel"}
        vendor_name = vendor_map.get(vendor_id.lower(), vendor_id)
        info = _pci_info(addr)
        group = pci_to_group.get(addr, "?")
        group_peers = groups.get(group, [])
        driver = _current_driver(addr)
        # Is it safe for passthrough? (only if full IOMMU group can be passed through)
        non_bridge_peers = [
            p for p in group_peers if p != addr and
            not _sysfs(f"/sys/bus/pci/devices/{p}/class").startswith("0x0604")
        ]
        passthrough_ready = len(non_bridge_peers) == 0

        gpus.append({
            "addr":             addr,
            "vendor":           vendor_name,
            "vendor_id":        vendor_id,
            "device_id":        device_id,
            "name":             info.get("device", f"{vendor_name} GPU"),
            "iommu_group":      group,
            "group_devices":    group_peers,
            "current_driver":   driver,
            "vfio_bound":       driver == "vfio-pci",
            "passthrough_ready": passthrough_ready,
            "note":             "" if passthrough_ready else
                                f"Group {group} has {len(non_bridge_peers)} other non-bridge device(s) — all must be passed through together",
        })
    return sorted(gpus, key=lambda g: g["addr"])


# ── Endpoints ─────────────────────────────────────────────────────

@router.get("/status")
def vfio_status():
    # IOMMU: check kernel cmdline
    cmdline = _sysfs("/proc/cmdline")
    iommu_enabled = "intel_iommu=on" in cmdline or "amd_iommu=on" in cmdline or "iommu=pt" in cmdline

    # VFIO modules loaded
    rc, lsmod = _run("lsmod")
    vfio_loaded = "vfio_pci" in lsmod

    # Existing vfio.conf ids
    ids_bound = []
    try:
        for line in Path(VFIO_IDS_FILE).read_text().splitlines():
            m = re.search(r"ids=([0-9a-fA-F:, ]+)", line)
            if m:
                ids_bound = [i.strip() for i in m.group(1).split(",") if i.strip()]
    except FileNotFoundError:
        pass

    return {
        "iommu_enabled":  iommu_enabled,
        "vfio_pci_loaded": vfio_loaded,
        "bound_ids":      ids_bound,
        "cmdline_note":   "Add intel_iommu=on iommu=pt (Intel) or amd_iommu=on iommu=pt (AMD) to kernel parameters and reboot." if not iommu_enabled else "",
    }


@router.get("/gpus")
def list_gpus():
    return _get_gpus()


class AssignReq(BaseModel):
    pci_addr: str    # e.g. "0000:01:00.0"
    vm_name: str


class UnassignReq(BaseModel):
    pci_addr: str
    vm_name: str


@router.post("/assign")
def assign_gpu(req: AssignReq):
    gpus = _get_gpus()
    gpu = next((g for g in gpus if g["addr"] == req.pci_addr), None)
    if not gpu:
        raise HTTPException(404, f"GPU {req.pci_addr} not found")

    vendor_id = gpu["vendor_id"]
    device_id = gpu["device_id"]
    pci_id = f"{vendor_id}:{device_id}".lower()

    # 1. Write vfio-pci binding
    # Collect all device IDs in the same IOMMU group that need binding
    bind_ids = set()
    for peer_addr in gpu["group_devices"]:
        peer_vid = _sysfs(f"/sys/bus/pci/devices/{peer_addr}/vendor").replace("0x", "").lower()
        peer_did = _sysfs(f"/sys/bus/pci/devices/{peer_addr}/device").replace("0x", "").lower()
        if peer_vid and peer_did:
            bind_ids.add(f"{peer_vid}:{peer_did}")

    ids_str = ",".join(sorted(bind_ids))
    conf_content = f"options vfio-pci ids={ids_str}\nsoftdep nvidia pre: vfio-pci\n"

    import subprocess as sp
    r = sp.run(["sudo", "-n", "tee", VFIO_IDS_FILE],
               input=conf_content, capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        raise HTTPException(500, f"Could not write {VFIO_IDS_FILE}: {r.stderr}")

    # 2. Try to rebind to vfio-pci now (best-effort, may need reboot)
    for peer_addr in gpu["group_devices"]:
        peer_vid = _sysfs(f"/sys/bus/pci/devices/{peer_addr}/vendor").replace("0x","").lower()
        peer_did = _sysfs(f"/sys/bus/pci/devices/{peer_addr}/device").replace("0x","").lower()
        _sudo("bash", "-c",
              f"echo {peer_vid} {peer_did} > /sys/bus/pci/drivers/vfio-pci/new_id 2>/dev/null; "
              f"echo {peer_addr} > /sys/bus/pci/drivers/{_current_driver(peer_addr) or 'nvidia'}/unbind 2>/dev/null; "
              f"echo {peer_addr} > /sys/bus/pci/drivers/vfio-pci/bind 2>/dev/null")

    # 3. Patch VM XML with hostdev element
    vm_errors = []
    if req.vm_name:
        vm_errors = _add_hostdev_to_vm(req.vm_name, gpu["group_devices"])

    return {
        "ok": True,
        "bound_ids": ids_str,
        "vm_name": req.vm_name,
        "vm_errors": vm_errors,
        "note": "VFIO binding written. If the driver is still active, a reboot is required for the rebind to take effect.",
    }


def _add_hostdev_to_vm(vm_name: str, pci_addrs: list[str]) -> list[str]:
    """Add PCIe hostdev elements to a VM's libvirt XML."""
    errors = []
    rc, xml = _run("virsh", "dumpxml", vm_name)
    if rc != 0:
        return [f"Could not get VM XML: {xml.strip()}"]
    try:
        root = ET.fromstring(xml)
        devices = root.find("devices")
        if devices is None:
            return ["No <devices> element in VM XML"]

        # Remove existing vfio hostdevs
        for existing in list(devices.findall("hostdev[@type='pci']")):
            devices.remove(existing)

        for addr in pci_addrs:
            # Parse addr like "0000:01:00.0" → domain=0, bus=1, slot=0, function=0
            m = re.match(r"(\w+):(\w+):(\w+)\.(\w+)", addr)
            if not m:
                errors.append(f"Could not parse PCI addr: {addr}")
                continue
            domain, bus, slot, func = m.groups()
            hostdev = ET.SubElement(devices, "hostdev", attrib={
                "mode": "subsystem", "type": "pci", "managed": "yes"})
            src = ET.SubElement(hostdev, "source")
            ET.SubElement(src, "address", attrib={
                "type": "pci",
                "domain": f"0x{domain}",
                "bus":    f"0x{bus}",
                "slot":   f"0x{slot}",
                "function": f"0x{func}",
            })
            ET.SubElement(hostdev, "rom", attrib={"bar": "on"})

        # Write back via virsh define
        import tempfile
        new_xml = ET.tostring(root, encoding="unicode")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False) as f:
            f.write(new_xml)
            tmppath = f.name
        rc2, out2 = _run("virsh", "define", tmppath)
        Path(tmppath).unlink(missing_ok=True)
        if rc2 != 0:
            errors.append(f"virsh define failed: {out2.strip()}")
    except Exception as e:
        errors.append(str(e))
    return errors


@router.post("/unassign")
def unassign_gpu(req: UnassignReq):
    if req.vm_name:
        # Remove hostdev from VM XML
        rc, xml = _run("virsh", "dumpxml", req.vm_name)
        if rc == 0:
            try:
                root = ET.fromstring(xml)
                devices = root.find("devices")
                if devices is not None:
                    for hd in list(devices.findall("hostdev[@type='pci']")):
                        devices.remove(hd)
                import tempfile
                new_xml = ET.tostring(root, encoding="unicode")
                with tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False) as f:
                    f.write(new_xml)
                    tmppath = f.name
                _run("virsh", "define", tmppath)
                Path(tmppath).unlink(missing_ok=True)
            except Exception:
                pass

    # Remove vfio.conf
    try:
        Path(VFIO_IDS_FILE).unlink()
    except FileNotFoundError:
        pass

    return {"ok": True, "message": "VFIO binding removed. Reboot to restore normal GPU driver."}


@router.get("/vm/{vm_name}/gpu")
def vm_gpu_assignment(vm_name: str):
    rc, xml = _run("virsh", "dumpxml", vm_name)
    if rc != 0:
        raise HTTPException(404, f"VM '{vm_name}' not found")
    try:
        root = ET.fromstring(xml)
        devices = root.find("devices")
        hostdevs = []
        if devices:
            for hd in devices.findall("hostdev[@type='pci']"):
                src = hd.find("source/address")
                if src is not None:
                    hostdevs.append({
                        "domain":   src.get("domain", ""),
                        "bus":      src.get("bus", ""),
                        "slot":     src.get("slot", ""),
                        "function": src.get("function", ""),
                    })
        return {"vm": vm_name, "hostdevs": hostdevs}
    except ET.ParseError as e:
        raise HTTPException(500, str(e))
