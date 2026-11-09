"""
Background service: polls smartctl every 60 s for all configured drives
and writes results to STATE_DIR/drives.json.

The drives router reads from that cache so the dashboard never waits for
smartctl to respond (which can take 2-5 s per drive).
"""

import asyncio
import json
import os
import re
import subprocess
import time
from pathlib import Path

from config import DISKS_CFG, STATE_DIR

DRIVES_CACHE = os.path.join(STATE_DIR, "drives.json")
_ALERTS_FILE  = os.path.join(STATE_DIR, "drive_alerts.json")
POLL_INTERVAL = 60  # seconds

# Thresholds for notification triggers
TEMP_WARN_C   = 48
TEMP_ERR_C    = 55
REALLOC_WARN  = 1    # any realloc sectors → warning


# ── INI helpers ───────────────────────────────────────────────────────────────

def _parse_disks_cfg() -> list[dict]:
    """Return list of {name, device, role} from disks.cfg."""
    drives = []
    try:
        content = Path(DISKS_CFG).read_text()
    except FileNotFoundError:
        return drives

    section = None
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r'^\[(\w+)\]$', line)
        if m:
            section = m.group(1)
            if section not in ("sharedefaults",):
                role = ("parity" if section.startswith("parity")
                        else "cache" if section.startswith(("cache", "pool"))
                        else "data")
                drives.append({"name": section, "device": None, "role": role})
            continue
        if section and line.startswith("device=") and drives and drives[-1]["name"] == section:
            drives[-1]["device"] = line.split("=", 1)[1].strip()

    return [d for d in drives if d["device"]]


# ── smartctl helpers ──────────────────────────────────────────────────────────

_SMART_ATTRS = {
    "1":   "raw_read_error_rate",
    "5":   "reallocated_sectors",
    "9":   "power_on_hours",
    "187": "reported_uncorrectable",
    "190": "airflow_temp_c",
    "194": "temperature_c",
    "197": "pending_sectors",
    "198": "offline_uncorrectable",
    "199": "udma_crc_errors",
}


def _smartctl_json(device: str, *flags) -> dict | None:
    """Run smartctl with -j and return parsed JSON, or None on failure."""
    try:
        r = subprocess.run(
            ["smartctl", *flags, "-j", device],
            capture_output=True, text=True, timeout=10
        )
        return json.loads(r.stdout) if r.stdout.strip() else None
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return None


def _probe_drive(name: str, device: str, role: str) -> dict:
    info = {
        "name": name,
        "device": device,
        "role": role,
        "polled_at": int(time.time()),
        "smart_available": False,
        "smart_passed": None,
        "model": None,
        "serial": None,
        "capacity_gb": None,
        "rpm": None,
        "temp_c": None,
        "power_on_hours": None,
        "reallocated_sectors": None,
        "pending_sectors": None,
        "offline_uncorrectable": None,
        "udma_crc_errors": None,
        "attributes": {},
    }

    # Overall health
    health = _smartctl_json(device, "-H")
    if health:
        info["smart_available"] = True
        status = health.get("smart_status", {})
        info["smart_passed"] = status.get("passed")

    # Drive info + attributes
    data = _smartctl_json(device, "-i", "-A")
    if not data:
        return info

    # Identity
    info["model"]  = data.get("model_name")
    info["serial"] = data.get("serial_number")
    cap = data.get("user_capacity", {})
    if isinstance(cap, dict):
        info["capacity_gb"] = round(cap.get("bytes", 0) / 1e9, 0)
    rpm_val = data.get("rotation_rate")
    info["rpm"] = rpm_val if isinstance(rpm_val, int) and rpm_val > 0 else None

    # NVMe temperature
    nvme_temp = data.get("temperature", {})
    if isinstance(nvme_temp, dict) and "current" in nvme_temp:
        info["temp_c"] = nvme_temp["current"]

    # NVMe power-on hours
    nvme_poh = data.get("power_on_time", {})
    if isinstance(nvme_poh, dict) and "hours" in nvme_poh:
        info["power_on_hours"] = nvme_poh["hours"]

    # ATA attributes table
    for attr in data.get("ata_smart_attributes", {}).get("table", []):
        attr_id = str(attr.get("id", ""))
        raw = attr.get("raw", {})
        raw_val = raw.get("value") if isinstance(raw, dict) else None

        if attr_id in _SMART_ATTRS:
            key = _SMART_ATTRS[attr_id]
            info[key] = raw_val
            if key in ("temperature_c", "airflow_temp_c") and info["temp_c"] is None:
                info["temp_c"] = raw_val

        # Store all in attributes dict for the full-detail endpoint
        info["attributes"][attr_id] = {
            "name":  attr.get("name"),
            "value": attr.get("value"),
            "worst": attr.get("worst"),
            "thresh": attr.get("thresh"),
            "raw":   raw_val,
            "flags": attr.get("flags", {}).get("string", ""),
        }

    # Normalise temp field name
    if info["temp_c"] is None and info.get("airflow_temp_c") is not None:
        info["temp_c"] = info["airflow_temp_c"]

    return info


