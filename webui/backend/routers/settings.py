"""
System identity and global settings.

GET  /api/settings/identity        — hostname, description, model, timezone
PUT  /api/settings/identity        — update hostname, description, timezone
GET  /api/settings/samba           — global Samba settings (workgroup, etc.)
PUT  /api/settings/samba           — update global Samba settings
POST /api/settings/reboot          — reboot the system
POST /api/settings/shutdown        — shut down the system

Config stored in HELIX_CONFIG/ident.cfg and HELIX_CONFIG/samba.cfg (key=value INI).
"""

import os
import platform
import socket
import subprocess
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import HELIX_CONFIG

from services import validation

router = APIRouter(prefix="/api/settings", tags=["settings"])

IDENT_CFG = os.path.join(HELIX_CONFIG, "ident.cfg")
SAMBA_CFG = os.path.join(HELIX_CONFIG, "samba.cfg")


# ── INI helpers ───────────────────────────────────────────────────────────────

def _read_cfg(path: str) -> dict[str, str]:
    cfg: dict[str, str] = {}
    try:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            cfg[k.strip()] = v.strip().strip('"')
    except FileNotFoundError:
        pass
    return cfg


def _write_cfg(path: str, updates: dict[str, str]) -> None:
    cfg = _read_cfg(path)
    cfg.update(updates)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    lines = [f'{k}="{v}"' for k, v in sorted(cfg.items())]
    Path(path).write_text("\n".join(lines) + "\n")


# ── Identity endpoints ────────────────────────────────────────────────────────

@router.get("/identity")
def get_identity():
    cfg = _read_cfg(IDENT_CFG)
    # Try to get real hostname; fall back to config
    try:
        real_hostname = socket.gethostname()
    except Exception:
        real_hostname = cfg.get("NAME", "Tower")

    return {
        "hostname":     real_hostname,
        "name":         cfg.get("NAME", real_hostname),
        "description":  cfg.get("COMMENT", "Helix OS"),
        "timezone":     cfg.get("TIMEZONE", _detect_timezone()),
        "model":        platform.machine(),
        "os_version":   _os_version(),
    }


def _detect_timezone() -> str:
    try:
        tz = subprocess.run(["timedatectl", "show", "--property=Timezone", "--value"],
                            capture_output=True, text=True, timeout=3).stdout.strip()
        return tz or "UTC"
    except Exception:
        pass
    try:
        link = os.readlink("/etc/localtime")
        return link.split("zoneinfo/", 1)[-1] if "zoneinfo/" in link else "UTC"
    except Exception:
        return "UTC"


def _os_version() -> str:
    """The installed Helix OS version (stamped into /etc/helix-version at build time)."""
    try:
        return open("/etc/helix-version").read().strip() or "unknown"
    except OSError:
        return "unknown"


class IdentityUpdate(BaseModel):
    name:        str | None = None
    description: str | None = None
    timezone:    str | None = None


@router.put("/identity")
def update_identity(req: IdentityUpdate):
    if req.name is not None:
        err = validation.hostname(req.name.strip())
        if err:
            raise HTTPException(400, err)
    if req.description is not None and len(req.description) > 256:
        raise HTTPException(400, "Description can be at most 256 characters.")
    updates: dict[str, str] = {}
    if req.name is not None:
        updates["NAME"] = req.name.strip()
    if req.description is not None:
        updates["COMMENT"] = req.description.strip()
    if req.timezone is not None:
        updates["TIMEZONE"] = req.timezone.strip()
    if not updates:
        raise HTTPException(400, "No fields to update")
    _write_cfg(IDENT_CFG, updates)

    # Apply hostname if name changed
    if req.name:
        try:
            subprocess.run(["hostname", req.name.strip()], timeout=3, check=False)
        except Exception:
            pass

    # Apply timezone if changed
    if req.timezone:
        try:
            subprocess.run(["timedatectl", "set-timezone", req.timezone.strip()],
                           timeout=5, check=False)
        except Exception:
            pass

    return get_identity()


# ── Samba global settings ─────────────────────────────────────────────────────

