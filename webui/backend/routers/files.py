"""
File browser.

GET  /api/files/list?path=      — list directory contents
GET  /api/files/download?path=  — download a file
POST /api/files/upload?path=    — upload file(s) to a directory
POST /api/files/mkdir           — create directory
POST /api/files/delete          — delete file or empty directory
POST /api/files/rename          — rename/move a file or directory
"""

import os
import shutil
import stat
from pathlib import Path
from typing import List

from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

router = APIRouter(prefix="/api/files", tags=["files"])

# Allowed root paths (security: never serve outside these)
ALLOWED_ROOTS = [
    "/mnt/user",
    "/mnt/disk",
    "/mnt/cache",
    "/home/pi",
    "/tmp",
]

# Default browse root
DEFAULT_ROOT = "/mnt/user"


def _safe_path(path: str) -> Path:
    """Resolve path and check it's under an allowed root."""
    if not path or path.strip() == "":
        path = DEFAULT_ROOT
    p = Path(path).resolve()
    for root in ALLOWED_ROOTS:
        try:
            p.relative_to(root)
            return p
        except ValueError:
            continue
    # Also allow if it IS an allowed root
    if str(p) in ALLOWED_ROOTS:
        return p
    raise HTTPException(status_code=403, detail=f"Path not allowed: {path}")


def _entry(p: Path, root_str: str) -> dict:
    try:
        st = p.stat()
        is_dir = stat.S_ISDIR(st.st_mode)
        return {
            "name":     p.name,
            "path":     str(p),
            "is_dir":   is_dir,
            "size":     st.st_size if not is_dir else None,
            "modified": int(st.st_mtime),
            "ext":      p.suffix.lower() if not is_dir else "",
        }
    except OSError:
        return {"name": p.name, "path": str(p), "is_dir": False, "size": None, "modified": 0, "ext": ""}


@router.get("/list")
def list_dir(path: str = ""):
    p = _safe_path(path or DEFAULT_ROOT)
    if not p.exists():
        # Return default root listing if mnt/user doesn't exist
        for fallback in ["/home/pi", "/tmp"]:
            fp = Path(fallback)
            if fp.exists():
                p = fp
                break
    if not p.is_dir():
        raise HTTPException(status_code=400, detail="Not a directory")

    entries = []
    try:
        for child in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            entries.append(_entry(child, str(p)))
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")

    # Build breadcrumb
    crumbs = []
    parts = p.parts
    for i in range(1, len(parts) + 1):
        crumb_path = str(Path(*parts[:i]))
        # Only show breadcrumbs within allowed roots
        allowed = any(
            crumb_path == r or crumb_path.startswith(r + "/")
            for r in ALLOWED_ROOTS
        )
        if allowed or crumb_path in ALLOWED_ROOTS:
            crumbs.append({"name": parts[i - 1] or "/", "path": crumb_path})

    parent = str(p.parent) if str(p) not in ALLOWED_ROOTS else None
    return {
        "path":    str(p),
        "parent":  parent,
        "crumbs":  crumbs,
        "entries": entries,
    }


@router.get("/download")
def download_file(path: str):
    p = _safe_path(path)
    if not p.exists() or p.is_dir():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(p), filename=p.name, media_type="application/octet-stream")


@router.post("/upload")
async def upload_files(path: str = "", files: List[UploadFile] = File(...)):
    dest_dir = _safe_path(path or DEFAULT_ROOT)
    if not dest_dir.is_dir():
        raise HTTPException(status_code=400, detail="Destination is not a directory")
    saved = []
    for f in files:
        dest = dest_dir / f.filename
        with open(dest, "wb") as out:
            shutil.copyfileobj(f.file, out)
        saved.append(str(dest))
    return {"saved": saved}


class PathRequest(BaseModel):
    path: str


class RenameRequest(BaseModel):
    path: str
    new_name: str


@router.post("/mkdir")
def make_dir(req: PathRequest):
    p = _safe_path(req.path)
    if p.exists():
        raise HTTPException(status_code=400, detail="Already exists")
    p.mkdir(parents=True, exist_ok=True)
    return {"created": str(p)}


@router.post("/delete")
def delete_path(req: PathRequest):
    p = _safe_path(req.path)
    if not p.exists():
        raise HTTPException(status_code=404, detail="Not found")
    if p.is_dir():
        shutil.rmtree(str(p))
    else:
        p.unlink()
    return {"deleted": str(p)}


