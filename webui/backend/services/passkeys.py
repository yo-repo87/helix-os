"""Passkeys and security keys (WebAuthn) plus one-time recovery codes.

RP ID defaults to helixos.arc-it.uk. A bare IP and a .local name cannot be an
RP ID, so those visits keep password and TOTP. Credentials live on the login
account in HELIX_CONFIG/login_users.json, which is already persistent.
"""
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from config import HELIX_CONFIG, STATE_DIR

USERS_FILE = os.path.join(HELIX_CONFIG, "login_users.json")
AUTH_FILE = os.path.join(HELIX_CONFIG, "auth.json")
RP_FILE = os.path.join(HELIX_CONFIG, "webauthn.json")
CHALLENGE_FILE = os.path.join(STATE_DIR, "webauthn_challenges.json")

DEFAULT_RP_ID = "helixos.arc-it.uk"
DEFAULT_RP_NAME = "Helix OS"
CHALLENGE_TTL = 300


def _load(path: str, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def _save(path: str, data, mode=0o600) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2))
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _users() -> dict:
    data = _load(USERS_FILE, {})
    return data if isinstance(data, dict) else {}


def _save_users(users: dict) -> None:
    _save(USERS_FILE, users)


def rp_config() -> dict:
    saved = _load(RP_FILE, {})
    rp_id = (saved.get("rp_id") or DEFAULT_RP_ID).strip().lower().rstrip(".")
    return {"rp_id": rp_id, "rp_name": saved.get("rp_name") or DEFAULT_RP_NAME}


def credential_count() -> int:
    return sum(len(u.get("credentials") or []) for u in _users().values() if isinstance(u, dict))


def set_rp_id(rp_id: str, confirm: bool = False) -> dict:
    rp_id = (rp_id or "").strip().lower().rstrip(".")
    if not _rp_id_ok(rp_id):
        raise ValueError("Passkey domain must be a hostname such as helixos.arc-it.uk, or localhost.")
    current = rp_config()["rp_id"]
    if rp_id == current:
        return {"ok": True, "rp_id": rp_id, "changed": False}
    if credential_count() and not confirm:
        raise PermissionError(
            f"Changing the passkey domain from {current} to {rp_id} makes existing passkeys stop working. Confirm to continue."
        )
    _save(RP_FILE, {"rp_id": rp_id, "rp_name": DEFAULT_RP_NAME})
    return {"ok": True, "rp_id": rp_id, "changed": True, "invalidated": credential_count() > 0}


def _rp_id_ok(rp_id: str) -> bool:
    if rp_id in ("localhost",):
        return True
    if not rp_id or _is_ip(rp_id) or rp_id.endswith(".local") or "." not in rp_id:
        return False
    return all(part and part.replace("-", "").isalnum() and not part.startswith("-") for part in rp_id.split("."))


def _is_ip(host: str) -> bool:
    if ":" in host:
        return True
    parts = host.split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


def request_host(request) -> str:
    raw = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    return raw.split(",")[0].strip().split(":")[0].lower().rstrip(".")


def request_https(request) -> bool:
    proto = (request.headers.get("x-forwarded-proto") or getattr(request.url, "scheme", "") or "")
    return proto.split(",")[0].strip().lower() == "https"


def availability(host: str, https: bool) -> dict:
    """Whether this visit can register or use a passkey."""
    rp_id = rp_config()["rp_id"]
    host = (host or "").split(":")[0].lower().rstrip(".")
    reason = ""
    ok = False
    if _is_ip(host):
        reason = "Reach Helix by a hostname over HTTPS to use passkeys. A bare IP address cannot be a passkey domain."
    elif host.endswith(".local"):
        reason = "Passkeys are not used on a .local address. Sign in with a password and an authenticator code."
    elif host in ("localhost", "127.0.0.1"):
        ok = rp_id == "localhost"
        if not ok:
            reason = f"This address does not match the passkey domain {rp_id}. Open https://{rp_id}/ to use a passkey."
    elif not https:
        reason = "Passkeys need HTTPS."
    elif host != rp_id and not host.endswith("." + rp_id):
        reason = f"This address does not match the passkey domain {rp_id}. Open https://{rp_id}/ to use a passkey."
    else:
        ok = True
    return {"available": ok, "reason": reason, "rp_id": rp_id, "registered": credential_count()}


