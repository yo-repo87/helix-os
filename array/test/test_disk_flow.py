"""Disk page + Format flow, against a throwaway array (see test-disk-flow.sh, which sets it up).

Image files stand in for disks, so nothing here can touch a real device. Covers: the per-disk page,
choosing a file system (and that it does NOT format), type-the-name Erase, the Format confirmation
gate, an honest error when a format cannot run, and that assign keeps each disk's chosen format.
"""
import json, os, subprocess, sys, time, urllib.request, urllib.error
D = os.environ["DK_DIR"]; PORT = os.environ.get("DK_PORT", "8094"); B = f"http://127.0.0.1:{PORT}/api/array"; ok = bad = 0


def call(m, p, body=None):
    r = urllib.request.Request(B + p, data=json.dumps(body).encode() if body is not None else None, method=m)
    if body is not None:
        r.add_header("Content-Type", "application/json")
    try:
        x = urllib.request.urlopen(r, timeout=60)
        return x.status, json.loads(x.read() or b"{}")
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
        print("FAIL", n, "->", x)


cfg = lambda: open(D + "/cfg/disks.cfg").read()
setstate = lambda s: open(D + "/state/array_state", "w").write(s + "\n")
fstype = lambda f: subprocess.run(["blkid", "-s", "TYPE", "-o", "value", f], capture_output=True, text=True).stdout.strip()

# --- stopped: disk page
c, r = call("GET", "/disk/disk1")
T("disk page", c == 200 and r["role"] == "data" and r["fs"] == "xfs" and r["fs_editable"] and r["present"] and not r["mounted"]
  and r["prev"] == "parity1" and r["next"] == "disk2", r)
c, r = call("GET", "/disk/parity1")
T("parity page: raw, nothing to format", c == 200 and r["role"] == "parity" and not r["fs_editable"] and not r["erase_enabled"]
  and "raw" in r["note"].lower() and r["fs"] == "", r)
c, r = call("GET", "/disk/cache1")
T("cache default btrfs, last disk has no next", c == 200 and r["fs"] == "btrfs" and r["next"] is None, r)
for n in ("disk9", "..%2Fetc", "parity", "disk"):
    c, r = call("GET", "/disk/" + n)
    T(f"unknown disk {n!r} -> 404", c == 404, (c, r))

# --- choose filesystem (does not format)
c, r = call("POST", "/disk/disk1/filesystem", {"fs": "btrfs"})
T("set fs", c == 200 and r["fs"] == "btrfs", (c, r))
T("only disk1 changed, every other section kept",
  "[parity1]" in cfg() and "[cache1]" in cfg() and "[disk2]" in cfg() and cfg().count("format=btrfs") == 2
  and "format=xfs" in cfg() and "d2.img" in cfg(), cfg())
T("image untouched by choosing a fs", fstype(D + "/d1.img") == "")
for fs in ("zfs", "ntfs", "", "xfs; rm -rf /"):
    c, r = call("POST", "/disk/disk1/filesystem", {"fs": fs})
    T(f"rejects fs {fs!r}", c == 400, (c, r))
c, r = call("POST", "/disk/parity1/filesystem", {"fs": "xfs"})
T("parity cannot get a fs", c == 409, (c, r))
c, r = call("POST", "/disk/disk7/filesystem", {"fs": "xfs"})
T("fs on unknown disk 404", c == 404, (c, r))

# --- disk that already has a fs
subprocess.run(["mkfs.ext4", "-q", "-F", D + "/d2.img"], check=True)
c, r = call("GET", "/disk/disk2")
T("existing fs detected, not editable, erase offered",
  c == 200 and r["fstype"] == "ext4" and not r["fs_editable"] and r["erase_enabled"] and "already has" in r["note"], r)
c, r = call("POST", "/disk/disk2/filesystem", {"fs": "xfs"})
T("cannot change fs on a formatted disk", c == 409, (c, r))

