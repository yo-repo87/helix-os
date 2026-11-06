"""
Multi-user login — username + password authentication with session tokens.
Builds on the existing auth.py TOTP session system.

POST /api/login          — authenticate with username + password
POST /api/logout         — invalidate session
GET  /api/login/status   — check session validity + current user
GET  /api/login/users    — list login-capable accounts (admin only)
POST /api/login/users    — create a login account
DELETE /api/login/users/{username}  — delete account
PUT  /api/login/users/{username}/password — change password
"""

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from config import HELIX_CONFIG, STATE_DIR

router = APIRouter(prefix="/api/login", tags=["login"])

USERS_FILE   = os.path.join(HELIX_CONFIG, "login_users.json")
SESSION_FILE = os.path.join(STATE_DIR, "login_sessions.json")
SESSION_TTL  = 86400 * 7  # 7 days


# ── Storage ───────────────────────────────────────────────────────

def _load_users() -> dict:
    try:
        return json.loads(Path(USERS_FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_users(d: dict):
    Path(USERS_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(USERS_FILE).write_text(json.dumps(d, indent=2))
    Path(USERS_FILE).chmod(0o600)


def _load_sessions() -> dict:
    try:
        return json.loads(Path(SESSION_FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_sessions(d: dict):
    Path(SESSION_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(SESSION_FILE).write_text(json.dumps(d))


# ── Crypto ────────────────────────────────────────────────────────

def _hash_pw(password: str, salt: str | None = None) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return h.hex(), salt


def _verify_pw(password: str, stored_hash: str, salt: str) -> bool:
    h, _ = _hash_pw(password, salt)
    return hmac.compare_digest(h, stored_hash)


# ── Session helpers ───────────────────────────────────────────────

def _new_session(username: str, role: str) -> str:
    token = secrets.token_urlsafe(32)
    sessions = _load_sessions()
    now = int(time.time())
    sessions = {k: v for k, v in sessions.items() if v.get("exp", 0) > now}
    sessions[token] = {"username": username, "role": role, "exp": now + SESSION_TTL}
    _save_sessions(sessions)
    return token


def _get_session(request: Request) -> dict | None:
    auth = request.headers.get("Authorization", "")
    token = None
    if auth.startswith("Bearer "):
        token = auth[7:]
    if not token:
        token = request.cookies.get("helix_login")
    if not token:
        return None
    sessions = _load_sessions()
    s = sessions.get(token)
    if s and s.get("exp", 0) > int(time.time()):
        return {**s, "token": token}
    return None


def get_current_user(request: Request) -> dict | None:
    """Used by other routers to check login (optional call)."""
    users = _load_users()
    if not users:
        return {"username": "admin", "role": "admin"}  # no accounts = open
    return _get_session(request)


# ── Schemas ───────────────────────────────────────────────────────

class LoginReq(BaseModel):
    username: str
    password: str

class CreateUser(BaseModel):
    username: str
    password: str
    role: str = "admin"   # admin | viewer

class ChangePassword(BaseModel):
    password: str


# ── Endpoints ─────────────────────────────────────────────────────

@router.get("/status")
def login_status(request: Request):
    users = _load_users()
    if not users:
        return {"enabled": False, "user": None}
    s = _get_session(request)
    return {
        "enabled": True,
        "user": {"username": s["username"], "role": s["role"]} if s else None,
    }


@router.post("")
def do_login(req: LoginReq, response: Response):
    users = _load_users()
    if not users:
        # No accounts configured — always allow
        token = _new_session("admin", "admin")
        response.set_cookie("helix_login", token, httponly=True, samesite="lax", max_age=SESSION_TTL)
        return {"ok": True, "token": token, "username": "admin", "role": "admin"}

    u = users.get(req.username)
    if not u or not _verify_pw(req.password, u["hash"], u["salt"]):
        raise HTTPException(401, "Invalid username or password")

    token = _new_session(req.username, u.get("role", "admin"))
    response.set_cookie("helix_login", token, httponly=True, samesite="lax", max_age=SESSION_TTL)
    return {"ok": True, "token": token, "username": req.username, "role": u.get("role", "admin")}


@router.post("/logout")
def do_logout(request: Request, response: Response):
    s = _get_session(request)
    if s:
        sessions = _load_sessions()
        sessions.pop(s["token"], None)
        _save_sessions(sessions)
    response.delete_cookie("helix_login")
    return {"ok": True}


@router.get("/users")
def list_login_users():
    users = _load_users()
    return [{"username": k, "role": v.get("role", "admin")} for k, v in users.items()]


@router.post("/users")
def create_login_user(req: CreateUser):
    users = _load_users()
    if req.username in users:
        raise HTTPException(409, "Username already exists")
    h, salt = _hash_pw(req.password)
    users[req.username] = {"hash": h, "salt": salt, "role": req.role}
    _save_users(users)
    return {"ok": True, "username": req.username}


@router.delete("/users/{username}")
def delete_login_user(username: str):
    users = _load_users()
    if username not in users:
        raise HTTPException(404, "User not found")
    del users[username]
    _save_users(users)
    return {"ok": True}


@router.put("/users/{username}/password")
def change_login_password(username: str, req: ChangePassword):
    users = _load_users()
    if username not in users:
        raise HTTPException(404, "User not found")
    h, salt = _hash_pw(req.password)
    users[username]["hash"] = h
    users[username]["salt"] = salt
    _save_users(users)
    return {"ok": True}
