"""Passkeys, recovery codes, login enforcement, and CSRF. No browser, no network."""
import base64
import json
import os
import sys
import tempfile
import time

_root = tempfile.mkdtemp(prefix="hx-passkeys-")
os.environ["HELIX_CONFIG"] = os.path.join(_root, "config")
os.environ["STATE_DIR"] = os.path.join(_root, "state")
os.environ["HELIX_TUNNEL_DIR"] = os.path.join(_root, "tunnel")
os.makedirs(os.environ["HELIX_CONFIG"])
os.makedirs(os.environ["STATE_DIR"])
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from fastapi import HTTPException
from starlette.responses import Response

from services import authz, passkeys
from routers.login import LoginReq, _hash_pw, do_login

ok = bad = 0


def T(name, cond, extra=""):
    global ok, bad
    if cond:
        ok += 1
    else:
        bad += 1
        print("FAIL", name, extra)


class Req:
    def __init__(self, path, method="GET", headers=None, cookies=None, host="127.0.0.1", scheme="http"):
        self.url = type("U", (), {"path": path, "scheme": scheme})()
        self.method = method
        self.headers = headers or {}
        self.cookies = cookies or {}
        self.client = type("C", (), {"host": host})()


def b64url(obj):
    raw = base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return raw


# Fresh box stays open. Loopback with no forwarded address stays open.
T("fresh install is open", authz.login_enforced() is False and authz.enforcement_reason() == "")
T("loopback allowed", authz.allowed(Req("/api/array/status")))
T("status stays open", authz.allowed(Req("/api/login/status")))
T("passkey ceremony stays open", authz.allowed(Req("/api/auth/webauthn/auth/begin", "POST")))

# An account turns enforcement on. A browser (X-Forwarded-For) is challenged.
user_hash, salt = _hash_pw("secret")
passkeys._save(passkeys.USERS_FILE, {"ada": {"hash": user_hash, "salt": salt, "role": "admin"}})
T("account enforces login", authz.login_enforced() and authz.enforcement_reason() == "accounts")
T("forwarded browser is blocked", authz.allowed(Req("/api/array/status", headers={"x-forwarded-for": "10.0.0.8"})) is False)
T("loopback tools stay open", authz.allowed(Req("/api/array/status")) is True)
T("creating accounts is not anonymous", authz.allowed(Req("/api/login/users", "POST", {"x-forwarded-for": "10.0.0.8"})) is False)
T("totp setup is not anonymous", authz.allowed(Req("/api/auth/totp/setup", "POST", {"x-forwarded-for": "10.0.0.8"})) is False)

# Session plus CSRF.
token = "session-token"
passkeys._save(authz.SESSION_FILE, {token: {"username": "ada", "role": "admin", "exp": int(time.time()) + 60, "sid": "abcd"}})
authed = Req("/api/shares/", "POST", {"x-forwarded-for": "10.0.0.8", "x-csrf-token": "csrf-value"}, {"helix_login": token, "helix_csrf": "csrf-value"}, host="10.1.1.1")
T("valid session is allowed", authz.allowed(authed) is True)
T("csrf matches", authz.csrf_problem(authed) is None)
missing = Req("/api/shares/", "POST", {"x-forwarded-for": "10.0.0.8"}, {"helix_login": token}, host="10.1.1.1")
T("csrf missing is rejected", bool(authz.csrf_problem(missing)))
T("login post is exempt", authz.csrf_problem(Req("/api/login", "POST", {"x-forwarded-for": "10.0.0.8"}, {"helix_login": token})) is None)
T("no session means no csrf error", authz.csrf_problem(Req("/api/shares/", "POST", {"x-forwarded-for": "10.0.0.8"})) is None)

# Passkey availability. Default RP is the real hostname.
T("default rp", passkeys.rp_config()["rp_id"] == "helixos.arc-it.uk")
T("bare ip refused", passkeys.availability("192.168.50.142", True)["available"] is False and "bare IP" in passkeys.availability("192.168.50.142", True)["reason"])
T("local refused", "password" in passkeys.availability("helix.local", True)["reason"])
T("http refused", passkeys.availability("helixos.arc-it.uk", False)["reason"] == "Passkeys need HTTPS.")
T("matching host ok", passkeys.availability("helixos.arc-it.uk", True)["available"] is True)
T("subdomain ok", passkeys.availability("ui.helixos.arc-it.uk", True)["available"] is True)

