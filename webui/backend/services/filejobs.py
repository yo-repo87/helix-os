"""Background file jobs (copy / move / delete / chown / chmod) with progress, queue and cancel.

One job runs at a time; later ones wait (state "pending", started automatically) or are held
(state "queued", started by the user). Job state is kept in STATE_DIR/file_jobs.json so a page
reload picks a running job back up. A job that was running when the backend restarted is marked
failed ("interrupted").

Files are copied by this process (as the Helix user) so everything it creates is owned by the
same identity that SMB, NFS and the apps use. Owner/permission jobs run through sudo.
"""
import json
import os
import shutil
import subprocess
import threading
import time
import uuid

from config import STATE_DIR
from services import fileutil

CHUNK = 4 * 1024 * 1024
_TEST_DELAY = float(os.getenv("HELIX_FILEJOBS_DELAY", "0"))   # seconds per chunk; only for tests (RAM disks copy instantly)
MAX_ERRORS = 50
KEEP_FINISHED = 30
_STATE = os.path.join(STATE_DIR, "file_jobs.json")

_lock = threading.RLock()
_jobs: dict[str, dict] = {}
_cancel: dict[str, threading.Event] = {}
_procs: dict[str, subprocess.Popen] = {}


class Cancelled(Exception):
    pass


# ── persistence ──────────────────────────────────────────────────────────────
def _save() -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = _STATE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(list(_jobs.values()), f)
        os.replace(tmp, _STATE)
    except OSError:
        pass


def _load() -> None:
    try:
        with open(_STATE) as f:
            for j in json.load(f):
                if j.get("state") in ("running",):
                    j["state"] = "failed"
                    j["error"] = "Interrupted by a restart of the web service"
                    j["finished"] = j.get("finished") or time.time()
                _jobs[j["id"]] = j
    except (OSError, ValueError):
        pass


_load()


def _prune() -> None:
    done = sorted((j for j in _jobs.values() if j["state"] in ("done", "failed", "cancelled")),
                  key=lambda j: j.get("finished") or 0)
    for j in done[:-KEEP_FINISHED]:
        _jobs.pop(j["id"], None)


# ── public API ───────────────────────────────────────────────────────────────
def list_jobs() -> list[dict]:
    with _lock:
        return sorted((dict(j) for j in _jobs.values()), key=lambda j: j["created"], reverse=True)


def get(job_id: str) -> dict | None:
    with _lock:
        j = _jobs.get(job_id)
        return dict(j) if j else None


def create(action: str, sources: list[str], target: str = "", overwrite: bool = False, sparse: bool = False,
           queue: bool = False, params: dict | None = None) -> dict:
    j = {
        "id": uuid.uuid4().hex[:10], "action": action, "sources": sources, "target": target,
        "overwrite": bool(overwrite), "sparse": bool(sparse), "params": params or {},
        "state": "queued" if queue else "pending", "created": time.time(), "started": None, "finished": None,
        "bytes_total": 0, "bytes_done": 0, "files_total": 0, "files_done": 0,
        "current": "", "speed": 0.0, "error": "", "errors": [], "skipped": 0,
    }
    with _lock:
        _jobs[j["id"]] = j
        _prune()
        _save()
    _kick()
    return dict(j)


def start(job_id: str) -> dict:
    with _lock:
        j = _jobs.get(job_id)
        if not j:
            raise KeyError(job_id)
        if j["state"] != "queued":
            raise ValueError(f"Job is {j['state']}, not queued")
        j["state"] = "pending"
        _save()
    _kick()
    return get(job_id)


def cancel(job_id: str) -> dict:
    with _lock:
        j = _jobs.get(job_id)
        if not j:
            raise KeyError(job_id)
        if j["state"] in ("queued", "pending"):
            j["state"] = "cancelled"
            j["finished"] = time.time()
            _save()
            return dict(j)
        if j["state"] == "running":
            ev = _cancel.get(job_id)
            if ev:
                ev.set()
            p = _procs.get(job_id)
            if p and p.poll() is None:
                p.terminate()
    return get(job_id)


def delete(job_id: str) -> None:
    with _lock:
        j = _jobs.get(job_id)
        if not j:
            raise KeyError(job_id)
        if j["state"] == "running":
            raise ValueError("Cancel the running job first")
        _jobs.pop(job_id, None)
        _save()


# ── runner ───────────────────────────────────────────────────────────────────
def _kick() -> None:
    """Start the oldest pending job if none is running."""
    with _lock:
        if any(j["state"] == "running" for j in _jobs.values()):
            return
        pend = sorted((j for j in _jobs.values() if j["state"] == "pending"), key=lambda j: j["created"])
        if not pend:
            return
        j = pend[0]
        j["state"] = "running"
        j["started"] = time.time()
        _cancel[j["id"]] = threading.Event()
        _save()
    threading.Thread(target=_worker, args=(j["id"],), daemon=True, name=f"filejob-{j['id']}").start()