@router.post("/rename")
def rename_path(req: RenameRequest):
    p = _safe_path(req.path)
    if not p.exists():
        raise HTTPException(status_code=404, detail="Not found")
    new_p = p.parent / req.new_name
    _safe_path(str(new_p))  # validate destination is also allowed
    p.rename(new_p)
    return {"renamed_to": str(new_p)}


# ── Recycle bin ───────────────────────────────────────────────────────────────
# Samba VFS recycle places deleted files at <share_root>/.recycle/<user>/<orig_path>

RECYCLE_ROOTS = (["/mnt/user"] + [f"/mnt/disk{i}" for i in range(1, 29)]
                 + [f"/mnt/cache{i}" for i in range(1, 5)])      # helix-array-start mounts cache1, cache2, …


def _walk_recycle(root: Path) -> list[dict]:
    """Walk a .recycle directory and return flat file list."""
    items = []
    for dirpath, dirnames, filenames in os.walk(str(root)):
        for fname in filenames:
            full = Path(dirpath) / fname
            try:
                st = full.stat()
                # Path relative to .recycle root = original path hint
                rel = str(full.relative_to(root))
                items.append({
                    "path":     str(full),
                    "name":     fname,
                    "rel":      rel,
                    "size":     st.st_size,
                    "modified": int(st.st_mtime),
                    "ext":      full.suffix.lower(),
                    "share":    root.parent.name,
                })
            except OSError:
                pass
    return items


@router.get("/recycle")
def recycle_list():
    """List all files in Samba recycle bins across all shares."""
    items = []
    checked = set()
    # Check /mnt/user/<share>/.recycle
    for base in ["/mnt/user", "/mnt/cache"]:
        bp = Path(base)
        if not bp.is_dir():
            continue
        try:
            for share_dir in bp.iterdir():
                if not share_dir.is_dir():
                    continue
                recycle = share_dir / ".recycle"
                key = str(recycle)
                if key in checked or not recycle.is_dir():
                    checked.add(key)
                    continue
                checked.add(key)
                items.extend(_walk_recycle(recycle))
        except PermissionError:
            pass
    # Sort newest first
    items.sort(key=lambda x: x["modified"], reverse=True)
    return {"items": items, "count": len(items)}


class RestoreRequest(BaseModel):
    path: str          # full path inside .recycle
    dest: str = ""     # optional explicit destination; defaults to restoring to share root


@router.post("/recycle/restore")
def recycle_restore(req: RestoreRequest):
    """Move a file out of the recycle bin, restoring it to the share."""
    src = Path(req.path).resolve()
    # Must be under a .recycle directory inside an allowed root
    if ".recycle" not in src.parts:
        raise HTTPException(400, "Path is not inside a recycle bin")
    # Find the .recycle anchor
    parts = src.parts
    recycle_idx = next((i for i, p in enumerate(parts) if p == ".recycle"), None)
    if recycle_idx is None:
        raise HTTPException(400, "Cannot locate .recycle in path")
    share_root = Path(*parts[:recycle_idx])  # e.g. /mnt/user/Movies
    # Relative path after .recycle/<user>/ or .recycle/
    rel_parts = parts[recycle_idx + 1:]
    # Strip leading username component if it looks like one (no extension, single segment)
    if len(rel_parts) > 1 and "." not in rel_parts[0]:
        rel_parts = rel_parts[1:]

    if req.dest:
        dest = Path(req.dest).resolve()
    else:
        dest = share_root / Path(*rel_parts)

    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        # Avoid clobbering: add suffix
        dest = dest.with_name(dest.stem + "_restored" + dest.suffix)
    shutil.move(str(src), str(dest))
    return {"restored_to": str(dest)}


class PurgeRequest(BaseModel):
    path: str = ""   # specific file; empty = purge everything


@router.post("/recycle/purge")
def recycle_purge(req: PurgeRequest):
    """Permanently delete a file from the recycle bin, or purge all."""
    if req.path:
        p = Path(req.path).resolve()
        if ".recycle" not in p.parts:
            raise HTTPException(400, "Path is not inside a recycle bin")
        if not p.exists():
            raise HTTPException(404, "Not found")
        if p.is_dir():
            shutil.rmtree(str(p))
        else:
            p.unlink()
        return {"purged": str(p)}

    # Purge all recycle bins
    purged = []
    for base in ["/mnt/user", "/mnt/cache"]:
        bp = Path(base)
        if not bp.is_dir():
            continue
        try:
            for share_dir in bp.iterdir():
                recycle = share_dir / ".recycle"
                if recycle.is_dir():
                    shutil.rmtree(str(recycle))
                    purged.append(str(recycle))
        except PermissionError:
            pass
    return {"purged_dirs": purged}
