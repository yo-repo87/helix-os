"""
Apps router — template catalog + source management + install/remove lifecycle.

Template sources (GET /api/apps/sources):
  builtin      — data/builtin-templates/*.json
  unraid-ca    — Unraid Community Applications AppFeed (default enabled)
  portainer    — Any Portainer v1/v2 JSON template URL
  unraid-xml   — Individual Unraid XML template URLs

Install/remove uses docker compose and stores state in STATE_DIR/apps/{name}/.
"""

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from config import STATE_DIR, HELIX_CONFIG
from services.template_loader import (
    add_source,
    invalidate_cache,
    load_all_templates,
    load_sources,
    remove_source,
    save_sources,
    _slugify,
)

# Persistent: an app's compose file, variables (tokens!) and extras must survive a reboot.
APPS_STATE_DIR = os.path.join(HELIX_CONFIG, "apps")

router = APIRouter(prefix="/api/apps", tags=["apps"])


# ── Template source endpoints ─────────────────────────────────────────────────

@router.get("/sources")
def list_sources():
    return load_sources()


class SourceCreate(BaseModel):
    name:  str
    type:  str   # "portainer" | "unraid-xml" | "unraid-ca-feed"
    url:   str
    enabled: bool = True


@router.post("/sources")
def create_source(req: SourceCreate):
    if req.type not in ("portainer", "unraid-xml", "unraid-ca-feed"):
        raise HTTPException(400, "type must be portainer, unraid-xml, or unraid-ca-feed")
    src = {
        "id":      _slugify(req.name),
        "name":    req.name,
        "type":    req.type,
        "url":     req.url,
        "enabled": req.enabled,
    }
    if any(x["id"] == src["id"] for x in load_sources()):
        raise HTTPException(409, f"Source id '{src['id']}' already exists")
    # Try it first: a source that yields nothing should say why instead of silently adding an empty entry.
    try:
        from services import template_loader as _tl
        found = {"portainer": _tl._load_portainer, "unraid-xml": _tl._load_unraid_xml_url,
                 "unraid-ca-feed": _tl._load_unraid_ca_feed}[req.type](src)
    except Exception as e:
        invalidate_cache(src["id"])
        raise HTTPException(400, f"Could not read {req.url}: {e}. If it is a private repository, make it public or use a direct link to a file.")
    if not found:
        invalidate_cache(src["id"])
        hint = {"unraid-xml": "no Unraid template .xml files were found there",
                "portainer": "no Portainer templates were found there (expected a templates JSON file)",
                "unraid-ca-feed": "the feed contained no apps"}[req.type]
        raise HTTPException(400, f"Nothing to add: {hint}.")
    try:
        add_source(src)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {**src, "count": len(found)}


@router.post("/sources/restore-defaults")
def restore_defaults():
    from services.template_loader import restore_default_sources
    return {"added": restore_default_sources()}


@router.patch("/sources/{source_id}")
def update_source(source_id: str, enabled: bool | None = None):
    sources = load_sources()
    for s in sources:
        if s["id"] == source_id:
            if enabled is not None:
                s["enabled"] = enabled
            save_sources(sources)
            return s
    raise HTTPException(404, f"Source '{source_id}' not found")


