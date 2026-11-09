"""
Helix OS image updates from GitHub Releases.

Distinct from routers/updates.py (which runs apt package upgrades). This updates
the OS *image itself* by downloading a newer boot-image set published to a GitHub
release and swapping it on the boot partition, then rebooting. Because the OS runs
from a read-only squashfs in RAM, settings live on /boot/config, and apps/data live
on the array/pools, an update never touches user data — it is a boot-image swap.

A release is consumed via a `helix-release.json` manifest asset:

    {
      "version": "0.1.1",
      "channel": "stable",
      "notes":   "…",
      "arch": {
        "amd64": {
          "filesystem.squashfs": {"asset": "amd64-filesystem.squashfs", "sha256": "…", "size": 123},
          "vmlinuz":             {"asset": "amd64-vmlinuz",             "sha256": "…", "size": 123},
          "initrd.img":          {"asset": "amd64-initrd.img",          "sha256": "…", "size": 123}
        },
        "arm64": { … }
      }
    }

Endpoints:
  GET  /api/osupdate/current    current version + boot-device info
  GET  /api/osupdate/settings   repo / channel / token-configured flag
  POST /api/osupdate/settings   update repo / channel / token
  GET  /api/osupdate/check      query GitHub, compare, report availability
  POST /api/osupdate/apply      download + verify + stage + commit (background)
  GET  /api/osupdate/stream     SSE of apply progress
  GET  /api/osupdate/status     last check/apply state + running flag
  POST /api/osupdate/rollback   restore the previously installed image
  POST /api/osupdate/reboot     reboot to finish applying an update
"""

import hashlib
import json
import os
import platform
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse
from pathlib import Path

from fastapi import APIRouter, Body
from fastapi.responses import StreamingResponse

from config import HELIX_CONFIG, STATE_DIR

router = APIRouter(prefix="/api/osupdate", tags=["osupdate"])

SETTINGS_FILE = os.path.join(HELIX_CONFIG, "osupdate.cfg")      # persists on /boot
STATE_FILE    = os.path.join(STATE_DIR, "osupdate_state.json")
DL_DIR        = os.path.join(STATE_DIR, "osupdate", "dl")
HELPER        = os.getenv("HELIX_OSUPDATE_HELPER", "/usr/local/sbin/helix-osupdate")
GH_API        = "https://api.github.com"

OS_FILES = ["filesystem.squashfs", "vmlinuz", "initrd.img"]

DEFAULTS = {"repo": "yo-repo87/helix-os", "channel": "stable", "token": "", "auto_check": True}

_apply_thread: threading.Thread | None = None
_apply_output: list[str] = []
_apply_done = True


# ── settings / state ──────────────────────────────────────────────

def _load_settings() -> dict:
    try:
        return {**DEFAULTS, **json.loads(Path(SETTINGS_FILE).read_text())}
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(DEFAULTS)


def _save_settings(s: dict):
    Path(SETTINGS_FILE).parent.mkdir(parents=True, exist_ok=True)
    cur = _load_settings()
    cur.update({k: v for k, v in s.items() if k in DEFAULTS})
    Path(SETTINGS_FILE).write_text(json.dumps(cur))
    try:
        os.chmod(SETTINGS_FILE, 0o600)   # token lives here
    except OSError:
        pass
    return cur


