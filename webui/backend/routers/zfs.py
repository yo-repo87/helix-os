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

import subprocess, json, os, re
from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel
from typing import Optional
from config import HELIX_CONFIG, STATE_DIR, MNT_BASE, USER_SHARE

router = APIRouter(prefix="/api/zfs", tags=["zfs"])

ZFS_KEYS_FILE = os.path.join(HELIX_CONFIG, "zfs", "encryption-keys.json")

def _ensure_zfs_dir():
    os.makedirs(os.path.dirname(ZFS_KEYS_FILE), exist_ok=True)

def _run(*args, check=False, timeout=30) -> tuple[int, str, str]:
    """Run a command and return (rc, stdout, stderr)."""
    try:
        r = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
        if check and r.returncode != 0:
            raise HTTPException(500, r.stderr.strip() or f"{args[0]} failed (rc={r.returncode})")
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        raise HTTPException(500, f"{args[0]} timed out")
    except Exception as e:
        raise HTTPException(500, str(e))

def _zfs_available() -> bool:
    """Check if ZFS tools are installed."""
    rc, _, _ = _run("which", "zpool")
    return rc == 0


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

@router.get("/pools")
def list_pools():
    """List all ZFS pools."""
    if not _zfs_available():
        return {"available": False, "pools": []}
    pools = _parse_pool_list()
    # Add datasets for each pool
    for pool in pools:
        pool["datasets"] = _get_pool_datasets(pool["name"])
    return {"available": True, "pools": pools}


@router.get("/discover")
def discover_pools():
    """Scan for importable ZFS pools on available drives."""
    if not _zfs_available():
        return {"available": False, "discovered": []}
    rc, out, err = _run("zpool", "import")
    if rc != 0:
        # No pools found or an error
        return {"available": True, "discovered": [], "error": err if err else None}

    discovered = []
    pool_name = None
    pool_info = {}

    for line in out.splitlines():
        line = line.rstrip()
        if line.startswith("  pool:"):
            if pool_name and pool_info:
                discovered.append({"name": pool_name, **pool_info})
            pool_name = line.split(":", 1)[1].strip()
            pool_info = {}
        elif pool_name and ":" in line:
            key, val = line.strip().split(":", 1)
            key = key.strip()
            val = val.strip()
            if key in ("id", "state", "status", "action"):
                pool_info[key] = val

    if pool_name and pool_info:
        discovered.append({"name": pool_name, **pool_info})

    return {"available": True, "discovered": discovered}


class PoolImport(BaseModel):
    pool: str
    passphrase: Optional[str] = None  # for encrypted pools


@router.post("/import")
def import_pool(req: PoolImport):
    """Import an existing ZFS pool."""
    if not _zfs_available():
        raise HTTPException(400, "ZFS not available")

    # For now, import without encryption support (can add later if needed)
    cmd = ["zpool", "import", "-f", req.pool]
    rc, out, err = _run(*cmd)
    if rc != 0:
        raise HTTPException(500, f"Import failed: {err}")

    # Auto-share the pool root dataset
    _add_pool_share(req.pool)

    return {"ok": True, "pool": req.pool, "message": f"Imported {req.pool}"}