@router.delete("/sources/{source_id}")
def delete_source(source_id: str):
    try:
        remove_source(source_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.post("/sources/{source_id}/refresh")
def refresh_source(source_id: str, background_tasks: BackgroundTasks):
    sources = load_sources()
    if not any(s["id"] == source_id for s in sources):
        raise HTTPException(404, f"Source '{source_id}' not found")
    invalidate_cache(source_id)
    background_tasks.add_task(load_all_templates, source_id)
    return {"ok": True, "message": f"Cache invalidated for '{source_id}' — will re-fetch on next request"}


# ── Template catalog endpoints ────────────────────────────────────────────────

@router.get("/templates")
def list_templates(
    q:        str | None = None,
    category: str | None = None,
    source:   str | None = None,
):
    templates = load_all_templates(source_filter=source)

    if category:
        templates = [t for t in templates if t.get("category", "").lower() == category.lower()]
    if q:
        ql = q.lower()
        templates = [
            t for t in templates
            if ql in t.get("title", "").lower()
            or ql in t.get("description", "").lower()
            or ql in t.get("category", "").lower()
        ]

    # The CA catalog has thousands of apps: send only what a card shows. The full
    # template (compose + variables) comes from /templates/{name} when one is opened.
    def card(t: dict) -> dict:
        c = {k: (t.get(k) or "") for k in ("name", "title", "description", "category", "icon", "source_id")}
        c["port"] = t.get("port")
        return c
    return [card(t) for t in templates]


@router.get("/templates/{name}")
def get_template(name: str):
    templates = load_all_templates()
    for t in templates:
        if t.get("name") == name:
            return {**t, "options": _options_info(t.get("compose") or {}, None)}
    raise HTTPException(404, f"Template '{name}' not found")


# ── Installed app helpers ─────────────────────────────────────────────────────

def _installed_apps() -> dict[str, dict]:
    result = {}
    base = Path(APPS_STATE_DIR)
    if not base.exists():
        return result
    for d in base.iterdir():
        if d.is_dir() and (d / "docker-compose.yml").exists():
            info: dict[str, Any] = {
                "compose_path": str(d / "docker-compose.yml"),
                "dir":          str(d),
            }
            vars_file = d / "vars.json"
            if vars_file.exists():
                try:
                    info["vars"] = json.loads(vars_file.read_text())
                except Exception:
                    info["vars"] = {}
            result[d.name] = info
    return result


def _container_states() -> dict[str, str]:
    try:
        r = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}"],
            capture_output=True, text=True, timeout=5
        )
        states: dict[str, str] = {}
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 2:
                states[parts[0].strip()] = parts[1].strip()
        return states
    except Exception:
        return {}


@router.get("/installed")
def list_installed():
    installed = _installed_apps()
    states    = _container_states()
    result    = []
    templates = {t["name"]: t for t in load_all_templates()}
    for name, info in installed.items():
        state = states.get(name, "unknown")
        t     = templates.get(name)
        result.append({
            "name":      name,
            "title":     t["title"]    if t else name,
            "category":  t["category"] if t else "",
            "icon":      t.get("icon") if t else "",
            "port":      t.get("port") if t else None,
            "source_id": t.get("source_id", "unknown") if t else "unknown",
            "state":     state,
            "vars":      info.get("vars", {}),
        })
    return result


# ── Install helpers ───────────────────────────────────────────────────────────

def _substitute_vars(obj: Any, vars_map: dict) -> Any:
    if isinstance(obj, str):
        for k, v in vars_map.items():
            obj = obj.replace("{{" + k + "}}", str(v))
        return obj
    if isinstance(obj, list):
        return [_substitute_vars(i, vars_map) for i in obj]
    if isinstance(obj, dict):
        return {k: _substitute_vars(v, vars_map) for k, v in obj.items()}
    return obj


def _check_host_path(label: str, val: str) -> str:
    """A host path from the install form. It is substituted into a compose volume spec
    ('<host>:<container>'), so a ':' or newline would let a value rewrite the mapping."""
    val = (val or "").strip()
    if not val:
        return val
    if not val.startswith("/"):
        raise HTTPException(400, f"{label}: the folder must be an absolute path (start with /), got {val!r}")
    if any(c in val for c in (":", "\n", "\r", "\0")):
        raise HTTPException(400, f"{label}: the folder path cannot contain ':' or line breaks")
    if ".." in val.split("/"):
        raise HTTPException(400, f"{label}: the folder path cannot contain '..'")
    return os.path.normpath(val) if val != "/" else val


def _validate_path_vars(template: dict, vars_map: dict) -> dict:
    """Return vars_map with every folder-type value validated/normalised (raises 400)."""
    out = dict(vars_map)
    for v in template.get("vars", []):
        if v.get("type") == "path":
            k = v["key"]
            out[k] = _check_host_path(v.get("label") or k, out.get(k, v.get("default", "")))
    return out


