"""
Notification service — sends alerts via multiple channels and maintains a local history.

Channels supported:
  smtp     — email via SMTP (TLS or plain)
  discord  — Discord webhook POST
  slack    — Slack incoming webhook POST
  ntfy     — ntfy.sh (or self-hosted) topic push
  gotify   — Gotify app push
  telegram — Telegram bot sendMessage

History stored in STATE_DIR/notifications.json (ring buffer, 200 events).
Config stored in HELIX_CONFIG/notifications.cfg (INI-style key=value).
"""

import json
import logging
import os
import smtplib
import ssl
import threading
import time
import urllib.error
import urllib.request
from email.mime.text import MIMEText
from pathlib import Path
from typing import Literal

from config import HELIX_CONFIG, STATE_DIR

log = logging.getLogger(__name__)

HISTORY_FILE  = os.path.join(STATE_DIR, "notifications.json")
NOTIF_CFG     = os.path.join(HELIX_CONFIG, "notifications.cfg")
MAX_HISTORY   = 200

Level = Literal["info", "warning", "error"]

_history_lock = threading.Lock()

# ── Config ─────────────────────────────────────────────────────────────────────

_CFG_DEFAULTS: dict[str, str] = {
    # SMTP
    "SMTP_ENABLED":  "no",
    "SMTP_HOST":     "",
    "SMTP_PORT":     "587",
    "SMTP_TLS":      "yes",
    "SMTP_USER":     "",
    "SMTP_PASS":     "",
    "SMTP_FROM":     "",
    "SMTP_TO":       "",
    # Discord
    "DISCORD_ENABLED":  "no",
    "DISCORD_WEBHOOK":  "",
    # Slack
    "SLACK_ENABLED":    "no",
    "SLACK_WEBHOOK":    "",
    # ntfy
    "NTFY_ENABLED":  "no",
    "NTFY_URL":      "https://ntfy.sh",
    "NTFY_TOPIC":    "helixos-alerts",
    "NTFY_TOKEN":    "",
    # Gotify
    "GOTIFY_ENABLED":  "no",
    "GOTIFY_URL":      "",
    "GOTIFY_TOKEN":    "",
    # Telegram
    "TELEGRAM_ENABLED": "no",
    "TELEGRAM_TOKEN":   "",
    "TELEGRAM_CHAT_ID": "",
    # General
    "MIN_LEVEL":        "warning",
}


