from fastapi import APIRouter, HTTPException
import subprocess, json, os
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
                    '"created":"{{.CreatedAt}}","size":"{{.Size}}"}'
    )
    if rc != 0:
        return {"available": True, "containers": [], "error": out}
    containers = []
    for line in out.splitlines():
        try:
            c = json.loads(line)
            c["app"] = _is_helix_app(c.get("name", ""))      # has a compose project + settings to edit
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
    if _is_helix_app(name):
        from routers.apps import remove_app
        return remove_app(name, purge=True)
    rc, out = _docker("rm", "-f", *(["-v"] if volumes else []), name, timeout=60)
    if rc != 0:
        raise HTTPException(500, out)
    return {"ok": True, "message": f"Removed {name}"}

@router.get("/stats")
def docker_stats():
    if not _docker_available():
        return {"available": False}
    rc, out = _docker(
        "stats", "--no-stream", "--format",
        '{"name":"{{.Name}}","cpu":"{{.CPUPerc}}","mem":"{{.MemUsage}}","net":"{{.NetIO}}"}'
    )
    stats = []
    for line in out.splitlines():
        try:
            stats.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return {"available": True, "stats": stats}