def _b64(data: bytes) -> str:
    from webauthn.helpers import bytes_to_base64url
    return bytes_to_base64url(data)


def _unb64(text: str) -> bytes:
    from webauthn.helpers import base64url_to_bytes
    return base64url_to_bytes(text)


def _challenges() -> dict:
    now = int(time.time())
    rows = _load(CHALLENGE_FILE, {})
    if not isinstance(rows, dict):
        rows = {}
    return {k: v for k, v in rows.items() if isinstance(v, dict) and v.get("exp", 0) > now}


def _put_challenge(kind: str, username: str | None, token: str, label: str = "") -> None:
    rows = _challenges()
    rows[token] = {
        "kind": kind,
        "username": username or "",
        "label": (label or "Passkey")[:40],
        "exp": int(time.time()) + CHALLENGE_TTL,
    }
    _save(CHALLENGE_FILE, rows, mode=0o600)


def _take_challenge(token: str, kind: str) -> dict | None:
    rows = _challenges()
    row = rows.pop(token, None)
    _save(CHALLENGE_FILE, rows, mode=0o600)
    if not row or row.get("kind") != kind:
        return None
    return row


def _user_handle(user: dict) -> bytes:
    if not user.get("webauthn_user_id"):
        user["webauthn_user_id"] = _b64(secrets.token_bytes(32))
    return _unb64(user["webauthn_user_id"])


def _find_credential(cred_id: str) -> tuple[str, dict, dict] | None:
    for name, user in _users().items():
        if not isinstance(user, dict):
            continue
        for cred in user.get("credentials") or []:
            if cred.get("id") == cred_id:
                return name, user, cred
    return None


def begin_registration(username: str, label: str = "Passkey") -> dict:
    users = _users()
    user = users.get(username)
    if not isinstance(user, dict):
        raise LookupError("Create a login account, sign in, then add a passkey to that account.")
    from webauthn import generate_registration_options, options_to_json
    from webauthn.helpers.structs import (
        AuthenticatorSelectionCriteria,
        PublicKeyCredentialDescriptor,
        ResidentKeyRequirement,
        UserVerificationRequirement,
    )
    challenge = secrets.token_bytes(32)
    existing = [
        PublicKeyCredentialDescriptor(id=_unb64(c["id"]))
        for c in (user.get("credentials") or [])
        if c.get("id")
    ]
    rp = rp_config()
    options = generate_registration_options(
        rp_id=rp["rp_id"],
        rp_name=rp["rp_name"],
        user_name=username,
        user_id=_user_handle(user),
        user_display_name=username,
        challenge=challenge,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
        exclude_credentials=existing,
    )
    _save_users(users)  # persists a new webauthn_user_id
    payload = json.loads(options_to_json(options))
    _put_challenge("reg", username, payload["challenge"], label)
    return payload