def _ensure_dirs(template: dict, vars_map: dict) -> None:
    """Create the folders an app will mount, as the Helix user (so they are owned by Helix and
    shared apps/SMB/NFS can all write them). Docker would otherwise create missing bind sources
    as root. Only under /mnt: system paths such as /var/run/docker.sock must never be created."""
    for v in template.get("vars", []):
        if v.get("type") != "path":
            continue
        val = vars_map.get(v["key"], "")
        # Only the app's own settings folder is created for it. Media/library folders are never
        # created automatically (the user points them at existing folders, or leaves them unused).
        if val.startswith("/mnt/user/appdata/"):
            try:
                Path(val).mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise RuntimeError(f"cannot create the folder {val} for '{v.get('label') or v['key']}': {e.strerror or e}")


_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _clean_extras(raw: dict | None) -> dict:
    """Validate user-supplied extras: {"env": {KEY: value}, "volumes": [{host, container, ro}]}."""
    raw = raw or {}
    env = {}
    for k, v in (raw.get("env") or {}).items():
        k = str(k).strip()
        if not k:
            continue
        if not _ENV_KEY_RE.match(k):
            raise HTTPException(400, f"Invalid environment variable name: {k!r}")
        env[k] = str(v)
    vols = []
    for v in raw.get("volumes") or []:
        host, cont = str(v.get("host", "")).strip(), str(v.get("container", "")).strip()
        if not host and not cont:
            continue
        if cont and not cont.startswith("/"):
            cont = "/" + cont          # a bare name like "Anime" means /Anime inside the container
        if not host:
            raise HTTPException(400, f"Extra folder: the server folder is empty (the container path is {cont!r})")
        if not cont:
            raise HTTPException(400, f"Extra folder {host!r}: say where it appears inside the container, e.g. /data")
        if not host.startswith("/"):
            raise HTTPException(400, f"Extra folder: the server folder must start with / (got {host!r}), e.g. /mnt/user/media")
        if ".." in host.split("/") or ".." in cont.split("/"):
            raise HTTPException(400, "Folder paths cannot contain '..'")
        if ":" in host or ":" in cont:
            raise HTTPException(400, "Folder paths cannot contain ':'")
        vols.append({"host": host, "container": cont, "ro": bool(v.get("ro"))})
    out = {"env": env, "volumes": vols}
    if raw.get("options") is not None:
        out["options"] = _clean_options(raw["options"])
    return out


# ── Container options (restart policy, privileged, log limit …) ───────────────
# Stored in extras.json as "options". Absent = the template's compose is used unchanged.

RESTART_POLICIES = ("unless-stopped", "always", "on-failure", "no")
_BOOL_OPTIONS = ("privileged", "init", "localtime", "log_limit", "gpu_dri")
_LOG_LIMIT = {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}}


def _num_option(label: str, val, lo: float, hi: float) -> str:
    val = str(val if val is not None else "").strip()
    if not val:
        return ""
    try:
        n = float(val)
    except ValueError:
        raise HTTPException(400, f"{label}: enter a number, or leave it empty for no limit (got {val!r})")
    if not lo <= n <= hi:
        raise HTTPException(400, f"{label}: must be between {lo:g} and {hi:g} (got {val})")
    return f"{n:g}"


