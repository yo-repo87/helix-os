#!/usr/bin/env python3
"""
cmk-docker-bridge — Watches docker events and auto-registers/deregisters
containers as Checkmk hosts.

Runs as a systemd service. Reads automation secret from /etc/helix/checkmk_secret.
"""

import json
import logging
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [cmk-bridge] %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

CMK_URL     = os.getenv("CMK_URL", "http://localhost:5050/cmk/check_mk/api/1.0")
SECRET_FILE = os.getenv("CMK_SECRET_FILE", "/home/pi/.config/helix/checkmk_secret")
USER_FILE   = os.getenv("CMK_USER_FILE",   "/home/pi/.config/helix/checkmk_user")
RETRY_DELAY = 10   # seconds between retries on connection error


def _credentials() -> tuple[str, str]:
    try:
        user = open(USER_FILE).read().strip()
    except FileNotFoundError:
        user = "automation"
    try:
        secret = open(SECRET_FILE).read().strip()
    except FileNotFoundError:
        secret = os.getenv("CMK_SECRET", "")
    return user, secret


def _api(method: str, path: str, data: dict | None = None) -> tuple[int, dict]:
    user, secret = _credentials()
    import base64
    creds  = base64.b64encode(f"{user}:{secret}".encode()).decode()
    url    = f"{CMK_URL}{path}"
    body   = json.dumps(data).encode() if data else None
    req    = urllib.request.Request(
        url, data=body, method=method.upper(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Basic {creds}",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read()
            return resp.status, json.loads(content) if content else {}
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception as e:
        log.warning("API call failed: %s", e)
        return 0, {}


def _activate():
    _api("POST", "/domain-types/activation_run/collections/all",
         {"sites": ["cmk"], "force_foreign_changes": True})


def register(name: str):
    log.info("Registering container: %s", name)
    status, _ = _api("POST", "/domain-types/host_config/collections/all",
                     {"host_name": name, "folder": "/"})
    if status not in (200, 201, 400):  # 400 = already exists
        log.warning("Register returned HTTP %s for %s", status, name)
        return
    _api("POST", "/domain-types/service_discovery_run/collections/all",
         {"host_name": name, "mode": "fix_all"})
    _activate()
    log.info("Registered: %s", name)


def deregister(name: str):
    log.info("Deregistering container: %s", name)
    status, _ = _api("DELETE", f"/objects/host_config/{name}")
    if status not in (204, 404):
        log.warning("Deregister returned HTTP %s for %s", status, name)
        return
    _activate()
    log.info("Deregistered: %s", name)


def watch():
    log.info("Starting docker event watcher (CMK_URL=%s)", CMK_URL)
    while True:
        try:
            proc = subprocess.Popen(
                ["docker", "events",
                 "--filter", "type=container",
                 "--filter", "event=start",
                 "--filter", "event=destroy",
                 "--format", "{{json .}}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            log.info("Connected to docker events stream")
            for line in proc.stdout:
                try:
                    ev   = json.loads(line.decode())
                    name = ev.get("Actor", {}).get("Attributes", {}).get("name", "")
                    if not name:
                        continue
                    action = ev.get("Action", "")
                    if action == "start":
                        register(name)
                    elif action == "destroy":
                        deregister(name)
                except (json.JSONDecodeError, KeyError):
                    continue
            proc.wait()
            log.warning("docker events stream ended (rc=%s) — reconnecting in %ss",
                        proc.returncode, RETRY_DELAY)
        except FileNotFoundError:
            log.error("docker not found — retrying in %ss", RETRY_DELAY)
        except Exception as e:
            log.error("Unexpected error: %s — retrying in %ss", e, RETRY_DELAY)
        time.sleep(RETRY_DELAY)


if __name__ == "__main__":
    watch()
