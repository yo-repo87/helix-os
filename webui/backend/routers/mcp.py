"""
Helix OS — MCP (Model Context Protocol) server.

Exposes Helix controls as MCP tools over the Streamable-HTTP transport so an LLM
client (e.g. Claude) can inspect and operate the NAS: array status/start/stop,
disk & hardware inventory, shares, Docker containers, system stats.

Transport: JSON-RPC 2.0 over POST /mcp (single or batch). GET /mcp returns 405
(no server-initiated streaming needed — request/response covers every tool).

Auth: every request must carry `Authorization: Bearer <token>` where <token> is a
Helix API key (Settings → API Keys). Fails CLOSED — if no keys are configured the
endpoint refuses all calls, so the control surface is never exposed unauthenticated.

Dependency-free: tool calls are thin loopback HTTP requests to the local API via
urllib, so every tool reuses an existing, tested endpoint.
"""
import json
import os
import urllib.request
import urllib.error

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from config import HELIX_CONFIG

router = APIRouter(prefix="/mcp", tags=["mcp"])

SERVER_PROTOCOL = "2025-06-18"
API_BASE = os.getenv("HELIX_API_BASE", "http://127.0.0.1:8082")
KEYS_FILE = os.path.join(HELIX_CONFIG, "api_keys.json")

# name -> (http method, local API path, description, destructive?)
TOOLS = [
    ("system_stats",  "GET",  "/api/system/stats",      "CPU %, RAM, uptime, load and hostname.", False),
    ("array_status",  "GET",  "/api/array/status",      "Array state (started/stopped), user-share usage, per-disk and cache-drive status.", False),
    ("list_disks",    "GET",  "/api/drives/available",  "Every physical disk with size, model, health and array assignment.", False),
    ("list_shares",   "GET",  "/api/shares/",           "Configured user shares.", False),
    ("list_docker",   "GET",  "/api/docker/containers", "Docker containers with status and ports.", False),
    ("hardware_info", "GET",  "/api/hardware/",         "Hardware profile (CPU, memory, devices).", False),
    ("array_start",   "POST", "/api/array/start",       "Start the array (mounts data+cache drives, brings the user share online).", True),
    ("array_stop",    "POST", "/api/array/stop",        "Stop the array (unmounts the user share and drives).", True),
]
_BY_NAME = {t[0]: t for t in TOOLS}
_EMPTY_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


# ── Auth (fail closed against the API-key store) ──────────────────────────────
def _auth_error(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not token:
        return "Missing 'Authorization: Bearer <token>' header."
    try:
        keys = json.loads(open(KEYS_FILE).read())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        keys = []
    valid = {k.get("token") for k in keys if isinstance(k, dict)}
    if not valid:
        return "No API keys configured — create one in Settings → API Keys and use it as the MCP bearer token."
    if token not in valid:
        return "Invalid API token."
    return None


# ── Loopback call to the local REST API ───────────────────────────────────────
def _call_api(method: str, path: str) -> tuple[bool, str]:
    req = urllib.request.Request(API_BASE + path, method=method)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return True, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return False, f"HTTP {e.code} from {path}: {body[:500]}"
    except Exception as e:
        return False, f"request to {path} failed: {e}"


def _rpc(id_, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": id_}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    return msg


def _handle(msg: dict):
    """Return a JSON-RPC response dict, or None for a notification (no id)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _rpc(None, error={"code": -32600, "message": "Invalid Request"})
    method = msg.get("method")
    mid = msg.get("id")
    # Notifications (no id) get no response.
    if mid is None and (method or "").startswith("notifications/"):
        return None

    if method == "initialize":
        client_proto = (msg.get("params") or {}).get("protocolVersion") or SERVER_PROTOCOL
        return _rpc(mid, {
            "protocolVersion": client_proto,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "helix-os", "version": _os_version()},
            "instructions": "Helix OS NAS control. Use tools/list, then tools/call. "
                            "array_start/array_stop change array state.",
        })
    if method == "ping":
        return _rpc(mid, {})
    if method == "tools/list":
        return _rpc(mid, {"tools": [
            {"name": n, "description": d, "inputSchema": _EMPTY_SCHEMA}
            for (n, _m, _p, d, _x) in TOOLS
        ]})
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        tool = _BY_NAME.get(name)
        if not tool:
            return _rpc(mid, error={"code": -32602, "message": f"Unknown tool: {name}"})
        _n, http_method, path, _d, _x = tool
        ok, text = _call_api(http_method, path)
        return _rpc(mid, {
            "content": [{"type": "text", "text": text}],
            "isError": not ok,
        })
    return _rpc(mid, error={"code": -32601, "message": f"Method not found: {method}"})


def _os_version() -> str:
    try:
        return open("/etc/helix-version").read().strip() or "0.0.0"
    except OSError:
        return "0.0.0"


@router.post("")
@router.post("/")
async def mcp_post(request: Request):
    err = _auth_error(request)
    if err:
        return JSONResponse(status_code=401,
                            content=_rpc(None, error={"code": -32001, "message": err}))
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400,
                            content=_rpc(None, error={"code": -32700, "message": "Parse error"}))
    # _handle makes blocking loopback HTTP calls; run it off the event loop so a
    # single-worker uvicorn can still serve those loopback requests (no deadlock).
    if isinstance(body, list):
        handled = await run_in_threadpool(lambda: [_handle(m) for m in body])
        out = [r for r in handled if r is not None]
        return JSONResponse(out) if out else Response(status_code=202)
    res = await run_in_threadpool(_handle, body)
    if res is None:
        return Response(status_code=202)
    return JSONResponse(res)


@router.get("")
@router.get("/")
async def mcp_get():
    # No server-initiated streaming; clients use POST request/response.
    return Response(status_code=405, headers={"Allow": "POST"})
