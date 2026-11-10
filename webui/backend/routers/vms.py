"""
Virtual machines (libvirt / QEMU-KVM), modelled on Unraid's VM Manager.

Manager       GET  /api/vms/                         list + whether VMs can run here (and why not)
              GET  /api/vms/host                     what the add/edit form can offer (CPUs, PCI, USB, networks, ISOs ...)
              GET|PUT /api/vms/settings              VM Manager settings (enable, folders, default network, shutdown action)
              GET  /api/vms/stats                    live CPU / memory per running VM
              GET  /api/vms/templates                built-in OS templates + your own;  POST/DELETE for your own
              GET  /api/vms/isos                     install images found in the ISO folder
              POST /api/vms/virtio-iso (download) | DELETE   the VirtIO drivers ISO for Windows guests
              POST /api/vms/start-all | stop-all
VM            POST /api/vms/                         create from a spec (see services/vmxml.py)
              GET  /api/vms/{name}                   detail: spec, disks with sizes, NICs with IPs, snapshots
              PUT  /api/vms/{name}                   edit from a spec (VM must be shut off)
              GET|PUT /api/vms/{name}/xml            expert editor
              DELETE /api/vms/{name}?remove_disks=   remove the VM (and optionally its vDisks)
              POST /api/vms/{name}/{start|stop|force-stop|pause|resume|reboot|reset|hibernate}
              PUT  /api/vms/{name}/autostart
              POST /api/vms/{name}/clone             copy a shut-off VM (background job)
              POST /api/vms/{name}/disks/resize      grow a vDisk (VM shut off)
              POST /api/vms/{name}/cdrom             insert / eject an image
              POST /api/vms/{name}/usb/attach|detach hot-plug a USB device
              GET|POST /api/vms/{name}/snapshots, POST .../{snap}/revert, DELETE .../{snap}
              GET  /api/vms/{name}/log               QEMU's log for this VM
              GET  /api/vms/{name}/console           how to reach the console (the browser console is /novnc via routers/vmconsole.py)
              GET  /api/vms/jobs                     background jobs (clone, ISO download)

Everything privileged runs through services/privileged.py (virsh, qemu-img). Paths that end up in a VM must be under /mnt:
the backend creates disk images as root, so a path from the browser is never trusted.
"""
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid as uuidlib
import xml.etree.ElementTree as ET
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import ARRAY_SBIN, HELIX_CONFIG, STATE_DIR
from services import privileged, vmhost, vmxml

router = APIRouter(prefix="/api/vms", tags=["vms"])

URI = "qemu:///system"
NVRAM_DIR = "/var/lib/libvirt/qemu/nvram"
TEMPLATE_DIR = os.path.join(HELIX_CONFIG, "vm-templates")
VIRTIO_URL = "https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso"
MNT = os.getenv("VM_MNT", "/mnt")          # tests point this at a temp tree
_lock = threading.RLock()


# ── libvirt plumbing ─────────────────────────────────────────────────────────

