"""
LAN device scanner — nmap ping sweep.

POST /api/scanner/scan?subnet=   — scan subnet (e.g. 192.168.1.0/24)
GET  /api/scanner/hosts          — return cached results
GET  /api/scanner/status         — scan in progress?
"""

import asyncio
import json
import os
import re
import subprocess
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from config import STATE_DIR

router = APIRouter(prefix="/api/scanner", tags=["scanner"])

HOSTS_FILE = os.path.join(STATE_DIR, "scanner_hosts.json")
_scan_proc: asyncio.subprocess.Process | None = None
_scan_running = False


def _load_hosts() -> dict:
    try:
        return json.loads(Path(HOSTS_FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"hosts": [], "scanned_at": None, "subnet": ""}


def _save_hosts(d: dict):
    Path(HOSTS_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(HOSTS_FILE).write_text(json.dumps(d))


def _nmap_available() -> bool:
    r = subprocess.run(["which", "nmap"], capture_output=True)
    return r.returncode == 0


def _detect_subnet() -> str:
    """Best-guess subnet from default route interface."""
    try:
        r = subprocess.run(["ip", "-o", "-4", "addr", "show"], capture_output=True, text=True)
        for line in r.stdout.splitlines():
            # Skip loopback
            if " lo " in line:
                continue
            m = re.search(r'inet (\d+\.\d+\.\d+\.\d+)/(\d+)', line)
            if m:
                ip, prefix = m.group(1), m.group(2)
                parts = ip.split(".")
                return f"{parts[0]}.{parts[1]}.{parts[2]}.0/{prefix}"
    except Exception:
        pass
    return "192.168.1.0/24"


def _parse_nmap_output(text: str) -> list[dict]:
    hosts = []
    current: dict | None = None
    for line in text.splitlines():
        m = re.match(r'Nmap scan report for (.+)', line)
        if m:
            current = {"host": m.group(1), "ip": "", "mac": "", "vendor": "", "hostname": ""}
            # Extract IP if host is "hostname (ip)"
            mi = re.search(r'\((\d+\.\d+\.\d+\.\d+)\)', m.group(1))
            if mi:
                current["ip"] = mi.group(1)
                current["hostname"] = m.group(1).split("(")[0].strip()
            else:
                current["ip"] = m.group(1).strip()
            hosts.append(current)
        elif current and line.strip().startswith("MAC Address:"):
            m2 = re.search(r'MAC Address: ([\dA-F:]+)(?: \((.+)\))?', line)
            if m2:
                current["mac"]    = m2.group(1)
                current["vendor"] = m2.group(2) or ""
    return hosts


@router.get("/hosts")
def get_hosts():
    return _load_hosts()


@router.get("/status")
def scan_status():
    return {"running": _scan_running}


@router.post("/scan")
async def scan(subnet: str = ""):
    global _scan_proc, _scan_running
    if _scan_running:
        raise HTTPException(409, "Scan already in progress")
    if not _nmap_available():
        raise HTTPException(503, "nmap is not installed — run: sudo apt-get install nmap")
    if not subnet:
        subnet = _detect_subnet()

    async def _gen():
        global _scan_proc, _scan_running
        _scan_running = True
        output_lines = []
        try:
            _scan_proc = await asyncio.create_subprocess_exec(
                "nmap", "-sn", "--host-timeout", "3s", subnet,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            async for raw in _scan_proc.stdout:
                line = raw.decode(errors="replace").rstrip()
                output_lines.append(line)
                yield f"data: {line}\n\n"
            await _scan_proc.wait()
        except Exception as e:
            yield f"data: ERROR: {e}\n\n"
        finally:
            _scan_running = False
            _scan_proc = None

        hosts = _parse_nmap_output("\n".join(output_lines))
        _save_hosts({"hosts": hosts, "scanned_at": int(time.time()), "subnet": subnet})
        yield f"data: DONE:{len(hosts)}\n\n"

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/scan/stop")
def stop_scan():
    global _scan_proc, _scan_running
    if _scan_proc:
        _scan_proc.terminate()
    _scan_running = False
    return {"ok": True}
