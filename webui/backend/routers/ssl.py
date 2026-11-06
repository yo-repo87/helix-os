"""
HTTPS / SSL certificate management.

GET  /api/ssl/status         — current cert info (subject, expiry, issuer)
POST /api/ssl/self-signed    — generate a self-signed cert
POST /api/ssl/upload         — upload a PEM cert + key
POST /api/ssl/letsencrypt    — issue/renew via certbot
GET  /api/ssl/enable         — is HTTPS currently enabled in nginx
POST /api/ssl/enable         — enable HTTPS in nginx
POST /api/ssl/disable        — disable HTTPS (HTTP-only)
"""

import datetime
import os
import subprocess
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File
from pydantic import BaseModel

router = APIRouter(prefix="/api/ssl", tags=["ssl"])

CERT_DIR  = "/etc/helix/ssl"
CERT_FILE = f"{CERT_DIR}/helix.crt"
KEY_FILE  = f"{CERT_DIR}/helix.key"
NGINX_SSL_CONF = "/etc/nginx/conf.d/helix-ssl.conf"


def _run(*args, timeout: int = 30) -> tuple[int, str]:
    try:
        r = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout + r.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def _sudo(*args, timeout: int = 60) -> tuple[int, str]:
    return _run("sudo", "-n", *args, timeout=timeout)


def _cert_info(cert_path: str) -> dict:
    """Parse cert info via openssl."""
    rc, out = _run("openssl", "x509", "-noout",
                   "-subject", "-issuer", "-dates", "-in", cert_path)
    if rc != 0:
        return {}
    info: dict = {}
    for line in out.splitlines():
        if "subject=" in line or "subject =" in line:
            info["subject"] = line.split("=", 1)[-1].strip()
        elif "issuer=" in line or "issuer =" in line:
            info["issuer"] = line.split("=", 1)[-1].strip()
        elif "notBefore" in line:
            info["not_before"] = line.split("=", 1)[-1].strip()
        elif "notAfter" in line:
            info["not_after"] = line.split("=", 1)[-1].strip()
    # Expiry in days
    rc2, out2 = _run("openssl", "x509", "-noout", "-checkend", "0", "-in", cert_path)
    info["expired"] = rc2 != 0
    rc3, days_out = _run("openssl", "x509", "-noout", "-enddate", "-in", cert_path)
    # Parse "notAfter=Jun  5 00:00:00 2026 GMT"
    try:
        date_str = days_out.split("=")[-1].strip()
        exp = datetime.datetime.strptime(date_str, "%b %d %H:%M:%S %Y %Z")
        info["days_remaining"] = (exp - datetime.datetime.utcnow()).days
    except (ValueError, IndexError):
        info["days_remaining"] = None
    return info


# ── Endpoints ─────────────────────────────────────────────────────

@router.get("/status")
def ssl_status():
    cert_exists = Path(CERT_FILE).exists() and Path(KEY_FILE).exists()
    nginx_ssl   = Path(NGINX_SSL_CONF).exists()
    info = _cert_info(CERT_FILE) if cert_exists else {}
    return {
        "cert_exists": cert_exists,
        "https_enabled": nginx_ssl,
        "cert_path": CERT_FILE if cert_exists else None,
        **info,
    }


class SelfSignedReq(BaseModel):
    hostname: str = "helixos.local"
    days: int = 3650
    country: str = "US"
    org: str = "Helix OS"


@router.post("/self-signed")
def gen_self_signed(req: SelfSignedReq):
    """Generate a self-signed certificate."""
    _sudo("mkdir", "-p", CERT_DIR)
    subject = f"/C={req.country}/O={req.org}/CN={req.hostname}"
    rc, out = _sudo("openssl", "req", "-x509", "-newkey", "rsa:4096",
                    "-keyout", KEY_FILE, "-out", CERT_FILE,
                    "-days", str(req.days), "-nodes", "-subj", subject)
    if rc != 0:
        raise HTTPException(500, f"openssl failed: {out[-300:]}")
    _sudo("chmod", "640", CERT_FILE, KEY_FILE)
    return {"ok": True, "cert": CERT_FILE, **_cert_info(CERT_FILE)}


@router.post("/upload")
async def upload_cert(cert: UploadFile = File(...), key: UploadFile = File(...)):
    """Upload a PEM certificate and private key."""
    _sudo("mkdir", "-p", CERT_DIR)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".crt") as cf:
        cf.write(await cert.read())
        tmp_cert = cf.name
    with tempfile.NamedTemporaryFile(delete=False, suffix=".key") as kf:
        kf.write(await key.read())
        tmp_key = kf.name

    # Validate cert
    rc, out = _run("openssl", "x509", "-noout", "-in", tmp_cert)
    if rc != 0:
        Path(tmp_cert).unlink(missing_ok=True)
        Path(tmp_key).unlink(missing_ok=True)
        raise HTTPException(400, "Invalid PEM certificate")

    _sudo("cp", tmp_cert, CERT_FILE)
    _sudo("cp", tmp_key, KEY_FILE)
    _sudo("chmod", "640", CERT_FILE, KEY_FILE)
    Path(tmp_cert).unlink(missing_ok=True)
    Path(tmp_key).unlink(missing_ok=True)
    return {"ok": True, **_cert_info(CERT_FILE)}


class LetsEncryptReq(BaseModel):
    domain: str
    email: str


@router.post("/letsencrypt")
def letsencrypt(req: LetsEncryptReq):
    """Run certbot to issue/renew a certificate."""
    if not req.domain or "." not in req.domain:
        raise HTTPException(400, "Valid domain required")
    rc, out = _sudo(
        "certbot", "certonly", "--standalone",
        "--non-interactive", "--agree-tos",
        "-m", req.email, "-d", req.domain,
        "--cert-path", CERT_FILE, "--key-path", KEY_FILE,
        timeout=120)
    if rc != 0:
        # Fallback: check if certbot isn't installed
        if "not found" in out.lower() or "no such file" in out.lower():
            raise HTTPException(500, "certbot not installed: sudo apt-get install certbot")
        raise HTTPException(500, out[-500:])
    return {"ok": True, **_cert_info(CERT_FILE)}


NGINX_SSL_SNIPPET = """\
# Generated by Helix OS — HTTPS
server {{
    listen 443 ssl http2;
    ssl_certificate     {cert};
    ssl_certificate_key {key};
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;
    # Proxy to Helix OS backend
    location / {{
        proxy_pass http://127.0.0.1:8082;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 300;
    }}
}}
"""


@router.post("/enable")
def enable_https():
    if not Path(CERT_FILE).exists():
        raise HTTPException(400, "No certificate found — generate or upload one first")
    conf = NGINX_SSL_SNIPPET.format(cert=CERT_FILE, key=KEY_FILE)
    import subprocess as sp
    r = sp.run(["sudo", "-n", "tee", NGINX_SSL_CONF],
               input=conf, capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        raise HTTPException(500, r.stderr)
    _sudo("systemctl", "reload", "nginx")
    return {"ok": True, "message": "HTTPS enabled on port 443"}


@router.post("/disable")
def disable_https():
    _sudo("rm", "-f", NGINX_SSL_CONF)
    _sudo("systemctl", "reload", "nginx")
    return {"ok": True, "message": "HTTPS disabled"}
