"""
VM management via libvirt / virsh CLI.

GET    /api/vms/                        — list all domains (or {available:false})
POST   /api/vms/                        — create VM (virt-install)
GET    /api/vms/isos                    — scan for ISO files
GET    /api/vms/os-variants             — common OS variant list
GET    /api/vms/{name}                  — domain detail
DELETE /api/vms/{name}                  — undefine (+ optional disk removal)
POST   /api/vms/{name}/start
POST   /api/vms/{name}/stop             — graceful shutdown
POST   /api/vms/{name}/force-stop       — virsh destroy
POST   /api/vms/{name}/pause
POST   /api/vms/{name}/resume
POST   /api/vms/{name}/reboot
PUT    /api/vms/{name}/autostart        — {enabled: bool}
GET    /api/vms/{name}/vnc              — {port, display, host}
GET    /api/vms/{name}/snapshots
POST   /api/vms/{name}/snapshots        — {name, description?}
DELETE /api/vms/{name}/snapshots/{snap}
"""

import glob
import json
import os
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/vms", tags=["vms"])

HELIX_CONFIG = os.getenv("HELIX_CONFIG", "/etc/helix")
STATE_DIR   = os.getenv("STATE_DIR",   "/var/lib/helix")

ISO_SEARCH_PATHS = [
    "/var/lib/libvirt/boot",
    "/mnt/user/isos",
    "/mnt/isos",
    os.path.expanduser("~/Downloads"),
    "/tmp",
]

_IS_ROOT = os.geteuid() == 0

COMMON_OS_VARIANTS = [
    {"id": "ubuntu24.04",      "name": "Ubuntu 24.04 LTS"},
    {"id": "ubuntu22.04",      "name": "Ubuntu 22.04 LTS"},
    {"id": "ubuntu20.04",      "name": "Ubuntu 20.04 LTS"},
    {"id": "debian12",         "name": "Debian 12 (Bookworm)"},
    {"id": "debian11",         "name": "Debian 11 (Bullseye)"},
    {"id": "fedora39",         "name": "Fedora 39"},
    {"id": "centos-stream9",   "name": "CentOS Stream 9"},
    {"id": "almalinux9",       "name": "AlmaLinux 9"},
    {"id": "win11",            "name": "Windows 11"},
    {"id": "win10",            "name": "Windows 10"},
    {"id": "win2k22",          "name": "Windows Server 2022"},
    {"id": "generic",          "name": "Generic / Other"},
]


def _run(*args, timeout: int = 30) -> tuple[int, str, str]:
    r = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def _virsh_available() -> bool:
    rc, _, _ = _run("virsh", "version")
    return rc == 0


def _require_root():
    if not _IS_ROOT:
        raise HTTPException(503, "VM management requires the backend to run as root")


# ── Parsing helpers ───────────────────────────────────────────────────────────

