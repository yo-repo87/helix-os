"""
ZFS pool management: discover, import, create, expand, and manage ZFS pools.

Endpoints:
GET  /api/zfs/pools                         — list all pools (imported + created)
GET  /api/zfs/discover                      — scan for importable pools
POST /api/zfs/import                        — import an existing pool
POST /api/zfs/pools                         — create new pool
POST /api/zfs/pools/{pool}/expand           — add drives to pool
DELETE /api/zfs/pools/{pool}                — destroy/export pool
GET  /api/zfs/available-drives              — drives not in any pool/array
POST /api/zfs/pools/{pool}/scrub            — start scrub
POST /api/zfs/pools/{pool}/scrub/stop       — stop scrub
GET  /api/zfs/pools/{pool}/snapshots        — list snapshots
POST /api/zfs/pools/{pool}/snapshots        — create snapshot
DELETE /api/zfs/snapshots                   — destroy snapshot
POST /api/zfs/snapshots/rollback            — rollback snapshot
GET  /api/zfs/pools/{pool}/status           — detailed pool status
POST /api/zfs/datasets                      — create dataset in pool
"""

import subprocess, json, os, re, shutil
from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel
from typing import Optional
from config import HELIX_CONFIG, STATE_DIR, MNT_BASE, USER_SHARE

router = APIRouter(prefix="/api/zfs", tags=["zfs"])

ZFS_KEYS_FILE = os.path.join(HELIX_CONFIG, "zfs", "encryption-keys.json")

def _ensure_zfs_dir():
    os.makedirs(os.path.dirname(ZFS_KEYS_FILE), exist_ok=True)

_ZFS_CMDS = ("zpool", "zfs", "modprobe")
_POOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,254}$")


def _run(*args, check=False, timeout=30) -> tuple[int, str, str]:
    """Run a command and return (rc, stdout, stderr).

    zpool/zfs need root (import, create, mount, and on this ZFS version even some
    reads); the backend runs as the unprivileged `helix` user with passwordless
    sudo, so those go through `sudo -n`.
    """
    cmd = list(args)
    if cmd and cmd[0] in _ZFS_CMDS and os.geteuid() != 0:
        cmd = ["sudo", "-n", *cmd]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if check and r.returncode != 0:
            raise HTTPException(500, r.stderr.strip() or f"{args[0]} failed (rc={r.returncode})")
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        raise HTTPException(500, f"{args[0]} timed out")
    except FileNotFoundError:
        return 127, "", f"{args[0]} is not installed"


def _check_pool_name(pool: str) -> str:
    """Pool names reach zpool as an argument: refuse anything that could read as an option."""
    if not _POOL_RE.match(pool or ""):
        raise HTTPException(400, f"Invalid pool name: {pool!r}")
    return pool


def _zfs_state() -> tuple[bool, str]:
    """(usable, reason). Tools installed AND the kernel module loaded (/dev/zfs)."""
    if not shutil.which("zpool"):
        return False, "ZFS tools are not installed in this Helix OS image."
    if not os.path.exists("/dev/zfs"):
        rc, _, err = _run("modprobe", "zfs", timeout=60)
        if rc != 0 or not os.path.exists("/dev/zfs"):
            return False, ("The ZFS kernel module could not be loaded"
                           + (f": {err.strip()}" if err.strip() else "."))
    return True, ""


def _zfs_available() -> bool:
    return _zfs_state()[0]


# Pools Helix imported or created, with the altroot each one needs:
# {"zpool": {"altroot": "/mnt"}, "tank": {"altroot": ""}}. helix-array-start imports
# exactly these at array start (never every pool it happens to find on the disks).
POOLS_FILE = os.path.join(HELIX_CONFIG, "zfs", "pools.json")


def _load_pools() -> dict:
    try:
        data = json.load(open(POOLS_FILE))
    except Exception:
        return {}
    if isinstance(data, list):                       # first format: a plain list of names
        return {n: {"altroot": ""} for n in data if isinstance(n, str)}
    return data if isinstance(data, dict) else {}