_SAMBA_DEFAULTS = {
    "WORKGROUP":      "WORKGROUP",
    "DESCRIPTION":    "Helix OS",
    "GUEST_ACCOUNT":  "nobody",
    "LOCAL_MASTER":   "yes",
    "ENHANCED_MACOS": "yes",
    "RECYCLE_BIN":    "yes",
    "MIN_PROTOCOL":   "SMB2",
    "MAX_PROTOCOL":   "SMB3",
}


@router.get("/samba")
def get_samba_config():
    cfg = dict(_SAMBA_DEFAULTS)
    cfg.update(_read_cfg(SAMBA_CFG))
    return cfg


class SambaUpdate(BaseModel):
    model_config = {"extra": "allow"}


@router.put("/samba")
def update_samba_config(req: SambaUpdate):
    updates = {k: str(v) for k, v in req.model_extra.items()}
    _write_cfg(SAMBA_CFG, updates)
    return get_samba_config()


# ── System power actions ──────────────────────────────────────────────────────

@router.post("/reboot")
def reboot():
    try:
        subprocess.Popen(["shutdown", "-r", "+0"])
    except Exception as e:
        raise HTTPException(500, f"Reboot failed: {e}")
    return {"ok": True, "message": "System is rebooting…"}


@router.post("/shutdown")
def shutdown():
    try:
        subprocess.Popen(["shutdown", "-h", "+0"])
    except Exception as e:
        raise HTTPException(500, f"Shutdown failed: {e}")
    return {"ok": True, "message": "System is shutting down…"}


# ── Timezone list ─────────────────────────────────────────────────────────────

@router.get("/timezones")
def list_timezones():
    try:
        r = subprocess.run(["timedatectl", "list-timezones"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip().splitlines()
    except Exception:
        # Return a sensible short list as fallback
        return [
            "UTC", "America/New_York", "America/Chicago", "America/Denver",
            "America/Los_Angeles", "America/Toronto", "America/Vancouver",
            "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Amsterdam",
            "Europe/Madrid", "Europe/Rome", "Europe/Stockholm",
            "Asia/Tokyo", "Asia/Shanghai", "Asia/Singapore", "Asia/Dubai",
            "Australia/Sydney", "Australia/Melbourne", "Pacific/Auckland",
        ]


# ── Dashboard card layout (Customize) ─────────────────────────────
# Stored on the server so the arrangement follows the box, not the browser.
# {"cols": 12, "tiles": {"<tile id>": {"x":0,"y":0,"w":2,"h":4}, ...}}
import json as _json
import re as _re

LAYOUT_FILE = os.path.join(HELIX_CONFIG, "dashboard_layout.json")
_TILE_ID = _re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


class DashboardLayout(BaseModel):
    cols: int = 12
    tiles: dict[str, dict[str, int]] = {}


@router.get("/dashboard-layout")
def get_dashboard_layout():
    try:
        with open(LAYOUT_FILE) as f:
            return _json.load(f)
    except (OSError, ValueError):
        return {"cols": 12, "tiles": {}}


@router.put("/dashboard-layout")
def put_dashboard_layout(layout: DashboardLayout):
    if layout.cols != 12:
        raise HTTPException(400, "Layout must use 12 columns.")
    if len(layout.tiles) > 200:
        raise HTTPException(400, "Too many cards.")
    clean = {}
    for tid, pos in layout.tiles.items():
        if not _TILE_ID.match(tid):
            raise HTTPException(400, f"Bad card id: {tid!r}")
        try:
            x, y, w, h = (int(pos[k]) for k in ("x", "y", "w", "h"))
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, f"Card {tid} needs x, y, w and h.")
        if not (0 <= x < 12 and 1 <= w <= 12 and x + w <= 12 and 0 <= y <= 500 and 1 <= h <= 60):
            raise HTTPException(400, f"Card {tid} is outside the grid.")
        clean[tid] = {"x": x, "y": y, "w": w, "h": h}
    data = {"cols": 12, "tiles": clean}
    tmp = LAYOUT_FILE + ".tmp"
    with open(tmp, "w") as f:
        _json.dump(data, f)
    os.replace(tmp, LAYOUT_FILE)
    return data


@router.delete("/dashboard-layout")
def reset_dashboard_layout():
    try:
        os.remove(LAYOUT_FILE)
    except FileNotFoundError:
        pass
    return {"cols": 12, "tiles": {}}
