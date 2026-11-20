"""VFIO binding safety. Offline, fake sysfs: python webui/tests/test_vfio_bind.py"""
import os, sys, tempfile
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
os.environ["HELIX_VFIO_CFG"] = str(tmp / "vfio-pci.cfg")
os.environ["HELIX_SYSFS_PCI"] = str(tmp / "devices")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from services import vfio_bind as vb  # noqa: E402

fails = 0
def check(name, cond):
    global fails
    fails += not cond
    print(("PASS " if cond else "FAIL ") + name)


def device(addr, drv):
    d = tmp / "devices" / addr
    d.mkdir(parents=True, exist_ok=True)
    if drv:
        (tmp / "drivers" / drv).mkdir(parents=True, exist_ok=True)
        link = d / "driver"
        if link.is_symlink():
            link.unlink()
        link.symlink_to(tmp / "drivers" / drv)


GPU, AUDIO, OTHER = "0000:21:00.0", "0000:21:00.1", "0000:10:00.0"
device(GPU, "nvidia"); device(AUDIO, "snd_hda_intel"); device(OTHER, "nvidia")

check("driver() reads the symlink (old code always returned '')", vb.driver(GPU) == "nvidia")
check("driver() of an unbound device is ''", (device("0000:30:00.0", None), vb.driver("0000:30:00.0"))[1] == "")
check("valid_addr accepts 0000:21:00.0", vb.valid_addr(GPU))
for bad in ["21:00.0", "0000:21:00.0; rm -rf /", "../../x", "0000:21:00.8", ""]:
    check(f"valid_addr rejects {bad!r}", not vb.valid_addr(bad))

check("nothing saved yet", vb.saved() == [])
vb.save([GPU, AUDIO])
check("saved per address", vb.saved() == [GPU, AUDIO])
check("config has one BIND line", "BIND=0000:21:00.0 0000:21:00.1" in Path(os.environ["HELIX_VFIO_CFG"]).read_text())
vb.save([GPU])
check("saving again does not duplicate", vb.saved() == [GPU, AUDIO])
try:
    vb.save(["0000:21:00.0 0000:10:00.0"])
    check("save rejects a smuggled second address", False)
except ValueError:
    check("save rejects a smuggled second address", True)
vb.forget([AUDIO])
check("forget removes only that address", vb.saved() == [GPU])
vb.forget([GPU])
check("forget all leaves no BIND line", vb.saved() == [] and "BIND=" not in Path(os.environ["HELIX_VFIO_CFG"]).read_text())

busy = lambda files: [os.getpid()]
idle = lambda files: []
unknown = lambda files: [-1]
check("busy GPU reports its user", vb.users(GPU, fuser=busy) and "pid" in vb.users(GPU, fuser=busy)[0])
check("idle GPU has no users", vb.users(GPU, fuser=idle) == [])
check("fuser failure counts as busy", vb.users(GPU, fuser=unknown) != [])
check("audio function is never 'in use' (not a GPU driver)", vb.users(AUDIO, fuser=busy) == [])

# The handover must refuse the whole group, without touching sysfs, when the GPU is busy.
writes = []
vb._root_write = lambda path, value: writes.append(path) or True
orig = vb.users
vb.users = lambda addr, drv=None, fuser=None: ["Plex Transcoder (pid 4242)"] if addr == GPU else []
r = vb.handover([GPU, AUDIO])
vb.users = orig
check("busy: nothing moved", r["moved"] == [])
check("busy: both group members skipped with the reason", set(r["skipped"]) == {GPU, AUDIO}
      and "Plex Transcoder" in r["skipped"][GPU])
check("busy: no sysfs write at all", writes == [])

print("FAILED" if fails else "all passed")
sys.exit(1 if fails else 0)
