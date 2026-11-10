"""End-to-end test of the VM API against a REAL Helix machine with VM Manager running (nested KVM is fine).

    webui/.venv/bin/python webui/tests/vm_api_test.py [host]

It needs a tiny bootable disk image at /mnt/user/isos/cirros.qcow2 on that machine
(https://download.cirros-cloud.net/0.6.2/cirros-0.6.2-x86_64-disk.img, 21 MB) and creates / deletes VMs named vt-*.
"""
import asyncio, json, os, subprocess, sys, time, urllib.error, urllib.request

HOST = sys.argv[1] if len(sys.argv) > 1 else "192.168.50.12"
B = f"http://{HOST}/api/vms"
ok = bad = 0
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "build", "tools"))
import vmsh


def call(m, p, body=None, timeout=120):
    r = urllib.request.Request(B + p, data=json.dumps(body).encode() if body is not None else None, method=m)
    if body is not None:
        r.add_header("Content-Type", "application/json")
    try:
        x = urllib.request.urlopen(r, timeout=timeout)
        t = x.read()
        return x.status, (json.loads(t) if t[:1] in b"{[" else t)
    except urllib.error.HTTPError as e:
        t = e.read()
        try:
            return e.code, json.loads(t)
        except Exception:
            return e.code, t


def T(n, c, x=""):
    global ok, bad
    if c:
        ok += 1
    else:
        bad += 1
        print("FAIL", n, "->", str(x)[:400])


def sh(cmd, timeout=60):
    for attempt in range(4):                      # the terminal drops while the backend restarts after a dev push
        try:
            return vmsh.run(HOST, cmd, timeout)[1]
        except Exception:
            time.sleep(4)
    raise RuntimeError("terminal unavailable")


def wait_state(name, want, secs=60):
    t = time.time()
    while time.time() - t < secs:
        c, r = call("GET", f"/{name}")
        if c == 200 and r["state"] == want:
            return True
        time.sleep(2)
    return False


def vm(name):
    c, r = call("GET", "/")
    return next((v for v in r["vms"] if v["name"] == name), None)


# ---- clean slate and prerequisites
for n in ("vt-c1", "vt-c2", "vt-clone", "vt-fail"):
    if call("GET", f"/{n}")[0] == 200:
        call("POST", f"/{n}/force-stop"); call("DELETE", f"/{n}?remove_disks=true")
sh("sudo -n rm -rf /mnt/user/domains/vt-*; ls /mnt/user/isos/cirros.qcow2 >/dev/null && mkdir -p /mnt/user/domains/vt-c1 && cp /mnt/user/isos/cirros.qcow2 /mnt/user/domains/vt-c1/vdisk1.qcow2 && echo ready")
c, r = call("GET", "/")
T("VM Manager is running", c == 200 and r["running"] and r["kvm"], r)

# ---- host info for the form
c, h = call("GET", "/host")
T("host: cpu topology, memory, networks, ovmf", c == 200 and h["cpu"]["threads"] >= 1 and h["memory_mb"] > 1000 and any(n["source"] == "default" for n in h["networks"]) and h["ovmf"].get("ovmf"), h if c != 200 else {k: h[k] for k in ("ovmf", "networks")})
T("host: pci devices carry class and iommu group fields", all(k in h["pci"][0] for k in ("address", "name", "iommu_group", "bound_to_vfio")) if h["pci"] else True)
c, t = call("GET", "/templates")
T("templates: windows 11, linux, custom present", {"windows11", "ubuntu", "custom"} <= {x["id"] for x in t["templates"]}, [x["id"] for x in t["templates"]])
T("templates: windows 11 asks for TPM + secure boot", next(x for x in t["templates"] if x["id"] == "windows11")["defaults"]["tpm"] is True)

# ---- validation (nothing may be created by a bad request)
spec = lambda **k: {"name": "vt-fail", "vcpus": 1, "memory_mb": 128, "disks": [{"path": "/mnt/user/domains/vt-fail/d.qcow2", "size_gb": 1}], "nics": [{"source": "default"}], "graphics": [{"type": "vnc", "video": "vga"}], "agent": False, **k}
for label, body, frag in [
    ("bad name", spec(name="a/b"), "Name"), ("disk outside /mnt", spec(disks=[{"path": "/etc/evil.qcow2", "size_gb": 1}]), "/mnt"),
    ("disk with ..", spec(disks=[{"path": "/mnt/user/../etc/evil", "size_gb": 1}]), "'..'"), ("new disk without a size", spec(disks=[{"path": "/mnt/user/domains/vt-fail/n.qcow2"}]), "size"),
    ("share outside /mnt", spec(shares=[{"source": "/etc", "target": "x"}]), "/mnt"), ("missing pci device", spec(pci=[{"address": "0000:ff:1f.7"}]), "not found"),
    ("tpm on seabios", spec(tpm=True), "TPM"), ("vcpus 0", spec(vcpus=0), "vCPUs")]:
    c, r = call("POST", "/", {"spec": body})
    T(f"create rejects: {label}", c in (400, 409) and frag.lower() in json.dumps(r).lower(), (c, r))
