from fastapi import APIRouter, HTTPException
import subprocess, json, os, re, threading, time
from config import DOCKER_SOCKET, HELIX_CONFIG

router = APIRouter(prefix="/api/docker", tags=["docker"])

def _docker(*args, timeout: int = 10, merge_stderr: bool = False) -> tuple[int, str]:
    """Run the docker CLI. stop/restart/rm can take a while (a container gets 10 s to
    exit cleanly before SIGKILL), so callers pass a longer timeout; logs need stderr,
    which is where most apps write."""
    try:
        r = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, f"docker {args[0]} timed out after {timeout}s")
    except FileNotFoundError:
        raise HTTPException(503, "Docker is not installed on this system")
    out = (r.stdout + r.stderr) if merge_stderr else r.stdout
    return r.returncode, out.strip() if r.returncode == 0 or merge_stderr else (r.stderr.strip() or r.stdout.strip())


def _is_helix_app(name: str) -> bool:
    """True when the container belongs to an app installed through Helix (Apps page)."""
    return os.path.isfile(os.path.join(HELIX_CONFIG, "apps", name, "docker-compose.yml"))

def _app_name(name: str, project: str = "") -> str:
    """The Helix app a container belongs to, or "". By name first; then by its compose project label, so a leftover duplicate
    such as "b4d1a98c331c_emby" (compose renames the old container when it recreates one) still maps to the app "emby"."""
    if _is_helix_app(name):
        return name
    if not project:
        rc, out = _docker("inspect", "-f", '{{index .Config.Labels "com.docker.compose.project"}}', name, timeout=15)
        project = out.strip() if rc == 0 else ""
    return project if project and _is_helix_app(project) else ""


def _docker_available() -> bool:
    return os.path.exists(DOCKER_SOCKET)

@router.get("/containers")
def list_containers():
    if not _docker_available():
        return {"available": False, "containers": []}
    rc, out = _docker(
        "ps", "-a",
        "--format", '{"id":"{{.ID}}","name":"{{.Names}}","image":"{{.Image}}",'
                    '"status":"{{.Status}}","state":"{{.State}}","ports":"{{.Ports}}",'
                    '"created":"{{.CreatedAt}}","size":"{{.Size}}",'
                    '"project":"{{.Label "com.docker.compose.project"}}"}'
    )
    if rc != 0:
        return {"available": True, "containers": [], "error": out}
    containers = []
    for line in out.splitlines():
        try:
            c = json.loads(line)
            c["app_name"] = _app_name(c.get("name", ""), c.pop("project", "") or "-")
            c["app"] = bool(c["app_name"])                   # belongs to an app with a compose project + settings to edit
            containers.append(c)
        except json.JSONDecodeError:
            pass
    return {"available": True, "containers": containers}

@router.post("/containers/{name}/start")
def start_container(name: str):
    rc, out = _docker("start", name)
    if rc != 0:
        raise HTTPException(500, out)
    return {"ok": True}

@router.post("/containers/{name}/stop")
def stop_container(name: str):
    rc, out = _docker("stop", name, timeout=60)
    if rc != 0:
        raise HTTPException(500, out)
    return {"ok": True}

@router.post("/containers/{name}/restart")
def restart_container(name: str):
    rc, out = _docker("restart", name, timeout=60)
    if rc != 0:
        raise HTTPException(500, out)
    return {"ok": True}

@router.get("/containers/{name}/logs")
def container_logs(name: str, lines: int = 200):
    rc, out = _docker("logs", "--tail", str(max(1, min(lines, 2000))), name, merge_stderr=True)
    if rc != 0:
        raise HTTPException(404, out or f"no such container: {name}")
    return {"lines": out.splitlines()}


