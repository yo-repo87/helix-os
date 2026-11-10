#!/usr/bin/env python3
"""Run a command on a Helix test machine through its web terminal (no SSH needed).

    vmsh.py [--host 192.168.50.12] [--timeout 60] 'sudo -n virsh list --all'
    vmsh.py --push-dev            # sync webui/backend, webui/frontend and array/sbin onto the machine and restart the backend

The terminal is a PTY running as the unprivileged `helix` user (passwordless sudo). Output is returned with the
terminal noise stripped and the command's exit status is the exit status of this script.

--push-dev is for development only: it overwrites the code in /opt/helix and /usr/local/sbin in the RAM overlay.
A reboot (or an OTA) puts the shipped image back.
"""
import argparse, asyncio, base64, json, os, re, subprocess, sys, tempfile, time, urllib.request, uuid

import websockets

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[()][A-Z0-9]|\r")


async def _run(host: str, cmd: str, timeout: int) -> tuple[int, str]:
    tok = uuid.uuid4().hex[:12]
    async with websockets.connect(f"ws://{host}/api/terminal/ws", open_timeout=15, max_size=None) as ws:
        await ws.send(json.dumps({"type": "resize", "cols": 250, "rows": 50}))
        await asyncio.sleep(0.8)
        # the formatted marker never appears in the echo of the command line, only in its output
        # one group with stdin closed: a command that reads the terminal must not swallow the marker line
        await ws.send(f"stty -echo 2>/dev/null; {{ {cmd}\n}} </dev/null; __rc=$?; printf '\\n@@%s@@%s@@\\n' {tok} $__rc; stty echo 2>/dev/null\n")
        buf, t0 = "", time.time()
        pat = re.compile(r"@@" + tok + r"@@(\d+)@@")
        while time.time() - t0 < timeout:
            try:
                m = await asyncio.wait_for(ws.recv(), 1.0)
            except asyncio.TimeoutError:
                continue
            buf += m.decode(errors="replace") if isinstance(m, bytes) else m
            hit = pat.search(ANSI.sub("", buf))
            if hit:
                text = ANSI.sub("", buf)
                body = text[:hit.start()]
                body = re.sub(r"^.*?stty -echo[^\n]*\n", "", body, count=1, flags=re.S) if "stty -echo" in body else body
                return int(hit.group(1)), body.strip("\n")
        return 124, ANSI.sub("", buf) + "\n[vmsh: timed out]"


def run(host: str, cmd: str, timeout: int = 60) -> tuple[int, str]:
    return asyncio.run(_run(host, cmd, timeout))


def upload(host: str, local: str, dest_dir: str, name: str) -> None:
    CH = 4 * 1024 * 1024
    size = os.path.getsize(local)
    with open(local, "rb") as f:
        off = 0
        while True:
            chunk = f.read(CH)
            last = off + len(chunk) >= size
            url = (f"http://{host}/api/files/upload/chunk?path={urllib.request.quote(dest_dir, safe='')}"
                   f"&name={urllib.request.quote(name)}&offset={off}&last={'true' if last else 'false'}&overwrite=true")
            req = urllib.request.Request(url, data=chunk, method="POST", headers={"Content-Type": "application/octet-stream"})
            urllib.request.urlopen(req, timeout=120).read()
            off += len(chunk)
            if last:
                break


def push_dev(host: str) -> int:
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    with tempfile.TemporaryDirectory() as td:
        tgz = os.path.join(td, "devsync.tgz")
        subprocess.run(["tar", "-czf", tgz, "--exclude=__pycache__", "--exclude=.venv", "--exclude=state", "--exclude=*.pyc",
                        "-C", repo, "webui/backend", "webui/frontend", "array/sbin", "network", "shares"], check=True)
        upload(host, tgz, "/mnt/user/appdata", "devsync.tgz")
    rc, out = run(host, (
        "set -e; T=$(mktemp -d); sudo -n tar -xzf /mnt/user/appdata/devsync.tgz -C $T; "
        "sudo -n rm -rf /opt/helix/backend /opt/helix/frontend; "
        "sudo -n cp -r $T/webui/backend /opt/helix/backend; sudo -n cp -r $T/webui/frontend /opt/helix/frontend; "
        "sudo -n sh -c \"cp $T/array/sbin/* /usr/local/sbin/ && cp $T/network/helix-* /usr/local/sbin/ 2>/dev/null; "
        "cp $T/shares/helix-* /usr/local/sbin/ 2>/dev/null; chmod 755 /usr/local/sbin/helix-*; "
        "chown -R helix:helix /opt/helix/backend /opt/helix/frontend\"; "
        "sudo -n rm -rf $T /mnt/user/appdata/devsync.tgz; echo pushed; sudo -n systemd-run --on-active=2 systemctl restart helix-backend >/dev/null 2>&1"), 120)
    print(out)
    return rc


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.50.12")
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--push-dev", action="store_true")
    ap.add_argument("cmd", nargs="?")
    a = ap.parse_args()
    if a.push_dev:
        sys.exit(push_dev(a.host))
    if not a.cmd:
        ap.error("give a command or --push-dev")
    rc, out = run(a.host, a.cmd, a.timeout)
    print(out)
    sys.exit(rc)