def _clean_options(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise HTTPException(400, "Container options must be an object")
    restart = str(raw.get("restart") or "unless-stopped")
    if restart not in RESTART_POLICIES:
        raise HTTPException(400, f"Restart policy must be one of {', '.join(RESTART_POLICIES)}")
    opts = {"restart": restart}
    for k in _BOOL_OPTIONS:
        opts[k] = bool(raw.get(k))
    opts["memory_gb"] = _num_option("Memory limit (GB)", raw.get("memory_gb"), 0.1, 4096)
    opts["cpus"] = _num_option("CPU limit (cores)", raw.get("cpus"), 0.1, float(os.cpu_count() or 1))
    return opts


def _main_service(compose: dict) -> str | None:
    """The service the user is configuring: the first one that publishes ports (or uses host
    networking), else the last one (helpers such as databases are usually listed first)."""
    services = compose.get("services") or {}
    if not services:
        return None
    for n, svc in services.items():
        if isinstance(svc, dict) and (svc.get("ports") or svc.get("network_mode") == "host"):
            return n
    return list(services)[-1]


def _is_localtime(v) -> bool:
    return isinstance(v, str) and v.split(":", 1)[0] == "/etc/localtime"


def _is_dri(d) -> bool:
    return isinstance(d, str) and d.split(":", 1)[0].startswith("/dev/dri")


def _mem_to_gb(v) -> str:
    """'2g' / '512m' / 2147483648 -> '2' / '0.5' / '2' (for showing a template's own limit)."""
    m = re.match(r"^\s*([\d.]+)\s*([kmgt]?)b?\s*$", str(v or "").lower())
    if not m:
        return ""
    n = float(m.group(1)) * {"": 1 / 1024**3, "k": 1 / 1024**2, "m": 1 / 1024, "g": 1, "t": 1024}[m.group(2)]
    return f"{round(n, 2):g}"


def _effective_options(compose: dict) -> dict:
    """The options a compose project currently has (what the form shows)."""
    services = compose.get("services") or {}
    main = services.get(_main_service(compose)) or {}
    allsvc = [s for s in services.values() if isinstance(s, dict)]
    restart = main.get("restart") or "no"
    return {
        "restart":    restart if restart in RESTART_POLICIES else "unless-stopped",
        "privileged": bool(main.get("privileged")),
        "init":       bool(main.get("init")),
        "localtime":  any(_is_localtime(v) for v in (main.get("volumes") or [])),
        "log_limit":  bool(allsvc) and all(bool(s.get("logging")) for s in allsvc),
        "gpu_dri":    any(_is_dri(d) for d in (main.get("devices") or [])),
        "memory_gb":  _mem_to_gb(main.get("mem_limit")),
        "cpus":       f"{float(main['cpus']):g}" if main.get("cpus") else "",
    }


def _host_timezone() -> str:
    try:
        from routers.settings import _detect_timezone
        return _detect_timezone()
    except Exception:
        return "UTC"


def _apply_options(compose: dict, opts: dict | None) -> dict:
    if not opts:
        return compose
    services = compose.get("services") or {}
    main_name = _main_service(compose)
    for n, svc in services.items():
        if not isinstance(svc, dict):
            continue
        # restart policy and log size apply to every container of the app (incl. its database)
        svc["restart"] = opts["restart"]
        if opts["log_limit"]:
            svc["logging"] = json.loads(json.dumps(_LOG_LIMIT))
        elif svc.get("logging") == _LOG_LIMIT:
            del svc["logging"]
        if n != main_name:
            continue
        for key in ("privileged", "init"):
            if opts[key]:
                svc[key] = True
            else:
                svc.pop(key, None)
        vols = [v for v in (svc.get("volumes") or []) if not _is_localtime(v)]
        env = svc.get("environment") or {}
        if isinstance(env, list):
            env = dict(e.split("=", 1) if "=" in e else (e, "") for e in env)
        if opts["localtime"]:
            vols.append("/etc/localtime:/etc/localtime:ro")
            env.setdefault("TZ", _host_timezone())
        if vols:
            svc["volumes"] = vols
        else:
            svc.pop("volumes", None)
        if env:
            svc["environment"] = env
        devs = [d for d in (svc.get("devices") or []) if not _is_dri(d)]
        if opts["gpu_dri"]:
            devs.append("/dev/dri:/dev/dri")
        if devs:
            svc["devices"] = devs
        else:
            svc.pop("devices", None)
        svc.pop("mem_limit", None)
        svc.pop("cpus", None)
        if opts["memory_gb"]:
            svc["mem_limit"] = f"{int(float(opts['memory_gb']) * 1024)}m"
        if opts["cpus"]:
            svc["cpus"] = float(opts["cpus"])
    return compose


def _options_info(compose: dict, saved: dict | None) -> dict:
    """Current option values for the form plus what this host can offer."""
    return {"values": saved or _effective_options(compose),
            "gpu_dri_available": os.path.isdir("/dev/dri"),
            "max_cpus": os.cpu_count() or 1,
            "timezone": _host_timezone()}


def _read_extras(app_dir: Path) -> dict:
    try:
        return _clean_extras(json.loads((Path(app_dir) / "extras.json").read_text()))
    except Exception:
        return {"env": {}, "volumes": []}


def _apply_extras(compose: dict, extras: dict) -> dict:
    """Merge extras into the app's main service (not a helper such as its database)."""
    services = compose.get("services") or {}
    if not services or not (extras.get("env") or extras.get("volumes")):
        return compose
    svc = services[_main_service(compose)]
    if extras.get("env"):
        env = svc.get("environment") or {}
        if isinstance(env, list):
            env = dict(e.split("=", 1) for e in env if "=" in e)
        env.update(extras["env"])
        svc["environment"] = env
    for v in extras.get("volumes", []):
        spec = f'{v["host"]}:{v["container"]}' + (":ro" if v.get("ro") else "")
        vols = svc.setdefault("volumes", [])
        if spec not in vols:
            vols.append(spec)
    return compose


def _write_compose(app_dir: Path, template: dict, vars_map: dict, extras: dict | None = None) -> Path:
    compose = _substitute_vars(template["compose"], vars_map)
    extras = extras if extras is not None else _read_extras(app_dir)
    compose = _apply_extras(compose, extras)
    compose = _apply_options(compose, extras.get("options"))
    # a folder left empty is "not used": drop its mapping instead of mounting ':/path'
    for svc in (compose.get("services") or {}).values():
        if isinstance(svc.get("volumes"), list):
            svc["volumes"] = [x for x in svc["volumes"] if not (isinstance(x, str) and x.startswith(":"))]
            if not svc["volumes"]:
                del svc["volumes"]
    if "version" not in compose:
        compose = {"version": "3.8", **compose}
    dest = app_dir / "docker-compose.yml"
    dest.write_text(yaml.dump(compose, default_flow_style=False, allow_unicode=True))
    return dest


def _require_docker() -> None:
    """Fail fast with a clear 503 instead of reporting success for work that
    can never happen (start/stop/update/install all shell out to docker)."""
    import shutil
    if not shutil.which("docker"):
        raise HTTPException(503, "Docker is not installed on this system")
    try:
        up = subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except subprocess.TimeoutExpired:
        up = False
    if not up:
        raise HTTPException(503, "Docker is installed but not running. Start the array first (Docker starts with it).")


def _compose_base() -> list[str]:
    """`docker compose` (v2 plugin) when present, else Debian's docker-compose v1."""
    import shutil
    try:
        if subprocess.run(["docker", "compose", "version"], capture_output=True, timeout=10).returncode == 0:
            return ["docker", "compose"]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return ["docker-compose"] if shutil.which("docker-compose") else ["docker", "compose"]


def _docker_compose(app_dir: str | Path, *args, timeout: int = 60) -> tuple[int, str, str]:
    base = _compose_base()
    args = list(args)
    if base[0] == "docker-compose" and "--pull" in args:
        # v1 has no `up --pull <policy>`; pull first, then bring it up.
        i = args.index("--pull")
        del args[i:i + 2]
        subprocess.run([*base, "pull"], capture_output=True, text=True, cwd=str(app_dir), timeout=timeout)
    r = subprocess.run(
        [*base, *args],
        capture_output=True, text=True,
        cwd=str(app_dir), timeout=timeout
    )
    return r.returncode, r.stdout, r.stderr


def _set_install_status(name: str, state: str, error: str = "") -> None:
    status_file = Path(APPS_STATE_DIR) / name / "install_status.json"
    status_file.parent.mkdir(parents=True, exist_ok=True)
    status_file.write_text(json.dumps({"state": state, "error": error}))


def _do_install(name: str, vars_map: dict, extras: dict | None = None) -> None:
    app_dir = Path(APPS_STATE_DIR) / name
    app_dir.mkdir(parents=True, exist_ok=True)
    _set_install_status(name, "installing")
    try:
        templates = {t["name"]: t for t in load_all_templates()}
        t = templates.get(name)
        if not t:
            _set_install_status(name, "error", "Template not found")
            return

        final_vars = {v["key"]: v["default"] for v in t.get("vars", [])}
        final_vars.update(vars_map)
        (app_dir / "vars.json").write_text(json.dumps(final_vars, indent=2))
        extras = _clean_extras(extras)
        (app_dir / "extras.json").write_text(json.dumps(extras, indent=2))
        for ev in extras["volumes"]:
            try:
                Path(ev["host"]).mkdir(parents=True, exist_ok=True)
            except OSError:
                pass   # e.g. an existing media mount we cannot create into; docker will report real problems

        _ensure_dirs(t, final_vars)

        _write_compose(app_dir, t, final_vars, extras)
        rc, out, err = _docker_compose(app_dir, "up", "-d", "--pull", "always", timeout=300)
        if rc != 0:
            _set_install_status(name, "error", err.strip()[-500:] or out.strip()[-500:])
        else:
            _set_install_status(name, "installed")
    except Exception as exc:
        _set_install_status(name, "error", str(exc))


# ── Install / lifecycle endpoints ─────────────────────────────────────────────

class InstallRequest(BaseModel):
    name: str
    vars: dict[str, str] = {}
    extras: dict | None = None      # {"env": {...}, "volumes": [{host, container, ro}], "options": {...}}


@router.post("/install")
def install_app(req: InstallRequest, background_tasks: BackgroundTasks):
    templates = {t["name"]: t for t in load_all_templates()}
    if req.name not in templates:
        raise HTTPException(404, f"Template '{req.name}' not found")
    if req.name in _installed_apps():
        raise HTTPException(409, f"'{req.name}' is already installed")
    _require_docker()
    _clean_extras(req.extras)    # fail fast (400) on bad input, before the background task
    clean_vars = _validate_path_vars(templates[req.name], {**{v["key"]: v["default"] for v in templates[req.name].get("vars", [])}, **req.vars})
    background_tasks.add_task(_do_install, req.name, {**req.vars, **{k: clean_vars[k] for k in req.vars if k in clean_vars}}, req.extras)
    return {"ok": True, "message": f"Installing {templates[req.name]['title']}…"}


@router.get("/{name}/config")
def get_app_config(name: str):
    """Everything the Configure dialog needs: the template's variables with their
    current values, plus the user's extra environment variables and folders."""
    installed = _installed_apps()
    if name not in installed:
        raise HTTPException(404, f"'{name}' is not installed")
    t = {x["name"]: x for x in load_all_templates()}.get(name)
    cur = installed[name].get("vars", {})
    tvars = [{**v, "value": cur.get(v["key"], v.get("default", ""))} for v in (t.get("vars", []) if t else [])]
    extras = _read_extras(installed[name]["dir"])
    try:
        current = yaml.safe_load(Path(installed[name]["compose_path"]).read_text()) or {}
    except Exception:
        current = (t or {}).get("compose") or {}
    return {"name": name, "title": t["title"] if t else name,
            "template_available": t is not None,
            "vars": tvars, "extras": extras,
            "options": _options_info(current, extras.get("options"))}


class ConfigUpdate(BaseModel):
    vars: dict[str, str] = {}
    extras: dict | None = None


@router.put("/{name}/config")
def set_app_config(name: str, req: ConfigUpdate, background_tasks: BackgroundTasks):
    """Save new variables/extras, regenerate the compose file and recreate the
    container so the change takes effect. Progress shows up in install-status."""
    installed = _installed_apps()
    if name not in installed:
        raise HTTPException(404, f"'{name}' is not installed")
    t = {x["name"]: x for x in load_all_templates()}.get(name)
    if not t:
        raise HTTPException(409, "The template this app came from is no longer available "
                                 "(re-enable its source), so its configuration cannot be rebuilt.")
    extras = _clean_extras(req.extras)
    _require_docker()
    app_dir = Path(installed[name]["dir"])
    merged = _validate_path_vars(t, {**installed[name].get("vars", {}), **req.vars})
    background_tasks.add_task(_reconfigure, name, app_dir, t, merged, extras)
    return {"ok": True, "message": f"Applying configuration to {t['title']}…"}


def _reconfigure(name: str, app_dir: Path, t: dict, vars_map: dict, extras: dict) -> None:
    _set_install_status(name, "installing")
    try:
        (app_dir / "vars.json").write_text(json.dumps(vars_map, indent=2))
        (app_dir / "extras.json").write_text(json.dumps(extras, indent=2))
        _ensure_dirs(t, vars_map)
        for ev in extras["volumes"]:
            try:
                Path(ev["host"]).mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
        _write_compose(app_dir, t, vars_map, extras)
        rc, out, err = _docker_compose(app_dir, "up", "-d", "--force-recreate", timeout=300)
        if rc != 0:
            _set_install_status(name, "error", err.strip()[-500:] or out.strip()[-500:])
        else:
            _set_install_status(name, "installed")
    except Exception as exc:
        _set_install_status(name, "error", str(exc))


@router.get("/{name}/install-status")
def install_status(name: str):
    status_file = Path(APPS_STATE_DIR) / name / "install_status.json"
    if not status_file.exists():
        raise HTTPException(404)
    return json.loads(status_file.read_text())


@router.post("/{name}/start")
def start_app(name: str, background_tasks: BackgroundTasks):
    installed = _installed_apps()
    if name not in installed:
        raise HTTPException(404)
    _require_docker()
    background_tasks.add_task(_docker_compose, installed[name]["dir"], "start")
    return {"ok": True}


@router.post("/{name}/stop")
def stop_app(name: str, background_tasks: BackgroundTasks):
    installed = _installed_apps()
    if name not in installed:
        raise HTTPException(404)
    _require_docker()
    background_tasks.add_task(_docker_compose, installed[name]["dir"], "stop")
    return {"ok": True}


@router.post("/{name}/update")
def update_app(name: str, background_tasks: BackgroundTasks):
    installed = _installed_apps()
    if name not in installed:
        raise HTTPException(404)
    _require_docker()
    background_tasks.add_task(_docker_compose, installed[name]["dir"], "pull")
    background_tasks.add_task(
        _docker_compose, installed[name]["dir"], "up", "-d", "--force-recreate"
    )
    return {"ok": True, "message": f"Updating {name}…"}


@router.delete("/{name}")
def remove_app(name: str, purge: bool = False):
    installed = _installed_apps()
    if name not in installed:
        raise HTTPException(404)
    import shutil
    has_docker = bool(shutil.which("docker"))
    if has_docker:
        _docker_compose(installed[name]["dir"], "down", "--remove-orphans")
    # Without docker nothing can be running, so drop the state entry too —
    # otherwise a failed install can never be cleared from the app list.
    if purge or not has_docker:
        shutil.rmtree(installed[name]["dir"], ignore_errors=True)
        return {"ok": True, "message": f"Removed {name} and deleted compose files"}
    return {"ok": True, "message": f"Stopped {name}"}


@router.get("/{name}/logs")
def app_logs(name: str, lines: int = 100):
    installed = _installed_apps()
    if name not in installed:
        raise HTTPException(404)
    rc, out, err = _docker_compose(
        installed[name]["dir"], "logs", "--tail", str(lines), "--no-color"
    )
    return {"lines": out.splitlines(), "error": err if rc != 0 else None}
