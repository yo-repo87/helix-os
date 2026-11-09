"""Map Pi-Hosted/Portainer host paths onto Helix shares (single source of truth).

Used by build/tools/convert-portainer-catalog.py (to generate the bundled catalog) and by
the template loader (so a Pi-Hosted-style catalog added as a custom source gets the same
folders). See that script's docstring for the rules.
"""
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
        if sub[:1] == ["Downloads"]:                       # /portainer/Files/Downloads is a download folder, not app data
            return "/".join([DOWNLOADS] + sub[1:]) + suffix
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
