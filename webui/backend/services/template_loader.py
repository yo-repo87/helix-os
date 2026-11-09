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

from config import STATE_DIR, HELIX_CONFIG
from services import pathmap

log = logging.getLogger(__name__)

TEMPLATES_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "../../apps/templates")
)
CACHE_DIR   = os.path.join(STATE_DIR, "template_cache")
# Persistent (/boot/config): custom sources were in RAM and vanished on every reboot.
SOURCES_CFG = os.path.join(HELIX_CONFIG, "template_sources.json")
SOURCES_SEEN = os.path.join(HELIX_CONFIG, "template_sources.seen")   # default sources already offered once
CATALOG_DIR = Path(__file__).resolve().parent.parent / "data" / "catalogs"
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
        "url":     "https://raw.githubusercontent.com/Squidly271/AppFeed/master/applicationFeed.json",
        "enabled": True,
        "description": "Full Unraid CA catalog (fetched + cached on first use)",
    },
    {
        "id":      "pi-hosted",
        "name":    "Pi-Hosted",
        "type":    "portainer",
        # bundled with the OS: the Pi-Hosted catalog with its paths mapped to Helix shares
        # (appdata / downloads / media) by build/tools/convert-portainer-catalog.py
        "url":     "bundled:pi-hosted-helix.json",
        "enabled": True,
        "description": "Pi-Hosted app catalog with folders mapped to Helix shares",
    },
]


# ── Source config management ──────────────────────────────────────────────────

_LEGACY_DEFAULTS = ("builtin", "unraid-ca")      # the default sources before "seen" tracking existed