class _Progress:
    def __init__(self, job: dict):
        self.j = job
        self.ev = _cancel[job["id"]]
        self._t = time.time()
        self._b = 0
        self._saved = 0.0

    def check(self) -> None:
        if self.ev.is_set():
            raise Cancelled()

    def add(self, nbytes: int = 0, files: int = 0, current: str | None = None) -> None:
        with _lock:
            self.j["bytes_done"] += nbytes
            self.j["files_done"] += files
            if current is not None:
                self.j["current"] = current
            now = time.time()
            if now - self._t >= 1.0:
                self.j["speed"] = (self.j["bytes_done"] - self._b) / (now - self._t)
                self._t, self._b = now, self.j["bytes_done"]
            if now - self._saved >= 2.0:
                self._saved = now
                _save()

    def error(self, msg: str) -> None:
        with _lock:
            if len(self.j["errors"]) < MAX_ERRORS:
                self.j["errors"].append(msg)
            else:
                self.j["errors_more"] = self.j.get("errors_more", 0) + 1

    def skip(self, nbytes: int) -> None:
        with _lock:
            self.j["skipped"] += 1
        self.add(nbytes, 1)


def _worker(job_id: str) -> None:
    j = _jobs[job_id]
    prog = _Progress(j)
    try:
        act = j["action"]
        if act in ("copy", "move"):
            _run_copy_move(j, prog)
        elif act == "delete":
            _run_delete(j, prog)
        elif act in ("chown", "chmod"):
            _run_chown_chmod(j, prog)
        else:
            raise ValueError(f"Unknown action {act!r}")
        state = "done"
    except Cancelled:
        state = "cancelled"
    except Exception as e:                                   # noqa: BLE001 - reported to the user
        state = "failed"
        j["error"] = str(e) or e.__class__.__name__
    with _lock:
        j["state"] = state
        if state == "done" and j["errors"]:
            j["error"] = f"Finished with {len(j['errors']) + j.get('errors_more', 0)} error(s)"
        j["finished"] = time.time()
        j["current"] = ""
        j["speed"] = 0.0
        _cancel.pop(job_id, None)
        _procs.pop(job_id, None)
        _save()
    _kick()


# ── scanning ─────────────────────────────────────────────────────────────────
def _scan(paths: list[str]) -> tuple[int, int]:
    files = size = 0
    for p in paths:
        if os.path.islink(p) or not os.path.isdir(p):
            files += 1
            try:
                size += 0 if os.path.islink(p) else os.lstat(p).st_size
            except OSError:
                pass
            continue
        for dp, _dn, fn in os.walk(p, followlinks=False):
            for n in fn:
                files += 1
                try:
                    fp = os.path.join(dp, n)
                    if not os.path.islink(fp):
                        size += os.lstat(fp).st_size
                except OSError:
                    pass
    return files, size


def _file_size(p: str) -> int:
    try:
        return 0 if os.path.islink(p) else os.lstat(p).st_size
    except OSError:
        return 0


# ── copy / move ──────────────────────────────────────────────────────────────
def _copy_file(src: str, dst: str, prog: _Progress, sparse: bool) -> None:
    try:
        with open(src, "rb") as fi, open(dst, "wb") as fo:
            while True:
                prog.check()
                buf = fi.read(CHUNK)
                if not buf:
                    break
                if sparse and buf.count(0) == len(buf):
                    fo.seek(len(buf), os.SEEK_CUR)
                else:
                    fo.write(buf)
                prog.add(len(buf))
                if _TEST_DELAY:
                    time.sleep(_TEST_DELAY)
            if sparse:
                fo.truncate(fo.tell())
    except BaseException:
        try:
            os.unlink(dst)                                    # never leave a partial file behind
        except OSError:
            pass
        raise
    try:
        shutil.copystat(src, dst)
    except OSError:
        pass                                                  # FAT and some mounts refuse mode/time changes


