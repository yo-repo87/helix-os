#!/usr/bin/env python3
"""
mk_docker.py — Checkmk agent plugin for Docker container monitoring.
Place in /usr/lib/check_mk_agent/plugins/ and make executable.
Outputs: container state, name, image, status for each container.
"""

import json
import subprocess
import sys


def _run(*args):
    try:
        r = subprocess.run(list(args), capture_output=True, text=True, timeout=10)
        return r.stdout
    except Exception:
        return ""


def main():
    out = _run("docker", "ps", "-a", "--no-trunc", "--format", "{{json .}}")
    if not out.strip():
        return

    print("<<<docker_containers:sep(0)>>>")
    for line in out.strip().splitlines():
        try:
            c = json.loads(line)
            state  = c.get("State", "unknown").lower()
            name   = c.get("Names", "").lstrip("/")
            image  = c.get("Image", "")
            status = c.get("Status", "")
            cid    = c.get("ID", "")[:12]
            print(json.dumps({
                "id":     cid,
                "name":   name,
                "image":  image,
                "state":  state,
                "status": status,
            }))
        except (json.JSONDecodeError, KeyError):
            continue


if __name__ == "__main__":
    main()