def load_sources() -> list[dict]:
    Path(SOURCES_CFG).parent.mkdir(parents=True, exist_ok=True)
    try:
        sources = json.loads(Path(SOURCES_CFG).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        sources = [dict(d) for d in DEFAULT_SOURCES]
        Path(SOURCES_CFG).write_text(json.dumps(sources, indent=2))
        _remember_defaults([d["id"] for d in DEFAULT_SOURCES])
        return sources
    # A default source added in a later release must appear on systems that already have a
    # saved list — but only once: if the user removed it, it must stay removed.
    try:
        seen = set(json.loads(Path(SOURCES_SEEN).read_text()))
    except (OSError, ValueError):
        # First run of this logic on an existing system: the defaults that existed BEFORE this
        # feature count as already offered, so one the user removed earlier is not resurrected,
        # while newer defaults (pi-hosted) are added.
        seen = set(_LEGACY_DEFAULTS) | {s["id"] for s in sources}
    have = {s["id"] for s in sources}
    new = [dict(d) for d in DEFAULT_SOURCES if d["id"] not in have and d["id"] not in seen]
    if new:
        sources += new
        Path(SOURCES_CFG).write_text(json.dumps(sources, indent=2))
    _remember_defaults(list(seen | have | {d["id"] for d in DEFAULT_SOURCES}))
    return sources


def _remember_defaults(ids) -> None:
    try:
        Path(SOURCES_SEEN).write_text(json.dumps(sorted(set(ids))))
    except OSError:
        pass


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
    """'8096/tcp' → 8096; '5006:5006/tcp' → 5006 (host port of a host:container pair)"""
    try:
        return int(str(port_str).split("/")[0].split(":")[0])
    except (ValueError, AttributeError):
        return None


def _localize_host_path(host: str) -> str:
    """Pi-Hosted templates bind to '/portainer/...' (a Raspberry Pi layout). Map those onto Helix
    shares (appdata / downloads / media); every other path, including system paths, is unchanged.
    The bundled catalog is already converted; this covers Pi-Hosted-style catalogs added later."""
    return pathmap.map_path(host) if host else host


def _portainer_to_unified(item: dict, source_id: str) -> dict | None:
    """Convert a Portainer v1/v2 template entry to our unified format."""
    item_type = item.get("type", 1)
    # 1=container. Type 3 is normally a compose *stack* (deployed from a git
    # repository, no single image) which this loader cannot build yet, so those
    # fall out below when they carry no image; type 2 is swarm.
    if item_type not in (1, 3):
        return None

    title = item.get("title") or ""        # a JSON null is present-but-empty: .get(k, "") would keep None
    if not title:
        return None

    name   = _slugify(title)
    image  = item.get("image", "")
    if not image:
        return None

    desc  = item.get("description") or ""
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
        default   = _localize_host_path(host) or f"/mnt/user/appdata/{name}{container.replace('/','_')}"
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
        entry  = {"key": ekey, "label": elabel, "default": edef, "type": etype}
        sel = e.get("select")
        if isinstance(sel, list) and sel:               # Portainer "select": [{text, value, default?}]
            vals = [str(o.get("value", "")) for o in sel if isinstance(o, dict)]
            if vals:
                entry["choices"] = vals
                entry["default"] = next((str(o.get("value", "")) for o in sel
                                         if isinstance(o, dict) and o.get("default")), vals[0])
        if ekey:
            vars_list.append(entry)
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
        if url.startswith("bundled:"):                    # shipped inside the OS image
            name = os.path.basename(url[len("bundled:"):])        # no path traversal
            raw = (CATALOG_DIR / name).read_bytes()
        elif url.startswith("/"):                         # a file on this machine
            raw = Path(url).read_bytes()
        else:
            raw = _fetch(url)
        data = json.loads(raw)
    except Exception as e:
        log.warning("Failed to load Portainer templates from %s: %s", url, e)
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


_CA_CATEGORY = {
    "mediaapp": "Media", "mediaserver": "Media", "downloaders": "Downloaders", "cloud": "Cloud",
    "homeautomation": "Home Automation", "tools": "Tools", "backup": "Backup", "network": "Network",
    "security": "Security", "productivity": "Productivity", "games": "Games", "gameservers": "Games",
    "other": "Other", "drivers": "Other", "status": "Monitoring", "management": "Management",
}


def _ca_category(cats) -> str:
    """CategoryList looks like ['MediaApp-Video', 'Tools:'] — map the first usable prefix."""
    for c in (cats or []):
        prefix = re.split(r"[-:]", str(c))[0].strip().lower()
        if prefix in _CA_CATEGORY:
            return _CA_CATEGORY[prefix]
        if prefix:
            return prefix.title()
    return "Other"


def _plain(text: str, limit: int = 300) -> str:
    """Strip HTML/whitespace from a feed description."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()[:limit]


def _ca_app_to_template(app: dict, name: str, source_id: str) -> dict | None:
    """Convert one Unraid CA container entry to our template format.

    Config items are {"@attributes": {Name, Target, Default, Mode, Description,
    Type: Port|Path|Variable|Device|Label, Required, Mask}}. For a Port, Target is
    the CONTAINER port and Default the HOST port.
    """
    image = app.get("Repository") or ""
    if not image:
        return None
    net = str(app.get("Network") or "bridge").lower()

    cfgs = app.get("Config") or []
    if isinstance(cfgs, dict):
        cfgs = [cfgs]

    vars_list, vols, ports, devices, env = [], [], [], [], {}
    port_num = None
    pm = re.search(r"\[PORT:(\d+)\]", app.get("WebUI") or "")
    web_container_port = int(pm.group(1)) if pm else None
    taken = set()

    def add_var(base: str, v: dict) -> str:
        key, n = base, 2
        while key in taken:                       # two config items can share a target
            key, n = f"{base}_{n}", n + 1
        taken.add(key); v["key"] = key; vars_list.append(v)
        return key

    for item in cfgs:
        if not isinstance(item, dict):
            continue
        cfg = item.get("@attributes", item)
        ctype  = str(cfg.get("Type") or "").lower()
        label  = cfg.get("Name") or cfg.get("Target") or ""
        target = str(cfg.get("Target") or "")
        default = str(cfg.get("Default") or item.get("value") or "")
        desc   = _plain(cfg.get("Description") or "")
        req    = str(cfg.get("Required") or "").lower() == "true"
        mask   = str(cfg.get("Mask") or "").lower() == "true"
        base   = {"label": label, "default": default, "description": desc, "required": req}

        if ctype == "path" and target.startswith("/"):
            key = add_var("PATH_" + (re.sub(r"[^A-Z0-9]", "_", target.upper()).strip("_") or "DATA"),
                          {**base, "type": "path", "default": default or f"/mnt/user/appdata/{name}"})
            ro = str(cfg.get("Mode") or "").lower().startswith("ro")
            vols.append(f"{{{{{key}}}}}:{target}" + (":ro" if ro else ""))
        elif ctype == "port" and target.isdigit():
            proto = "udp" if str(cfg.get("Mode") or "").lower() == "udp" else "tcp"
            host = default if default.isdigit() else target
            key = add_var(f"PORT_{target}_{proto.upper()}", {**base, "type": "port", "default": host})
            ports.append(f"{{{{{key}}}}}:{target}" + ("/udp" if proto == "udp" else ""))
            if port_num is None or (web_container_port and int(target) == web_container_port):
                port_num = int(host)
        elif ctype == "device" and target.startswith("/"):
            key = add_var("DEVICE_" + (re.sub(r"[^A-Z0-9]", "_", target.upper()).strip("_") or "DEV"),
                          {**base, "type": "device", "default": default or target})
            devices.append(f"{{{{{key}}}}}:{target}")
        elif ctype in ("variable", "env") and target:
            # Mask secrets even when the template author forgot to (tokens, keys, passwords).
            secret = mask or bool(re.search(r"PASS|SECRET|TOKEN|API_?KEY|PRIVATE", target.upper()))
            var = {**base, "type": "password" if secret else "env"}
            # Unraid convention: Default="a|b|c" is a dropdown of choices (the current
            # value, else the first, is selected) — NOT the literal string "a|b|c".
            parts = [x.strip() for x in default.split("|")]
            # An empty entry (e.g. "|oidc|ldap") is a legitimate "unset" choice.
            if len(parts) > 1 and any(parts) and " | " not in default:     # " | " = prose, not a list
                cur = str(item.get("value") or "").strip()
                var["choices"] = parts
                var["default"] = cur if cur in parts else parts[0]
            key = add_var(target, var)
            env[target] = f"{{{{{key}}}}}"
        # Label / unknown types are Unraid UI metadata only

    svc: dict[str, Any] = {"image": image, "container_name": name, "restart": "unless-stopped"}
    if net == "host":
        svc["network_mode"] = "host"
    elif ports:
        svc["ports"] = ports
    if vols:
        svc["volumes"] = vols
    if devices:
        svc["devices"] = devices
    if env:
        svc["environment"] = env
    if str(app.get("Privileged") or "").lower() == "true":
        svc["privileged"] = True

    return {
        "name": name, "title": app.get("Name", name), "description": _plain(app.get("Overview", "")),
        "category": _ca_category(app.get("CategoryList")), "icon": app.get("Icon", ""),
        "port": port_num, "source_id": source_id, "vars": vars_list,
        "compose": {"services": {name: svc}},
    }


def _load_unraid_ca_feed(source: dict) -> list[dict]:
    """Load the Unraid Community Applications feed (container apps only)."""
    cached = _cache_load(source["id"])
    if cached is not None:
        return cached

    url = source.get("url", "")
    if not url:
        return []
    # The feed moved: the old .json.gz URL now 404s, the plain .json is current.
    url = url.replace("applicationFeed.json.gz", "applicationFeed.json")

    log.info("Fetching Unraid CA AppFeed from %s …", url)
    try:
        raw = _fetch(url, timeout=120)
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        feed = json.loads(raw)
    except Exception as e:
        log.warning("Failed to fetch CA AppFeed: %s", e)
        return []
    del raw

    # {"applist": [ ... ]}, or {"Apps": [...]} / a bare list in older layouts
    if isinstance(feed, list):
        apps = feed
    else:
        apps = feed.get("applist") or feed.get("Apps") or []
        if isinstance(apps, dict):
            apps = apps.get("apps", [])

    templates, seen = [], set()
    for app in apps:
        if not isinstance(app, dict) or not app.get("Repository") or not app.get("Name"):
            continue                       # plugins/language packs have no Repository
        name = _slugify(app["Name"])
        if not name:
            continue
        if name in seen:                   # many authors publish an app with the same name
            name = f"{name}-{_slugify(app.get('Repo') or app.get('Author') or '')[:16]}".rstrip("-")
        base, n = name, 2
        while name in seen:
            name, n = f"{base}-{n}", n + 1
        t = _ca_app_to_template(app, name, source["id"])
        if t:
            seen.add(name)
            templates.append(t)

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
            n = t.get("name", "")
            if n in seen_names:
                # Same app name in another source. Dropping it silently meant the big
                # Unraid CA catalog hid every same-named app from a source the user added
                # on purpose. Keep it, under a distinct name, labelled with its source.
                import copy
                t = copy.deepcopy(t)
                new = f"{n}-{src['id']}"
                for svc in (t.get("compose", {}).get("services", {}) or {}).values():
                    if svc.get("container_name") == n:
                        svc["container_name"] = new
                svcs = t.get("compose", {}).get("services")
                if svcs and n in svcs:
                    t["compose"]["services"] = {(new if k == n else k): v for k, v in svcs.items()}
                t["name"], t["title"] = new, f"{t.get('title', n)} ({src.get('name', src['id'])})"
                n = new
                if n in seen_names:
                    continue
            seen_names.add(n)
            all_templates.append(t)

    return all_templates


def invalidate_cache(source_id: str) -> None:
    p = _cache_path(source_id)
    p.unlink(missing_ok=True)
