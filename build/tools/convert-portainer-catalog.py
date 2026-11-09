#!/usr/bin/env python3
"""Rewrite a Portainer app catalog (Pi-Hosted) so its host paths fit Helix OS.

The Pi-Hosted catalog assumes a Raspberry Pi layout: everything under /portainer/...
Helix keeps app settings in the `appdata` share and media/downloads in ordinary
shares of /mnt/user, so each host path is mapped as below. System paths such as
/etc/localtime, /var/run/docker.sock and /dev/* are left exactly as they are, and
named Docker volumes are untouched. Container-side paths are left alone, with one deliberate
exception: an app that mounts a folder at the SAME path inside and outside the container
(Dockge's stacks dir) has both sides and its env var changed together so they still match.

  /portainer/Files/AppData/Config/<x>   -> /mnt/user/appdata/<x>
  /portainer/Files/AppData/<x>          -> /mnt/user/appdata/<x>
  /portainer/Files/Config/<x>           -> /mnt/user/appdata/<x>
  /portainer/Files/<x>                  -> /mnt/user/appdata/<x>
  /portainer/Downloads[/<x>]            -> /mnt/user/downloads[/<x>]
  /portainer/{TV,Movies,Music,Podcasts,Books,Comics,Pictures}[/<x>]
                                        -> /mnt/user/media/<Name>[/<x>]
  /mnt/Books                            -> /mnt/user/media/Books

Usage: convert-portainer-catalog.py [--url URL | --in FILE] --out FILE
"""
import argparse, json, re, sys, urllib.request

DEFAULT_URL = "https://raw.githubusercontent.com/yo-repo87/pi-hosted/master/template/portainer-v3-amd64.json"
MEDIA = {"tv": "TV", "movies": "Movies", "music": "Music", "podcasts": "Podcasts",
         "books": "Books", "comics": "Comics", "pictures": "Pictures"}
APPDATA, MEDIA_SHARE, DOWNLOADS = "/mnt/user/appdata", "/mnt/user/media", "/mnt/user/downloads"


def map_path(p: str) -> str:
    """Map one host path. Anything that is not a Pi-Hosted path is returned unchanged."""
    suffix = ""
    if p.endswith(":ro") or p.endswith(":rw"):
        p, suffix = p[:-3], p[-3:]
    if p == "/mnt/Books" or p.startswith("/mnt/Books/"):
        return MEDIA_SHARE + "/Books" + p[len("/mnt/Books"):] + suffix
    if not p.startswith("/portainer"):
        return p + suffix
    rest = p[len("/portainer"):].lstrip("/")
    parts = rest.split("/") if rest else []
    if not parts:
        return APPDATA + suffix
    head = parts[0].lower()
    if head == "files":
        sub = parts[1:]
        if sub[:2] == ["AppData", "Config"]:
            sub = sub[2:]
        elif sub[:1] == ["AppData"] or sub[:1] == ["Config"]:
            sub = sub[1:]
        return "/".join([APPDATA] + sub) + suffix
    if head == "downloads":
        return "/".join([DOWNLOADS] + parts[1:]) + suffix
    if head in MEDIA:
        return "/".join([MEDIA_SHARE, MEDIA[head]] + parts[1:]) + suffix
    return "/".join([APPDATA] + parts) + suffix           # unknown /portainer/<x>: keep it, under appdata


_TEXT_PATH = re.compile(r"/portainer(?:/[^\s\"'<>),;]*)?")


def convert(obj):
    """Walk the catalog. Host paths live in volumes[].bind and a few env defaults; free
    text (notes, descriptions) that mentions /portainer/... is rewritten too."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "bind" and isinstance(v, str) and v.startswith("/"):
                out[k] = map_path(v)
            else:
                out[k] = convert(v)
        return out
    if isinstance(obj, list):
        return [convert(x) for x in obj]
    if isinstance(obj, str) and "/portainer" in obj:
        return _TEXT_PATH.sub(lambda m: map_path(m.group(0)), obj)
    if isinstance(obj, str) and (obj == "/mnt/Books"):
        return map_path(obj)
    return obj


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=DEFAULT_URL); ap.add_argument("--in", dest="infile"); ap.add_argument("--out", required=True)
    a = ap.parse_args()
    raw = open(a.infile, "rb").read() if a.infile else urllib.request.urlopen(
        urllib.request.Request(a.url, headers={"User-Agent": "HelixOS-catalog-converter/1"}), timeout=60).read()
    src = json.loads(raw)
    out = convert(src)
    out["_helix"] = {"converted_from": a.infile or a.url,
                     "note": "Host paths rewritten for Helix OS by build/tools/convert-portainer-catalog.py"}
    json.dump(out, open(a.out, "w"), indent=1, ensure_ascii=False)
    left = json.dumps(out).count("/portainer")
    print(f"wrote {a.out}: {len(out.get('templates', []))} templates, '/portainer' occurrences left: {left}")
    sys.exit(0 if left == 0 else 1)


if __name__ == "__main__":
    main()