def _remember_pool(pool: str, keep: bool = True, altroot: str = ""):
    pools = _load_pools()
    if keep:
        pools[pool] = {"altroot": altroot}
    else:
        pools.pop(pool, None)
    os.makedirs(os.path.dirname(POOLS_FILE), exist_ok=True)
    with open(POOLS_FILE + ".tmp", "w") as f:
        json.dump(pools, f, indent=1, sort_keys=True)
    os.replace(POOLS_FILE + ".tmp", POOLS_FILE)


def _import(pool: str) -> str:
    """Import `pool` so its file systems land under /mnt; returns the altroot used.

    TrueNAS pools keep mountpoint=/<pool> and are imported with altroot /mnt (so
    they mount at /mnt/<pool>); pools made by Helix already say /mnt/<pool>. The
    altroot is not stored in the pool, so neither choice changes the pool: try
    /mnt first without mounting, and fall back to no altroot when that would
    double the prefix. -f: a pool last used by another system (TrueNAS, Unraid,
    an earlier boot of this RAM-root OS) is refused without it.
    """
    base = ["zpool", "import", "-f", "-d", "/dev/disk/by-id", "-N"]
    rc, out, err = _run(*base, "-R", MNT_BASE, pool, timeout=300)
    if rc != 0:
        raise HTTPException(500, f"Import failed: {err.strip() or out.strip()}")
    altroot = MNT_BASE
    _, mp, _ = _run("zfs", "get", "-H", "-o", "value", "mountpoint", pool)
    if mp.strip().startswith(MNT_BASE.rstrip("/") + MNT_BASE.rstrip("/") + "/"):
        _run("zpool", "export", pool, timeout=120)
        rc, out, err = _run(*base, pool, timeout=300)
        if rc != 0:
            raise HTTPException(500, f"Import failed: {err.strip() or out.strip()}")
        altroot = ""
    rc, _, err = _run("zfs", "mount", "-a", timeout=300)
    return altroot


def _parse_pool_list() -> list[dict]:
    """Parse `zpool list -H` output."""
    pools = []
    rc, out, err = _run(
        "zpool", "list", "-H", "-o",
        "name,size,alloc,free,frag,cap,health,altroot"
    )
    if rc != 0:
        return pools
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 7:
            # Capacity warning/critical colour (§30.4 rule)
            cap_pct = int(parts[5].rstrip("%")) if parts[5] != "-" else 0
            health = parts[6]
            if health != "ONLINE":
                colour = "error"  # red
            elif cap_pct >= 90:
                colour = "error"  # red
            elif cap_pct >= 70:
                colour = "warning"  # orange
            else:
                colour = "ok"  # green
            pools.append({
                "name":    parts[0],
                "size":    parts[1],
                "alloc":   parts[2],
                "free":    parts[3],
                "frag":    parts[4],
                "cap":     parts[5],
                "cap_pct": cap_pct,
                "health":  health,
                "colour":  colour,
                "altroot": parts[7] if len(parts) > 7 else "-",
            })
    return pools

def _get_pool_datasets(pool: str) -> list[dict]:
    """List all datasets in a pool."""
    datasets = []
    rc, out, err = _run(
        "zfs", "list", "-H", "-o", "name,used,avail,mountpoint,type", "-r", pool
    )
    if rc != 0:
        return datasets
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 5:
            datasets.append({
                "name":       parts[0],
                "used":       parts[1],
                "available":  parts[2],
                "mountpoint": parts[3],
                "type":       parts[4],
            })
    return datasets

# ── ZFS safety net: install at runtime (helix-zfs-ensure) ─────────────────────
ZFS_ENSURE = os.path.join(os.getenv("ARRAY_SBIN", "/usr/local/sbin"), "helix-zfs-ensure")
ZFS_INSTALL_STATUS = os.path.join(STATE_DIR, "zfs_install.json")
ZFS_INSTALL_LOG = "/var/log/helix-zfs.log"


