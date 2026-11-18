"""Cloudflare Tunnel gate (CLAUDE.md §54.7).

The connector token and the API token stay on HELIXDATA, mode 0600.
Ingress is built here: one public hostname to one localhost port, then a
404 catch-all. Management ports and Helix's own name are refused.
Access is on unless the caller types NO ACCESS.
"""
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

from config import DATA_MNT

CONFIRM_ENABLE = "EXPOSE TO INTERNET"
CONFIRM_OPEN = "NO ACCESS"
TUNNEL_NAME = "helix"
MGMT_PORTS = {22, 80, 81, 443, 8082, 8443, 3781, 5050, 6556}
_HOST = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")


def tunnel_dir() -> str | None:
    override = os.environ.get("HELIX_TUNNEL_DIR")
    if override:
        return override
    if os.path.ismount(DATA_MNT):
        return os.path.join(DATA_MNT, "cloudflared")
    return None


def _paths(directory: str) -> dict:
    return {
        "dir": directory,
        "state": os.path.join(directory, "state.json"),
        "api_token": os.path.join(directory, "api.token"),
        "connector": os.path.join(directory, "connector.token"),
    }


def _read_state(directory: str) -> dict:
    try:
        data = json.loads(Path(_paths(directory)["state"]).read_text())
    except (OSError, ValueError):
        data = {}
    data.setdefault("hostnames", [])
    data.setdefault("enabled", False)
    return data


def _write_state(directory: str, state: dict) -> None:
    os.makedirs(directory, mode=0o700, exist_ok=True)
    path = _paths(directory)["state"]
    Path(path).write_text(json.dumps(state, indent=2))
    os.chmod(path, 0o600)