def _virsh(*args, timeout: int = 30, input: str | None = None) -> tuple[int, str, str]:
    r = privileged.run(["virsh", "-c", URI, *args], input=input, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def _err(text: str) -> str:
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    msg = next((l for l in reversed(lines) if l.startswith("error:")), lines[-1] if lines else "libvirt reported an error")
    return re.sub(r"^error:\s*", "", msg)


def _libvirt_up() -> bool:
    rc, _, _ = _virsh("uri", timeout=6)
    return rc == 0


def _require_up() -> None:
    if not vmhost.libvirt_installed():
        raise HTTPException(503, "libvirt is not installed on this system")
    if not _libvirt_up():
        raise HTTPException(503, "VM Manager is not running. VMs start after the array starts and VM Manager is enabled.")


def _must(rc: int, err: str, code: int = 409) -> None:
    if rc != 0:
        raise HTTPException(code, _err(err))


def _name(name: str) -> str:
    if not vmxml.NAME_RE.match(name or ""):
        raise HTTPException(404, f"No VM named {name!r}")
    return name


def _exists(name: str) -> bool:
    rc, _, _ = _virsh("dominfo", name)
    return rc == 0


def _need_vm(name: str) -> str:
    _name(name)
    _require_up()
    if not _exists(name):
        raise HTTPException(404, f"No VM named {name!r}")
    return name


def _dumpxml(name: str, inactive: bool = True) -> str:
    rc, out, err = _virsh("dumpxml", "--security-info", *(["--inactive"] if inactive else []), name)
    _must(rc, err)
    return out


def _dominfo(name: str) -> dict:
    rc, out, _ = _virsh("dominfo", name)
    info = {}
    for line in out.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            info[k.strip().lower()] = v.strip()
    return info


def _state(info: dict) -> str:
    s = info.get("state", "unknown")
    if s == "shut off" and info.get("managed save", "no") == "yes":
        return "hibernated"
    return {"pmsuspended": "suspended", "idle": "running"}.get(s, s)


def _host() -> dict:
    return {"kvm": vmhost.kvm_available(), "amd": vmhost.cpu_info()["vendor"] == "amd", "ovmf": vmhost.ovmf_files()}


# ── path safety (the backend writes these as root) ───────────────────────────

def _under_mnt(p: str) -> str:
    """The real path of p, which must be inside /mnt. A path that does not exist yet is resolved through its deepest
    existing folder, so a symlink inside a share cannot point a root-run qemu-img somewhere else."""
    if not isinstance(p, str) or not p.startswith("/") or ".." in p.split("/"):
        raise HTTPException(400, f"{p}: must be a full path inside {MNT}, without '..'")
    head, tail = p, []
    while head not in ("", "/") and not os.path.lexists(head):
        head, leaf = os.path.split(head)
        tail.insert(0, leaf)
    rp = os.path.join(os.path.realpath(head or "/"), *tail)
    root = os.path.realpath(MNT)
    if not (rp == root or rp.startswith(root + "/")):
        raise HTTPException(400, f"{p}: must be inside {MNT}")
    return rp


def _check_paths(spec: dict) -> None:
    for d in spec.get("disks", []):
        if d.get("type") == "block":
            if not re.match(r"^/dev/(disk/by-(id|uuid|path)|mapper)/[A-Za-z0-9._:+@=-]+$", d["path"]):
                raise HTTPException(400, f"{d['path']}: a passed-through disk must be a /dev/disk/by-id/... path")
        else:
            _under_mnt(d["path"])
    for c in spec.get("cdroms", []):
        if c.get("path"):
            _under_mnt(c["path"])
    for s in spec.get("shares", []):
        _under_mnt(s["source"])
    for p in spec.get("pci", []):
        if p.get("rom"):
            _under_mnt(p["rom"])


def _chown_helix(path: str) -> None:
    privileged.run(["chown", "helix:helix", path])


def _mkdirs(folder: str) -> None:
    """Create folder (and parents under /mnt) owned by the web user; no-CoW on btrfs, like Unraid does for vDisks."""
    for p in (folder,):
        r = privileged.run(["mkdir", "-p", p])
        if r.returncode != 0:
            raise HTTPException(500, f"Could not create {p}: {r.stderr.strip()}")
    privileged.run(["chown", "-R", "helix:helix", folder])
    if vmhost.fs_type(folder) == "btrfs":
        privileged.run(["chattr", "+C", folder])


def _img_info(path: str) -> dict:
    r = privileged.run(["qemu-img", "info", "--output=json", "-U", path], timeout=20)
    try:
        d = json.loads(r.stdout or "{}")
        return {"capacity": d.get("virtual-size", 0), "allocation": d.get("actual-size", 0), "format": d.get("format", "")}
    except ValueError:
        return {"capacity": 0, "allocation": 0, "format": ""}


def _create_disks(spec: dict, storage: str) -> list[str]:
    """Create vDisk images that do not exist yet. Returns the files created (so a failed create can clean up)."""
    made = []
    n = 0
    for d in spec["disks"]:
        n += 1
        if d.get("type") == "block":
            continue
        if not d.get("path"):
            ext = "img" if d.get("format", "qcow2") == "raw" else "qcow2"
            d["path"] = f"{storage}/{spec['name']}/vdisk{n}.{ext}"
        path = _under_mnt(d["path"])
        if os.path.exists(path):
            if int(d.get("size_gb") or 0):
                pass                              # an existing image is attached as it is; size_gb is ignored
            continue
        size = int(d.get("size_gb") or 0)
        if size < 1 or size > 65536:
            raise HTTPException(400, f"vDisk {n}: {path} does not exist yet; give it a size (1 to 65536 GB)")
        _mkdirs(os.path.dirname(path))
        fmt = d.get("format", "qcow2")
        cmd = ["qemu-img", "create", "-f", fmt, *(["-o", "preallocation=off"] if fmt == "qcow2" else []), path, f"{size}G"]
        r = privileged.run(cmd, timeout=60)
        if r.returncode != 0:
            raise HTTPException(500, f"Could not create the vDisk {path}: {_err(r.stderr or r.stdout)}")
        _chown_helix(path)
        made.append(path)
    return made


def _disks_in_use(except_vm: str = "") -> dict[str, str]:
    """{disk path: vm name} for every vDisk of every defined VM."""
    used = {}
    rc, out, _ = _virsh("list", "--all", "--name")
    for n in [x.strip() for x in out.splitlines() if x.strip()]:
        if n == except_vm:
            continue
        try:
            for d in ET.fromstring(_dumpxml(n)).findall("devices/disk"):
                s = d.find("source")
                if d.get("device") == "disk" and s is not None:
                    used[s.get("file") or s.get("dev") or ""] = n
        except (HTTPException, ET.ParseError):
            continue
    return used


def _check_disk_conflicts(spec: dict, except_vm: str = "") -> None:
    used = _disks_in_use(except_vm)
    for d in spec["disks"]:
        who = used.get(d.get("path", ""))
        if who:
            raise HTTPException(409, f"{d['path']} is already a vDisk of the VM {who!r}")


# ── describing a VM ──────────────────────────────────────────────────────────

_cap_cache: dict[tuple, dict] = {}


def _disk_detail(spec: dict) -> list[dict]:
    out = []
    for d in spec["disks"]:
        key = (d["path"], os.path.getmtime(d["path"]) if os.path.exists(d["path"]) else 0)
        info = _cap_cache.get(key) or _img_info(d["path"]) if os.path.exists(d["path"]) else {"capacity": 0, "allocation": 0, "format": d["format"]}
        _cap_cache[key] = info
        out.append({**d, **info, "exists": os.path.exists(d["path"])})
    return out


def _ips(name: str, state: str, xml_live: str | None) -> list[dict]:
    if state != "running":
        return []
    out = []
    for source in ("lease", "agent"):
        rc, txt, _ = _virsh("domifaddr", name, "--source", source, timeout=8)
        if rc != 0:
            continue
        for line in txt.splitlines()[2:]:
            p = line.split()
            if len(p) >= 4 and p[2] in ("ipv4", "ipv6"):
                if p[1].lower().startswith("00:00:00") or p[3].startswith(("127.", "::1", "fe80")):
                    continue
                out.append({"mac": p[1], "ip": p[3].split("/")[0], "prefix": p[3].split("/")[1] if "/" in p[3] else ""})
        if out:
            break
    return out


def _graphics_live(name: str) -> list[dict]:
    try:
        root = ET.fromstring(_dumpxml(name, inactive=False))
    except (HTTPException, ET.ParseError):
        return []
    return [{"type": g.get("type"), "port": int(g.get("port")) if (g.get("port") or "-1").lstrip("-").isdigit() and g.get("port") != "-1" else None,
             "listen": g.get("listen", "")} for g in root.findall("devices/graphics")]


def _snapshot_list(name: str) -> list[dict]:
    rc, out, _ = _virsh("snapshot-list", name, "--name")
    snaps = []
    for n in [x.strip() for x in out.splitlines() if x.strip()]:
        rc, x, _ = _virsh("snapshot-dumpxml", name, n)
        if rc != 0:
            continue
        try:
            r = ET.fromstring(x)
        except ET.ParseError:
            continue
        ts = int(r.findtext("creationTime") or 0)
        snaps.append({"name": n, "description": r.findtext("description") or "", "state": r.findtext("state") or "",
                      "created": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "", "created_ts": ts,
                      "parent": r.findtext("parent/name") or "", "current": False})
    snaps.sort(key=lambda s: s["created_ts"])
    return snaps


def _summary(name: str, with_ip: bool = False) -> dict:
    info = _dominfo(name)
    state = _state(info)
    spec = vmxml.parse_xml(_dumpxml(name))
    live = _graphics_live(name) if state in ("running", "paused") else []
    gfx = []
    for i, g in enumerate(spec["graphics"]):
        port = live[i]["port"] if i < len(live) else (g["port"] if g["port"] != -1 else None)
        gfx.append({"type": g["type"], "video": g["video"], "port": port, "password": bool(g["password"])})
    total = 0
    for d in spec["disks"]:
        if os.path.exists(d["path"]):
            key = (d["path"], os.path.getmtime(d["path"]))
            info_d = _cap_cache.get(key) or _img_info(d["path"])
            _cap_cache[key] = info_d
            total += info_d["capacity"]
    return {
        "name": name, "uuid": spec["uuid"], "state": state, "description": spec["description"], "template": spec["template"],
        "webui": spec["webui"], "vcpus": spec["vcpus"], "pins": spec["pins"], "memory_mb": spec["memory_mb"], "max_memory_mb": spec["max_memory_mb"],
        "autostart": info.get("autostart") == "enable", "machine": spec["machine"], "firmware": spec["firmware"], "tpm": spec["tpm"],
        "disks": len(spec["disks"]), "disks_capacity": total, "cdroms": len(spec["cdroms"]),
        "graphics": gfx, "gpus": [p["address"] for p in spec["pci"]], "usb": spec["usb"], "ips": _ips(name, state, None) if with_ip else [],
        "snapshots": len(_snapshot_list(name)), "unsupported": spec["unsupported"], "kind": "vm",
    }


# ── manager: list / host / settings ──────────────────────────────────────────

def _availability() -> dict:
    s = vmhost.load_settings()
    cpu = vmhost.cpu_info()
    base = {"installed": vmhost.libvirt_installed(), "kvm": vmhost.kvm_available(), "enabled": s["enabled"], "running": False,
            "hw_virtualization": cpu["hw_virtualization"], "reason": "", "settings": s}
    if not base["installed"]:
        base["reason"] = "libvirt and QEMU are not installed on this system."
    elif not base["kvm"]:
        base["reason"] = ("Hardware virtualization is not available. Enable Intel VT-x / AMD-V in the BIOS"
                          + ("; if this machine is itself a virtual machine, turn on nested virtualization for it in the hypervisor." if "hypervisor" in vmhost._read("/proc/cpuinfo") else "."))
    elif not s["enabled"]:
        base["reason"] = "VM Manager is disabled. Enable it in VM Manager settings."
    elif not _libvirt_up():
        base["reason"] = "VMs start after the array starts. Start the array, or check VM Manager settings."
    else:
        base["running"] = True
    return base


@router.get("/")
def list_vms():
    av = _availability()
    vms = []
    if av["running"]:
        rc, out, _ = _virsh("list", "--all", "--name")
        for n in [x.strip() for x in out.splitlines() if x.strip()]:
            try:
                vms.append(_summary(n, with_ip=True))
            except (HTTPException, ET.ParseError, KeyError):
                vms.append({"name": n, "state": "unknown", "kind": "vm", "unsupported": ["unreadable definition"], "graphics": [], "gpus": [],
                            "ips": [], "disks": 0, "disks_capacity": 0, "cdroms": 0, "snapshots": 0, "vcpus": 0, "memory_mb": 0, "autostart": False})
    av["available"] = av["installed"]          # kept for older clients
    av["vms"] = sorted(vms, key=lambda v: v["name"].lower())
    return av


@router.get("/host")
def host_info():
    s = vmhost.load_settings()
    nets = []
    if vmhost.libvirt_installed() and _libvirt_up():
        rc, out, _ = _virsh("net-list", "--all", "--name")
        for n in [x.strip() for x in out.splitlines() if x.strip()]:
            br = ""
            rc2, x2, _ = _virsh("net-dumpxml", n)
            m = re.search(r"<bridge name='([^']+)'", x2)
            br = m.group(1) if m else ""
            nets.append({"type": "network", "source": n, "label": f"{br or n} (NAT, libvirt network '{n}')"})
    for b in vmhost.bridges():
        nets.append({"type": "bridge", "source": b, "label": f"{b} (bridge to your network)"})
    cpu = vmhost.cpu_info()
    storage = s["storage"]
    return {
        "cpu": cpu, "memory_mb": vmhost.memory_total_mb(), "kvm": vmhost.kvm_available(), "networks": nets,
        "ovmf": {k: True for k in vmhost.ovmf_files()}, "pci": [d for d in vmhost.pci_devices() if not d["is_bridge"]],
        "usb": vmhost.usb_devices(), "isos": vmhost.list_isos(s["isos"]), "settings": s,
        "storage_fs": vmhost.fs_type(storage), "virtio_iso": s["virtio_iso"] if s["virtio_iso"] and os.path.exists(s["virtio_iso"]) else "",
        "keymaps": ["en-us", "en-gb", "de", "fr", "es", "it", "pt", "nl", "da", "sv", "no", "fi", "ja", "ru", "pl", "tr", "cs", "hu"],
    }


@router.get("/settings")
def get_settings():
    s = vmhost.load_settings()
    ver = ""
    if vmhost.libvirt_installed():
        rc, out, _ = _virsh("version", timeout=8)
        ver = " · ".join(l.strip() for l in out.splitlines() if "library" in l.lower() or "hypervisor" in l.lower())
    return {**s, "libvirt_version": ver, "virtio_iso_present": bool(s["virtio_iso"]) and os.path.exists(s["virtio_iso"])}


class SettingsReq(BaseModel):
    enabled: bool | None = None
    storage: str | None = None
    isos: str | None = None
    default_network: str | None = None
    shutdown_action: str | None = None
    shutdown_timeout: int | None = None
    usage_stats: bool | None = None


@router.put("/settings")
def put_settings(req: SettingsReq):
    old = vmhost.load_settings()
    try:
        new = vmhost.validate_settings({k: v for k, v in req.model_dump().items() if v is not None})
    except ValueError as e:
        raise HTTPException(400, str(e))
    if new["storage"] != old["storage"] or new["isos"] != old["isos"]:
        if _libvirt_up() and any(v["state"] in ("running", "paused") for v in _quick_states()):
            raise HTTPException(409, "Stop all VMs before changing the VM or ISO folder.")
    vmhost.save_settings(new)
    msg = ""
    if new["enabled"] != old["enabled"]:
        script = os.path.join(ARRAY_SBIN, "helix-vm-start" if new["enabled"] else "helix-vm-stop")
        r = privileged.run(["env", f"HELIX_CONFIG={HELIX_CONFIG}", f"STATE_DIR={STATE_DIR}", "/bin/bash", script], timeout=180)
        if r.returncode != 0:
            msg = (r.stdout + r.stderr).strip().splitlines()[-1] if (r.stdout + r.stderr).strip() else "could not change the state of VM Manager"
    for p in (new["storage"], new["isos"]):
        if os.path.isdir(os.path.dirname(p)):
            try:
                _mkdirs(p)
            except HTTPException:
                pass
    return {"ok": True, "settings": new, "message": msg}


def _quick_states() -> list[dict]:
    rc, out, _ = _virsh("list", "--all")
    res = []
    for line in out.splitlines()[2:]:
        p = line.split(None, 2)
        if len(p) == 3:
            res.append({"name": p[1], "state": p[2].strip()})
    return res


_cpu_prev: dict[str, tuple[int, float]] = {}


@router.get("/stats")
def stats():
    if not (vmhost.libvirt_installed() and _libvirt_up()):
        return {"stats": {}}
    rc, out, _ = _virsh("domstats", "--cpu-total", "--balloon", "--state", timeout=15)
    cur: dict[str, dict] = {}
    name = None
    for line in out.splitlines():
        m = re.match(r"^Domain: '(.+)'$", line.strip())
        if m:
            name = m.group(1); cur[name] = {}
        elif name and "=" in line:
            k, _, v = line.strip().partition("=")
            cur[name][k] = v
    now = time.time()
    res = {}
    ncpu = vmhost.cpu_info()["threads"] or 1
    for n, d in cur.items():
        if d.get("state.state") != "1":
            _cpu_prev.pop(n, None)
            continue
        t = int(d.get("cpu.time", "0") or 0)
        prev = _cpu_prev.get(n)
        pct = 0.0
        if prev and now > prev[1]:
            pct = max(0.0, (t - prev[0]) / 1e9 / (now - prev[1]) * 100)
        _cpu_prev[n] = (t, now)
        res[n] = {"cpu_pct": round(min(pct, 100.0 * ncpu), 1), "mem_mb": int(d.get("balloon.current", "0") or 0) // 1024,
                  "mem_max_mb": int(d.get("balloon.maximum", "0") or 0) // 1024}
    return {"stats": res, "host_threads": ncpu}


# ── templates ────────────────────────────────────────────────────────────────

def _lin(**k):
    return {"machine": "q35", "firmware": "ovmf", "vcpus": 2, "memory_mb": 2048, "clock": "utc", "agent": True,
            "disks": [{"size_gb": 30, "bus": "virtio", "boot": 1}], "cdroms": [{"boot": 2}], "nics": [{"model": "virtio"}],
            "graphics": [{"type": "vnc", "video": "virtio"}], **k}


def _win(**k):
    base = {"machine": "q35", "firmware": "ovmf", "vcpus": 4, "memory_mb": 8192, "clock": "localtime", "hyperv": True, "agent": True,
            "disks": [{"size_gb": 80, "bus": "virtio", "boot": 1}], "cdroms": [{"boot": 2}, {"role": "virtio"}], "nics": [{"model": "virtio"}],
            "graphics": [{"type": "vnc", "video": "qxl"}]}
    base.update(k)
    return base


BUILTIN_TEMPLATES = [
    {"id": "windows11", "name": "Windows 11", "group": "Windows", "badge": "W11", "note": "Needs your own Windows ISO. UEFI + secure boot + a virtual TPM; the VirtIO drivers disc is attached for the installer.",
     "defaults": _win(firmware="ovmf-secure", tpm=True, memory_mb=8192)},
    {"id": "windows10", "name": "Windows 10", "group": "Windows", "badge": "W10", "note": "Needs your own Windows ISO. UEFI; VirtIO drivers disc attached.", "defaults": _win(memory_mb=4096)},
    {"id": "windows-server", "name": "Windows Server 2019/2022", "group": "Windows", "badge": "WS", "note": "Needs your own ISO. UEFI; VirtIO drivers disc attached.", "defaults": _win(memory_mb=4096)},
    {"id": "windows-legacy", "name": "Windows 7 / 8.1", "group": "Windows", "badge": "W7", "note": "Older Windows: BIOS boot, SATA disk, e1000 network, VGA.",
     "defaults": {"machine": "i440fx", "firmware": "seabios", "vcpus": 2, "memory_mb": 4096, "clock": "localtime", "hyperv": True, "agent": False,
                  "disks": [{"size_gb": 60, "bus": "sata", "boot": 1}], "cdroms": [{"bus": "ide", "boot": 2}], "nics": [{"model": "e1000"}],
                  "graphics": [{"type": "vnc", "video": "vga"}]}},
    {"id": "ubuntu", "name": "Ubuntu", "group": "Linux", "badge": "Ub", "note": "", "defaults": _lin()},
    {"id": "debian", "name": "Debian", "group": "Linux", "badge": "Deb", "note": "", "defaults": _lin()},
    {"id": "fedora", "name": "Fedora / RHEL family", "group": "Linux", "badge": "Fed", "note": "Fedora, AlmaLinux, Rocky, CentOS Stream.", "defaults": _lin()},
    {"id": "arch", "name": "Arch / other Linux", "group": "Linux", "badge": "Lnx", "note": "", "defaults": _lin()},
    {"id": "linux-desktop", "name": "Linux desktop with GPU", "group": "Linux", "badge": "GPU", "note": "Pick the graphics card to pass through in the form.",
     "defaults": _lin(vcpus=4, memory_mb=8192, graphics=[{"type": "vnc", "video": "none"}])},
    {"id": "freebsd", "name": "FreeBSD", "group": "Other", "badge": "BSD", "note": "", "defaults": _lin(firmware="ovmf", agent=False)},
    {"id": "firewall", "name": "pfSense / OPNsense", "group": "Other", "badge": "FW", "note": "Add a second network card for WAN/LAN.",
     "defaults": _lin(agent=False, vcpus=2, memory_mb=2048, disks=[{"size_gb": 16, "bus": "virtio", "boot": 1}], nics=[{"model": "virtio"}, {"model": "virtio"}])},
    {"id": "appliance", "name": "Disk image appliance", "group": "Other", "badge": "IMG", "note": "Home Assistant OS, LibreELEC and similar: attach the downloaded .qcow2/.img as the vDisk and boot it.",
     "defaults": {"machine": "q35", "firmware": "ovmf", "vcpus": 2, "memory_mb": 2048, "agent": True, "disks": [{"path": "", "size_gb": 0, "bus": "virtio", "boot": 1, "existing": True}],
                  "cdroms": [], "nics": [{"model": "virtio"}], "graphics": [{"type": "vnc", "video": "virtio"}]}},
    {"id": "custom", "name": "Custom", "group": "Other", "badge": "+", "note": "Start from a blank VM.", "defaults": _lin(disks=[{"size_gb": 20, "bus": "virtio", "boot": 1}], cdroms=[])},
]


def _user_templates() -> list[dict]:
    out = []
    for f in sorted(glob_templates()):
        try:
            d = json.loads(Path(f).read_text())
            out.append({"id": "user:" + Path(f).stem, "name": d.get("name", Path(f).stem), "group": "Your templates", "badge": "★", "note": d.get("note", ""),
                        "defaults": d.get("defaults", {}), "user": True})
        except (OSError, ValueError):
            continue
    return out


def glob_templates() -> list[str]:
    try:
        return [os.path.join(TEMPLATE_DIR, f) for f in os.listdir(TEMPLATE_DIR) if f.endswith(".json")]
    except OSError:
        return []


@router.get("/templates")
def templates():
    return {"templates": BUILTIN_TEMPLATES + _user_templates()}


class TemplateReq(BaseModel):
    name: str
    from_vm: str


@router.post("/templates")
def save_template(req: TemplateReq):
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,40}$", req.name):
        raise HTTPException(400, "Template name: letters, digits, space, '.', '_' or '-'")
    _need_vm(req.from_vm)
    spec = vmxml.parse_xml(_dumpxml(req.from_vm))
    for k in ("name", "uuid", "unsupported", "webui"):
        spec.pop(k, None)
    for n in spec["nics"]:
        n.pop("mac", None)
    for d in spec["disks"]:
        d["path"] = ""; d.pop("dev", None); d["size_gb"] = 20      # a template carries the shape of the VM, not its disks
    for g in spec["graphics"]:
        g["password"] = ""
    os.makedirs(TEMPLATE_DIR, exist_ok=True)
    fn = re.sub(r"[^A-Za-z0-9._-]+", "_", req.name)
    Path(os.path.join(TEMPLATE_DIR, fn + ".json")).write_text(json.dumps({"name": req.name, "defaults": spec}, indent=1))
    return {"ok": True, "id": "user:" + fn}


@router.delete("/templates/{tid}")
def delete_template(tid: str):
    if not tid.startswith("user:") or not re.match(r"^user:[A-Za-z0-9._-]+$", tid):
        raise HTTPException(400, "Only your own templates can be removed")
    try:
        os.unlink(os.path.join(TEMPLATE_DIR, tid[5:] + ".json"))
    except FileNotFoundError:
        raise HTTPException(404, "No such template")
    return {"ok": True}


@router.get("/isos")
def isos():
    return {"isos": vmhost.list_isos(vmhost.load_settings()["isos"])}


# ── jobs (clone, ISO download) ───────────────────────────────────────────────

_jobs: dict[str, dict] = {}


def _job(kind: str, vm: str, fn) -> dict:
    jid = uuidlib.uuid4().hex[:8]
    j = {"id": jid, "kind": kind, "vm": vm, "state": "running", "message": "", "started": time.time(), "progress": 0.0}
    _jobs[jid] = j

    def run():
        try:
            fn(j)
            j["state"], j["progress"] = "done", 1.0
        except HTTPException as e:
            j["state"], j["message"] = "failed", str(e.detail)
        except Exception as e:                                # noqa: BLE001 - reported to the user
            j["state"], j["message"] = "failed", str(e) or e.__class__.__name__
        j["finished"] = time.time()
    threading.Thread(target=run, daemon=True).start()
    return j


@router.get("/jobs")
def jobs():
    for k in [k for k, v in _jobs.items() if v.get("finished") and time.time() - v["finished"] > 3600]:
        _jobs.pop(k, None)
    return {"jobs": sorted(_jobs.values(), key=lambda j: -j["started"])}


# ── VirtIO drivers ISO ───────────────────────────────────────────────────────

@router.post("/virtio-iso")
def download_virtio():
    s = vmhost.load_settings()
    dest = f"{s['isos']}/virtio-win.iso"
    if any(j["kind"] == "virtio" and j["state"] == "running" for j in _jobs.values()):
        raise HTTPException(409, "The download is already running.")
    _under_mnt(dest)

    def work(j):
        _mkdirs(s["isos"])
        j["message"] = "Downloading virtio-win.iso"
        tmp = dest + ".part"
        r = privileged.run(["curl", "-fL", "--retry", "3", "-o", tmp, VIRTIO_URL], timeout=3600)
        if r.returncode != 0:
            privileged.run(["rm", "-f", tmp])
            raise HTTPException(502, "Download failed: " + _err(r.stderr or r.stdout))
        privileged.run(["mv", "-f", tmp, dest])
        _chown_helix(dest)
        cur = vmhost.load_settings()
        cur["virtio_iso"] = dest
        vmhost.save_settings(cur)
    return {"ok": True, "job": _job("virtio", "virtio-win.iso", work)}


@router.delete("/virtio-iso")
def remove_virtio():
    s = vmhost.load_settings()
    p = s["virtio_iso"]
    if p:
        _under_mnt(p)
        privileged.run(["rm", "-f", p])
    s["virtio_iso"] = ""
    vmhost.save_settings(s)
    return {"ok": True}


# ── create / read / edit / delete ────────────────────────────────────────────

class VmSpec(BaseModel):
    spec: dict
    start: bool = False
    autostart: bool = False


def _apply_virtio_role(spec: dict, s: dict) -> None:
    """A CD marked role=virtio gets the VirtIO drivers ISO (or is dropped if it was not downloaded)."""
    keep = []
    for c in spec.get("cdroms", []):
        if c.get("role") == "virtio":
            if s["virtio_iso"] and os.path.exists(s["virtio_iso"]):
                keep.append({"path": s["virtio_iso"], "bus": c.get("bus", "sata")})
            continue
        c.pop("role", None)
        keep.append(c)
    spec["cdroms"] = keep


def _clean_spec(spec: dict) -> dict:
    for d in spec.get("disks", []):
        d.pop("existing", None); d.pop("dev", None); d.pop("exists", None); d.pop("capacity", None); d.pop("allocation", None)
    spec.pop("unsupported", None)
    return spec


def _write_tmp(text: str) -> str:
    os.makedirs(STATE_DIR, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="vm-", suffix=".xml", dir=STATE_DIR)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o644)
    return path


