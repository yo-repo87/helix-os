"""The real login route, end to end over HTTP against a throwaway backend (3.3.6 shipped with @router.post("") on the
wrong function: POST /api/login answered 200 but never created a session, so nobody could log in).
    python webui/tests/test_login_route.py        (run with the backend venv's python; stdlib client only)
"""
import http.cookiejar, json, os, socket, subprocess, sys, tempfile, time, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.join(HERE, "..", "backend")
tmp = tempfile.mkdtemp()
env = dict(os.environ, HELIX_CONFIG=os.path.join(tmp, "config"), STATE_DIR=os.path.join(tmp, "state"),
           HELIX_TUNNEL_DIR=os.path.join(tmp, "tunnel"))
os.makedirs(env["HELIX_CONFIG"]); os.makedirs(env["STATE_DIR"])

fails = 0
def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(("PASS " if cond else "FAIL ") + name + ("" if cond or not detail else f" -- {detail}"))

# A login account (same hashing as routers/login.py).
sys.path.insert(0, BACKEND)
os.environ.update({k: env[k] for k in ("HELIX_CONFIG", "STATE_DIR")})
from routers import login  # noqa: E402
h, salt = login._hash_pw("correct horse")
json.dump({"admin": {"hash": h, "salt": salt, "role": "admin"}}, open(login.USERS_FILE, "w"))

s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
log = open(os.path.join(tmp, "server.log"), "w")
srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "main:app", "--app-dir", BACKEND, "--host", "127.0.0.1",
                        "--port", str(port)], env=env, stdout=log, stderr=subprocess.STDOUT)
B = f"http://127.0.0.1:{port}"
try:
    for _ in range(60):
        try:
            urllib.request.urlopen(B + "/api/health", timeout=2); break
        except Exception:
            time.sleep(0.5)
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    LAN = {"X-Forwarded-For": "192.168.1.20", "X-Real-IP": "192.168.1.20", "Content-Type": "application/json"}

    def req(method, path, body=None):
        r = urllib.request.Request(B + path, data=json.dumps(body).encode() if body is not None else None,
                                   headers=LAN, method=method)
        try:
            with op.open(r, timeout=20) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    code, _ = req("GET", "/api/shares/")
    check("enforced: protected page needs a session", code == 401, code)
    code, text = req("POST", "/api/login", {"username": "admin", "password": "wrong"})
    check("wrong password -> 401", code == 401, f"{code} {text[:100]}")
    code, text = req("POST", "/api/login", {"username": "admin", "password": "correct horse"})
    try:
        body = json.loads(text)
    except ValueError:
        body = {}
    check("right password -> 200 with ok and a token", code == 200 and isinstance(body, dict) and body.get("ok") is True
          and body.get("token"), f"{code} {text[:120]}")
    check("browser got the session cookie", any(c.name == "helix_login" for c in jar))
    sessions = json.load(open(login.SESSION_FILE)) if os.path.exists(login.SESSION_FILE) else {}
    check("session stored where the login check reads it", isinstance(body, dict) and body.get("token") in sessions)
    code, text = req("GET", "/api/login/status")
    check("status shows the logged-in user", code == 200 and json.loads(text).get("user") is not None, text[:120])
    code, _ = req("GET", "/api/settings/identity")
    check("the cookie opens a protected page", code == 200, code)
finally:
    srv.terminate(); srv.wait(10); log.close()
logtext = open(os.path.join(tmp, "server.log")).read()
check("failed login logged for the ban watcher",
      "helix-auth: login failed user=admin ip=192.168.1.20 reason=password" in logtext)

print("FAILED" if fails else "all passed")
sys.exit(1 if fails else 0)
