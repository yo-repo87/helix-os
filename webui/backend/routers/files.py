"""
File manager.

Browsing and editing
GET  /api/files/roots                 — quick links: shares, each disk/pool, the flash drive
GET  /api/files/list?path=            — directory listing with owner, permissions, size, location (which disks hold it)
GET  /api/files/download?path=&inline= — download, or view in the browser (images, PDF, audio, video, text)
GET  /api/files/read?path=            — text file contents for the editor (max 2 MB, UTF-8 only)
POST /api/files/write                 — save from the editor (refuses if the file changed on disk meanwhile)
POST /api/files/mkdir | rename | delete
POST /api/files/upload/chunk | upload/abort — chunked, resumable upload
POST /api/files/calc                  — folder sizes, with a per-disk breakdown for user shares
GET  /api/files/search?path=&q=       — find by name (substring or * ? wildcards)
GET  /api/files/owners                — user and group names for the Owner dialog

Jobs (services/filejobs.py): copy, move, delete, owner, permissions. One runs at a time with
progress; more can be queued. See GET/POST /api/files/jobs and /jobs/{id}/{start,cancel}.

Rules (services/fileutil.py): only /mnt and /boot are browsable; nothing is created, renamed or
deleted directly under /mnt, /mnt/user or a disk root, and share folders are managed on the Shares
page; a job cannot mix user-share paths with disk paths.
"""
import fnmatch
import glob
import grp
import mimetypes
import os
import pwd
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path
from typing import List

from fastapi import APIRouter, HTTPException, UploadFile, File, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from services import fileutil as fu, filejobs

router = APIRouter(prefix="/api/files", tags=["files"])

MAX_LIST = 5000
MAX_EDIT = 2 * 1024 * 1024
_NAME_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*\$?|\d+)$")
_INLINE_OK = ("image/", "audio/", "video/", "text/plain", "application/pdf")


class PathRequest(BaseModel):
    path: str


class RenameRequest(BaseModel):
    path: str
    new_name: str


# ── helpers ──────────────────────────────────────────────────────────────────
_ucache: dict = {}
_gcache: dict = {}


def _uname(uid: int) -> str:
    if uid not in _ucache:
        try:
            _ucache[uid] = pwd.getpwuid(uid).pw_name
        except KeyError:
            _ucache[uid] = str(uid)
    return _ucache[uid]


def _gname(gid: int) -> str:
    if gid not in _gcache:
        try:
            _gcache[gid] = grp.getgrgid(gid).gr_name
        except KeyError:
            _gcache[gid] = str(gid)
    return _gcache[gid]


def _disk_roots() -> list[str]:
    out = []
    for pat in ("disk[0-9]*", "cache*"):
        out += [d for d in sorted(glob.glob(os.path.join(fu.MNT, pat))) if os.path.isdir(d)]
    return out


def _entry(p: str, disks: list[str], user_root: str | None) -> dict:
    name = os.path.basename(p)
    try:
        st = os.lstat(p)
    except OSError:
        return {"name": name, "path": p, "is_dir": False, "is_link": False, "size": None, "modified": 0, "ext": "",
                "owner": "", "group": "", "perm": "", "mode": "", "location": []}
    link = stat.S_ISLNK(st.st_mode)
    if link:
        try:
            st = os.stat(p)
        except OSError:
            pass                                                  # broken link: show the link itself
    is_dir = stat.S_ISDIR(st.st_mode)
    e = {
        "name": name, "path": p, "is_dir": is_dir, "is_link": link,
        "size": None if is_dir else st.st_size, "modified": int(st.st_mtime),
        "ext": "" if is_dir else os.path.splitext(name)[1].lower(),
        "owner": _uname(st.st_uid), "group": _gname(st.st_gid),
        "perm": stat.filemode(st.st_mode)[1:], "mode": format(stat.S_IMODE(st.st_mode), "04o"),
        "location": [],
    }
    if user_root and disks:
        rel = os.path.relpath(p, user_root)
        e["location"] = [os.path.basename(d) for d in disks if os.path.lexists(os.path.join(d, rel))]
    return e