@router.get("/available-drives")
def available_drives():
    """Get list of drives not in any ZFS pool or Helix array."""
    from pathlib import Path
    from services.drive_health import DRIVES_CACHE

    # Load drives from cache
    all_disks = []
    try:
        all_disks = json.loads(Path(DRIVES_CACHE).read_text())
    except:
        pass

    # Get disks in ZFS pools
    zfs_disks = set()
    for pool in _parse_pool_list():
        rc, out, err = _run("zpool", "status", pool["name"])
        for line in out.splitlines():
            # Parse device lines from zpool status
            if "ONLINE" in line or "OFFLINE" in line or "DEGRADED" in line:
                parts = line.split()
                if parts:
                    dev = parts[0]
                    if dev not in ("NAME", "state", "scan"):
                        # Normalize to /dev/xxx
                        if not dev.startswith("/dev/"):
                            dev = f"/dev/{dev}"
                        zfs_disks.add(dev)

    # Get disks in Helix array (from disks.cfg)
    array_disks = set()
    try:
        from config import DISKS_CFG
        content = open(DISKS_CFG).read() if os.path.exists(DISKS_CFG) else ""
        # Extract all device= values
        for dev_match in re.finditer(r'device=([^\s\n]+)', content):
            array_disks.add(dev_match.group(1))
    except:
        pass

    # Filter out boot disk
    boot_disk = _get_boot_disk()
    in_use = zfs_disks | array_disks
    if boot_disk:
        in_use.add(boot_disk)

    available = [d for d in all_disks if d.get("device") not in in_use]
    return {
        "available": available,
        "in_use_count": len(in_use),
        "in_use_devices": sorted(list(in_use))
    }


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


def _add_pool_share(pool: str):
    """Auto-add the pool root dataset as a share."""
    from services import shares_cfg
    from config import SHARES_CFG

    try:
        content = open(SHARES_CFG).read() if os.path.exists(SHARES_CFG) else ""
    except:
        content = ""

    # Check if this pool is already a share
    if f"[{pool}]" in content:
        return

    # Add new share section
    share_config = f"""
[{pool}]
storage={pool}
smb=yes
nfs=yes
"""
    new_content = content.rstrip() + "\n" + share_config.strip() + "\n"

    try:
        os.makedirs(os.path.dirname(SHARES_CFG), exist_ok=True)
        open(SHARES_CFG, "w").write(new_content)
        # Apply shares to export it
        from routers.shares import _apply_shares
        _apply_shares()
    except Exception as e:
        # Non-critical: log it but don't fail the import
        pass


@router.get("/pools/{pool}/status")
def pool_status(pool: str):
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

    # Auto-add as share if requested
    if req.auto_share:
        background_tasks.add_task(_add_pool_share, req.name)

    return {"ok": True, "pool": req.name, "raid": req.raid_type, "devices": len(req.devices)}


class PoolExpand(BaseModel):
    pool:      str
    devices:   list[str]
    raid_type: str = "mirror"  # type of new vdev to add


@router.post("/pools/{pool}/expand")
def expand_pool(pool: str, req: PoolExpand):
    """Add new drives to an existing pool."""
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

    # First try to export (safer than destroy)
    rc, out, err = _run("zpool", "export", pool)

    if rc != 0:
        # If export fails, try destroy
        cmd = ["zpool", "destroy", "-f", pool]
        rc, out, err = _run(*cmd)
        if rc != 0:
            raise HTTPException(500, f"Could not remove pool: {err}")

    # Remove from shares
    try:
        from config import SHARES_CFG
        content = open(SHARES_CFG).read() if os.path.exists(SHARES_CFG) else ""
        # Remove [pool] section
        new_content = re.sub(rf'\n\[{pool}\].*?(?=\n\[|\Z)', '', content, flags=re.DOTALL)
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
    if not _zfs_available():
        raise HTTPException(400, "ZFS not available")

    rc, out, err = _run("zpool", "scrub", pool)
    if rc != 0:
        raise HTTPException(500, f"Scrub failed: {err}")

    return {"ok": True, "pool": pool, "operation": "scrub started"}


@router.post("/pools/{pool}/scrub/stop")
def stop_scrub(pool: str):
    """Stop a running scrub operation."""
    if not _zfs_available():
        raise HTTPException(400, "ZFS not available")

    rc, out, err = _run("zpool", "scrub", "-s", pool)
    if rc != 0:
        raise HTTPException(500, f"Stop scrub failed: {err}")

    return {"ok": True, "pool": pool, "operation": "scrub stopped"}


@router.get("/pools/{pool}/snapshots")
def list_snapshots(pool: str):
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
