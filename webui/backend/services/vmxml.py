"""VM spec <-> libvirt domain XML.

The VM form posts a plain "spec" (a dict); this module turns it into domain XML and parses a domain's XML
back into the same spec, so a VM can be edited in the form. It is pure (no libvirt, no disk access) so it can
be tested anywhere. Anything in an existing domain that the spec cannot express is reported in
spec["unsupported"]; the UI then offers the XML editor instead of the form, so nothing is silently dropped.

Spec (all keys optional unless noted; defaults in DEFAULTS):
  name (required), description, template, webui, uuid
  cpu_mode  host-passthrough | host-model        vcpus  threads (1|2)   pins [host cpu ids]   migratable
  memory_mb (initial)  max_memory_mb
  machine   q35 | i440fx           firmware seabios | ovmf | ovmf-secure          tpm (bool)   hyperv (bool)
  clock     utc | localtime        usb_controller qemu-xhci | nec-xhci | ich9-ehci1 | none
  disks     [{path, format, bus, size_gb, discard, ssd, serial, boot, type(file|block), readonly}]
  cdroms    [{path, bus, boot}]    (path may be empty: an empty drive)
  nics      [{type(network|bridge), source, model, mac}]
  graphics  [{type(vnc|spice), port, listen, password, keymap, video(qxl|virtio|vga|cirrus|none), copypaste}]
  pci       [{address "0000:01:00.0", rom}]       usb [{vendor, product}]
  shares    [{mode(virtiofs|9p), source, target, readonly}]
  sound     none | ich9 | ac97 | ich6 | es1370 | sb16     input_tablet (bool)    agent (bool)
  qemu_args [str]
"""
import re
import uuid
import xml.etree.ElementTree as ET

QEMU_NS = "http://libvirt.org/schemas/domain/qemu/1.0"
HELIX_NS = "https://helix-os.local/vm/1"
ET.register_namespace("qemu", QEMU_NS)
ET.register_namespace("helix", HELIX_NS)

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,62}$")
MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
PCI_RE = re.compile(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$")
HEX4 = re.compile(r"^[0-9a-fA-F]{4}$")

DEFAULTS = {
    "description": "", "template": "other", "webui": "",
    "cpu_mode": "host-passthrough", "vcpus": 2, "threads": 1, "pins": [], "migratable": False,
    "memory_mb": 2048, "max_memory_mb": 0,
    "machine": "q35", "firmware": "seabios", "tpm": False, "hyperv": False, "clock": "utc",
    "usb_controller": "qemu-xhci", "input_tablet": True, "agent": True, "sound": "none",
    "disks": [], "cdroms": [], "nics": [], "graphics": [], "pci": [], "usb": [], "shares": [], "qemu_args": [],
}

OVMF = {   # firmware files shipped by Debian's `ovmf`; the router passes what actually exists on the host
    "seabios": None,
    "ovmf": ("/usr/share/OVMF/OVMF_CODE_4M.fd", "/usr/share/OVMF/OVMF_VARS_4M.fd"),
    "ovmf-secure": ("/usr/share/OVMF/OVMF_CODE_4M.secboot.fd", "/usr/share/OVMF/OVMF_VARS_4M.ms.fd"),
}
NVRAM_DIR = "/var/lib/libvirt/qemu/nvram"

BUS_PREFIX = {"virtio": "vd", "sata": "sd", "scsi": "sd", "usb": "sd", "ide": "hd"}
KNOWN_DEVICES = {"emulator", "disk", "interface", "graphics", "video", "input", "hostdev", "filesystem", "sound", "audio",
                 "controller", "channel", "serial", "console", "memballoon", "rng", "tpm", "watchdog"}


class SpecError(ValueError):
    pass


def new_mac() -> str:
    import random
    return "52:54:00:%02x:%02x:%02x" % (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))


