"""Proxmox VM import API (CLAUDE.md §58).

GET  /api/pve/scan              discover VMs on former-Proxmox disks (read-only)
GET  /api/pve/confs?container=  read the VM definitions from a former pve root (if any)
POST /api/pve/import            activate the VM's storage, translate its config, and
                                create it in Helix's libvirt via routers/vms.create_vm

Discovery is tool-free (§58.3). Activation/mount/virsh run as root through
services/privileged. The hardware-dependent steps need lvm2 + the §34 hypervisor,
neither of which is in the 3.3.4 base image; this returns a clear 503 when a piece is
missing instead of half-importing.
"""
import os, tempfile
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import MNT_BASE
from services import pve_import, privileged
from routers import vms

router = APIRouter(prefix="/api/pve", tags=["pve"])


def _dir_roots() -> list[str]:
    roots = []
    for base in ("disk", "cache"):
        for n in range(1, 29):
            p = f"{MNT_BASE}/{base}{n}"
            if os.path.isdir(p):
                roots += [p, os.path.join(p, "images")]
    roots.append("/var/lib/vz")                    # Proxmox default 'dir' storage, if mounted there
    return [r for r in roots if os.path.isdir(r)]


def _scan() -> dict:
    data = pve_import.scan(_dir_roots())
    hints = []
    if not data["lvm2_present"]:
        hints.append("lvm2 is not installed: LVM VMs are listed from raw metadata but "
                     "cannot be activated/imported until lvm2 ships in the image.")
    if not data["zfs_present"]:
        hints.append("zfs is not installed: zvol-backed VMs are not scanned.")
    data["hints"] = hints
    return data


@router.get("/scan")
def scan():
    return _scan()


def _find(key: str) -> dict:
    for vm in _scan()["vms"]:
        if vm["key"] == key:
            return vm
    raise HTTPException(404, f"No discovered VM with key {key!r}")


def _read_conf(vm: dict, vmid: int):
    """Best effort: mount the VG's `root` LV read-only and read <vmid>'s config out
    of pmxcfs config.db (or a qemu-server dir). Returns a parsed config or None."""
    if vm["source"] != "lvm" or not pve_import._have("vgchange"):
        return None
    vg = vm["container"]
    root_dev = f"/dev/{vg}/root"
    privileged.run(["lvchange", "-ay", f"{vg}/root"], timeout=30)
    if not os.path.exists(root_dev):
        return None
    mnt = tempfile.mkdtemp(prefix="helix-pve-root-")
    try:
        if privileged.run(["mount", "-o", "ro", root_dev, mnt], timeout=30).returncode != 0:
            return None
        # config.db is root-owned 0600; copy to a readable temp so sqlite can open it.
        db = os.path.join(mnt, "var/lib/pve-cluster/config.db")
        confs = {}
        if os.path.exists(db):
            tmp = tempfile.mktemp(prefix="helix-pve-db-")
            if privileged.run(["cp", db, tmp]).returncode == 0:
                privileged.run(["chmod", "644", tmp])
                confs = pve_import.read_configdb(tmp)
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        if vmid not in confs:
            qs = os.path.join(mnt, "etc/pve/qemu-server", f"{vmid}.conf")
            txt = privileged.read_file(qs)
            if txt:
                confs[vmid] = txt
        return pve_import.parse_pve_conf(confs[vmid]) if vmid in confs else None
    finally:
        privileged.run(["umount", mnt], timeout=15)
        try:
            os.rmdir(mnt)
        except OSError:
            pass


@router.get("/confs")
def confs(container: str, vmid: int):
    """Read one VM's Proxmox config (used by the UI to preview before import)."""
    key = f"lvm:{container}:{vmid}"
    conf = _read_conf({"source": "lvm", "container": container}, vmid)
    if conf is None:
        raise HTTPException(404, "No Proxmox config found (need lvm2 + a former pve root LV).")
    return {"vmid": vmid, "container": container, "config": conf,
            "spec_preview": pve_import.import_spec(_find(key) if _has(key) else {"vmid": vmid, "disks": []}, conf)}


def _has(key: str) -> bool:
    return any(v["key"] == key for v in _scan()["vms"])


class ImportReq(BaseModel):
    key: str
    bridge: str = "br0"
    with_config: bool = True
    start: bool = False


@router.post("/import")
def do_import(req: ImportReq):
    vm = _find(req.key)
    if vm["template"]:
        raise HTTPException(400, "This is a Proxmox template (base-*); clone it in Proxmox first.")

    # 1) activate the VM's storage so its disk devices/files exist
    if vm["source"] == "lvm":
        if not pve_import._have("vgchange"):
            raise HTTPException(503, "LVM tools (lvm2) are not installed in this image — "
                                     "ship lvm2 (and thin-provisioning-tools) to import LVM VMs.")
        r = privileged.run(["vgchange", "-ay", vm["container"]], timeout=90)
        if r.returncode != 0:
            raise HTTPException(500, f"Could not activate VG {vm['container']}: {r.stderr.strip()}")
    elif vm["source"] == "zfs":
        if not pve_import._have("zpool"):
            raise HTTPException(503, "ZFS is not available in this image — import the pool first (ZFS page).")
        privileged.run(["zpool", "import", "-N", vm["container"]], timeout=60)   # harmless if already imported
    # 'dir' source is already mounted.

    # 2) optional Proxmox config
    conf = _read_conf(vm, vm["vmid"]) if req.with_config else None

    # 3) pick a NIC target: the requested Linux bridge if it exists, else libvirt NAT
    if req.bridge and os.path.isdir(f"/sys/class/net/{req.bridge}"):
        net_type, net_source = "bridge", req.bridge
    else:
        net_type, net_source = "network", "default"
        privileged.run(["virsh", "-c", "qemu:///system", "net-autostart", "default"])
        privileged.run(["virsh", "-c", "qemu:///system", "net-start", "default"])

    # 4) translate to a libvirt spec, then resolve block devices to /dev/disk/by-id
    #    paths (vmxml requires that for a passed-through disk) now that they're active
    spec = pve_import.import_spec(vm, conf, net_type=net_type, net_source=net_source)
    for d in spec["disks"]:
        if d["type"] == "block":
            d["path"] = pve_import.resolve_byid(d["path"])

    # 5) the disk images must now be present
    missing = [d["path"] for d in spec["disks"]
               if d["type"] == "block" and not os.path.exists(d["path"])]
    if missing:
        raise HTTPException(500, "Disk device(s) not present after activation: " + ", ".join(missing))

    # 5) create the VM (needs the §34 hypervisor; vms.create_vm raises 503 if libvirt is down)
    result = vms.create_vm(vms.VmSpec(spec=spec, start=req.start))
    return {"ok": True, "key": req.key, "used_config": conf is not None,
            "spec": spec, "warnings": spec.get("warnings", []), "create": result}