def _install_running() -> bool:
    try:
        r = subprocess.run(["pgrep", "-f", "helix-zfs-ensure"], capture_output=True, text=True, timeout=3)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _install_status() -> dict:
    try:
        st = json.load(open(ZFS_INSTALL_STATUS))
    except Exception:
        st = {"state": "unknown", "message": ""}
    running = _install_running()
    if st.get("state") == "installing" and not running:
        st = {**st, "state": "failed", "message": "The ZFS install stopped before it finished."}
    st["running"] = running
    return st


@router.get("/install")
def install_status(log: bool = False):
    st = _install_status()
    if log:
        try:
            with open(ZFS_INSTALL_LOG, errors="replace") as f:
                st["log"] = f.readlines()[-60:]
        except OSError:
            st["log"] = []
    return st


@router.post("/install")
def install_zfs():
    """Download (or restore from the saved copy) and install ZFS now, in the background."""
    if _install_running():
        return {"ok": True, "started": False, "message": "A ZFS install is already running."}
    if not os.path.exists(ZFS_ENSURE):
        raise HTTPException(500, f"{ZFS_ENSURE} is missing from this image")
    cmd = [ZFS_ENSURE, "--force"] if _zfs_state()[0] else [ZFS_ENSURE]
    cmd = ["env", f"HELIX_CONFIG={HELIX_CONFIG}", f"STATE_DIR={STATE_DIR}", *cmd]
    # Own systemd scope: a backend restart must not kill a compile halfway (it would, inside
    # helix-backend.service's cgroup).
    if shutil.which("systemd-run") and os.path.isdir("/run/systemd/system"):
        cmd = ["systemd-run", "--scope", "--quiet", "--collect", f"--unit=helix-zfs-install-{int(__import__('time').time())}", *cmd]
    if os.geteuid() != 0:
        cmd = ["sudo", "-n", *cmd]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return {"ok": True, "started": True}


def _pool_bytes() -> dict:
    """Usable space per pool, exact: the root dataset's used + available (`zfs list -p`).
    `zpool list` counts raw capacity including raidz parity (a 4 x 10 TB raidz1 reads 40 TB
    there but holds ~26 TB), which is not what a user can store."""
    rc, out, _ = _run("zfs", "list", "-Hp", "-d", "0", "-o", "name,used,avail")
    res = {}
    for line in out.splitlines() if rc == 0 else []:
        p = line.split("\t")
        if len(p) == 3 and p[1].isdigit() and p[2].isdigit():
            used, avail = int(p[1]), int(p[2])
            res[p[0]] = {"size_bytes": used + avail, "alloc_bytes": used, "free_bytes": avail}
    return res


def _parse_scan(status: str) -> dict:
    """The `scan:` block of `zpool status`: what ZFS is doing (resilver/scrub) and how far it is.
    {"op": "resilver"|"scrub"|"", "active": bool, "pct": float|None, "eta": str|None, "text": first line}"""
    m = re.search(r"^\s*scan:\s*(.*?)(?=^\s*config:|\Z)", status, re.M | re.S)
    if not m:
        return {"op": "", "active": False, "pct": None, "eta": None, "text": ""}
    block = " ".join(m.group(1).split())
    first = m.group(1).strip().splitlines()[0].strip() if m.group(1).strip() else ""
    op = "resilver" if "resilver" in first else "scrub" if "scrub" in first else ""
    active = "in progress" in first
    pct = re.search(r"([\d.]+)% done", block)
    eta = re.search(r"([\d:]+(?:\s*days?\s*[\d:]+)?) to go", block)
    return {"op": op, "active": active, "pct": float(pct.group(1)) if pct and active else None,
            "eta": eta.group(1) if eta and active else None, "text": first}


