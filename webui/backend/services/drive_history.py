"""
Drive SMART attribute time-series history.
Appends one sample per hour per drive to STATE_DIR/drive_history.json.
Stores at most 7 days of hourly samples (168 per drive).
"""

import asyncio
import json
import os
import re
import subprocess
import time
from pathlib import Path

from config import STATE_DIR, HELIX_CONFIG, DISKS_CFG

HISTORY_FILE = os.path.join(STATE_DIR, "drive_history.json")
MAX_SAMPLES  = 168  # 7 days × 24 hours
INTERVAL_S   = 3600  # 1 hour


def _load() -> dict:
    try:
        return json.loads(Path(HISTORY_FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(d: dict):
    Path(HISTORY_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(HISTORY_FILE).write_text(json.dumps(d))


def _get_devices() -> list[tuple[str, str]]:
    """Return [(name, device)] from disks.cfg."""
    pairs = []
    try:
        cfg = Path(DISKS_CFG).read_text()
    except FileNotFoundError:
        return pairs
    section = device = None
    for line in cfg.splitlines():
        m = re.match(r'^\[(\w+)\]$', line.strip())
        if m:
            section = m.group(1)
            device = None
        elif line.strip().startswith("device="):
            device = line.strip().split("=", 1)[1].strip()
            if section and device and section.startswith("disk"):
                pairs.append((section, device))
    return pairs


def _smart_sample(device: str) -> dict | None:
    try:
        r = subprocess.run(["smartctl", "-A", device],
                           capture_output=True, text=True, timeout=10)
        sample = {"ts": int(time.time())}
        for line in r.stdout.splitlines():
            # Reallocated Sectors
            if "Reallocated_Sector" in line:
                m = re.search(r'(\d+)\s*$', line)
                if m:
                    sample["reallocated"] = int(m.group(1))
            # Temperature
            if "Temperature_Celsius" in line or "Airflow_Temperature" in line:
                m = re.search(r'(\d+)\s*$', line)
                if m:
                    sample["temp_c"] = int(m.group(1))
            # Power-On Hours
            if "Power_On_Hours" in line:
                m = re.search(r'(\d+)\s*$', line)
                if m:
                    sample["power_on_hours"] = int(m.group(1))
            # Pending Sectors
            if "Current_Pending_Sector" in line:
                m = re.search(r'(\d+)\s*$', line)
                if m:
                    sample["pending"] = int(m.group(1))
            # Uncorrectable errors
            if "Offline_Uncorrectable" in line:
                m = re.search(r'(\d+)\s*$', line)
                if m:
                    sample["uncorrectable"] = int(m.group(1))
        return sample if len(sample) > 1 else None
    except Exception:
        return None


def _record_sample():
    history = _load()
    for name, device in _get_devices():
        sample = _smart_sample(device)
        if sample is None:
            continue
        series = history.setdefault(name, [])
        series.append(sample)
        # Keep only most recent MAX_SAMPLES
        if len(series) > MAX_SAMPLES:
            history[name] = series[-MAX_SAMPLES:]
    _save(history)


def get_history(drive_name: str) -> list[dict]:
    return _load().get(drive_name, [])


async def history_poll_loop():
    """Background task: record one sample per hour."""
    while True:
        try:
            _record_sample()
        except Exception:
            pass
        await asyncio.sleep(INTERVAL_S)