def _define(xml: str) -> None:
    path = _write_tmp(xml)
    try:
        rc, out, err = _virsh("define", "--validate", path)      # --validate: unknown elements are errors, not silently dropped
        _must(rc, err, 400)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


@router.post("/")
def create_vm(req: VmSpec):
    _require_up()
    s = vmhost.load_settings()
    spec = _clean_spec(req.spec)
    _apply_virtio_role(spec, s)
    for d in spec.get("disks", []):
        if not d.get("path") and d.get("type") != "block":
            d["path"] = ""                       # filled in below from the storage folder
    try:
        probe = json.loads(json.dumps(spec))
        for i, d in enumerate(probe.get("disks", []), 1):
            d["path"] = d.get("path") or f"{s['storage']}/x/vdisk{i}.qcow2"
        vmxml.normalize(probe)
    except vmxml.SpecError as e:
        raise HTTPException(400, str(e))
    if _exists(spec.get("name", "")):
        raise HTTPException(409, f"A VM named {spec['name']!r} already exists")
    if not vmhost.kvm_available():
        raise HTTPException(409, "Hardware virtualization (KVM) is not available on this machine")
    made = []
    try:
        made = _create_disks(spec, s["storage"])
        try:
            n = vmxml.normalize(spec)
        except vmxml.SpecError as e:
            raise HTTPException(400, str(e))
        _check_paths(n)
        _check_disk_conflicts(n)
        for p in n["pci"]:
            if not os.path.exists(f"/sys/bus/pci/devices/{p['address']}"):
                raise HTTPException(400, f"PCI device {p['address']} was not found on this machine")
        _define(vmxml.build_xml(n, _host()))
    except Exception:
        for p in made:
            try:
                os.unlink(p)
            except OSError:
                pass
        raise
    if req.autostart:
        _virsh("autostart", n["name"])
    out = {"ok": True, "name": n["name"], "started": False}
    if req.start:
        rc, _, err = _virsh("start", n["name"], timeout=60)
        out["started"] = rc == 0
        if rc != 0:
            out["start_error"] = _err(err)
    return out


