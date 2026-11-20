"""
Reverse proxy — drives the native Nginx Proxy Manager (CLAUDE.md §54.6).

The old hand-rolled nginx.conf writer is gone. This router talks to NPM's REST API
(admin nginx on 127.0.0.1:{NPM_ADMIN_PORT}, which injects the peer secret), signing in
with the admin account helix-npm-setup generated at /data/helix-admin.json.

GET    /api/proxy/status           — NPM available? enabled? running? host count
GET    /api/proxy/hosts            — list proxy hosts
POST   /api/proxy/hosts            — create a proxy host
PUT    /api/proxy/hosts/{id}       — update
DELETE /api/proxy/hosts/{id}       — delete
POST   /api/proxy/hosts/{id}/{enable|disable}
GET    /api/proxy/certificates     — list certificates
POST   /api/proxy/certificates     — request a Let's Encrypt cert (http or dns challenge)
GET    /api/proxy/dns-providers     — DNS-challenge providers available in this image
GET    /api/proxy/login            — admin creds for the embedded NPM UI (LAN only)
GET    /api/proxy/containers       — running docker containers, for "expose a container"

When NPM is off (no writable boot media → mode=ram, or array stopped) every endpoint
returns a clear message, never a 500. Nothing here is reachable off-LAN by design, but
the Helix API is still unauthenticated (§29.4): do not expose it to the internet.
"""

import json
import os
import re
import socket
import time
import urllib.error
import urllib.request

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import HELIX_CONFIG, STATE_DIR

router = APIRouter(prefix="/api/proxy", tags=["proxy"])

# ── Where NPM lives (env-overridable for tests) ───────────────────────────────
NPM_ENV_FILE  = os.getenv("NPM_ENV_FILE", "/usr/local/lib/helix/npm.env")
NPM_DATA_DIR  = os.getenv("NPM_DATA_DIR", "/data")
NPM_RUN_DIR   = os.getenv("NPM_RUN_DIR", "/run/npm")
NPM_STORAGE   = os.getenv("NPM_STATE_FILE", "/run/helix/npm-storage")
DNS_PLUGINS   = os.getenv("NPM_DNS_PLUGINS", "/opt/npm/dns-plugins.json")
PREINSTALLED  = os.getenv("NPM_PREINSTALLED", "/opt/npm/certbot-preinstalled.txt")
MIGRATE_FLAG  = os.path.join(STATE_DIR, "proxy_routes.migrated")
OLD_ROUTES    = os.path.join(HELIX_CONFIG, "proxy_routes.json")


def _admin_port() -> int:
    p = os.getenv("NPM_ADMIN_PORT")
    if not p:
        try:
            for line in open(NPM_ENV_FILE):
                if line.startswith("NPM_ADMIN_PORT="):
                    p = line.split("=", 1)[1].strip()
                    break
        except OSError:
            pass
    try:
        return int(p)
    except (TypeError, ValueError):
        return 81


def _admin_base() -> str:
    return os.getenv("NPM_ADMIN_URL", f"http://127.0.0.1:{_admin_port()}")


def _admin_file() -> str:
    return os.getenv("NPM_ADMIN_FILE", os.path.join(NPM_DATA_DIR, "helix-admin.json"))


# ── Availability ──────────────────────────────────────────────────────────────

def _storage_mode() -> tuple[str, str]:
    try:
        txt = open(NPM_STORAGE).read()
    except OSError:
        return "unknown", ""
    mode = reason = ""
    for line in txt.splitlines():
        if line.startswith("mode="):
            mode = line[5:].strip()
        elif line.startswith("reason="):
            reason = line[7:].strip()
    return mode or "unknown", reason


def _availability() -> dict:
    """{available, reason} — available means NPM is enabled and should answer."""
    if not os.path.exists(_admin_file()) and not os.path.exists(os.path.join(NPM_RUN_DIR, "enabled")):
        mode, reason = _storage_mode()
        if mode == "ram":
            from routers import datapart
            return {"available": False, "needs_data_partition": True,
                    "reason": datapart.explain("The reverse proxy keeps its settings and certificates on the data "
                                               "partition (HELIXDATA), which this system does not have mounted.")}
        return {"available": False,
                "reason": "Nginx Proxy Manager is not running yet. It starts with the array on a "
                          "writable install; check the array is started."}
    return {"available": True, "reason": ""}


def _require_npm():
    a = _availability()
    if not a["available"]:
        raise HTTPException(503, a["reason"])


# ── Talking to NPM ────────────────────────────────────────────────────────────
_token = {"value": "", "expires": 0.0}


