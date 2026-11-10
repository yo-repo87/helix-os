"""Browser console for a running VM: a WebSocket that relays to the VM's VNC port, for the noVNC client.

noVNC (Debian package `novnc`, shipped in /usr/share/novnc) is served at /novnc by main.py. The page to open is

    /novnc/vnc.html?path=api/vms/<name>/console/ws&autoconnect=true&resize=scale&reconnect=true

The proxy only ever connects to the VNC port libvirt reports for that VM (never to a port named by the client).
"""
import asyncio

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool

from routers import vms

router = APIRouter(tags=["vm-console"])


def _target(name: str) -> tuple[str, int]:
    vms._need_vm(name)
    if vms._state(vms._dominfo(name)) not in ("running", "paused"):
        raise HTTPException(409, "The VM is not running.")
    for g in vms._graphics_live(name):
        if g["type"] == "vnc" and g["port"]:
            host = g["listen"] if g["listen"] and g["listen"] not in ("0.0.0.0", "::", "") else "127.0.0.1"
            return host, g["port"]
    raise HTTPException(409, "This VM has no VNC console. Choose VNC graphics in Edit, or connect with a SPICE client.")


@router.websocket("/api/vms/{name}/console/ws")
async def console_ws(ws: WebSocket, name: str):
    try:
        host, port = await run_in_threadpool(_target, name)
    except HTTPException as e:
        await ws.close(code=4404 if e.status_code == 404 else 4409, reason=str(e.detail)[:120])
        return
    offered = ws.scope.get("subprotocols") or []
    await ws.accept(subprotocol="binary" if "binary" in offered else None)
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=5)
    except (OSError, asyncio.TimeoutError):
        await ws.close(code=4502, reason="Could not reach the VM's console")
        return

    async def to_vm():
        try:
            while True:
                writer.write(await ws.receive_bytes())
                await writer.drain()
        except (WebSocketDisconnect, RuntimeError, ConnectionError):
            pass

    async def to_browser():
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                await ws.send_bytes(data)
        except (WebSocketDisconnect, RuntimeError, ConnectionError):
            pass

    tasks = [asyncio.create_task(to_vm()), asyncio.create_task(to_browser())]
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in tasks:
        t.cancel()
    writer.close()
    try:
        await ws.close()
    except RuntimeError:
        pass