def _write_secret(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, content.encode())
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def _read_secret(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def public_status(rp_id: str = "") -> dict:
    directory = tunnel_dir()
    if not directory:
        return {
            "available": False,
            "reason": "The tunnel keeps its token on the data partition. Boot from writable media (HELIXDATA) first.",
            "enabled": False,
            "hostnames": [],
            "token_set": False,
            "account_id": "",
        }
    state = _read_state(directory)
    paths = _paths(directory)
    return {
        "available": True,
        "reason": "",
        "enabled": bool(state.get("enabled")),
        "running": _daemon_running(),
        "hostnames": [
            {"hostname": h.get("hostname"), "port": h.get("port"), "access": h.get("access", True),
             "dns_error": h.get("dns_error") or ""}
            for h in state.get("hostnames") or []
        ],
        "token_set": bool(_read_secret(paths["api_token"])),
        "account_id": state.get("account_id") or "",
        "tunnel_id": state.get("tunnel_id") or "",
        "rp_id": rp_id,
        "binary": os.path.isfile(os.path.join(directory, "bin", "cloudflared")),
    }


def save_settings(account_id: str, api_token: str | None) -> dict:
    directory = _need_dir()
    state = _read_state(directory)
    account_id = (account_id or "").strip()
    if account_id and not re.fullmatch(r"[A-Za-z0-9]{8,64}", account_id):
        raise ValueError("Account ID should be the 32-character id from the Cloudflare dashboard.")
    if account_id:
        state["account_id"] = account_id
    token = (api_token or "").strip()
    if token:
        if len(token) < 20:
            raise ValueError("That API token is too short.")
        _write_secret(_paths(directory)["api_token"], token)
    _write_state(directory, state)
    return {"ok": True, "token_set": bool(_read_secret(_paths(directory)["api_token"])), "account_id": state.get("account_id") or ""}


def check_hostname(hostname: str, port: int, rp_id: str, access: bool, confirm: str, ui_host: str = "") -> str | None:
    """Return an error string, or None when the route is allowed."""
    name = (hostname or "").strip().lower().rstrip(".")
    if "*" in name or not _HOST.match(name):
        return "Enter one hostname, such as files.example.com. Wildcards are not accepted."
    if name in ("localhost",) or name.endswith(".local") or name.endswith(".localhost"):
        return "The hostname has to be a public name Cloudflare can route."
    blocked = {(rp_id or "").lower(), (ui_host or "").lower()} - {""}
    if name in blocked:
        return "Helix's own address stays off the tunnel. Use Tailscale to reach the UI."
    try:
        port = int(port)
    except (TypeError, ValueError):
        return "Port must be a number."
    if port < 1 or port > 65535:
        return "Port must be between 1 and 65535."
    if port in MGMT_PORTS:
        return f"Port {port} is part of Helix's management plane and cannot be exposed."
    if not access and (confirm or "").strip() != CONFIRM_OPEN:
        return "Access protection is on unless you type NO ACCESS."
    return None


def ingress_for(hostnames: list[dict]) -> list[dict]:
    rules = [
        {"hostname": h["hostname"], "service": f"http://127.0.0.1:{int(h['port'])}"}
        for h in hostnames
    ]
    rules.append({"service": "http_status:404"})
    return rules


def enable_blockers(confirm: str, enforced: bool, has_credential: bool, media: bool, token_set: bool, account_id: str) -> list[str]:
    reasons = []
    if (confirm or "").strip() != CONFIRM_ENABLE:
        reasons.append('Type EXPOSE TO INTERNET to turn the tunnel on.')
    if not enforced:
        reasons.append("Turn on Require login, with at least one account, before exposing anything.")
    if not has_credential:
        reasons.append("Add a password, an authenticator, or a passkey first.")
    if not media:
        reasons.append("The tunnel token needs the data partition (HELIXDATA).")
    if not token_set:
        reasons.append("Add a Cloudflare API token first.")
    if not (account_id or "").strip():
        reasons.append("Add the Cloudflare account ID first.")
    return reasons


class Cloudflare:
    """Thin client. Tests pass fetch(method, url, body) -> (status, dict)."""

    def __init__(self, token: str, account_id: str, fetch=None):
        self.token = token
        self.account_id = account_id
        self._fetch = fetch or _urllib_fetch

    def _url(self, path: str) -> str:
        return "https://api.cloudflare.com/client/v4" + path

    def _call(self, method: str, path: str, body=None) -> dict:
        status, payload = self._fetch(method, self._url(path), self.token, body)
        if not isinstance(payload, dict) or not payload.get("success", status < 300):
            message = "Cloudflare refused the request."
            errors = payload.get("errors") if isinstance(payload, dict) else None
            if errors:
                message = errors[0].get("message") or message
            raise RuntimeError(message)
        return payload.get("result")

    def ensure_tunnel(self, name: str = TUNNEL_NAME) -> str:
        found = self._call("GET", f"/accounts/{self.account_id}/cfd_tunnel?is_deleted=false&name={name}")
        if isinstance(found, list):
            for row in found:
                if row.get("name") == name and row.get("id"):
                    return row["id"]
        created = self._call("POST", f"/accounts/{self.account_id}/cfd_tunnel",
                             {"name": name, "config_src": "cloudflare"})
        return created["id"]

    def connector_token(self, tunnel_id: str) -> str:
        result = self._call("GET", f"/accounts/{self.account_id}/cfd_tunnel/{tunnel_id}/token")
        if isinstance(result, str):
            return result
        if isinstance(result, dict) and result.get("token"):
            return result["token"]
        raise RuntimeError("Cloudflare did not return a connector token.")

    def put_ingress(self, tunnel_id: str, hostnames: list[dict]) -> None:
        self._call("PUT", f"/accounts/{self.account_id}/cfd_tunnel/{tunnel_id}/configurations",
                   {"config": {"ingress": ingress_for(hostnames)}})

    def ensure_access(self, hostname: str) -> str:
        apps = self._call("GET", f"/accounts/{self.account_id}/access/apps") or []
        app_id = ""
        if isinstance(apps, list):
            for app in apps:
                domain = (app.get("domain") or "").lower()
                if domain == hostname:
                    app_id = app.get("id") or ""
                    break
        if not app_id:
            created = self._call("POST", f"/accounts/{self.account_id}/access/apps", {
                "name": f"Helix {hostname}",
                "domain": hostname,
                "type": "self_hosted",
                "session_duration": "24h",
                "self_hosted_domains": [hostname],
            })
            app_id = created["id"]
        policies = self._call("GET", f"/accounts/{self.account_id}/access/apps/{app_id}/policies") or []
        if not policies:
            self._call("POST", f"/accounts/{self.account_id}/access/apps/{app_id}/policies", {
                "name": "Require sign-in",
                "decision": "allow",
                "include": [{"everyone": {}}],
                "precedence": 1,
            })
        return app_id

    def delete_access(self, app_id: str) -> None:
        if app_id:
            self._call("DELETE", f"/accounts/{self.account_id}/access/apps/{app_id}")

    def ensure_dns(self, hostname: str, tunnel_id: str) -> str:
        """Best-effort CNAME. Empty string on success, otherwise the reason."""
        zone_id = self._zone_for(hostname)
        if not zone_id:
            return "No Cloudflare zone matched this hostname. Add the CNAME in the dashboard."
        target = f"{tunnel_id}.cfargotunnel.com"
        records = self._call("GET", f"/zones/{zone_id}/dns_records?type=CNAME&name={hostname}") or []
        body = {"type": "CNAME", "name": hostname, "content": target, "proxied": True, "ttl": 1}
        if isinstance(records, list) and records:
            self._call("PUT", f"/zones/{zone_id}/dns_records/{records[0]['id']}", body)
        else:
            self._call("POST", f"/zones/{zone_id}/dns_records", body)
        return ""

    def _zone_for(self, hostname: str) -> str:
        parts = hostname.split(".")
        for i in range(len(parts) - 1):
            zone = ".".join(parts[i:])
            result = self._call("GET", f"/zones?name={zone}")
            if isinstance(result, list) and result:
                return result[0].get("id") or ""
        return ""


def _urllib_fetch(method: str, url: str, token: str, body):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = resp.read().decode() or "{}"
            return resp.status, json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"success": False, "errors": [{"message": raw[:300] or exc.reason}]}


