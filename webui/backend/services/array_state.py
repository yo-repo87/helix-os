"""Array Operation state machine (CLAUDE.md §30.5, adapted to Helix).

Pure function: given what is true right now, say which controls are valid, what each
will do, and — when one is unavailable — why. The UI renders this verbatim, and the
start/stop endpoints enforce the same answer, so a button that looks usable always is
and a refused request always has a reason ("refuse rather than fail").
"""

def _gb(n: float) -> str:
    return f"{n / 1000:.1f} TB" if n >= 1000 else f"{n:.0f} GB"


def compute(i: dict) -> dict:
    """i keys:
      state            "started" | "stopped"
      transition       "starting" | "stopping" | None
      parity, data, cache   lists of {name, device, size_gb, present}
      parity_running   bool;  parity_elapsed  "0:12:33" | None
      mover_running    bool
      synced           bool   (parity was built at least once)
      containers_running  int
      unmountable      list of {name, device} present while started but not mounted
      formatting       bool   a format job is running
      format_error     str    last format failure, if any
    """
    state, trans = i["state"], i.get("transition")
    parity, data, cache = i.get("parity", []), i.get("data", []), i.get("cache", [])
    busy_parity, busy_mover = bool(i.get("parity_running")), bool(i.get("mover_running"))
    rows: list[dict] = []

    def row(rid, status, label=None, action=None, enabled=True, explain="", warn=None, confirm=None, level="ok",
            detail=None, confirm_warn=False):
        rows.append({"id": rid, "status": status, "level": level, "explain": explain, "warn": warn,
                     "confirm": confirm, "detail": detail or [], "confirm_warn": confirm_warn,
                     "button": {"label": label, "action": action, "enabled": enabled} if label else None})

    footer = "Array " + {"started": "Started", "stopped": "Stopped"}[state]

    if trans:
        word = "Starting" if trans == "starting" else "Stopping"
        footer = f"Array {word}…"
        row("array", f"{word}…", "Start" if trans == "starting" else "Stop", None, False,
            f"The array is {word.lower()}; this page refreshes by itself when it is done.", level="busy")
        return {"state": state, "transition": trans, "rows": rows, "footer": footer,
                "busy": {"parity": busy_parity, "mover": busy_mover}}

    if state == "stopped":
        assigned = parity + data + cache
        missing = [d for d in assigned if not d.get("present")]
        biggest = max(data, key=lambda d: d.get("size_gb", 0), default=None)
        small_parity = next((p for p in parity if biggest and p.get("present") and p.get("size_gb", 0) + 0.5 < biggest.get("size_gb", 0)), None)
        if not assigned:
            row("array", "No devices assigned", "Start", "start", False,
                "Use Assign Disks to choose at least one data disk first.", level="error")
        elif not data:
            row("array", "No data disks", "Start", "start", False,
                "Parity and cache drives need at least one data disk. Assign one with Assign Disks.", level="error")
        elif small_parity:
            row("array", "Parity disk is too small", "Start", "start", False,
                f"The parity disk ({small_parity['name']}, {_gb(small_parity['size_gb'])}) must be at least as large as the "
                f"largest data disk ({biggest['name']}, {_gb(biggest['size_gb'])}). Swap them or use a bigger parity disk.", level="error")
        elif missing:
            names = ", ".join(d["name"] for d in missing)
            row("array", "Missing disk", "Start", "start", True,
                f"Start will go ahead without {names}. Anything stored only on a missing data disk is unavailable"
                f"{' (parity can rebuild it once the disk is replaced)' if parity else ''}.",
                warn=f"Not found: {names}", confirm="Yes, start without the missing disk(s)", level="warn")
        else:
            expl = "Start will bring the array online: mount the disks, start the shared view of your files, Docker, and SMB/NFS sharing."
            warn = None
            if parity and not i.get("synced"):
                warn = ("Parity has not been built yet. The array is available immediately but unprotected "
                        "until a Parity Sync finishes — run one right after starting.")
            elif not parity:
                warn = "No parity disk is assigned, so a failed disk would lose its data."
            row("array", "Stopped — configuration valid", "Start", "start", True, expl, warn=warn, level="ok")
        return {"state": state, "transition": None, "rows": rows, "footer": footer,
                "busy": {"parity": False, "mover": False}}

    # ── started ──
    ctr = int(i.get("containers_running") or 0)
    formatting = bool(i.get("formatting"))
    unmountable = list(i.get("unmountable") or [])
    format_error = (i.get("format_error") or "").strip()
    if formatting or busy_parity or busy_mover:
        if formatting:
            why = "disks are being formatted"
        elif busy_parity:
            why = "a parity operation is running"
        else:
            why = "the mover is running"
        extra = " or cancel it below" if busy_parity and not formatting else ""
        row("array", "Started, formatting…" if formatting else "Started", "Stop", "stop", False,
            f"Disabled — {why}. Wait for it to finish{extra}; stopping now could corrupt data.", level="busy" if formatting else "ok")
    else:
        note = f" {ctr} running container{'s' if ctr != 1 else ''} will be stopped." if ctr else ""
        row("array", "Started", "Stop", "stop", True,
            "Stop will stop Docker and file sharing, unmount all disks and take the array offline." + note, level="ok")

    if formatting:
        row("format", "Started, formatting…", "Format", "format", False,
            "Creating a file system on the unmountable disks. This page refreshes when it is done.",
            level="busy")
    elif unmountable or format_error:
        detail = [f"{d.get('name', '')} · {d.get('device_real') or d.get('device', '')}".strip(" ·") for d in unmountable]   # /dev/sdb, not the long by-id path
        noun = "disk" if len(unmountable) == 1 else "disks"
        explain = (f"Format will create a file system on the unmountable {noun}, using the file system chosen on each disk's page."
                   if unmountable else "Nothing left to format.")
        if format_error:
            explain = f"The last format failed: {format_error} " + explain
        row("format",
            "Unmountable disk present" if len(unmountable) == 1 else
            ("Unmountable disks present" if unmountable else "Format failed"),
            "Format" if unmountable else None, "format" if unmountable else None, True,
            explain, confirm="Yes, I want to do this" if unmountable else None,
            level="warn" if unmountable else "error", detail=detail, confirm_warn=bool(unmountable))

    if busy_parity:
        el = i.get("parity_elapsed")
        row("parity", f"Parity operation in progress{f' ({el})' if el else ''}", "Cancel", "parity_cancel", True,
            "Cancel stops the operation. A cancelled Sync leaves parity incomplete until you run it again.", level="busy")
    elif parity:
        row("parity_sync", "Parity is valid" if i.get("synced") else "Parity has not been built",
            "Parity Sync", "parity_sync", True,
            "Parity Sync reads every data disk and updates the parity disk, so a failed disk can be rebuilt from it. Run it after adding or changing files.",
            level="ok" if i.get("synced") else "warn")
        row("parity_scrub", "", "Parity Scrub", "parity_scrub", bool(i.get("synced")),
            "Scrub re-reads data and parity and checks them against stored checksums to find silent corruption." if i.get("synced")
            else "Disabled — build parity with a Sync first.")
    else:
        row("parity", "No parity disk assigned", level="warn",
            explain="Data is not protected. Stop the array and use Assign Disks to add a parity disk at least as large as your biggest data disk.")

    if formatting:
        footer += " · Formatting"
    if busy_parity:
        el = i.get("parity_elapsed")
        footer += " · Parity operation" + (f" {el}" if el else "")
    if busy_mover:
        footer += " · Mover running"
    return {"state": state, "transition": None, "rows": rows, "footer": footer,
            "busy": {"parity": busy_parity, "mover": busy_mover, "format": formatting}}
