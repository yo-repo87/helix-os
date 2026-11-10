"""Read and update disks.cfg without dropping sections the caller did not touch.

The file is INI-like:

    [disk1]
    device=/dev/disk/by-id/ata-...
    format=xfs

`format` is the file system to create later. It is not a sign that the disk
has already been formatted.
"""

import re

_SECTION = re.compile(r"^\[(\w+)\]\s*$")
_KV = re.compile(r"^([A-Za-z0-9_]+)=(.*)$")


def parse_sections(text: str) -> list[tuple[str, list[tuple[str, str]]]]:
    """Ordered (section, [(key, value), ...]) pairs. Blank lines and comments are dropped."""
    sections: list[tuple[str, list[tuple[str, str]]]] = []
    current: str | None = None
    items: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _SECTION.match(line)
        if m:
            if current is not None:
                sections.append((current, items))
            current = m.group(1)
            items = []
            continue
        kv = _KV.match(line)
        if current is not None and kv:
            items.append((kv.group(1), kv.group(2).strip()))
    if current is not None:
        sections.append((current, items))
    return sections


def render(sections: list[tuple[str, list[tuple[str, str]]]]) -> str:
    lines = ["# Helix OS — Disk Assignment (managed by the web UI)", ""]
    for name, items in sections:
        lines.append(f"[{name}]")
        for key, value in items:
            lines.append(f"{key}={value}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def rewrite_key(text: str, section: str, key: str, value: str) -> str | None:
    """Set one key inside one section. Returns None when that section does not exist."""
    sections = parse_sections(text)
    found = False
    out: list[tuple[str, list[tuple[str, str]]]] = []
    for name, items in sections:
        if name != section:
            out.append((name, items))
            continue
        found = True
        replaced = False
        new_items = []
        for k, v in items:
            if k == key:
                new_items.append((k, value))
                replaced = True
            else:
                new_items.append((k, v))
        if not replaced:
            new_items.append((key, value))
        out.append((name, new_items))
    if not found:
        return None
    return render(out)


def formats_by_device(text: str) -> dict[str, str]:
    """Stored device path → format= value. Paths are not resolved here."""
    found: dict[str, str] = {}
    for _name, items in parse_sections(text):
        device = ""
        fmt = ""
        for k, v in items:
            if k == "device":
                device = v
            elif k == "format":
                fmt = v
        if device and fmt:
            found[device] = fmt
    return found
