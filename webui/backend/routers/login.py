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
import re
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

def _new_session(username: str, role: str, ua: str = "") -> str:
    token = secrets.token_urlsafe(32)
    sessions = _load_sessions()
    now = int(time.time())
    sessions = {k: v for k, v in sessions.items() if v.get("exp", 0) > now}
    sessions[token] = {
        "username": username, "role": role, "exp": now + SESSION_TTL,
        "created": now, "ua": (ua or "")[:160], "sid": secrets.token_hex(4),
    }
    _save_sessions(sessions)
    return token


def _set_login_cookie(response: Response, request: Request, token: str) -> None:
    from services import authz
    response.set_cookie(
        "helix_login", token, httponly=True, samesite="lax", path="/",
        secure=authz.request_is_https(request), max_age=SESSION_TTL,
    )


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
    totp: str | None = None

class CreateUser(BaseModel):
    username: str
    password: str
    role: str = "admin"   # admin | viewer

class ChangePassword(BaseModel):
    password: str


# ── Endpoints ─────────────────────────────────────────────────────

@router.get("/status")
def login_status(request: Request):
    from services import authz, passkeys
    users = _load_users()
    session = _get_session(request)
    return {
        "enabled": bool(users),
        "enforced": authz.login_enforced(),
        "reason": authz.enforcement_reason(),
        "user": {"username": session["username"], "role": session["role"]} if session else None,
        "passkeys": passkeys.availability(passkeys.request_host(request), passkeys.request_https(request)),
    }


def _peer_ip(request: Request) -> str:
    """The address nginx saw (X-Real-IP, else the last X-Forwarded-For entry, which nginx appends). Earlier
    X-Forwarded-For entries come from the client and can be forged, so they are never used for bans."""
    real = (request.headers.get("x-real-ip") or "").strip()
    if real:
        return real
    fwd = [x.strip() for x in (request.headers.get("x-forwarded-for") or "").split(",") if x.strip()]
    return fwd[-1] if fwd else (request.client.host if request.client else "")


def _log_failure(request: Request, user: str, why: str) -> None:
    # Read by the firewall's ban watcher (helix-firewall watch) from the helix-backend journal.
    safe = re.sub(r"[^\w.@-]", "?", (user or "")[:40])
    print(f"helix-auth: login failed user={safe} ip={_peer_ip(request)} reason={why}", flush=True)


@router.post("")
def do_login(req: LoginReq, request: Request, response: Response):
    users = _load_users()
    if users:
        u = users.get(req.username)
        if not u or not _verify_pw(req.password, u["hash"], u["salt"]):
            _log_failure(request, req.username, "password")
            raise HTTPException(401, "Invalid username or password")
        username, role = req.username, u.get("role", "admin")
    else:
        username, role = "admin", "admin"      # no accounts: password step is open

    # Global TOTP 2FA (auth.py). A recovery code is accepted in the same field (§54.8).
    from routers import auth as _auth
    from services import passkeys
    acfg = _auth._load_auth()
    if acfg.get("totp_enabled"):
        code = (req.totp or "").strip()
        totp_ok = False
        compact = code.replace(" ", "")
        if compact.isdigit() and len(compact) == 6:
            totp_ok = int(compact) in _auth._totp(acfg.get("totp_secret") or "")
        if not code:
            raise HTTPException(401, "totp_required")
        if not totp_ok and not passkeys.consume_recovery(username, code):
            _log_failure(request, req.username, "totp")
            raise HTTPException(401, "Invalid TOTP code")

    token = _new_session(username, role, request.headers.get("user-agent", ""))
    _set_login_cookie(response, request, token)
    return {"ok": True, "token": token, "username": username, "role": role}


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


class RequireReq(BaseModel):
    require: bool


@router.get("/settings")
def login_settings():
    from services import authz
    from services.tunnel import public_status
    tunnel_on = bool(public_status().get("enabled"))
    return {
        "require": authz.login_enforced(),
        "reason": authz.enforcement_reason(),
        "tunnel": tunnel_on,
    }


@router.post("/settings")
def set_login_settings(req: RequireReq):
    from services import authz
    from services.tunnel import public_status
    if not req.require and public_status().get("enabled"):
        raise HTTPException(409, "Turn the Cloudflare tunnel off before opening the UI.")
    flag = authz.REQUIRE_FLAG
    if req.require:
        os.makedirs(os.path.dirname(flag), exist_ok=True)
        open(flag, "a").close()
    elif os.path.exists(flag):
        os.remove(flag)
    return login_settings()


@router.get("/sessions")
def list_sessions(request: Request):
    current = _get_session(request)
    rows = []
    for token, row in _load_sessions().items():
        if row.get("exp", 0) <= int(time.time()):
            continue
        rows.append({
            "sid": row.get("sid") or token[:8],
            "username": row.get("username"),
            "created": row.get("created"),
            "exp": row.get("exp"),
            "ua": row.get("ua") or "",
            "current": bool(current and current.get("token") == token),
        })
    return rows


@router.delete("/sessions/{sid}")
def revoke_session(sid: str):
    sessions = _load_sessions()
    for token, row in list(sessions.items()):
        if row.get("sid") == sid or token.startswith(sid):
            sessions.pop(token, None)
    _save_sessions(sessions)
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