def _parse_domlist(output: str) -> list[dict]:
    vms = []
    lines = output.strip().splitlines()
    # Skip header (first two lines: title + separator)
    for line in lines[2:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        vm_id  = parts[0]
        name   = parts[1]
        state  = " ".join(parts[2:]) if len(parts) > 2 else "unknown"
        vms.append({"id": None if vm_id == "-" else vm_id, "name": name, "state": state})
    return vms


def _parse_dominfo(output: str) -> dict:
    info: dict = {}
    for line in output.strip().splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            info[k.strip().lower().replace(" ", "_")] = v.strip()
    return info


def _parse_domain_xml(xml_text: str) -> dict:
    result: dict = {"vnc_port": None, "vnc_display": None, "disks": [], "nets": [], "os_type": "", "arch": ""}
    try:
        root = ET.fromstring(xml_text)

        # OS type + arch
        os_el = root.find("./os/type")
        if os_el is not None:
            result["os_type"] = os_el.text or ""
            result["arch"]    = os_el.get("arch", "")

        # VNC graphics
        gfx = root.find(".//graphics[@type='vnc']")
        if gfx is not None:
            port = gfx.get("port")
            if port and int(port) > 0:
                result["vnc_port"]    = int(port)
                result["vnc_display"] = f":{int(port) - 5900}"

        # Disks (device=disk only, skip cdrom)
        for disk in root.findall(".//disk[@device='disk']"):
            src = disk.find("source")
            tgt = disk.find("target")
            path = src.get("file") or src.get("dev") or "" if src is not None else ""
            dev  = tgt.get("dev", "") if tgt is not None else ""
            if path:
                result["disks"].append({"path": path, "dev": dev})

        # Network interfaces
        for iface in root.findall(".//interface"):
            src = iface.find("source")
            mac = iface.find("mac")
            net = ""
            if src is not None:
                net = src.get("network") or src.get("bridge") or src.get("dev") or ""
            mac_addr = mac.get("address", "") if mac is not None else ""
            result["nets"].append({"network": net, "mac": mac_addr})

        # CPU pinning / topology
        vcpu_el = root.find("vcpu")
        if vcpu_el is not None:
            result["vcpus_xml"] = int(vcpu_el.text or "0")

        # Memory
        mem_el = root.find("memory")
        if mem_el is not None:
            unit = mem_el.get("unit", "KiB")
            val  = int(mem_el.text or "0")
            if unit in ("KiB", "k"):
                result["memory_mb_xml"] = val // 1024
            elif unit in ("MiB", "M"):
                result["memory_mb_xml"] = val
            elif unit in ("GiB", "G"):
                result["memory_mb_xml"] = val * 1024
            else:
                result["memory_mb_xml"] = val // 1024

    except ET.ParseError:
        pass
    return result


# ── List / detail ─────────────────────────────────────────────────────────────

@router.get("/isos")
def list_isos():
    found = []
    for base in ISO_SEARCH_PATHS:
        if not os.path.isdir(base):
            continue
        for path in glob.glob(os.path.join(base, "**", "*.iso"), recursive=True):
            found.append({"path": path, "name": os.path.basename(path),
                          "size_gb": round(os.path.getsize(path) / 1e9, 1)})
    return found


@router.get("/os-variants")
def list_os_variants():
    return COMMON_OS_VARIANTS


@router.get("/")
def list_vms():
    if not _virsh_available():
        return {"available": False, "vms": []}

    rc, out, _ = _run("virsh", "list", "--all")
    if rc != 0:
        return {"available": True, "vms": []}

    vms = _parse_domlist(out)

    result = []
    for vm in vms:
        name = vm["name"]
        # Get extra detail
        _, info_out, _ = _run("virsh", "dominfo", name)
        info = _parse_dominfo(info_out)

        _, xml_out, _ = _run("virsh", "dumpxml", "--inactive", name)
        xml = _parse_domain_xml(xml_out)

        mem_kib = int(info.get("max_memory", "0 KiB").split()[0])
        vcpus   = int(info.get("cpu(s)", info.get("vcpu", "0")))

        result.append({
            "name":       name,
            "id":         vm["id"],
            "state":      vm["state"],
            "memory_mb":  mem_kib // 1024,
            "vcpus":      vcpus,
            "autostart":  info.get("autostart", "disable") == "enable",
            "persistent": info.get("persistent", "no") == "yes",
            "vnc_port":   xml.get("vnc_port"),
            "vnc_display":xml.get("vnc_display"),
            "disks":      xml.get("disks", []),
            "nets":       xml.get("nets", []),
            "os_type":    xml.get("os_type", ""),
            "arch":       xml.get("arch", ""),
        })

    return {"available": True, "vms": result}


@router.get("/{name}")
def get_vm(name: str):
    if not _virsh_available():
        raise HTTPException(503, "libvirt / virsh not available")

    rc, info_out, err = _run("virsh", "dominfo", name)
    if rc != 0:
        raise HTTPException(404, f"VM '{name}' not found")

    info = _parse_dominfo(info_out)
    _, xml_out, _ = _run("virsh", "dumpxml", name)
    xml = _parse_domain_xml(xml_out)

    mem_kib = int(info.get("max_memory", "0 KiB").split()[0])
    vcpus   = int(info.get("cpu(s)", info.get("vcpu", "0")))

    return {
        "name":       name,
        "state":      info.get("state", "unknown"),
        "memory_mb":  mem_kib // 1024,
        "vcpus":      vcpus,
        "autostart":  info.get("autostart", "disable") == "enable",
        "persistent": info.get("persistent", "no") == "yes",
        **xml,
    }


@router.get("/{name}/vnc")
def get_vnc(name: str):
    rc, out, _ = _run("virsh", "vncdisplay", name)
    if rc != 0 or not out.strip():
        raise HTTPException(409, f"VM '{name}' is not running or has no VNC display")
    display = out.strip()          # e.g. ":0" or "127.0.0.1:0"
    host    = "127.0.0.1"
    disp_num = 0
    if ":" in display:
        parts = display.rsplit(":", 1)
        if parts[0]:
            host = parts[0]
        disp_num = int(parts[1])
    port = 5900 + disp_num
    return {"display": display, "host": host, "port": port, "websocket_port": port + 100}


# ── Lifecycle actions ─────────────────────────────────────────────────────────

def _virsh_action(action: str, name: str, extra: list[str] | None = None) -> dict:
    _require_root()
    cmd = ["virsh", action, name] + (extra or [])
    rc, _, err = _run(*cmd)
    if rc != 0:
        raise HTTPException(500, err.strip() or f"virsh {action} failed")
    return {"ok": True, "action": action, "name": name}


@router.post("/{name}/start")
def start_vm(name: str):
    return _virsh_action("start", name)

@router.post("/{name}/stop")
def stop_vm(name: str):
    return _virsh_action("shutdown", name)

@router.post("/{name}/force-stop")
def force_stop_vm(name: str):
    return _virsh_action("destroy", name)

@router.post("/{name}/pause")
def pause_vm(name: str):
    return _virsh_action("suspend", name)

@router.post("/{name}/resume")
def resume_vm(name: str):
    return _virsh_action("resume", name)

@router.post("/{name}/reboot")
def reboot_vm(name: str):
    return _virsh_action("reboot", name)


class AutostartRequest(BaseModel):
    enabled: bool


@router.put("/{name}/autostart")
def set_autostart(name: str, req: AutostartRequest):
    _require_root()
    extra = [] if req.enabled else ["--disable"]
    rc, _, err = _run("virsh", "autostart", name, *extra)
    if rc != 0:
        raise HTTPException(500, err.strip())
    return {"ok": True, "autostart": req.enabled}


@router.delete("/{name}")
def delete_vm(name: str, remove_disks: bool = False):
    _require_root()
    # Optionally get disk paths before undefining
    if remove_disks:
        rc, xml_out, _ = _run("virsh", "dumpxml", "--inactive", name)
        xml = _parse_domain_xml(xml_out) if rc == 0 else {}
        disk_paths = [d["path"] for d in xml.get("disks", []) if d.get("path")]
    else:
        disk_paths = []

    rc, _, err = _run("virsh", "undefine", "--nvram", name)
    if rc != 0:
        # retry without --nvram (non-UEFI VMs don't have nvram)
        rc, _, err = _run("virsh", "undefine", name)
    if rc != 0:
        raise HTTPException(500, err.strip())

    removed_disks = []
    for path in disk_paths:
        try:
            os.remove(path)
            removed_disks.append(path)
        except OSError:
            pass

    return {"ok": True, "removed_disks": removed_disks}


# ── Create ────────────────────────────────────────────────────────────────────

class CreateVMRequest(BaseModel):
    name:       str
    memory_mb:  int   = 2048
    vcpus:      int   = 2
    disk_gb:    int   = 50
    disk_path:  str   = ""   # leave empty to auto-place in /var/lib/libvirt/images/
    iso:        str   = ""
    os_variant: str   = "generic"
    network:    str   = "default"  # libvirt network name or "bridge=br0"
    graphics:   str   = "vnc"


@router.post("/")
def create_vm(req: CreateVMRequest):
    _require_root()
    if not _virsh_available():
        raise HTTPException(503, "virt-install / libvirt not available")

    if not req.name or not req.name.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(400, "VM name must be alphanumeric (hyphens/underscores allowed)")

    disk_path = req.disk_path or f"/var/lib/libvirt/images/{req.name}.qcow2"

    cmd = [
        "virt-install",
        "--name",        req.name,
        "--memory",      str(req.memory_mb),
        "--vcpus",       str(req.vcpus),
        "--disk",        f"path={disk_path},size={req.disk_gb},format=qcow2",
        "--os-variant",  req.os_variant,
        "--graphics",    req.graphics,
        "--noautoconsole",
        "--noreboot",
    ]

    # Network
    if req.network.startswith("bridge="):
        cmd += ["--network", req.network]
    else:
        cmd += ["--network", f"network={req.network}"]

    # ISO boot medium
    if req.iso:
        cmd += ["--cdrom", req.iso]
    else:
        cmd += ["--boot", "hd"]

    rc, out, err = _run(*cmd, timeout=120)
    if rc != 0:
        raise HTTPException(500, err.strip() or "virt-install failed")

    return {"ok": True, "name": req.name, "disk_path": disk_path}


# ── Snapshots ─────────────────────────────────────────────────────────────────

@router.get("/{name}/snapshots")
def list_snapshots(name: str):
    rc, out, _ = _run("virsh", "snapshot-list", name, "--name")
    if rc != 0:
        return []
    names = [n.strip() for n in out.strip().splitlines() if n.strip()]
    result = []
    for sname in names:
        rc2, detail, _ = _run("virsh", "snapshot-info", name, sname)
        info = _parse_dominfo(detail) if rc2 == 0 else {}
        result.append({
            "name":    sname,
            "created": info.get("creation_time", ""),
            "state":   info.get("state", ""),
            "current": info.get("current", "no") == "yes",
        })
    return result


class SnapshotRequest(BaseModel):
    name:        str
    description: str = ""
    live:        bool = False   # snapshot running VM (crash-consistent)


@router.post("/{name}/snapshots")
def create_snapshot(name: str, req: SnapshotRequest):
    _require_root()
    cmd = ["virsh", "snapshot-create-as", name, "--name", req.name]
    if req.description:
        cmd += ["--description", req.description]
    if req.live:
        cmd += ["--live"]
    rc, _, err = _run(*cmd)
    if rc != 0:
        raise HTTPException(500, err.strip())
    return {"ok": True, "snapshot": req.name}


@router.delete("/{name}/snapshots/{snap}")
def delete_snapshot(name: str, snap: str):
    _require_root()
    rc, _, err = _run("virsh", "snapshot-delete", name, snap)
    if rc != 0:
        raise HTTPException(500, err.strip())
    return {"ok": True}
