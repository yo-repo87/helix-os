"""Run commands and write files as root from the unprivileged web backend.

The backend runs as the `helix` user, which has passwordless sudo. Routers that
manage accounts, VPNs and the like used to refuse with "requires the backend to
run as root"; they go through here instead.
"""
import os
import subprocess

_IS_ROOT = os.geteuid() == 0


def run(cmd, input: str | None = None, timeout: int = 30) -> subprocess.CompletedProcess:
    full = list(cmd) if _IS_ROOT else ["sudo", "-n", *cmd]
    try:
        return subprocess.run(full, input=input, capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(full, 127, "", str(e))


def read_file(path) -> str | None:
    """Contents of a root-only file, or None if it cannot be read."""
    r = run(["cat", str(path)])
    return r.stdout if r.returncode == 0 else None


def write_file(path, content: str, mode: str = "600", owner: str | None = None) -> None:
    r = run(["tee", str(path)], input=content)
    if r.returncode != 0:
        raise OSError(r.stderr.strip() or f"cannot write {path}")
    run(["chmod", mode, str(path)])
    if owner:
        run(["chown", owner, str(path)])