@router.get("/containers/{name}")
def container_details(name: str):
    """What the Manage dialog shows. Environment VALUES are never returned (tokens)."""
    rc, out = _docker("inspect", name)
    if rc != 0:
        raise HTTPException(404, out or f"no such container: {name}")
    d = json.loads(out)[0]
    cfg, host = d.get("Config", {}), d.get("HostConfig", {})
    return {
        "name": d.get("Name", "").lstrip("/"), "image": cfg.get("Image"), "state": d.get("State", {}).get("Status"),
        "created": d.get("Created"), "restart_policy": (host.get("RestartPolicy") or {}).get("Name") or "no",
        "networks": list((d.get("NetworkSettings", {}).get("Networks") or {}).keys()),
        "ports": sorted({f"{b.get('HostPort')}→{p}" for p, bs in (d.get("NetworkSettings", {}).get("Ports") or {}).items() for b in (bs or [])}),
        "mounts": [{"source": m.get("Source"), "target": m.get("Destination"), "rw": m.get("RW")} for m in d.get("Mounts", [])],
        "env_names": sorted(e.split("=", 1)[0] for e in (cfg.get("Env") or [])),
        "app": _is_helix_app(name),
        "protected": name in ("checkmk", "netdata"),
    }


@router.delete("/containers/{name}")
def remove_container(name: str, volumes: bool = False):
    """Remove a container. Containers installed through Helix are removed via the app
    API so their compose project and saved settings go too (otherwise they would
    still be listed as installed). Your data folders are never deleted."""
    if name in ("checkmk", "netdata"):
        raise HTTPException(409, "This is Helix's monitoring server (Monitoring tab). It cannot be removed here: use Monitoring → Turn off to stop it. It is removed only when the array is deleted.")
    app = _app_name(name)
    if app:
        # Removing any container of a Helix app removes the app (compose down + its saved record), otherwise the catalog
        # keeps saying "installed" for something that no longer exists. A renamed leftover is removed explicitly too.
        from routers.apps import remove_app
        res = remove_app(app, purge=True)
        if name != app:
            _docker("rm", "-f", name, timeout=60)
        return res
    rc, out = _docker("rm", "-f", *(["-v"] if volumes else []), name, timeout=60)
    if rc != 0:
        raise HTTPException(500, out)
    return {"ok": True, "message": f"Removed {name}"}

_UNITS = {"b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12,
          "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4}


def _bytes(txt: str) -> float:
    """'12.3MiB', '1.5kB', '0B' -> bytes."""
    m = re.match(r"\s*([\d.]+)\s*([A-Za-z]*)", txt or "")
    if not m:
        return 0.0
    try:
        return float(m.group(1)) * _UNITS.get(m.group(2).lower() or "b", 1)
    except ValueError:
        return 0.0


def _pair(txt: str) -> tuple[float, float]:
    a, _, b = (txt or "").partition("/")
    return _bytes(a), _bytes(b)


_stats_lock = threading.Lock()
_stats_last: dict = {"t": 0.0, "stats": [], "net": {}}


@router.get("/stats")
def docker_stats():
    """Live per-container CPU / memory / network. Network rates are the change in docker's
    cumulative counters between two samples, so they need at least two polls to appear.
    Samples are shared: callers within 2 s get the previous result (docker stats is slow)."""
    if not _docker_available():
        return {"available": False}
    with _stats_lock:
        now = time.time()
        if now - _stats_last["t"] < 2.0 and _stats_last["stats"]:
            return {"available": True, "stats": _stats_last["stats"]}
        rc, out = _docker(
            "stats", "--no-stream", "--format",
            '{"name":"{{.Name}}","cpu":"{{.CPUPerc}}","mem":"{{.MemUsage}}","net":"{{.NetIO}}"}',
            timeout=20)
        stats, prev = [], _stats_last["net"]
        net: dict = {}
        for line in out.splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            used, limit = _pair(r.get("mem", ""))
            rx, tx = _pair(r.get("net", ""))
            name = r.get("name", "")
            rx_bps = tx_bps = None
            if name in prev:
                t0, rx0, tx0 = prev[name]
                dt = now - t0
                if dt > 0.5 and rx >= rx0 and tx >= tx0:      # a restarted container resets its counters
                    rx_bps, tx_bps = (rx - rx0) / dt, (tx - tx0) / dt
            net[name] = (now, rx, tx)
            try:
                cpu = float(r.get("cpu", "0").strip("%"))
            except ValueError:
                cpu = 0.0
            stats.append({**r, "cpu_pct": cpu, "mem_bytes": used, "mem_limit": limit,
                          "rx_bytes": rx, "tx_bytes": tx, "rx_bps": rx_bps, "tx_bps": tx_bps})
        _stats_last.update(t=now, stats=stats, net=net)
        return {"available": True, "stats": stats}
