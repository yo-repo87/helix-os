"""Offline test of the edge-triggered alert logic (services/monitor_alerts.step)."""
import os, sys, tempfile
os.environ["HELIX_CONFIG"] = tempfile.mkdtemp(); os.environ["STATE_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from services import monitor_alerts as m
sent = []
m.notify = lambda level, title, message, source="": sent.append((level, title))
cfg = dict(m.DEFAULTS)
ok = bad = 0
def T(n, c, x=""):
    global ok, bad
    if c: ok += 1
    else: bad += 1; print("FAIL", n, x)
def S(state, name="Container emby", kind="container", downtime=False, text="t"):
    return {f"svc|h|{name}": {"state": state, "text": text, "name": name, "kind": kind, "since": 0, "downtime": downtime, "ack": False}}
pend = {}
known = m.step(pend, S(0), {}, False, cfg); T("seed silent when all OK", sent == [], sent)
known = m.step(pend, S(2), known, True, cfg); T("first CRIT poll waits (no flapping)", sent == [], sent)
known = m.step(pend, S(2), known, True, cfg); T("second CRIT poll announces once", sent == [("error", "Container emby is CRIT")], sent)
known = m.step(pend, S(2), known, True, cfg); T("no repeat while it stays CRIT", len(sent) == 1, sent)
known = m.step(pend, S(0), known, True, cfg); known = m.step(pend, S(0), known, True, cfg)
T("recovery announced", sent[-1] == ("info", "Container emby is back to normal"), sent)
known = m.step(pend, S(1), known, True, cfg); known = m.step(pend, S(0), known, True, cfg); known = m.step(pend, S(0), known, True, cfg)
T("a blip that clears is silent", len(sent) == 2, sent)
known = m.step(pend, S(1), known, True, cfg); known = m.step(pend, S(1), known, True, cfg)
known = m.step(pend, S(2), known, True, cfg); known = m.step(pend, S(2), known, True, cfg)
T("WARN then worse CRIT announces both", [s[0] for s in sent[2:]] == ["warning", "error"], sent)
n = len(sent); known = m.step(pend, S(2, downtime=True), known, True, cfg); known = m.step(pend, S(2, downtime=True), known, True, cfg)
T("downtime counts as OK (and announces recovery)", sent[-1][1].endswith("back to normal"), sent)
a = m.step({}, {**S(0), **S(0, "VM win", "vm")}, known, True, cfg)
T("new VM announces 'Now monitoring'", sent[-1] == ("info", "Now monitoring vm win"), sent)
a = m.step({}, {}, a, True, cfg)
T("removed things announce 'Stopped monitoring'", any("Stopped monitoring" in s[1] for s in sent[-2:]), sent)
sent.clear(); m.step({}, {**S(2, "A"), **S(2, "B"), **S(2, "C"), **S(2, "D"), **S(2, "E"), **S(1, "F"), **S(1, "G")}, {}, False, cfg)
T("seed with many problems: five + a summary", len(sent) == 6 and "more monitoring problems" in sent[-1][1], sent)
sent.clear(); c2 = {**cfg, "warning": False, "recovered": False}; k = m.step({}, S(0), {}, False, c2)
p2 = {}
for st in (1, 1): k = m.step(p2, S(st), k, True, c2)
T("warning switch off", sent == [], sent)
print(f"{ok} passed, {bad} failed"); sys.exit(1 if bad else 0)
