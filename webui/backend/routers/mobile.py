"""Mobile dashboard (/m): per-user card layout.

Each signed-in UI account gets its own layout. With login not enforced there is no
user, so everyone shares the "_default" layout.
Stored in HELIX_CONFIG/mobile_layouts.json: {"<user>": {"cards": [{"id","size"}], "hidden": [...]}}
"""
import json
import os
import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from config import HELIX_CONFIG
from routers.login import _get_session

router = APIRouter(prefix="/api/mobile", tags=["mobile"])

LAYOUT_FILE = os.path.join(HELIX_CONFIG, "mobile_layouts.json")
CARD_IDS = {"alerts", "system", "array", "pools", "docker", "vms", "notifications",
            "gpu", "network", "shares", "ups", "firewall"}
SIZES = {"compact", "detailed"}
_USER = re.compile(r"^[A-Za-z0-9_.@$-]{1,64}$")


class Card(BaseModel):
    id: str
    size: str = "compact"


class Layout(BaseModel):
    cards: list[Card] = []
    hidden: list[str] = []


def _user(request: Request) -> str:
    s = _get_session(request)
    name = (s or {}).get("username") or "_default"
    return name if _USER.match(name) else "_default"


def _load() -> dict:
    try:
        with open(LAYOUT_FILE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _store(data: dict) -> None:
    tmp = LAYOUT_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, LAYOUT_FILE)


@router.get("/layout")
def get_layout(request: Request):
    user = _user(request)
    return {"user": None if user == "_default" else user, "layout": _load().get(user)}


@router.put("/layout")
def put_layout(layout: Layout, request: Request):
    seen, cards = set(), []
    for c in layout.cards:
        if c.id not in CARD_IDS or c.size not in SIZES or c.id in seen:
            raise HTTPException(400, f"Unknown or repeated card: {c.id!r}")
        seen.add(c.id)
        cards.append({"id": c.id, "size": c.size})
    hidden = []
    for h in layout.hidden:
        if h not in CARD_IDS or h in seen or h in hidden:
            raise HTTPException(400, f"Unknown or repeated card: {h!r}")
        hidden.append(h)
    data = _load()
    data[_user(request)] = {"cards": cards, "hidden": hidden}
    _store(data)
    return {"ok": True}


@router.delete("/layout")
def reset_layout(request: Request):
    data = _load()
    if data.pop(_user(request), None) is not None:
        _store(data)
    return {"ok": True}
