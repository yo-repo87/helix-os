"""Proxmox VM discovery, config translation, and import (CLAUDE.md §58).

Finds VMs on former-Proxmox disks and brings them into Helix's libvirt hypervisor
(§34). Three stages, each callable on its own:

  scan()         read-only: list VMs + their disk images (LVM / ZFS / dir). Never
                 activates an LV, imports a pool, or mounts anything.
  read_confs()   best effort: from an *already-mounted* former Proxmox root, read the
                 VM definitions out of pmxcfs's config.db (SQLite) or a qemu-server dir.
  import_spec()  pure: turn a discovered VM (+ optional Proxmox conf) into the spec
                 services/vmxml.py builds a domain from. No I/O.

The privileged side (vgchange/lvchange, zpool import, mount, virsh) lives in
routers/pve.py, which calls activate_* here. LVM is read with lvm2 when present, else
straight from each PV's plain-text metadata — the base image ships neither lvm2 nor
zfs, and the raw path is enough to discover (CLAUDE.md §58.3).

CLI:  python3 pve_import.py scan [dir-root ...]
      python3 pve_import.py conf <vmid.conf>          # translate one config
      python3 pve_import.py configdb <config.db>      # dump VM configs from pmxcfs db
"""
from __future__ import annotations
import glob, json, os, re, shutil, sqlite3, subprocess, sys

VMDISK = re.compile(r"(?:vm|base)-(\d+)-disk-(\d+)")
_DISK_KEY = re.compile(r"^(scsi|virtio|sata|ide)(\d+)$")


_SBIN = ("/sbin", "/usr/sbin", "/usr/local/sbin", "/bin", "/usr/bin")


def _run(*cmd, timeout=20):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return 127, "", str(e)


def _priv_run(*cmd, timeout=20):
    """lvs/zfs/vgchange and reading PV devices need root; the backend runs as the
    unprivileged `helix` user, so prefix sudo -n unless we are already root."""
    if os.geteuid() != 0:
        cmd = ("sudo", "-n") + cmd
    return _run(*cmd, timeout=timeout)


def _have(cmd):
    return shutil.which(cmd) is not None or any(os.path.exists(os.path.join(p, cmd)) for p in _SBIN)


def _read_pv_head(pv, n=1024 * 1024):
    """First ~1 MiB of a PV (its LVM metadata area) — via sudo dd when not root."""
    if os.geteuid() == 0:
        try:
            with open(pv, "rb") as fh:
                return fh.read(n)
        except OSError:
            return b""
    try:
        r = subprocess.run(["sudo", "-n", "dd", f"if={pv}", "bs=1M", "count=1"],
                           capture_output=True, timeout=20)
        return r.stdout if r.returncode == 0 else b""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return b""


# ── LVM ───────────────────────────────────────────────────────────────────────

def _lvm_pv_devices():
    _, out, _ = _run("lsblk", "-rno", "NAME,FSTYPE")
    return ["/dev/" + l.split()[0] for l in out.splitlines()
            if len(l.split()) >= 2 and l.split()[1] == "LVM2_member"]


def _scan_lvm_tools():
    rc, out, _ = _priv_run("lvs", "--reportformat", "json", "--units", "b", "--nosuffix",
                      "-o", "lv_name,vg_name,lv_size,lv_attr,pool_lv")
    if rc != 0 or not out.strip():
        return None
    try:
        rep = json.loads(out)["report"][0]["lv"]
    except (ValueError, KeyError, IndexError):
        return None
    disks = []
    for lv in rep:
        m = VMDISK.fullmatch(lv.get("lv_name", ""))
        if not m:
            continue
        disks.append(_disk("lvm", vmid=int(m.group(1)), container=lv["vg_name"],
                           name=lv["lv_name"], path=f"/dev/{lv['vg_name']}/{lv['lv_name']}",
                           fmt="raw", dtype="block", template=lv["lv_name"].startswith("base-"),
                           bytes_=int(lv.get("lv_size") or 0) or None))
    return disks


