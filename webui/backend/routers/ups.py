"""
UPS monitoring. Supports apcupsd (apcaccess) and NUT (upsc).

GET /api/ups/status  — unified status dict; {available:false} if no UPS found
"""

import subprocess
from fastapi import APIRouter

router = APIRouter(prefix="/api/ups", tags=["ups"])


def _run(*args, timeout: int = 5) -> tuple[int, str, str]:
    try:
        r = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 1, "", ""


def _kv(output: str, sep: str = ":") -> dict:
    result: dict = {}
    for line in output.strip().splitlines():
        if sep in line:
            k, _, v = line.partition(sep)
            result[k.strip()] = v.strip()
    return result


def _float(s: str) -> float | None:
    try:
        return float(s.split()[0])
    except Exception:
        return None


def _apcupsd() -> dict | None:
    rc, out, _ = _run("apcaccess", "-u")
    if rc != 0 or not out.strip():
        return None
    d = _kv(out)
    if not d.get("STATUS"):
        return None

    status = d.get("STATUS", "UNKNOWN")
    return {
        "available":    True,
        "source":       "apcupsd",
        "model":        d.get("MODEL", d.get("UPSNAME", "UPS")),
        "status":       status,
        "online":       "ONLINE" in status,
        "on_battery":   "ONBATT" in status,
        "low_battery":  "LOWBATT" in status,
        "load_pct":     _float(d.get("LOADPCT", "")),
        "battery_pct":  _float(d.get("BCHARGE", "")),
        "runtime_min":  _float(d.get("TIMELEFT", "")),
        "input_v":      _float(d.get("LINEV", "")),
        "output_v":     _float(d.get("OUTPUTV", d.get("LINEV", ""))),
        "temp_c":       _float(d.get("ITEMP", "")),
        "last_xfer":    d.get("LASTXFER", ""),
        "firmware":     d.get("FIRMWARE", ""),
        "serial":       d.get("SERIALNO", ""),
    }


def _nut() -> dict | None:
    rc, out, _ = _run("upsc", "-l")
    if rc != 0 or not out.strip():
        return None
    names = [n.strip() for n in out.strip().splitlines() if n.strip()]
    if not names:
        return None

    rc, out, _ = _run("upsc", names[0])
    if rc != 0:
        return None
    d = _kv(out)

    ups_status = d.get("ups.status", "OL")
    runtime_s = _float(d.get("battery.runtime", ""))
    return {
        "available":    True,
        "source":       "nut",
        "model":        d.get("ups.model", d.get("device.model", names[0])),
        "status":       ups_status,
        "online":       "OL" in ups_status and "OB" not in ups_status,
        "on_battery":   "OB" in ups_status,
        "low_battery":  "LB" in ups_status,
        "load_pct":     _float(d.get("ups.load", "")),
        "battery_pct":  _float(d.get("battery.charge", "")),
        "runtime_min":  round(runtime_s / 60, 1) if runtime_s is not None else None,
        "input_v":      _float(d.get("input.voltage", "")),
        "output_v":     _float(d.get("output.voltage", d.get("input.voltage", ""))),
        "temp_c":       _float(d.get("ups.temperature", "")),
        "last_xfer":    "",
        "firmware":     d.get("ups.firmware", ""),
        "serial":       d.get("device.serial", ""),
    }


@router.get("/status")
def ups_status():
    for probe in (_apcupsd, _nut):
        result = probe()
        if result:
            return result
    return {"available": False}