def _need_dir(p: str) -> None:
    if not os.path.isdir(p):
        raise HTTPException(404, f"Folder not found: {p}")


def _not_top(p: str, what: str, allow_share: bool = False) -> None:
    lv = fu.level(p)
    if lv == "root" or (lv == "share" and not allow_share):
        raise HTTPException(403, f"Cannot {what} {os.path.basename(p) or p} here: folders at this level "
                                 "are shares and disks. Manage shares on the Shares page.")


# ── browsing ─────────────────────────────────────────────────────────────────
@router.get("/roots")
def roots():
    out = []
    mnt = fu.MNT
    if os.path.isdir(os.path.join(mnt, "user")):
        out.append({"name": "Shares", "path": os.path.join(mnt, "user"), "kind": "user"})
    for d in _disk_roots():
        out.append({"name": os.path.basename(d), "path": d, "kind": "disk"})
    out.append({"name": "/mnt", "path": mnt, "kind": "root"})
    if os.path.isdir(fu.BOOT):
        out.append({"name": "Flash (/boot)", "path": fu.BOOT, "kind": "boot"})
    return out


@router.get("/list")
def list_dir(path: str = ""):
    p = fu.safe(path or fu.default_root())
    _need_dir(p)
    disks = _disk_roots()
    user_root = os.path.join(os.path.realpath(fu.MNT), "user")
    user_root = user_root if (p == user_root or p.startswith(user_root + "/")) else None
    try:
        names = os.listdir(p)
    except PermissionError:
        raise HTTPException(403, "Permission denied")
    truncated = len(names) > MAX_LIST
    entries = [_entry(os.path.join(p, n), disks, user_root) for n in sorted(names, key=str.lower)[:MAX_LIST]]
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))

    crumbs = []
    for r in map(os.path.realpath, fu.ROOTS):
        if p == r or p.startswith(r + "/"):
            crumbs.append({"name": r, "path": r})
            cur = r
            for part in (os.path.relpath(p, r).split(os.sep) if p != r else []):
                cur = os.path.join(cur, part)
                crumbs.append({"name": part, "path": cur})
            break
    is_root = any(p == os.path.realpath(r) for r in fu.ROOTS)
    return {
        "path": p, "parent": None if is_root else os.path.dirname(p), "crumbs": crumbs,
        "entries": entries, "truncated": truncated, "level": fu.level(p), "kind": fu.classify(p),
        "can_create": fu.level(p) != "root",
        "child_level": fu.level(os.path.join(p, "x")),      # "inner" = items here may be renamed/moved/deleted
    }


@router.get("/download")
def download_file(path: str, inline: bool = False):
    p = fu.safe(path)
    if not os.path.isfile(p):
        raise HTTPException(404, "File not found")
    mt = mimetypes.guess_type(p)[0] or "application/octet-stream"
    headers = {"X-Content-Type-Options": "nosniff"}
    if inline and mt.startswith(_INLINE_OK):
        headers["Content-Security-Policy"] = "sandbox"            # a viewed SVG/HTML can never run scripts here
        return FileResponse(p, media_type=mt, headers=headers, content_disposition_type="inline")
    return FileResponse(p, filename=os.path.basename(p), media_type="application/octet-stream", headers=headers)