@router.get("/{name}")
def get_vm(name: str):
    _need_vm(name)
    info = _dominfo(name)
    state = _state(info)
    xml = _dumpxml(name)
    spec = vmxml.parse_xml(xml)
    summ = _summary(name, with_ip=True)
    nics = []
    ips = summ["ips"]
    for n in spec["nics"]:
        nics.append({**n, "ips": [i for i in ips if i["mac"].lower() == (n.get("mac") or "").lower()] or (ips if len(spec["nics"]) == 1 else [])})
    return {**summ, "spec": spec, "disks_detail": _disk_detail(spec), "nics_detail": nics,
            "snapshot_list": _snapshot_list(name), "editable": state in ("shut off", "hibernated") and not spec["unsupported"],
            "console": _graphics_live(name) if state in ("running", "paused") else []}


@router.put("/{name}")
def update_vm(name: str, req: VmSpec):
    _need_vm(name)
    st = _state(_dominfo(name))
    if st not in ("shut off",):
        raise HTTPException(409, "Shut the VM down before editing it." if st != "hibernated" else "Resume or discard the hibernation image first, then edit.")
    cur = vmxml.parse_xml(_dumpxml(name))
    if cur["unsupported"]:
        raise HTTPException(409, "This VM uses settings the form cannot edit (" + ", ".join(cur["unsupported"]) + "). Use the XML editor.")
    s = vmhost.load_settings()
    spec = _clean_spec(req.spec)
    _apply_virtio_role(spec, s)
    spec["uuid"] = cur["uuid"]
    new_name = spec.get("name", name)
    if new_name != name and _exists(new_name):
        raise HTTPException(409, f"A VM named {new_name!r} already exists")
    made = []
    try:
        made = _create_disks(spec, s["storage"])
        try:
            n = vmxml.normalize(spec)
        except vmxml.SpecError as e:
            raise HTTPException(400, str(e))
        _check_paths(n)
        _check_disk_conflicts(n, except_vm=name)
        for p in n["pci"]:
            if not os.path.exists(f"/sys/bus/pci/devices/{p['address']}"):
                raise HTTPException(400, f"PCI device {p['address']} was not found on this machine")
        if new_name != name:
            rc, _, err = _virsh("domrename", name, new_name)
            _must(rc, err)
        try:
            _define(vmxml.build_xml(n, _host()))
        except HTTPException:
            if new_name != name:
                _virsh("domrename", new_name, name)
            raise
    except Exception:
        for p in made:
            try:
                os.unlink(p)
            except OSError:
                pass
        raise
    return {"ok": True, "name": n["name"]}


