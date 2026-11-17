"""Stable array slots for POST /api/array/assign (CLAUDE.md §55).

A disk's slot (disk1, disk2, parity1, cache1 ...) is its mount point (/mnt/<slot>) and the name
every path, share and app setting uses. Slots used to be numbered by the order of the request
list, which the UI builds in device-name order, so adding /dev/sda moved every existing data disk
up one slot (2026-11-17: disk1 → disk2 → disk3 while the array was running).

Rules, independent of the order the client sends:
  - a disk that keeps its role keeps its slot number;
  - a new disk (or one that changed role) gets the next number above the highest one kept in
    that role; gaps left by removed disks stay gaps (mount points come from slot names, so a
    gap is harmless);
  - an assigned disk that is NOT connected and NOT named in `remove_missing` stays assigned in
    its slot (the UI only lists connected disks, so leaving it out is not a decision to remove it).

plan() is pure; the router supplies the current assignment, the request, and two callables.
"""
from __future__ import annotations

import re

ROLES = (("parity", "parity"), ("data", "disk"), ("cache", "cache"))   # role -> slot prefix
_SLOT = re.compile(r"^(parity|disk|cache)([0-9]+)$")


def role_of(slot: str) -> str | None:
    m = _SLOT.match(slot)
    if not m:
        return None
    return {"parity": "parity", "disk": "data", "cache": "cache"}[m.group(1)]


def plan(current: list[tuple[str, str]], requested: dict[str, list[str]], key, present,
         remove_missing: list[str] | None = None) -> dict:
    """current: [(slot, device)] from disks.cfg. requested: {"parity"|"data"|"cache": [device]}.
    key(device) -> identity of the physical disk (its resolved path). present(device) -> bool.

    Returns {"slots": [(slot, device)] in file order, "added": [...], "removed": [...],
             "moved": [(device, old_slot, new_slot)], "kept_missing": [(slot, device)]}.
    Raises ValueError for a device requested twice."""
    remove_keys = {key(d) for d in (remove_missing or [])}
    seen: dict[str, str] = {}
    for role, devs in requested.items():
        for d in devs:
            k = key(d)
            if k in seen:
                raise ValueError(f"{d} is assigned to more than one slot.")
            seen[k] = role

    old_by_key = {key(dev): slot for slot, dev in current if role_of(slot)}
    old_dev = {key(dev): dev for slot, dev in current if role_of(slot)}

    # Disks that are assigned but not connected stay, unless explicitly removed or re-requested.
    kept_missing = [(slot, dev) for slot, dev in current
                    if role_of(slot) and not present(dev)
                    and key(dev) not in seen and key(dev) not in remove_keys]

    slots: list[tuple[str, str]] = []
    for role, prefix in ROLES:
        devs = requested.get(role, [])
        numbered: list[tuple[int, str]] = []
        used: set[int] = set()
        for slot, dev in kept_missing:
            if role_of(slot) == role:
                n = int(_SLOT.match(slot).group(2))
                numbered.append((n, dev)); used.add(n)
        fresh = []
        for d in devs:
            old = old_by_key.get(key(d))
            if old and role_of(old) == role:
                n = int(_SLOT.match(old).group(2))
                numbered.append((n, d)); used.add(n)
            else:
                fresh.append(d)
        nxt = max(used, default=0) + 1
        for d in fresh:
            numbered.append((nxt, d)); used.add(nxt); nxt += 1
        slots += [(f"{prefix}{n}", d) for n, d in sorted(numbered)]

    new_by_key = {key(dev): slot for slot, dev in slots}
    added = [dev for slot, dev in slots if key(dev) not in old_by_key]
    removed = [old_dev[k] for k in old_by_key if k not in new_by_key]
    moved = [(old_dev[k], old_by_key[k], new_by_key[k]) for k in old_by_key
             if k in new_by_key and new_by_key[k] != old_by_key[k]]
    return {"slots": slots, "added": added, "removed": removed, "moved": moved, "kept_missing": kept_missing}