# ── editor ───────────────────────────────────────────────────────────────────
@router.get("/read")
def read_text(path: str):
    p = fu.safe(path)
    if not os.path.isfile(p):
        raise HTTPException(404, "File not found")
    st = os.stat(p)
    if st.st_size > MAX_EDIT:
        raise HTTPException(413, f"File is larger than {MAX_EDIT // 1048576} MB; download it instead.")
    try:
        with open(p, "rb") as f:
            raw = f.read()
    except PermissionError:
        raise HTTPException(403, "Permission denied")
    if b"\0" in raw[:8192]:
        raise HTTPException(415, "This looks like a binary file, not text.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(415, "This file is not UTF-8 text.")
    return {"path": p, "content": text, "mtime": st.st_mtime, "size": st.st_size,
            "writable": os.access(p, os.W_OK)}


class WriteRequest(BaseModel):
    path: str
    content: str
    mtime: float | None = None


@router.post("/write")
def write_text(req: WriteRequest):
    p = fu.safe(req.path)
    if not os.path.isfile(p):
        raise HTTPException(404, "File not found")
    st = os.stat(p)
    if req.mtime is not None and abs(st.st_mtime - req.mtime) > 0.01:
        raise HTTPException(409, "This file was changed on the server since you opened it. Reload it, or save a copy elsewhere.")
    data = req.content.encode("utf-8")
    if len(data) > MAX_EDIT:
        raise HTTPException(413, "Too large to save from the editor")
    d = os.path.dirname(p)
    try:
        if os.access(d, os.W_OK) and st.st_uid == os.geteuid():
            tmp = os.path.join(d, f".{os.path.basename(p)}.helixtmp")
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.chmod(tmp, stat.S_IMODE(st.st_mode))
            except OSError:
                pass                                              # FAT has no modes
            os.replace(tmp, p)
        else:
            with open(p, "wb") as f:                              # keeps owner/inode when we can't replace the file
                f.write(data)
    except PermissionError:
        raise HTTPException(403, "Permission denied: this file is not writable by the web service. Use Owner/Permission to change that.")
    except OSError as e:
        raise HTTPException(500, f"Could not save: {e.strerror or e}")
    return {"saved": p, "mtime": os.stat(p).st_mtime, "size": len(data)}


# ── simple operations ────────────────────────────────────────────────────────
@router.post("/mkdir")
def make_dir(req: PathRequest):
    p = fu.safe(req.path, follow=False)
    fu.check_name(os.path.basename(p))
    parent = os.path.dirname(p)
    if fu.level(parent) == "root":
        raise HTTPException(403, "Open a share first. New top-level folders are created on the Shares page.")
    _need_dir(parent)
    if os.path.lexists(p):
        raise HTTPException(400, "Already exists")
    try:
        os.mkdir(p)
    except PermissionError:
        raise HTTPException(403, "Permission denied")
    return {"created": p}


@router.post("/delete")
def delete_path(req: PathRequest):
    p = fu.safe(req.path, follow=False)
    _not_top(p, "delete")
    if not os.path.lexists(p):
        raise HTTPException(404, "Not found")
    try:
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p)
        else:
            os.unlink(p)
    except PermissionError:
        raise HTTPException(403, "Permission denied")
    return {"deleted": p}


@router.post("/rename")
def rename_path(req: RenameRequest):
    p = fu.safe(req.path, follow=False)
    _not_top(p, "rename")
    if not os.path.lexists(p):
        raise HTTPException(404, "Not found")
    new_p = os.path.join(os.path.dirname(p), fu.check_name(req.new_name))
    if os.path.lexists(new_p):
        raise HTTPException(409, f"'{os.path.basename(new_p)}' already exists here")
    try:
        os.rename(p, new_p)
    except PermissionError:
        raise HTTPException(403, "Permission denied")
    return {"renamed_to": new_p}


# ── upload (chunked, resumable) ──────────────────────────────────────────────
def _append(part: str, offset: int, body: bytes) -> None:
    with open(part, "wb" if offset == 0 else "r+b") as f:
        if offset:
            f.seek(offset)
        f.write(body)