def _scan_lvm_raw():
    disks = []
    for pv in _lvm_pv_devices():
        blob = _read_pv_head(pv)
        if not blob:
            continue
        text = blob.decode("latin-1", "replace")
        mvg = re.search(r"([A-Za-z0-9_+.\-]+)\s*\{\s*id\s*=", text)
        vg = mvg.group(1) if mvg else None
        for name in sorted(set(re.findall(r"(?:vm|base)-\d+-disk-\d+", text))):
            m = VMDISK.fullmatch(name)
            disks.append(_disk("lvm", vmid=int(m.group(1)), container=vg, name=name,
                               path=f"/dev/{vg}/{name}" if vg else None, fmt="raw",
                               dtype="block", template=name.startswith("base-"), bytes_=None,
                               note="read from PV metadata; install lvm2 for sizes + activation"))
    return disks


def scan_lvm():
    if _have("lvs"):
        r = _scan_lvm_tools()
        if r is not None:                     # None = lvs failed/unparseable → fall back
            return r
    return _scan_lvm_raw()


# ── ZFS (zvols) ────────────────────────────────────────────────────────────────

def scan_zfs():
    if not _have("zfs"):
        return []
    disks = []
    _, out, _ = _priv_run("zfs", "list", "-H", "-p", "-t", "volume", "-o", "name,volsize")
    for line in out.splitlines():
        p = line.split("\t")
        m = VMDISK.search(p[0]) if p else None
        if not m:
            continue
        pool = p[0].split("/", 1)[0]
        disks.append(_disk("zfs", vmid=int(m.group(1)), container=pool, name=p[0],
                           path="/dev/zvol/" + p[0], fmt="raw", dtype="block",
                           template="base-" in p[0],
                           bytes_=int(p[1]) if len(p) > 1 and p[1].isdigit() else None))
    return disks


# ── directory (qcow2/raw) ──────────────────────────────────────────────────────

def scan_dir(roots):
    disks = []
    for root in roots or []:
        for pat in ("images/*/vm-*-disk-*", "images/*/base-*-disk-*",
                    "vm-*-disk-*.qcow2", "vm-*-disk-*.raw", "vm-*-disk-*.vmdk"):
            for p in glob.glob(os.path.join(root, pat)):
                m = VMDISK.search(os.path.basename(p))
                if not m:
                    continue
                fmt = ("qcow2" if p.endswith(".qcow2") else "vmdk" if p.endswith(".vmdk") else "raw")
                try:
                    sz = os.path.getsize(p)
                except OSError:
                    sz = None
                disks.append(_disk("dir", vmid=int(m.group(1)), container=os.path.dirname(p),
                                   name=os.path.basename(p), path=p, fmt=fmt, dtype="file",
                                   template=os.path.basename(p).startswith("base-"), bytes_=sz))
    return disks


def _disk(source, *, vmid, container, name, path, fmt, dtype, template, bytes_, note=None):
    d = {"vmid": vmid, "source": source, "container": container, "name": name,
         "path": path, "format": fmt, "dtype": dtype, "template": template, "bytes": bytes_}
    if note:
        d["note"] = note
    return d


# ── grouping (keyed by storage + vmid, so vm-100 in two VGs stays two VMs, §58.8) ─

def group_vms(disks):
    vms = {}
    for d in disks:
        key = f"{d['source']}:{d['container']}:{d['vmid']}"
        v = vms.setdefault(key, {"key": key, "vmid": d["vmid"], "source": d["source"],
                                 "container": d["container"], "template": False, "disks": []})
        v["disks"].append(d)
        v["template"] = v["template"] or d["template"]
    for v in vms.values():
        v["disks"].sort(key=lambda d: d["name"])
    return [vms[k] for k in sorted(vms)]