@router.get("/pools")
def list_pools():
    """List all ZFS pools."""
    ok, reason = _zfs_state()
    if not ok:
        return {"available": False, "reason": reason, "install": _install_status(), "pools": []}
    pools = _parse_pool_list()
    # Add datasets for each pool
    exact = _pool_bytes()
    for pool in pools:
        pool["datasets"] = _get_pool_datasets(pool["name"])
        pool["mountpoint"] = _mountpoint(pool["name"])
        pool["shares"] = _pool_shares(pool["name"])
        pool.update(exact.get(pool["name"], {}))
        _, st, _ = _run("zpool", "status", pool["name"])
        pool["scan"] = _parse_scan(st)
        m = re.search(r"^errors:\s*(.+)$", st, re.M)
        pool["errors"] = m.group(1).strip() if m else ""
    return {"available": True, "pools": pools}


@router.get("/discover")
def discover_pools():
    """Scan the disks for pools that can be imported (read-only: imports nothing)."""
    ok, reason = _zfs_state()
    if not ok:
        return {"available": False, "reason": reason, "discovered": []}
    rc, out, err = _run("zpool", "import", timeout=120)
    discovered = _parse_import_scan(out)
    msg = err.strip()
    if not discovered and msg.startswith("no pools available"):
        msg = ""
    return {"available": True, "discovered": discovered, "error": msg or None}


def _parse_import_scan(out: str) -> list[dict]:
    """Parse `zpool import` (no arguments). Fields are indented a varying number
    of spaces; status/action/see continue on following lines; `config:` is
    followed by the vdev tree."""
    pools, cur, key = [], None, None
    for raw in out.splitlines():
        line = raw.strip()
        m = re.match(r"^(pool|id|state|status|action|see|comment|config):\s*(.*)$", line)
        if m and not (key == "config" and m.group(1) not in ("pool",)):
            key, val = m.group(1), m.group(2)
            if key == "pool":
                cur = {"name": val, "devices": []}
                pools.append(cur)
            elif cur is not None and key != "config":
                cur[key] = val
            continue
        if cur is None or not line:
            continue
        if key == "config":
            parts = line.split()
            if parts and parts[0] not in ("NAME", cur["name"]) and \
                    not re.match(r"^(mirror|raidz\d?|draid|spare|log|cache|special|dedup|logs|spares)", parts[0]):
                cur["devices"].append({"name": parts[0], "state": parts[1] if len(parts) > 1 else ""})
        elif key in ("status", "action", "see", "comment"):
            cur[key] = (cur.get(key, "") + " " + line).strip()
    for p in pools:
        st = p.get("state", "")
        p["importable"] = st in ("ONLINE", "DEGRADED")
    return pools


class ShareOpts(BaseModel):
    # pool = one share for the whole pool; datasets = one share per top-level dataset
    mode:         str  = "pool"      # pool | datasets | none
    smb_security: str  = "public"    # public | private
    nfs:          bool = False


class PoolImport(BaseModel):
    pool:  str
    share: Optional[ShareOpts] = None   # None/mode "none" = import only
    passphrase: Optional[str] = None  # for encrypted pools


@router.post("/import")
def import_pool(req: PoolImport):
    """Import an existing ZFS pool."""
    if not _zfs_available():
        raise HTTPException(400, "ZFS not available")

    _check_pool_name(req.pool)
    altroot = _import(req.pool)
    _remember_pool(req.pool, altroot=altroot)
    mountpoint = _mountpoint(req.pool)
    result = {"created": [], "skipped": []}
    if req.share and req.share.mode != "none":
        try:
            result = _share_pool(req.pool, req.share)
        except Exception as e:                     # the import itself worked: say so
            result["error"] = str(e)
    return {"ok": True, "pool": req.pool, "mountpoint": mountpoint, "shares": result,
            "message": f"Imported {req.pool} at {mountpoint}"}


@router.post("/pools/{pool}/share")
def share_pool(pool: str, opts: ShareOpts):
    """Share an imported pool (or its top-level datasets) over SMB/NFS."""
    _check_pool_name(pool)
    if not _mountpoint(pool) and opts.mode == "pool":
        raise HTTPException(409, f"{pool} is not mounted")
    try:
        return {"ok": True, **_share_pool(pool, opts)}
    except ValueError as e:
        raise HTTPException(400, str(e))


