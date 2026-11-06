"""
Template source loader — fetches, parses and caches app templates from:

  builtin    — apps/templates/*.json  (our own format, always available)
  portainer  — Portainer v1/v2 JSON template files (URL or local path)
  unraid-ca  — Unraid Community Applications: individual XML templates or the
               full AppFeed (Squidly's aggregated JSON/gz)

All sources are normalised to the same internal dict:
  {name, title, description, category, icon, port, source_id, vars[], compose{}}

A "vars" entry:  {key, label, default, type}
  type: "path" | "port" | "env" | "password"

Caches live at STATE_DIR/template_cache/{source_id}.json, refreshed every 24h.
"""

import gzip
import json
import logging
import os
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from config import STATE_DIR

log = logging.getLogger(__name__)

TEMPLATES_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "../../apps/templates")
)
CACHE_DIR   = os.path.join(STATE_DIR, "template_cache")
SOURCES_CFG = os.path.join(STATE_DIR, "template_sources.json")
CACHE_TTL   = 86400  # 24 h

DEFAULT_SOURCES = [
    {
        "id":      "builtin",
        "name":    "Helix OS Built-in",
        "type":    "builtin",
        "enabled": True,
        "url":     "",
    },
    {
        "id":      "unraid-ca",
        "name":    "Unraid Community Applications",
        "type":    "unraid-ca-feed",
        "url":     "https://raw.githubusercontent.com/Squidly271/AppFeed/master/applicationFeed.json.gz",
        "enabled": True,
        "description": "Full Unraid CA catalog (fetched + cached on first use)",
    },
]


# ── Source config management ──────────────────────────────────────────────────

def load_sources() -> list[dict]:
    Path(STATE_DIR).mkdir(parents=True, exist_ok=True)
    try:
        return json.loads(Path(SOURCES_CFG).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        Path(SOURCES_CFG).write_text(json.dumps(DEFAULT_SOURCES, indent=2))
        return list(DEFAULT_SOURCES)


def save_sources(sources: list[dict]) -> None:
    Path(SOURCES_CFG).write_text(json.dumps(sources, indent=2))


def add_source(src: dict) -> dict:
    sources = load_sources()
    if any(s["id"] == src["id"] for s in sources):
        raise ValueError(f"Source id '{src['id']}' already exists")
    sources.append(src)
    save_sources(sources)
    return src


def remove_source(source_id: str) -> None:
    if source_id == "builtin":
        raise ValueError("Cannot remove the built-in source")
    sources = [s for s in load_sources() if s["id"] != source_id]
    save_sources(sources)
    # Remove cache
    cache = Path(CACHE_DIR) / f"{source_id}.json"
    cache.unlink(missing_ok=True)


# ── Cache helpers ─────────────────────────────────────────────────────────────

def _cache_path(source_id: str) -> Path:
    Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)
    return Path(CACHE_DIR) / f"{source_id}.json"


def _cache_load(source_id: str) -> list[dict] | None:
    p = _cache_path(source_id)
    if not p.exists():
        return None
    if time.time() - p.stat().st_mtime > CACHE_TTL:
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _cache_save(source_id: str, templates: list[dict]) -> None:
    p = _cache_path(source_id)
    p.write_text(json.dumps(templates))


# ── HTTP fetch helper ─────────────────────────────────────────────────────────