@router.get("/{name}/xml")
def get_xml(name: str):
    _need_vm(name)
    return {"xml": _dumpxml(name)}


class XmlReq(BaseModel):
    xml: str


@router.put("/{name}/xml")
def put_xml(name: str, req: XmlReq):
    _need_vm(name)
    if _state(_dominfo(name)) != "shut off":
        raise HTTPException(409, "Shut the VM down before editing its XML.")
    try:
        root = ET.fromstring(req.xml)
    except ET.ParseError as e:
        raise HTTPException(400, f"That is not valid XML: {e}")
    if root.tag != "domain" or (root.findtext("name") or "") != name:
        raise HTTPException(400, f"The XML must be a <domain> named {name!r} (rename the VM in the form, not here).")
    _define(req.xml)
    return {"ok": True}


@router.delete("/{name}")
def delete_vm(name: str, remove_disks: bool = False):
    _need_vm(name)
    st = _state(_dominfo(name))
    if st in ("running", "paused", "in shutdown"):
        raise HTTPException(409, "Stop the VM first.")
    spec = vmxml.parse_xml(_dumpxml(name))
    paths = [d["path"] for d in spec["disks"] if d["type"] == "file"]
    others = _disks_in_use(except_vm=name)
    rc, _, err = _virsh("undefine", name, "--nvram", "--managed-save", "--snapshots-metadata", "--tpm")
    if rc != 0:
        rc, _, err = _virsh("undefine", name, "--nvram", "--managed-save", "--snapshots-metadata")
    if rc != 0:
        rc, _, err = _virsh("undefine", name)
    _must(rc, err)
    removed, kept = [], []
    if remove_disks:
        for p in paths:
            if p in others:
                kept.append(p)
                continue
            try:
                _under_mnt(p)
            except HTTPException:
                kept.append(p)
                continue
            r = privileged.run(["rm", "-f", p])
            (removed if r.returncode == 0 else kept).append(p)
            d = os.path.dirname(p)
            if r.returncode == 0 and os.path.basename(d) == spec["name"]:
                privileged.run(["rmdir", d])                  # the VM's own folder, only if now empty
    return {"ok": True, "removed_disks": removed, "kept_disks": kept}


