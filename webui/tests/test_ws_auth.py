"""WebSocket login check (terminal + VM console). Offline: python webui/tests/test_ws_auth.py"""
import json, os, sys, tempfile, time

tmp = tempfile.mkdtemp()
os.environ["HELIX_CONFIG"] = os.path.join(tmp, "config")
os.environ["STATE_DIR"] = os.path.join(tmp, "state")
os.makedirs(os.environ["HELIX_CONFIG"]); os.makedirs(os.environ["STATE_DIR"])
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from services import authz  # noqa: E402


class Client:
    def __init__(self, host): self.host = host


class WS:
    def __init__(self, headers=None, cookies=None, client="192.168.50.20"):
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.cookies = cookies or {}
        self.client = Client(client)


fails = 0
def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f": got {got!r}, want {want!r}"))


LAN = {"X-Forwarded-For": "192.168.50.20", "Host": "192.168.50.142", "Origin": "http://192.168.50.142"}
check("open box: allowed", authz.ws_problem(WS(LAN)), None)

json.dump({"admin": {"hash": "x", "salt": "y"}}, open(authz.LOGIN_USERS, "w"))
check("enforced, no session: refused", authz.ws_problem(WS(LAN)), "Authentication required")
check("enforced, bad session: refused",
      authz.ws_problem(WS(LAN, {"helix_login": "nope"})), "Authentication required")

json.dump({"tok": {"exp": int(time.time()) + 60}}, open(authz.SESSION_FILE, "w"))
good = {"helix_login": "tok"}
check("valid session, same origin: allowed", authz.ws_problem(WS(LAN, good)), None)
check("valid session, :8443 origin vs portless Host: allowed",
      authz.ws_problem(WS({**LAN, "Origin": "https://192.168.50.142:8443"}, good)), None)
check("valid session, hostname via proxy: allowed",
      authz.ws_problem(WS({**LAN, "Host": "helixos.arc-it.uk", "Origin": "https://helixos.arc-it.uk"}, good)), None)
check("valid session, foreign origin: refused",
      authz.ws_problem(WS({**LAN, "Origin": "https://evil.example"}, good)), "Cross-site WebSocket refused")
json.dump({"tok": {"exp": int(time.time()) - 1}}, open(authz.SESSION_FILE, "w"))
check("expired session: refused", authz.ws_problem(WS(LAN, good)), "Authentication required")
check("loopback, no X-Forwarded-For: allowed",
      authz.ws_problem(WS({"Host": "127.0.0.1:8082"}, client="127.0.0.1")), None)
check("loopback but forwarded by nginx: refused",
      authz.ws_problem(WS(LAN, client="127.0.0.1")), "Authentication required")

print("FAILED" if fails else "all passed")
sys.exit(1 if fails else 0)