def normalize(spec: dict) -> dict:
    """Fill defaults and validate. Raises SpecError with a message naming the field."""
    s = {**{k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}, **spec}
    name = str(s.get("name", "")).strip()
    if not NAME_RE.match(name):
        raise SpecError("Name: use letters, digits, space, '.', '_' or '-' (start with a letter or digit, 63 characters at most)")
    s["name"] = name
    s["uuid"] = s.get("uuid") or str(uuid.uuid4())
    if s["cpu_mode"] not in ("host-passthrough", "host-model"):
        raise SpecError("CPU mode must be host-passthrough or host-model")
    s["vcpus"] = int(s["vcpus"])
    if not 1 <= s["vcpus"] <= 512:
        raise SpecError("vCPUs: choose between 1 and 512")
    s["threads"] = int(s["threads"])
    if s["threads"] not in (1, 2) or s["vcpus"] % s["threads"]:
        raise SpecError("Threads per core must be 1, or 2 with an even number of vCPUs")
    s["pins"] = [int(x) for x in s["pins"]]
    if s["pins"] and len(s["pins"]) != s["vcpus"]:
        raise SpecError(f"Pinning: pick exactly {s['vcpus']} host CPUs (one per vCPU), or none")
    if len(set(s["pins"])) != len(s["pins"]):
        raise SpecError("Pinning: a host CPU is listed twice")
    s["memory_mb"] = int(s["memory_mb"])
    s["max_memory_mb"] = int(s["max_memory_mb"] or s["memory_mb"])
    if s["memory_mb"] < 64:
        raise SpecError("Memory: at least 64 MB")
    if s["max_memory_mb"] < s["memory_mb"]:
        raise SpecError("Max memory cannot be lower than the initial memory")
    if s["machine"] not in ("q35", "i440fx"):
        raise SpecError("Machine must be q35 or i440fx")
    if s["firmware"] not in OVMF:
        raise SpecError("BIOS must be seabios, ovmf or ovmf-secure")
    if s["firmware"] == "ovmf-secure" and s["machine"] != "q35":
        raise SpecError("Secure boot needs the Q35 machine type")
    if s["tpm"] and s["firmware"] == "seabios":
        raise SpecError("A TPM needs OVMF (UEFI) firmware")
    if s["clock"] not in ("utc", "localtime"):
        raise SpecError("Clock must be utc or localtime")
    if s["usb_controller"] not in ("qemu-xhci", "nec-xhci", "ich9-ehci1", "none"):
        raise SpecError("Unknown USB controller")
    if s["sound"] not in ("none", "ich9", "ac97", "ich6", "es1370", "sb16", "usb"):
        raise SpecError("Unknown sound card")
    s["description"] = str(s["description"])[:512]

    for i, d in enumerate(s["disks"], 1):
        d.setdefault("format", "qcow2"); d.setdefault("bus", "virtio"); d.setdefault("type", "file")
        d.setdefault("discard", True); d.setdefault("ssd", False); d.setdefault("serial", ""); d.setdefault("readonly", False)
        if not str(d.get("path", "")).startswith("/"):
            raise SpecError(f"vDisk {i}: the location must be a full path starting with /")
        if d["format"] not in ("qcow2", "raw"):
            raise SpecError(f"vDisk {i}: format must be qcow2 or raw")
        if d["bus"] not in BUS_PREFIX:
            raise SpecError(f"vDisk {i}: unknown bus {d['bus']!r}")
        if d["type"] not in ("file", "block"):
            raise SpecError(f"vDisk {i}: type must be file or block")
        if d["type"] == "block":
            d["format"] = "raw"
        if d.get("serial") and not re.match(r"^[A-Za-z0-9._-]{1,20}$", d["serial"]):
            raise SpecError(f"vDisk {i}: the serial can only contain letters, digits, '.', '_' and '-' (20 characters at most)")
    seen_paths: dict[str, int] = {}
    for i, d in enumerate(s["disks"], 1):
        if d["path"] in seen_paths:
            raise SpecError(f"vDisk {i}: {d['path']} is already vDisk {seen_paths[d['path']]} of this VM")
        seen_paths[d["path"]] = i
    for i, c in enumerate(s["cdroms"], 1):
        c.setdefault("bus", "sata" if s["machine"] == "q35" else "ide"); c.setdefault("path", "")
        if c["path"] and not c["path"].startswith("/"):
            raise SpecError(f"CD {i}: the image must be a full path")
        if c["bus"] not in ("sata", "ide", "scsi", "usb"):
            raise SpecError(f"CD {i}: unknown bus")
    for i, n in enumerate(s["nics"], 1):
        n.setdefault("type", "network"); n.setdefault("model", "virtio")
        if n["type"] not in ("network", "bridge"):
            raise SpecError(f"Network {i}: type must be network or bridge")
        if not re.match(r"^[A-Za-z0-9._:-]{1,32}$", str(n.get("source", ""))):
            raise SpecError(f"Network {i}: choose a network source")
        if n["model"] not in ("virtio", "e1000", "e1000e", "rtl8139", "vmxnet3"):
            raise SpecError(f"Network {i}: unknown model")
        n["mac"] = n.get("mac") or new_mac()
        if not MAC_RE.match(n["mac"]):
            raise SpecError(f"Network {i}: that is not a MAC address")
        if int(n["mac"].split(":")[0], 16) & 1:
            raise SpecError(f"Network {i}: the MAC must be a unicast address (first octet even)")
    for i, g in enumerate(s["graphics"], 1):
        g.setdefault("type", "vnc"); g.setdefault("port", -1); g.setdefault("listen", "0.0.0.0")
        g.setdefault("password", ""); g.setdefault("keymap", "en-us"); g.setdefault("video", "virtio"); g.setdefault("copypaste", True)
        if g["type"] not in ("vnc", "spice"):
            raise SpecError(f"Graphics {i}: type must be vnc or spice")
        if g["video"] not in ("qxl", "virtio", "vga", "cirrus", "none"):
            raise SpecError(f"Graphics {i}: unknown video model")
        port = int(g["port"])
        if port != -1 and not 5900 <= port <= 65535:
            raise SpecError(f"Graphics {i}: port must be automatic or 5900-65535")
        g["port"] = port
        if not re.match(r"^[0-9.:a-fA-F]+$", str(g["listen"])):
            raise SpecError(f"Graphics {i}: bad listen address")
        if len(g["password"]) > 8 and g["type"] == "vnc":
            raise SpecError(f"Graphics {i}: a VNC password is at most 8 characters")
    for i, p in enumerate(s["pci"], 1):
        if not PCI_RE.match(str(p.get("address", ""))):
            raise SpecError(f"PCI device {i}: address must look like 0000:01:00.0")
        p.setdefault("rom", "")
        if p["rom"] and not p["rom"].startswith("/"):
            raise SpecError(f"PCI device {i}: the ROM file must be a full path")
    for i, u in enumerate(s["usb"], 1):
        if not (HEX4.match(str(u.get("vendor", ""))) and HEX4.match(str(u.get("product", "")))):
            raise SpecError(f"USB device {i}: vendor and product must be 4 hex digits")
    tags = set()
    for i, f in enumerate(s["shares"], 1):
        f.setdefault("mode", "virtiofs"); f.setdefault("readonly", False)
        if f["mode"] not in ("virtiofs", "9p"):
            raise SpecError(f"Share {i}: mode must be virtiofs or 9p")
        if not str(f.get("source", "")).startswith("/") or ".." in str(f["source"]).split("/"):
            raise SpecError(f"Share {i}: the source must be a full path with no '..'")
        if not re.match(r"^[A-Za-z0-9._-]{1,36}$", str(f.get("target", ""))):
            raise SpecError(f"Share {i}: the mount tag can only contain letters, digits, '.', '_' and '-'")
        if f["target"] in tags:
            raise SpecError(f"Share {i}: the mount tag {f['target']!r} is used twice")
        tags.add(f["target"])
    for a in s["qemu_args"]:
        if not isinstance(a, str) or "\n" in a:
            raise SpecError("QEMU arguments: one argument per entry, no line breaks")
    return s


