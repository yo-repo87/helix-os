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
import json
import pwd
import subprocess
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import HELIX_CONFIG
from services import privileged as priv
from services import validation

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
    username = req.username.strip()
    err = (validation.username(username) or validation.user_description(req.description)
           or validation.password(req.password))
    if err:
        raise HTTPException(400, err)
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

    r = priv.run(cmd)
    if r.returncode != 0:
        raise HTTPException(500, f"useradd failed: {r.stderr.strip()}")

    if req.password:
        _set_password(username, req.password)

    _persist()
    return get_user(username)


@router.delete("/{username}")
def delete_user(username: str, remove_home: bool = False):
    try:
        pwd.getpwnam(username)
    except KeyError:
        raise HTTPException(404, f"User '{username}' not found")

    cmd = ["userdel"]
    if remove_home:
        cmd.append("-r")
    cmd.append(username)

    r = priv.run(cmd)
    if r.returncode != 0:
        raise HTTPException(500, f"userdel failed: {r.stderr.strip()}")
    priv.run(["smbpasswd", "-x", username])        # best effort: drop the Samba entry too
    _persist()
    return {"ok": True, "message": f"User '{username}' deleted"}


# ── Passwords ─────────────────────────────────────────────────────────────────

def _set_password(username: str, password: str) -> None:
    r = priv.run(["chpasswd"], input=f"{username}:{password}")
    if r.returncode != 0:
        raise HTTPException(500, f"chpasswd failed: {r.stderr.strip()}")


class PasswordUpdate(BaseModel):
    password: str


@router.put("/{username}/password")
def set_password(username: str, req: PasswordUpdate):
    try:
        pwd.getpwnam(username)
    except KeyError:
        raise HTTPException(404, f"User '{username}' not found")
    if not req.password:
        raise HTTPException(400, "Password cannot be empty")
    if validation.password(req.password):
        raise HTTPException(400, validation.password(req.password))
    _set_password(username, req.password)
    _persist()
    return {"ok": True}


@router.post("/{username}/samba-password")
def set_samba_password(username: str, req: PasswordUpdate):
    try:
        pwd.getpwnam(username)
    except KeyError:
        raise HTTPException(404, f"User '{username}' not found")
    if not req.password:
        raise HTTPException(400, "Password cannot be empty")

    # smbpasswd -a adds or updates; stdin: "password\npassword\n"
    r = priv.run(["smbpasswd", "-a", "-s", username], input=f"{req.password}\n{req.password}\n")
    if r.returncode != 0:
        raise HTTPException(500, f"smbpasswd failed: {r.stderr.strip()}")
    _persist()
    return {"ok": True}


# ── SSH authorized keys ───────────────────────────────────────────────────────

def _authorized_keys_path(username: str) -> Path:
    try:
        pw = pwd.getpwnam(username)
    except KeyError:
        raise HTTPException(404, f"User '{username}' not found")
    return Path(pw.pw_dir) / ".ssh" / "authorized_keys"


def _read_keys(username: str) -> list[str]:
    text = priv.read_file(_authorized_keys_path(username))      # homes are 0700: needs root
    return [l for l in (text or "").splitlines() if l.strip() and not l.startswith("#")]


def _write_keys(pw, keys: list[str]) -> None:
    ssh_dir = Path(pw.pw_dir) / ".ssh"
    priv.run(["install", "-d", "-m", "700", "-o", str(pw.pw_uid), "-g", str(pw.pw_gid), str(ssh_dir)])
    priv.write_file(ssh_dir / "authorized_keys", "\n".join(keys) + ("\n" if keys else ""),
                    mode="600", owner=f"{pw.pw_uid}:{pw.pw_gid}")


# ── Persistence ───────────────────────────────────────────────────────────────
# /etc/passwd, /etc/shadow and Samba's database live in the RAM root and vanish on
# reboot. Record the accounts + password hashes in the persistent config; the
# helix-restore-state script rebuilds them at boot (like Unraid's flash copies).

def _persist() -> None:
    try:
        users = {}
        for p in pwd.getpwall():
            if p.pw_uid < 1000 or p.pw_uid >= 65534 or p.pw_name in ("helix", "nobody"):
                continue
            sh = priv.run(["getent", "shadow", p.pw_name]).stdout.strip().split(":")
            users[p.pw_name] = {"uid": p.pw_uid, "gid": p.pw_gid, "shell": p.pw_shell,
                                "gecos": p.pw_gecos, "home": p.pw_dir,
                                "hash": sh[1] if len(sh) > 1 else "",
                                "ssh_keys": [l for l in (priv.read_file(Path(p.pw_dir) / ".ssh" / "authorized_keys") or "").splitlines()
                                             if l.strip() and not l.startswith("#")]}
        cfg = Path(HELIX_CONFIG)
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "users.json").write_text(json.dumps(users, indent=2))
        r = priv.run(["pdbedit", "-L", "-w"])
        if r.returncode == 0:
            (cfg / "smbpasswd").write_text(r.stdout)
    except OSError:
        pass          # best effort: a read-only config must not break account management


@router.get("/{username}/ssh-keys")
def list_ssh_keys(username: str):
    keys = _read_keys(username)
    return [{"index": i, "key": k} for i, k in enumerate(keys)]


class SSHKeyAdd(BaseModel):
    key: str


@router.post("/{username}/ssh-keys")
def add_ssh_key(username: str, req: SSHKeyAdd):
    key = req.key.strip()
    if not key.startswith(("ssh-rsa", "ssh-ed25519", "ssh-dss", "ecdsa-sha2", "sk-")):
        raise HTTPException(400, "Does not look like a valid public key")

    pw = None
    try:
        pw = pwd.getpwnam(username)
    except KeyError:
        raise HTTPException(404, f"User '{username}' not found")

    existing = _read_keys(username)
    if key in existing:
        raise HTTPException(409, "Key already present")
    _write_keys(pw, existing + [key])
    _persist()
    return list_ssh_keys(username)


@router.delete("/{username}/ssh-keys/{index}")
def remove_ssh_key(username: str, index: int):
    keys = _read_keys(username)
    if index < 0 or index >= len(keys):
        raise HTTPException(404, "Key index out of range")
    keys.pop(index)
    _write_keys(pwd.getpwnam(username), keys)
    _persist()
    return list_ssh_keys(username)
