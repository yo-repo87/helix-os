import socket
from fastapi import APIRouter
import psutil, time, subprocess, os, re

router = APIRouter(prefix="/api/system", tags=["system"])

_boot_time = psutil.boot_time()

@router.get("/stats")
def system_stats():
    cpu     = psutil.cpu_percent(interval=0.2)
    mem     = psutil.virtual_memory()
    swap    = psutil.swap_memory()
    uptime  = int(time.time() - _boot_time)
    load    = os.getloadavg()

    # CPU temp (try thermal zone, fall back to sensors)
    temp_c = None
    try:
        temps = psutil.sensors_temperatures()
        for key in ("coretemp", "cpu_thermal", "k10temp", "acpitz"):
            if key in temps and temps[key]:
                temp_c = round(temps[key][0].current, 1)
                break
    except Exception:
        pass

    return {
        "cpu_pct":     round(cpu, 1),
        "cpu_count":   psutil.cpu_count(),
        "load_1m":     round(load[0], 2),
        "load_5m":     round(load[1], 2),
        "load_15m":    round(load[2], 2),
        "cpu_temp_c":  temp_c,
        "ram_total_gb": round(mem.total / 1e9, 2),
        "ram_used_gb":  round(mem.used  / 1e9, 2),
        "ram_pct":      mem.percent,
        "swap_total_gb": round(swap.total / 1e9, 2),
        "swap_used_gb":  round(swap.used  / 1e9, 2),
        "uptime_sec":   uptime,
        "hostname":     socket.gethostname(),
    }

@router.get("/network")
def network_stats():
    ifaces = {}
    counters = psutil.net_io_counters(pernic=True)
    addrs    = psutil.net_if_addrs()
    stats    = psutil.net_if_stats()
    for name, cnt in counters.items():
        if name == "lo":
            continue
        ip = next(
            (a.address for a in addrs.get(name, []) if a.family.name == "AF_INET"),
            None
        )
        ifaces[name] = {
            "rx_bytes": cnt.bytes_recv,
            "tx_bytes": cnt.bytes_sent,
            "rx_mb":    round(cnt.bytes_recv / 1e6, 1),
            "tx_mb":    round(cnt.bytes_sent / 1e6, 1),
            "ip":       ip,
            "up":       stats[name].isup if name in stats else False,
            "speed_mbps": stats[name].speed if name in stats else 0,
        }
    return ifaces

@router.get("/log")
def array_log(lines: int = 100):
    from config import LOG_PATH
    try:
        result = subprocess.run(
            ["tail", "-n", str(lines), LOG_PATH],
            capture_output=True, text=True
        )
        return {"lines": result.stdout.splitlines()}
    except Exception as e:
        return {"lines": [], "error": str(e)}