# ── lifecycle ────────────────────────────────────────────────────────────────

def _preflight(name: str) -> None:
    spec = vmxml.parse_xml(_dumpxml(name))
    for d in spec["disks"]:
        if d["type"] == "file" and not os.path.exists(d["path"]):
            raise HTTPException(409, f"The vDisk {d['path']} is missing, so this VM cannot start.")
    for c in spec["cdroms"]:
        if c["path"] and not os.path.exists(c["path"]):
            raise HTTPException(409, f"The image {c['path']} is missing. Eject it or pick another in Edit.")
    for s in spec["shares"]:
        if not os.path.isdir(s["source"]):
            raise HTTPException(409, f"The shared folder {s['source']} does not exist.")
    if not vmhost.kvm_available():
        raise HTTPException(409, "Hardware virtualization (KVM) is not available on this machine")


@router.post("/start-all")
def start_all():
    _require_up()
    done, failed = [], []
    for v in _quick_states():
        if v["state"] == "shut off":
            try:
                _preflight(v["name"])
                rc, _, err = _virsh("start", v["name"], timeout=60)
                (done if rc == 0 else failed).append(v["name"] if rc == 0 else f"{v['name']}: {_err(err)}")
            except HTTPException as e:
                failed.append(f"{v['name']}: {e.detail}")
    return {"ok": not failed, "started": done, "failed": failed}


