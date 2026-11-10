"""Offline tests for services/vmxml.py (no libvirt needed). Run: webui/.venv/bin/python webui/tests/test_vmxml.py"""
import copy, os, sys, xml.etree.ElementTree as ET
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from services import vmxml

ok = bad = 0
def T(n, c, x=""):
    global ok, bad
    if c: ok += 1
    else: bad += 1; print("FAIL", n, "->", x)

def raises(spec, frag):
    try: vmxml.normalize(spec); return False
    except vmxml.SpecError as e: return frag.lower() in str(e).lower()

HOST = {"kvm": True, "amd": True}
LINUX = {"name": "ubuntu", "vcpus": 2, "memory_mb": 2048,
         "disks": [{"path": "/mnt/cache1/domains/ubuntu/vdisk1.img", "boot": 1}],
         "cdroms": [{"path": "/mnt/user/isos/ubuntu.iso", "boot": 2}],
         "nics": [{"source": "virbr0"}], "graphics": [{"type": "vnc"}]}
WIN = {"name": "Windows 11", "template": "windows11", "vcpus": 4, "threads": 2, "pins": [2, 6, 3, 7], "memory_mb": 8192, "max_memory_mb": 16384,
       "machine": "q35", "firmware": "ovmf-secure", "tpm": True, "hyperv": True, "clock": "localtime", "sound": "ich9", "description": "Gaming",
       "webui": "http://10.0.0.5:3389",
       "disks": [{"path": "/mnt/cache1/domains/w11/vdisk1.img", "bus": "virtio", "discard": True, "serial": "WIN11SSD", "boot": 1},
                 {"path": "/mnt/disk3/domains/w11/data.img", "format": "raw", "bus": "sata", "ssd": True},
                 {"path": "/dev/disk/by-id/ata-WDC_X", "type": "block", "bus": "scsi"}],
       "cdroms": [{"path": "/mnt/user/isos/w11.iso", "boot": 2}, {"path": "/mnt/user/isos/virtio-win.iso"}],
       "nics": [{"type": "bridge", "source": "br0", "model": "virtio", "mac": "52:54:00:aa:bb:cc"}, {"source": "default", "model": "e1000"}],
       "graphics": [{"type": "spice", "video": "qxl", "password": "", "copypaste": False}],
       "pci": [{"address": "0000:01:00.0", "rom": "/mnt/user/isos/vbios.rom"}, {"address": "0000:01:00.1"}],
       "usb": [{"vendor": "046d", "product": "c52b"}],
       "shares": [{"mode": "virtiofs", "source": "/mnt/user/media", "target": "media"}, {"mode": "9p", "source": "/mnt/user/docs", "target": "docs", "readonly": True}],
       "qemu_args": ["-global", "ICH9-LPC.disable_s3=1"]}

for label, spec in (("linux", LINUX), ("windows", WIN)):
    x = vmxml.build_xml(copy.deepcopy(spec), HOST)
    try: ET.fromstring(x); T(f"{label}: well-formed XML", True)
    except ET.ParseError as e: T(f"{label}: well-formed XML", False, e)
    back = vmxml.parse_xml(x)
    T(f"{label}: nothing unsupported on our own output", back["unsupported"] == [], back["unsupported"])
    # second generation from the parsed spec must be byte-identical (idempotent edit cycle)
    x2 = vmxml.build_xml(back, HOST)
    T(f"{label}: edit round trip is byte-identical", x == x2, "\n".join(__import__("difflib").unified_diff(x.splitlines(), x2.splitlines(), lineterm="", n=0)))

x = vmxml.build_xml(copy.deepcopy(WIN), HOST); r = ET.fromstring(x)
T("windows: secure-boot loader + nvram + smm", r.find("os/loader").get("secure") == "yes" and "secboot" in r.find("os/loader").text and r.find("features/smm") is not None and r.find("os/nvram") is not None)
T("windows: TPM 2.0 emulator", r.find("devices/tpm/backend").get("version") == "2.0")
T("windows: hyper-v enlightenments + hypervclock", r.find("features/hyperv") is not None and any(t.get("name") == "hypervclock" for t in r.findall("clock/timer")))
T("windows: topology 2 cores x 2 threads, topoext on AMD", r.find("cpu/topology").get("cores") == "2" and r.find("cpu/topology").get("threads") == "2" and r.find("cpu/feature").get("name") == "topoext")
T("windows: pinning", [(p.get("vcpu"), p.get("cpuset")) for p in r.findall("cputune/vcpupin")] == [("0", "2"), ("1", "6"), ("2", "3"), ("3", "7")])
T("windows: max/current memory", r.find("memory").text == "16384" and r.find("currentMemory").text == "8192")
T("windows: kvm hidden because a GPU is passed through", r.find("features/kvm/hidden").get("state") == "on")
T("windows: virtiofs needs shared memory backing", r.find("memoryBacking/access").get("mode") == "shared")
names = [d.find("target").get("dev") for d in r.findall("devices/disk")]
T("disk names are unique and follow the bus (vda, sda, sdb, sdc, sdd)", names == ["vda", "sda", "sdb", "sdc", "sdd"], names)
T("virtio-scsi controller added for the scsi disk", any(c.get("model") == "virtio-scsi" for c in r.findall("devices/controller")))
T("block device passthrough uses source dev and cache=none", r.findall("devices/disk")[2].find("source").get("dev") == "/dev/disk/by-id/ata-WDC_X" and r.findall("devices/disk")[2].find("driver").get("cache") == "none")
T("ssd flag becomes rotation_rate", r.findall("devices/disk")[1].find("target").get("rotation_rate") == "1")
T("boot order kept", [b.get("order") for b in r.findall("devices/disk/boot")] == ["1", "2"])
T("GPU passthrough: two PCI hostdevs, rom kept", len(r.findall("devices/hostdev[@type='pci']")) == 2 and r.find("devices/hostdev/rom").get("file") == "/mnt/user/isos/vbios.rom")
T("USB device by vendor:product", r.find("devices/hostdev[@type='usb']/source/vendor").get("id") == "0x046d")
T("qemu extra args", [a.get("value") for a in r.findall("{%s}commandline/{%s}arg" % (vmxml.QEMU_NS, vmxml.QEMU_NS))] == ["-global", "ICH9-LPC.disable_s3=1"])
T("spice gets a clipboard setting and the vmc channel", r.find("devices/graphics/clipboard").get("copypaste") == "no" and r.find("devices/channel[@type='spicevmc']") is not None)
T("metadata: template + webui url", r.find("metadata/{%s}vm/{%s}template" % (vmxml.HELIX_NS, vmxml.HELIX_NS)).text == "windows11")
T("no host KVM falls back to plain QEMU", ET.fromstring(vmxml.build_xml(copy.deepcopy(LINUX), {"kvm": False})).get("type") == "qemu")
lx = ET.fromstring(vmxml.build_xml(copy.deepcopy(LINUX), HOST))
T("linux default: SeaBIOS, no loader, no TPM", lx.find("os/loader") is None and lx.find("devices/tpm") is None)
T("default MAC generated and unicast", int(lx.find("devices/interface/mac").get("address").split(":")[0], 16) % 2 == 0)

