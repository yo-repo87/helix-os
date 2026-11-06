"""
Notifications router.

GET  /api/notifications/           — recent history (+ ?unread_only=true, ?limit=N)
POST /api/notifications/mark-read  — mark events as read (body: {ids:[...]} or empty = mark all)
GET  /api/notifications/unread     — unread count only
GET  /api/notifications/config     — get channel config (passwords redacted)
PUT  /api/notifications/config     — update channel config
POST /api/notifications/test       — send a test notification
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.notifications import (
    get_history,
    load_config,
    mark_read,
    notify,
    save_config,
    unread_count,
)

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

_REDACT = {"SMTP_PASS", "NTFY_TOKEN", "GOTIFY_TOKEN", "TELEGRAM_TOKEN", "DISCORD_WEBHOOK", "SLACK_WEBHOOK"}


def _redact(cfg: dict) -> dict:
    return {k: ("••••••" if k in _REDACT and v else v) for k, v in cfg.items()}


@router.get("/")
def list_notifications(limit: int = 50, unread_only: bool = False):
    return get_history(limit=limit, unread_only=unread_only)


@router.get("/unread")
def get_unread_count():
    return {"unread": unread_count()}


class MarkReadRequest(BaseModel):
    ids: list[int] = []  # empty = mark all


@router.post("/mark-read")
def mark_notifications_read(req: MarkReadRequest):
    count = mark_read(req.ids if req.ids else None)
    return {"marked": count}


@router.get("/config")
def get_notification_config():
    return _redact(load_config())


class ConfigUpdate(BaseModel):
    model_config = {"extra": "allow"}


@router.put("/config")
def update_notification_config(req: ConfigUpdate):
    updates = {k: str(v) for k, v in req.model_extra.items()}
    if not updates:
        raise HTTPException(400, "No fields provided")
    # Don't clobber secrets with the redaction placeholder
    real_cfg = load_config()
    for k, v in updates.items():
        if k in _REDACT and v == "••••••":
            updates[k] = real_cfg.get(k, "")
    save_config(updates)
    return _redact(load_config())


class TestNotification(BaseModel):
    level:   str = "info"
    channel: str = "all"


@router.post("/test")
def test_notification(req: TestNotification):
    if req.level not in ("info", "warning", "error"):
        raise HTTPException(400, "level must be info, warning, or error")
    notify(
        req.level,
        "Test notification from Helix OS",
        f"This is a test {req.level} alert sent via '{req.channel}'. "
        "If you received this, your notifications are configured correctly.",
        source="test",
    )
    return {"ok": True, "message": "Test notification dispatched"}
