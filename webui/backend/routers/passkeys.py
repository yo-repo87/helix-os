"""Passkey and security-key routes (CLAUDE.md §54.8).

Register and the credential list require a signed-in login account.
Authentication begin/finish stay open so the login screen can use them.
"""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from services import authz, passkeys

router = APIRouter(prefix="/api/auth/webauthn", tags=["webauthn"])


class RegisterBegin(BaseModel):
    label: str = "Passkey"


class RpUpdate(BaseModel):
    rp_id: str
    confirm: bool = False


def _session_user(request: Request) -> str:
    from routers.login import _get_session
    session = _get_session(request)
    if not session:
        raise HTTPException(401, "Sign in before adding a passkey.")
    return session["username"]


def _guard_available(request: Request) -> None:
    info = passkeys.availability(passkeys.request_host(request), passkeys.request_https(request))
    if not info["available"]:
        raise HTTPException(400, info["reason"] or "Passkeys are not available from this address.")


def _credential(body: dict) -> dict:
    if not isinstance(body, dict):
        raise HTTPException(400, "The browser did not return a passkey.")
    cred = body.get("credential") if isinstance(body.get("credential"), dict) else body
    return cred


@router.get("/status")
def webauthn_status(request: Request):
    return passkeys.availability(passkeys.request_host(request), passkeys.request_https(request))


@router.get("/credentials")
def credentials(request: Request):
    username = _session_user(request)
    return {
        "credentials": passkeys.list_credentials(username),
        "recovery_left": passkeys.recovery_remaining(username),
        **passkeys.availability(passkeys.request_host(request), passkeys.request_https(request)),
    }


@router.put("/rp")
def update_rp(req: RpUpdate, request: Request):
    _session_user(request)
    try:
        return passkeys.set_rp_id(req.rp_id, req.confirm)
    except PermissionError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/register/begin")
def register_begin(req: RegisterBegin, request: Request):
    _guard_available(request)
    username = _session_user(request)
    try:
        options = passkeys.begin_registration(username, req.label)
    except LookupError as exc:
        raise HTTPException(400, str(exc))
    return {"options": options}


@router.post("/register/finish")
def register_finish(body: dict, request: Request):
    _guard_available(request)
    username = _session_user(request)
    try:
        return passkeys.finish_registration(username, _credential(body))
    except (ValueError, LookupError) as exc:
        raise HTTPException(400, str(exc))


@router.delete("/credentials/{cred_id}")
def delete_credential(cred_id: str, request: Request):
    username = _session_user(request)
    try:
        passkeys.delete_credential(username, cred_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    return {"ok": True, "credentials": passkeys.list_credentials(username)}


@router.post("/recovery")
def new_recovery_codes(request: Request):
    username = _session_user(request)
    try:
        codes = passkeys.regenerate_recovery(username)
    except LookupError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "recovery_codes": codes}


@router.post("/auth/begin")
def auth_begin(body: dict | None = None):
    username = ""
    if isinstance(body, dict):
        username = (body.get("username") or "").strip()
    try:
        options = passkeys.begin_authentication(username or None)
    except LookupError as exc:
        raise HTTPException(400, str(exc))
    return {"options": options}


@router.post("/auth/finish")
def auth_finish(body: dict, request: Request):
    from routers.login import _new_session, _set_login_cookie
    try:
        username, role = passkeys.finish_authentication(_credential(body))
    except (ValueError, LookupError) as exc:
        raise HTTPException(401, str(exc))
    token = _new_session(username, role, request.headers.get("user-agent", ""))
    response = JSONResponse({"ok": True, "username": username, "role": role, "token": token})
    _set_login_cookie(response, request, token)
    authz.stamp_csrf(request, response)
    return response
