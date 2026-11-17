"""Offline tests for services/disk_slots.py (CLAUDE.md §55). Run: python3 webui/tests/test_disk_slots.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from services.disk_slots import plan  # noqa: E402

A, B, C, D = "/dev/disk/by-id/ata-A", "/dev/disk/by-id/ata-B", "/dev/disk/by-id/ata-C", "/dev/disk/by-id/ata-D"
key = lambda d: d                     # the test devices are their own identity
here = lambda *devs: (lambda d: d in devs)
fails = 0


def check(name, got, want):
    global fails
    if got == want:
        print(f"PASS {name}")
    else:
        fails += 1
        print(f"FAIL {name}\n  got  {got}\n  want {want}")


# 2026-11-17: disk1=A, disk2=B; the UI sends [C, A, B] (C sorts first). C must become disk3.
p = plan([("disk1", A), ("disk2", B)], {"data": [C, A, B]}, key, here(A, B, C))
check("new disk does not shift existing slots", p["slots"], [("disk1", A), ("disk2", B), ("disk3", C)])
check("  reported as added, nothing moved", (p["added"], p["moved"], p["removed"]), ([C], [], []))

p = plan([("disk1", A), ("disk2", B), ("disk3", C)], {"data": [C, B, A]}, key, here(A, B, C))
check("request order is irrelevant", p["slots"], [("disk1", A), ("disk2", B), ("disk3", C)])

p = plan([("disk1", A), ("disk2", B), ("disk3", C)], {"data": [A, C]}, key, here(A, B, C))
check("removed disk leaves a gap", (p["slots"], p["removed"]), ([("disk1", A), ("disk3", C)], [B]))

p = plan([("disk1", A), ("disk3", C)], {"data": [A, C, D]}, key, here(A, C, D))
check("new disk goes after the highest slot, not into the gap", p["slots"], [("disk1", A), ("disk3", C), ("disk4", D)])

p = plan([("disk1", A), ("disk2", B)], {"data": [A], "cache": [B]}, key, here(A, B))
check("role change is a move", (p["slots"], p["moved"]), ([("disk1", A), ("cache1", B)], [(B, "disk2", "cache1")]))

p = plan([("parity1", D), ("disk1", A), ("disk2", B)], {"parity": [D], "data": [B, A]}, key, here(A, B, D))
check("parity keeps its slot", p["slots"], [("parity1", D), ("disk1", A), ("disk2", B)])

# B is assigned but not connected; the UI only lists connected disks, so it is absent from the request.
p = plan([("disk1", A), ("disk2", B)], {"data": [A, C]}, key, here(A, C))
check("missing disk stays in its slot", (p["slots"], p["removed"]), ([("disk1", A), ("disk2", B), ("disk3", C)], []))
check("  and is reported", p["kept_missing"], [("disk2", B)])

p = plan([("disk1", A), ("disk2", B)], {"data": [A]}, key, here(A), remove_missing=[B])
check("missing disk removed only when named", (p["slots"], p["removed"]), ([("disk1", A)], [B]))

p = plan([], {"data": [B, A], "parity": [C]}, key, here(A, B, C))
check("first assignment keeps the order given", p["slots"], [("parity1", C), ("disk1", B), ("disk2", A)])

try:
    plan([], {"data": [A], "cache": [A]}, key, here(A))
    check("duplicate device refused", "no error", "ValueError")
except ValueError:
    check("duplicate device refused", True, True)

p = plan([("disk1", A), ("disk2", B)], {}, key, here(A, B))
check("unassign everything", (p["slots"], sorted(p["removed"])), ([], [A, B]))

print(f"== {fails} failed")
sys.exit(1 if fails else 0)
