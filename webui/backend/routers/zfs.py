"""
ZFS pool management.

GET  /api/zfs/pools                         — list pools
POST /api/zfs/pools                         — create pool
DELETE /api/zfs/pools/{pool}                — destroy pool
POST /api/zfs/pools/{pool}/scrub            — start scrub
POST /api/zfs/pools/{pool}/scrub/stop       — stop scrub
GET  /api/zfs/pools/{pool}/snapshots        — list snapshots
POST /api/zfs/pools/{pool}/snapshots        — create snapshot
DELETE /api/zfs/snapshots                   — destroy snapshot (path in body)
POST /api/zfs/snapshots/rollback            — rollback to snapshot
GET  /api/zfs/pools/{pool}/status           — zpool status -v
"""

import subprocess
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/zfs", tags=["zfs"])


def _run(*args, check=False) -> tuple[int, str, str]:
    r = subprocess.run(list(args), capture_output=True, text=True, timeout=30)
    if check and r.returncode != 0:
        raise HTTPException(500, r.stderr.strip() or f"{args[0]} failed (rc={r.returncode})")
    return r.returncode, r.stdout, r.stderr


def _zfs_available() -> bool:
    rc, _, _ = _run("which", "zpool")
    return rc == 0


@router.get("/pools")
def list_pools():
    if not _zfs_available():
        return {"available": False, "pools": []}
    rc, out, err = _run(
        "zpool", "list", "-H", "-o",
        "name,size,alloc,free,frag,cap,health,altroot"
    )
    pools = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 7:
            pools.append({
                "name":    parts[0],
                "size":    parts[1],
                "alloc":   parts[2],
                "free":    parts[3],
                "frag":    parts[4],
                "cap":     parts[5],
                "health":  parts[6],
                "altroot": parts[7] if len(parts) > 7 else "-",
            })
    return {"available": True, "pools": pools}


@router.get("/pools/{pool}/status")
def pool_status(pool: str):
    rc, out, err = _run("zpool", "status", "-v", pool)
    return {"output": out, "error": err, "rc": rc}


class PoolCreate(BaseModel):
    name:    str
    vdevs:   list[str]          # device paths, e.g. ["/dev/sdb", "/dev/sdc"]
    raid:    str = "mirror"     # mirror | raidz | raidz2 | raidz3 | stripe
    mountpoint: str = ""        # optional -m path


@router.post("/pools")
def create_pool(req: PoolCreate):
    cmd = ["zpool", "create"]
    if req.mountpoint:
        cmd += ["-m", req.mountpoint]
    cmd.append(req.name)
    if req.raid != "stripe":
        cmd.append(req.raid)
    cmd.extend(req.vdevs)
    _run(*cmd, check=True)
    return {"ok": True, "name": req.name}


@router.delete("/pools/{pool}")
def destroy_pool(pool: str, force: bool = False):
    cmd = ["zpool", "destroy"]
    if force:
        cmd.append("-f")
    cmd.append(pool)
    _run(*cmd, check=True)
    return {"ok": True}


@router.post("/pools/{pool}/scrub")
def start_scrub(pool: str):
    _run("zpool", "scrub", pool, check=True)
    return {"ok": True}


@router.post("/pools/{pool}/scrub/stop")
def stop_scrub(pool: str):
    _run("zpool", "scrub", "-s", pool, check=True)
    return {"ok": True}


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