T("no VM or disk was created by the bad requests", call("GET", "/vt-fail")[0] == 404 and "evil" not in sh("ls /mnt/user/domains /etc | grep -c evil || true"))
# symlink escape: a link inside /mnt pointing outside must not let root-run qemu-img write there
sh("mkdir -p /mnt/user/domains/vt-link && ln -sfn /tmp /mnt/user/domains/vt-link/out")
c, r = call("POST", "/", {"spec": spec(disks=[{"path": "/mnt/user/domains/vt-link/out/evil-from-vm.qcow2", "size_gb": 1}])})
T("symlink pointing out of /mnt is refused", c == 400 and "evil-from-vm" not in sh("ls /tmp | grep evil-from-vm || true"), (c, r))
sh("rm -rf /mnt/user/domains/vt-link")

# ---- create with an existing disk, start, console
body = {"name": "vt-c1", "template": "custom", "description": "api test", "vcpus": 1, "memory_mb": 192, "machine": "q35", "firmware": "seabios", "agent": False,
        "disks": [{"path": "/mnt/user/domains/vt-c1/vdisk1.qcow2", "boot": 1}], "nics": [{"source": "default"}], "graphics": [{"type": "vnc", "video": "vga"}]}
c, r = call("POST", "/", {"spec": body, "start": True, "autostart": True})
T("create + start + autostart", c == 200 and r["ok"] and r["started"], (c, r))
T("running", wait_state("vt-c1", "running", 30))
c, d = call("GET", "/vt-c1")
T("detail: spec round-trips, disk sizes known, autostart on", c == 200 and d["spec"]["vcpus"] == 1 and d["disks_detail"][0]["capacity"] > 100 * 2 ** 20 and d["autostart"] and d["description"] == "api test", d if c != 200 else d["disks_detail"])
T("list shows graphics port and disk summary", (vm("vt-c1") or {}).get("graphics", [{}])[0].get("port", 0) >= 5900 and vm("vt-c1")["disks"] == 1)
c, r = call("POST", "/vt-c1/start")
T("starting a running VM is refused", c == 409, (c, r))
ip = None
for _ in range(40):
    time.sleep(3)
    v = vm("vt-c1")
    if v and v["ips"]:
        ip = v["ips"][0]["ip"]; break
T("guest got an IP address from the libvirt network", bool(ip), vm("vt-c1"))
c, r = call("GET", "/vt-c1/console")
T("console info", c == 200 and r["graphics"][0]["type"] == "vnc", (c, r))
import websockets
async def vnc():
    async with websockets.connect(f"ws://{HOST}/api/vms/vt-c1/console/ws", subprotocols=["binary"], open_timeout=10) as ws:
        banner = await asyncio.wait_for(ws.recv(), 10)
        return bytes(banner)[:12], ws.subprotocol
try:
    banner, sub = asyncio.run(vnc())
    T("browser console proxy relays the VNC handshake (RFB banner)", banner.startswith(b"RFB 003."), banner)
    T("console proxy negotiates the 'binary' subprotocol noVNC asks for", sub == "binary", sub)
except Exception as e:
    T("browser console proxy relays the VNC handshake (RFB banner)", False, e)
T("noVNC client is served", urllib.request.urlopen(f"http://{HOST}/novnc/vnc.html", timeout=10).status == 200)
try:
    async def refused():
        async with websockets.connect(f"ws://{HOST}/api/vms/nosuchvm/console/ws", open_timeout=10) as ws:
            await ws.recv()
    asyncio.run(refused()); T("console for an unknown VM is refused", False)
except Exception:
    T("console for an unknown VM is refused", True)

