"""
Network traffic history via vnstat.

GET /api/vnstat/interfaces          — list monitored interfaces
GET /api/vnstat/traffic?iface=&period=  — hourly|daily|monthly stats
GET /api/vnstat/live?iface=         — SSE live 1-second samples
"""

import asyncio
import json
import re
import subprocess

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/api/vnstat", tags=["vnstat"])


def _vnstat_available() -> bool:
    r = subprocess.run(["which", "vnstat"], capture_output=True)
    return r.returncode == 0


def _run_vnstat(*args) -> dict | None:
    try:
        r = subprocess.run(["vnstat", "--json"] + list(args),
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return json.loads(r.stdout)
    except Exception:
        pass
    return None


@router.get("/interfaces")
def list_interfaces():
    if not _vnstat_available():
        return {"available": False, "interfaces": []}
    data = _run_vnstat()
    if not data:
        return {"available": True, "interfaces": []}
    ifaces = [i.get("name") for i in data.get("interfaces", [])]
    return {"available": True, "interfaces": ifaces}


@router.get("/traffic")
def get_traffic(iface: str = "", period: str = "daily"):
    """
    period: hourly | daily | monthly
    Returns list of {time, rx_bytes, tx_bytes} samples.
    """
    if not _vnstat_available():
        raise HTTPException(503, "vnstat not installed")

    period_flag = {"hourly": "-h", "daily": "-d", "monthly": "-m"}.get(period, "-d")
    args = [period_flag]
    if iface:
        args = ["-i", iface] + args

    data = _run_vnstat(*args)
    if not data:
        raise HTTPException(500, "vnstat returned no data")

    rows = []
    for iface_data in data.get("interfaces", []):
        name = iface_data.get("name", "")
        traffic = iface_data.get("traffic", {})

        if period == "hourly":
            samples = traffic.get("hour", [])
            for s in samples:
                d = s.get("date", {})
                t = s.get("time", {})
                rows.append({
                    "iface":    name,
                    "time":     f"{d.get('year',0)}-{d.get('month',1):02d}-{d.get('day',1):02d} {t.get('hour',0):02d}:00",
                    "rx":       s.get("rx", 0),
                    "tx":       s.get("tx", 0),
                })
        elif period == "daily":
            samples = traffic.get("day", [])
            for s in samples:
                d = s.get("date", {})
                rows.append({
                    "iface": name,
                    "time":  f"{d.get('year',0)}-{d.get('month',1):02d}-{d.get('day',1):02d}",
                    "rx":    s.get("rx", 0),
                    "tx":    s.get("tx", 0),
                })
        elif period == "monthly":
            samples = traffic.get("month", [])
            for s in samples:
                d = s.get("date", {})
                rows.append({
                    "iface": name,
                    "time":  f"{d.get('year',0)}-{d.get('month',1):02d}",
                    "rx":    s.get("rx", 0),
                    "tx":    s.get("tx", 0),
                })

    return {"period": period, "rows": rows}


@router.get("/live")
async def live_traffic(iface: str = ""):
    """SSE: one sample per second from vnstat -tr 1."""
    args = ["vnstat", "-tr", "1"]
    if iface:
        args = ["vnstat", "-i", iface, "-tr", "1"]

    async def _gen():
        while True:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await proc.communicate()
                text = stdout.decode(errors="replace")
                # Parse rx/tx lines
                rx = tx = 0
                for line in text.splitlines():
                    m = re.search(r'rx\s+([\d.]+)\s+(\S+)', line, re.IGNORECASE)
                    if m:
                        val, unit = float(m.group(1)), m.group(2).lower()
                        rx = _to_bytes(val, unit)
                    m = re.search(r'tx\s+([\d.]+)\s+(\S+)', line, re.IGNORECASE)
                    if m:
                        val, unit = float(m.group(1)), m.group(2).lower()
                        tx = _to_bytes(val, unit)
                import time
                yield f"data: {json.dumps({'rx': rx, 'tx': tx, 'ts': int(time.time())})}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _to_bytes(val: float, unit: str) -> int:
    units = {"b": 1, "kb": 1000, "mb": 1_000_000, "gb": 1_000_000_000,
             "kib": 1024, "mib": 1048576, "gib": 1073741824}
    return int(val * units.get(unit.lower().rstrip("/s"), 1))
