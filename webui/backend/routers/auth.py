"""
Authentication — TOTP 2FA and session management.

GET  /api/auth/status            — 2FA enabled? session valid?
POST /api/auth/totp/setup        — generate new TOTP secret → returns QR data URI + secret
POST /api/auth/totp/enable       — confirm code + enable 2FA
POST /api/auth/totp/disable      — disable 2FA (requires current code)
POST /api/auth/totp/verify       — verify a TOTP code and issue a session token
POST /api/auth/logout            — invalidate session token
"""

import base64
import hashlib
import hmac
import io
import json
import os
import secrets
import struct
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from config import HELIX_CONFIG, STATE_DIR

router = APIRouter(prefix="/api/auth", tags=["auth"])

AUTH_FILE     = os.path.join(HELIX_CONFIG, "auth.json")
SESSION_FILE  = os.path.join(STATE_DIR, "sessions.json")
SESSION_TTL   = 86400 * 7  # 7 days


# ── Minimal TOTP implementation (no pyotp dependency) ─────────────

def _hotp(secret_b32: str, counter: int) -> int:
    key = base64.b32decode(secret_b32.upper().replace(" ", ""), casefold=True)
    msg = struct.pack(">Q", counter)
    h   = hmac.new(key, msg, hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    code   = struct.unpack(">I", h[offset:offset+4])[0] & 0x7FFFFFFF
    return code % 1_000_000


def _totp(secret_b32: str, t: float | None = None, window: int = 1) -> list[int]:
    """Return valid TOTP codes for ±window steps around current time."""
    t = int((t or time.time()) // 30)
    return [_hotp(secret_b32, t + i) for i in range(-window, window + 1)]


def _gen_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode()


def _qr_url(secret: str, issuer: str = "HelixOS", account: str = "admin") -> str:
    return f"otpauth://totp/{issuer}:{account}?secret={secret}&issuer={issuer}&algorithm=SHA1&digits=6&period=30"


# ── Auth state ────────────────────────────────────────────────────

def _load_auth() -> dict:
    try:
        return json.loads(Path(AUTH_FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"totp_enabled": False, "totp_secret": None, "pending_secret": None}


def _save_auth(d: dict):
    Path(AUTH_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(AUTH_FILE).write_text(json.dumps(d, indent=2))
    Path(AUTH_FILE).chmod(0o600)


def _load_sessions() -> dict:
    try:
        return json.loads(Path(SESSION_FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_sessions(d: dict):
    Path(SESSION_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(SESSION_FILE).write_text(json.dumps(d))


def _new_session() -> str:
    token = secrets.token_urlsafe(32)
    sessions = _load_sessions()
    # Prune expired
    now = int(time.time())
    sessions = {k: v for k, v in sessions.items() if v > now}
    sessions[token] = now + SESSION_TTL
    _save_sessions(sessions)
    return token


def _valid_session(token: str) -> bool:
    sessions = _load_sessions()
    exp = sessions.get(token, 0)
    return int(time.time()) < exp


def _invalidate_session(token: str):
    sessions = _load_sessions()
    sessions.pop(token, None)
    _save_sessions(sessions)


def _get_bearer(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return request.cookies.get("helix_session")


# ── Endpoints ─────────────────────────────────────────────────────

@router.get("/status")
def auth_status(request: Request):
    cfg = _load_auth()
    token = _get_bearer(request)
    session_ok = _valid_session(token) if token else not cfg["totp_enabled"]
    return {
        "totp_enabled":  cfg["totp_enabled"],
        "session_valid": session_ok,
    }


class VerifyReq(BaseModel):
    code: str


@router.post("/totp/setup")
def totp_setup():
    """Generate a pending TOTP secret — not yet enabled."""
    secret = _gen_secret()
    cfg = _load_auth()
    cfg["pending_secret"] = secret
    _save_auth(cfg)
    uri = _qr_url(secret)
    # Build a simple text-based QR placeholder (browser will use the URI directly)
    return {"secret": secret, "uri": uri,
            "note": "Scan the QR code or enter the secret in your authenticator app, then confirm with a code to enable 2FA."}


@router.post("/totp/enable")
def totp_enable(req: VerifyReq):
    cfg = _load_auth()
    secret = cfg.get("pending_secret")
    if not secret:
        raise HTTPException(400, "No pending setup — call /totp/setup first")
    code = int(req.code.replace(" ", "")) if req.code.replace(" ", "").isdigit() else -1
    if code not in _totp(secret):
        raise HTTPException(400, "Invalid TOTP code")
    cfg["totp_enabled"] = True
    cfg["totp_secret"]  = secret
    cfg["pending_secret"] = None
    _save_auth(cfg)
    return {"ok": True, "message": "TOTP 2FA enabled"}


@router.post("/totp/disable")
def totp_disable(req: VerifyReq):
    cfg = _load_auth()
    if not cfg["totp_enabled"]:
        raise HTTPException(400, "2FA is not enabled")
    secret = cfg.get("totp_secret", "")
    code = int(req.code.replace(" ", "")) if req.code.replace(" ", "").isdigit() else -1
    if code not in _totp(secret):
        raise HTTPException(400, "Invalid TOTP code — required to disable 2FA")
    cfg["totp_enabled"] = False
    cfg["totp_secret"]  = None
    _save_auth(cfg)
    # Invalidate all sessions so users re-authenticate
    _save_sessions({})
    return {"ok": True, "message": "TOTP 2FA disabled"}


@router.post("/totp/verify")
def totp_verify(req: VerifyReq):
    cfg = _load_auth()
    if not cfg["totp_enabled"]:
        raise HTTPException(400, "2FA is not enabled")
    secret = cfg.get("totp_secret", "")
    code = int(req.code.replace(" ", "")) if req.code.replace(" ", "").isdigit() else -1
    if code not in _totp(secret):
        raise HTTPException(401, "Invalid TOTP code")
    token = _new_session()
    return {"ok": True, "token": token, "expires_in": SESSION_TTL}


@router.post("/logout")
def logout(request: Request):
    token = _get_bearer(request)
    if token:
        _invalidate_session(token)
    return {"ok": True}
