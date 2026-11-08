"""
Apps router — template catalog + source management + install/remove lifecycle.

Template sources (GET /api/apps/sources):
  builtin      — apps/templates/*.json
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

from config import STATE_DIR
from services.template_loader import (
    add_source,
    invalidate_cache,
    load_all_templates,
    load_sources,
    remove_source,
    save_sources,
    _slugify,
)

APPS_STATE_DIR = os.path.join(STATE_DIR, "apps")

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
    try:
        add_source(src)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return src


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

    return templates


@router.get("/templates/{name}")
def get_template(name: str):
    templates = load_all_templates()
    for t in templates:
        if t.get("name") == name:
            return t
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


def _write_compose(app_dir: Path, template: dict, vars_map: dict) -> Path:
    compose = _substitute_vars(template["compose"], vars_map)
    if "version" not in compose:
        compose = {"version": "3.8", **compose}
    dest = app_dir / "docker-compose.yml"
    dest.write_text(yaml.dump(compose, default_flow_style=False, allow_unicode=True))
    return dest


def _docker_compose(app_dir: str | Path, *args, timeout: int = 60) -> tuple[int, str, str]:
    r = subprocess.run(
        ["docker", "compose", *args],
        capture_output=True, text=True,
        cwd=str(app_dir), timeout=timeout
    )
    return r.returncode, r.stdout, r.stderr


def _set_install_status(name: str, state: str, error: str = "") -> None:
    status_file = Path(APPS_STATE_DIR) / name / "install_status.json"
    status_file.parent.mkdir(parents=True, exist_ok=True)
    status_file.write_text(json.dumps({"state": state, "error": error}))


def _do_install(name: str, vars_map: dict) -> None:
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

        # Ensure required host dirs
        for v_def in t.get("vars", []):
            if v_def.get("type") == "path":
                val = final_vars.get(v_def["key"], "")
                if val:
                    Path(val).mkdir(parents=True, exist_ok=True)

        _write_compose(app_dir, t, final_vars)
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


@router.post("/install")
def install_app(req: InstallRequest, background_tasks: BackgroundTasks):
    templates = {t["name"]: t for t in load_all_templates()}
    if req.name not in templates:
        raise HTTPException(404, f"Template '{req.name}' not found")
    if req.name in _installed_apps():
        raise HTTPException(409, f"'{req.name}' is already installed")
    background_tasks.add_task(_do_install, req.name, req.vars)
    return {"ok": True, "message": f"Installing {templates[req.name]['title']}…"}


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
    background_tasks.add_task(_docker_compose, installed[name]["dir"], "start")
    return {"ok": True}


@router.post("/{name}/stop")
def stop_app(name: str, background_tasks: BackgroundTasks):
    installed = _installed_apps()
    if name not in installed:
        raise HTTPException(404)
    background_tasks.add_task(_docker_compose, installed[name]["dir"], "stop")
    return {"ok": True}


@router.post("/{name}/update")
def update_app(name: str, background_tasks: BackgroundTasks):
    installed = _installed_apps()
    if name not in installed:
        raise HTTPException(404)
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
    _docker_compose(installed[name]["dir"], "down", "--remove-orphans")
    if purge:
        import shutil
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