@router.post("/stop-all")
def stop_all():
    _require_up()
    done = []
    for v in _quick_states():
        if v["state"] in ("running", "paused"):
            _virsh("shutdown", v["name"])
            done.append(v["name"])
    return {"ok": True, "stopping": done}


class AutostartReq(BaseModel):
    enabled: bool


@router.put("/{name}/autostart")
def set_autostart(name: str, req: AutostartReq):
    _need_vm(name)
    rc, _, err = _virsh("autostart", *([] if req.enabled else ["--disable"]), name)
    _must(rc, err)
    return {"ok": True, "autostart": req.enabled}


# ── clone, resize, media, usb ────────────────────────────────────────────────

class CloneReq(BaseModel):
    new_name: str
    regenerate_macs: bool = True
    overwrite: bool = False


@router.post("/{name}/clone")
def clone_vm(name: str, req: CloneReq):
    _need_vm(name)
    if _state(_dominfo(name)) != "shut off":
        raise HTTPException(409, "Shut the VM down before cloning it.")
    if not vmxml.NAME_RE.match(req.new_name):
        raise HTTPException(400, "Name: letters, digits, space, '.', '_' or '-'")
    if _exists(req.new_name):
        raise HTTPException(409, f"A VM named {req.new_name!r} already exists")
    spec = vmxml.parse_xml(_dumpxml(name))
    if spec["unsupported"]:
        raise HTTPException(409, "This VM uses settings the form cannot copy (" + ", ".join(spec["unsupported"]) + ").")
    s = vmhost.load_settings()
    old_uuid = spec["uuid"]
    spec["name"] = req.new_name
    spec["uuid"] = str(uuidlib.uuid4())
    if req.regenerate_macs:
        for n in spec["nics"]:
            n["mac"] = vmxml.new_mac()
    plan = []
    for i, d in enumerate(spec["disks"], 1):
        if d["type"] == "block":
            raise HTTPException(409, "A VM with a passed-through disk cannot be cloned.")
        ext = "img" if d["format"] == "raw" else "qcow2"
        dst = f"{s['storage']}/{req.new_name}/vdisk{i}.{ext}"
        if os.path.exists(dst) and not req.overwrite:
            raise HTTPException(409, f"{dst} already exists. Tick Overwrite to replace it.")
        plan.append((d["path"], dst, d["format"]))
        d["path"] = dst
    _check_paths(vmxml.normalize(json.loads(json.dumps(spec))))

    def work(j):
        total = max(1, len(plan))
        for k, (src, dst, fmt) in enumerate(plan):
            j["message"] = f"Copying {os.path.basename(src)} ({k + 1} of {total})"
            _mkdirs(os.path.dirname(dst))
            if fmt == "qcow2":
                r = privileged.run(["qemu-img", "convert", "-f", "qcow2", "-O", "qcow2", src, dst], timeout=7200)
            else:
                r = privileged.run(["cp", "--sparse=always", src, dst], timeout=7200)
            if r.returncode != 0:
                raise HTTPException(500, f"Copy failed: {_err(r.stderr or r.stdout)}")
            _chown_helix(dst)
            j["progress"] = (k + 1) / total
        old_nv, new_nv = f"{NVRAM_DIR}/{old_uuid}_VARS.fd", f"{NVRAM_DIR}/{spec['uuid']}_VARS.fd"
        if os.path.exists(old_nv) or privileged.run(["test", "-f", old_nv]).returncode == 0:
            privileged.run(["cp", old_nv, new_nv])
        _define(vmxml.build_xml(vmxml.normalize(spec), _host()))
        j["message"] = "Done"
    return {"ok": True, "job": _job("clone", req.new_name, work)}


class ResizeReq(BaseModel):
    path: str
    size_gb: int


@router.post("/{name}/disks/resize")
def resize_disk(name: str, req: ResizeReq):
    _need_vm(name)
    if _state(_dominfo(name)) != "shut off":
        raise HTTPException(409, "Shut the VM down before growing a vDisk.")
    spec = vmxml.parse_xml(_dumpxml(name))
    d = next((x for x in spec["disks"] if x["path"] == req.path), None)
    if not d or d["type"] != "file":
        raise HTTPException(404, "That is not a file vDisk of this VM")
    cur = _img_info(d["path"])["capacity"]
    if req.size_gb * 2 ** 30 <= cur:
        raise HTTPException(400, f"A vDisk can only grow. It is {cur / 2 ** 30:.0f} GB now; enter a larger size.")
    if req.size_gb > 65536:
        raise HTTPException(400, "At most 65536 GB")
    r = privileged.run(["qemu-img", "resize", "-f", d["format"], d["path"], f"{req.size_gb}G"], timeout=120)
    if r.returncode != 0:
        raise HTTPException(500, _err(r.stderr or r.stdout))
    _cap_cache.clear()
    return {"ok": True, "size_gb": req.size_gb}


