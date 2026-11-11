"""
Add-ins: optional software that is not in the base image (NVIDIA driver, ...).

GET  /api/addins/?page=gpu     — add-ins offered on a page: hardware found, installed, job state
POST /api/addins/{id}/install  — download to the array + install (background job)
POST /api/addins/{id}/remove
GET  /api/addins/{id}/log      — job log + state

The catalog is `data/addins/index.json`, refreshed from the helix-os repo on GitHub when it can be
reached (so a new add-in needs no OS release). The privileged work is `helix-addin` (see
addins/helix-addin); the manifest is only data and is validated again by that root helper.
"""
import glob
import json
import os
import platform
import subprocess
import threading
import time
import urllib.request

from fastapi import APIRouter, HTTPException

from config import HELIX_CONFIG, STATE_DIR

router = APIRouter(prefix="/api/addins", tags=["addins"])

BUNDLED = os.path.join(os.path.dirname(__file__), "..", "data", "addins", "index.json")
REMOTE = os.environ.get("HELIX_ADDINS_URL",
                        "https://raw.githubusercontent.com/yo-repo87/helix-os/main/webui/backend/data/addins/index.json")
HELPER = os.environ.get("HELIX_ADDIN_BIN", "/usr/local/sbin/helix-addin")
WORK = os.path.join(STATE_DIR, "addins")
_jobs: dict[str, dict] = {}


def _catalog() -> list[dict]:
    cached = os.path.join(WORK, "index.json")
    if not os.path.exists(cached) or time.time() - os.path.getmtime(cached) > 3600:
        try:
            with urllib.request.urlopen(REMOTE, timeout=5) as r:
                data = json.loads(r.read(1_000_000))
            if isinstance(data.get("addins"), list):
                os.makedirs(WORK, exist_ok=True)
                json.dump(data, open(cached, "w"))
        except Exception:
            if os.path.exists(cached):
                os.utime(cached)          # do not retry on every page view while offline
    for path in (cached, BUNDLED):
        try:
            return json.load(open(path))["addins"]
        except (OSError, ValueError, KeyError):
            continue
    return []


def _pci_matches(req: dict) -> bool:
    for d in glob.glob("/sys/bus/pci/devices/*"):
        try:
            vendor = open(d + "/vendor").read().strip().lower()
            cls = open(d + "/class").read().strip().lower()
        except OSError:
            continue
        if req.get("pci_vendor") and vendor != "0x" + req["pci_vendor"].lower():
            continue
        if req.get("pci_class_prefix") and not cls.startswith("0x" + req["pci_class_prefix"]):
            continue
        return True
    return False


def _arch() -> str:
    return {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine(), platform.machine())


def _installed() -> dict:
    try:
        return json.load(open(os.path.join(HELIX_CONFIG, "addins.json")))
    except (OSError, ValueError):
        return {}


def _find(aid: str) -> dict:
    for a in _catalog():
        if a.get("id") == aid:
            return a
    raise HTTPException(404, "Unknown add-in")


def _log_path(aid: str) -> str:
    return os.path.join(WORK, f"{aid}.log")


@router.get("/")
def list_addins(page: str = ""):
    inst = _installed()
    out = []
    for a in _catalog():
        if page and a.get("page") != page:
            continue
        req = a.get("requires") or {}
        if req.get("arch") and _arch() not in req["arch"]:
            continue
        found = _pci_matches(req) if (req.get("pci_vendor") or req.get("pci_class_prefix")) else True
        job = _jobs.get(a["id"], {})
        rec = inst.get(a["id"])
        out.append({
            "id": a["id"], "title": a.get("title"), "description": a.get("description"),
            "size_mb": a.get("size_mb"), "hardware_found": found,
            "installed": bool(rec), "installed_bytes": (rec or {}).get("bytes"),
            "needs_reinstall": bool(rec) and rec.get("kver") != os.uname().release,
            "reboot": bool(a.get("reboot")),
            "job": job.get("state", "idle"),
        })
    return {"addins": out}


def _run_job(aid: str, verb: str, entry: dict):
    job = _jobs[aid]
    os.makedirs(WORK, exist_ok=True)
    with open(_log_path(aid), "w") as log:
        try:
            p = subprocess.Popen(["sudo", "-n", "env", f"HELIX_CONFIG={HELIX_CONFIG}", "python3", HELPER, verb, aid],
                                 stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, text=True)
            p.communicate(json.dumps(entry), timeout=3600)
            job["state"] = "done" if p.returncode == 0 else "error"
        except Exception as e:
            log.write(f"\n{e}\n")
            job["state"] = "error"


def _start(aid: str, verb: str):
    entry = _find(aid)
    if _jobs.get(aid, {}).get("state") == "running":
        raise HTTPException(409, "Already running")
    req = entry.get("requires") or {}
    if verb == "install" and req.get("arch") and _arch() not in req["arch"]:
        raise HTTPException(400, "Not available for this CPU architecture")
    if verb == "install" and (req.get("pci_vendor") or req.get("pci_class_prefix")) and not _pci_matches(req):
        raise HTTPException(400, "The hardware this add-in is for was not found")
    _jobs[aid] = {"state": "running", "started": time.time()}
    threading.Thread(target=_run_job, args=(aid, verb, entry), daemon=True).start()
    return {"ok": True}


@router.post("/{aid}/install")
def install(aid: str):
    return _start(aid, "install")


@router.post("/{aid}/remove")
def remove(aid: str):
    return _start(aid, "remove")


@router.get("/{aid}/log")
def log(aid: str):
    try:
        text = open(_log_path(aid)).read()[-20000:]
    except OSError:
        text = ""
    steps = [l for l in text.splitlines() if l.startswith("STEP ")]
    return {"state": _jobs.get(aid, {}).get("state", "idle"), "log": text, "step": steps[-1] if steps else ""}
