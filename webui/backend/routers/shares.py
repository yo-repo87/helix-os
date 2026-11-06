from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
import os, re, subprocess
from config import SHARES_CFG, HELIX_CONFIG, USER_SHARE

SHARES_SBIN = os.path.join(os.path.dirname(__file__), "../../../../shares")

def _apply_shares():
    """Regenerate smb.conf + /etc/exports after any config change."""
    script = os.path.join(SHARES_SBIN, "helix-shares-apply")
    if os.path.exists(script):
        subprocess.run(
            ["/bin/bash", script],
            env={**os.environ, "HELIX_CONFIG": HELIX_CONFIG, "USER_SHARE": USER_SHARE},
            capture_output=True
        )

router = APIRouter(prefix="/api/shares", tags=["shares"])

def _parse_shares():
    shares = {}
    try:
        content = open(SHARES_CFG).read()
    except FileNotFoundError:
        return shares
    section = None
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r'^\[(\w+)\]$', line)
        if m:
            section = m.group(1)
            shares[section] = {}
            continue
        if section and "=" in line:
            k, _, v = line.partition("=")
            shares[section][k.strip()] = v.strip()
    return shares

@router.get("/")
def list_shares():
    shares = _parse_shares()
    result = []
    for name, cfg in shares.items():
        share_path = os.path.join(USER_SHARE, name)
        info = {"name": name, **cfg}
        try:
            st = os.statvfs(share_path)
            total = st.f_blocks * st.f_frsize
            free  = st.f_bfree  * st.f_frsize
            used  = total - free
            info["total_gb"] = round(total / 1e9, 2)
            info["used_gb"]  = round(used  / 1e9, 2)
            info["free_gb"]  = round(free  / 1e9, 2)
        except Exception:
            info["total_gb"] = None
        result.append(info)
    return result

@router.post("/")
def create_share(name: str, comment: str = "", smb: bool = True, nfs: bool = False):
    if not re.match(r'^[a-zA-Z0-9_-]+$', name):
        raise HTTPException(400, "Invalid share name")
    shares = _parse_shares()
    if name in shares:
        raise HTTPException(409, f"Share '{name}' already exists")
    # Append to shares.cfg
    with open(SHARES_CFG, "a") as f:
        f.write(f"\n[{name}]\n")
        f.write(f"comment={comment}\n")
        f.write(f"included_disks=all\n")
        f.write(f"allocation=most_free\n")
        f.write(f"split_level=1\n")
        f.write(f"smb={'yes' if smb else 'no'}\n")
        f.write(f"nfs={'yes' if nfs else 'no'}\n")
    os.makedirs(os.path.join(USER_SHARE, name), exist_ok=True)
    _apply_shares()
    return {"ok": True, "name": name}

class ShareUpdate(BaseModel):
    comment: Optional[str] = None
    smb: Optional[bool] = None
    nfs: Optional[bool] = None
    smb_security: Optional[str] = None  # "public" | "private" | username
    nfs_security: Optional[str] = None  # e.g. "192.168.1.0/24(rw,sync)"
    recycle_bin: Optional[bool] = None
    allocation: Optional[str] = None    # "most_free" | "first_found"
    split_level: Optional[int] = None
    included_disks: Optional[str] = None


def _write_shares(shares: dict):
    lines = []
    for section, cfg in shares.items():
        lines.append(f"[{section}]")
        for k, v in cfg.items():
            lines.append(f"{k}={v}")
        lines.append("")
    with open(SHARES_CFG, "w") as f:
        f.write("\n".join(lines))


@router.get("/{name}")
def get_share(name: str):
    shares = _parse_shares()
    if name not in shares:
        raise HTTPException(404, f"Share '{name}' not found")
    share_path = os.path.join(USER_SHARE, name)
    info = {"name": name, **shares[name]}
    try:
        st = os.statvfs(share_path)
        total = st.f_blocks * st.f_frsize
        free  = st.f_bfree  * st.f_frsize
        info.update({"total_gb": round(total/1e9, 2), "used_gb": round((total-free)/1e9, 2)})
    except Exception:
        pass
    return info


@router.put("/{name}")
def update_share(name: str, body: ShareUpdate):
    shares = _parse_shares()
    if name not in shares:
        raise HTTPException(404, f"Share '{name}' not found")
    cfg = shares[name]
    if body.comment      is not None: cfg["comment"]       = body.comment
    if body.smb          is not None: cfg["smb"]           = "yes" if body.smb else "no"
    if body.nfs          is not None: cfg["nfs"]           = "yes" if body.nfs else "no"
    if body.smb_security is not None: cfg["smb_security"]  = body.smb_security
    if body.nfs_security is not None: cfg["nfs_security"]  = body.nfs_security
    if body.recycle_bin  is not None: cfg["recycle_bin"]   = "yes" if body.recycle_bin else "no"
    if body.allocation   is not None: cfg["allocation"]    = body.allocation
    if body.split_level  is not None: cfg["split_level"]   = str(body.split_level)
    if body.included_disks is not None: cfg["included_disks"] = body.included_disks
    _write_shares(shares)
    _apply_shares()
    return {"ok": True, "name": name}


@router.delete("/{name}")
def delete_share(name: str):
    shares = _parse_shares()
    if name not in shares:
        raise HTTPException(404, f"Share '{name}' not found")
    del shares[name]
    _write_shares(shares)
    _apply_shares()
    return {"ok": True}