def _get_token() -> str:
    if _token["value"] and _token["expires"] - time.time() > 60:
        return _token["value"]
    try:
        admin = json.load(open(_admin_file()))
    except (OSError, ValueError):
        raise HTTPException(503, "The NPM admin account is not set up yet (no /data/helix-admin.json).")
    body = json.dumps({"identity": admin.get("email"), "secret": admin.get("password")}).encode()
    try:
        req = urllib.request.Request(_admin_base() + "/api/tokens", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.load(r)
    except urllib.error.URLError as e:
        raise HTTPException(503, f"Could not reach Nginx Proxy Manager: {getattr(e, 'reason', e)}")
    _token["value"] = d["token"]
    # NPM tokens last 1 day; refresh well before.
    _token["expires"] = time.time() + 3600
    return _token["value"]


def _npm(method: str, path: str, body=None, _retry=True):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(_admin_base() + "/api" + path, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + _get_token()})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        if e.code in (401, 403) and _retry:
            _token["value"] = ""
            return _npm(method, path, body, _retry=False)
        try:
            msg = json.load(e).get("error", {}).get("message") or e.reason
        except Exception:
            msg = e.reason
        raise HTTPException(e.code if e.code in (400, 404) else 502, f"NPM: {msg}")
    except urllib.error.URLError as e:
        raise HTTPException(503, f"Could not reach Nginx Proxy Manager: {getattr(e, 'reason', e)}")


# ── Self-reference guard (phase 5 defense) ────────────────────────────────────

def _local_ips() -> set[str]:
    ips = {"127.0.0.1", "::1", "localhost"}
    try:
        host = socket.gethostname()
        ips.add(host.lower())
        for fam, *_rest, sa in socket.getaddrinfo(host, None):
            ips.add(sa[0])
    except OSError:
        pass
    # every address bound on this box
    try:
        import subprocess
        out = subprocess.run(["ip", "-o", "addr", "show"], capture_output=True, text=True, timeout=5).stdout
        ips.update(re.findall(r"inet6?\s+([0-9a-fA-F:.]+)/", out))
    except Exception:
        pass
    return {i.lower() for i in ips}


def _reject_self(domains: list[str], forward_host: str, forward_port: int):
    """Refuse a host that would proxy Helix onto itself (locks the UI). A domain that
    resolves to this box, or a forward target that is the Helix backend, is rejected."""
    mine = _local_ips()
    helix_port = int(os.getenv("HELIX_PORT", "8082"))
    fh = (forward_host or "").strip().lower()
    if fh in mine and int(forward_port) in (helix_port, 80, 443):
        raise HTTPException(400, "That upstream points at Helix itself. Choose the app's address and "
                                 "port, not this server's management port.")
    for d in domains:
        d = d.strip().lower()
        if not d or d.startswith("*."):
            continue
        try:
            resolved = {sa[0].lower() for *_r, sa in socket.getaddrinfo(d, None)}
        except OSError:
            resolved = set()
        if d in mine or (resolved and resolved <= mine):
            # Only block when the domain is used to reach Helix's own management UI port set.
            raise HTTPException(400, f"{d} already points at this server. Proxying it would shadow the "
                                     "Helix web UI. Use a different hostname for the app, or reach Helix "
                                     "on its fallback HTTPS port.")


# ── Schemas ───────────────────────────────────────────────────────────────────

class HostReq(BaseModel):
    domain: str                      # one or more, space/comma separated
    forward_host: str = ""           # app address; or use upstream
    forward_port: int = 0
    upstream: str = ""               # "host:port" convenience (overrides forward_* if set)
    forward_scheme: str = "http"     # http | https
    websocket: bool = True
    block_exploits: bool = True
    certificate_id: int = 0          # 0 none; -1 request a new LE cert (http challenge)
    ssl_forced: bool = False
    http2_support: bool = True
    hsts_enabled: bool = False
    advanced_config: str = ""


class CertReq(BaseModel):
    domain: str                      # space/comma separated
    email: str = ""
    method: str = "http"             # http | dns
    dns_provider: str = ""           # e.g. cloudflare (dns method only)
    dns_credentials: str = ""        # the provider credentials file body


def _domains(s: str) -> list[str]:
    return [d.strip() for d in re.split(r"[,\s]+", s or "") if d.strip()]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/status")
def status():
    a = _availability()
    if not a["available"]:
        return {"available": False, "reason": a["reason"], "hosts": 0}
    try:
        hosts = _npm("GET", "/nginx/proxy-hosts")
    except HTTPException as e:
        return {"available": False, "reason": e.detail, "hosts": 0}
    _migrate_once(hosts)
    return {"available": True, "reason": "", "hosts": len(hosts or []),
            "admin_port": _admin_port()}


