"""
Network router — config CRUD and live interface status.
Reads/writes network.cfg; calls helix-apply-network to apply.
"""

import json
import os
import re
import subprocess
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from config import HELIX_CONFIG

NETWORK_CFG = os.path.join(HELIX_CONFIG, "network.cfg")
NETWORK_SBIN = os.path.join(
    os.path.dirname(__file__), "../../../../network"
)

router = APIRouter(prefix="/api/network", tags=["network"])


# ── Config parser ─────────────────────────────────────────────────────────────

def _parse_cfg() -> dict:
    defaults = {
        "ifname": "", "use_dhcp": "yes", "ipaddr": "", "netmask": "255.255.255.0",
        "gateway": "", "dhcp_hostname": "", "dns_server1": "", "dns_server2": "",
        "domain_name": "", "mtu": "0", "bonding": "no", "bond_name": "bond0",
        "bond_members": "", "bonding_mode": "1", "bonding_miimon": "100",
        "bridging": "no", "bridge_name": "br0", "vlan_id": "0", "use_dhcp6": "auto",
    }
    try:
        content = Path(NETWORK_CFG).read_text()
    except FileNotFoundError:
        return defaults

    result = dict(defaults)
    in_section = False
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line == "[network]":
            in_section = True
            continue
        if line.startswith("[") and in_section:
            break
        if in_section and "=" in line:
            k, _, v = line.partition("=")
            k = k.strip().lower()
            v = v.strip().strip('"')
            if k in result:
                result[k] = v
    return result


def _write_cfg(fields: dict) -> None:
    """Merge fields into network.cfg, preserving comments."""
    try:
        content = Path(NETWORK_CFG).read_text()
    except FileNotFoundError:
        content = "[network]\n"

    for key, val in fields.items():
        upper = key.upper()
        # Replace existing key
        new_line = f"{upper}={val}"
        pattern = rf"(?m)^({upper}\s*=.*)$"
        if re.search(pattern, content, re.IGNORECASE):
            content = re.sub(pattern, new_line, content, flags=re.IGNORECASE)
        else:
            # Append before end of [network] section
            content = re.sub(r"(\[network\][^\[]*)", rf"\1{new_line}\n", content, count=1)

    Path(NETWORK_CFG).write_text(content)


def _apply(background: bool = False) -> dict:
    script = os.path.join(NETWORK_SBIN, "helix-apply-network")
    if not os.path.exists(script):
        return {"ok": False, "error": f"Script not found: {script}"}
    env = {**os.environ, "HELIX_CONFIG": HELIX_CONFIG}
    r = subprocess.run(["/bin/bash", script], capture_output=True, text=True, env=env, timeout=15)
    return {"ok": r.returncode == 0, "stdout": r.stdout, "stderr": r.stderr}


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/config")
def get_config():
    return _parse_cfg()


class NetworkUpdate(BaseModel):
    use_dhcp:      str | None = None
    ipaddr:        str | None = None
    netmask:       str | None = None
    gateway:       str | None = None
    dhcp_hostname: str | None = None
    dns_server1:   str | None = None
    dns_server2:   str | None = None
    domain_name:   str | None = None
    mtu:           str | None = None
    ifname:        str | None = None
    bonding:       str | None = None
    bond_members:  str | None = None
    bonding_mode:  str | None = None
    bridging:      str | None = None
    bridge_name:   str | None = None
    vlan_id:       str | None = None
    use_dhcp6:     str | None = None
    apply:         bool = True


@router.put("/config")
def update_config(update: NetworkUpdate, background_tasks: BackgroundTasks):
    fields = {k: v for k, v in update.model_dump(exclude={"apply"}).items() if v is not None}
    if not fields:
        raise HTTPException(400, "No fields to update")
    _write_cfg(fields)
    if update.apply:
        background_tasks.add_task(_apply)
    return {"ok": True, "updated": list(fields.keys())}


@router.post("/apply")
def apply_network():
    result = _apply()
    if not result["ok"]:
        raise HTTPException(500, result.get("stderr") or "Apply failed")
    return result


@router.get("/status")
def network_status():
    """Live interface state from ip addr/route."""
    script = os.path.join(NETWORK_SBIN, "helix-apply-network")
    if os.path.exists(script):
        try:
            r = subprocess.run(
                ["/bin/bash", script, "--status"],
                capture_output=True, text=True, timeout=5,
                env={**os.environ, "HELIX_CONFIG": HELIX_CONFIG}
            )
            if r.returncode == 0 and r.stdout.strip():
                return json.loads(r.stdout)
        except Exception:
            pass

    # Fallback: basic interface data via psutil (same as system/network)
    import psutil
    ifaces = {}
    counters = psutil.net_io_counters(pernic=True)
    addrs    = psutil.net_if_addrs()
    stats_   = psutil.net_if_stats()
    for name, cnt in counters.items():
        if name == "lo":
            continue
        ip = next(
            (a.address for a in addrs.get(name, []) if a.family.name == "AF_INET"),
            None
        )
        ifaces[name] = {"ip": ip, "up": stats_[name].isup if name in stats_ else False,
                        "rx_mb": round(cnt.bytes_recv / 1e6, 1),
                        "tx_mb": round(cnt.bytes_sent / 1e6, 1)}
    return {"interfaces": ifaces, "routes": {}}


# ── Avahi / mDNS ─────────────────────────────────────────────────────────────

@router.get("/avahi")
def avahi_status():
    """Return avahi-daemon running state."""
    st = subprocess.run(["systemctl", "is-active", "avahi-daemon"], capture_output=True, text=True)
    active = st.stdout.strip() == "active"
    installed = subprocess.run(["which", "avahi-daemon"], capture_output=True).returncode == 0
    return {"installed": installed, "active": active}


@router.post("/avahi/enable")
def avahi_enable():
    r = subprocess.run(["sudo", "-n", "systemctl", "enable", "--now", "avahi-daemon"],
                       capture_output=True, text=True)
    return {"ok": r.returncode == 0, "output": (r.stdout + r.stderr).strip()}


@router.post("/avahi/disable")
def avahi_disable():
    r = subprocess.run(["sudo", "-n", "systemctl", "disable", "--now", "avahi-daemon"],
                       capture_output=True, text=True)
    return {"ok": r.returncode == 0, "output": (r.stdout + r.stderr).strip()}