def _copy_item(src: str, dst: str, prog: _Progress, overwrite: bool, sparse: bool, copied: list) -> None:
    prog.check()
    try:
        if os.path.islink(src):
            if os.path.lexists(dst):
                if not overwrite:
                    prog.skip(0)
                    return
                os.unlink(dst)
            os.symlink(os.readlink(src), dst)
            copied.append(src)
            prog.add(0, 1, src)
            return
        if os.path.isdir(src):
            if os.path.lexists(dst) and not os.path.isdir(dst):
                raise OSError(f"{dst} exists and is not a folder")
            os.makedirs(dst, exist_ok=True)
            for name in sorted(os.listdir(src)):
                _copy_item(os.path.join(src, name), os.path.join(dst, name), prog, overwrite, sparse, copied)
            try:
                shutil.copystat(src, dst)
            except OSError:
                pass
            return
        size = _file_size(src)
        if os.path.lexists(dst):
            if os.path.isdir(dst):
                raise OSError(f"{dst} exists and is a folder")
            if not overwrite:
                prog.skip(size)
                return
        prog.add(0, 0, src)
        _copy_file(src, dst, prog, sparse)
        copied.append(src)
        prog.add(0, 1)
    except Cancelled:
        raise
    except OSError as e:
        prog.error(f"{src}: {e.strerror or e}")
        prog.add(_file_size(src) if not os.path.isdir(src) else 0, 0 if os.path.isdir(src) else 1)


def _prune_dirs(src: str) -> None:
    for dp, _dn, _fn in os.walk(src, topdown=False):
        try:
            os.rmdir(dp)
        except OSError:
            pass


def _run_copy_move(j: dict, prog: _Progress) -> None:
    target = j["target"]
    srcs = j["sources"]
    os.makedirs(target, exist_ok=True)
    files, size = _scan(srcs)
    j["files_total"], j["bytes_total"] = files, size
    moving = j["action"] == "move"
    for src in srcs:
        prog.check()
        dst = os.path.join(target, os.path.basename(src.rstrip("/")))
        if os.path.realpath(src) == os.path.realpath(dst):
            raise ValueError(f"{src} is already in {target}")
        if os.path.isdir(src) and not os.path.islink(src) and \
                (os.path.realpath(dst) + os.sep).startswith(os.path.realpath(src) + os.sep):
            raise ValueError(f"Cannot put {src} inside itself")
        if moving and not os.path.lexists(dst):
            try:
                os.rename(src, dst)                           # same filesystem: instant
                f, b = _scan([dst])
                prog.add(b, f, src)
                continue
            except OSError:
                pass                                          # different filesystem: copy then delete
        copied: list[str] = []
        _copy_item(src, dst, prog, j["overwrite"], j["sparse"], copied)
        if moving:
            for f in copied:
                try:
                    os.unlink(f)
                except OSError as e:
                    prog.error(f"{f}: could not remove after copy ({e.strerror})")
            if os.path.isdir(src) and not os.path.islink(src):
                _prune_dirs(src)


# ── delete ───────────────────────────────────────────────────────────────────
def _run_delete(j: dict, prog: _Progress) -> None:
    files, size = _scan(j["sources"])
    j["files_total"], j["bytes_total"] = files, size
    for src in j["sources"]:
        prog.check()
        if os.path.islink(src) or not os.path.isdir(src):
            try:
                n = _file_size(src)
                os.unlink(src)
                prog.add(n, 1, src)
            except FileNotFoundError:
                pass
            except OSError as e:
                prog.error(f"{src}: {e.strerror or e}")
            continue
        for dp, dn, fn in os.walk(src, topdown=False, followlinks=False):
            for n in fn:
                prog.check()
                fp = os.path.join(dp, n)
                try:
                    sz = _file_size(fp)
                    os.unlink(fp)
                    prog.add(sz, 1, fp)
                except OSError as e:
                    prog.error(f"{fp}: {e.strerror or e}")
            for d in dn:                                      # symlinks to folders show up here
                dp2 = os.path.join(dp, d)
                if os.path.islink(dp2):
                    try:
                        os.unlink(dp2)
                        prog.add(0, 1)
                    except OSError as e:
                        prog.error(f"{dp2}: {e.strerror or e}")
            try:
                os.rmdir(dp)
            except OSError as e:
                prog.error(f"{dp}: {e.strerror or e}")


# ── owner / permissions ──────────────────────────────────────────────────────
def _sudo(cmd: list[str]) -> list[str]:
    return cmd if os.geteuid() == 0 else ["sudo", "-n", *cmd]


def _run_chown_chmod(j: dict, prog: _Progress) -> None:
    p = j["params"]
    rec = ["-R"] if p.get("recursive") else []
    cmds = []
    if j["action"] == "chown":
        cmds.append(["chown", *rec, f"{p['owner']}:{p['group']}", "--", *j["sources"]])
    else:
        cmds.append(["chmod", *rec, p["symbolic"], "--", *j["sources"]])
    j["files_total"] = len(j["sources"])
    for cmd in cmds:
        prog.check()
        proc = subprocess.Popen(_sudo(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        _procs[j["id"]] = proc
        while True:
            try:
                out, err = proc.communicate(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if prog.ev.is_set():
                    proc.terminate()
                    raise Cancelled()
        if proc.returncode != 0:
            for line in (err or out or "failed").strip().splitlines()[:MAX_ERRORS]:
                prog.error(line)
    prog.add(0, len(j["sources"]))