@router.post("/upload/chunk")
async def upload_chunk(request: Request, path: str, name: str, offset: int = 0, last: bool = False,
                       overwrite: bool = False):
    """Raw body = one chunk at `offset`. The partial file is `.<name>.helixpart`; `last=true`
    renames it into place. A wrong offset answers 409 with {have: N} so the client can resume."""
    d = fu.safe(path)
    _need_dir(d)
    if fu.level(d) == "root":
        raise HTTPException(403, "Open a share first. Files cannot be uploaded directly into /mnt or a disk root.")
    name = fu.check_name(name)
    final = os.path.join(d, name)
    part = os.path.join(d, f".{name}.helixpart")
    if offset == 0 and os.path.lexists(final) and not overwrite:
        raise HTTPException(409, f"'{name}' already exists")
    if os.path.isdir(final):
        raise HTTPException(409, f"'{name}' is a folder")
    have = os.path.getsize(part) if os.path.exists(part) else 0
    if offset > 0 and have != offset:
        return JSONResponse({"detail": "offset mismatch", "have": have}, status_code=409)
    body = await request.body()
    try:
        await run_in_threadpool(_append, part, offset, body)
        if last:
            os.replace(part, final)
    except PermissionError:
        raise HTTPException(403, "Permission denied")
    except OSError as e:
        raise HTTPException(507 if e.errno == 28 else 500, f"Upload failed: {e.strerror or e}")
    return {"received": offset + len(body), "done": last}


class AbortRequest(BaseModel):
    path: str
    name: str


@router.post("/upload/abort")
def upload_abort(req: AbortRequest):
    d = fu.safe(req.path)
    part = os.path.join(d, f".{fu.check_name(req.name)}.helixpart")
    try:
        os.unlink(part)
    except FileNotFoundError:
        pass
    return {"removed": part}


@router.post("/upload")
async def upload_files(path: str = "", files: List[UploadFile] = File(...)):
    """Whole-file upload kept for scripts; the web UI uses /upload/chunk."""
    d = fu.safe(path or fu.default_root())
    _need_dir(d)
    if fu.level(d) == "root":
        raise HTTPException(403, "Open a share first.")
    saved = []
    for f in files:
        dest = os.path.join(d, fu.check_name(os.path.basename(f.filename or "")))
        with open(dest, "wb") as out:
            shutil.copyfileobj(f.file, out)
        saved.append(dest)
    return {"saved": saved}


# ── size / search / owners ───────────────────────────────────────────────────
class CalcRequest(BaseModel):
    paths: List[str]


def _du(p: str, timeout: int = 300) -> int | None:
    try:
        r = subprocess.run(["du", "-sb", "--", p], capture_output=True, text=True, timeout=timeout)
        return int(r.stdout.split()[0]) if r.stdout.strip() else None
    except (subprocess.TimeoutExpired, ValueError, IndexError, FileNotFoundError):
        return None


@router.post("/calc")
def calc_sizes(req: CalcRequest):
    """Size of each item (a folder is summed). Items under /mnt/user also get a per-disk breakdown."""
    disks = _disk_roots()
    user_root = os.path.join(os.path.realpath(fu.MNT), "user")
    out = []
    for raw in req.paths[:200]:
        p = fu.safe(raw, follow=False)
        r = {"path": p, "bytes": _du(p), "disks": {}}
        if (p == user_root or p.startswith(user_root + "/")) and p != user_root:
            rel = os.path.relpath(p, user_root)
            for d in disks:
                dp = os.path.join(d, rel)
                if os.path.lexists(dp):
                    r["disks"][os.path.basename(d)] = _du(dp)
        out.append(r)
    return {"results": out}


@router.get("/search")
def search(path: str, q: str, limit: int = 500):
    base = fu.safe(path)
    _need_dir(base)
    q = (q or "").strip().lower()
    if not q:
        raise HTTPException(400, "Type something to search for")
    wild = any(c in q for c in "*?[")
    deadline = time.time() + 20
    disks = _disk_roots()
    user_root = os.path.join(os.path.realpath(fu.MNT), "user")
    user_root = user_root if (base == user_root or base.startswith(user_root + "/")) else None
    hits, timed_out = [], False
    for dp, dn, fn in os.walk(base, followlinks=False):
        for n in dn + fn:
            if (fnmatch.fnmatch(n.lower(), q) if wild else q in n.lower()):
                hits.append(_entry(os.path.join(dp, n), disks, user_root))
                if len(hits) >= limit:
                    break
        if len(hits) >= limit:
            break
        if time.time() > deadline:
            timed_out = True
            break
    return {"path": base, "query": q, "entries": hits, "truncated": len(hits) >= limit, "timed_out": timed_out}