def _load_state() -> dict:
    try:
        return json.loads(Path(STATE_FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"last_check": None, "available": None, "last_apply": None, "last_status": "unknown"}


def _save_state(s: dict):
    Path(STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(STATE_FILE).write_text(json.dumps(s))


# ── version helpers ───────────────────────────────────────────────

def current_version() -> str:
    for path, key in (("/etc/helix-version", None), ("/etc/os-release", "VERSION_ID")):
        try:
            txt = Path(path).read_text()
        except OSError:
            continue
        if key is None:
            v = txt.strip()
            if v:
                return v
        else:
            m = re.search(rf'^{key}="?([^"\n]+)"?', txt, re.M)
            if m:
                return m.group(1)
    return "0.0.0"


def _ver_tuple(v: str) -> tuple:
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums) if nums else (0,)


def _is_newer(candidate: str, base: str) -> bool:
    return _ver_tuple(candidate) > _ver_tuple(base)


def arch_key() -> str:
    m = platform.machine().lower()
    return {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(m, m)


def _is_live_boot() -> bool:
    """Return True only when running from a Helix OS live-boot squashfs.

    On a live-boot system the root filesystem is an overlayfs whose lower layer
    is a squashfs loop device.  On a plain Debian install it is ext4/btrfs/etc.
    We detect this by scanning /proc/mounts for 'overlay' on '/' backed by a
    squashfs loop, OR by the presence of /run/hive (the initramfs working dir).
    """
    try:
        if os.path.isdir("/run/helix"):
            return True
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == "/" and parts[2] == "overlay":
                    return True
    except OSError:
        pass
    return False


# ── GitHub access (stdlib urllib; strips auth on cross-host redirect) ──

class _AuthDropRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects but drop Authorization when the host changes.

    GitHub release-asset downloads 302 to a pre-signed S3 URL that rejects the
    GitHub Authorization header; it must not be forwarded to the new host.
    """
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            try:
                old_host = urlparse(req.full_url).hostname
                new_host = urlparse(newurl).hostname
            except Exception:
                old_host = new_host = None
            if old_host != new_host:
                for store in (getattr(new, "headers", {}), getattr(new, "unredirected_hdrs", {})):
                    for k in [h for h in store if h.lower() == "authorization"]:
                        store.pop(k, None)
        return new


def _opener():
    return urllib.request.build_opener(_AuthDropRedirect())


def _headers(token: str, accept: str) -> dict:
    h = {"Accept": accept, "User-Agent": "helix-os-updater", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _api_get_json(url: str, token: str):
    req = urllib.request.Request(url, headers=_headers(token, "application/vnd.github+json"))
    with _opener().open(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _pick_release(repo: str, channel: str, token: str) -> dict:
    """Latest non-draft release; include pre-releases only for channel=beta."""
    rels = _api_get_json(f"{GH_API}/repos/{repo}/releases?per_page=20", token)
    # Only real update releases: tagged vX.Y.Z AND carrying the manifest. Other
    # releases in the repo (e.g. 'images-2026.11.06', the installer images) parse as
    # huge version numbers and must never be offered as an update.
    cands = [r for r in rels
             if not r.get("draft")
             and re.match(r"^[vV]?\d+(\.\d+)+$", r.get("tag_name") or "")
             and any(a.get("name") == "helix-release.json" for a in r.get("assets", []))]
    if channel != "beta":
        cands = [r for r in cands if not r.get("prerelease")]
    if not cands:
        raise RuntimeError("no suitable release found")
    # Order by version, not by date: releases tagged from the same commit share
    # one published_at, and a timestamp tie made the updater pick the wrong one.
    cands.sort(key=lambda r: (_ver_tuple((r.get("tag_name") or "").lstrip("vV")),
                              r.get("published_at") or ""), reverse=True)
    return cands[0]


def _asset_map(release: dict) -> dict:
    return {a["name"]: a["url"] for a in release.get("assets", [])}


def _download_asset(asset_api_url: str, dest: str, token: str, expected_sha: str, log):
    req = urllib.request.Request(asset_api_url, headers=_headers(token, "application/octet-stream"))
    h = hashlib.sha256()
    tmp = dest + ".part"
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    with _opener().open(req, timeout=60) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        last = 0.0
        while True:
            chunk = r.read(1024 * 256)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            got += len(chunk)
            now = time.time()
            if now - last > 0.5:
                pct = f"{got * 100 // total}%" if total else f"{got // 1024}K"
                log(f"  {os.path.basename(dest)}: {pct}")
                last = now
    digest = h.hexdigest()
    if expected_sha and digest != expected_sha:
        os.remove(tmp)
        raise RuntimeError(f"{os.path.basename(dest)}: sha256 mismatch "
                           f"(got {digest[:12]}…, expected {expected_sha[:12]}…)")
    os.replace(tmp, dest)
    return digest


# ── manifest ──────────────────────────────────────────────────────

def _fetch_manifest(repo: str, channel: str, token: str):
    release = _pick_release(repo, channel, token)
    assets = _asset_map(release)
    if "helix-release.json" not in assets:
        raise RuntimeError("release has no helix-release.json manifest")
    req = urllib.request.Request(assets["helix-release.json"],
                                 headers=_headers(token, "application/octet-stream"))
    with _opener().open(req, timeout=30) as r:
        manifest = json.loads(r.read().decode())
    return release, assets, manifest


def _helper(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["sudo", "-n", HELPER, *args],
                          capture_output=True, text=True, timeout=120)


# ── endpoints ─────────────────────────────────────────────────────

@router.get("/current")
def get_current():
    info = {}
    try:
        r = _helper("info")
        if r.returncode == 0:
            info = json.loads(r.stdout.strip() or "{}")
    except Exception as e:
        info = {"error": str(e)}
    return {"version": current_version(), "arch": arch_key(), "boot": info}


@router.get("/settings")
def get_settings():
    s = _load_settings()
    return {"repo": s["repo"], "channel": s["channel"],
            "token_configured": bool(s.get("token")), "auto_check": s.get("auto_check", True)}


@router.post("/settings")
def set_settings(body: dict = Body(...)):
    patch = {}
    for k in ("repo", "channel", "auto_check"):
        if k in body:
            patch[k] = body[k]
    # Only overwrite token when a non-empty value is supplied; "" leaves it as-is.
    if body.get("token"):
        patch["token"] = body["token"].strip()
    if body.get("clear_token"):
        patch["token"] = ""
    s = _save_settings(patch)
    return {"ok": True, "repo": s["repo"], "channel": s["channel"],
            "token_configured": bool(s.get("token"))}


@router.get("/check")
def check():
    s = _load_settings()
    cur = current_version()
    ak = arch_key()
    try:
        release, assets, manifest = _fetch_manifest(s["repo"], s["channel"], s.get("token", ""))
    except urllib.error.HTTPError as e:
        hint = " (private repo — set an update token in settings)" if e.code in (401, 404) else ""
        return {"ok": False, "error": f"GitHub HTTP {e.code}{hint}", "current": cur}
    except Exception as e:
        return {"ok": False, "error": str(e), "current": cur}

    latest = manifest.get("version", "")
    arch_files = (manifest.get("arch") or {}).get(ak)
    available = bool(latest) and _is_newer(latest, cur)
    result = {
        "ok": True,
        "current": cur,
        "latest": latest,
        "update_available": available,
        "arch": ak,
        "arch_supported": bool(arch_files),
        "channel": s["channel"],
        "notes": manifest.get("notes", ""),
        "published_at": release.get("published_at"),
        "repo": s["repo"],
    }
    st = _load_state()
    st["last_check"] = int(time.time())
    st["available"] = result if available else None
    _save_state(st)
    return result


def _apply_worker(settings: dict):
    global _apply_done
    _apply_done = False
    log = lambda m: _apply_output.append(m)
    status = "failed"
    try:
        cur = current_version()
        ak = arch_key()
        log(f"Current version {cur} ({ak}). Contacting GitHub…")
        release, assets, manifest = _fetch_manifest(settings["repo"], settings["channel"],
                                                     settings.get("token", ""))
        latest = manifest.get("version", "")
        if not _is_newer(latest, cur):
            log(f"Already up to date (latest {latest}).")
            status = "up-to-date"
            return
        arch_files = (manifest.get("arch") or {}).get(ak)
        if not arch_files:
            log(f"No {ak} image in release {latest}.")
            status = "no-arch"
            return
        live = _is_live_boot()
        if live:
            log(f"Update available: {cur} -> {latest}. Downloading {ak} image…")
        else:
            log(f"Update available: {cur} -> {latest}. "
                f"(Standard install detected — will update web UI via git, skip boot-image swap.)")

        if live:
            dldir = os.path.join(DL_DIR, latest)
            Path(dldir).mkdir(parents=True, exist_ok=True)
            for fname in OS_FILES:
                spec = arch_files.get(fname) or {}
                asset_name = spec.get("asset") or f"{ak}-{fname}"
                if asset_name not in assets:
                    raise RuntimeError(f"release missing asset {asset_name}")
                _download_asset(assets[asset_name], os.path.join(dldir, fname),
                                settings.get("token", ""), spec.get("sha256", ""), log)
                log(f"  {fname}: verified")

            log("Staging new image on boot device…")
            r = _helper("stage", dldir)
            if r.returncode != 0:
                raise RuntimeError(f"stage failed: {r.stderr.strip() or r.stdout.strip()}")
            log("Committing (previous image kept for rollback)…")
            r = _helper("commit", latest)
            if r.returncode != 0:
                _helper("clean")
                raise RuntimeError(f"commit failed: {r.stderr.strip() or r.stdout.strip()}")

            # Free the downloaded copies now that they are on the boot device.
            for fname in OS_FILES:
                try:
                    os.remove(os.path.join(dldir, fname))
                except OSError:
                    pass
            log(f"Done. Version {latest} installed. Reboot to apply.")
            status = "staged"
        else:
            # Standard Debian install: pull latest code and restart the service.
            import shutil
            project_dir = os.path.abspath(
                os.path.join(os.path.dirname(__file__), "..", "..", "..")
            )
            git = shutil.which("git")
            if not git:
                raise RuntimeError("git not found — cannot pull update on standard install")
            log(f"Pulling latest code in {project_dir}…")
            r = subprocess.run([git, "pull"], capture_output=True, text=True,
                               cwd=project_dir, timeout=60)
            log(r.stdout.strip() or "(no output)")
            if r.returncode != 0:
                raise RuntimeError(f"git pull failed: {r.stderr.strip()}")
            # Update /etc/helix-version so the UI reflects the new version.
            try:
                Path("/etc/helix-version").write_text(latest + "\n")
            except OSError:
                pass
            log(f"Done. Version {latest} active. Restarting service…")
            subprocess.run(["sudo", "-n", "systemctl", "restart", "helix-webui"],
                           timeout=15)
            status = "staged"
    except Exception as e:
        log(f"ERROR: {e}")
        status = f"failed: {e}"
    finally:
        st = _load_state()
        st["last_apply"] = int(time.time())
        st["last_status"] = status
        _save_state(st)
        _apply_done = True


@router.post("/apply")
def apply():
    global _apply_thread, _apply_output
    if _apply_thread and _apply_thread.is_alive():
        return {"ok": False, "message": "An update is already in progress"}
    _apply_output = []
    settings = _load_settings()
    _apply_thread = threading.Thread(target=_apply_worker, args=(settings,), daemon=True)
    _apply_thread.start()
    return {"ok": True, "message": "Update started — stream /api/osupdate/stream"}


@router.get("/stream")
async def stream():
    import asyncio

    async def _gen():
        seen = 0
        while True:
            lines = _apply_output[seen:]
            for line in lines:
                yield f"data: {line}\n\n"
            seen += len(lines)
            if _apply_done and seen >= len(_apply_output):
                yield "data: DONE\n\n"
                break
            await asyncio.sleep(0.4)
    return StreamingResponse(_gen(), media_type="text/event-stream")


@router.get("/status")
def status():
    running = _apply_thread is not None and _apply_thread.is_alive()
    return {**_load_state(), "running": running, "recent_output": _apply_output[-40:]}


@router.post("/rollback")
def rollback():
    r = _helper("rollback")
    ok = r.returncode == 0
    return {"ok": ok, "message": (r.stdout or r.stderr).strip(),
            "reboot_required": ok}


@router.post("/reboot")
def reboot():
    r = _helper("reboot")
    return {"ok": r.returncode == 0, "message": (r.stdout or r.stderr).strip()}
