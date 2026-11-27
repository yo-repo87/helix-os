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

# ── Per-core load (dashboard Processor tile) ──────────────────────
# Load is computed from the change in /proc/stat times between two calls,
# so a poll never blocks; the first call (or one after a long gap) samples 0.2 s.
_prev_times = None
_prev_at = 0.0
_topology = None
_model = None

def _cpu_topology():
    """[[thread, sibling...], ...] grouped by physical core, ordered by first thread."""
    global _topology
    if _topology is not None:
        return _topology
    cores = {}
    n = psutil.cpu_count() or 1
    for cpu in range(n):
        base = f"/sys/devices/system/cpu/cpu{cpu}/topology/"
        try:
            pkg = open(base + "physical_package_id").read().strip()
            core = open(base + "core_id").read().strip()
        except OSError:
            pkg, core = "0", str(cpu)
        cores.setdefault((pkg, core), []).append(cpu)
    _topology = sorted((sorted(v) for v in cores.values()), key=lambda t: t[0])
    return _topology

def _cpu_model():
    global _model
    if _model is None:
        _model = ""
        try:
            for line in open("/proc/cpuinfo"):
                if line.startswith(("model name", "Model")):
                    _model = line.split(":", 1)[1].strip()
                    break
        except OSError:
            pass
    return _model

def _busy(t):
    idle = t.idle + getattr(t, "iowait", 0)
    return sum(t) - idle, sum(t)

@router.get("/cpu")
def cpu_cores():
    global _prev_times, _prev_at
    now = time.time()
    if _prev_times is None or now - _prev_at > 30:
        _prev_times = psutil.cpu_times(percpu=True)
        time.sleep(0.2)
        now = time.time()
    cur = psutil.cpu_times(percpu=True)
    pct = []
    for a, b in zip(_prev_times, cur):
        (ba, ta), (bb, tb) = _busy(a), _busy(b)
        dt = tb - ta
        pct.append(round(max(0.0, min(100.0, (bb - ba) / dt * 100)), 1) if dt > 0 else 0.0)
    _prev_times, _prev_at = cur, now

    mhz = []
    try:
        mhz = [round(f.current) for f in (psutil.cpu_freq(percpu=True) or [])]
    except Exception:
        pass
    temp_c = None
    try:
        temps = psutil.sensors_temperatures()
        for key in ("coretemp", "k10temp", "cpu_thermal", "acpitz"):
            if temps.get(key):
                temp_c = round(temps[key][0].current, 1)
                break
    except Exception:
        pass
    return {
        "model": _cpu_model(),
        "total_pct": round(sum(pct) / len(pct), 1) if pct else 0.0,
        "threads": pct,
        "cores": _cpu_topology(),
        "mhz": mhz,
        "temp_c": temp_c,
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
