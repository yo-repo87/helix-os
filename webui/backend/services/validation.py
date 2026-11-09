"""Input rules, following Unraid's documented limits (CLAUDE.md §30.14).

Each function returns an error message, or "" when the value is acceptable. The same
rules are mirrored in the UI (VALID in index.html) so a form can warn while typing;
the server stays the authority.
"""
import re

# Names that would collide with Samba's special sections ([global], [homes], [printers]),
# with Helix's own directories/mount points, or with ZFS/pool vocabulary.
SHARE_RESERVED = {
    "homes", "global", "printers", "flash", "boot", "user", "user0", "dev", "remotes",
    "recyclebin", "rootshare", "addons", "mirror", "draid", "spare", "log",
}
_SHARE_RESERVED_RE = re.compile(r"^(disk|parity|raidz|cache)\d*$", re.I)
_SHARE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]*$")
_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]*\$?$")


def share_name(name: str, existing=()) -> str:
    if not name:
        return "Share name is required."
    if len(name) > 40:
        return "Share name can be at most 40 characters."
    if not name[0].isalpha() or not name[0].isascii():
        return "Share name must start with a letter."
    if not _SHARE_RE.match(name):
        return "Share name may only contain letters, digits, '.', '_' and '-'."
    if name.endswith("."):
        return "Share name cannot end with a period."
    if name.lower() in SHARE_RESERVED or _SHARE_RESERVED_RE.match(name):
        return f"'{name}' is a reserved name."
    if any(e.lower() == name.lower() for e in existing):
        return f"A share named '{name}' already exists (names are not case-sensitive for SMB clients)."
    return ""


def share_comment(text: str) -> str:
    return "Comment can be at most 256 characters." if len(text or "") > 256 else ""


def hostname(name: str) -> str:
    if not name:
        return "Server name is required."
    if len(name) > 15:
        return "Server name can be at most 15 characters (NetBIOS limit)."
    if not _HOST_RE.match(name):
        return "Server name may contain letters, digits, '-' and '.', and must start and end with a letter or digit."
    return ""


def username(name: str) -> str:
    if not name:
        return "User name is required."
    if len(name) > 40:
        return "User name can be at most 40 characters."
    if not _USER_RE.match(name):
        return "User name must start with a lowercase letter or '_', then lowercase letters, digits, '_' or '-' (may end in '$')."
    return ""


def user_description(text: str) -> str:
    if len(text or "") > 64:
        return "Description can be at most 64 characters."
    if re.search(r'[&":]', text or ""):
        return "Description cannot contain '&', '\"' or ':'."
    return ""


def password(text: str) -> str:
    return "Password can be at most 128 characters." if len(text or "") > 128 else ""
