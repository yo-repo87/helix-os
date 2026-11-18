"""Cloudflare tunnel gate. Fake HTTP only. Does not download cloudflared or open a tunnel."""
import json
import os
import stat
import sys
import tempfile

_root = tempfile.mkdtemp(prefix="hx-tunnel-")
os.environ["HELIX_CONFIG"] = os.path.join(_root, "config")
os.environ["STATE_DIR"] = os.path.join(_root, "state")
os.environ["HELIX_TUNNEL_DIR"] = os.path.join(_root, "tunnel")
os.makedirs(os.environ["HELIX_CONFIG"])
os.makedirs(os.environ["STATE_DIR"])
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from fastapi import HTTPException

from services import authz, passkeys, tunnel
from routers import tunnel as tunnel_router

ok = bad = 0


def T(name, cond, extra=""):
    global ok, bad
    if cond:
        ok += 1
    else:
        bad += 1
        print("FAIL", name, extra)


class Req:
    def __init__(self, host="helixos.arc-it.uk"):
        self.headers = {"host": host, "x-forwarded-proto": "https"}
        self.url = type("U", (), {"scheme": "https"})()


RP = "helixos.arc-it.uk"
T("wildcard refused", "Wildcard" in (tunnel.check_hostname("*.example.com", 8096, RP, True, "") or ""))
T("local refused", tunnel.check_hostname("box.local", 8096, RP, True, "") is not None)
T("rp refused", "Tailscale" in (tunnel.check_hostname(RP, 8096, RP, True, "") or ""))
T("ui host refused", tunnel.check_hostname("helixos.arc-it.uk", 9, RP, True, "", "helixos.arc-it.uk") is not None)
T("ssh refused", "management" in (tunnel.check_hostname("files.example.com", 22, RP, True, "") or ""))
T("ui port refused", tunnel.check_hostname("files.example.com", 443, RP, True, "") is not None)
T("api port refused", tunnel.check_hostname("files.example.com", 8082, RP, True, "") is not None)
T("npm port refused", tunnel.check_hostname("files.example.com", 81, RP, True, "") is not None)
T("open access needs the phrase", "NO ACCESS" in (tunnel.check_hostname("files.example.com", 8096, RP, False, "") or ""))
T("open access with the phrase", tunnel.check_hostname("files.example.com", 8096, RP, False, "NO ACCESS") is None)
T("normal host allowed", tunnel.check_hostname("files.example.com", 8096, RP, True, "") is None)
rules = tunnel.ingress_for([{"hostname": "files.example.com", "port": 8096}])
T("ingress is one host then 404", rules == [
    {"hostname": "files.example.com", "service": "http://127.0.0.1:8096"},
    {"service": "http_status:404"},
])

# No media, no token, no phrase: every gap is named. The phrase is not implied.
gaps = tunnel.enable_blockers("", False, False, False, False, "")
T("enable lists the gaps", len(gaps) == 6 and any("EXPOSE TO INTERNET" in g for g in gaps))

# Tokens stay on the tunnel dir, mode 0600, and never appear in status.
tunnel.save_settings("abc12345", "x" * 24)
status = json.dumps(tunnel.public_status(RP))
T("status hides the token", "x" * 24 not in status and '"token_set": true' in status, status)
mode = stat.S_IMODE(os.stat(os.path.join(os.environ["HELIX_TUNNEL_DIR"], "api.token")).st_mode)
T("token file is private", mode == 0o600, oct(mode))
short_refused = False
try:
    tunnel.save_settings("abc12345", "too-short")
except ValueError:
    short_refused = True
T("short token refused", short_refused)

# A hostname cannot be queued until login is required and a credential exists.
try:
    tunnel_router.add_hostname(tunnel_router.HostReq(hostname="files.example.com", port=8096), Req())
    T("hostname blocked without login", False)
except HTTPException as exc:
    T("hostname blocked without login", exc.status_code == 409, getattr(exc, "detail", ""))

passkeys._save(passkeys.USERS_FILE, {"ada": {"hash": "h", "salt": "s", "role": "admin"}})
try:
    tunnel_router.add_hostname(tunnel_router.HostReq(hostname="helixos.arc-it.uk", port=8096), Req())
    T("ui hostname refused", False)
except HTTPException as exc:
    T("ui hostname refused", exc.status_code == 400 and "Tailscale" in str(exc.detail), getattr(exc, "detail", ""))
added = tunnel_router.add_hostname(tunnel_router.HostReq(hostname="files.example.com", port=8096), Req())
T("hostname saved while off", added["enabled"] is False and added["hostnames"][0]["hostname"] == "files.example.com")
try:
    tunnel_router.add_hostname(tunnel_router.HostReq(hostname="files.example.com", port=8096), Req())
    T("duplicate hostname refused", False)