# ── spec -> XML ──────────────────────────────────────────────────────────────

def _sub(parent, tag, attrib=None, text=None):
    el = ET.SubElement(parent, tag, {k: str(v) for k, v in (attrib or {}).items() if v is not None})
    if text is not None:
        el.text = str(text)
    return el


def build_xml(spec: dict, host: dict | None = None) -> str:
    """host: {"kvm": bool, "amd": bool, "ovmf": {"ovmf": (code, vars), ...}, "emulator": path, "machines": {"q35": "...", "i440fx": "..."}}"""
    s = normalize(spec)
    host = host or {}
    ovmf = {**OVMF, **(host.get("ovmf") or {})}
    root = ET.Element("domain", {"type": "kvm" if host.get("kvm", True) else "qemu"})
    ET.SubElement(root, "name").text = s["name"]
    ET.SubElement(root, "uuid").text = s["uuid"]
    if s["description"]:
        ET.SubElement(root, "description").text = s["description"]
    meta = ET.SubElement(ET.SubElement(root, "metadata"), f"{{{HELIX_NS}}}vm")
    ET.SubElement(meta, f"{{{HELIX_NS}}}template").text = s["template"]
    if s["webui"]:
        ET.SubElement(meta, f"{{{HELIX_NS}}}webui").text = s["webui"]

    _sub(root, "memory", {"unit": "MiB"}, s["max_memory_mb"])
    _sub(root, "currentMemory", {"unit": "MiB"}, s["memory_mb"])
    if any(f["mode"] == "virtiofs" for f in s["shares"]):
        mb = _sub(root, "memoryBacking")
        _sub(mb, "source", {"type": "memfd"})
        _sub(mb, "access", {"mode": "shared"})
    _sub(root, "vcpu", {"placement": "static"}, s["vcpus"])
    if s["pins"]:
        ct = _sub(root, "cputune")
        for i, cpu in enumerate(s["pins"]):
            _sub(ct, "vcpupin", {"vcpu": i, "cpuset": cpu})

    os_el = _sub(root, "os")
    machine = (host.get("machines") or {}).get(s["machine"]) or ("q35" if s["machine"] == "q35" else "pc")
    _sub(os_el, "type", {"arch": "x86_64", "machine": machine}, "hvm")
    fw = ovmf[s["firmware"]]
    if fw:
        code, vars_t = fw
        _sub(os_el, "loader", {"readonly": "yes", "type": "pflash", **({"secure": "yes"} if s["firmware"] == "ovmf-secure" else {})}, code)
        _sub(os_el, "nvram", {"template": vars_t}, f"{NVRAM_DIR}/{s['uuid']}_VARS.fd")
    _sub(os_el, "bootmenu", {"enable": "yes", "timeout": "3000"})

    feat = _sub(root, "features")
    _sub(feat, "acpi"); _sub(feat, "apic")
    if s["hyperv"]:
        hv = _sub(feat, "hyperv", {"mode": "custom"})
        for t in ("relaxed", "vapic"):
            _sub(hv, t, {"state": "on"})
        _sub(hv, "spinlocks", {"state": "on", "retries": "8191"})
        for t in ("vpindex", "synic", "stimer", "reset"):
            _sub(hv, t, {"state": "on"} if t != "reset" else {"state": "on"})
        _sub(hv, "frequencies", {"state": "on"})
    if s["pci"]:
        _sub(_sub(feat, "kvm"), "hidden", {"state": "on"})     # some GPU drivers refuse to run when they detect KVM
    if s["firmware"] == "ovmf-secure":
        _sub(feat, "smm", {"state": "on"})

    cpu = _sub(root, "cpu", {"mode": s["cpu_mode"], **({"check": "none", "migratable": "on" if s["migratable"] else "off"} if s["cpu_mode"] == "host-passthrough" else {})})
    _sub(cpu, "topology", {"sockets": 1, "dies": 1, "cores": s["vcpus"] // s["threads"], "threads": s["threads"]})
    if s["cpu_mode"] == "host-passthrough":
        _sub(cpu, "cache", {"mode": "passthrough"})
    if s["threads"] == 2 and host.get("amd"):
        _sub(cpu, "feature", {"policy": "require", "name": "topoext"})

    clock = _sub(root, "clock", {"offset": s["clock"]})
    _sub(clock, "timer", {"name": "rtc", "tickpolicy": "catchup"})
    _sub(clock, "timer", {"name": "pit", "tickpolicy": "delay"})
    _sub(clock, "timer", {"name": "hpet", "present": "no"})
    if s["hyperv"]:
        _sub(clock, "timer", {"name": "hypervclock", "present": "yes"})
    _sub(root, "on_poweroff", text="destroy"); _sub(root, "on_reboot", text="restart"); _sub(root, "on_crash", text="restart")

    dev = _sub(root, "devices")
    _sub(dev, "emulator", text=host.get("emulator") or "/usr/bin/qemu-system-x86_64")

    letters: dict[str, int] = {}

    def devname(bus: str) -> str:
        p = BUS_PREFIX[bus]
        n = letters.get(p, 0)
        letters[p] = n + 1
        return p + (chr(ord("a") + n) if n < 26 else chr(ord("a") + n // 26 - 1) + chr(ord("a") + n % 26))

    if any(d["bus"] == "scsi" for d in s["disks"]) or any(c["bus"] == "scsi" for c in s["cdroms"]):
        _sub(dev, "controller", {"type": "scsi", "model": "virtio-scsi"})
    if s["usb_controller"] != "none":
        if s["usb_controller"] == "ich9-ehci1":
            _sub(dev, "controller", {"type": "usb", "model": "ich9-ehci1"})
        else:
            _sub(dev, "controller", {"type": "usb", "model": s["usb_controller"]})

    for d in s["disks"]:
        block = d["type"] == "block"
        el = _sub(dev, "disk", {"type": d["type"], "device": "disk"})
        drv = {"name": "qemu", "type": d["format"], "cache": "none" if block else "writeback"}
        if d["discard"]:
            drv["discard"] = "unmap"
        _sub(el, "driver", drv)
        _sub(el, "source", {"dev" if block else "file": d["path"]})
        tgt = {"dev": devname(d["bus"]), "bus": d["bus"]}
        if d["ssd"] and d["bus"] in ("sata", "scsi", "usb", "ide"):
            tgt["rotation_rate"] = "1"
        _sub(el, "target", tgt)
        if d["serial"]:
            _sub(el, "serial", text=d["serial"])
        if d["readonly"]:
            _sub(el, "readonly")
        if d.get("boot"):
            _sub(el, "boot", {"order": int(d["boot"])})
    for c in s["cdroms"]:
        el = _sub(dev, "disk", {"type": "file", "device": "cdrom"})
        _sub(el, "driver", {"name": "qemu", "type": "raw"})
        if c["path"]:
            _sub(el, "source", {"file": c["path"]})
        _sub(el, "target", {"dev": devname(c["bus"]), "bus": c["bus"]})
        _sub(el, "readonly")
        if c.get("boot"):
            _sub(el, "boot", {"order": int(c["boot"])})
    for n in s["nics"]:
        el = _sub(dev, "interface", {"type": n["type"]})
        _sub(el, "mac", {"address": n["mac"]})
        _sub(el, "source", {n["type"]: n["source"]})
        _sub(el, "model", {"type": n["model"]})
    for f in s["shares"]:
        el = _sub(dev, "filesystem", {"type": "mount", "accessmode": "passthrough"})
        _sub(el, "driver", {"type": "virtiofs"} if f["mode"] == "virtiofs" else {"type": "path", "wrpolicy": "immediate"})
        _sub(el, "source", {"dir": f["source"]})
        _sub(el, "target", {"dir": f["target"]})
        if f["readonly"]:
            _sub(el, "readonly")
    for p in s["pci"]:
        dom, bus, rest = p["address"].split(":")
        slot, func = rest.split(".")
        el = _sub(dev, "hostdev", {"mode": "subsystem", "type": "pci", "managed": "yes"})
        _sub(_sub(el, "source"), "address", {"domain": "0x" + dom, "bus": "0x" + bus, "slot": "0x" + slot, "function": "0x" + func})
        if p["rom"]:
            _sub(el, "rom", {"file": p["rom"]})
    for u in s["usb"]:
        el = _sub(dev, "hostdev", {"mode": "subsystem", "type": "usb", "managed": "yes"})
        src = _sub(el, "source", {"startupPolicy": "optional"})
        _sub(src, "vendor", {"id": "0x" + u["vendor"].lower()}); _sub(src, "product", {"id": "0x" + u["product"].lower()})

    has_spice = any(g["type"] == "spice" for g in s["graphics"])
    for g in s["graphics"]:
        gx = _sub(dev, "graphics", {"type": g["type"], "port": g["port"] if g["port"] != -1 else "-1",
                                    "autoport": "yes" if g["port"] == -1 else "no", "listen": g["listen"]})
        if g["type"] == "vnc":
            gx.set("keymap", g["keymap"])
        if g["password"]:
            gx.set("passwd", g["password"])
        _sub(gx, "listen", {"type": "address", "address": g["listen"]})
        if g["type"] == "spice":
            _sub(gx, "image", {"compression": "off"})
            _sub(gx, "clipboard", {"copypaste": "yes" if g["copypaste"] else "no"})
        vm_ = g["video"]
        if vm_ == "none":
            _sub(_sub(dev, "video"), "model", {"type": "none"})
        elif vm_ == "qxl":
            _sub(_sub(dev, "video"), "model", {"type": "qxl", "ram": 65536, "vram": 65536, "vgamem": 16384, "heads": 1, "primary": "yes"})
        elif vm_ == "vga":
            _sub(_sub(dev, "video"), "model", {"type": "vga", "vram": 16384, "heads": 1, "primary": "yes"})
        else:
            _sub(_sub(dev, "video"), "model", {"type": vm_, "heads": 1, "primary": "yes"})
    if not s["graphics"] and not s["pci"]:
        _sub(_sub(dev, "video"), "model", {"type": "none"})
    if s["input_tablet"] and s["usb_controller"] != "none":
        _sub(dev, "input", {"type": "tablet", "bus": "usb"})
    _sub(dev, "input", {"type": "mouse", "bus": "ps2"}); _sub(dev, "input", {"type": "keyboard", "bus": "ps2"})
    if s["sound"] != "none":
        snd = _sub(dev, "sound", {"model": s["sound"]})
        _sub(snd, "audio", {"id": 1})
        _sub(dev, "audio", {"id": 1, "type": "none"})
    if s["agent"]:
        ch = _sub(dev, "channel", {"type": "unix"})
        _sub(ch, "source", {"mode": "bind"})
        _sub(ch, "target", {"type": "virtio", "name": "org.qemu.guest_agent.0"})
    if has_spice:
        _sub(_sub(dev, "channel", {"type": "spicevmc"}), "target", {"type": "virtio", "name": "com.redhat.spice.0"})
    if s["tpm"]:
        _sub(_sub(dev, "tpm", {"model": "tpm-crb"}), "backend", {"type": "emulator", "version": "2.0"})
    _sub(dev, "memballoon", {"model": "virtio"})
    _sub(_sub(dev, "rng", {"model": "virtio"}), "backend", {"model": "random"}, "/dev/urandom")

    if s["qemu_args"]:
        cl = ET.SubElement(root, f"{{{QEMU_NS}}}commandline")
        for a in s["qemu_args"]:
            ET.SubElement(cl, f"{{{QEMU_NS}}}arg", {"value": a})
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode")


# ── XML -> spec ──────────────────────────────────────────────────────────────

def _i(v, default=0):
    try:
        return int(str(v), 0)
    except (TypeError, ValueError):
        return default


def parse_xml(xml: str) -> dict:
    """The spec a domain's XML maps to, plus "unsupported": names of things the form cannot express."""
    root = ET.fromstring(xml)
    unsupported: list[str] = []
    spec: dict = {"name": (root.findtext("name") or "").strip(), "uuid": (root.findtext("uuid") or "").strip(),
                  "description": root.findtext("description") or ""}
    hm = root.find(f"metadata/{{{HELIX_NS}}}vm")
    spec["template"] = (hm.findtext(f"{{{HELIX_NS}}}template") if hm is not None else None) or "other"
    spec["webui"] = (hm.findtext(f"{{{HELIX_NS}}}webui") if hm is not None else None) or ""

    mem = root.find("memory"); cur = root.find("currentMemory")

    def mib(el):
        if el is None:
            return 0
        v = _i(el.text); u = el.get("unit", "KiB")
        return {"KiB": v // 1024, "MiB": v, "GiB": v * 1024, "B": v // 1048576}.get(u, v // 1024)
    spec["max_memory_mb"] = mib(mem); spec["memory_mb"] = mib(cur) or mib(mem)
    spec["vcpus"] = _i(root.findtext("vcpu"), 1)
    pins = {}
    for p in root.findall("cputune/vcpupin"):
        pins[_i(p.get("vcpu"))] = _i(p.get("cpuset"), -1)
    spec["pins"] = [pins[k] for k in sorted(pins)] if len(pins) == spec["vcpus"] and all(v >= 0 for v in pins.values()) else []
    if pins and not spec["pins"]:
        unsupported.append("CPU pinning with ranges")

    cpu = root.find("cpu")
    spec["cpu_mode"] = (cpu.get("mode") if cpu is not None else None) or "host-passthrough"
    if spec["cpu_mode"] not in ("host-passthrough", "host-model"):
        unsupported.append(f"CPU mode {spec['cpu_mode']}"); spec["cpu_mode"] = "host-model"
    spec["migratable"] = cpu is not None and cpu.get("migratable") == "on"
    top = cpu.find("topology") if cpu is not None else None
    spec["threads"] = _i(top.get("threads"), 1) if top is not None else 1
    if spec["threads"] not in (1, 2):
        unsupported.append("more than 2 threads per core"); spec["threads"] = 1

    ot = root.find("os/type")
    m = (ot.get("machine") if ot is not None else "") or ""
    spec["machine"] = "q35" if "q35" in m else "i440fx"
    loader = root.find("os/loader")
    if loader is None:
        spec["firmware"] = "seabios"
    else:
        spec["firmware"] = "ovmf-secure" if (loader.get("secure") == "yes" or "secboot" in (loader.text or "")) else "ovmf"
    spec["tpm"] = root.find("devices/tpm") is not None
    spec["hyperv"] = root.find("features/hyperv") is not None
    spec["clock"] = "localtime" if (root.find("clock") is not None and root.find("clock").get("offset") == "localtime") else "utc"

    dev = root.find("devices")
    spec.update({"disks": [], "cdroms": [], "nics": [], "graphics": [], "pci": [], "usb": [], "shares": []})
    spec["usb_controller"] = "none"; spec["input_tablet"] = False; spec["agent"] = False; spec["sound"] = "none"
    video_for_graphics = None
    if dev is not None:
        for c in dev.findall("controller"):
            if c.get("type") == "usb":
                spec["usb_controller"] = c.get("model") if c.get("model") in ("qemu-xhci", "nec-xhci", "ich9-ehci1") else spec["usb_controller"]
        for d in dev.findall("disk"):
            src = d.find("source"); tgt = d.find("target"); drv = d.find("driver")
            boot = d.find("boot")
            path = (src.get("file") or src.get("dev") or "") if src is not None else ""
            if d.get("device") == "cdrom":
                spec["cdroms"].append({"path": path, "bus": tgt.get("bus") if tgt is not None else "sata", "boot": _i(boot.get("order")) if boot is not None else 0})
            elif d.get("device") == "disk":
                if d.get("type") not in ("file", "block"):
                    unsupported.append(f"disk of type {d.get('type')}"); continue
                spec["disks"].append({
                    "path": path, "type": d.get("type"), "format": (drv.get("type") if drv is not None else "raw") or "raw",
                    "bus": tgt.get("bus") if tgt is not None else "virtio", "discard": drv is not None and drv.get("discard") == "unmap",
                    "ssd": tgt is not None and tgt.get("rotation_rate") == "1", "serial": d.findtext("serial") or "",
                    "readonly": d.find("readonly") is not None, "boot": _i(boot.get("order")) if boot is not None else 0,
                    "dev": tgt.get("dev") if tgt is not None else ""})
            else:
                unsupported.append(f"{d.get('device')} device")
        for n in dev.findall("interface"):
            if n.get("type") not in ("network", "bridge"):
                unsupported.append(f"network of type {n.get('type')}"); continue
            src = n.find("source"); mdl = n.find("model")
            spec["nics"].append({"type": n.get("type"), "source": (src.get(n.get("type")) if src is not None else "") or "",
                                 "model": (mdl.get("type") if mdl is not None else "virtio") or "virtio",
                                 "mac": (n.find("mac").get("address") if n.find("mac") is not None else "")})
        for g in dev.findall("graphics"):
            if g.get("type") not in ("vnc", "spice"):
                unsupported.append(f"graphics {g.get('type')}"); continue
            lst = g.find("listen")
            spec["graphics"].append({"type": g.get("type"), "port": _i(g.get("port"), -1) if g.get("autoport") != "yes" else -1,
                                     "listen": g.get("listen") or (lst.get("address") if lst is not None else "0.0.0.0") or "0.0.0.0",
                                     "password": g.get("passwd", ""), "keymap": g.get("keymap", "en-us"),
                                     "copypaste": (g.find("clipboard").get("copypaste") != "no") if g.find("clipboard") is not None else True,
                                     "video": "virtio"})
        vids = dev.findall("video/model")
        if vids:
            vt = vids[0].get("type", "virtio")
            video_for_graphics = vt if vt in ("qxl", "virtio", "vga", "cirrus", "none") else "virtio"
            if vt not in ("qxl", "virtio", "vga", "cirrus", "none"):
                unsupported.append(f"video model {vt}")
            for g in spec["graphics"]:
                g["video"] = video_for_graphics
        for h in dev.findall("hostdev"):
            src = h.find("source")
            if h.get("type") == "pci" and src is not None and src.find("address") is not None:
                a = src.find("address")
                spec["pci"].append({"address": "%s:%s:%s.%s" % tuple(x[2:].zfill(w) for x, w in ((a.get("domain", "0x0"), 4), (a.get("bus", "0x0"), 2), (a.get("slot", "0x0"), 2), (a.get("function", "0x0"), 1))),
                                    "rom": (h.find("rom").get("file") if h.find("rom") is not None and h.find("rom").get("file") else "")})
            elif h.get("type") == "usb" and src is not None and src.find("vendor") is not None and src.find("product") is not None:
                spec["usb"].append({"vendor": src.find("vendor").get("id", "0x")[2:].zfill(4), "product": src.find("product").get("id", "0x")[2:].zfill(4)})
            else:
                unsupported.append(f"host device of type {h.get('type')}")
        for f in dev.findall("filesystem"):
            drv = f.find("driver"); src = f.find("source"); tgt = f.find("target")
            if src is None or src.get("dir") is None or tgt is None:
                unsupported.append("filesystem of another kind"); continue
            spec["shares"].append({"mode": "virtiofs" if drv is not None and drv.get("type") == "virtiofs" else "9p",
                                   "source": src.get("dir"), "target": tgt.get("dir"), "readonly": f.find("readonly") is not None})
        for i in dev.findall("input"):
            if i.get("type") == "tablet":
                spec["input_tablet"] = True
        snd = dev.find("sound")
        if snd is not None:
            spec["sound"] = snd.get("model") if snd.get("model") in ("ich9", "ac97", "ich6", "es1370", "sb16", "usb") else "ich9"
        for ch in dev.findall("channel"):
            t = ch.find("target")
            if t is not None and t.get("name") == "org.qemu.guest_agent.0":
                spec["agent"] = True
        for el in dev:
            if el.tag not in KNOWN_DEVICES:
                unsupported.append(el.tag)
    if root.find(f"{{{QEMU_NS}}}commandline") is not None:
        spec["qemu_args"] = [a.get("value", "") for a in root.findall(f"{{{QEMU_NS}}}commandline/{{{QEMU_NS}}}arg")]
    else:
        spec["qemu_args"] = []
    for tag in ("memoryBacking", "numatune", "iothreads", "blkiotune", "memtune", "resource", "sysinfo", "seclabel", "pm", "devices/redirdev", "devices/smartcard"):
        if root.find(tag) is not None and tag != "memoryBacking":
            unsupported.append(tag)
    spec["unsupported"] = sorted(set(unsupported))
    return spec