# --- erase: type the name
c, r = call("POST", "/disk/disk2/erase", {"confirm": "disk1"})
T("erase with the wrong name refused", c == 409 and "Incorrect" in r["detail"], (c, r))
c, r = call("POST", "/disk/disk2/erase", {})
T("erase with no confirmation refused", c == 409, (c, r))
T("nothing erased yet", fstype(D + "/d2.img") == "ext4")
c, r = call("POST", "/disk/parity1/erase", {"confirm": "parity1"})
T("parity cannot be erased", c == 409, (c, r))
c, r = call("POST", "/disk/disk2/erase", {"confirm": "disk2"})
T("erase with the right name works", c == 200 and r["ok"], (c, r))
T("signature really wiped", fstype(D + "/d2.img") == "")
T("disk still assigned after erase", "[disk2]" in cfg() and "d2.img" in cfg())
c, r = call("GET", "/disk/disk2")
T("page offers fs choice again", r["fs_editable"] and r["fstype"] == "", r)

# --- assign keeps each disk's chosen format
c, r = call("POST", "/assign", {"parity": [D + "/par.img"], "data": [D + "/d1.img", D + "/d2.img"],
                                "cache": [D + "/cache.img"], "fmt": "xfs", "cache_fmt": "btrfs"})
T("assign ok", c == 200 and r.get("ok"), (c, r))
T("assign kept disk1's btrfs choice (request default is xfs)",
  "format=btrfs" in cfg().split("[disk2]")[0].split("[disk1]")[1], cfg())

# --- started: unmountable disks + format gate
setstate("started")
c, r = call("GET", "/operation")
rows = {x["id"]: x for x in r["rows"]}
T("started: Format row appears with the confirm gate",
  "format" in rows and rows["format"]["button"]["label"] == "Format" and rows["format"]["confirm"] == "Yes, I want to do this"
  and rows["format"]["level"] == "warn", rows.get("format"))
names = {d.split(" ")[0] for d in rows["format"]["detail"]}
T("unmountable list names data+cache but not parity", {"disk1", "disk2", "cache1"} <= names and "parity1" not in names, rows["format"]["detail"])
c, r = call("GET", "/disk/disk1")
T("started + unmountable: page says use Format on Main, erase off",
  not r["fs_editable"] and not r["erase_enabled"] and "Format on Main" in r["note"], r)
c, r = call("POST", "/disk/disk1/erase", {"confirm": "disk1"})
T("no erase while started", c == 409, (c, r))
c, r = call("POST", "/format")
T("format without confirm refused", c == 409 and "Confirmation" in r["detail"], (c, r))
c, r = call("POST", "/format?confirm=false")
T("confirm=false refused", c == 409, (c, r))
c, r = call("POST", "/format?confirm=true")
T("format with confirm accepted", c == 200 and r["ok"] and set(r["disks"]) >= {"disk1", "disk2", "cache1"}, (c, r))
rr = {}
for _ in range(60):
    time.sleep(0.5)
    c, op = call("GET", "/operation")
    rr = {x["id"]: x for x in op["rows"]}
    if "format" in rr and "formatting" not in rr["format"]["status"].lower() and "formatting" not in json.dumps(op["rows"][0]).lower():
        break
T("a failed format is reported on Main, not swallowed",
  "format" in rr and ("failed" in rr["format"]["explain"].lower() or "No unmountable" in rr["format"]["explain"]
                      or "stopped before" in rr["format"]["explain"].lower()), rr.get("format"))
T("disks still listed after the failure so the user can retry", rr.get("format", {}).get("button") is not None, rr.get("format"))
setstate("stopped")
c, r = call("POST", "/format?confirm=true")
T("format refused while stopped", c == 409, (c, r))

# --- old one-click endpoint no longer formats
req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/drives/sda/format", data=b'{"fs":"xfs","force":true}',
                             method="POST", headers={"Content-Type": "application/json"})
try:
    urllib.request.urlopen(req, timeout=20)
    code = 200
except urllib.error.HTTPError as e:
    code = e.code
T("old /api/drives/{name}/format refuses (never formats)", code in (404, 409, 422), code)
c, r = call("GET", "/status")
T("status ok", c == 200)
print(f"\n{ok} passed, {bad} failed")
sys.exit(1 if bad else 0)