def scan(dir_roots=None):
    disks = (scan_lvm() or []) + (scan_zfs() or []) + (scan_dir(dir_roots) or [])
    return {"vms": group_vms(disks),
            "lvm2_present": _have("lvs"), "zfs_present": _have("zfs")}


# ── Proxmox config.db (pmxcfs) + qemu-server dir readers ───────────────────────

def read_confs(mounted_root):
    """Given a mounted former-Proxmox root, return {vmid: conf_text}. Prefers the
    pmxcfs SQLite db (the live source); falls back to a plain qemu-server dir (as a
    vzdump/backup would have)."""
    out = {}
    db = os.path.join(mounted_root, "var/lib/pve-cluster/config.db")
    if os.path.exists(db):
        out.update(read_configdb(db))
    d = os.path.join(mounted_root, "etc/pve/qemu-server")
    if os.path.isdir(d):
        for f in glob.glob(os.path.join(d, "*.conf")):
            vmid = os.path.splitext(os.path.basename(f))[0]
            if vmid.isdigit():
                out.setdefault(int(vmid), open(f, encoding="utf-8", errors="replace").read())
    return out


def read_configdb(db_path):
    """pmxcfs stores every file of /etc/pve as a row in config.db (table `tree`,
    columns name/data). The VM configs are name='<vmid>.conf' under qemu-server."""
    out = {}
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
        for name, data in con.execute("SELECT name, data FROM tree"):
            m = re.fullmatch(r"(\d+)\.conf", name or "")
            if m:
                out[int(m.group(1))] = data.decode() if isinstance(data, bytes) else str(data)
        con.close()
    except sqlite3.Error:
        pass
    return out


# ── Proxmox conf → Helix/libvirt (vmxml) spec ──────────────────────────────────

def parse_pve_conf(text):
    cfg = {}
    for line in text.splitlines():
        if line.startswith("["):          # first [snapshot] header ends the current config
            break
        if line and not line.startswith("#") and ":" in line:
            k, v = line.split(":", 1)
            cfg[k.strip()] = v.strip()
    return cfg


def _vol(val):
    first, *rest = val.split(",")
    opts = dict(i.split("=", 1) if "=" in i else (i, "1") for i in rest)
    return first, opts


def _match_disk(volume, scanned):
    """Resolve a Proxmox volume id ('local-lvm:vm-100-disk-0') to a scanned disk."""
    diskname = volume.split(":")[-1].split("/")[-1]
    for d in scanned:
        if d["name"] == diskname or os.path.basename(d.get("path") or "") == diskname:
            return d
    return None