# ── Polling loop ──────────────────────────────────────────────────────────────

def _poll_all() -> list[dict]:
    drives = _parse_disks_cfg()
    return [_probe_drive(d["name"], d["device"], d["role"]) for d in drives]


def _write_cache(data: list[dict]) -> None:
    Path(STATE_DIR).mkdir(parents=True, exist_ok=True)
    tmp = DRIVES_CACHE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, DRIVES_CACHE)


def _load_prev_alerts() -> dict:
    try:
        return json.loads(Path(_ALERTS_FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_prev_alerts(state: dict) -> None:
    Path(_ALERTS_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(_ALERTS_FILE).write_text(json.dumps(state))


def _check_and_notify(drives: list[dict], prev: dict) -> dict:
    """Compare new drive data against previous state; fire notifications for new issues."""
    # Import here to avoid a circular import at module load time
    from services.notifications import notify

    new_state: dict = {}
    for d in drives:
        name   = d["name"]
        alerts = set()

        if d.get("smart_available") and d.get("smart_passed") is False:
            alerts.add("smart_fail")

        realloc = d.get("reallocated_sectors") or 0
        if realloc >= REALLOC_WARN:
            alerts.add("realloc")

        temp = d.get("temp_c")
        if temp is not None:
            if temp >= TEMP_ERR_C:
                alerts.add("temp_critical")
            elif temp >= TEMP_WARN_C:
                alerts.add("temp_warn")

        new_state[name] = list(alerts)

        prev_alerts = set(prev.get(name, []))
        new_alerts  = alerts - prev_alerts

        for alert in new_alerts:
            if alert == "smart_fail":
                notify("error", f"Drive failure: {name}",
                       f"SMART self-assessment FAILED on {d['device']} ({d.get('model','?')}).",
                       source=f"drive:{name}")
            elif alert == "realloc":
                notify("warning", f"Drive warning: {name}",
                       f"{realloc} reallocated sector(s) on {d['device']} ({d.get('model','?')}). "
                       "This may indicate impending drive failure.",
                       source=f"drive:{name}")
            elif alert == "temp_critical":
                notify("error", f"Drive overheating: {name}",
                       f"{d['device']} temperature is {temp}°C (critical ≥{TEMP_ERR_C}°C).",
                       source=f"drive:{name}")
            elif alert == "temp_warn":
                notify("warning", f"Drive temperature warning: {name}",
                       f"{d['device']} temperature is {temp}°C (warning ≥{TEMP_WARN_C}°C).",
                       source=f"drive:{name}")

    return new_state


async def poll_loop() -> None:
    """Runs forever; call once at startup via asyncio.create_task."""
    prev_alerts: dict = {}
    while True:
        try:
            data = await asyncio.get_event_loop().run_in_executor(None, _poll_all)
            _write_cache(data)
            prev_alerts = await asyncio.get_event_loop().run_in_executor(
                None, _check_and_notify, data, prev_alerts
            )
        except Exception:
            pass  # never crash the loop
        await asyncio.sleep(POLL_INTERVAL)
