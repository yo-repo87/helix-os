from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
import os, re, subprocess
from config import SHARES_CFG, HELIX_CONFIG, USER_SHARE
from services import validation

# Installed to /usr/local/sbin by the build (hook 03). The old relative path
# resolved to a non-existent /shares in the built image, so share apply silently
# no-op'd. Configurable for dev.
SHARES_SBIN = os.getenv("SHARES_SBIN", "/usr/local/sbin")

def _apply_shares() -> str:
    """Regenerate smb.conf + /etc/exports after any config change.

    Needs root (it writes /etc/samba and /etc/exports and starts services), and the
    backend is the unprivileged helix user, so go through sudo. Returns '' on
    success or the tail of the script's output, so callers can show the failure
    instead of reporting a share that was never actually exported.
    """
    script = os.path.join(SHARES_SBIN, "helix-shares-apply")
    if not os.path.exists(script):
        return f"{script} not found"
    cmd = ["/bin/bash", script]
    if os.geteuid() != 0:
        cmd = ["sudo", "-n", "env", f"HELIX_CONFIG={HELIX_CONFIG}", f"USER_SHARE={USER_SHARE}", *cmd]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90,
                           env={**os.environ, "HELIX_CONFIG": HELIX_CONFIG, "USER_SHARE": USER_SHARE})
    except (OSError, subprocess.TimeoutExpired) as e:
        return str(e)
    if r.returncode != 0:
        return ((r.stderr or "") + (r.stdout or "")).strip()[-400:] or f"exit {r.returncode}"
    # The wrapper exits 0 even when the NFS step warned; surface that too.
    warn = [l for l in (r.stderr or "").splitlines() if "WARN" in l or "requires fsid" in l]
    return "\n".join(warn)[-400:]

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
        m = re.match(r'^\[([a-zA-Z0-9_-]+)\]$', line)
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
    existing = list(_parse_shares().keys())
    try:       # a differently-cased folder would clash for SMB clients even if no share is configured
        existing += [d for d in os.listdir(USER_SHARE) if d != name and os.path.isdir(os.path.join(USER_SHARE, d))]
    except OSError:
        pass
    err = validation.share_name(name, existing) or validation.share_comment(comment)
    if err:
        raise HTTPException(400, err)
    # Read raw config to detect duplicates that survive parse de-duplication
    try:
        raw = open(SHARES_CFG).read()
    except FileNotFoundError:
        raw = ""
    if re.search(rf'^\[{re.escape(name)}\]', raw, re.MULTILINE):
        raise HTTPException(409, f"Share '{name}' already exists")
    new_entry = (
        f"\n[{name}]\n"
        f"comment={comment}\n"
        f"included_disks=all\n"
        f"allocation=most_free\n"
        f"split_level=1\n"
        f"smb={'yes' if smb else 'no'}\n"
        f"nfs={'yes' if nfs else 'no'}\n"
    )
    with open(SHARES_CFG, "a") as f:
        f.write(new_entry)
    try:
        os.makedirs(os.path.join(USER_SHARE, name), exist_ok=True)
    except OSError as exc:
        # Roll back the config write on directory creation failure
        with open(SHARES_CFG, "r") as f:
            content = f.read()
        with open(SHARES_CFG, "w") as f:
            f.write(content.replace(new_entry, ""))
        raise HTTPException(500, f"Share config written but directory creation failed: {exc}")
    err = _apply_shares()
    return {"ok": True, "name": name, **({"apply_error": err} if err else {})}

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
    if body.comment is not None:
        err = validation.share_comment(body.comment)
        if err:
            raise HTTPException(400, err)
        cfg["comment"] = body.comment
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
