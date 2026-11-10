"""Checkmk -> Helix notifications (the bell, and every channel configured in Settings -> Notifications).

A background loop reads the current service and host states from Checkmk (Livestatus) and raises a notification when
something *changes*, the way Unraid's monitor does (CLAUDE.md §30.15): one message per problem, not one per minute.

  OK -> WARN/UNKNOWN   warning        "<service> is WARN"
  any -> CRIT          error          "<service> is CRIT"        (also when WARN gets worse)
  problem -> OK        info           "<service> is back to normal"
  new Container/VM/Disk in monitoring, or removed        info   ("Now monitoring container x")

A new state must be seen on two polls in a row before it is announced (no flapping), and anything in a Checkmk
downtime is ignored. The first poll after a start only records the current states, except that existing problems are
announced once (at most five, then a summary).
"""
import json
import logging
import os
import threading
import time

from config import HELIX_CONFIG, STATE_DIR
from services.notifications import notify

log = logging.getLogger(__name__)

INTERVAL = int(os.getenv("MONITOR_ALERT_INTERVAL", "20"))
SETTINGS = os.path.join(HELIX_CONFIG, "monitor_alerts.json")
STATE = os.path.join(STATE_DIR, "monitor_alert_state.json")
PROBLEMS = os.path.join(STATE_DIR, "monitor_problems.json")
DEFAULTS = {"enabled": True, "warning": True, "critical": True, "recovered": True, "changes": True}
NAMES = {0: "OK", 1: "WARN", 2: "CRIT", 3: "UNKNOWN"}
_lock = threading.Lock()


def load_settings() -> dict:
    try:
        return {**DEFAULTS, **{k: bool(v) for k, v in json.load(open(SETTINGS)).items() if k in DEFAULTS}}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save_settings(new: dict) -> dict:
    s = {**load_settings(), **{k: bool(v) for k, v in new.items() if k in DEFAULTS}}
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    json.dump(s, open(SETTINGS, "w"))
    return s


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 3


def read_states() -> dict | None:
    """{key: {state, text, name, kind, since}} for every non-downtime service and host, or None when Checkmk is unreachable."""
    from routers import checkmk as cmk
    svc = cmk._query_dicts("GET services\nColumns: host_name description state plugin_output last_state_change scheduled_downtime_depth acknowledged\n",
                           ["host", "desc", "state", "out", "since", "downtime", "ack"])
    hosts = cmk._query_dicts("GET hosts\nColumns: name state plugin_output last_state_change scheduled_downtime_depth acknowledged\n",
                             ["host", "state", "out", "since", "downtime", "ack"])
    if not svc and not hosts:
        return None
    out = {}
    for h in hosts:
        out[f"host|{h['host']}"] = {"state": 0 if _int(h["state"]) == 0 else 2, "text": h["out"], "name": f"Host {h['host']}", "kind": "host",
                                    "since": _int(h["since"]), "downtime": _int(h["downtime"]) > 0, "ack": _int(h["ack"]) > 0}
    for s in svc:
        d = s["desc"]
        kind = "container" if d.startswith("Container ") else "vm" if d.startswith("VM ") else "disk" if d.startswith("Disk ") else "service"
        out[f"svc|{s['host']}|{d}"] = {"state": _int(s["state"]), "text": s["out"], "name": d, "kind": kind, "since": _int(s["since"]),
                                        "downtime": _int(s["downtime"]) > 0, "ack": _int(s["ack"]) > 0}
    return out


def _load_state() -> dict:
    try:
        return json.load(open(STATE))
    except (OSError, ValueError):
        return {}


def _save(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(data, open(path, "w"))


def problems_from(states: dict) -> list[dict]:
    return sorted(({"name": v["name"], "state": v["state"], "label": NAMES.get(v["state"], "?"), "text": v["text"], "since": v["since"], "ack": v["ack"]}
                   for v in states.values() if v["state"] != 0 and not v["downtime"]), key=lambda p: (-p["state"], p["name"]))


def announce(kind: str, name: str, state: int, text: str, cfg: dict) -> None:
    detail = (text or "").strip()[:200]
    if kind == "recovered" and cfg["recovered"]:
        notify("info", f"{name} is back to normal", detail, source="monitoring")
    elif kind == "problem":
        if state == 2 and cfg["critical"]:
            notify("error", f"{name} is CRIT", detail, source="monitoring")
        elif state != 2 and cfg["warning"]:
            notify("warning", f"{name} is {NAMES.get(state, '?')}", detail, source="monitoring")


def step(pending: dict, now_states: dict, known: dict, seeded: bool, cfg: dict) -> dict:
    """One poll: compare with `known` (last announced states), announce changes, return the new `known`."""
    new_known = dict(known)
    problems_at_seed = []
    for key, v in now_states.items():
        st = 0 if v["downtime"] else v["state"]
        if key not in known:
            new_known[key] = {"state": st, "name": v["name"], "kind": v["kind"]}
            if not seeded:
                if st != 0:
                    problems_at_seed.append((v["name"], st, v["text"]))
            else:
                if v["kind"] in ("container", "vm", "disk") and cfg["changes"]:
                    notify("info", f"Now monitoring {v['name'].lower() if v['kind'] != 'disk' else v['name']}", "Added to Checkmk automatically.", source="monitoring")
                if st != 0:
                    announce("problem", v["name"], st, v["text"], cfg)
            continue
        old = known[key]["state"]
        if st == old:
            pending.pop(key, None)
            continue
        if pending.get(key, (None, 0))[0] == st:                 # same new state on a second poll in a row
            pending.pop(key, None)
            new_known[key] = {"state": st, "name": v["name"], "kind": v["kind"]}
            if st == 0:
                announce("recovered", v["name"], 0, v["text"], cfg)
            elif st > old or old == 0:
                announce("problem", v["name"], st, v["text"], cfg)
        else:
            pending[key] = (st, 1)
    for key in [k for k in known if k not in now_states]:
        gone = known[key]
        new_known.pop(key, None); pending.pop(key, None)
        if seeded and gone.get("kind") in ("container", "vm", "disk") and cfg["changes"]:
            notify("info", f"Stopped monitoring {gone['name'].lower() if gone['kind'] != 'disk' else gone['name']}", "It no longer exists on this server.", source="monitoring")
    for name, st, text in problems_at_seed[:5]:
        announce("problem", name, st, text, cfg)
    if len(problems_at_seed) > 5:
        notify("warning", f"{len(problems_at_seed) - 5} more monitoring problems", "Open Monitoring to see them all.", source="monitoring")
    return new_known


def run_once(pending: dict) -> bool:
    """Returns False when Checkmk could not be read."""
    cfg = load_settings()
    states = read_states()
    if states is None:
        return False
    with _lock:
        _save(PROBLEMS, {"at": int(time.time()), "problems": problems_from(states), "total": len(states)})
        if cfg["enabled"]:
            existing = os.path.exists(STATE)
            known = step(pending, states, _load_state(), existing, cfg)
            _save(STATE, known)
        else:
            _save(STATE, {k: {"state": v["state"], "name": v["name"], "kind": v["kind"]} for k, v in states.items()})   # stay current so re-enabling does not flood
    return True


def loop() -> None:
    """Blocking loop for a daemon thread. Does nothing until monitoring has been enabled."""
    pending: dict = {}
    while True:
        try:
            if os.path.exists(os.path.join(HELIX_CONFIG, "checkmk", "monitor.json")):
                run_once(pending)
        except Exception as e:                                    # noqa: BLE001 - keep watching
            log.error("monitor alert loop: %s", e)
        time.sleep(INTERVAL)