except HTTPException as exc:
    T("duplicate hostname refused", exc.status_code == 409)

# Wrong confirm does not create the require-login flag or mark the tunnel on.
try:
    tunnel_router.enable(tunnel_router.EnableReq(confirm="yes"))
    T("weak confirm refused", False)
except HTTPException as exc:
    T("weak confirm refused", exc.status_code == 409)
T("flag not created yet", not os.path.exists(authz.REQUIRE_FLAG))
T("still off", tunnel.public_status()["enabled"] is False)

calls = []
started = []


def fetch(method, url, token, body):
    calls.append((method, url, body))
    if "cfd_tunnel?" in url:
        return 200, {"success": True, "result": []}
    if url.endswith("/cfd_tunnel") and method == "POST":
        return 200, {"success": True, "result": {"id": "tid-1"}}
    if url.endswith("/token"):
        return 200, {"success": True, "result": "connector-secret-value"}
    if "/access/apps" in url and "/policies" not in url and method == "GET":
        return 200, {"success": True, "result": []}
    if url.endswith("/access/apps") and method == "POST":
        return 200, {"success": True, "result": {"id": "app-1"}}
    if url.endswith("/policies") and method == "GET":
        return 200, {"success": True, "result": []}
    if url.endswith("/policies") and method == "POST":
        return 200, {"success": True, "result": {"id": "pol-1"}}
    if "/zones?" in url:
        return 200, {"success": True, "result": [{"id": "zone-1"}]}
    if "dns_records?" in url:
        return 200, {"success": True, "result": []}
    if url.endswith("/dns_records") and method == "POST":
        return 200, {"success": True, "result": {"id": "dns-1"}}
    if url.endswith("/configurations") and method == "PUT":
        return 200, {"success": True, "result": {}}
    return 500, {"success": False, "errors": [{"message": "unexpected " + method + " " + url}]}


def start(directory):
    started.append(directory)


# Access failure must not publish ingress or start the connector.
def fetch_fail(method, url, token, body):
    if url.endswith("/access/apps") and method == "POST":
        return 400, {"success": False, "errors": [{"message": "access down"}]}
    return fetch(method, url, token, body)


state = tunnel._read_state(os.environ["HELIX_TUNNEL_DIR"])
state["enabled"] = True
tunnel._write_state(os.environ["HELIX_TUNNEL_DIR"], state)
raised = False
try:
    tunnel.sync(fetch=fetch_fail, start=start)
except RuntimeError as exc:
    raised = "access down" in str(exc)
T("access failure raises", raised)
T("access failure does not start", started == [])
T("access failure does not publish", not any(url.endswith("/configurations") for _m, url, _b in calls))

calls.clear()
done = tunnel.sync(fetch=fetch, start=start)
blob = json.dumps(done)
T("sync hides connector token", "connector-secret-value" not in blob)
secret_path = os.path.join(os.environ["HELIX_TUNNEL_DIR"], "connector.token")
T("connector token stored private", open(secret_path).read().strip() == "connector-secret-value" and stat.S_IMODE(os.stat(secret_path).st_mode) == 0o600)
urls = [url for _m, url, _b in calls]
T("access happens before ingress", urls.index(next(u for u in urls if u.endswith("/access/apps"))) < urls.index(next(u for u in urls if u.endswith("/configurations"))))
T("connector started", started == [os.environ["HELIX_TUNNEL_DIR"]])
ingress = next(body for method, url, body in calls if url.endswith("/configurations"))
T("published ingress ends in 404", ingress["config"]["ingress"][-1] == {"service": "http_status:404"})

# Enable itself fails closed: a connector error turns the flag on (login stays required) and leaves the tunnel off.
def explode():
    raise RuntimeError("connector refused")


tunnel_router._push_and_start = explode
try:
    tunnel_router.enable(tunnel_router.EnableReq(confirm="EXPOSE TO INTERNET"))
    T("enable failure is 502", False)
except HTTPException as exc:
    T("enable failure is 502", exc.status_code == 502 and "connector refused" in str(exc.detail))
T("require login forced", os.path.exists(authz.REQUIRE_FLAG) and authz.login_enforced())
T("tunnel left off", tunnel.public_status()["enabled"] is False)

# Disable does not need the network.
tunnel_router._run = lambda *a, **k: type("R", (), {"returncode": 0, "stderr": ""})()
state = tunnel._read_state(os.environ["HELIX_TUNNEL_DIR"])
state["enabled"] = True
tunnel._write_state(os.environ["HELIX_TUNNEL_DIR"], state)
off = tunnel_router.disable()
T("disable turns it off", off["enabled"] is False)

print(f"{ok} passed, {bad} failed")
sys.exit(1 if bad else 0)