def _fetch(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "HelixOS/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ── Built-in loader ───────────────────────────────────────────────────────────

def _load_builtin() -> list[dict]:
    templates = []
    try:
        for f in sorted(Path(TEMPLATES_DIR).glob("*.json")):
            try:
                t = json.loads(f.read_text())
                t.setdefault("source_id", "builtin")
                templates.append(t)
            except Exception:
                pass
    except FileNotFoundError:
        pass
    return templates


# ── Portainer parser ──────────────────────────────────────────────────────────

def _slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", s.lower()).strip("-")


def _extract_port_number(port_str: str) -> int | None:
    """'8096/tcp' → 8096"""
    try:
        return int(str(port_str).split("/")[0])
    except (ValueError, AttributeError):
        return None


def _portainer_to_unified(item: dict, source_id: str) -> dict | None:
    """Convert a Portainer v1/v2 template entry to our unified format."""
    item_type = item.get("type", 1)
    if item_type not in (1, 3):  # 1=container, 3=standalone (skip stacks/swarm)
        return None

    title = item.get("title", "")
    if not title:
        return None

    name   = _slugify(title)
    image  = item.get("image", "")
    if not image:
        return None

    desc  = item.get("description", "")
    cats  = item.get("categories", item.get("category", []))
    cat   = (cats[0] if isinstance(cats, list) and cats else str(cats)).title()
    icon  = item.get("logo", item.get("icon", ""))
    ports = item.get("ports", [])
    vols  = item.get("volumes", [])
    envs  = item.get("env", [])
    net   = item.get("network", "")

    # First port becomes the display port
    port_num = None
    compose_ports = []
    for p in ports:
        ps = str(p)
        n  = _extract_port_number(ps)
        if n:
            compose_ports.append(f"{n}:{n}")
            if port_num is None:
                port_num = n

    # Volumes: use env var KEY for host path so user can override
    compose_vols = []
    vars_list    = []
    for vol in vols:
        container = vol.get("container", vol) if isinstance(vol, dict) else str(vol)
        host      = vol.get("bind", "")
        label     = vol.get("label", f"Path for {container}")
        key       = "VOL_" + re.sub(r"[^A-Z0-9]", "_", container.upper()).strip("_")
        default   = host or f"/mnt/user/appdata/{name}{container.replace('/','_')}"
        vars_list.append({"key": key, "label": label, "default": default, "type": "path"})
        compose_vols.append(f"{{{{{key}}}}}:{container}")

    # Env vars
    compose_env  = {}
    for e in envs:
        if not isinstance(e, dict):
            continue
        ekey   = e.get("name", e.get("variable", ""))
        elabel = e.get("label", ekey)
        edef   = str(e.get("default", e.get("value", "")))
        etype  = "password" if e.get("preset") is False and "password" in elabel.lower() else "env"
        if ekey:
            vars_list.append({"key": ekey, "label": elabel, "default": edef, "type": etype})
            compose_env[ekey] = f"{{{{{ekey}}}}}"

    svc: dict[str, Any] = {"image": image, "container_name": name}
    if compose_ports:
        svc["ports"] = compose_ports
    if compose_vols:
        svc["volumes"] = compose_vols
    if compose_env:
        svc["environment"] = compose_env
    if net == "host":
        svc["network_mode"] = "host"
        svc.pop("ports", None)
    svc["restart"] = "unless-stopped"
    if item.get("privileged"):
        svc["privileged"] = True

    return {
        "name":      name,
        "title":     title,
        "description": desc,
        "category":  cat,
        "icon":      icon,
        "port":      port_num,
        "source_id": source_id,
        "vars":      vars_list,
        "compose":   {"services": {name: svc}},
    }


def _load_portainer(source: dict) -> list[dict]:
    cached = _cache_load(source["id"])
    if cached is not None:
        return cached

    url = source.get("url", "")
    if not url:
        return []

    try:
        raw = _fetch(url)
        data = json.loads(raw)
    except Exception as e:
        log.warning("Failed to fetch Portainer templates from %s: %s", url, e)
        return []

    # Support v1 (list), v2 ({version, templates:[]}), and plain list
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("templates", data.get("apps", []))
    else:
        items = []

    templates = []
    for item in items:
        t = _portainer_to_unified(item, source["id"])
        if t:
            templates.append(t)

    _cache_save(source["id"], templates)
    return templates


# ── Unraid XML parser ─────────────────────────────────────────────────────────

def _parse_unraid_xml(xml_text: str, source_id: str) -> dict | None:
    """Parse a single Unraid CA XML template string into unified format."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    def tag(name: str) -> str:
        el = root.find(name)
        return el.text.strip() if el is not None and el.text else ""

    title = tag("Name")
    if not title:
        return None

    name        = _slugify(title)
    image       = tag("Repository")
    description = tag("Overview") or tag("Description")
    category_raw = tag("Category")
    # "MediaServer: Video Music" → "Media"
    cat_map = {
        "mediaserver": "Media", "downloader": "Downloaders",
        "cloud": "Cloud", "homeautomation": "Home Automation",
        "tools": "Tools", "other": "Other", "backup": "Backup",
        "network": "Network", "security": "Security",
    }
    cat = "Other"
    for key, val in cat_map.items():
        if key in category_raw.lower().replace(" ", ""):
            cat = val
            break

    icon   = tag("Icon")
    net    = tag("Network").lower()
    webui  = tag("WebUI")
    # Extract port from [PORT:8096]
    pm = re.search(r"\[PORT:(\d+)\]", webui)
    port_num = int(pm.group(1)) if pm else None

    vars_list    = []
    compose_vols = []
    compose_ports = []
    compose_env  = {}

    for cfg in root.findall("Config"):
        cfg_type    = cfg.get("Type", "").lower()
        cfg_name    = cfg.get("Name", "")
        cfg_target  = cfg.get("Target", "")
        cfg_default = cfg.get("Default", cfg.text or "")
        cfg_mask    = cfg.get("Mask", "false").lower() == "true"

        if cfg_type == "path":
            key     = "PATH_" + re.sub(r"[^A-Z0-9]", "_", cfg_target.upper()).strip("_")
            default = cfg_default or f"/mnt/user/appdata/{name}"
            vars_list.append({"key": key, "label": cfg_name, "default": default, "type": "path"})
            compose_vols.append(f"{{{{{key}}}}}:{cfg_target}")

        elif cfg_type == "port":
            try:
                p = int(cfg_target)
                compose_ports.append(f"{p}:{p}")
                if port_num is None:
                    port_num = p
            except ValueError:
                pass

        elif cfg_type in ("variable", "env"):
            key     = cfg_target or re.sub(r"\s+", "_", cfg_name.upper())
            default = cfg_default
            vtype   = "password" if cfg_mask else "env"
            vars_list.append({"key": key, "label": cfg_name, "default": default, "type": vtype})
            compose_env[key] = f"{{{{{key}}}}}"

    if not image:
        return None

    svc: dict[str, Any] = {"image": image, "container_name": name}
    if net == "host":
        svc["network_mode"] = "host"
    elif compose_ports:
        svc["ports"] = compose_ports
    if compose_vols:
        svc["volumes"] = compose_vols
    if compose_env:
        svc["environment"] = compose_env
    if tag("Privileged").lower() == "true":
        svc["privileged"] = True
    svc["restart"] = "unless-stopped"

    return {
        "name":        name,
        "title":       title,
        "description": description[:300],
        "category":    cat,
        "icon":        icon,
        "port":        port_num,
        "source_id":   source_id,
        "vars":        vars_list,
        "compose":     {"services": {name: svc}},
    }


def _load_unraid_xml_url(source: dict) -> list[dict]:
    """Load a single XML template from a URL."""
    cached = _cache_load(source["id"])
    if cached is not None:
        return cached

    try:
        raw  = _fetch(source["url"])
        t    = _parse_unraid_xml(raw.decode("utf-8", errors="replace"), source["id"])
        result = [t] if t else []
    except Exception as e:
        log.warning("Failed to fetch XML template %s: %s", source["url"], e)
        result = []

    _cache_save(source["id"], result)
    return result


def _load_unraid_ca_feed(source: dict) -> list[dict]:
    """Load the full Unraid AppFeed (compressed JSON)."""
    cached = _cache_load(source["id"])
    if cached is not None:
        return cached

    url = source.get("url", "")
    if not url:
        return []

    log.info("Fetching Unraid CA AppFeed from %s …", url)
    try:
        raw = _fetch(url, timeout=60)
        if url.endswith(".gz"):
            raw = gzip.decompress(raw)
        feed = json.loads(raw)
    except Exception as e:
        log.warning("Failed to fetch CA AppFeed: %s", e)
        return []

    # Feed structure: {"Apps": [...]} or {"applist": {"apps": [...]}}
    apps = []
    if "Apps" in feed:
        apps = feed["Apps"]
    elif "applist" in feed:
        apps = feed["applist"].get("apps", feed["applist"])
    elif isinstance(feed, list):
        apps = feed

    templates = []
    seen = set()
    for app in apps:
        if not isinstance(app, dict):
            continue
        # Skip apps without a Repository (can't install)
        if not app.get("Repository"):
            continue

        title = app.get("Name", "")
        if not title:
            continue

        name = _slugify(title)
        if name in seen:
            name = name + "-" + _slugify(app.get("Author", ""))[:8]
        seen.add(name)

        image   = app.get("Repository", "")
        desc    = app.get("Overview", "")[:300]
        cat_raw = app.get("Category", "")
        icon    = app.get("Icon", "")
        webui   = app.get("WebUI", "")
        net     = app.get("Network", "bridge").lower()

        pm = re.search(r"\[PORT:(\d+)\]", webui)
        port_num = int(pm.group(1)) if pm else None

        cat_map = {
            "mediaserver": "Media", "downloader": "Downloaders",
            "cloud": "Cloud", "homeautomation": "Home Automation",
            "tools": "Tools", "backup": "Backup",
            "network": "Network", "security": "Security",
        }
        cat = "Other"
        for key, val in cat_map.items():
            if key in cat_raw.lower().replace(" ", ""):
                cat = val
                break

        # Config items — may be list or single dict
        cfgs = app.get("Config", [])
        if isinstance(cfgs, dict):
            cfgs = [cfgs]

        vars_list     = []
        compose_vols  = []
        compose_ports = []
        compose_env   = {}

        for cfg in cfgs:
            if isinstance(cfg, dict) and "@attributes" in cfg:
                cfg = cfg["@attributes"]  # some feed formats wrap in @attributes

            cfg_type   = (cfg.get("Type") or cfg.get("@Type") or "").lower()
            cfg_name   = cfg.get("Name")  or cfg.get("@Name")  or ""
            cfg_target = cfg.get("Target") or cfg.get("@Target") or ""
            cfg_def    = cfg.get("Default") or cfg.get("@Default") or ""
            cfg_mask   = str(cfg.get("Mask") or cfg.get("@Mask") or "false").lower() == "true"

            if cfg_type == "path":
                key = "PATH_" + re.sub(r"[^A-Z0-9]", "_", cfg_target.upper()).strip("_") or "PATH_DATA"
                default = cfg_def or f"/mnt/user/appdata/{name}"
                vars_list.append({"key": key, "label": cfg_name, "default": default, "type": "path"})
                compose_vols.append(f"{{{{{key}}}}}:{cfg_target}")

            elif cfg_type == "port":
                try:
                    p = int(str(cfg_target).split("/")[0])
                    compose_ports.append(f"{p}:{p}")
                    if port_num is None:
                        port_num = p
                except ValueError:
                    pass

            elif cfg_type in ("variable", "env"):
                key = cfg_target or re.sub(r"\s+", "_", cfg_name.upper())
                if key:
                    vtype = "password" if cfg_mask else "env"
                    vars_list.append({"key": key, "label": cfg_name, "default": cfg_def, "type": vtype})
                    compose_env[key] = f"{{{{{key}}}}}"

        if not image:
            continue

        svc: dict[str, Any] = {"image": image, "container_name": name}
        if net == "host":
            svc["network_mode"] = "host"
        elif compose_ports:
            svc["ports"] = compose_ports
        if compose_vols:
            svc["volumes"] = compose_vols
        if compose_env:
            svc["environment"] = compose_env
        if str(app.get("Privileged", "false")).lower() == "true":
            svc["privileged"] = True
        svc["restart"] = "unless-stopped"

        templates.append({
            "name":        name,
            "title":       title,
            "description": desc,
            "category":    cat,
            "icon":        icon,
            "port":        port_num,
            "source_id":   source["id"],
            "vars":        vars_list,
            "compose":     {"services": {name: svc}},
        })

    log.info("Loaded %d apps from Unraid CA feed", len(templates))
    _cache_save(source["id"], templates)
    return templates


# ── Main loader ───────────────────────────────────────────────────────────────

def load_all_templates(source_filter: str | None = None) -> list[dict]:
    """Return merged, deduped template list from all enabled sources."""
    sources = load_sources()
    all_templates: list[dict] = []
    seen_names: set[str] = set()

    for src in sources:
        if not src.get("enabled", True):
            continue
        if source_filter and src["id"] != source_filter:
            continue

        src_type = src["type"]
        try:
            if src_type == "builtin":
                items = _load_builtin()
            elif src_type == "portainer":
                items = _load_portainer(src)
            elif src_type == "unraid-xml":
                items = _load_unraid_xml_url(src)
            elif src_type == "unraid-ca-feed":
                items = _load_unraid_ca_feed(src)
            else:
                items = []
        except Exception as e:
            log.warning("Error loading source %s: %s", src["id"], e)
            items = []

        for t in items:
            # Deduplicate by name: built-in wins over external
            n = t.get("name", "")
            if n in seen_names:
                continue
            seen_names.add(n)
            all_templates.append(t)

    return all_templates


def invalidate_cache(source_id: str) -> None:
    p = _cache_path(source_id)
    p.unlink(missing_ok=True)
