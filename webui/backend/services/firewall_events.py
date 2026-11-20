"""Firewall notifications -> the bell and the notification channels (services/notifications.notify).

Two sources, both filtered by the firewall config's `notify` switches:
- events queued by the root helper (network/helix-firewall) in STATE_DIR/firewall-events.jsonl: bans, a change undone
  because Keep was not pressed, list updates that failed. The helper runs as root and must not write the backend's
  (user helix) notification store itself, so it queues and this loop sends.
- a check every 5 minutes for services that started listening on the network with no rule covering them (blocked
  while Protecting). Ports already known are remembered in HELIX_CONFIG/firewall-seen.json, so containers starting
  after a reboot are not announced again; the very first check only records what exists.
"""
import json
import os
import time

from config import HELIX_CONFIG, STATE_DIR

EVENTS = os.path.join(STATE_DIR, "firewall-events.jsonl")
SEEN = os.path.join(HELIX_CONFIG, "firewall-seen.json")
SOURCE = "firewall"
SERVICE_EVERY = 300


def _cfg() -> dict:
    from routers import firewall as fr
    return fr._pending() or fr._saved()


def _notify(level, title, message):
    from services import notifications
    notifications.notify(level, title, message, source=SOURCE)


def drain_events(cfg: dict | None = None, notify=_notify) -> int:
    """Send queued helper events; returns how many were sent. The queue is renamed first, so the helper's next append
    starts a new file and nothing is read twice or lost."""
    if not os.path.exists(EVENTS):
        return 0
    work = EVENTS + f".{os.getpid()}.work"
    try:
        os.replace(EVENTS, work)
    except OSError:
        return 0
    cfg = cfg or _cfg()
    on = cfg.get("notify") or {}
    sent = 0
    try:
        with open(work) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if not on.get(e.get("kind"), True):
                    continue
                notify(e.get("level", "warning"), str(e.get("title", "Firewall"))[:120], str(e.get("message", ""))[:800])
                sent += 1
    finally:
        try:
            os.unlink(work)
        except OSError:
            pass
    return sent


def check_new_services(cfg: dict | None = None, rows: list | None = None, notify=_notify, now: float | None = None) -> int:
    from services import firewall as fw
    cfg = cfg or _cfg()
    if cfg["mode"] == "off" or not (cfg.get("notify") or {}).get("new_service", True):
        return 0
    if rows is None:
        from routers import firewall as fr
        containers = fr._containers()
        rows = fw.audit(cfg, fr._listeners(containers), containers)
    now = now or time.time()
    try:
        seen = json.load(open(SEEN))
        first_run = False
    except (OSError, ValueError):
        seen, first_run = {}, True
    report, state = fw.new_uncovered(rows, seen, now)
    if first_run:                                        # record what already exists; announce nothing
        report = []
        state = {k: {**v, "told": True} for k, v in state.items()}
        for r in rows:
            if not r["loopback"] and not r["covered"]:
                state[f"{r['proto']}/{r['port']}"] = {"first": now, "count": 2, "told": True}
    for r in report:
        who = r["container"] and f"container {r['container']}" or r["process"] or "a program"
        if cfg["mode"] == "enforce":
            notify("warning", f"New service blocked: port {r['port']}/{r['proto']}",
                       f"{who} started listening on port {r['port']}/{r['proto']}. No firewall rule covers it, so it "
                       f"is blocked. Add a rule on Firewall > Exposure if it should be reachable.")
        else:
            notify("info", f"New service on port {r['port']}/{r['proto']}",
                       f"{who} started listening on port {r['port']}/{r['proto']} with no firewall rule. Once "
                       f"protection is on it will be blocked unless you add a rule (Firewall > Exposure).")
    try:
        tmp = SEEN + ".tmp"
        with open(tmp, "w") as f:
            json.dump({**seen, **state}, f)              # keep every port ever seen: one that comes back is not new
        os.replace(tmp, SEEN)
    except OSError:
        pass
    return len(report)


def loop():
    last_services = 0.0
    while True:
        try:
            drain_events()
        except Exception:                                   # noqa: BLE001 - a notification problem must not kill it
            pass
        if time.time() - last_services >= SERVICE_EVERY:
            last_services = time.time()
            try:
                check_new_services()
            except Exception:                               # noqa: BLE001
                pass
        time.sleep(10)