# ---- pause / resume / stats / reboot
c, r = call("POST", "/vt-c1/pause"); T("pause", c == 200 and wait_state("vt-c1", "paused", 10), (c, r))
c, r = call("POST", "/vt-c1/resume"); T("resume", c == 200 and wait_state("vt-c1", "running", 10), (c, r))
c, r = call("GET", "/stats")
call("GET", "/stats"); time.sleep(2); c, r = call("GET", "/stats")
T("stats: cpu% and memory for the running VM", c == 200 and "vt-c1" in r["stats"] and r["stats"]["vt-c1"]["mem_max_mb"] >= 150, r)
c, r = call("POST", "/vt-c1/reboot"); T("reboot request accepted", c == 200, (c, r))

# ---- snapshots (BIOS VM, internal qcow2)
c, r = call("POST", "/vt-c1/snapshots", {"name": "s1", "description": "first"})
T("snapshot create (running)", c == 200 and r["name"] == "s1", (c, r))
c, r = call("POST", "/vt-c1/snapshots", {"name": "bad name!"}); T("snapshot name validated", c == 400, (c, r))
c, r = call("GET", "/vt-c1/snapshots"); T("snapshot list", c == 200 and [s["name"] for s in r["snapshots"]] == ["s1"], r)
T("list shows snapshot count", vm("vt-c1")["snapshots"] == 1)

# ---- hibernate and resume from it
c, r = call("POST", "/vt-c1/hibernate", timeout=120)
T("hibernate", c == 200 and wait_state("vt-c1", "hibernated", 30), (c, r))
c, r = call("POST", "/vt-c1/start", timeout=120)
T("starting a hibernated VM resumes it", c == 200 and wait_state("vt-c1", "running", 30), (c, r))

# ---- stop (ACPI, then force if the guest ignores it)
call("POST", "/vt-c1/stop")
stopped = wait_state("vt-c1", "shut off", 45)
if not stopped:
    call("POST", "/vt-c1/force-stop"); stopped = wait_state("vt-c1", "shut off", 15)
T("VM stops", stopped)
c, r = call("POST", "/vt-c1/stop"); T("stopping a stopped VM is refused", c == 409, (c, r))

# ---- edit (shut off only), rename, add a drive
c, r = call("PUT", "/vt-c1", {"spec": {**body, "memory_mb": 256, "vcpus": 2, "disks": [{"path": "/mnt/user/domains/vt-c1/vdisk1.qcow2", "boot": 1}, {"path": "/mnt/user/domains/vt-c1/vdisk2.qcow2", "size_gb": 1, "bus": "virtio"}],
                         "cdroms": [{"path": "/mnt/user/isos/cirros.qcow2", "bus": "sata", "boot": 2}]}})
T("edit: memory, vcpus, a new vDisk, a CD", c == 200, (c, r))
c, d = call("GET", "/vt-c1")
T("edit persisted and the new disk exists", d["memory_mb"] == 256 and d["vcpus"] == 2 and len(d["disks_detail"]) == 2 and d["disks_detail"][1]["exists"] and d["cdroms"] == 1, d if c != 200 else d["disks_detail"])
c, r = call("PUT", "/vt-c1", {"spec": {**body, "disks": [{"path": "/mnt/user/domains/vt-c1/vdisk1.qcow2"}, {"path": "/mnt/user/domains/vt-c1/vdisk1.qcow2"}]}})
T("the same vDisk twice is refused", c in (400, 409), (c, r))

# ---- CD change media, resize, xml editor
c, r = call("POST", "/vt-c1/cdrom", {"dev": "sda", "path": ""}); T("eject CD", c == 200, (c, r))
c, r = call("POST", "/vt-c1/cdrom", {"dev": "sda", "path": "/mnt/user/isos/cirros.qcow2"}); T("insert CD", c == 200, (c, r))
c, r = call("POST", "/vt-c1/cdrom", {"dev": "sda", "path": "/etc/passwd"}); T("insert CD from outside /mnt refused", c == 400, (c, r))
c, r = call("POST", "/vt-c1/disks/resize", {"path": "/mnt/user/domains/vt-c1/vdisk2.qcow2", "size_gb": 1}); T("resize: cannot shrink or keep the size", c == 400, (c, r))
c, r = call("POST", "/vt-c1/disks/resize", {"path": "/mnt/user/domains/vt-c1/vdisk2.qcow2", "size_gb": 2}); T("resize: grow", c == 200, (c, r))
c, d = call("GET", "/vt-c1"); T("resize visible in detail", d["disks_detail"][1]["capacity"] == 2 * 2 ** 30, d["disks_detail"][1])
c, r = call("POST", "/vt-c1/disks/resize", {"path": "/etc/passwd", "size_gb": 9}); T("resize only for this VM's own disks", c == 404, (c, r))
c, x = call("GET", "/vt-c1/xml"); T("xml editor: read", c == 200 and "<domain" in x["xml"], c)
c, r = call("PUT", "/vt-c1/xml", {"xml": x["xml"].replace("</domain>", "<bogus/></domain>")}); T("xml editor: libvirt rejects an invalid definition", c == 400, (c, r))
c, r = call("PUT", "/vt-c1/xml", {"xml": "<domain><name>other</name></domain>"}); T("xml editor: cannot smuggle in another VM", c == 400, (c, r))
c, r = call("PUT", "/vt-c1/xml", {"xml": x["xml"]}); T("xml editor: save unchanged", c == 200, (c, r))

