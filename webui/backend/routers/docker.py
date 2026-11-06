from fastapi import APIRouter, HTTPException
import subprocess, json, os
from config import DOCKER_SOCKET

router = APIRouter(prefix="/api/docker", tags=["docker"])

def _docker(*args) -> tuple[int, str]:
    r = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=10)
    return r.returncode, r.stdout.strip()

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
            containers.append(json.loads(line))
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
    rc, out = _docker("stop", name)
    if rc != 0:
        raise HTTPException(500, out)
    return {"ok": True}

@router.post("/containers/{name}/restart")
def restart_container(name: str):
    rc, out = _docker("restart", name)
    if rc != 0:
        raise HTTPException(500, out)
    return {"ok": True}

@router.get("/containers/{name}/logs")
def container_logs(name: str, lines: int = 50):
    rc, out = _docker("logs", "--tail", str(lines), name)
    return {"lines": out.splitlines()}

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
