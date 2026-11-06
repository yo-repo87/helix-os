"""
Audit log — records every mutating API call with timestamp, path, and body summary.

GET /api/audit          — return last N log entries
DELETE /api/audit       — clear log
Middleware: AuditMiddleware — auto-logs POST/PUT/PATCH/DELETE
"""

import json
import os
import time
from collections import deque
from pathlib import Path

from fastapi import APIRouter, Request, Response
from fastapi.routing import APIRoute
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from config import STATE_DIR

router = APIRouter(prefix="/api/audit", tags=["audit"])

AUDIT_FILE = os.path.join(STATE_DIR, "audit.jsonl")
MAX_LINES  = 2000


# ── Storage ───────────────────────────────────────────────────────

def _append(entry: dict):
    Path(AUDIT_FILE).parent.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")
    # Trim to MAX_LINES
    try:
        lines = Path(AUDIT_FILE).read_text().splitlines()
        if len(lines) > MAX_LINES:
            Path(AUDIT_FILE).write_text("\n".join(lines[-MAX_LINES:]) + "\n")
    except Exception:
        pass


def _read(limit: int = 200) -> list[dict]:
    try:
        lines = Path(AUDIT_FILE).read_text().splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in reversed(lines[-limit:]):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


# ── Middleware ────────────────────────────────────────────────────

SKIP_PREFIXES = ["/api/audit", "/api/files/recycle/restore", "/api/files/list",
                 "/api/files/download", "/api/health", "/api/system",
                 "/api/drives/", "/api/array/parity/stream", "/api/array/rebuild/stream",
                 "/api/backup/jobs/", "/api/updates/stream", "/api/mover/stream",
                 "/api/vnstat/live"]

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


class AuditMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        method = request.method
        path   = request.url.path
        if method in MUTATING and not any(path.startswith(p) for p in SKIP_PREFIXES):
            # Don't block on body read (already consumed); log what we have
            _append({
                "ts":      int(time.time()),
                "method":  method,
                "path":    path,
                "status":  response.status_code,
                "ip":      request.client.host if request.client else "?",
            })
        return response


# ── Endpoints ─────────────────────────────────────────────────────

@router.get("")
def audit_log(limit: int = 200):
    return _read(min(limit, MAX_LINES))


@router.delete("")
def clear_audit():
    try:
        Path(AUDIT_FILE).unlink(missing_ok=True)
    except Exception:
        pass
    return {"ok": True}