def finish_registration(username: str, credential: dict) -> dict:
    if not isinstance(credential, dict):
        raise ValueError("The browser did not return a passkey.")
    from webauthn import verify_registration_response
    challenge_b64 = _challenge_from_client(credential)
    row = _take_challenge(challenge_b64, "reg")
    if not row or row.get("username") != username:
        raise ValueError("That passkey request expired. Start again.")
    rp = rp_config()
    verified = verify_registration_response(
        credential=credential,
        expected_challenge=_unb64(challenge_b64),
        expected_rp_id=rp["rp_id"],
        expected_origin=_expected_origins(credential),
        require_user_verification=False,
    )
    users = _users()
    user = users.get(username)
    if not isinstance(user, dict):
        raise LookupError("Login account not found.")
    creds = list(user.get("credentials") or [])
    cred_id = _b64(verified.credential_id)
    if any(c.get("id") == cred_id for c in creds):
        raise ValueError("This passkey is already registered.")
    transports = credential.get("transports") or []
    if credential.get("authenticatorAttachment"):
        transports = list(transports) + [credential["authenticatorAttachment"]]
    creds.append({
        "id": cred_id,
        "public_key": _b64(verified.credential_public_key),
        "sign_count": int(verified.sign_count or 0),
        "label": row.get("label") or "Passkey",
        "created": int(time.time()),
        "transports": transports[:6],
    })
    user["credentials"] = creds
    recovery = None
    if not user.get("recovery_hashes"):
        recovery = _stamp_recovery(user)
    users[username] = user
    _save_users(users)
    out = {"ok": True, "id": cred_id, "label": creds[-1]["label"]}
    if recovery:
        out["recovery_codes"] = recovery
    return out


def begin_authentication(username: str | None = None) -> dict:
    from webauthn import generate_authentication_options, options_to_json
    from webauthn.helpers.structs import PublicKeyCredentialDescriptor
    users = _users()
    allow = []
    if username:
        user = users.get(username)
        if not isinstance(user, dict) or not user.get("credentials"):
            raise LookupError("No passkey is registered for that account.")
        allow = [PublicKeyCredentialDescriptor(id=_unb64(c["id"])) for c in user["credentials"] if c.get("id")]
    elif credential_count() == 0:
        raise LookupError("No passkey is registered yet.")
    challenge = secrets.token_bytes(32)
    rp = rp_config()
    options = generate_authentication_options(
        rp_id=rp["rp_id"],
        challenge=challenge,
        allow_credentials=allow or None,
    )
    payload = json.loads(options_to_json(options))
    _put_challenge("auth", username or "", payload["challenge"])
    return payload


def finish_authentication(credential: dict) -> tuple[str, str]:
    if not isinstance(credential, dict):
        raise ValueError("The browser did not return a passkey.")
    from webauthn import verify_authentication_response
    challenge_b64 = _challenge_from_client(credential)
    row = _take_challenge(challenge_b64, "auth")
    if not row:
        raise ValueError("That passkey request expired. Start again.")
    cred_id = credential.get("id") or credential.get("rawId") or ""
    found = _find_credential(cred_id)
    if not found:
        raise ValueError("This passkey is not registered on Helix.")
    username, user, cred = found
    if row.get("username") and row["username"] != username:
        raise ValueError("This passkey does not belong to that account.")
    rp = rp_config()
    verified = verify_authentication_response(
        credential=credential,
        expected_challenge=_unb64(challenge_b64),
        expected_rp_id=rp["rp_id"],
        expected_origin=_expected_origins(credential),
        credential_public_key=_unb64(cred["public_key"]),
        credential_current_sign_count=int(cred.get("sign_count") or 0),
        require_user_verification=False,
    )
    cred["sign_count"] = int(verified.new_sign_count or 0)
    users = _users()
    users[username] = user
    _save_users(users)
    return username, user.get("role") or "admin"


def list_credentials(username: str) -> list[dict]:
    user = _users().get(username) or {}
    out = []
    for cred in user.get("credentials") or []:
        out.append({
            "id": cred.get("id"),
            "label": cred.get("label") or "Passkey",
            "created": cred.get("created") or 0,
            "transports": cred.get("transports") or [],
        })
    return out


def delete_credential(username: str, cred_id: str) -> None:
    users = _users()
    user = users.get(username)
    if not isinstance(user, dict):
        raise LookupError("Login account not found.")
    kept = [c for c in (user.get("credentials") or []) if c.get("id") != cred_id]
    if len(kept) == len(user.get("credentials") or []):
        raise LookupError("Passkey not found.")
    user["credentials"] = kept
    users[username] = user
    _save_users(users)