def _need_dir() -> str:
    directory = tunnel_dir()
    if not directory:
        raise RuntimeError("The tunnel keeps its token on the data partition. Boot from writable media (HELIXDATA) first.")
    return directory


def _client(directory: str, fetch=None) -> tuple[Cloudflare, dict]:
    state = _read_state(directory)
    token = _read_secret(_paths(directory)["api_token"])
    account = state.get("account_id") or ""
    if not token or not account:
        raise RuntimeError("Add the Cloudflare account ID and API token first.")
    return Cloudflare(token, account, fetch=fetch), state


def _daemon_running() -> bool:
    pidfile = "/run/helix-cloudflared.pid"
    try:
        pid = int(Path(pidfile).read_text().strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def sync(fetch=None, start=None, stop=None) -> dict:
    """Push ingress and start or stop the connector to match saved state."""
    directory = _need_dir()
    state = _read_state(directory)
    if not state.get("enabled"):
        if stop:
            stop()
        return public_status()
    client, state = _client(directory, fetch)
    tunnel_id = state.get("tunnel_id") or client.ensure_tunnel()
    state["tunnel_id"] = tunnel_id
    token = client.connector_token(tunnel_id)
    _write_secret(_paths(directory)["connector"], token)
    for row in state.get("hostnames") or []:
        if row.get("access", True):
            row["access_app_id"] = client.ensure_access(row["hostname"])
        row["dns_error"] = client.ensure_dns(row["hostname"], tunnel_id)
    client.put_ingress(tunnel_id, state.get("hostnames") or [])
    _write_state(directory, state)
    if start:
        start(directory)
    return public_status()
