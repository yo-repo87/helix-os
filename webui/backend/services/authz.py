"""Login enforcement and CSRF (CLAUDE.md §54.8).

Once login is enforced, a browser that reached us through nginx must carry a
valid `helix_login` session. Loopback calls with no X-Forwarded-For stay open
so the MCP tools and other local callers keep working.

Enforcement is on when a login account exists, global TOTP is on, or
HELIX_CONFIG/require_login exists (the tunnel gate creates that flag).
A fresh box with none of those stays open.
"""
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from config import HELIX_CONFIG, STATE_DIR

LOGIN_USERS = os.path.join(HELIX_CONFIG, "login_users.json")
SESSION_FILE = os.path.join(STATE_DIR, "login_sessions.json")
AUTH_FILE = os.path.join(HELIX_CONFIG, "auth.json")
REQUIRE_FLAG = os.path.join(HELIX_CONFIG, "require_login")
CSRF_COOKIE = "helix_csrf"

# Exact paths that stay reachable with no session. `/api/auth` and `/api/login`
# are not open as a whole: setup and account-create must require a session.
_OPEN_GET = {
    "/api/health",
    "/api/login/status",
    "/api/auth/status",
    "/api/auth/webauthn/status",
    "/manifest.json",
    "/favicon.ico",
}
_OPEN_POST = {
    "/api/login",
    "/api/login/logout",
    "/api/auth/totp/verify",
    "/api/auth/webauthn/auth/begin",
    "/api/auth/webauthn/auth/finish",
}
_CSRF_EXEMPT = _OPEN_POST


def _json(path: str):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def login_enforced() -> bool:
    if os.path.exists(REQUIRE_FLAG):
        return True
    if _json(LOGIN_USERS):
        return True
    auth = _json(AUTH_FILE)
    return bool(auth and auth.get("totp_enabled"))


def enforcement_reason() -> str:
    if os.path.exists(REQUIRE_FLAG):
        return "flag"
    if _json(LOGIN_USERS):
        return "accounts"
    auth = _json(AUTH_FILE)
    if auth and auth.get("totp_enabled"):
        return "totp"
    return ""


def _token(request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer "):
        return header[7:]
    return request.cookies.get("helix_login")


def valid_session(request) -> bool:
    token = _token(request)
    if not token:
        return False
    sessions = _json(SESSION_FILE) or {}
    row = sessions.get(token)
    return bool(row) and row.get("exp", 0) > int(time.time())


def _open(path: str, method: str) -> bool:
    if path == "/" or path.startswith(("/static/", "/static", "/novnc", "/mcp")):
        return True
    if method == "GET" and path in _OPEN_GET:
        return True
    if method == "POST" and path in _OPEN_POST:
        return True
    return False


def allowed(request) -> bool:
    """True when the request may proceed without a session."""
    if not login_enforced():
        return True
    if _open(request.url.path, request.method):
        return True
    if not request.headers.get("x-forwarded-for"):
        client = request.client.host if request.client else ""
        if client in ("127.0.0.1", "::1", "localhost"):
            return True
    return valid_session(request)


def ws_problem(ws) -> str | None:
    """Why a WebSocket handshake must be refused, or None to let it through.

    The HTTP middleware never sees WebSockets, and these sockets are a shell (terminal) and a VM
    screen (console). Same rules as `allowed()`, plus an Origin check: a cookie alone is not
    proof the page is ours, because browsers send cookies on cross-site WebSocket handshakes.
    """
    if not login_enforced():
        return None
    if not ws.headers.get("x-forwarded-for"):
        client = ws.client.host if ws.client else ""
        if client in ("127.0.0.1", "::1", "localhost"):
            return None
    if not valid_session(ws):
        return "Authentication required"
    origin = ws.headers.get("origin") or ""
    if origin:
        # Compare host names only: nginx forwards `Host: $host`, which drops the port (:8443).
        from urllib.parse import urlsplit
        host = (ws.headers.get("x-forwarded-host") or ws.headers.get("host") or "").split(",")[0].strip()
        want = urlsplit("//" + host).hostname or ""
        if (urlsplit(origin).hostname or "") != want.lower():
            return "Cross-site WebSocket refused"
    return None


def csrf_problem(request) -> str | None:
    """Reject a logged-in browser POST that did not echo the CSRF cookie.

    Cross-site form posts send the SameSite=lax session cookie and cannot set
    a custom header. Safe methods and the login/passkey ceremony are exempt.
    """
    if not login_enforced():
        return None
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    if request.url.path in _CSRF_EXEMPT:
        return None
    if not valid_session(request):
        return None
    cookie = request.cookies.get(CSRF_COOKIE) or ""
    header = request.headers.get("x-csrf-token") or ""
    if cookie and header and len(cookie) == len(header) and hmac.compare_digest(cookie, header):
        return None
    return "Missing security token. Reload the page and try again."


def request_is_https(request) -> bool:
    proto = (request.headers.get("x-forwarded-proto") or getattr(request.url, "scheme", "") or "")
    return proto.split(",")[0].strip().lower() == "https"


def stamp_csrf(request, response) -> None:
    if request.cookies.get(CSRF_COOKIE):
        return
    response.set_cookie(
        CSRF_COOKIE,
        secrets.token_urlsafe(32),
        httponly=False,
        samesite="lax",
        secure=request_is_https(request),
        path="/",
        max_age=86400 * 7,
    )
