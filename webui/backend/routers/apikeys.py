"""
API key management.

GET    /api/apikeys/        — list keys (name, prefix, created, last_used)
POST   /api/apikeys/        — generate new key  body: {name: str}
DELETE /api/apikeys/{id}    — revoke key
POST   /api/apikeys/verify  — check a bearer token (internal use)
"""

import json
import os
import secrets
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from config import HELIX_CONFIG

router = APIRouter(prefix="/api/apikeys", tags=["apikeys"])

KEYS_FILE = os.path.join(HELIX_CONFIG, "api_keys.json")


def _load() -> list[dict]:
    try:
        return json.loads(Path(KEYS_FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save(keys: list[dict]):
    Path(KEYS_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(KEYS_FILE).write_text(json.dumps(keys, indent=2))


def _mask(key: str) -> str:
    return key[:8] + "…" + key[-4:] if len(key) > 12 else key[:4] + "…"


class KeyCreate(BaseModel):
    name: str


@router.get("/")
def list_keys():
    keys = _load()
    return [
        {
            "id":         k["id"],
            "name":       k["name"],
            "prefix":     k["token"][:8],
            "created":    k["created"],
            "last_used":  k.get("last_used"),
        }
        for k in keys
    ]


@router.post("/")
def create_key(req: KeyCreate):
    if not req.name.strip():
        raise HTTPException(400, "name is required")
    keys = _load()
    token = "helix_" + secrets.token_urlsafe(32)
    entry = {
        "id":        secrets.token_hex(8),
        "name":      req.name.strip(),
        "token":     token,
        "created":   int(time.time()),
        "last_used": None,
    }
    keys.append(entry)
    _save(keys)
    return {"id": entry["id"], "name": entry["name"], "token": token,
            "note": "Save this token — it will not be shown again."}


@router.delete("/{key_id}")
def revoke_key(key_id: str):
    keys = _load()
    new_keys = [k for k in keys if k["id"] != key_id]
    if len(new_keys) == len(keys):
        raise HTTPException(404, "Key not found")
    _save(new_keys)
    return {"ok": True, "revoked": key_id}


@router.post("/verify")
def verify_key(request: Request):
    """Check Bearer token from Authorization header."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Missing Bearer token")
    token = auth[7:]
    keys = _load()
    for k in keys:
        if k["token"] == token:
            k["last_used"] = int(time.time())
            _save(keys)
            return {"ok": True, "name": k["name"], "id": k["id"]}
    raise HTTPException(401, "Invalid API key")