def _mountpoint(ds: str) -> str:
    """Where a dataset is mounted right now ('' if it is not)."""
    try:
        for line in open("/proc/self/mounts"):
            src, tgt, fstype = line.split()[:3]
            if fstype == "zfs" and src == ds:
                return tgt.replace("\\040", " ")
    except OSError:
        pass
    return ""


@router.get("/available-drives")
def available_drives():
    """Physical disks that can go into a new ZFS pool.

    Everything is compared as real whole-disk names (/dev/sdX): disks.cfg holds by-id links and
    `zpool status` names members by id or by partition, so comparing raw strings (the old code) never
    matched and array disks were offered for a new pool. Left out: array disks, the boot medium, members
    of imported pools, disks with anything mounted. Disks that already hold data are listed with a
    warning (e.g. members of a pool used by a VM or not imported here).
    """
    from routers import drives as drives_router
    phys = drives_router.available_drives().get("disks", [])
    pool_members = _pool_member_disks()
    layout = _lsblk_layout()
    available, in_use = [], {}
    for d in phys:
        dev = d["device"]
        info = layout.get(dev, {})
        if d.get("is_boot"):
            in_use[dev] = "boot device"
        elif d.get("assigned"):
            in_use[dev] = f"array ({d.get('array_name') or d.get('role')})"
        elif dev in pool_members:
            in_use[dev] = f"ZFS pool {pool_members[dev]}"
        elif info.get("mounted"):
            in_use[dev] = "mounted (" + ", ".join(info["mounted"]) + ")"
        else:
            sizes = d.get("size_gb") or 0
            available.append({**d, "size": f"{sizes / 1000:.1f} TB" if sizes >= 1000 else f"{sizes:.0f} GB",
                              "warn": ("holds data: " + ", ".join(info["signatures"]) + ". It will be erased.")
                                      if info.get("signatures") else ""})
    return {"available": available, "in_use_count": len(in_use),
            "in_use_devices": sorted(in_use), "in_use": in_use}


def _whole(dev: str) -> str:
    """/dev/sdb1, /dev/nvme0n1p3 or a by-id link -> the whole disk (/dev/sdb, /dev/nvme0n1)."""
    real = os.path.realpath(dev)
    name = os.path.basename(real)
    if os.path.exists(f"/sys/class/block/{name}/partition"):
        return "/dev/" + os.path.basename(os.path.dirname(os.path.realpath(f"/sys/class/block/{name}")))
    return real


def _pool_member_disks() -> dict:
    """{whole disk: pool} for every vdev of every imported pool (-P full paths, -L resolve links)."""
    out = {}
    for pool in _parse_pool_list():
        rc, text, _ = _run("zpool", "status", "-P", "-L", pool["name"])
        for line in text.splitlines():
            parts = line.split()
            if parts and parts[0].startswith("/dev/"):
                out[_whole(parts[0])] = pool["name"]
    return out


def _lsblk_layout() -> dict:
    """{whole disk: {mounted: [...], signatures: [...]}} from lsblk (disk and its partitions)."""
    try:
        r = subprocess.run(["lsblk", "-J", "-o", "PATH,TYPE,FSTYPE,MOUNTPOINT"], capture_output=True, text=True, timeout=15)
        data = json.loads(r.stdout or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}
    out = {}

    def walk(node, disk):
        info = out.setdefault(disk, {"mounted": [], "signatures": []})
        if node.get("mountpoint"):
            info["mounted"].append(node["mountpoint"])
        if node.get("fstype"):
            info["signatures"].append(node["fstype"] + ("" if node.get("path") == disk else f" on {os.path.basename(node.get('path', ''))}"))
        elif node.get("path") != disk and node.get("type") == "part":
            info["signatures"].append(f"partition {os.path.basename(node.get('path', ''))}")
        for c in node.get("children") or []:
            walk(c, disk)

    for d in data.get("blockdevices", []):
        if d.get("type") == "disk" and d.get("path"):
            walk(d, d["path"])
    return out