def load_config() -> dict[str, str]:
    cfg = dict(_CFG_DEFAULTS)
    try:
        for line in Path(NOTIF_CFG).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            cfg[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return cfg


def save_config(updates: dict[str, str]) -> None:
    cfg = load_config()
    cfg.update(updates)
    Path(NOTIF_CFG).parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{k}={v}" for k, v in sorted(cfg.items())]
    Path(NOTIF_CFG).write_text("\n".join(lines) + "\n")


# ── History ────────────────────────────────────────────────────────────────────

def _load_history() -> list[dict]:
    try:
        return json.loads(Path(HISTORY_FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_history(history: list[dict]) -> None:
    Path(HISTORY_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(HISTORY_FILE).write_text(json.dumps(history[-MAX_HISTORY:]))


def _record(level: Level, title: str, message: str, source: str = "") -> dict:
    event = {
        "id":        int(time.time() * 1000),
        "ts":        int(time.time()),
        "level":     level,
        "title":     title,
        "message":   message,
        "source":    source,
        "read":      False,
    }
    with _history_lock:
        h = _load_history()
        h.append(event)
        _save_history(h)
    return event


def get_history(limit: int = 50, unread_only: bool = False) -> list[dict]:
    with _history_lock:
        h = _load_history()
    if unread_only:
        h = [e for e in h if not e.get("read")]
    return list(reversed(h))[:limit]


def mark_read(event_ids: list[int] | None = None) -> int:
    with _history_lock:
        h = _load_history()
        count = 0
        for e in h:
            if event_ids is None or e["id"] in event_ids:
                if not e.get("read"):
                    e["read"] = True
                    count += 1
        _save_history(h)
    return count


def unread_count() -> int:
    with _history_lock:
        h = _load_history()
    return sum(1 for e in h if not e.get("read"))


# ── Channel senders ────────────────────────────────────────────────────────────

def _priority(level: Level) -> int:
    return {"info": 2, "warning": 4, "error": 5}.get(level, 3)


def _send_smtp(cfg: dict, level: Level, title: str, message: str) -> bool:
    if cfg.get("SMTP_ENABLED") != "yes":
        return False
    try:
        msg = MIMEText(f"{title}\n\n{message}")
        msg["Subject"] = f"[Helix OS] {title}"
        msg["From"]    = cfg["SMTP_FROM"] or cfg["SMTP_USER"]
        msg["To"]      = cfg["SMTP_TO"]
        host, port     = cfg["SMTP_HOST"], int(cfg["SMTP_PORT"])
        if cfg.get("SMTP_TLS") == "yes":
            with smtplib.SMTP(host, port, timeout=10) as s:
                s.starttls(context=ssl.create_default_context())
                if cfg.get("SMTP_USER"):
                    s.login(cfg["SMTP_USER"], cfg["SMTP_PASS"])
                s.send_message(msg)
        else:
            with smtplib.SMTP_SSL(host, port, timeout=10) as s:
                if cfg.get("SMTP_USER"):
                    s.login(cfg["SMTP_USER"], cfg["SMTP_PASS"])
                s.send_message(msg)
        return True
    except Exception as e:
        log.warning("SMTP send failed: %s", e)
        return False


def _post_json(url: str, body: dict, headers: dict | None = None, timeout: int = 8) -> bool:
    data = json.dumps(body).encode()
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    req  = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception as e:
        log.warning("HTTP post to %s failed: %s", url, e)
        return False


def _send_discord(cfg: dict, level: Level, title: str, message: str) -> bool:
    if cfg.get("DISCORD_ENABLED") != "yes" or not cfg.get("DISCORD_WEBHOOK"):
        return False
    color = {"info": 0x3498DB, "warning": 0xE67E22, "error": 0xE74C3C}.get(level, 0x888888)
    return _post_json(cfg["DISCORD_WEBHOOK"], {
        "embeds": [{"title": title, "description": message, "color": color}]
    })


def _send_slack(cfg: dict, level: Level, title: str, message: str) -> bool:
    if cfg.get("SLACK_ENABLED") != "yes" or not cfg.get("SLACK_WEBHOOK"):
        return False
    emoji = {"info": ":information_source:", "warning": ":warning:", "error": ":red_circle:"}.get(level, "")
    return _post_json(cfg["SLACK_WEBHOOK"], {
        "text": f"{emoji} *{title}*\n{message}"
    })


def _send_ntfy(cfg: dict, level: Level, title: str, message: str) -> bool:
    if cfg.get("NTFY_ENABLED") != "yes" or not cfg.get("NTFY_TOPIC"):
        return False
    url  = f"{cfg['NTFY_URL'].rstrip('/')}/{cfg['NTFY_TOPIC']}"
    hdrs = {
        "Title":    title,
        "Priority": str(_priority(level)),
        "Tags":     {"info": "white_check_mark", "warning": "warning", "error": "rotating_light"}.get(level, ""),
    }
    if cfg.get("NTFY_TOKEN"):
        hdrs["Authorization"] = f"Bearer {cfg['NTFY_TOKEN']}"
    data = message.encode()
    req  = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=8):
            return True
    except Exception as e:
        log.warning("ntfy send failed: %s", e)
        return False


def _send_gotify(cfg: dict, level: Level, title: str, message: str) -> bool:
    if cfg.get("GOTIFY_ENABLED") != "yes" or not cfg.get("GOTIFY_URL") or not cfg.get("GOTIFY_TOKEN"):
        return False
    url = f"{cfg['GOTIFY_URL'].rstrip('/')}/message?token={cfg['GOTIFY_TOKEN']}"
    return _post_json(url, {
        "title":    title,
        "message":  message,
        "priority": _priority(level),
    })


def _send_telegram(cfg: dict, level: Level, title: str, message: str) -> bool:
    if cfg.get("TELEGRAM_ENABLED") != "yes" or not cfg.get("TELEGRAM_TOKEN") or not cfg.get("TELEGRAM_CHAT_ID"):
        return False
    url  = f"https://api.telegram.org/bot{cfg['TELEGRAM_TOKEN']}/sendMessage"
    text = f"*{title}*\n{message}"
    return _post_json(url, {"chat_id": cfg["TELEGRAM_CHAT_ID"], "text": text, "parse_mode": "Markdown"})


# ── Public entry point ─────────────────────────────────────────────────────────

_LEVEL_ORDER = {"info": 0, "warning": 1, "error": 2}


def notify(level: Level, title: str, message: str, source: str = "") -> None:
    """Record the event and dispatch to all enabled channels asynchronously."""
    event = _record(level, title, message, source)
    cfg   = load_config()

    min_level = cfg.get("MIN_LEVEL", "warning")
    if _LEVEL_ORDER.get(level, 0) < _LEVEL_ORDER.get(min_level, 1):
        return

    def _dispatch():
        for sender in (_send_smtp, _send_discord, _send_slack, _send_ntfy, _send_gotify, _send_telegram):
            try:
                sender(cfg, level, title, message)
            except Exception as e:
                log.error("Notification dispatch error: %s", e)

    threading.Thread(target=_dispatch, daemon=True).start()