# ---- usb hot-plug error paths (no USB device is attached to this test machine)
c, r = call("POST", "/vt-c1/usb/attach", {"vendor": "046d", "product": "c52b"}); T("usb hot-plug needs a running VM", c == 409, (c, r))
c, r = call("POST", "/vt-c1/usb/attach", {"vendor": "zz", "product": "1"}); T("usb ids validated", c in (400, 409), (c, r))

# ---- clone (background job)
c, r = call("POST", "/vt-c1/clone", {"new_name": "vt-clone"})
T("clone starts a job", c == 200 and r["job"]["state"] == "running", (c, r))
for _ in range(60):
    time.sleep(2)
    jobs = call("GET", "/jobs")[1]["jobs"]
    j = next((x for x in jobs if x["vm"] == "vt-clone"), None)
    if j and j["state"] != "running":
        break
T("clone finished", j and j["state"] == "done", j)
c, d = call("GET", "/vt-clone")
T("clone has new uuid, new MACs, copied disks", c == 200 and d["uuid"] != call("GET", "/vt-c1")[1]["uuid"] and d["spec"]["nics"][0]["mac"] != call("GET", "/vt-c1")[1]["spec"]["nics"][0]["mac"] and all(x["exists"] for x in d["disks_detail"]), d if c != 200 else d["disks_detail"])
c, r = call("POST", "/vt-c1/clone", {"new_name": "vt-clone"}); T("clone over an existing VM refused", c == 409, (c, r))
c, r = call("POST", "/vt-clone/start"); T("the clone boots", c == 200 and wait_state("vt-clone", "running", 30), (c, r))
call("POST", "/vt-clone/force-stop")

# ---- templates from a VM
c, r = call("POST", "/templates", {"name": "vt template", "from_vm": "vt-c1"}); T("save a VM as a template", c == 200, (c, r))
c, t = call("GET", "/templates"); T("your template is listed, without disks/MACs", any(x.get("user") and x["name"] == "vt template" and not x["defaults"]["disks"][0]["path"] for x in t["templates"]), [x["id"] for x in t["templates"]])
c, r = call("DELETE", "/templates/user:vt_template"); T("delete your template", c == 200, (c, r))
c, r = call("DELETE", "/templates/windows11"); T("built-in templates cannot be deleted", c == 400, (c, r))

# ---- settings validation
c, r = call("PUT", "/settings", {"storage": "/etc"}); T("settings: folder must be under /mnt", c == 400, (c, r))
c, r = call("PUT", "/settings", {"shutdown_action": "explode"}); T("settings: shutdown action validated", c == 400, (c, r))
c, r = call("PUT", "/settings", {"shutdown_timeout": 3}); T("settings: timeout range", c == 400, (c, r))

# ---- delete: only the VM's own disks, never someone else's
c, r = call("DELETE", "/vt-clone?remove_disks=true"); T("remove clone and its disks", c == 200 and r["removed_disks"], (c, r))
T("clone's disk files are gone, the original's are not", "vdisk1" not in sh("ls /mnt/user/domains/vt-clone 2>&1 | grep -c vdisk || true").replace("0", "") and "vdisk1.qcow2" in sh("ls /mnt/user/domains/vt-c1"))
c, r = call("DELETE", "/vt-c1"); T("remove VM but keep disks", c == 200 and "vdisk1.qcow2" in sh("ls /mnt/user/domains/vt-c1"), (c, r))
c, r = call("GET", "/vt-c1"); T("gone from libvirt", c == 404, (c, r))
sh("sudo -n rm -rf /mnt/user/domains/vt-*")
print(f"\n{ok} passed, {bad} failed")
sys.exit(1 if bad else 0)
