"""Path rules for the file manager (shared by routers/files.py and services/filejobs.py).

Browsable roots are /mnt (user shares, array disks, cache pools) and /boot (the flash/config
volume), like Unraid's file manager. Everything is resolved through realpath so a symlink
inside a share can never lead out of the roots.

Levels, counted below /mnt:
  root  /mnt, /mnt/user, /mnt/disk1, /mnt/cache1 ...   (nothing may be created, renamed or deleted here)
  share /mnt/user/<share>, /mnt/disk1/<share>          (a share folder: manage it on the Shares page)
  inner anything deeper; /boot itself counts as a share, everything inside it as inner
"""
import os
import re

from fastapi import HTTPException

MNT = os.getenv("FILES_MNT", "/mnt")
BOOT = os.getenv("FILES_BOOT", "/boot")
ROOTS = [MNT, BOOT]

_NAME_BAD = re.compile(r"[\x00/]")


def _under(p: str, root: str) -> bool:
    return p == root or p.startswith(root.rstrip("/") + "/")


def default_root() -> str:
    user = os.path.join(MNT, "user")
    return user if os.path.isdir(user) else MNT


def safe(path: str, follow: bool = True) -> str:
    """Absolute, resolved path that is inside a browsable root, else HTTP 400/403.
    follow=False keeps the final component unresolved (so a symlink itself can be renamed or removed)."""
    if not path:
        path = default_root()
    if "\0" in path:
        raise HTTPException(403, "Path not allowed")
    if not path.startswith("/"):
        raise HTTPException(400, f"Path must be absolute: {path!r}")
    path = os.path.normpath(path)
    if follow or path == "/":
        rp = os.path.realpath(path)
    else:
        rp = os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))
    if not any(_under(rp, os.path.realpath(r)) for r in ROOTS):
        raise HTTPException(403, f"Path not allowed: {path}")
    return rp


def level(p: str) -> str:
    mnt = os.path.realpath(MNT)
    boot = os.path.realpath(BOOT)
    if _under(p, mnt):
        rel = os.path.relpath(p, mnt)
        depth = 0 if rel == "." else len(rel.split(os.sep))
        return "root" if depth <= 1 else ("share" if depth == 2 else "inner")
    if p == boot:
        return "share"        # the flash drive itself cannot be renamed/deleted, but files can be created inside it
    return "inner"


def classify(p: str) -> str:
    """'user' (merged share view), 'disk' (a physical disk or pool) or 'other'."""
    mnt = os.path.realpath(MNT)
    if not _under(p, mnt) or p == mnt:
        return "other"
    first = os.path.relpath(p, mnt).split(os.sep)[0]
    if first == "user":
        return "user"
    if re.match(r"^(disk\d+|cache\d*)$", first):
        return "disk"
    return "other"


def check_name(name: str) -> str:
    name = (name or "").strip()
    if not name or name in (".", "..") or _NAME_BAD.search(name) or len(name.encode()) > 255:
        raise HTTPException(400, "Names cannot be empty or contain '/'")
    return name


def check_mixed(paths) -> None:
    kinds = {classify(p) for p in paths}
    if "user" in kinds and "disk" in kinds:
        raise HTTPException(400, "A user share path and a disk path cannot be mixed in one job; "
                                 "copying between them can lose data. Use /mnt/user paths for both, or disk paths for both.")