class CdromReq(BaseModel):
    dev: str
    path: str = ""          # empty = eject


@router.post("/{name}/cdrom")
def change_cdrom(name: str, req: CdromReq):
    _need_vm(name)
    if not re.match(r"^(sd|hd)[a-z]{1,2}$", req.dev):
        raise HTTPException(400, "Unknown drive")
    spec = vmxml.parse_xml(_dumpxml(name))
    st = _state(_dominfo(name))
    live = st in ("running", "paused")
    if req.path:
        p = _under_mnt(req.path)
        if not os.path.isfile(p):
            raise HTTPException(404, f"{req.path} does not exist")
    args = ["change-media", name, req.dev, *(["--eject"] if not req.path else [req.path, "--insert"]), "--config", *(["--live"] if live else [])]
    rc, _, err = _virsh(*args)
    if rc != 0 and req.path:
        rc, _, err = _virsh("change-media", name, req.dev, req.path, "--update", "--config", *(["--live"] if live else []))
    _must(rc, err)
    return {"ok": True}


class UsbReq(BaseModel):
    vendor: str
    product: str


def _usb_xml(u: UsbReq) -> str:
    if not (vmxml.HEX4.match(u.vendor) and vmxml.HEX4.match(u.product)):
        raise HTTPException(400, "Vendor and product must be 4 hex digits")
    return (f"<hostdev mode='subsystem' type='usb' managed='yes'><source><vendor id='0x{u.vendor.lower()}'/>"
            f"<product id='0x{u.product.lower()}'/></source></hostdev>")


@router.post("/{name}/usb/{op}")
def usb_hotplug(name: str, op: str, req: UsbReq):
    if op not in ("attach", "detach"):
        raise HTTPException(404, "Unknown operation")
    _need_vm(name)
    if _state(_dominfo(name)) != "running":
        raise HTTPException(409, "USB devices can be plugged in and out of a running VM. For a stopped VM, tick the device in Edit.")
    path = _write_tmp(_usb_xml(req))
    try:
        rc, _, err = _virsh(f"{op}-device", name, path, "--live")
    finally:
        os.unlink(path)
    _must(rc, err)
    return {"ok": True}


# ── snapshots ────────────────────────────────────────────────────────────────

class SnapReq(BaseModel):
    name: str = ""
    description: str = ""


@router.get("/{name}/snapshots")
def list_snapshots(name: str):
    _need_vm(name)
    return {"snapshots": _snapshot_list(name)}


@router.post("/{name}/snapshots")
def create_snapshot(name: str, req: SnapReq):
    _need_vm(name)
    snap = req.name.strip() or time.strftime("snapshot-%Y%m%d-%H%M%S")
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}$", snap):
        raise HTTPException(400, "Snapshot name: letters, digits, '.', '_' or '-'")
    args = ["snapshot-create-as", name, snap]
    if req.description.strip():
        args += ["--description", req.description.strip()[:200]]
    rc, _, err = _virsh(*args, timeout=300)
    if rc != 0 and ("pflash" in err.lower() or "nvram" in err.lower()):
        raise HTTPException(409, "This VM uses UEFI firmware, and libvirt cannot take an internal snapshot of that. Use a BIOS (SeaBIOS) VM for snapshots, or copy the vDisk while the VM is off.")
    _must(rc, err)
    return {"ok": True, "name": snap}


@router.post("/{name}/snapshots/{snap}/revert")
def revert_snapshot(name: str, snap: str):
    _need_vm(name)
    rc, _, err = _virsh("snapshot-revert", name, snap, timeout=300)
    _must(rc, err)
    return {"ok": True}


@router.delete("/{name}/snapshots/{snap}")
def delete_snapshot(name: str, snap: str):
    _need_vm(name)
    rc, _, err = _virsh("snapshot-delete", name, snap, timeout=300)
    _must(rc, err)
    return {"ok": True}


# ── log, console info ────────────────────────────────────────────────────────

@router.get("/{name}/log")
def vm_log(name: str, lines: int = 300):
    _need_vm(name)
    r = privileged.run(["tail", "-n", str(max(10, min(lines, 2000))), f"/var/log/libvirt/qemu/{name}.log"], timeout=10)
    return {"lines": r.stdout.splitlines() if r.returncode == 0 else [], "error": "" if r.returncode == 0 else "No log yet. It appears after the VM has been started."}


@router.get("/{name}/console")
def console_info(name: str):
    _need_vm(name)
    st = _state(_dominfo(name))
    if st not in ("running", "paused"):
        raise HTTPException(409, "The VM is not running.")
    gfx = _graphics_live(name)
    if not gfx:
        raise HTTPException(409, "This VM has no graphics console (it may use a passed-through GPU).")
    return {"graphics": gfx}


# The generic two-segment action route must come last: /{name}/clone, /{name}/snapshots, /{name}/cdrom ... are declared above it.
ACTIONS = {"stop": ["shutdown"], "force-stop": ["destroy"], "pause": ["suspend"], "resume": ["resume"], "reboot": ["reboot"], "reset": ["reset"],
           "hibernate": ["managedsave"], "start": ["start"], "discard-hibernation": ["managedsave-remove"]}


@router.post("/{name}/{action}")
def vm_action(name: str, action: str):
    if action not in ACTIONS:
        raise HTTPException(404, f"Unknown action {action!r}")
    _need_vm(name)
    st = _state(_dominfo(name))
    if action == "start":
        if st in ("running", "in shutdown"):
            raise HTTPException(409, "The VM is already running.")
        if st == "paused":
            raise HTTPException(409, "The VM is paused; use Resume.")
        _preflight(name)
    if action in ("stop", "reboot", "pause", "force-stop", "hibernate", "reset") and st in ("shut off", "hibernated"):
        raise HTTPException(409, "The VM is not running.")
    if action == "resume" and st != "paused" and st != "suspended":
        raise HTTPException(409, "The VM is not paused.")
    if action == "resume" and st == "suspended":
        rc, _, err = _virsh("dompmwakeup", name)
        _must(rc, err)
        return {"ok": True, "action": action, "name": name}
    rc, _, err = _virsh(*ACTIONS[action], name, timeout=120 if action in ("start", "hibernate") else 30)
    _must(rc, err)
    return {"ok": True, "action": action, "name": name}