def _host_view(h: dict) -> dict:
    cert = h.get("certificate") or {}
    return {
        "id": h["id"],
        "domain_names": h.get("domain_names", []),
        "forward_scheme": h.get("forward_scheme"),
        "forward_host": h.get("forward_host"),
        "forward_port": h.get("forward_port"),
        "enabled": bool(h.get("enabled", 1)),
        "ssl_forced": bool(h.get("ssl_forced", 0)),
        "certificate_id": h.get("certificate_id", 0),
        "certificate": {"id": cert.get("id"), "provider": cert.get("provider"),
                        "nice_name": cert.get("nice_name"), "expires": cert.get("expires_on")} if cert else None,
        "websocket": bool(h.get("allow_websocket_upgrade", 0)),
        "online": h.get("meta", {}).get("nginx_online", None),
    }


@router.get("/hosts")
def list_hosts():
    _require_npm()
    hosts = _npm("GET", "/nginx/proxy-hosts?expand=certificate") or []
    return [_host_view(h) for h in hosts]


def _build_host(req: HostReq) -> dict:
    fh, fp = req.forward_host, req.forward_port
    if req.upstream:
        m = re.match(r"^\s*(?:https?://)?([^:/\s]+):(\d+)\s*$", req.upstream)
        if not m:
            raise HTTPException(400, "Upstream must be host:port, e.g. 192.168.1.10:8096")
        fh, fp = m.group(1), int(m.group(2))
    domains = _domains(req.domain)
    if not domains:
        raise HTTPException(400, "Enter at least one domain name.")
    if not fh or not (0 < int(fp) < 65536):
        raise HTTPException(400, "Enter the app's address and port.")
    _reject_self(domains, fh, int(fp))
    return {
        "domain_names": domains,
        "forward_scheme": "https" if req.forward_scheme == "https" else "http",
        "forward_host": fh, "forward_port": int(fp),
        "access_list_id": 0, "certificate_id": max(req.certificate_id, 0),
        "meta": {"letsencrypt_agree": False, "dns_challenge": False},
        "advanced_config": req.advanced_config or "",
        "locations": [],
        "block_exploits": bool(req.block_exploits),
        "caching_enabled": False,
        "allow_websocket_upgrade": bool(req.websocket),
        "http2_support": bool(req.http2_support),
        "hsts_enabled": bool(req.hsts_enabled), "hsts_subdomains": False,
        "ssl_forced": bool(req.ssl_forced) and req.certificate_id > 0,
    }, domains


@router.post("/hosts")
def create_host(req: HostReq):
    _require_npm()
    payload, domains = _build_host(req)
    # -1 → request a new Let's Encrypt cert (http challenge) for these domains first.
    if req.certificate_id == -1:
        cert = _request_cert(CertReq(domain=req.domain, email="", method="http"))
        payload["certificate_id"] = cert["id"]
        payload["ssl_forced"] = True
    h = _npm("POST", "/nginx/proxy-hosts", payload)
    return {"ok": True, "host": _host_view(h)}


@router.put("/hosts/{host_id}")
def update_host(host_id: int, req: HostReq):
    _require_npm()
    payload, _ = _build_host(req)
    if req.certificate_id == -1:
        cert = _request_cert(CertReq(domain=req.domain, email="", method="http"))
        payload["certificate_id"] = cert["id"]
        payload["ssl_forced"] = True
    h = _npm("PUT", f"/nginx/proxy-hosts/{host_id}", payload)
    return {"ok": True, "host": _host_view(h)}


@router.delete("/hosts/{host_id}")
def delete_host(host_id: int):
    _require_npm()
    _npm("DELETE", f"/nginx/proxy-hosts/{host_id}")
    return {"ok": True}


@router.post("/hosts/{host_id}/{action}")
def host_action(host_id: int, action: str):
    _require_npm()
    if action not in ("enable", "disable"):
        raise HTTPException(404, "Unknown action")
    _npm("POST", f"/nginx/proxy-hosts/{host_id}/{action}")
    return {"ok": True}


# ── Certificates ──────────────────────────────────────────────────────────────

@router.get("/certificates")
def list_certs():
    _require_npm()
    certs = _npm("GET", "/nginx/certificates") or []
    return [{"id": c["id"], "provider": c.get("provider"), "nice_name": c.get("nice_name"),
             "domain_names": c.get("domain_names", []), "expires": c.get("expires_on")} for c in certs]


def _request_cert(req: CertReq) -> dict:
    domains = _domains(req.domain)
    if not domains:
        raise HTTPException(400, "Enter at least one domain name.")
    meta = {"letsencrypt_email": req.email or "admin@helix.local", "letsencrypt_agree": True,
            "dns_challenge": req.method == "dns"}
    if req.method == "dns":
        if not req.dns_provider:
            raise HTTPException(400, "Choose a DNS provider for the DNS challenge.")
        meta["dns_provider"] = req.dns_provider
        meta["dns_provider_credentials"] = req.dns_credentials
        meta["propagation_seconds"] = 60
    return _npm("POST", "/nginx/certificates", {"provider": "letsencrypt",
                                                "domain_names": domains, "meta": meta})