@router.get("/owners")
def owners():
    users = sorted({u.pw_name for u in pwd.getpwall() if u.pw_uid == 0 or 1000 <= u.pw_uid < 60000 or u.pw_name == "nobody"})
    groups = sorted({g.gr_name for g in grp.getgrall() if g.gr_gid == 0 or 100 <= g.gr_gid < 60000 or g.gr_name in ("nogroup", "users")})
    return {"users": users, "groups": groups}


# ── jobs ─────────────────────────────────────────────────────────────────────
class JobRequest(BaseModel):
    action: str                       # copy | move | delete | chown | chmod
    paths: List[str]
    target: str = ""
    overwrite: bool = False
    sparse: bool = False
    queue: bool = False
    # chown
    owner: str = ""
    group: str = ""
    # chmod: each of owner/group/other is a subset of "rwx"
    perm_owner: str = ""
    perm_group: str = ""
    perm_other: str = ""
    recursive: bool = False


@router.get("/jobs")
def jobs_list():
    return {"jobs": filejobs.list_jobs()}


@router.post("/jobs")
def jobs_create(req: JobRequest):
    act = req.action
    if act not in ("copy", "move", "delete", "chown", "chmod"):
        raise HTTPException(400, f"Unknown action {act!r}")
    if not req.paths:
        raise HTTPException(400, "Nothing selected")
    srcs = []
    for raw in req.paths:
        p = fu.safe(raw, follow=False)
        if p not in srcs:
            srcs.append(p)
    for p in srcs:
        if not os.path.lexists(p):
            raise HTTPException(404, f"Not found: {p}")
        if act in ("delete", "move"):
            _not_top(p, "delete" if act == "delete" else "move")
        else:
            _not_top(p, act, allow_share=True)
    target, params = "", {}
    if act in ("copy", "move"):
        if not req.target:
            raise HTTPException(400, "Choose a destination folder")
        target = fu.safe(req.target)
        tl = fu.level(target)
        if tl == "root":
            raise HTTPException(400, "Choose a share, or a folder inside one, as the destination.")
        if not os.path.isdir(target):
            if os.path.lexists(target):
                raise HTTPException(400, "The destination is a file, not a folder")
            if tl == "share":
                raise HTTPException(400, "That share does not exist. Create shares on the Shares page.")
        fu.check_mixed(srcs + [target])
    elif act == "chown":
        o, g = req.owner.strip(), req.group.strip()
        if not _NAME_RE.match(o) or not _NAME_RE.match(g):
            raise HTTPException(400, "Owner and group must be valid user and group names")
        params = {"owner": o, "group": g, "recursive": req.recursive}
    elif act == "chmod":
        bits = []
        for k, v in (("u", req.perm_owner), ("g", req.perm_group), ("o", req.perm_other)):
            v = v or ""
            if not re.fullmatch(r"[rwx]*", v):
                raise HTTPException(400, "Permissions can only contain r, w and x")
            bits.append(f"{k}={v}")
        # +X keeps folders (and files that were executable) reachable, like Unraid's permission dialog
        params = {"symbolic": ",".join(bits) + ",ugo+X", "recursive": req.recursive}
    job = filejobs.create(act, srcs, target, req.overwrite, req.sparse, req.queue, params)
    return job


@router.post("/jobs/{job_id}/start")
def job_start(job_id: str):
    try:
        return filejobs.start(job_id)
    except KeyError:
        raise HTTPException(404, "No such job")
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.post("/jobs/{job_id}/cancel")
def job_cancel(job_id: str):
    try:
        return filejobs.cancel(job_id)
    except KeyError:
        raise HTTPException(404, "No such job")


@router.delete("/jobs/{job_id}")
def job_delete(job_id: str):
    try:
        filejobs.delete(job_id)
    except KeyError:
        raise HTTPException(404, "No such job")
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


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