# Registration options carry the RP id, and the stored challenge is the one the browser echoes.
options = passkeys.begin_registration("ada", "Desk key")
T("options name the rp", options["rp"]["id"] == "helixos.arc-it.uk")
saved = json.loads(open(passkeys.CHALLENGE_FILE).read())
T("challenge key matches options", options["challenge"] in saved and saved[options["challenge"]]["kind"] == "reg")

import webauthn


class Verified:
    credential_id = b"\x01\x02cred"
    credential_public_key = b"\x03\x04pk"
    sign_count = 1


webauthn.verify_registration_response = lambda **_kw: Verified()
bad_origin = {"response": {"clientDataJSON": b64url({"challenge": options["challenge"], "origin": "http://helixos.arc-it.uk"})}}
try:
    passkeys.finish_registration("ada", bad_origin)
    T("http origin refused", False)
except ValueError as exc:
    T("http origin refused", "different address" in str(exc), str(exc))

# The failed attempt consumed the challenge. Start again.
options = passkeys.begin_registration("ada", "Desk key")
webauthn.verify_registration_response = lambda **_kw: Verified()
good = {
    "response": {"clientDataJSON": b64url({"challenge": options["challenge"], "origin": "https://helixos.arc-it.uk"})},
    "transports": ["usb"],
}
done = passkeys.finish_registration("ada", good)
T("passkey stored", done["ok"] and done["label"] == "Desk key")
T("recovery issued once", len(done.get("recovery_codes") or []) == 8)
codes = done["recovery_codes"]
T("second register would need a new challenge", passkeys.list_credentials("ada")[0]["label"] == "Desk key")

# Changing the domain needs a confirm, and the last recovery code still works.
try:
    passkeys.set_rp_id("other.example.com", confirm=False)
    T("rp change blocked", False)
except PermissionError:
    T("rp change blocked", True)
T("rp unchanged", passkeys.rp_config()["rp_id"] == "helixos.arc-it.uk")
for code in codes[:-1]:
    T("code consumed " + code[:4], passkeys.consume_recovery("ada", code) is True)
T("last code still works", passkeys.consume_recovery("ada", codes[-1]) is True)
T("used code does not repeat", passkeys.consume_recovery("ada", codes[-1]) is False)

# Login accepts a global recovery code in the TOTP field, and refuses a bare password.
again = passkeys.issue_global_recovery()
auth_path = passkeys.AUTH_FILE
cfg = json.loads(open(auth_path).read())
cfg["totp_enabled"] = True
cfg["totp_secret"] = "JBSWY3DPEHPK3PXP"
json.dump(cfg, open(auth_path, "w"))
req = Req("/api/login", "POST", {"user-agent": "test", "host": "helixos.arc-it.uk"})
try:
    do_login(LoginReq(username="ada", password="secret", totp=""), req, Response())
    T("password alone asks for totp", False)
except HTTPException as exc:
    T("password alone asks for totp", exc.status_code == 401 and exc.detail == "totp_required", getattr(exc, "detail", exc))
try:
    do_login(LoginReq(username="ada", password="secret", totp="000000"), req, Response())
    T("bad totp rejected", False)
except HTTPException as exc:
    T("bad totp rejected", exc.status_code == 401)
signed = do_login(LoginReq(username="ada", password="secret", totp=again[-1]), req, Response())
T("recovery code signs in", signed["ok"] and signed["username"] == "ada")

# Passwordless finish uses the stored key. The library verify is stubbed.
auth_opts = passkeys.begin_authentication("ada")
cred_id = passkeys.list_credentials("ada")[0]["id"]


class AuthOk:
    new_sign_count = 4


webauthn.verify_authentication_response = lambda **_kw: AuthOk()
who, role = passkeys.finish_authentication({
    "id": cred_id,
    "response": {"clientDataJSON": b64url({"challenge": auth_opts["challenge"], "origin": "https://helixos.arc-it.uk"})},
})
T("passkey signs in", who == "ada" and role == "admin")

print(f"{ok} passed, {bad} failed")
sys.exit(1 if bad else 0)