def recovery_remaining(username: str | None) -> int:
    if not username:
        return 0
    user = _users().get(username) or {}
    return len(user.get("recovery_hashes") or [])


def regenerate_recovery(username: str) -> list[str]:
    users = _users()
    user = users.get(username)
    if not isinstance(user, dict):
        raise LookupError("Login account not found.")
    codes = _stamp_recovery(user)
    users[username] = user
    _save_users(users)
    return codes


def issue_global_recovery() -> list[str]:
    """Replacement codes for the global TOTP factor. Returned once."""
    codes = _new_codes()
    cfg = _load(AUTH_FILE, {})
    if not isinstance(cfg, dict):
        cfg = {}
    cfg["recovery_hashes"] = [_hash_code(c) for c in codes]
    _save(AUTH_FILE, cfg)
    return codes


def consume_recovery(username: str, code: str) -> bool:
    """Accept a one-time code for this account, or a global TOTP recovery code."""
    digest = _hash_code(code)
    if not digest:
        return False
    users = _users()
    user = users.get(username)
    if isinstance(user, dict):
        kept, hit = _drop(user.get("recovery_hashes") or [], digest)
        if hit:
            user["recovery_hashes"] = kept
            users[username] = user
            _save_users(users)
            return True
    cfg = _load(AUTH_FILE, {})
    if isinstance(cfg, dict):
        kept, hit = _drop(cfg.get("recovery_hashes") or [], digest)
        if hit:
            cfg["recovery_hashes"] = kept
            _save(AUTH_FILE, cfg)
            return True
    return False


def _drop(hashes: list, digest: str) -> tuple[list, bool]:
    for i, saved in enumerate(hashes):
        if isinstance(saved, str) and len(saved) == len(digest) and hmac.compare_digest(saved, digest):
            return hashes[:i] + hashes[i + 1:], True
    return list(hashes), False


def _stamp_recovery(user: dict) -> list[str]:
    codes = _new_codes()
    user["recovery_hashes"] = [_hash_code(c) for c in codes]
    return codes


def _new_codes(n: int = 8) -> list[str]:
    codes = []
    for _ in range(n):
        raw = secrets.token_hex(8)
        codes.append("-".join(raw[i:i + 4] for i in range(0, 16, 4)))
    return codes


def _hash_code(code: str) -> str:
    norm = (code or "").strip().lower().replace(" ", "")
    if len(norm) < 8:
        return ""
    return hashlib.sha256(norm.encode()).hexdigest()


def _challenge_from_client(credential: dict) -> str:
    """The challenge we stored is echoed inside clientDataJSON."""
    import base64
    response = credential.get("response") or {}
    raw = response.get("clientDataJSON") or ""
    if not raw:
        raise ValueError("The browser did not return a passkey.")
    pad = "=" * ((4 - len(raw) % 4) % 4)
    data = json.loads(base64.urlsafe_b64decode(raw + pad))
    token = data.get("challenge") or ""
    if not token:
        raise ValueError("The browser did not return a passkey challenge.")
    return token


def _expected_origins(credential: dict) -> list[str]:
    import base64
    response = credential.get("response") or {}
    raw = response.get("clientDataJSON") or ""
    pad = "=" * ((4 - len(raw) % 4) % 4)
    origin = json.loads(base64.urlsafe_b64decode(raw + pad)).get("origin") or ""
    if not _origin_matches(origin, rp_config()["rp_id"]):
        raise ValueError("This passkey was created for a different address.")
    return [origin]


def _origin_matches(origin: str, rp_id: str) -> bool:
    from urllib.parse import urlparse
    parsed = urlparse(origin)
    host = (parsed.hostname or "").lower()
    if host in ("localhost", "127.0.0.1") and rp_id == "localhost" and parsed.scheme in ("http", "https"):
        return True
    if parsed.scheme != "https":
        return False
    return host == rp_id or host.endswith("." + rp_id)