def _get_boot_disk() -> str:
    """Get the boot disk device path (to exclude it from ZFS)."""
    try:
        rc, out, err = _run("df", "/boot")
        for line in out.splitlines():
            if "/boot" in line:
                dev = line.split()[0]
                if dev.startswith("/dev/"):
                    return dev
    except:
        pass
    return ""


def _share_targets(pool: str, mode: str) -> list[tuple[str, str]]:
    """(share name, path) pairs for a pool. "datasets" = every top-level dataset
    that is mounted under /mnt; system datasets (TrueNAS .system, .ix-virt,
    ix-apps; anything legacy-mounted) are left out."""
    if mode == "pool":
        return [(pool, _mountpoint(pool))]
    rc, out, _ = _run("zfs", "list", "-H", "-d", "1", "-o", "name", pool)
    targets = []
    for ds in out.split():
        if ds == pool:
            continue
        leaf = ds.split("/", 1)[1]
        if leaf.startswith((".", "ix-", "ix_")):
            continue
        mp = _mountpoint(ds)
        if mp:
            targets.append((leaf, mp))
    return targets


def _share_pool(pool: str, opts) -> dict:
    """Add shares for a pool. Each share is a [name] section in shares.cfg with
    path= pointing into the mounted pool; it stays there when the pool is
    exported and comes back by itself when the pool is imported again (array
    start imports pools before it applies shares)."""
    from config import SHARES_CFG
    from services import validation
    from routers.shares import _parse_shares, _apply_shares
    if opts.mode not in ("pool", "datasets"):
        raise ValueError(f"Unknown share mode {opts.mode!r}")
    if opts.smb_security not in ("public", "private"):
        raise ValueError("Security must be public or private")
    mnt = MNT_BASE.rstrip("/") + "/"
    existing = list(_parse_shares().keys())
    try:
        existing += [d for d in os.listdir(USER_SHARE) if os.path.isdir(os.path.join(USER_SHARE, d))]
    except OSError:
        pass
    created, skipped, block = [], [], ""
    for name, path in _share_targets(pool, opts.mode):
        if not path.startswith(mnt) or path.startswith(USER_SHARE.rstrip("/") + "/"):
            skipped.append({"name": name, "reason": f"not mounted under {MNT_BASE} ({path or 'not mounted'})"})
            continue
        problem = validation.share_name(name, existing)
        if problem:
            skipped.append({"name": name, "reason": problem})
            continue
        existing.append(name)
        block += (f"\n[{name}]\ncomment=ZFS {pool}\npath={path}\nzfs_pool={pool}\n"
                  f"smb=yes\nnfs={'yes' if opts.nfs else 'no'}\nsmb_security={opts.smb_security}\n")
        created.append(name)
    if created:
        content = open(SHARES_CFG).read() if os.path.exists(SHARES_CFG) else ""
        os.makedirs(os.path.dirname(SHARES_CFG), exist_ok=True)
        with open(SHARES_CFG + ".tmp", "w") as f:
            f.write((content.rstrip() + "\n" + block).lstrip("\n"))
        os.replace(SHARES_CFG + ".tmp", SHARES_CFG)
        err = _apply_shares()
        return {"created": created, "skipped": skipped, **({"apply_error": err} if err else {})}
    return {"created": created, "skipped": skipped}


def _pool_shares(pool: str) -> list[str]:
    """Names of shares that live on this pool."""
    from routers.shares import _parse_shares
    return [n for n, c in _parse_shares().items() if c.get("zfs_pool") == pool]


@router.get("/pools/{pool}/status")
def pool_status(pool: str):
    _check_pool_name(pool)
    rc, out, err = _run("zpool", "status", "-v", pool)
    return {"output": out, "error": err, "rc": rc}


class PoolCreate(BaseModel):
    name:        str
    devices:     list[str]       # device paths, e.g. ["/dev/sdb", "/dev/sdc"]
    raid_type:   str = "mirror"  # stripe | mirror | raidz1 | raidz2 | raidz3
    encryption:  bool = False
    passphrase:  Optional[str] = None
    auto_share:  bool = True


