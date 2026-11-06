"""
Wake-on-LAN — send magic packets to saved devices.

GET    /api/wol/devices          — list saved devices
POST   /api/wol/devices          — add device
DELETE /api/wol/devices/{id}     — remove device
POST   /api/wol/send/{id}        — send magic packet
POST   /api/wol/send             — send to arbitrary MAC (body: {mac, broadcast})
"""

import json
import os
import secrets
import socket
import struct
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import HELIX_CONFIG

router = APIRouter(prefix="/api/wol", tags=["wol"])

DEVICES_FILE = os.path.join(HELIX_CONFIG, "wol_devices.json")


# ── Persistence ───────────────────────────────────────────────────

def _load() -> list[dict]:
    try:
        return json.loads(Path(DEVICES_FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def _save(d: list[dict]):
    Path(DEVICES_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(DEVICES_FILE).write_text(json.dumps(d, indent=2))


# ── Magic packet ──────────────────────────────────────────────────

def _send_magic(mac: str, broadcast: str = "255.255.255.255", port: int = 9):
    # Normalize MAC: remove separators, uppercase
    mac_clean = mac.replace(":", "").replace("-", "").replace(".", "").upper()
    if len(mac_clean) != 12:
        raise ValueError(f"Invalid MAC address: {mac}")
    mac_bytes = bytes.fromhex(mac_clean)
    packet = b"\xff" * 6 + mac_bytes * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.connect_ex((broadcast, port))
        s.send(packet)


# ── Schemas ───────────────────────────────────────────────────────

class DeviceCreate(BaseModel):
    name:      str
    mac:       str
    broadcast: str = "255.255.255.255"
    port:      int = 9

class WolRequest(BaseModel):
    mac:       str
    broadcast: str = "255.255.255.255"
    port:      int = 9


# ── Endpoints ─────────────────────────────────────────────────────

@router.get("/devices")
def list_devices():
    return _load()


@router.post("/devices")
def add_device(req: DeviceCreate):
    devices = _load()
    dev = req.model_dump()
    dev["id"] = secrets.token_hex(5)
    devices.append(dev)
    _save(devices)
    return dev


@router.delete("/devices/{device_id}")
def remove_device(device_id: str):
    devices = [d for d in _load() if d["id"] != device_id]
    _save(devices)
    return {"ok": True}


@router.post("/send/{device_id}")
def wake_device(device_id: str):
    devices = _load()
    dev = next((d for d in devices if d["id"] == device_id), None)
    if not dev:
        raise HTTPException(404, "Device not found")
    try:
        _send_magic(dev["mac"], dev.get("broadcast", "255.255.255.255"), dev.get("port", 9))
    except (ValueError, OSError) as e:
        raise HTTPException(500, str(e))
    return {"ok": True, "mac": dev["mac"]}


@router.post("/send")
def wake_arbitrary(req: WolRequest):
    try:
        _send_magic(req.mac, req.broadcast, req.port)
    except (ValueError, OSError) as e:
        raise HTTPException(500, str(e))
    return {"ok": True, "mac": req.mac}
