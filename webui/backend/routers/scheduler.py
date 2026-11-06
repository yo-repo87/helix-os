"""
Cron job scheduler.

GET    /api/scheduler/jobs          — list all cron jobs
POST   /api/scheduler/jobs          — create a new job
PUT    /api/scheduler/jobs/{id}     — update a job
DELETE /api/scheduler/jobs/{id}     — delete a job
POST   /api/scheduler/jobs/{id}/run — run a job immediately (background)
GET    /api/scheduler/log           — last N lines of cron syslog entries
"""

import hashlib
import os
import re
import subprocess
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])

HELIX_TAG = "# helix-scheduler"


# ── Crontab helpers ───────────────────────────────────────────────

def _read_crontab() -> str:
    r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def _write_crontab(content: str):
    p = subprocess.run(["crontab", "-"], input=content, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"crontab write failed: {p.stderr.strip()}")


def _job_id(schedule: str, command: str) -> str:
    return hashlib.sha1(f"{schedule}|{command}".encode()).hexdigest()[:10]


def _parse_jobs(raw: str) -> list[dict]:
    jobs = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Only lines tagged with our marker
        if HELIX_TAG not in line:
            continue
        # Strip the tag comment at the end
        code, _, meta = line.partition(HELIX_TAG)
        code = code.strip()
        # Parse name from meta: "# helix-scheduler name=My Job"
        name_m = re.search(r'name=([^;]+)', meta)
        name = name_m.group(1).strip() if name_m else ""
        # Enabled/disabled
        enabled = not code.startswith("#PAUSED#")
        if not enabled:
            code = code.lstrip("#PAUSED# ").strip()
        # Split schedule (5 fields) from command
        parts = code.split(None, 5)
        if len(parts) < 6:
            continue
        schedule = " ".join(parts[:5])
        command  = parts[5]
        job_id   = _job_id(schedule, command)
        jobs.append({
            "id":       job_id,
            "name":     name or command[:40],
            "schedule": schedule,
            "command":  command,
            "enabled":  enabled,
        })
    return jobs


def _rebuild_crontab(jobs: list[dict]) -> str:
    raw = _read_crontab()
    # Keep all lines that are NOT our tagged lines
    kept = [l for l in raw.splitlines()
            if HELIX_TAG not in l and not (l.startswith("#PAUSED#") and HELIX_TAG in l)]
    # Rebuild our section
    for j in jobs:
        prefix = "" if j["enabled"] else "#PAUSED# "
        line = f"{prefix}{j['schedule']} {j['command']} {HELIX_TAG} name={j['name']}"
        kept.append(line)
    return "\n".join(kept) + "\n"


# ── Pydantic models ───────────────────────────────────────────────

class JobCreate(BaseModel):
    name: str
    schedule: str          # "0 3 * * *"
    command: str
    enabled: bool = True

class JobUpdate(BaseModel):
    name: Optional[str]     = None
    schedule: Optional[str] = None
    command: Optional[str]  = None
    enabled: Optional[bool] = None


# ── Endpoints ─────────────────────────────────────────────────────

@router.get("/jobs")
def list_jobs():
    return _parse_jobs(_read_crontab())


@router.post("/jobs", status_code=201)
def create_job(body: JobCreate):
    if not re.match(r'^(\S+ ){4}\S+$', body.schedule.strip()):
        raise HTTPException(400, "schedule must be 5 cron fields (e.g. '0 3 * * *')")
    jobs = _parse_jobs(_read_crontab())
    job = {
        "id":       _job_id(body.schedule, body.command),
        "name":     body.name,
        "schedule": body.schedule.strip(),
        "command":  body.command,
        "enabled":  body.enabled,
    }
    # Prevent exact duplicate
    if any(j["id"] == job["id"] for j in jobs):
        raise HTTPException(409, "Identical job already exists")
    jobs.append(job)
    _write_crontab(_rebuild_crontab(jobs))
    return job


@router.put("/jobs/{job_id}")
def update_job(job_id: str, body: JobUpdate):
    jobs = _parse_jobs(_read_crontab())
    idx = next((i for i, j in enumerate(jobs) if j["id"] == job_id), None)
    if idx is None:
        raise HTTPException(404, "Job not found")
    j = jobs[idx]
    if body.name     is not None: j["name"]     = body.name
    if body.schedule is not None: j["schedule"] = body.schedule.strip()
    if body.command  is not None: j["command"]  = body.command
    if body.enabled  is not None: j["enabled"]  = body.enabled
    j["id"] = _job_id(j["schedule"], j["command"])
    jobs[idx] = j
    _write_crontab(_rebuild_crontab(jobs))
    return j


@router.delete("/jobs/{job_id}")
def delete_job(job_id: str):
    jobs = _parse_jobs(_read_crontab())
    before = len(jobs)
    jobs = [j for j in jobs if j["id"] != job_id]
    if len(jobs) == before:
        raise HTTPException(404, "Job not found")
    _write_crontab(_rebuild_crontab(jobs))
    return {"ok": True}


@router.post("/jobs/{job_id}/run")
def run_job(job_id: str, background_tasks: BackgroundTasks):
    jobs = _parse_jobs(_read_crontab())
    job = next((j for j in jobs if j["id"] == job_id), None)
    if not job:
        raise HTTPException(404, "Job not found")
    def _exec():
        subprocess.run(job["command"], shell=True, capture_output=True)
    background_tasks.add_task(_exec)
    return {"ok": True, "message": f"Running: {job['command'][:60]}"}


@router.get("/log")
def get_log(lines: int = 100):
    entries = []
    # Try journalctl for cron
    r = subprocess.run(
        ["journalctl", "-u", "cron", "-u", "crond", "-n", str(lines),
         "--no-pager", "-o", "short-iso"],
        capture_output=True, text=True
    )
    if r.returncode == 0 and r.stdout.strip():
        entries = r.stdout.strip().splitlines()
    else:
        # Fallback: grep /var/log/syslog for CRON
        for path in ("/var/log/syslog", "/var/log/cron"):
            try:
                with open(path) as f:
                    all_lines = [l.rstrip() for l in f if "CRON" in l or "cron" in l]
                entries = all_lines[-lines:]
                break
            except (OSError, PermissionError):
                continue
    return {"lines": entries, "count": len(entries)}