def _validate_pool_name(name: str) -> str | None:
    """Validate pool name. Returns error message or None if valid."""
    if not name:
        return "Pool name cannot be empty"
    if not re.match(r"^[a-zA-Z0-9_-]+$", name):
        return "Pool name must be alphanumeric with _ or - only"
    if len(name) > 255:
        return "Pool name too long (max 255 chars)"
    # Check if already exists
    for pool in _parse_pool_list():
        if pool["name"] == name:
            return f"Pool '{name}' already exists"
    return None


def _validate_devices(devices: list[str], raid_type: str) -> str | None:
    """Validate device selection. Returns error message or None if valid."""
    if not devices:
        return "No devices selected"

    min_devs = {
        "stripe": 1,
        "mirror": 2,
        "raidz1": 3,
        "raidz2": 4,
        "raidz3": 5,
    }

    required = min_devs.get(raid_type, 1)
    if len(devices) < required:
        return f"{raid_type} requires at least {required} devices, got {len(devices)}"

    # Check devices exist and are not mounted
    for dev in devices:
        if not dev.startswith("/dev/"):
            return f"Invalid device path: {dev}"
        if not os.path.exists(dev):
            return f"Device not found: {dev}"
        # Check if mounted
        rc, out, err = _run("findmnt", dev)
        if rc == 0:
            return f"Device {dev} is mounted or in use"

    return None


@router.post("/pools")
def create_pool(req: PoolCreate, background_tasks: BackgroundTasks):
    """Create a new ZFS pool."""
    if not _zfs_available():
        raise HTTPException(400, "ZFS not available")

    # Validate
    err = _validate_pool_name(req.name)
    if err:
        raise HTTPException(400, err)

    err = _validate_devices(req.devices, req.raid_type)
    if err:
        raise HTTPException(400, err)

    # Build zpool create command
    cmd = ["zpool", "create", "-m", f"{MNT_BASE}/{req.name}"]

    # Encryption (for future use, storing UI support now)
    if req.encryption and req.passphrase:
        cmd += [
            "-O", "encryption=on",
            "-O", "keyformat=passphrase",
            "-O", "keylocation=prompt",
        ]

    cmd.append(req.name)

    # RAID configuration
    if req.raid_type != "stripe":
        cmd.append(req.raid_type)

    cmd.extend(req.devices)

    # Run create
    rc, out, err = _run(*cmd, timeout=60)
    if rc != 0:
        raise HTTPException(500, f"Pool creation failed: {err}")
    _remember_pool(req.name, altroot="")

    # Auto-add as share if requested
    if req.auto_share:
        background_tasks.add_task(_share_pool, req.name, ShareOpts(mode="pool"))

    return {"ok": True, "pool": req.name, "raid": req.raid_type, "devices": len(req.devices)}


class PoolExpand(BaseModel):
    pool:      str
    devices:   list[str]
    raid_type: str = "mirror"  # type of new vdev to add


@router.post("/pools/{pool}/expand")
def expand_pool(pool: str, req: PoolExpand):
    """Add new drives to an existing pool."""
    _check_pool_name(pool)
    if not _zfs_available():
        raise HTTPException(400, "ZFS not available")

    err = _validate_devices(req.devices, req.raid_type)
    if err:
        raise HTTPException(400, err)

    cmd = ["zpool", "add", pool]

    if req.raid_type != "stripe":
        cmd.append(req.raid_type)

    cmd.extend(req.devices)

    rc, out, err = _run(*cmd, timeout=60)
    if rc != 0:
        raise HTTPException(500, f"Pool expansion failed: {err}")

    return {"ok": True, "pool": pool, "devices_added": len(req.devices)}


