"""
WebSocket PTY terminal.

WS  /api/terminal/ws   — spawn /bin/bash in a PTY, relay bytes bidirectionally
GET /api/terminal/     — availability check

Message protocol (client → server):
  - Binary/text bytes    → forwarded verbatim to PTY stdin
  - JSON {"type":"resize","cols":N,"rows":N}  → TIOCSWINSZ on master_fd
"""

import asyncio
import fcntl
import json
import os
import pty
import struct
import termios

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter(prefix="/api/terminal", tags=["terminal"])


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


@router.get("/")
def terminal_info():
    return {"available": True, "shell": "/bin/bash"}


@router.websocket("/ws")
async def terminal_ws(websocket: WebSocket):
    await websocket.accept()

    master_fd, slave_fd = pty.openpty()
    _set_winsize(master_fd, 24, 80)

    env = {
        **os.environ,
        "TERM": "xterm-256color",
        "COLORTERM": "truecolor",
        "COLUMNS": "80",
        "LINES": "24",
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
    }

    def _preexec():
        os.setsid()
        try:
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
        except OSError:
            pass

    try:
        proc = await asyncio.create_subprocess_exec(
            "/bin/bash", "--login",
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=_preexec,
            env=env,
            close_fds=True,
        )
    except Exception as exc:
        await websocket.send_text(f"\r\nFailed to start shell: {exc}\r\n")
        await websocket.close()
        try:
            os.close(master_fd)
            os.close(slave_fd)
        except OSError:
            pass
        return

    os.close(slave_fd)

    # Non-blocking reads from master_fd
    flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    loop = asyncio.get_event_loop()
    pty_queue: asyncio.Queue[bytes] = asyncio.Queue()
    done = asyncio.Event()

    def _on_readable():
        try:
            data = os.read(master_fd, 4096)
            if data:
                pty_queue.put_nowait(data)
        except (BlockingIOError, InterruptedError):
            pass
        except OSError:
            loop.remove_reader(master_fd)
            done.set()

    loop.add_reader(master_fd, _on_readable)

    async def _send_loop():
        while not done.is_set():
            try:
                data = await asyncio.wait_for(pty_queue.get(), timeout=0.3)
                await websocket.send_bytes(data)
            except asyncio.TimeoutError:
                continue
            except Exception:
                done.set()
                break

    async def _recv_loop():
        while not done.is_set():
            try:
                msg = await websocket.receive()
            except WebSocketDisconnect:
                break
            except Exception:
                break

            if msg.get("type") == "websocket.disconnect":
                break

            raw: bytes | None = msg.get("bytes")
            if raw is None and msg.get("text"):
                raw = msg["text"].encode()
            if not raw:
                continue

            # Detect JSON resize message
            if raw[:1] == b"{":
                try:
                    obj = json.loads(raw)
                    if obj.get("type") == "resize":
                        _set_winsize(master_fd,
                                     int(obj.get("rows", 24)),
                                     int(obj.get("cols", 80)))
                        continue
                except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
                    pass

            try:
                os.write(master_fd, raw)
            except OSError:
                done.set()
                break

        done.set()

    try:
        await asyncio.gather(_send_loop(), _recv_loop())
    finally:
        done.set()
        loop.remove_reader(master_fd)
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            os.close(master_fd)
        except OSError:
            pass
