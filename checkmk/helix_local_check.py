#!/usr/bin/env python3
"""Checkmk "local check" plugin for Helix OS.

The Checkmk agent runs every executable in its local/ folder and treats each output line as a service:

    <state> "<service name>" <metrics or -> <detail>        (state: 0 OK, 1 WARN, 2 CRIT, 3 UNKNOWN)

Services are named after what they describe ("Container emby", "VM Windows 11", "Disk disk1 SMART"), so when a
container or VM appears or disappears the *set of services* changes; helix-monitor watches for that and has Checkmk
re-run service discovery, which is how new things get monitored without anybody touching Checkmk.
"""
import glob
import json
import os
import re
import subprocess
import sys

HELIX_CONFIG = os.getenv("HELIX_CONFIG", "/boot/config")
STATE_DIR = os.getenv("STATE_DIR", "/var/local/helix")


def run(*cmd, timeout=15):
    try:
        r = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""


def emit(state, name, detail, metrics="-"):
    name = re.sub(r'["\n]', "'", name)
    print(f'{state} "{name}" {metrics} {detail}'.replace("\n", " "))


def ini(path):
    out, sec = {}, None
    try:
        for line in open(path):
            line = line.strip()
            if line.startswith("[") and line.endswith("]"):
                sec = line[1:-1]; out[sec] = {}
            elif sec and "=" in line and not line.startswith("#"):
                k, _, v = line.partition("="); out[sec][k.strip()] = v.strip()
    except OSError:
        pass
    return out


def array():
    try:
        started = open(os.path.join(STATE_DIR, "array_state")).readline().strip() == "started"
    except OSError:
        started = False
    disks = ini(os.path.join(HELIX_CONFIG, "disks.cfg"))
    assigned = [s for s, v in disks.items() if v.get("device")]
    if not assigned:
        emit(0, "Array", "no disks assigned yet"); return started, disks
    missing = [s for s in assigned if not os.path.exists(os.path.realpath(disks[s]["device"]))]
    unmounted = [s for s in assigned if started and not s.startswith("parity") and not os.path.ismount(f"/mnt/{s}")]
    if missing:
        emit(2, "Array", f"disk missing: {', '.join(missing)}")
    elif unmounted:
        emit(2, "Array", f"started but not mounted (needs Format?): {', '.join(unmounted)}")
    elif started:
        emit(0, "Array", f"started, {len(assigned)} disk(s)")
    else:
        emit(1, "Array", "stopped")
    return started, disks


def smart(disks):
    for sec, v in sorted(disks.items()):
        dev = v.get("device")
        if not dev:
            continue
        real = os.path.realpath(dev)
        if not os.path.exists(real):
            continue
        rc, out = run("smartctl", "-n", "standby", "-H", "-A", real)
        if "STANDBY" in out.upper() and "Device is in" in out:
            emit(0, f"Disk {sec} SMART", "spun down (not woken to read SMART)"); continue
        if not out.strip():
            emit(3, f"Disk {sec} SMART", "smartctl gave no output"); continue
        temp = None
        m = re.search(r"Temperature_Celsius.*?(\d+)(?:\s*\(.*\))?\s*$", out, re.M) or re.search(r"^Temperature:\s+(\d+)", out, re.M) \
            or re.search(r"Current Drive Temperature:\s+(\d+)", out, re.M)
        if m and int(m.group(1)) > 0:          # virtual disks report 0
            temp = int(m.group(1))
        bad = re.search(r"overall-health.*FAILED|Health Status:\s*(?!OK)\S|FAILING_NOW", out)
        realloc = re.search(r"(Reallocated_Sector_Ct|Current_Pending_Sector)\s.*?(\d+)\s*$", out, re.M)
        state, note = 0, "PASSED"
        if bad:
            state, note = 2, "SMART health FAILED"
        elif temp is not None and temp >= 60:
            state, note = 2, f"{temp} °C is too hot"
        elif temp is not None and temp >= 50:
            state, note = 1, f"{temp} °C is warm"
        elif realloc and int(realloc.group(2)) > 0:
            state, note = 1, f"{realloc.group(1)} = {realloc.group(2)}"
        metrics = f"temp={temp};50;60" if temp is not None else "-"
        emit(state, f"Disk {sec} SMART", f"{note}" + (f", {temp} °C" if temp is not None else ""), metrics)


def docker():
    rc, out = run("docker", "ps", "-a", "--format", "{{json .}}")
    if rc != 0:
        return
    for line in out.splitlines():
        try:
            c = json.loads(line)
        except ValueError:
            continue
        name, status, state = c.get("Names", ""), c.get("Status", ""), c.get("State", "")
        if name == "checkmk":
            continue
        if state == "running":
            emit(1 if "unhealthy" in status else 0, f"Container {name}", status + (" · " + c.get("Image", "")))
        elif state in ("restarting", "paused"):
            emit(1, f"Container {name}", f"{state}: {status}")
        else:
            m = re.search(r"Exited \((\d+)\)", status)
            emit(2 if m and m.group(1) not in ("0", "137", "143") else 1, f"Container {name}", f"stopped: {status}")


def vms():
    if not os.path.exists("/usr/bin/virsh"):
        return
    rc, out = run("virsh", "-c", "qemu:///system", "list", "--all", "--name")
    if rc != 0:
        return
    for name in [x.strip() for x in out.splitlines() if x.strip()]:
        _, info = run("virsh", "-c", "qemu:///system", "dominfo", name)
        d = dict((k.strip().lower(), v.strip()) for k, _, v in (l.partition(":") for l in info.splitlines()) if v)
        st, auto = d.get("state", "unknown"), d.get("autostart") == "enable"
        if st == "running":
            emit(0, f"VM {name}", "running")
        elif st in ("paused", "pmsuspended", "in shutdown"):
            emit(1, f"VM {name}", st)
        elif st == "crashed":
            emit(2, f"VM {name}", "crashed")
        else:
            emit(1 if auto else 0, f"VM {name}", "stopped" + (" but set to start with the array" if auto else ""))


def shares():
    cfg = ini(os.path.join(HELIX_CONFIG, "shares.cfg"))
    want_smb = any(v.get("smb") == "yes" for k, v in cfg.items() if k != "sharedefaults")
    want_nfs = any(v.get("nfs") == "yes" for k, v in cfg.items() if k != "sharedefaults")
    for want, unit, label in ((want_smb, "smbd", "SMB"), (want_nfs, "nfs-server", "NFS")):
        if want:
            rc, _ = run("systemctl", "is-active", "--quiet", unit)
            emit(0 if rc == 0 else 2, f"Share service {label}", "running" if rc == 0 else f"{unit} is not running although shares use it")


def main():
    started, disks = array()
    smart(disks)
    shares()
    docker()
    vms()


if __name__ == "__main__":
    main()
