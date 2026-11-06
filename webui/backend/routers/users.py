"""
Local user management.

GET  /api/users/                   — list users (UID ≥ 1000, non-system)
POST /api/users/                   — create user
DELETE /api/users/{username}       — delete user
PUT  /api/users/{username}/password — change system password
POST /api/users/{username}/samba-password — set Samba password
GET  /api/users/{username}/ssh-keys — list SSH authorized keys
POST /api/users/{username}/ssh-keys — add SSH authorized key
DELETE /api/users/{username}/ssh-keys/{index} — remove SSH key by index

All write operations require the backend to be running as root (or with sudo).
In demo / non-root mode the GET endpoints still work (read /etc/passwd),
write endpoints return 503 with a clear message.
"""

import os
import pwd
import subprocess
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/users", tags=["users"])

_IS_ROOT = os.geteuid() == 0


def _require_root():
    if not _IS_ROOT:
        raise HTTPException(503, "User management requires the backend to run as root")


# ── User listing ──────────────────────────────────────────────────────────────

def _user_info(pw_entry) -> dict:
    return {
        "username": pw_entry.pw_name,
        "uid":      pw_entry.pw_uid,
        "gid":      pw_entry.pw_gid,
        "gecos":    pw_entry.pw_gecos,
        "home":     pw_entry.pw_dir,
        "shell":    pw_entry.pw_shell,
    }


@router.get("/")
def list_users():
    users = []
    for pw in pwd.getpwall():
        if pw.pw_uid < 1000:
            continue
        if pw.pw_name in ("nobody",):
            continue
        users.append(_user_info(pw))
    return sorted(users, key=lambda u: u["username"])


@router.get("/{username}")
def get_user(username: str):
    try:
        pw = pwd.getpwnam(username)
    except KeyError:
        raise HTTPException(404, f"User '{username}' not found")
    return _user_info(pw)


# ── Create / delete ───────────────────────────────────────────────────────────

class UserCreate(BaseModel):
    username:    str
    password:    str = ""
    description: str = ""
    shell:       str = "/bin/bash"
    create_home: bool = True


@router.post("/")
def create_user(req: UserCreate):
    _require_root()
    username = req.username.strip()
    if not username or not username.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(400, "Invalid username (alphanumeric, hyphens, underscores only)")
    try:
        pwd.getpwnam(username)
        raise HTTPException(409, f"User '{username}' already exists")
    except KeyError:
        pass

    cmd = ["useradd", "-s", req.shell]
    if req.create_home:
        cmd += ["-m"]
    if req.description:
        cmd += ["-c", req.description]
    cmd.append(username)

    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise HTTPException(500, f"useradd failed: {r.stderr.strip()}")

    if req.password:
        _set_password(username, req.password)

    return get_user(username)


@router.delete("/{username}")
def delete_user(username: str, remove_home: bool = False):
    _require_root()
    try:
        pwd.getpwnam(username)
    except KeyError:
        raise HTTPException(404, f"User '{username}' not found")

    cmd = ["userdel"]
    if remove_home:
        cmd.append("-r")
    cmd.append(username)

    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise HTTPException(500, f"userdel failed: {r.stderr.strip()}")
    return {"ok": True, "message": f"User '{username}' deleted"}


# ── Passwords ─────────────────────────────────────────────────────────────────

def _set_password(username: str, password: str) -> None:
    r = subprocess.run(
        ["chpasswd"],
        input=f"{username}:{password}",
        capture_output=True, text=True
    )
    if r.returncode != 0:
        raise HTTPException(500, f"chpasswd failed: {r.stderr.strip()}")


class PasswordUpdate(BaseModel):
    password: str


@router.put("/{username}/password")
def set_password(username: str, req: PasswordUpdate):
    _require_root()
    try:
        pwd.getpwnam(username)
    except KeyError:
        raise HTTPException(404, f"User '{username}' not found")
    if not req.password:
        raise HTTPException(400, "Password cannot be empty")
    _set_password(username, req.password)
    return {"ok": True}


@router.post("/{username}/samba-password")
def set_samba_password(username: str, req: PasswordUpdate):
    _require_root()
    try:
        pwd.getpwnam(username)
    except KeyError:
        raise HTTPException(404, f"User '{username}' not found")
    if not req.password:
        raise HTTPException(400, "Password cannot be empty")

    # smbpasswd -a adds or updates; stdin: "password\npassword\n"
    r = subprocess.run(
        ["smbpasswd", "-a", "-s", username],
        input=f"{req.password}\n{req.password}\n",
        capture_output=True, text=True
    )
    if r.returncode != 0:
        raise HTTPException(500, f"smbpasswd failed: {r.stderr.strip()}")
    return {"ok": True}


# ── SSH authorized keys ───────────────────────────────────────────────────────

def _authorized_keys_path(username: str) -> Path:
    try:
        pw = pwd.getpwnam(username)
    except KeyError:
        raise HTTPException(404, f"User '{username}' not found")
    return Path(pw.pw_dir) / ".ssh" / "authorized_keys"


def _read_keys(username: str) -> list[str]:
    p = _authorized_keys_path(username)
    try:
        return [l for l in p.read_text().splitlines() if l.strip() and not l.startswith("#")]
    except FileNotFoundError:
        return []


@router.get("/{username}/ssh-keys")
def list_ssh_keys(username: str):
    keys = _read_keys(username)
    return [{"index": i, "key": k} for i, k in enumerate(keys)]


class SSHKeyAdd(BaseModel):
    key: str


@router.post("/{username}/ssh-keys")
def add_ssh_key(username: str, req: SSHKeyAdd):
    _require_root()
    key = req.key.strip()
    if not key.startswith(("ssh-rsa", "ssh-ed25519", "ssh-dss", "ecdsa-sha2", "sk-")):
        raise HTTPException(400, "Does not look like a valid public key")

    pw = None
    try:
        pw = pwd.getpwnam(username)
    except KeyError:
        raise HTTPException(404, f"User '{username}' not found")

    ssh_dir = Path(pw.pw_dir) / ".ssh"
    ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    keys_file = ssh_dir / "authorized_keys"

    existing = _read_keys(username)
    if key in existing:
        raise HTTPException(409, "Key already present")

    with keys_file.open("a") as f:
        f.write(key + "\n")

    # Fix ownership if running as root
    if _IS_ROOT:
        os.chown(ssh_dir, pw.pw_uid, pw.pw_gid)
        os.chown(keys_file, pw.pw_uid, pw.pw_gid)

    return list_ssh_keys(username)


@router.delete("/{username}/ssh-keys/{index}")
def remove_ssh_key(username: str, index: int):
    _require_root()
    keys = _read_keys(username)
    if index < 0 or index >= len(keys):
        raise HTTPException(404, "Key index out of range")
    keys.pop(index)
    p = _authorized_keys_path(username)
    p.write_text("\n".join(keys) + ("\n" if keys else ""))
    return list_ssh_keys(username)
