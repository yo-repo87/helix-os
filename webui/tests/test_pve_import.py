"""Offline tests for services/pve_import.py (CLAUDE.md §58). No lvm2/zfs/libvirt."""
import os, sqlite3, sys, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from services import pve_import as P   # noqa: E402

ok = 0
def check(cond, msg):
    global ok
    assert cond, "FAIL: " + msg
    ok += 1
    print("ok:", msg)


# ── grouping keys by (source, container, vmid) so vm-100 in two VGs stays two VMs ─
disks = (
    P.scan_dir.__wrapped__ if hasattr(P.scan_dir, "__wrapped__") else lambda *_: []
)  # noop; build disks by hand instead
raw = [
    P._disk("lvm", vmid=100, container="pve", name="vm-100-disk-0", path="/dev/pve/vm-100-disk-0",
            fmt="raw", dtype="block", template=False, bytes_=None),
    P._disk("lvm", vmid=100, container="pve", name="vm-100-disk-1", path="/dev/pve/vm-100-disk-1",
            fmt="raw", dtype="block", template=False, bytes_=None),
    P._disk("lvm", vmid=100, container="proxpi", name="vm-100-disk-0", path="/dev/proxpi/vm-100-disk-0",
            fmt="raw", dtype="block", template=False, bytes_=None),
    P._disk("lvm", vmid=102, container="pve", name="base-102-disk-0", path="/dev/pve/base-102-disk-0",
            fmt="raw", dtype="block", template=True, bytes_=None),
]
vms = P.group_vms(raw)
check(len(vms) == 3, "vm-100 in pve and proxpi -> two VMs, plus 102 = 3 groups")
pve100 = [v for v in vms if v["key"] == "lvm:pve:100"][0]
check(len(pve100["disks"]) == 2, "pve:100 has its two disks grouped")
check(any(v["key"] == "lvm:proxpi:100" for v in vms), "proxpi:100 is a separate VM")
check([v for v in vms if v["vmid"] == 102][0]["template"], "base-102 flagged as template")


# ── Proxmox conf parsing (stops at the first snapshot section) ──────────────────
CONF = """\
boot: order=scsi0
cores: 4
sockets: 2
memory: 8192
name: web-server
bios: ovmf
machine: q35
scsihw: virtio-scsi-single
scsi0: local-lvm:vm-100-disk-0,size=32G,discard=on,ssd=1
scsi1: local-lvm:vm-100-disk-1,size=500G
ide2: local:iso/debian.iso,media=cdrom
net0: virtio=BC:24:11:AA:BB:CC,bridge=vmbr0,firewall=1
efidisk0: local-lvm:vm-100-disk-9,size=4M
agent: 1
[snap1]
memory: 999999
"""
cfg = P.parse_pve_conf(CONF)
check(cfg["cores"] == "4" and cfg["memory"] == "8192", "conf parsed")
check(cfg.get("memory") != "999999", "snapshot section ignored")


# ── translation to the vmxml spec shape ────────────────────────────────────────
spec = P.import_spec(pve100, cfg, bridge="br0")
check(spec["name"] == "web-server", "name from conf")
check(spec["vcpus"] == 8, "vcpus = cores*sockets")
check(spec["memory_mb"] == 8192, "memory carried")
check(spec["machine"] == "q35" and spec["firmware"] == "ovmf", "q35 + ovmf")
d0 = spec["disks"][0]
check(d0["type"] == "block" and d0["bus"] == "scsi" and d0["path"] == "/dev/pve/vm-100-disk-0",
      "scsi0 resolved to the real block device, bus=scsi")
check(d0.get("discard") is True, "discard=on carried")
check(any(d["path"] == "/dev/pve/vm-100-disk-1" for d in spec["disks"]), "scsi1 resolved")
check(len(spec["nics"]) == 1 and spec["nics"][0]["type"] == "bridge"
      and spec["nics"][0]["mac"] == "BC:24:11:AA:BB:CC" and spec["nics"][0]["source"] == "br0",
      "net0 -> bridge nic on br0 with MAC preserved")
check(any("vmbr0" in w for w in spec["warnings"]), "bridge remap warning")
check(any("efidisk" in w.lower() or "OVMF" in w for w in spec["warnings"]), "efidisk warning")


# ── disk-only import (no conf) gets a default shell + the disks ─────────────────
spec2 = P.import_spec(pve100, None, bridge="br0")
check(spec2["vcpus"] == 2 and spec2["memory_mb"] == 2048, "default shell when no conf")
check(len(spec2["disks"]) == 2 and all(d["bus"] == "virtio" for d in spec2["disks"]),
      "both disks attached virtio with no conf")
check(any("default" in w.lower() for w in spec2["warnings"]), "disk-only warning present")


# ── pmxcfs config.db reader (build a tiny db matching Proxmox's schema) ─────────
dbf = tempfile.mktemp(suffix=".db")
con = sqlite3.connect(dbf)
con.execute("CREATE TABLE tree (inode INTEGER, parent INTEGER, version INTEGER, "
            "writer INTEGER, mtime INTEGER, type INTEGER, name TEXT, data BLOB)")
con.execute("INSERT INTO tree (name, data) VALUES (?, ?)", ("100.conf", CONF.encode()))
con.execute("INSERT INTO tree (name, data) VALUES (?, ?)", ("datacenter.cfg", b"keyboard: en-us"))
con.commit(); con.close()
got = P.read_configdb(dbf)
check(set(got) == {100}, "config.db: only <vmid>.conf rows returned")
check("cores: 4" in got[100], "config.db: conf text recovered")
os.unlink(dbf)


# ── the spec is accepted by vmxml.normalize (shape is valid) ───────────────────
try:
    from services import vmxml
    vmxml.normalize(dict(spec))
    check(True, "import_spec output passes vmxml.normalize")
    vmxml.normalize(dict(spec2))
    check(True, "disk-only spec passes vmxml.normalize")
except ImportError:
    print("skip: vmxml not importable standalone")
except Exception as e:                       # SpecError etc.
    raise AssertionError(f"FAIL: vmxml.normalize rejected import_spec: {e}")

print(f"\nALL {ok} CHECKS PASSED")