def import_spec(vm, conf=None, *, net_type="bridge", net_source="br0", bridge=None,
                default_vcpus=2, default_mem=2048):
    """Pure: build the spec services/vmxml.py normalises. `vm` is a scan() entry;
    `conf` is a parsed Proxmox config (or None for disk-only import). The NIC lands on
    `net_source` (a Linux bridge when net_type='bridge', else a libvirt network name).
    `bridge=` is kept as an alias for net_source."""
    if bridge is not None:
        net_source = bridge
    scanned = vm["disks"]
    name = f"pve-{vm['vmid']}"
    spec = {"name": name, "vcpus": default_vcpus, "memory_mb": default_mem,
            "machine": "q35", "firmware": "seabios", "tpm": False,
            "disks": [], "cdroms": [], "nics": [],
            "graphics": [{"type": "vnc"}],       # a console so the imported VM is viewable
            "warnings": []}
    used = set()

    if conf:
        name = conf.get("name", name).strip() or name
        spec["name"] = re.sub(r"[^A-Za-z0-9_.-]", "-", name)[:48]
        spec["vcpus"] = max(1, int(conf.get("cores", "1")) * int(conf.get("sockets", "1")))
        spec["memory_mb"] = int(conf.get("memory", default_mem))
        spec["machine"] = "q35" if conf.get("machine", "").startswith("q35") else "i440fx"
        spec["firmware"] = "ovmf" if conf.get("bios") == "ovmf" else "seabios"
        spec["tpm"] = "tpmstate0" in conf and spec["firmware"] != "seabios"
        for key, val in conf.items():
            m = _DISK_KEY.fullmatch(key)
            if m and "media=cdrom" not in val:
                vol, opts = _vol(val)
                d = _match_disk(vol, scanned)
                if d:
                    used.add(d["name"])
                    spec["disks"].append({"type": d["dtype"], "bus": m.group(1),
                                          "format": d["format"], "path": d["path"],
                                          "discard": opts.get("discard") == "on"})
                else:
                    spec["warnings"].append(f"{key}: volume {vol} not found on these disks")
            elif m and "media=cdrom" in val:
                spec["cdroms"].append({"bus": "sata", "path": ""})   # ISO not carried over
            elif key.startswith("net"):
                model = re.search(r"\b(virtio|e1000|rtl8139|vmxnet3)\b", val)
                mac = re.search(r"=([0-9A-Fa-f:]{17})", val)
                br = re.search(r"bridge=(\w+)", val)
                spec["nics"].append({"type": net_type, "source": net_source,
                                     "model": model.group(1) if model else "virtio",
                                     "mac": mac.group(1) if mac else None})
                if br and br.group(1) != net_source:
                    spec["warnings"].append(f"{key}: Proxmox bridge {br.group(1)} -> {net_source}")
        if conf.get("efidisk0"):
            spec["warnings"].append("UEFI VM: Helix creates fresh OVMF vars (Proxmox efidisk not reused).")
        if any(k.startswith("hostpci") for k in conf):
            spec["warnings"].append("VM used PCI passthrough (hostpci) — reassign it on Helix.")

    # Any disks not referenced by the conf (or disk-only import) get attached virtio.
    for d in scanned:
        if d["name"] not in used and not d["template"]:
            spec["disks"].append({"type": d["dtype"], "bus": "virtio",
                                  "format": d["format"], "path": d["path"]})
    if not conf:
        spec["nics"].append({"type": net_type, "source": net_source, "model": "virtio"})
        spec["warnings"].append("No Proxmox config found — using a default 2-vCPU/2 GB shell; "
                                "check CPU/RAM/boot order before first start.")
    spec["warnings"].append("Source VM must be powered off in Proxmox; its disks must be quiesced.")
    return spec


# ── activation (privileged; routers/pve.py calls these before import) ──────────

def resolve_byid(path):
    """libvirt/vmxml wants a /dev/disk/by-id/... path for a passed-through block
    device (stable across reboots). After activation an LVM LV / zvol has
    dm-name/dm-uuid (or wwn) symlinks there; return one, preferring dm-name."""
    if not path:
        return path
    try:
        target = os.path.realpath(path)
    except OSError:
        return path
    byid = "/dev/disk/by-id"
    best = None
    try:
        for name in os.listdir(byid):
            lnk = os.path.join(byid, name)
            try:
                if os.path.realpath(lnk) == target:
                    if name.startswith("dm-name-"):
                        return lnk               # most readable; good enough
                    best = best or lnk
            except OSError:
                continue
    except OSError:
        return path
    return best or path


def activate_lvm(vg):
    return _priv_run("vgchange", "-ay", vg, timeout=60)


def activate_lv(vg, lv):
    return _priv_run("lvchange", "-ay", f"{vg}/{lv}", timeout=60)


# ── CLI ────────────────────────────────────────────────────────────────────────

def main(argv):
    if len(argv) >= 3 and argv[1] == "conf":
        print(json.dumps(import_spec({"vmid": 0, "disks": []},
                                     parse_pve_conf(open(argv[2]).read())), indent=2))
    elif len(argv) >= 3 and argv[1] == "configdb":
        print(json.dumps({str(k): v for k, v in read_configdb(argv[2]).items()}, indent=2))
    else:
        print(json.dumps(scan(argv[2:] if len(argv) > 2 else []), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