# ---- validation
base = lambda **k: {**copy.deepcopy(LINUX), **k}
for label, spec, frag in [
    ("empty name", base(name=""), "Name"), ("name with slash", base(name="a/b"), "Name"), ("name too long", base(name="x" * 80), "Name"),
    ("zero vcpus", base(vcpus=0), "vCPUs"), ("odd vcpus with 2 threads", base(vcpus=3, threads=2), "Threads"),
    ("pins count mismatch", base(vcpus=2, pins=[1]), "Pinning"), ("pins duplicate", base(vcpus=2, pins=[1, 1]), "twice"),
    ("max memory below initial", base(memory_mb=4096, max_memory_mb=1024), "Max memory"), ("tiny memory", base(memory_mb=8), "Memory"),
    ("secure boot on i440fx", base(firmware="ovmf-secure", machine="i440fx"), "Q35"), ("tpm on seabios", base(tpm=True), "TPM"),
    ("relative disk path", base(disks=[{"path": "vdisk.img"}]), "full path"), ("bad disk format", base(disks=[{"path": "/a", "format": "vmdk"}]), "format"),
    ("bad bus", base(disks=[{"path": "/a", "bus": "nvme"}]), "bus"), ("bad serial", base(disks=[{"path": "/a", "serial": "a b;c"}]), "serial"),
    ("bad mac", base(nics=[{"source": "br0", "mac": "zz"}]), "MAC"), ("multicast mac", base(nics=[{"source": "br0", "mac": "53:54:00:00:00:00"}]), "unicast"),
    ("nic source with shell chars", base(nics=[{"source": "br0;rm"}]), "source"), ("vnc port too low", base(graphics=[{"port": 80}]), "port"),
    ("vnc password too long", base(graphics=[{"password": "123456789"}]), "8 characters"),
    ("bad pci address", base(pci=[{"address": "01:00.0"}]), "address"), ("bad usb id", base(usb=[{"vendor": "zz", "product": "1"}]), "hex"),
    ("share with ..", base(shares=[{"source": "/mnt/../etc", "target": "x"}]), ".."), ("share tag with space", base(shares=[{"source": "/mnt/x", "target": "a b"}]), "tag"),
    ("duplicate share tag", base(shares=[{"source": "/mnt/a", "target": "t"}, {"source": "/mnt/b", "target": "t"}]), "twice"),
    ("qemu arg with newline", base(qemu_args=["-a\n-b"]), "line breaks")]:
    T(f"rejects: {label}", raises(spec, frag), vmxml.__name__)

# ---- injection: hostile text must end up as inert data, never as XML structure
evil = base(name="vm1", description='</description><devices><disk type="file"><source file="/etc/shadow"/></disk></devices><description>',
            webui='"><x/>', disks=[{"path": '/mnt/x/"><evil/>.img'}])
ex = vmxml.build_xml(evil, HOST); er = ET.fromstring(ex)
T("injection via description/webui/path is escaped", er.find("devices/disk/source").get("file") == '/mnt/x/"><evil/>.img' and len(er.findall("devices/disk")) == 2 and er.find(".//evil") is None and "shadow" not in [d.find("source").get("file") for d in er.findall("devices/disk")])

# ---- an unknown domain: what the form cannot express is reported, not dropped
foreign = """<domain type='kvm'><name>old</name><memory unit='KiB'>1048576</memory><vcpu>1</vcpu><os><type arch='x86_64' machine='pc-i440fx-7.2'>hvm</type></os>
<devices><disk type='network' device='disk'><source protocol='rbd' name='x'/><target dev='vda' bus='virtio'/></disk><smartcard mode='passthrough'/>
<interface type='direct'><source dev='eth0'/></interface><controller type='usb' model='qemu-xhci'/></devices></domain>"""
f = vmxml.parse_xml(foreign)
T("foreign XML: reports what the form cannot express", {"disk of type network", "smartcard", "network of type direct"} <= set(f["unsupported"]), f["unsupported"])
T("foreign XML: memory in KiB converted", f["memory_mb"] == 1024 and f["machine"] == "i440fx")
print(f"\n{ok} passed, {bad} failed"); sys.exit(1 if bad else 0)
