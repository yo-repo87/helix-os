"""Cloudflare Tunnel control (CLAUDE.md §54.7).

Off until someone types EXPOSE TO INTERNET, login is required, and a
credential exists. Each hostname is one localhost port. The management
plane (this UI, the API, SSH, NPM admin) is refused.
"""
import os

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from services import authz, passkeys, tunnel as tunnel_svc
from services.privileged import run as priv_run

router = APIRouter(prefix="/api/tunnel", tags=["tunnel"])


class SettingsReq(BaseModel):
    account_id: str = ""
    api_token: str = ""


class EnableReq(BaseModel):
    confirm: str = ""


class HostReq(BaseModel):
    hostname: str
    port: int
    access: bool = True
    confirm: str = ""


def _script() -> str:
    forced = os.environ.get("HELIX_CLOUDFLARED_HELPER")
    if forced:
        return forced
    sbin = os.path.join(os.environ.get("ARRAY_SBIN", "/usr/local/sbin"), "helix-cloudflared")
    if os.path.isfile(sbin):
        return sbin
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "network", "helix-cloudflared"))


def _has_credential() -> bool:
    users = passkeys._users()
    for user in users.values():
        if isinstance(user, dict) and (user.get("hash") or user.get("credentials")):
            return True
    auth = passkeys._load(passkeys.AUTH_FILE, {})
    return bool(isinstance(auth, dict) and auth.get("totp_enabled"))


def _rp(request: Request) -> str:
    return passkeys.rp_config()["rp_id"]


@router.get("/status")
def status(request: Request):
    info = tunnel_svc.public_status(_rp(request))
    info["enforced"] = authz.login_enforced()
    info["has_credential"] = _has_credential()
    info["blockers"] = tunnel_svc.enable_blockers(
        tunnel_svc.CONFIRM_ENABLE if info["enabled"] else "",
        info["enforced"],
        info["has_credential"],
        info["available"],
        info["token_set"],
        info.get("account_id") or "",
    ) if not info["enabled"] else []
    # The line above treats a missing confirm as a blocker even on the status
    # view, which is noisy. Status lists the real gaps, not the typed phrase.
    info["blockers"] = [b for b in info["blockers"] if "EXPOSE TO INTERNET" not in b]
    return info


@router.put("/settings")
def save_settings(req: SettingsReq):
    try:
        return tunnel_svc.save_settings(req.account_id, req.api_token)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc))


@router.post("/enable")
def enable(req: EnableReq):
    directory = tunnel_svc.tunnel_dir()
    token_set = False
    account = ""
    if directory:
        paths = tunnel_svc._paths(directory)
        token_set = bool(tunnel_svc._read_secret(paths["api_token"]))
        account = (tunnel_svc._read_state(directory).get("account_id") or "")
    reasons = tunnel_svc.enable_blockers(
        req.confirm, authz.login_enforced(), _has_credential(), directory is not None, token_set, account,
    )
    if reasons:
        raise HTTPException(409, " ".join(reasons))
    # The flag is what keeps login on even if the last account check races.
    try:
        os.makedirs(os.path.dirname(authz.REQUIRE_FLAG), exist_ok=True)
        open(authz.REQUIRE_FLAG, "a").close()
    except OSError as exc:
        raise HTTPException(500, f"Could not turn on Require login: {exc}")
    state_dir = directory
    state = tunnel_svc._read_state(state_dir)
    state["enabled"] = True
    tunnel_svc._write_state(state_dir, state)
    try:
        _push_and_start()
    except RuntimeError as exc:
        state["enabled"] = False
        tunnel_svc._write_state(state_dir, state)
        raise HTTPException(502, str(exc))
    return tunnel_svc.public_status()


@router.post("/disable")
def disable():
    directory = tunnel_svc.tunnel_dir()
    if directory:
        state = tunnel_svc._read_state(directory)
        state["enabled"] = False
        tunnel_svc._write_state(directory, state)
    _run("stop")
    return tunnel_svc.public_status()


@router.post("/hostnames")
def add_hostname(req: HostReq, request: Request):
    if not authz.login_enforced() or not _has_credential():
        raise HTTPException(409, "Turn on Require login and add a password, authenticator, or passkey before exposing a hostname.")
    directory = tunnel_svc.tunnel_dir()
    if not directory:
        raise HTTPException(400, "The tunnel needs the data partition (HELIXDATA).")
    error = tunnel_svc.check_hostname(
        req.hostname, req.port, _rp(request), req.access, req.confirm, passkeys.request_host(request),
    )
    if error:
        raise HTTPException(400, error)
    name = req.hostname.strip().lower().rstrip(".")
    state = tunnel_svc._read_state(directory)
    if any(h.get("hostname") == name for h in state["hostnames"]):
        raise HTTPException(409, "That hostname is already exposed.")
    row = {"hostname": name, "port": int(req.port), "access": bool(req.access), "access_app_id": "", "dns_error": ""}
    if state.get("enabled"):
        try:
            client, _st = tunnel_svc._client(directory)
            if req.access:
                row["access_app_id"] = client.ensure_access(name)
            row["dns_error"] = client.ensure_dns(name, state.get("tunnel_id") or client.ensure_tunnel())
            state["hostnames"].append(row)
            tunnel_svc._write_state(directory, state)
            client.put_ingress(state["tunnel_id"], state["hostnames"])
        except RuntimeError as exc:
            raise HTTPException(502, str(exc))
    else:
        state["hostnames"].append(row)
        tunnel_svc._write_state(directory, state)
    return tunnel_svc.public_status()


@router.delete("/hostnames/{hostname}")
def remove_hostname(hostname: str):
    directory = tunnel_svc.tunnel_dir()
    if not directory:
        raise HTTPException(400, "The tunnel needs the data partition (HELIXDATA).")
    name = hostname.strip().lower().rstrip(".")
    state = tunnel_svc._read_state(directory)
    kept, removed = [], None
    for row in state["hostnames"]:
        if row.get("hostname") == name and removed is None:
            removed = row
        else:
            kept.append(row)
    if removed is None:
        raise HTTPException(404, "Hostname not found.")
    state["hostnames"] = kept
    tunnel_svc._write_state(directory, state)
    if state.get("enabled") and state.get("tunnel_id"):
        try:
            client, _st = tunnel_svc._client(directory)
            if removed.get("access_app_id"):
                client.delete_access(removed["access_app_id"])
            client.put_ingress(state["tunnel_id"], kept)
        except RuntimeError as exc:
            raise HTTPException(502, str(exc))
    return tunnel_svc.public_status()


def _push_and_start() -> None:
    directory = tunnel_svc._need_dir()

    def _start(_directory: str) -> None:
        result = _run("install", _directory)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "Could not install cloudflared.")
        result = _run("start", _directory)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "Could not start the tunnel.")

    tunnel_svc.sync(start=_start)


def _run(cmd: str, directory: str | None = None):
    args = [_script(), cmd]
    if directory:
        args.append(directory)
    return priv_run(args, timeout=180)
