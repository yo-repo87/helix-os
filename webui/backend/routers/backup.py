"""
Backup job management (rsync / rclone).

GET    /api/backup/jobs              — list jobs
POST   /api/backup/jobs              — create job
PUT    /api/backup/jobs/{id}         — update job
DELETE /api/backup/jobs/{id}         — delete job
POST   /api/backup/jobs/{id}/run     — run job now (background)
GET    /api/backup/jobs/{id}/stream  — SSE live log
POST   /api/backup/jobs/{id}/stop    — stop running job
"""

import asyncio
import json
import os
import secrets
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from config import HELIX_CONFIG, STATE_DIR

router = APIRouter(prefix="/api/backup", tags=["backup"])

JOBS_FILE  = os.path.join(HELIX_CONFIG, "backup_jobs.json")
STATE_FILE = os.path.join(STATE_DIR,   "backup_state.json")

# Running processes keyed by job id
_procs: dict[str, asyncio.subprocess.Process] = {}


# ── Persistence ───────────────────────────────────────────────────

def _load_jobs() -> list[dict]:
    try:
        return json.loads(Path(JOBS_FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def _save_jobs(jobs: list[dict]):
    Path(JOBS_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(JOBS_FILE).write_text(json.dumps(jobs, indent=2))

def _load_state() -> dict:
    try:
        return json.loads(Path(STATE_FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def _save_state(s: dict):
    Path(STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(STATE_FILE).write_text(json.dumps(s))

def _get_job(job_id: str) -> dict:
    for j in _load_jobs():
        if j["id"] == job_id:
            return j
    raise HTTPException(404, "Job not found")


# ── Schemas ───────────────────────────────────────────────────────

class JobCreate(BaseModel):
    name:        str
    source:      str               # local path, e.g. /mnt/user/Movies
    dest:        str               # local path OR user@host:/path OR rclone:remote:path
    delete:      bool = False      # --delete flag
    compress:    bool = False      # -z flag (SSH only)
    exclude:     str  = ""         # space-separated patterns
    ssh_port:    int  = 22
    schedule:    str  = ""         # cron expression (empty = manual only)

class JobUpdate(JobCreate):
    pass


# ── Helpers ───────────────────────────────────────────────────────

def _build_cmd(job: dict) -> list[str]:
    dest: str = job["dest"]
    is_rclone = ":" in dest and not dest.startswith("/") and not dest.startswith("~")
    is_ssh    = "@" in dest.split(":")[0] if ":" in dest else False

    if is_rclone:
        cmd = ["rclone", "sync", "-v", job["source"], dest]
        if job.get("exclude"):
            for pat in job["exclude"].split():
                cmd += ["--exclude", pat]
        return cmd

    # rsync
    flags = ["-avh", "--progress"]
    if job.get("delete"):
        flags.append("--delete")
    if job.get("compress") and is_ssh:
        flags.append("-z")
    if job.get("exclude"):
        for pat in job["exclude"].split():
            flags += ["--exclude", pat]
    if is_ssh and job.get("ssh_port", 22) != 22:
        flags += ["-e", f"ssh -p {job['ssh_port']}"]

    src = job["source"].rstrip("/") + "/"
    return ["rsync"] + flags + [src, dest]


# ── Endpoints ─────────────────────────────────────────────────────

@router.get("/jobs")
def list_jobs():
    jobs  = _load_jobs()
    state = _load_state()
    for j in jobs:
        s = state.get(j["id"], {})
        j["last_run"]    = s.get("last_run")
        j["last_status"] = s.get("last_status", "never")
        j["running"]     = j["id"] in _procs
    return jobs


@router.post("/jobs")
def create_job(req: JobCreate):
    jobs = _load_jobs()
    job  = req.model_dump()
    job["id"] = secrets.token_hex(6)
    jobs.append(job)
    _save_jobs(jobs)
    return job


@router.put("/jobs/{job_id}")
def update_job(job_id: str, req: JobUpdate):
    jobs = _load_jobs()
    for i, j in enumerate(jobs):
        if j["id"] == job_id:
            updated = req.model_dump()
            updated["id"] = job_id
            jobs[i] = updated
            _save_jobs(jobs)
            return updated
    raise HTTPException(404, "Job not found")


@router.delete("/jobs/{job_id}")
def delete_job(job_id: str):
    jobs = [j for j in _load_jobs() if j["id"] != job_id]
    _save_jobs(jobs)
    return {"ok": True}


@router.post("/jobs/{job_id}/run")
async def run_job(job_id: str):
    if job_id in _procs:
        raise HTTPException(409, "Job already running")
    job = _get_job(job_id)
    cmd = _build_cmd(job)
    # Fire and forget — log captured by /stream
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    _procs[job_id] = proc
    # Background waiter to clean up
    async def _wait():
        await proc.wait()
        rc = proc.returncode
        _procs.pop(job_id, None)
        state = _load_state()
        state[job_id] = {
            "last_run":    int(time.time()),
            "last_status": "ok" if rc == 0 else f"error (rc={rc})",
        }
        _save_state(state)
    asyncio.create_task(_wait())
    return {"ok": True, "cmd": cmd}


@router.get("/jobs/{job_id}/stream")
async def stream_job(job_id: str):
    async def _gen():
        proc = _procs.get(job_id)
        if proc is None:
            yield "data: ERROR: job not running — use POST /run first\n\n"
            return
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            yield f"data: {line}\n\n"
        rc = proc.returncode if proc.returncode is not None else 0
        yield f"data: {'DONE' if rc == 0 else f'ERROR rc={rc}'}\n\n"
    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/jobs/{job_id}/stop")
def stop_job(job_id: str):
    proc = _procs.get(job_id)
    if proc is None:
        return {"ok": True, "message": "Not running"}
    proc.terminate()
    _procs.pop(job_id, None)
    return {"ok": True}