@router.post("/certificates")
def request_cert(req: CertReq):
    _require_npm()
    c = _request_cert(req)
    return {"ok": True, "id": c.get("id"), "domain_names": c.get("domain_names", [])}


@router.delete("/certificates/{cert_id}")
def delete_cert(cert_id: int):
    _require_npm()
    _npm("DELETE", f"/nginx/certificates/{cert_id}")
    return {"ok": True}


@router.get("/dns-providers")
def dns_providers():
    """DNS-challenge providers usable in this image: the ones pre-installed plus those with a
    wheelhouse (phase 4). Returns [{id, name, credentials_template}]."""
    try:
        plugins = json.load(open(DNS_PLUGINS))
    except (OSError, ValueError):
        return []
    usable = set()
    for src in (PREINSTALLED, os.getenv("NPM_WHEELHOUSE_LIST", "")):
        try:
            usable.update(open(src).read().split()) if src and os.path.exists(src) else None
        except OSError:
            pass
    # the wheelhouse set is fixed (powerdns, google) + whatever was pre-installed
    usable.update({"powerdns", "google"})
    out = []
    for key, p in plugins.items():
        if key in usable:
            out.append({"id": key, "name": p.get("name", key),
                        "credentials_template": p.get("credentials", "")})
    out.sort(key=lambda x: x["name"].lower())
    return out


# ── Embedded admin UI login (LAN only) ────────────────────────────────────────

@router.get("/login")
def login():
    _require_npm()
    try:
        admin = json.load(open(_admin_file()))
    except (OSError, ValueError):
        raise HTTPException(503, "The NPM admin account is not set up yet.")
    return {"user": admin.get("email"), "password": admin.get("password"),
            "port": _admin_port()}


# ── "Expose a container" helper ───────────────────────────────────────────────

@router.get("/containers")
def running_containers():
    """Running docker containers with a guessable address:port, for the expose picker."""
    _require_npm()
    try:
        from routers import docker as dk
        data = dk.list_containers()
    except Exception:
        return []
    out = []
    for c in (data.get("containers") if isinstance(data, dict) else data) or []:
        if c.get("state") != "running" and c.get("status", "").lower().find("up") < 0:
            continue
        ports = c.get("ports") or []
        # prefer a published host port; else the container's first exposed port
        host = c.get("ip") or ""
        guess_port = 0
        for p in ports:
            pp = p.get("public") or p.get("PublicPort") or p.get("host")
            if pp:
                guess_port = int(pp); host = host or "127.0.0.1"; break
        if not guess_port:
            for p in ports:
                cp = p.get("private") or p.get("PrivatePort") or p.get("container")
                if cp:
                    guess_port = int(cp); break
        out.append({"name": c.get("name"), "host": host or "", "port": guess_port})
    return out


# ── One-time migration from the old proxy_routes.json ─────────────────────────

def _migrate_once(existing_hosts):
    if os.path.exists(MIGRATE_FLAG) or not os.path.exists(OLD_ROUTES):
        return
    try:
        routes = json.load(open(OLD_ROUTES))
    except (OSError, ValueError):
        routes = []
    have = {d for h in (existing_hosts or []) for d in h.get("domain_names", [])}
    done = []
    for r in routes:
        dom = r.get("domain")
        if not dom or dom in have:
            continue
        m = re.match(r"^\s*(?:https?://)?([^:/\s]+):(\d+)", r.get("upstream", ""))
        if not m:
            continue
        try:
            _reject_self([dom], m.group(1), int(m.group(2)))
            _npm("POST", "/nginx/proxy-hosts", {
                "domain_names": [dom], "forward_scheme": "http",
                "forward_host": m.group(1), "forward_port": int(m.group(2)),
                "access_list_id": 0, "certificate_id": 0,
                "meta": {"letsencrypt_agree": False, "dns_challenge": False},
                "advanced_config": r.get("custom_headers", ""), "locations": [],
                "block_exploits": False, "caching_enabled": False,
                "allow_websocket_upgrade": bool(r.get("websocket")), "http2_support": False,
                "hsts_enabled": False, "hsts_subdomains": False, "ssl_forced": False})
            done.append(dom)
        except Exception:
            pass
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(MIGRATE_FLAG, "w") as f:
            f.write(f"migrated {len(done)} of {len(routes)} old routes: {', '.join(done)}\n")
        os.rename(OLD_ROUTES, OLD_ROUTES + ".migrated")
    except OSError:
        pass
