#!/usr/bin/env python3
"""Run a command on a Helix test machine over SSH, or push dev code to it.

    vmsh.py [--host 192.168.50.12] [--timeout 60] 'sudo -n virsh list --all'
    vmsh.py --host 192.168.50.142 --push-dev   # sync backend/frontend + helpers onto the machine, restart the backend

Logs in as `helix` (passwordless sudo) with ~/.ssh/helix_ed25519 (--key, --user to change). The command's exit
status is this script's exit status.

It used to go through the web terminal and the file-upload API; both need a signed-in session once login is
enforced (3.3.5+), so it uses SSH now.

--push-dev is for development only: it overwrites the code in /opt/helix and /usr/local/sbin in the RAM overlay.
A reboot (or an OTA) puts the shipped image back.
"""
import argparse, os, shlex, subprocess, sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PUSH_PATHS = ["webui/backend", "webui/frontend", "array/sbin", "network", "shares", "checkmk", "addins", "proxy"]

INSTALL = (
    "set -e; T=$(mktemp -d); trap 'sudo -n rm -rf \"$T\"' EXIT; tar -xzf - -C \"$T\"; "
    "sudo -n rm -rf /opt/helix/backend /opt/helix/frontend; "
    "sudo -n cp -r \"$T/webui/backend\" /opt/helix/backend; sudo -n cp -r \"$T/webui/frontend\" /opt/helix/frontend; "
    "sudo -n sh -c \"cp $T/array/sbin/* /usr/local/sbin/; cp $T/network/helix-* /usr/local/sbin/ 2>/dev/null; "
    "cp $T/shares/helix-* /usr/local/sbin/ 2>/dev/null; cp $T/checkmk/helix-monitor /usr/local/sbin/; "
    "cp $T/addins/helix-addin /usr/local/sbin/; cp $T/proxy/helix-* /usr/local/sbin/ 2>/dev/null; "
    "mkdir -p /usr/local/lib/helix; cp $T/checkmk/helix_local_check.py /usr/local/lib/helix/; "
    "chmod 755 /usr/local/lib/helix/*.py /usr/local/sbin/helix-*; "
    "chown -R helix:helix /opt/helix/backend /opt/helix/frontend\"; "
    "echo pushed; sudo -n systemd-run --on-active=2 systemctl restart helix-backend >/dev/null 2>&1"
)


def ssh_cmd(a) -> list[str]:
    return ["ssh", "-i", os.path.expanduser(a.key), "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            f"{a.user}@{a.host}"]


def run(a, cmd: str) -> int:
    try:
        return subprocess.run(ssh_cmd(a) + [cmd], timeout=a.timeout).returncode
    except subprocess.TimeoutExpired:
        print("[vmsh: timed out]", file=sys.stderr)
        return 124


def push_dev(a) -> int:
    tar = subprocess.Popen(["tar", "-czf", "-", "--exclude=__pycache__", "--exclude=.venv", "--exclude=state",
                            "--exclude=*.pyc", "-C", REPO, *PUSH_PATHS], stdout=subprocess.PIPE)
    rc = subprocess.run(ssh_cmd(a) + ["bash -c " + shlex.quote(INSTALL)], stdin=tar.stdout, timeout=300).returncode
    tar.stdout.close()
    return rc or tar.wait()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.50.12")
    ap.add_argument("--user", default="helix")
    ap.add_argument("--key", default="~/.ssh/helix_ed25519")
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--push-dev", action="store_true")
    ap.add_argument("cmd", nargs="?")
    a = ap.parse_args()
    if a.push_dev:
        sys.exit(push_dev(a))
    if not a.cmd:
        ap.error("give a command or --push-dev")
    sys.exit(run(a, a.cmd))
