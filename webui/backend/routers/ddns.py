"""
Dynamic DNS — update public IP with DuckDNS, Cloudflare, No-IP, or custom URL.

GET  /api/ddns/config     — current config
PUT  /api/ddns/config     — save config
POST /api/ddns/update     — force immediate update
GET  /api/ddns/status     — last update result + timestamp
"""

import json
import os
import subprocess
import time
import urllib.request
import urllib.parse
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import HELIX_CONFIG, STATE_DIR

router = APIRouter(prefix="/api/ddns", tags=["ddns"])

CFG_FILE   = os.path.join(HELIX_CONFIG, "ddns.cfg")
STATE_FILE = os.path.join(STATE_DIR, "ddns_state.json")

PROVIDERS = ["duckdns", "cloudflare", "noip", "custom"]


def _load_cfg() -> dict:
    defaults = {
        "enabled":  "false",
        "provider": "duckdns",
        "domain":   "",
        "token":    "",
        "cf_zone":  "",
        "cf_record":"",
        "noip_user":"",
        "noip_pass":"",
        "custom_url":"",
        "interval_min": "5",
    }
    try:
        for line in Path(CFG_FILE).read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                defaults[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    defaults["enabled"] = defaults["enabled"].lower() in ("true", "1", "yes")
    return defaults


def _save_cfg(d: dict):
    Path(CFG_FILE).parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{k}={v}" for k, v in d.items()]
    Path(CFG_FILE).write_text("\n".join(lines) + "\n")


def _load_state() -> dict:
    try:
        return json.loads(Path(STATE_FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"last_update": None, "last_ip": None, "last_status": "never", "last_error": ""}


def _save_state(s: dict):
    Path(STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(STATE_FILE).write_text(json.dumps(s))


def _get_public_ip() -> str:
    for url in ["https://api.ipify.org", "https://ifconfig.me/ip"]:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                return r.read().decode().strip()
        except Exception:
            pass
    return ""


def _do_update(cfg: dict) -> dict:
    ip = _get_public_ip()
    if not ip:
        return {"ok": False, "error": "Could not determine public IP"}

    provider = cfg.get("provider", "duckdns")
    domain   = cfg.get("domain", "")
    token    = cfg.get("token", "")
    error    = ""
    ok       = False

    try:
        if provider == "duckdns":
            url = f"https://www.duckdns.org/update?domains={domain}&token={token}&ip={ip}"
            with urllib.request.urlopen(url, timeout=8) as r:
                body = r.read().decode().strip()
            ok = body.lower().startswith("ok")
            if not ok:
                error = body

        elif provider == "cloudflare":
            zone_id = cfg.get("cf_zone", "")
            record_name = cfg.get("cf_record", domain)
            if not zone_id:
                error = "Cloudflare zone ID not configured"
            else:
                # list records to find ID
                req = urllib.request.Request(
                    f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records?type=A&name={record_name}",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=8) as r:
                    data = json.loads(r.read())
                records = data.get("result", [])
                if records:
                    record_id = records[0]["id"]
                    payload = json.dumps({"type": "A", "name": record_name, "content": ip, "ttl": 60}).encode()
                    req2 = urllib.request.Request(
                        f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{record_id}",
                        data=payload, method="PUT",
                        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
                    with urllib.request.urlopen(req2, timeout=8) as r2:
                        res = json.loads(r2.read())
                    ok = res.get("success", False)
                    if not ok:
                        error = str(res.get("errors", "unknown"))
                else:
                    error = f"No A record found for {record_name}"

        elif provider == "noip":
            user = cfg.get("noip_user", "")
            pw   = cfg.get("noip_pass", "")
            encoded = urllib.parse.urlencode({"hostname": domain, "myip": ip})
            req = urllib.request.Request(
                f"https://dynupdate.no-ip.com/nic/update?{encoded}",
                headers={"User-Agent": "HelixOS/1.0 contact@helix-os.local",
                         "Authorization": "Basic " + __import__("base64").b64encode(
                             f"{user}:{pw}".encode()).decode()})
            with urllib.request.urlopen(req, timeout=8) as r:
                body = r.read().decode().strip()
            ok = body.startswith("good") or body.startswith("nochg")
            if not ok:
                error = body

        elif provider == "custom":
            url = cfg.get("custom_url", "").replace("{ip}", ip).replace("{domain}", domain)
            if not url:
                error = "Custom URL not configured"
            else:
                with urllib.request.urlopen(url, timeout=8) as r:
                    r.read()
                ok = True

        else:
            error = f"Unknown provider: {provider}"

    except Exception as e:
        error = str(e)

    state = {"last_update": int(time.time()), "last_ip": ip,
             "last_status": "ok" if ok else "error", "last_error": error}
    _save_state(state)
    return {"ok": ok, "ip": ip, "error": error}


# ── Endpoints ──────────────────────────────────────────────────────

class DdnsCfg(BaseModel):
    enabled: bool = False
    provider: str = "duckdns"
    domain: str = ""
    token: str = ""
    cf_zone: str = ""
    cf_record: str = ""
    noip_user: str = ""
    noip_pass: str = ""
    custom_url: str = ""
    interval_min: int = 5


@router.get("/config")
def get_config():
    return _load_cfg()


@router.put("/config")
def put_config(req: DdnsCfg):
    cfg = req.dict()
    cfg["enabled"] = "true" if cfg["enabled"] else "false"
    _save_cfg(cfg)
    return {"ok": True}


@router.post("/update")
def force_update():
    cfg = _load_cfg()
    if not cfg.get("domain") or not cfg.get("provider"):
        raise HTTPException(400, "DDNS not configured")
    result = _do_update(cfg)
    return result


@router.get("/status")
def get_status():
    return {**_load_state(), **{"config": _load_cfg()}}