@router.delete("/pools/{pool}")
def destroy_pool(pool: str, confirm: str = ""):
    """Export/destroy a ZFS pool."""
    if not _zfs_available():
        raise HTTPException(400, "ZFS not available")

    # Safety: require the pool name to be typed
    if confirm != pool:
        raise HTTPException(400, f"Confirmation mismatch. Type '{pool}' to confirm.")

    _check_pool_name(pool)
    # Export only: the pool and its data stay on the disks and can be imported
    # again. (This used to fall back to `zpool destroy -f` when the export
    # failed, e.g. because a file was open: that erased the pool.)
    rc, out, err = _run("zpool", "export", pool, timeout=120)
    if rc != 0:
        raise HTTPException(409, f"Could not export {pool}: {err.strip()} "
                                 "(stop the apps or shares using it and try again)")
    _remember_pool(pool, keep=False)

    # Remove from shares
    try:
        from config import SHARES_CFG
        content = open(SHARES_CFG).read() if os.path.exists(SHARES_CFG) else ""
        # Remove [pool] section
        new_content = re.sub(rf'\n\[{re.escape(pool)}\].*?(?=\n\[|\Z)', '', content, flags=re.DOTALL)
        open(SHARES_CFG, "w").write(new_content)
        from routers.shares import _apply_shares
        _apply_shares()
    except:
        pass

    return {"ok": True, "pool": pool, "message": f"Pool {pool} removed"}


class DatasetCreate(BaseModel):
    pool:      str
    dataset:   str  # e.g. "movies" to create pool/movies
    mountpoint: Optional[str] = None


@router.post("/datasets")
def create_dataset(req: DatasetCreate):
    """Create a new dataset within a pool."""
    if not _zfs_available():
        raise HTTPException(400, "ZFS not available")

    full_name = f"{req.pool}/{req.dataset}"
    cmd = ["zfs", "create"]

    if req.mountpoint:
        cmd += ["-m", req.mountpoint]

    cmd.append(full_name)

    rc, out, err = _run(*cmd)
    if rc != 0:
        raise HTTPException(500, f"Dataset creation failed: {err}")

    return {"ok": True, "dataset": full_name}


@router.post("/pools/{pool}/scrub")
def start_scrub(pool: str):
    """Start a scrub operation on a pool."""
    _check_pool_name(pool)
    if not _zfs_available():
        raise HTTPException(400, "ZFS not available")

    rc, out, err = _run("zpool", "scrub", pool)
    if rc != 0:
        raise HTTPException(500, f"Scrub failed: {err}")

    return {"ok": True, "pool": pool, "operation": "scrub started"}


@router.post("/pools/{pool}/scrub/stop")
def stop_scrub(pool: str):
    """Stop a running scrub operation."""
    _check_pool_name(pool)
    if not _zfs_available():
        raise HTTPException(400, "ZFS not available")

    rc, out, err = _run("zpool", "scrub", "-s", pool)
    if rc != 0:
        raise HTTPException(500, f"Stop scrub failed: {err}")

    return {"ok": True, "pool": pool, "operation": "scrub stopped"}


@router.get("/pools/{pool}/snapshots")
def list_snapshots(pool: str):
    _check_pool_name(pool)
    rc, out, err = _run(
        "zfs", "list", "-H", "-t", "snapshot", "-o",
        "name,used,refer,creation", "-s", "creation", "-r", pool
    )
    snaps = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 4:
            snaps.append({
                "name":     parts[0],
                "used":     parts[1],
                "refer":    parts[2],
                "creation": parts[3],
            })
    return snaps


class SnapCreate(BaseModel):
    tag: str   # snapshot name suffix after @

class SnapAction(BaseModel):
    name: str  # full snapshot name, e.g. pool/dataset@tag


@router.post("/pools/{pool}/snapshots")
def create_snapshot(pool: str, req: SnapCreate):
    snap = f"{pool}@{req.tag}"
    _run("zfs", "snapshot", snap, check=True)
    return {"ok": True, "snapshot": snap}


@router.delete("/snapshots")
def destroy_snapshot(req: SnapAction):
    _run("zfs", "destroy", req.name, check=True)
    return {"ok": True}


@router.post("/snapshots/rollback")
def rollback_snapshot(req: SnapAction):
    _run("zfs", "rollback", "-r", req.name, check=True)
    return {"ok": True}
