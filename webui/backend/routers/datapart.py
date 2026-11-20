"""The HELIXDATA data partition on the boot drive (root helper: array/sbin/helix-data-partition).

GET  /api/system/data-partition          what the boot drive has, whether a data partition can be added, and why not
POST /api/system/data-partition {confirm: true}   add it in the unused space (takes effect at the next boot)

Old sticks (and old image layouts) have no HELIXDATA, so the reverse proxy, the Cloudflare tunnel and persistent
settings on ext4 are unavailable; this explains that precisely and offers to fix it without re-flashing.
"""
import json
import os
import subprocess

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import ARRAY_SBIN

router = APIRouter(prefix="/api/system/data-partition", tags=["system"])

HELPER = os.path.join(ARRAY_SBIN, "helix-data-partition")
if not os.path.exists(HELPER):
    _repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "array", "sbin", "helix-data-partition"))
    if os.path.exists(_repo):
        HELPER = _repo


def _helper(arg: str, timeout: int) -> tuple[int, dict | None, str]:
    try:
        r = subprocess.run(["sudo", "-n", "python3", HELPER, arg], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, None, str(e)
    try:
        return r.returncode, json.loads(r.stdout.strip().splitlines()[-1]), ""
    except (ValueError, IndexError):
        return r.returncode, None, (r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else "failed"


def status() -> dict:
    code, data, err = _helper("status", 30)
    if data is None:
        return {"can_create": False, "data_partition": "", "data_mounted": False,
                "reason": f"Could not check the boot drive: {err}"}
    return data


def explain(prefix: str) -> str:
    """One sentence for a feature that needs the data partition (proxy, tunnel)."""
    st = status()
    if st.get("data_partition") and not st.get("data_mounted"):
        return f"{prefix} A data partition was created; reboot to start using it."
    if st.get("can_create"):
        return f"{prefix} {st['reason']} Create a data partition (below) and reboot."
    return f"{prefix} {st.get('reason', '')}".strip()


@router.get("")
def get_status():
    return status()


class CreateReq(BaseModel):
    confirm: bool = False


@router.post("")
def create(req: CreateReq):
    if not req.confirm:
        raise HTTPException(400, "Confirm creating the data partition.")
    code, data, err = _helper("create", 600)
    if code != 0 or not data:
        raise HTTPException(409, err or "Could not create the data partition.")
    return data
