"""
System diagnostics.

GET  /api/diagnostics/syslog?lines=300    — recent journal / syslog entries
GET  /api/diagnostics/processes?n=25     — top processes by CPU
GET  /api/diagnostics/dmesg?lines=200    — kernel ring buffer
GET  /api/diagnostics/bundle             — download diagnostics ZIP
"""

import io
import json
import os
import subprocess
import zipfile
from datetime import datetime

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])


def _run(*args, timeout: int = 15) -> tuple[int, str, str]:
    try:
        r = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


# ── Syslog ────────────────────────────────────────────────────────────────────

def _get_syslog(lines: int) -> list[str]:
    # Prefer journalctl (systemd)
    rc, out, _ = _run("journalctl", "-n", str(lines), "--no-pager",
                      "-o", "short-iso", "--no-hostname")
    if rc == 0 and out.strip():
        return out.strip().splitlines()

    # Fallback: /var/log/syslog
    for path in ("/var/log/syslog", "/var/log/messages"):
        try:
            with open(path) as f:
                all_lines = f.readlines()
            return [l.rstrip() for l in all_lines[-lines:]]
        except (OSError, PermissionError):
            continue

    return ["[syslog not available]"]


@router.get("/syslog")
def get_syslog(lines: int = 300, unit: str = ""):
    if unit:
        rc, out, _ = _run("journalctl", "-n", str(lines), "--no-pager",
                          "-o", "short-iso", "-u", unit)
        entries = out.strip().splitlines() if rc == 0 else []
    else:
        entries = _get_syslog(lines)
    return {"lines": entries, "count": len(entries)}


@router.get("/dmesg")
def get_dmesg(lines: int = 200):
    rc, out, _ = _run("dmesg", "--human", "--color=never", "-T")
    if rc != 0:
        rc, out, _ = _run("dmesg")
    entries = out.strip().splitlines()[-lines:]
    return {"lines": entries, "count": len(entries)}


# ── Processes ─────────────────────────────────────────────────────────────────

@router.get("/processes")
def get_processes(n: int = 25):
    rc, out, _ = _run("ps", "aux", "--sort=-pcpu",
                      "--no-headers", "--cols=256")
    if rc != 0:
        return {"processes": []}

    procs = []
    for line in out.strip().splitlines()[:n]:
        parts = line.split(None, 10)
        if len(parts) < 11:
            continue
        procs.append({
            "user":    parts[0],
            "pid":     int(parts[1]),
            "cpu_pct": float(parts[2]),
            "mem_pct": float(parts[3]),
            "vsz_kb":  int(parts[4]),
            "rss_kb":  int(parts[5]),
            "stat":    parts[7],
            "cmd":     parts[10].strip(),
        })
    return {"processes": procs}


# ── Diagnostics bundle ────────────────────────────────────────────────────────

def _collect_text(*cmd, label: str, timeout: int = 10) -> str:
    rc, out, err = _run(*cmd, timeout=timeout)
    if rc != 0 or not out.strip():
        return f"[{label}: not available — {err.strip()[:200]}]\n"
    return out


@router.get("/bundle")
def download_bundle():
    buf = io.BytesIO()
    ts  = datetime.now().strftime("%Y%m%d-%H%M%S")

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:

        zf.writestr("uname.txt",
            _collect_text("uname", "-a", label="uname"))

        zf.writestr("os-release.txt",
            _collect_text("cat", "/etc/os-release", label="os-release"))

        zf.writestr("dmesg.txt",
            _collect_text("dmesg", "--human", "--color=never", label="dmesg", timeout=10))

        zf.writestr("syslog.txt",
            "\n".join(_get_syslog(500)))

        zf.writestr("ps.txt",
            _collect_text("ps", "aux", "--sort=-pcpu", label="ps"))

        zf.writestr("df.txt",
            _collect_text("df", "-h", label="df"))

        zf.writestr("free.txt",
            _collect_text("free", "-h", label="free"))

        zf.writestr("ip-addr.txt",
            _collect_text("ip", "addr", label="ip addr"))

        zf.writestr("ip-route.txt",
            _collect_text("ip", "route", label="ip route"))

        zf.writestr("lsblk.txt",
            _collect_text("lsblk", "-o", "NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL",
                          label="lsblk"))

        zf.writestr("docker-ps.txt",
            _collect_text("docker", "ps", "-a", "--no-trunc", label="docker ps"))

        zf.writestr("systemctl-failed.txt",
            _collect_text("systemctl", "--failed", "--no-pager", label="systemctl failed"))

        # SMART per drive
        rc, blk, _ = _run("lsblk", "-dno", "NAME,TYPE")
        for line in blk.strip().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "disk":
                dev = f"/dev/{parts[0]}"
                smart = _collect_text("smartctl", "-a", dev, label=f"smart:{dev}", timeout=15)
                zf.writestr(f"smart/{parts[0]}.txt", smart)

        # GPU info
        rc, nvout, _ = _run("nvidia-smi", label="nvidia-smi")
        zf.writestr("gpu/nvidia-smi.txt",
            nvout if rc == 0 else "[nvidia-smi not available]\n")

        # Summary metadata
        summary = {
            "generated": ts,
            "hostname":  _collect_text("hostname", label="hostname").strip(),
            "kernel":    _collect_text("uname", "-r", label="uname -r").strip(),
        }
        zf.writestr("summary.json", json.dumps(summary, indent=2))

    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=helix-diagnostics-{ts}.zip"},
    )
