"""Firewall notifications (services/firewall_events.py). Offline: python webui/tests/test_firewall_events.py"""
import json, os, sys, tempfile

tmp = tempfile.mkdtemp()
os.environ["HELIX_CONFIG"] = os.path.join(tmp, "config"); os.environ["STATE_DIR"] = os.path.join(tmp, "state")
os.makedirs(os.environ["HELIX_CONFIG"]); os.makedirs(os.environ["STATE_DIR"])
# A stand-in root helper: the router's apply path runs it (no sudo, no nft).
stub = os.path.join(tmp, "helper.py"); open(stub, "w").write("import sys\nsys.exit(0)\n")
os.environ.update(HELIX_FIREWALL_HELPER=stub, HELIX_FW_DIRECT="1", HELIX_NFT="/bin/true")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from services import firewall as fw, firewall_events as fe  # noqa: E402

fails = 0
def check(name, cond, detail=""):
    global fails
    fails += not cond
    print(("PASS " if cond else "FAIL ") + name + ("" if cond or not detail else f" -- {detail}"))

sent = []
note = lambda level, title, msg: sent.append((level, title, msg))

# ── config ──
c = fw.clean_config({})
check("all four notifications on by default", c["notify"] == {"bans": True, "reverted": True, "lists": True, "new_service": True})
check("a switch can be turned off", fw.clean_config({"notify": {"bans": False}})["notify"]["bans"] is False)
try:
    fw.clean_config({"notify": "yes"}); check("bad notify setting rejected", False)
except ValueError:
    check("bad notify setting rejected", True)

# ── queued helper events ──
def queue(*events):
    with open(fe.EVENTS, "a") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
queue({"kind": "bans", "level": "warning", "title": "Firewall banned 203.0.113.9", "message": "m"},
      {"kind": "lists", "level": "warning", "title": "Firewall lists could not be updated", "message": "m"},
      "not json")
with open(fe.EVENTS, "a") as f:
    f.write("garbage\n")
n = fe.drain_events(fw.clean_config({"notify": {"lists": False}}), notify=note)
check("queued events sent, switched-off kind skipped, junk ignored", n == 1 and sent[-1][1] == "Firewall banned 203.0.113.9", str(sent))
check("queue emptied after sending", not os.path.exists(fe.EVENTS) and not any(p.endswith(".work") for p in os.listdir(os.environ["STATE_DIR"])))
check("nothing queued -> nothing sent", fe.drain_events(fw.clean_config({}), notify=note) == 0)

# ── new services ──
def row(port, covered=False, loopback=False, container="", proto="tcp"):
    return {"proto": proto, "port": port, "loopback": loopback, "covered": covered, "container": container, "process": "srv"}
enforce = fw.clean_config({"mode": "enforce"}); learn = fw.clean_config({"mode": "learn"})
sent.clear()
base = [row(22, covered=True), row(6379), row(8082, loopback=True)]
fe.check_new_services(enforce, base, notify=note, now=1000)
check("first check only records what exists", sent == [] and "tcp/6379" in json.load(open(fe.SEEN)))
fe.check_new_services(enforce, base, notify=note, now=1300)
check("existing uncovered port is not announced later either", sent == [])
fe.check_new_services(enforce, base + [row(9000, container="newapp")], notify=note, now=1600)
check("new port seen once: not announced yet (could be a restart)", sent == [])
fe.check_new_services(enforce, base + [row(9000, container="newapp")], notify=note, now=1900)
check("new port seen twice: announced once, as blocked (protecting)", len(sent) == 1 and sent[0][0] == "warning"
      and "blocked" in sent[0][2] and "container newapp" in sent[0][2], str(sent))
fe.check_new_services(enforce, base + [row(9000, container="newapp")], notify=note, now=2200)
check("not announced again", len(sent) == 1)
fe.check_new_services(enforce, base, notify=note, now=2500)
fe.check_new_services(enforce, base + [row(9000, container="newapp")], notify=note, now=2800)
fe.check_new_services(enforce, base + [row(9000, container="newapp")], notify=note, now=3100)
check("a service that stops and comes back is not new", len(sent) == 1)
sent.clear()
fe.check_new_services(learn, base + [row(7000)], notify=note, now=3400)
fe.check_new_services(learn, base + [row(7000)], notify=note, now=3700)
check("learn mode: info, says it will be blocked once protection is on", len(sent) == 1 and sent[0][0] == "info"
      and "Once protection is on" in sent[0][2], str(sent))
sent.clear()
off = fw.clean_config({"mode": "enforce", "notify": {"new_service": False}})
for t in (4000, 4300):
    fe.check_new_services(off, base + [row(7100)], notify=note, now=t)
check("switched off: no announcement", sent == [])
check("firewall off: nothing checked", fe.check_new_services(fw.clean_config({"mode": "off"}), base + [row(7200)], notify=note) == 0)

# ── a notification-only change needs no Keep confirmation ──
from routers import firewall as fr  # noqa: E402
from starlette.requests import Request  # noqa: E402
req = Request({"type": "http", "headers": [(b"x-forwarded-for", b"192.168.1.5")], "client": ("192.168.1.5", 1)})
fr._available = lambda: {"available": True, "reason": ""}
saved = fw.clean_config({"mode": "enforce", "rules": [{"name": "Web", "ports": "8080"}]})
open(fr.SAVED, "w").write(fw.dumps(saved))
r = fr.put_config(fr.ConfigReq(config={**saved, "notify": {**saved["notify"], "bans": False}}), req)
check("notification switch only: applied without the Keep countdown", r["ok"] and r["needs_confirm"] is False, str(r))
os.path.exists(fr.PENDING) and os.unlink(fr.PENDING)
r = fr.put_config(fr.ConfigReq(config={**saved, "rules": []}), req)
check("a rule change still needs Keep", r["needs_confirm"] is True, str(r))

print("FAILED" if fails else "all passed")
sys.exit(1 if fails else 0)
