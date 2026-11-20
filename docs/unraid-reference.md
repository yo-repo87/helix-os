# Unraid 7.3.4 reference (moved out of CLAUDE.md, 2026-11-20)

Static reading of `unRAIDServer-7.3.4-x86_64.zip` (gitignored source: `unraid-extracted/`, `bzroot-fs/`, `bzmodules-emhttp/`). Behaviour summary only — **never paste Unraid source into this public repo**. Consult this before building a feature that has an Unraid equivalent.

## Part A — OS internals (original CLAUDE.md §1–§11)
## 1. What Unraid Actually Is

Unraid is **Slackware Linux** (a 374-package pkgtools-based distro) with heavy LimeTech customization layered on top. It runs entirely from RAM — the USB flash drive is a boot device and config store, not a running filesystem. Everything interesting lives in four compressed images on that USB.

---

## 2. Boot Architecture

### Hardware boot
- **Legacy BIOS:** syslinux + MBR (`syslinux/mbr.bin` written to the drive, `syslinux_linux` installs the bootloader on partition 1)
- **UEFI:** `EFI/boot/bootx64.efi` → `EFI/boot/syslinux.cfg` (just `include /syslinux/syslinux.cfg`)
- **Boot device:** FAT32 partition labeled `UNRAID` (or identified by UUID). The script `make_bootable_linux` does the USB prep.

### Kernel command line (from `syslinux/syslinux.cfg`)
```
label Unraid OS
  kernel /bzimage
  append initrd=/bzroot

label Unraid OS GUI Mode
  kernel /bzimage
  append initrd=/bzroot,/bzroot-gui

label Unraid OS Safe Mode (no plugins, no GUI)
  kernel /bzimage
  append initrd=/bzroot unraidsafemode
```

Custom kernel params: `unraidtimeout=`, `unraidlabel=`, `unraiduuid=`, `unraidvers=` (rollback to previous version), `root=` (dev mode — boot from a block device instead of USB).

### The four bz* image files
| File | Type | Size | Purpose |
|------|------|------|---------|
| `bzimage` | Linux kernel 6.18.58-Unraid, bzImage LZMA | 9.6 MB | Custom patched kernel |
| `bzroot` | Concat initramfs: microcode CPIO + zstd CPIO | 86 MB | Root filesystem in RAM |
| `bzmodules` | Squashfs (zstd) | 803 MB | Mounted as `/usr` |
| `bzfirmware` | Squashfs (zstd) | 332 MB | Mounted as `/lib/firmware` |
| `bzroot-gui` | zstd stream | ~small | GUI mode inittab overlay |

### bzroot internal structure (discovered by binary analysis)
```
Offset 0x00000000  — CPIO archive: early microcode (AMD + Intel bins)
Offset 0x0002dbaa  — false positive (gzip magic in CPIO payload)
Offset 0x0112e000  — zstd-compressed CPIO: full root filesystem (~80 MB uncompressed)
Offset 0x1a67ce8+  — additional CPIO segments (GUI / extra)
```

---

## 3. Init System & Boot Sequence

Unraid uses **SysVinit** (Slackware-style), not systemd.

```
kernel → initramfs (bzroot) runs /sbin/init
  → inittab: si:S:sysinit:/etc/rc.d/rc.S
      → mounts /dev (devtmpfs), /proc, /sys, /run
      → modprobes ZFS
      → polls for FAT32/ZFS device labeled UNRAID (up to 30s)
      → mounts USB as /boot
      → sha256-verifies bzimage, bzroot, bzfirmware, bzmodules
      → mounts bzmodules squashfs → /usr (read-only)
         then overlayfs (upperdir=/var/local/overlay/usr) → /usr (writeable)
      → mounts bzfirmware squashfs → /lib/firmware (overlayfs same pattern)
      → symlinks /etc/rc.d → /usr/local/etc/rc.d  (rc scripts live in bzmodules!)
      → moves /var/log to tmpfs
      → sources /etc/rc.d/rc.S.cont (continues in /usr land)

  → inittab: rc:2345:wait:/etc/rc.d/rc.M  (multiuser startup)
      → starts network, Samba, NFS, Docker, libvirt, nginx, PHP-FPM
      → runs /boot/config/go  → executes /usr/local/sbin/emhttp  (THE management daemon)

  → inittab: c1–c6 — agetty on tty1–6
  → inittab: x1:4 — /etc/rc.d/rc.4 starts display manager (GUI mode)
```

**Key insight:** Because `/etc/rc.d` is a symlink into `/usr` (which is bzmodules), all init scripts come from the squashfs, not bzroot. bzroot is just the minimal userland + config.

---

## 4. Filesystem Architecture (The Overlay Stack)

```
/boot         ← FAT32 USB (persistent, writable — config lives here)
/boot/config/ ← ALL user configuration (ident.cfg, network.cfg, go, plugins/, etc.)
/usr          ← bzmodules squashfs (RO) + overlayfs (RW upper in RAM)
/lib/firmware ← bzfirmware squashfs (RO) + overlayfs (RW upper in RAM)
/             ← bzroot zstd CPIO (pure RAM — tmpfs effectively)
/var/log      ← tmpfs (128 MB)
/run          ← tmpfs (128 MB)
```

Nothing written to `/` survives a reboot. The only persistence is:
- `/boot/config/` on the USB FAT32
- Docker/VM data wherever the user configured appdata/domain paths (array or pool)

---

## 5. The Management Daemon: emhttp

`/usr/local/sbin/emhttp` is the central daemon. It is started by `/boot/config/go`:
```bash
#!/bin/bash
/usr/local/sbin/emhttp
```

emhttp is a **C++ binary** that provides:
- The web UI backend (served via nginx reverse proxy on port 80/443)
- Array start/stop/parity-check control
- Drive health monitoring (via smartctl)
- Share management
- Plugin lifecycle management
- State files in `/var/local/emhttp/` (INI-format flat files, e.g. `disks.ini`, `shares.ini`, `network.ini`)

The web UI is **nginx + PHP 8.4 + PHP-FPM**:
- nginx config: `/etc/nginx/nginx.conf` with `nchan_shared_memory_size 256M` for server-sent events
- nginx is used as a reverse proxy + static file server for the UI assets
- PHP handles the dynamic pages (plugins, settings forms, etc.)

---

## 6. The Unraid Array (What Makes It Unique)

Unraid's array is its core differentiator. It is NOT standard mdadm RAID.

### How it works
- **Custom kernel module** (`md/unraid` driver) — not standard md/mdadm
- Supports **mixed drive sizes** (each data drive is independent XFS/Btrfs/ZFS)
- **Parity drive(s):** 1 or 2 parity disks; parity disk must be ≥ largest data disk
- Parity is calculated per-bit across all data drives at the same offset
- **On read:** reads directly from one drive (no striping)
- **On write:** read-modify-write for parity; slow but tolerates mixed sizes
- **On drive failure:** any single (or dual with 2 parity) data drive can be rebuilt
- **shfs:** custom FUSE-based "user share" filesystem — presents `/mnt/user/sharename` as a merged view across `/mnt/disk1/sharename`, `/mnt/disk2/sharename`, etc.

### Storage pools (Unraid 6.9+)
In addition to the array, Unraid supports **storage pools** (NVMe cache, all-flash):
- ZFS pool: native ZFS with RAID-Z support
- Btrfs pool: Btrfs with RAID 1/5/6 options
- XFS pool: single or multi-device

### Drive management
Key binaries in `/sbin/`: `btrfs`, `btrfsck`, `btrfstune`, `zfs`, `zpool`, `cfdisk`, `gdisk`, `fdisk`, `lvmdiskscan`, `cryptsetup`, `fsck.btrfs`

---

## 7. Services Stack

| Service | Version | Purpose |
|---------|---------|---------|
| Docker | 29.8.2 | Container engine |
| libvirt + QEMU | 12.2.0 / 10.2.3 | KVM virtual machines |
| Samba | 4.22.11 | SMB/CIFS shares |
| nfs-utils | 3.1.1 | NFS shares |
| nginx | 1.30.6 | Web UI + reverse proxy |
| PHP | 8.4.27 | Web UI dynamic layer |
| avahi | 20260508 | mDNS/Bonjour discovery |
| apcupsd | 3.14.14 | UPS monitoring |
| ZFS | 2.4.4 | Native ZFS support |
| WireGuard | 1.0.20260223 | Built-in VPN |
| dcron | 4.5 | Cron scheduler |
| openssh | (yes) | SSH access |
| bind | 9.20.29 | DNS |
| dnsmasq | (yes) | DHCP/DNS for VMs |

---

## 8. Plugin System

Plugins are `.plg` XML files that describe Slackware `.txz` packages to download and install. The pkg database is at `/var/lib/pkgtools/packages/`. Plugins live under `/boot/config/plugins/` and are restored on each boot. This is how the community extends Unraid without modifying the core images.

---

## 9. Cloud & API

### Unraid Connect (flash backup)
`/etc/rc.d/rc.flash_backup` — monitors `/boot` (the USB) for changes using `inotify`-equivalent polling + `git`. When changes are detected, pushes to `backup.unraid.net` via git. This backs up the entire config/ directory to the cloud.

### Unraid API service
`/etc/rc.d/rc.unraid-api` — starts `/usr/local/bin/unraid-api` (a Node.js binary based on the pm2 log dir and the `api_base_dir=/usr/local/unraid-api` layout). This is the GraphQL/REST API that:
- Powers the Unraid Connect cloud dashboard
- Provides the programmatic interface for remote management
- Lives at `/usr/local/unraid-api/.env.production`

---

## 10. Full Feature Hit List (sourced from live bzmodules UI code)

> Interactive version: https://claude.ai/artifact/LhTs8i8k3cQCWhbKnCjVVy  
> Check items off as they are implemented. The list below is the canonical reference.

### Boot & OS Layer
- USB boot device (FAT32 labeled UNRAID), run-from-RAM, overlayfs on squashfs
- BIOS + UEFI dual boot (syslinux MBR / EFI), SHA256 image verification
- Boot rollback (previous version select), internal boot (NVMe ZFS mirror), safe mode

### Array & Storage Engine
- Mixed-size parity array (mergerfs + SnapRAID), single + dual parity
- Array start/stop, parity check (progress/ETA), drive assignment UI
- Format drives (XFS/Btrfs/ZFS), ZFS native pools (RAID-Z), Btrfs pools
- LUKS encryption, mover (cache→array), mover progress display
- TRIM scheduler, drive spin-down, drive pre-clear, rebuild after failure

### Shares & File Access
- User shares via mergerfs (/mnt/user), create/edit/delete shares
- Per-share: included disks, use-cache setting, split level
- SMB (Samba) + AD integration, NFS + security rules, FTP, Avahi/mDNS
- Recycle bin, new permissions (recursive chmod) tool

### Dashboard (real-time, nchan/SSE push)
- CPU/RAM/temp tile, array status tile, per-drive tiles (temp/health/usage)
- Cache pool tile, Docker summary, VM summary, network RX/TX tile
- UPS status, WireGuard status, share usage, parity check overlay
- Mover indicator, notifications badge, uptime, GPU utilization tile
- Customizable tile layout (drag/drop, 3-column, saved per user)

### Docker
- Container list (status, icon, ports, CPU/RAM), start/stop/restart/remove
- Container logs viewer, add from template, update container (image pull)
- **GPU sharing — NVIDIA** (Container Toolkit, --gpus all/per-container)
- **GPU sharing — AMD** (ROCm, /dev/dri device passthrough)
- **GPU sharing — Intel** (/dev/dri/renderD128, QSV transcoding)
- GPU assignment UI per container + GPU utilization on dashboard
- CPU pinning, docker-compose support, Docker settings, autostart
- Community Applications (app store with template browsing)

### Virtual Machines
- VM list, full lifecycle (start/stop/pause/resume/force-stop)
- Add VM wizard, edit config, VNC + SPICE browser console
- **GPU passthrough (VFIO)** — exclusive GPU to single VM
- USB passthrough, vdisk management (qcow2/raw), VM templates
- CPU pinning, usage statistics, autostart, snapshots, BIOS/OVMF

### Networking
- Interface config (DHCP/static), NIC bonding, network bridging (br0)
- VLAN, WiFi, interface firewall rules, routing table viewer, outgoing proxy
- **WireGuard VPN** (server + client, wg0…wgN, peer management UI)
- Tailscale integration, unified VPN manager, DDNS

### Web UI & Theme
- **Dark (Black)** default: #141414 bg, #f07820 orange accent
- Dark Gray, Azure (light blue), White variants
- Custom header/background hex color, custom banner image upload
- Responsive/mobile, sticky nav, Font Awesome icons, collapsible sidebar
- Favorites menu, GUI search, xterm.js terminal, language/i18n
- HTTPS/SSL management, management access port settings

### Users & Access Control
- Local user accounts, Samba password management, per-share SMB ACL
- SSH key upload, API keys, TOTP 2FA, session management

### Notifications & Monitoring
- Drive failure, array events, SMART alerts, temperature alerts
- Email (SMTP), Discord, Slack, Telegram, Pushover, ntfy, Gotify, Prowl, Bark
- In-UI notification feed, UPS monitoring (apcupsd/NUT)
- Docker update badge, OS update badge

### Tools & Diagnostics
- Syslog viewer, diagnostics ZIP, SMART self-test + attributes
- Drive LED identify, PCI device list, driver list, process list
- Hardware profile, file manager (Browse), scheduler (cron calendar UI)
- Power mode/CPU governor, CPU isolation (isolcpus), Memtest86, boot params editor

### Plugin System
- Installed plugins list, install from URL (.plg), update/uninstall
- Plugin history log, Community Applications store, Docker template XML
- Plugin error reporting

### Cloud & Remote
- Flash backup (config git sync to cloud), REST/GraphQL API
- Remote dashboard relay, API key management, OS update from UI

---

## 11. Files & Directories of Interest

```
/home/pi/Claude/UnraidProject/
├── usb-creator.exe              — Unraid USB Creator (Inno Setup installer, Qt/C++ app)
├── unRAIDServer-7.3.4-x86_64.zip  — OS zip (1.2 GB)
├── unraid-extracted/            — Raw zip contents
│   ├── bzimage                  — Linux kernel 6.18.58-Unraid
│   ├── bzroot                   — Root initramfs (microcode + zstd CPIO)
│   ├── bzmodules                — /usr squashfs (803 MB)
│   ├── bzfirmware               — /lib/firmware squashfs (332 MB)
│   ├── bzroot-gui               — GUI overlay
│   ├── config/go                — Startup script: runs emhttp
│   ├── config/ident.cfg         — Default identity (name=Tower)
│   ├── config/network.cfg       — Default network config (DHCP, bonding, bridging)
│   ├── syslinux/syslinux.cfg    — Boot menu
│   └── EFI/boot/bootx64.efi    — UEFI bootloader
├── bzroot-contents/             — Early CPIO (microcode only)
│   └── kernel/x86/microcode/   — AMD + Intel microcode blobs
└── bzroot-fs/                   — Full extracted root filesystem
    ├── etc/rc.d/rc.S            — Main init script (Slackware-style)
    ├── etc/rc.d/rc.unraid-api   — Unraid API service control
    ├── etc/rc.d/rc.flash_backup — Flash backup (git → backup.unraid.net)
    ├── etc/inittab              — SysVinit table
    ├── etc/nginx/nginx.conf     — nginx (nchan, PHP-FPM upstream)
    ├── etc/unraid-version       — version="7.3.4"
    ├── var/lib/pkgtools/packages/ — 374 Slackware packages
    └── var/local/emhttp/        — Runtime state (INI files, written by emhttp)
```

---


## Part B — Web UI: layout, features, and what each click does (original CLAUDE.md §30.1–§30.15)
## 30. Unraid Web UI Reference: layout, features, and what each click does

Read this before changing Helix's layout or a feature that has an Unraid equivalent. Source: `bzmodules-emhttp/local/emhttp/` (Unraid 7.3.4; gitignored). `P=plugins/dynamix`, `DM=plugins/dynamix.docker.manager`, `VM=plugins/dynamix.vm.manager`. This section is a behaviour summary: **do not paste Unraid source into this repo** (it is public).

**Not visible in this source:** `emhttp` is a compiled binary, so what happens inside a `cmd*` command is inferred from the UI and help text. The header and notification components are compiled Vue (`unraid-*` elements, Unraid API). Community Applications is a separately downloaded plugin; only its installer page ships here.

### 30.1 How a page is built

- Every screen is a `.page` file: an INI header, `---`, then Markdown/PHP/HTML. `PageBuilder.php` scans them all.
- Header keys:

| Key | Effect |
|---|---|
| `Menu="Parent:rank"` | Attaches to a parent, sorted naturally by rank. May list several parents, so one page appears in several places. |
| `Type="xmenu"` | Parent that shows its children as sections or tabs |
| `Type="menu"` | Parent that shows an icon grid (`Panel`s) |
| `Tabs="true/false"` | Forces tabs on or off, overriding the user's "Page view" preference |
| `Cond="php"` | Page exists only if true, e.g. Docker tab only when dockerd runs, Shares only when the array is started |
| `Title`, `Icon`, `Tag`, `Code` | Text; panel icon; title icon; icon-font glyph for the nav |
| `Nchan="a,b"` | Background publisher scripts to start while the page is open |
| `Lock="true"` | Adds the padlock nav button that enables drag-sorting |
| `Load="30"` | Reloads the page every N minutes |

- **Consequence:** the menu is data, and conditions hide features that cannot work right now. Helix hard-codes its sidebar in `index.html`.

### 30.2 The shell around every page (`P/include/DefaultPageLayout*`)

Top to bottom: header banner (min-height 91px), nav bar, content (`#displaybox`, padding `1rem 1rem 4rem`), sticky footer.

- **Primary nav, by `Menu="Tasks:n"` rank:** Dashboard and Main (1); Shares, and Favorites only when any exist (2); Users (3, or inside User Preferences by preference); Settings (4); Plugins (50); Docker (60, only if enabled in `docker.cfg`); VMs (70, only if enabled in `domain.cfg`); Apps (80); Tools (90).
- **Right-side `Buttons`, by rank:** Search (Ctrl/Cmd+K; autocompletes every page, then jumps there and highlights the field), Language (hidden with no language packs), Logout, Terminal (a Display preference), File Manager, Feedback, Info (system information dialog), Log (live syslog window), Help (F1; toggles all inline help on the page), plus the padlock on sortable pages.
- **The menu tree:** Settings has System Settings (`OtherSettings`), Network Services, User Preferences and User Utilities grids. Tools has Unraid OS (`UNRAID-OS`), webGUI, Wizards (Onboarding and Internal Boot, both open only while the array is stopped), About.
- **Footer, on every page:** live array status (`Array Started/Stopped/Starting...`, coloured green/red/orange), running operation with percentage (`Parity-Check 41.2 %`, `Paused`), filesystem progress text, theme switcher, manual link, Wi-Fi indicator.
- **Top-of-page alert banners:** warnings, e.g. notifications disabled, or "operation continues in background" with an abort icon.
- **Tabs vs tabless:** with tabs, each child is a tab and the last-used one is remembered in a cookie. Tabless stacks every child vertically under a grey title bar with an icon.
- **Settings and Tools** are `Panel` icon grids (max 120px wide) under section titles. Hovering a panel reveals a heart to add it to Favorites.

**Settings form layout:** a `dl` grid with the label left and control right. Most fields have a help toggle revealing an inline explanation (`helptext.txt`, 392 entries). Forms end with **Apply + Done** (or Default). **Apply stays disabled until something changes.** Done returns to the parent.

### 30.3 How actions flow

| Mechanism | Used for | Behaviour |
|---|---|---|
| Form POST to `/update.htm` | Commands to emhttp: `cmdStart`, `cmdStop`, `cmdEditShare`, `cmdStartMover`, `changeDevice`... | Targets a hidden `progressFrame`, so the page does not navigate; the page then refreshes on the state change |
| Form POST to `/update.php` | Settings | Merges fields into an INI file on the flash (`#file`, `#section`), atomically, keeping keys not submitted. Optional `#include` validates first (can veto the save); `#command` + `#arg[]` then applies it. `#default` restores defaults. |
| `$.post` to `include/*.php` | Small actions and list fragments | Returns HTML or text that replaces a table body |
| Nchan pub/sub over WebSocket `/sub/<channel>` | All live data | PHP scripts in `nchan/` loop and publish; each stops itself after about 30s with no subscribers |
| `openPlugin` / `openDocker` / `openVMAction` | Long jobs | Starts the command, opens a modal with a live log and spinner. Closing it **does not stop the job**: it continues with a banner and abort icon. The Close button is disabled until `_DONE_`. |

Other rules worth copying:
- `csrf_token` is on every POST. A `session` channel forces back to login when the token changes.
- State comes from emhttp-written INI files in `state/` (`var`, `disks`, `devs`, `shares`, `sec`, `sec_nfs`, `users`, `network`), read on every page load. No page keeps its own truth.
- Spinners appear only if a request exceeds 500ms, so fast actions do not flicker.
- Polling stops when the browser tab is hidden.
- Destructive actions use a SweetAlert dialog ("Proceed?"). Reboot, power-off and array stop can be turned off in Settings → Confirmations. The riskiest ones need a separate "Yes, I want to do this" checkbox before the button enables.

**Behaviour every page inherits from the shell** (`BodyInlineJS`, `HeadInlineJS`, full read):
- **Apply/Done is global, not per page.** On load, every form's Apply is disabled. Any change to any field enables that form's Apply and turns its Done into **Reset** (which reloads at the form's scroll position). Fields with class `lock` opt out. Forms opting in with `js-confirm-leave` warn on navigating away with unsaved changes.
- **CSRF:** a hidden token is appended to every form, and a jQuery prefilter adds it to every same-origin POST. Quotes in text inputs are escaped before POST to `update.*`.
- **Banner warnings** rotate in one strip every 3s. Each can be dismissed for 30 days (cookie keyed on its text) or be undismissable.
- **Reboot notices are a shared service:** any feature calls `addRebootNotice(text)` / `removeRebootNotice(text)`. They are stored server-side, so they reappear on every page until reboot.
- 6s after each page load the boot config files are checked and, if corrupt or missing, a banner says the boot drive is corrupted or offline. "System notifications are disabled" and "running in safe mode" are also shown globally.
- **Inline help:** clicking any field label toggles the help block beneath it.
- **External links** open a confirm dialog naming the URL, with "always allow this domain". Internal links and Unraid's own domains skip it.
- Tab selection is remembered in cookies; a missing tab resets to the first. Scroll position is restored after a refresh (a `top` cookie). `Load=` page reloads pause while a dialog is open and restart on any click.
- Footer is static rather than fixed on phones and tablets.

### 30.4 Dashboard (`P/DashStats.page`, read in full)

Three columns (`#db_box1..3`), each a table whose `tbody` elements are tiles. Tiles start empty and are filled by live data. Plugins can inject tiles per column.

**Every tile header:** large icon, title, one summary line, then right-aligned controls (cog to its settings page; chevron to collapse). When collapsed, the header swaps to a compact summary (e.g. CPU load bar, RAM bar, container counts), so collapsed tiles still show state.

| Tile | Header | Body and actions |
|---|---|---|
| **Server** (always first; cannot be dismissed) | Name, description, clock when collapsed. Buttons: start/stop array (`busy` and unclickable during parity, mover or btrfs), sleep (if plugin), reboot, shutdown; cog; wrench (Tile Management); eject (collapse all) | Time (links to Date & Time; hover shows NTP/PTP sync status), date, Model, Registration, Uptime; case image opens a case picker |
| Motherboard | Temperature | Board, BIOS vendor/version, BIOS date; info button opens system info |
| Processor | Model; cog to CPU Pinning | Total power and temp; overall load bar; "Show details" expands per-thread bars paired `CPU n - HT m`, with core type on hybrid Intel; time-range select 10s/30s/1m/2m/5m; bar-chart toggle; 96px streaming line chart |
| System | Memory size, type, ECC; info to Processes | Usable and maximum RAM; legend; four CSS `conic-gradient` pies: RAM (segmented by consumer), Boot device, Log filesystem, Docker vdisk/folder. Hovering a pie swaps percent for absolute value. |
| Interface | NIC select and view select (General, Counters, Errors, Network traffic); Inbound/Outbound | Table per NIC (`bondN/ethN/wlanN/lo`) for mode, packets or errors; Traffic view is a 120px chart in brand red (in) and orange (out) |
| VPN (if WireGuard) | Active and inactive tunnel counts | Each tunnel has a click-to-toggle icon; peers show RX/TX and last handshake age |
| Power (if UPS) | Status, model, load | Status, Load, Battery charge, Runtime left, Nominal power, Output voltage |
| Airflow (if fans) | Fan count | Three fans per row with RPM |
| Docker Containers | "All Apps / Started only" switch; counts when collapsed | Icon grid; left-click gives the same context menu as the Docker page |
| Virtual Machines | Same switch | Same, plus optional **VM Usage** tile |
| Shares | SMB/NFS select; "Share count: N with X public SMB and Y public NFS" | Name (link), Description, Security, active stream count (orange when above 0) |
| Users | "User count: N with X unprotected" | Name (**orange if no password**), Description, Write and Read share counts |
| Parity | Status text | Last check result, and next scheduled check |
| Array / each pool / Unassigned | "X used of Y (Z %)", "(stopped)" | Warning summary line ("Array has 2 heat warnings."), then Device, Status, Temp, SMART, Utilization |

**SMART icon click** opens a menu: Attributes, Capabilities, Identity (jumping to that disk tab), plus **Acknowledge** when not healthy, which turns it to a green "healthy" thumb. Live rendering pauses for 15s after the click so the menu stays open.

**Layout management:**
- Padlock (`Lock="true"` page) toggles drag mode for the session: `tbody` tiles sort between all three columns, a close (X) button appears on each tile. Dismissing hides the tile.
- Wrench opens Tile Management: a checkbox for every tile, with Reset, All, None, Apply, Cancel. A column with no visible tiles disappears.
- **Persistence is server-side:** a JSON file on the flash (`/boot/config/dashboard_settings.json`, via `InitCharts.php`). It holds column order, dismissed tiles, collapsed tiles, chart ranges, selected NIC and views. Tile IDs are the MD5 of the title. **So layout follows the server, not the browser.** Only the unlock state is a session cookie and is cleared on load.

**Data channels:** `update1` every 5s (RAM, pies, fans, SMB streams), `update2` every 2s (disks, pools, parity, schedule), `update3` every 1s (NIC rates, counters, clock), `wireguard`, `apcups`, `vm_dashusage`; CPU load and power come from a GraphQL subscription with capped exponential backoff.

**Colour rule for all load bars:** at or above critical (default 90) red bar and `#f0000c` text; at or above warning (default 70) orange bar and `#e68a00`; otherwise `#4f8a10`. Threshold 0 disables that level.

**Efficiency:** charts render only values that changed; per-core bars update only when visible (IntersectionObserver). A ping channel lets publisher scripts stop when nobody is viewing.

**Clone VM dialog:** target name (`<name>_clone`), Overwrite, Regenerate MACs (default on), Check free space. **Snapshot dialog:** name (`--generate` default), description, memory dump (running VMs only), native-FS snapshot or QEMU external. Both can be started from Dashboard because the VM scripts are loaded there.

### 30.5 Main tab (`P/ArrayDevices`, `CacheDevices`, `BootDevice`, `OpenDevices`, `ArrayOperation`)

Tabs or stacked sections: **Array Devices, Pool Devices, Boot Device, Unassigned Devices, Array Operation**. Pool and Unassigned hide when empty.

**Disk table columns:** Device, Identification, Temp, Reads, Writes, Errors, FS, Size, Used, Free, with a totals row. Rows are pushed live over the `devices` channel (`nchan/device_list`), which swaps the whole table body.

**Status orb** (shape and colour together, so it reads without colour vision):

| Orb | Meaning |
|---|---|
| Green circle | Active, normal |
| Grey circle | Spun down |
| Blue square | New device |
| Yellow warning | Contents emulated or invalid; parity invalid |
| Red X | Disabled or missing |
| Grey square | Not present |

- Clicking an orb spins that device up or down; the icon becomes a spinner until the feed updates.
- Device name links to the disk's settings page. A folder icon opens it in the file manager. A log icon opens the disk log.
- A toggle swaps Reads/Writes between counts and live speed.

**Assigning disks (array stopped only):** each slot's Identification cell becomes a dropdown of free devices. **Choosing one submits immediately**, with no Save button. A `Slots` dropdown at the table bottom sets the slot count. Pools have an Add Pool button, a Slots control, Add Subpool, and a Remove Pool on the pool's page.

**Array Operation is a state machine** (`ArrayOperation.page`, read in full). Each row is status, button, then a sentence saying exactly what the button will do. Driven by `fsState`, then `mdState`, then `mdResyncAction`.

| `fsState` / `mdState` | Status text | Controls and explanation |
|---|---|---|
| Started, idle | Started (+ "Maintenance Mode") | **Stop**; Check or Sync or Clear depending on `mdResyncAction`; History with last-check summary (date, duration + speed, errors, or "incomplete" + error code) |
| Started, Unmountable disks | Lists each disk (name, ID, device) | **Format**, disabled until "Yes, I want to do this". Ticking it pops a warning that format is never part of data recovery. |
| Started, check available | "Parity is valid" | **Check** with "Write corrections to parity" ticked by default, plus a Schedule link. With no parity, Check is a Read-Check. |
| Operation running | "Parity-Check in progress" etc. (Parity-Sync, Data-Rebuild, Disk-Clear, Read-Check) | **Pause/Resume** and **Cancel**, enabled 250ms after load. Cancelling a sync warns it may leave the array unprotected. Live rows: Total size, Elapsed, Current position, Estimated speed, Estimated finish, errors detected/corrected. When finished, Cancel becomes **Done** and finish reads "completed". |
| Starting / Stopping / Formatting | "Starting...", "Started, formatting..." | Button present but disabled; page refreshes on `arraymonitor`/`fsState` |
| Copying / Clearing | With live progress text | Cancel |
| Stopped + licence state error (`configValid`) | Stopped | Start disabled, reason with a link |
| STARTED or STOPPED | "Configuration valid" | Start, "will bring the array online" |
| STOPPED, recon pending | Configuration valid | Start "...and start Parity-Sync / Data-Rebuild" |
| STOPPED, clear pending | New data disk(s) detected | Start "...and start Disk-Clear" |
| STOPPED, unclean (`sbClean!=yes`) | Unclean shutdown detected | Start "...and start Parity-Check" + write-corrections box; without parity just Start |
| STOPPED, pool disk missing | Missing pool disk | Start disabled until confirm; "will remove the missing pool disk" |
| NEW_ARRAY, no parity | Configuration valid | Start: "immediately available, but **unprotected**, since parity has not been assigned" |
| NEW_ARRAY, with parity | Configuration valid | Start; "start Parity-Sync"; "Parity is already valid" box. A dialog states the consequence either way (parity will be overwritten, or all disks must hold original content/slots). |
| DISABLE_DISK | Missing disk | Start disabled until confirm; "will disable the missing disk... install a replacement as soon as possible" |
| RECON_DISK | Replacement disk installed | Start "will start Parity-Sync and/or Data-Rebuild" |
| SWAP_DSBL, copy not done | Upgrading disk/swapping parity | Copy, gated by confirm |
| SWAP_DSBL, copy 100% | Upgrading disk/swapping parity | Start "expand the file system... then Data-Rebuild" |
| `ERROR:INVALID_EXPANSION` | Invalid expansion | Disabled: cannot add and remove disks together |
| `ERROR:NEW_DISK_TOO_SMALL` | Replacement disk is too small | Disabled, must be as big or bigger |
| `ERROR:PARITY_NOT_BIGGEST` | Disk in parity slot is not biggest | Disabled; move largest disk to parity or try Parity-Swap |
| `ERROR:TOO_MANY_MISSING_DISKS` | Invalid configuration | Disabled |
| `ERROR:NO_DATA_DISKS` / `NO_DEVICES` | No data disks / No devices | Disabled |

Every startable state also shows a **Maintenance mode** checkbox ("Start array but do not mount disks"). When LUKS is in play, extra rows appear: status (Enter new key / Missing key / Wrong key), Passphrase or Keyfile selector, show-passphrase, retype when creating, and "permit reformat" when wrong. Start stays disabled until valid.

**Secondary rows while started:** Spin Up / Spin Down (all); Clear Stats (visible only in counters view); Move, or **Empty** if there are no pools (only when user shares are on); Reboot / Shutdown with a Safe Mode checkbox.

**Blocked actions explain themselves.** A `mymonitor` channel reports 0 normal, 1 parity, 2 mover, 3 btrfs. While busy, Stop and Move disable and the sentence changes to e.g. "Disabled -- Parity operation is running". Spin buttons also disable during a parity run.

**Start click:** button disables, server checks pool state (`Report.php`); on a problem shows "Wrong Pool State" and re-enables. A passphrase is checked as printable ASCII. Keyfiles upload first. Then `cmdStart` is submitted with the current `mdState` as `startState`.

**Stop/Cancel:** confirm dialog (if enabled), then submit. Declining removes the hidden command field.

**Pause/Resume:** logs a timestamp in `ParityControl.php` so elapsed time excludes pauses, then submits. The button label also self-corrects from the live feed.

**Reboot/Shutdown:** confirm, then POST to `Boot.php` (progress page). Safe mode warns that Tailscale/Connect access will be lost and highlights if you are connected through it.

**Device rows in detail** (`nchan/device_list`, read in full; republished every 1s):
- Sections are built separately and sent as one JSON object keyed by element ID (`array_devices`, `boot_device`, `pool_deviceN`, `open_devices`), each replacing that table body.
- **Stopped layout:** parity slots, a thin divider, data slots, then `Slots:` dropdown (min is the larger of current disks or 3). The Identification cell is the assignment dropdown listing free devices sorted by size, then model; first option is the current disk, then `unassigned` or `no device`.
- **Inline row warnings while stopped:** "All existing data on this device will be OVERWRITTEN when array is Started" in red, replacing that row's stats. It shows for a new-array parity disk, for invalid/replacement disks being rebuilt, and new disks to be cleared. `Missing` or `Wrong` tags appear under the name, with the previously recorded ID beside the dropdown. Duplicate serials get an orange clone icon listing the other devices.
- **Started layout:** folder (browse) icon only if mounted, orb, name link, description with a device-type icon (usb, disk, nvme, disk-encrypted) that opens the disk log. Tooltips explain each orb and add "Click to spin up/down device". Orbs are clickable only while started and not on boot devices.
- Description format: `ModelSerial - 14 TB (sdb)`, with `*` temperature when unknown.
- Reads/Writes hold both a live speed and a count; a toggle in the section title switches them, remembered in a cookie, and Clear Stats appears only in count view.
- Errors combine device errors and ZFS/btrfs member errors; a non-zero count gets an orange triangle whose tooltip breaks it down by pool and type.
- Used/Free are text or a coloured bar per the Display preference, coloured by the disk's warning and critical thresholds. Encrypted filesystems show a padlock (to be encrypted, encrypted + unlocked, encrypted, wrong key, unknown error).
- **Totals row:** "Array of N devices" with spin-down and spin-up icons for the group, average temperature, summed I/O and errors, summed capacity.
- **Pools:** a `pool_header` row (Data Partition, or Boot Partition for bootable pools) with pool status in brackets (`(ONLINE)`, or degraded plus a warning icon, linking to the pool's status). Members follow, indented, as `Device 1`, `Device 2`. ZFS subpools show as `» Special Device 1`. Slot count locks once members change on a formatted pool. Dropdown max is 1 for single-device filesystems.
- Unassigned: blue orb; preclear status text fills the last four columns if a preclear is running.
- A public SMB export of the boot device gets an orange warning icon on its row.

**Pool actions** (`CacheDevices.page`, read in full), shown only while stopped:
- **Add Pool** dialog: Name (lowercase, starts with a letter, not reserved, not an existing share or pool; default name `cache` for the first pool) and Slots. A specific error message explains each rejection.
- **Add Subpool** on ZFS pools: special, logs, dedup, cache, spares (disabled); already-used types are removed.
- **Add Bootable Pool** (when eligible): Name, 1 or 2 slots, device per slot (no USB, each unique), reserved boot size (16 to 128 GB, or custom 4 GB up to 50% of the smallest drive), update BIOS order. A confirmation lists everything and states in red that the devices will be formatted.

**Per-disk page** (`/Main/Device?name=diskN`, tabs: Settings, Attributes, Capabilities, Identity, Self-Test):
- Previous/next arrows cycle through devices. **Fields:** Name (click to rename a pool while stopped; same validation as Add Pool), Identification, Comments, Partition size/format, Spinup group(s), Spin-down delay (Use default, Never, 15/30/45 min, 1 to 9 hours), File system status ("Mounted, empty/not empty"), File system type, Allocation profile, Compression, Autotrim, Enable user share assignment, Minimum free space (10% if blank), per-disk warning and critical thresholds with the global defaults as placeholders.
- **File system type** is editable only when stopped and the pool is unformatted. Groups: Standard (auto, xfs, ext4, btrfs, zfs), Encrypted, Limited support (ntfs, exfat; reiserfs disabled as deprecated). Single-device filesystems are offered only for 1 slot.
- **Allocation profile** options depend on slot count. btrfs: single, raid0, raid1, raid1c3, raid1c4, raid10, raid5, raid6. ZFS: stripe, mirror, raidz1/2/3 with a width shown as "N vdevs of M devices". ZFS defaults: 2 slots mirror, 3 to 8 raidz1, 9+ raidz2.
- **Buttons:** Apply, Done, Erase (stopped and new, or started in maintenance mode), Erase Pool, Remove Pool (stopped only). **The dangerous ones require typing the device name** ("To confirm type: name"). Erase warns existing content is lost; Remove says it unassigns without changing contents; a wrong entry shows "Incorrect confirmation". Upgrade ZFS Pool also requires typing the name and warns it cannot be reversed.
- **btrfs sections:** Pool Status; Pool Device Status with Reset; Balance Status with usage output and a verdict ("Current usage ratio X% --- Full Balance recommended" at or below 50%, otherwise "No Balance required"), conversion dropdown limited by device count, Balance or Cancel + "Running"; Balance schedule (Disabled, Hourly, Daily, Weekly, Monthly recommended; block-group usage %); Scrub Status and schedule. Balance and Scrub are disabled with an explanation when not mounted.
- **ZFS sections:** Status Summary (coloured), Pool Status with Scrub, or Clear when zpool asks for it, Upgrade Pool if available, Pool Information, Scrub Schedule.
- **Check Filesystem** (one block per filesystem) is available only "when array is Started in **Maintenance** mode", otherwise the button is disabled with that sentence. btrfs defaults its Options field to `--readonly`; reiserfs to `--check`.
- **xfs is a guided flow:** Check runs `-n`. By result: exit 0 "No file system corruption detected"; exit 4 "File system corruption fixed"; corruption shows a red message and a **Fix** button (`-e`); a dirty log shows a red message and a **Zero Log** button (`-eL`) with a note that this is the last resort when the filesystem cannot be mounted to clear the log. ntfs reports repaired, fatal or hibernated by exit code. Running jobs show Cancel + "Running".
- A running job polls `FileSystemStatus.php` every 1s, and a cookie remembers it across page loads.
- Schedule forms share one pattern: mode, then day/date/time enabled only when relevant; "Hourly" swaps the time to "Every N hours".

**SMART tabs** (each loads by POST to `SmartInfo.php` with a `cmd`, showing a spinner row until filled):
- Attributes: #, Name, Flag, Value, Worst, Threshold, Type, Updated, Failed, Raw Value. Capabilities and Identity (Title, Information) are read-only tables.
- **Self-Test:** Download SMART report (saved, zipped, delivered, then the temp file is deleted once the window regains focus); Show/Hide self-test history; Show/Hide SMART error log; Short test Start; Extended test Start; Last result. The Extended button is **disabled unless the disk's spin-down is Never** (or inherits a global Never), because spin-down aborts it.
- Start flips the button to **Stop** and disables the other test. A cookie per device remembers the running test so reloading the page resumes the display. Progress polls every 3s and stops when the reply has no `%`.
- Device tabs are `Menu="Device New"`, so they exist for assigned and unassigned disks alike.

**Disk Shares tab** (`DiskList.page`): Name, Comment, SMB, NFS, Type, Size, Free, plus Compute All. Exists only when the array is started and disk shares are enabled. The `Disk` parent adds previous/next arrows, cycling only through data disks and pools.

### 30.6 Shares (`P/ShareList`, `ShareEdit`, `SecuritySMB`, `SecurityNFS`, `DiskList`)

Requires a started array; otherwise a notice explains why. **User Shares** and **Disk Shares** tabs.

**List columns:** Name, Comment, SMB, NFS, Storage, Size, Free.
- Orb: green "all files protected" / yellow "some or all unprotected"; padlock for encryption; folder icon browses it.
- Storage shows the path, e.g. `cache → array` with an arrow for mover direction.
- **Size is not computed by default**, because it means spinning up every disk. The cell is a `Compute...` link that also expands a per-disk breakdown; `Compute All` does every share. Buttons: Compute All, Add Share, Clean Up (enabled only when stale config files exist).

**Editing a share** (`ShareEdit.page`, read in full). Add and Edit use one form; each field group is a shaded block with its own help.

| Field | Behaviour |
|---|---|
| Read settings from (top) | Pick another share, **Read** copies its allocation, floor, split, include/exclude, storage and COW, then saves |
| Write settings to | Multi-select with `(All)`, **Write** applies this share's settings to the others, one every 3s; disabled once the form has unsaved edits |
| Share name | 40 max; orange inline warning on restricted characters while typing. On submit, a named error dialog for each failure: empty, too long, must start with a letter, allowed characters, cannot end in `.`, reserved, same as a pool, already exists. Comparisons ignore case. A case-insensitive name clash disables the whole form with a warning. |
| Comments | 256 max |
| Minimum free space | Blank placeholder "Free Space will be calculated". **If left blank, the floor is computed on save: 10% of the storage size; or 90% of free if that exceeds free; capped by the largest array free.** Numbers without units mean KB. Changing Primary clears it. |
| Share status | Shown when editing: "Share is empty" or "Share contains data" (checked by `ShareList.php`) |
| Exclusive access | Read-only Yes/No |
| Primary storage | Array or any enabled pool. A configured pool that no longer exists stays selected, disabled, with a red "Warning: Configured Pool 'x' is missing", and **Apply refuses until a valid choice is made**. |
| Copy-on-write | Only for btrfs pools, and settable only at creation |
| Allocation, Split, Included, Excluded | Shown only when Array is the target (under Primary if Primary is Array, under Secondary if Secondary is Array); multi-selects show All or None when empty |
| Secondary storage | Hidden when Primary is Array. None, Array, or another pool (never the same as Primary). |
| Mover action | Dropdown labelled with real names (`Cache ⮕ Array`, `Array ⮕ Cache`), with a sentence ("Mover transfers files from Primary to Secondary"); "Not used / Mover takes no action" when nothing applies |

Selections map to the stored `shareUseCache`: Primary Array = `no`; pool + None = `only`; pool + secondary with direction forward = `yes`, reverse = `prefer`.

**Footer:** Add Share + Done when new. When editing: Delete checkbox (disabled with a tooltip "Share must be empty to be deleted" unless empty), Apply + Done. Ticking Delete turns Apply into Delete. Deletion first removes stray `.DS_Store` files.

**Pools-only servers** (no array slots) disable every "Array" option and default new shares to the first pool.

**SMB tab** (`SecuritySMB.page`, full read): top strip Read settings from / Write settings to; Export (No, Yes, Yes hidden; plus Time Machine variants when Enhanced macOS is on, with a volume size limit in MB); Case-sensitive names; Security Public/Secure/Private, with a red inline warning on Public about Windows needing a valid user. Apply saves. When Secure or Private, a second section "SMB User Access" appears with a per-user row (Read-only/Read-write for Secure, adding No Access for Private), its own Read/Write copy strip, and a heading that states guest access ("Guests have **read-only** access").

**NFS tab** (`SecurityNFS.page`, full read): Export No/Yes, Security Public/Secure/Private; Private reveals a Rule textarea, one host per line, normalised on save.

The Share page adds **previous/next share arrows** beside the tabs, cycling through shares in natural sort order.

### 30.7 File manager (`P/Browse.page`, read in full)

Opens from any folder icon or the nav button; only `/mnt` and `/boot` are browsable. Title bar is a clickable breadcrumb (home icon first; right-click a crumb shows the path with a Terminal button and click-to-copy), plus a file-type filter and a Time/Age toggle.

**Table:** checkbox, Type, Name, Owner, Permission, Size, Last Modified, Location (which disk), Action. Sortable; sort remembered.

**Bottom buttons:** Done, Jobs, Search, Delete, Copy, Move, Rename, Owner, Permission, Calculate, Create, Upload. Extras stay disabled until something is ticked; Rename needs exactly one; Create and Upload are disabled at the root or share level; Rename is not offered there either.

**Row action menu** (left or right click):

| On | Items |
|---|---|
| Folder | Delete, Copy, Move, Rename, Owner, Permission, Calculate, Search, then Create, Upload |
| File | Delete, Copy, Move, Rename, Owner, Permission, Download, then Create, Upload |
| While a job runs | Just "Job running" |

**Every action dialog:** source list, target field (with a folder tree and popular destinations; typing a trailing `/` expands the tree), and one warning line stating what will happen ("This deletes the folder and all its content"). Buttons: **Start, Queue, Cancel**. Copy/Move have Sparse and "Overwrite" options.

**Rules enforced in the client:** names cannot contain `/`; a target must be valid; **user-share and disk paths cannot be mixed** in one job, because that can lose data; recursive owner uses `user:users` (or `root:root`); permission dialog sets owner/group/other separately, always adding `ugo+X`.

**Running a job:** Start disables inputs and shows live progress. **Queue** stores it for later; Jobs lists queued items with Start and Delete (selected). Cancel kills a running job. The job persists server-side, so a page reload picks it back up.

**Upload:** 20 MB raw chunks with parallel read of the next chunk, percent + speed + `[n/total]` file name beside the button; closing the page prompts first; the button becomes Cancel; on failure the partial file is removed and the reason (timeout, network, read, HTTP) is shown.

**Open a file:** known text types open in an Ace editor dialog with Save/Cancel and an expand-to-fullscreen button (extension list is user-editable in `/boot/config/editor.cfg`); images/PDF open in a viewer; zip downloads.

### 30.8 Docker (`DM/DockerContainers.page`, `DockerContainers.php`, `docker.js`, `CreateDocker.php`, all read in full)

**Columns:** Application (icon, name, status), Version, Network, Container IP (+ MAC in Advanced), Container Port, LAN IP:Port, Volume Mappings (`container ↔ host`, `←` for read-only), CPU & Memory (Advanced), Autostart, Uptime and Created.

**Basic/Advanced switch** top right (cookie). Advanced adds Container ID, image, author, version tag, MAC, force-update links, CPU/memory bars, autostart wait, and orphan-image rows (Remove only).

**Row status:** play icon green (started), pause orange (paused), square red (stopped). During an action the icon becomes a spinner until the list reloads.

**Version column states:** `up-to-date` (green), `update ready` + **apply update**, `rebuilding`, `not available` (orange, registry unreachable). Advanced adds **force update**. Containers not created through this UI show `Compose` or `3rd Party` instead, with no update or Edit, and no autostart switch.

**Bottom buttons:** Add Container, Start All, Stop All, Pause All, Resume All, Check for Updates, Update All, Container Size. **Busy banner** "starting up containers" shows beside the title while Docker is starting, with the list reloading.

**Left-click icon menu** (built per container):

| Condition | Items |
|---|---|
| Running, not paused | WebUI, Tailscale WebUI, Console |
| Update available | Update |
| Running | Stop, Pause (or Resume), Restart |
| Stopped | Start |
| Always | Logs, Edit (template only), Remove |
| Metadata | Read Me First, Project Page, Support, More Info, Donate |

Clicking the **name** opens Edit. Tailscale containers show a tooltip with online state, version and update availability, hostname, relay, addresses, routes, exit node, URL, key expiry.

**Click results:**
- Start/Stop/Pause/Resume/Restart: spinner, POST `Events.php`, reload; on failure a dialog shows the error.
- Remove: "Are you sure?", checkbox "also remove image" (on by default).
- Update: confirm, then a live modal. The sequence: pull with per-layer progress, stop gracefully, remove, recreate from the saved template, **start only if it was running**, then delete the now-orphaned old image. If it used another container's network and that container no longer exists, it is recreated with no network and left stopped.
- Autostart: switch saves at once. Drag rows (after padlock) sets the start order. Per-container wait in seconds in Advanced.
- Logs/Console: open a separate terminal window.

**Add/Edit form:**
- Top: **Template** dropdown with `Default templates` and `User templates` groups; a red X removes a user template. Choosing one reloads the form pre-filled. Editing an existing container hides the dropdown. Basic/Advanced switch also here.
- Fields: Name (required), Overview and Additional Requirements (rendered read-only in Basic, editable in Advanced), Repository (required). Advanced: Registry URL, Icon URL, WebUI, Extra Parameters, Post Arguments, CPU Pinning grid (CPU row, HT row). Network Type: Bridge, Host, Container, None, plus each custom network labelled with its description. Fixed IP shows only for custom networks (with the subnet shown); Fixed MAC for bridge and custom; Container shows a picker of other containers. Console shell, Privileged. Authoring mode adds Categories, Support, Project, Read Me, and a **Save** that shows the generated XML and command without running.
- **Config items** render as rows: icon, name, value input, Edit/Remove buttons, and an orange line beneath showing `Container Path: /config` plus the description. Path and Device inputs open a file picker on click. Variables whose default contains `|` become dropdowns. Port inputs accept digits only. Masked values are password fields. Required items cannot be emptied. Items marked advanced sit under "Show more settings". On Host network, each port's container side is set to match the host side.
- **Add another Path, Port, Variable, Label or Device** opens a dialog: Config Type, Name, then labels that change by type (Container Path / Host Path; Container Port / Host Port; Key / Value). Path has an Access Mode (Read/Write, Read Only, each plus Slave and Shared); Port has TCP/UDP.
- **Show docker allocations** lists every container's ports and paths, with the current one bold.
- A default-app template gets its `/config` path pre-filled from the default appdata location.
- Tailscale section (bridge and custom only; explains why when disabled): Hostname, Exit Node options, Userspace Networking, SSH, Serve/Funnel, Advanced. Options that conflict disable themselves with a short reason ("Disabled due to use of an Exit Node"). Warnings for key expiry, unapproved routes, hostname mismatch.

**Apply:** the form becomes XML and is **saved to the boot device (`my-<name>.xml`) first**. A full-page blocker stops further clicks. Output is a log page showing the exact command, image pull, stop and removal of any existing container (and the old one if renamed, keeping it stopped if it was), then `docker run`. Ends with **View Container Log** and **Done**.

**Docker settings** (`DockerSettings.page`, full read). The page has two modes:
- **Docker stopped:** editable. Enable, readmore, Stop Timeout, PID Limit, data-root (btrfs vDisk, xfs vDisk, directory), vDisk size and location, or directory, storage driver (directory only), default appdata, log rotation, authoring mode, network type, host access, preserve networks. Network rows per interface with an Edit checkbox: read-only Subnet, Gateway, and a DHCP pool picker that builds the range from octet and mask dropdowns and shows "(N hosts)". Interfaces with no IP get free-entry Subnet/Gateway/pool. Delete vDisk or directory checkbox turns Apply into Delete.
- **Docker running:** the same fields shown read-only, plus Docker version and, for btrfs, volume info and Scrub.
- If the array is stopped, inline warnings say "Docker will be available after Array is Started" and "unable to validate path until Array is Started".
- Paths use a click-to-open folder tree. Invalid patterns turn red with a title explaining the rule. On Apply: DHCP pool must fit in the subnet and IPs must be valid, each with a named error dialog. Basic/Advanced toggle hides log rotation and networking.

### 30.9 VMs (`VMMachines.page`, `VMMachines.php`, `vmmanager.js`, `VMSettings.page`, `VMTemplates.page`, read in full; editor field list only)

**Add/Update VM editor** (`templates/Custom.form.php`, 128 KB; fields and function names extracted, not read line by line). Sections in order: Name, Description, WebUI, Override Storage Location; CPU Mode, Migratable, vCPU/Pinned Cores grid with Select All; Memory Initial and Max; Machine, BIOS, Enable USB boot, Hyper-V, USB Controller; OS Install ISO + bus + Boot Order; VirtIO Drivers ISO; repeating vDisk blocks (Location, Size, Type, Bus, Discard, SSD, Serial); repeating Unraid Share blocks (mode, share, source path, mount tag); repeating Graphics Card blocks (VNC/SPICE protocol, copy/paste, autoport, ports, video driver, render GPU, password, keyboard; or passthrough GPU with ROM BIOS path, multifunction and PCI Check); Sound Card; repeating Network blocks (MAC with generate, source, model); USB Devices checkboxes; Other PCI Devices; Start VM after creation; Create and Cancel. Advanced adds QEMU command line, clocks, evdev, and an XML expert view. Also Create/Modify Template. Warnings cover NUMA placement and Wi-Fi source ("Configure the VM with a static IP address", "Only one VM can be active at the time"). Back-end actions are in `VMajax.php`: domain lifecycle, media change, memory/vCPU change, disk create/resize, snapshots, USB hot attach/detach, VirtIO ISO download, templates.

**Columns:** Name (icon, name, state), Description, CPUs (hover/click shows a pinning map, orange dots for pinned threads, "vCPUs: N"), Memory, vDisks/vCDs (`count / size`, then `n / 2` CDs with a pick-ISO icon, and `(Snapshots: N)`), Graphics (VNC:port, driver, or GPU name), IP Address ("Requires guest agent installed" or "Requires guest running" otherwise), Autostart switch. Reset sort icon and drag handle under padlock. Buttons: Add VM, Start All, Stop All.

**Status icon:** play green (running), pause orange (paused or suspended), square red (stopped). Orange triangle by the name if PCI devices changed or an SR-IOV root is used ("Start disabled").

**Clicking the name** expands a detail row (state remembered in a cookie): 
- Disk devices: path, real volume, Serial, Bus, Capacity, Allocation, Boot Order. **While shut off, clicking the capacity turns it into an input; only growing is allowed.**
- CDs: eject icon, or insert icon with "No CD image inserted".
- Interfaces: name (MAC), type, IP, prefix.
- Snapshots: indented tree with a clickable icon, description, date/time, type (method), parent, memory.

**Left-click icon menu:**

| State | Items |
|---|---|
| Running | VM Console (VNC/SPICE, opens new window), remote-viewer, Open WebUI, RDP; Stop, Pause, Restart, Hibernate, Force Stop; Create Snapshot |
| Suspended | Resume, Force Stop |
| Paused/unknown | Resume, Force Stop |
| Stopped | Start, Start with console, Start with remote-viewer; or a red disabled item explaining "Start disabled due to PCI Changes" |
| Any | Logs (if a log exists), Edit |
| Shut off | Clone; Create Snapshot; Remove VM (definition only); **Remove VM & Disks** ("Completely REMOVE ... disk image and definition") |

Console and RDP options depend on VM Manager settings.

**Snapshot row menu:** Revert snapshot, Block Commit and Block Pull (QEMU external only), Remove snapshot ("Removing Snapshots can break the chain"). Revert offers Remove Images and Remove Meta. Block Commit picks Base and Top images, Pivot, Delete.

**Dialogs:** ISO picker with file tree (one or both CD drives); Clone and Create Snapshot as in §30.4.

**VM Manager settings:** Enable VMs; a hardware check shows a notice if VT-x/AMD-V is missing and disables the rest; inline warning that VMs start only after the array is started. Advanced: libvirt version, storage location (edit only while stopped, with a Delete image checkbox), default VM and ISO paths, VirtIO ISO with Download/Remove, default network source, host-shutdown action, timeout (warns if above the disk shutdown timeout), console options, RDP, VM Usage and its refresh timer, View libvirt log. PCIe ACS and VFIO settings moved to Boot Parameters, and a highlighted box links there.

### 30.10 Settings map (`Menu=` groups)

| Section | Pages |
|---|---|
| System Settings | CPU Pinning, Date and Time, Disk Settings, Docker, Identification, Management Access, Network Settings, Global Share Settings, UPS Settings, VM Manager, Boot Parameters |
| Network Services | NFS, SMB, FTP Server, Syslog Server, Tailscale, VPN Manager, Outgoing Proxy |
| User Preferences | Confirmations, Console Settings, Display Settings, Notification Settings, Scheduler (Parity Check, Mover, TRIM) |
| User Utilities | Plugin-added pages |

Details that matter:
- **Disk Settings:** auto-start, default spin-down, default filesystem, tunables, encryption key, default SMART handling, temperature and utilisation thresholds.
- **Network Settings:** tabs per interface. Bonding, bridging, IPv4/IPv6 assignment, MTU, VLAN list. Fields are locked while Docker or VMs are running, with a message saying which service to stop.
- **Management Access:** root password link, start page, SSH/Telnet with ports, UPnP, HTTP/HTTPS ports, certificate management, local URLs.
- **Display Settings:** width, language, font size, number format, page view (tabs or stacked), used/free as bar or text, temperature unit, theme, banner, custom header colours.
- **Scheduler:** each uses one pattern: schedule dropdown; day, date and time fields enable only when relevant.
- **Parity:** write corrections; cumulative check (pause outside a window, resume later).

**Disk Settings** (full read), three separate forms each with its own Apply: (1) main: auto start, default spin-down (Never, 15/30/45 min, 1 to 9 hours), spinup groups, default array filesystem (xfs, zfs, btrfs, ext4, each with an encrypted variant), shutdown time-out (default 90s), tunables, zfs_arc_max (Unlimited, 10% to 90% with GB shown, Custom GB up to 90% of RAM), NVMe power monitoring, utilisation and temperature thresholds (HDD and SSD); (2) Change encryption key (only when encrypted and started); (3) Global SMART Settings (Raw or Normalized, tolerance Absolute to 50%, controller type list, custom attributes plus preset attribute checkboxes, Default button). Tunables show the system default as placeholder text.

**VM Templates** (`VMTemplates.page`): a grid of OS icons; hovering a user template shows delete and export; buttons Cancel, Import from file, Upload. Clicking opens Add VM pre-filled.

**Notifications:** Display position and formats; per-event toggles for notices, warnings and alerts by channel (Browser / Email / Agents); update checks for OS, plugins, Docker and language; array status report schedule. SMTP has provider presets and a Test button. **Notification Agents:** Bark, Boxcar, Discord, Gotify, Join, Prowl, PushBits, Pushbullet, Pushover, Pushplus, ServerChan, Slack, Telegram, ntfy.sh, each with fields, Enable and Test, from one XML file per agent.

**Users** (`UserList`, `UserAdd`, `UserEdit`, full read):
- List has two panels: Management Access (root only) and Shares Access (everyone else), each user an image + name tile with the description on hover. With no other users it says to click Add User.
- Add: username, description, PNG image (drag-drop or click; 512 kB, PNG only), password + retype with show/hide eye. Add is disabled until the name is filled and passwords match. A strength bar appears only if a `zxcvbn.js` has been placed on the flash. Passwords are base64-encoded in hidden fields before POST.
- Edit is **several independent forms on one page**: (1) description, image, Delete checkbox (not for root) that turns Apply into Delete; (2) Password + Change; (3) root only: SSH authorized_keys textarea (validated line by line, including key type and `from=` entries, with the failing line number in the error); (4) a table of every exported share: Share, Security, User Access dropdown (Read/Write only for Public; adds Read-only for Secure; adds No Access for Private).
- Applying (4) rebuilds each changed share's full access list and POSTs one share every 3s behind a spinner. The Apply is disabled until a row changes; only changed rows are submitted.

**Network Settings** (`Eth0`, `EthX` template, `NetworkExtra`, `NetworkRules`, `RoutingTable`, `Wireless`; logic read in full):
- **One tab per NIC, generated at render time:** the eth0 page lists `/sys/class/net/eth[1-9]*`, writes a page file from the `EthX` template for each new port, deletes files for missing ports, and refreshes itself.
- **Service lock:** if libvirt or Docker is running, the form is disabled (except descriptions) and says which service to stop. Only the description can then be saved, and the form uses an `arg1` value to skip reconfiguring when nothing structural changed.
- **eth0 fields:** DNS block on top (separate form, merged into the main POST): IPv4 and IPv6 assignment Automatic/Static, 3 servers each; Static is forced when the address is static. Interface description + Info dialog, MAC, Enable bonding (mode 0 to 6, default active-backup; "this mode requires a network switch" appears for 0, 2, 3, 4; members multi-select, excluding ports locked to another bond or bridge), Enable bridging (members hidden when bonding is on, because the bond becomes the bridge member; 2+ members force STP on), Network protocol (IPv4, IPv6, both), address assignment (Automatic, Static, None), address with netmask, default gateway with a checkbox and metric, IPv6 privacy extensions, Desired MTU behind an "Enable jumbo frames" checkbox with a warning, Enable VLANs.
- **VLANs:** Network type access or trunk; `Show/Hide` plus `Add VLAN`, `Delete VLAN`; each VLAN repeats description, number and the IP block. Adding one enables Apply at once.
- **Field visibility** is entirely driven by Protocol and Assignment: Static shows address/mask/gateway and makes them required; Automatic hides them but keeps the gateway/metric row; None hides both. The hidden values are cleared on submit, so a stale static IP is never posted.
- **Submit checks:** duplicate VLAN numbers, duplicate metrics, each with a named dialog. Applying reloads the page after 25 seconds, with a "Please wait... configuring interface" indicator in the tab bar, because the connection may drop.
- A per-port check polls every 5s and posts a top-of-page notice when the cable is down, with a toggle to bring the port up or down.
- **Interface Extra:** live "Current listening interfaces" (5s refresh), Include and Exclude textareas for what services bind to. Management interfaces are silently removed from Include and rejected from Exclude with an error.
- **Interface Rules** (only if the rules file exists): one MAC dropdown per ethN, the NIC's description updating beside it; duplicates rejected; after Apply a red "Please **Reboot**" line appears.
- **Routing Table:** Protocol, Route, Gateway, Metric, Delete; refreshes every 10s. Add form: route, gateway (datalist of interfaces), optional metric. Delete asks first.
- **Wireless** (only if `wlan0` exists): Wi-Fi Enabled/Disabled, regulatory region (Automatic, Manual with a 2-letter code, or country). When enabled: Connected, My networks, Other networks (6s refresh), Add network by SSID. Clicking a network opens a join dialog; security type decides whether username and password show.
- **Outgoing Proxy:** select of None or a defined proxy, where **only proxies that test online are selectable**; three slots with Name, URL, User, Password and live status. Credentials typed in the URL are parsed out.

**WireGuard** (`VPNmanager`; `WG0` plus `WGX` template; structure, all dialogs and field lists extracted, not read line by line). Each tunnel is a tab:
- Local: name, private and public key with Generate Keypair, Network protocol (IPv4, IPv6, both), tunnel network pool, tunnel address, endpoint (validated: "does not resolve to..."), NAT, UPnP, firewall, MTU, DNS servers.
- **Peer type of access is the central choice:** Remote access to server, to LAN; Server to server; LAN to LAN; Server hub & spoke; LAN hub & spoke; Remote tunneled access; VPN tunneled access for system; VPN tunneled access for docker. Tailored sentences appear per type: e.g. "configure your router with port forwarding of port...", "connect to this server using IP address...", "docker containers on custom networks need static routing", "this must be the only peer in the tunnel and sole active tunnel when in use".
- Peer: name, private, public, preshared key (Generate), tunnel addresses, endpoint, allowed IPs (auto-filled from access type), DNS, keepalive; running stats Data received/sent, Last handshake; Ping button; Import Tunnel from a config file; QR/view via an eye icon, locked while there are unapplied changes.
- Submitting force-opens any collapsed section holding an invalid field before reporting it. Delete Tunnel warns connections will drop, and **is refused while containers are attached** ("Stop corresponding docker containers"). Basic/Advanced view is remembered per tunnel in a cookie. Changing UPnP state warns that configurations should be reapplied.

**Boot Parameters** (`BootParameters.page`, 155 KB; layout, every action function and dialog read; the syslinux and GRUB text-parsing routines were not):
- Top switch between **Menu view** and **Raw view** (remembered). Sections: VM Passthrough (PCIe ACS override: Disabled, Downstream, Multi-function, Both; VFIO unsafe interrupts), Framebuffer (disable EFI, VESA, Simple, or Block All, which turns the others off and locks them; boot mode decides which are enabled), Hardware Compatibility (sensor access, disable ACPI and PCIe error reporting, PCI reallocation), Power Management (USB autosuspend, NVMe deep power states, ASPM, PCIe port PM), Custom Parameters (param + comment, list with edit/remove).
- A live **"Parameters to Apply" preview**, then the current config, shown as Global Configuration plus Boot Entries, each entry with a default-boot radio, an apply toggle and a diff. Boot menu timeout dropdown.
- **Typing a custom parameter that a toggle already controls converts it to that toggle**, so the same setting can't exist twice. Custom parameters are validated per bootloader.
- Apply gates: if a Safe Mode entry is included, a modal requires "I've Tested - Apply to Safe Mode"; if framebuffers are disabled on a GUI entry, a conflict modal offers to remove them from GUI entries. Buttons Apply, Done, Reset, Default, Restore Backup.
- **Every write makes a dated backup.** Restore lists backups grouped by minute with A/B suffixes, can delete all. Raw save has an "are you sure" warning that syntax errors can stop booting.
- Each change registers a persistent **"A reboot is required"** notice. A hardware change detected since last visit shows a warning naming what changed, with "I Understand" to acknowledge.

**System Devices:** master switch Show All / Filtered plus filters (GPU/Audio, Network, Storage, USB, SR-IOV, Other), stored in cookies; turning any filter on turns Show All off, and SR-IOV also turns on Network and Display. **Apply is only visible in Show All**, so a filtered list can never save a partial binding set. Also warns when booted with ACS override, since groups then aren't the hardware's own. Below: CPU Thread Pairings, USB Devices, SCSI Devices. Results: "VFIO Changes saved. Reboot to take effect", "No changes", and a reboot notice is added or removed. **SR-IOV:** per-card VF count, per-VF VFIO and MAC (generator takes a 2-digit host ID); applying checks first whether a VM is using it and lists those VMs instead of proceeding; warns "Physical card will reset" on removal. PCI changes since last boot get Acknowledge actions, each listing affected VMs.

**Identification:** Server name (15 chars; keypress filter plus inline warning), Description, Model. All disabled with "Array must be **Stopped** to change". `"` and `\` are stripped from the free-text fields on submit.

**Rule: changing identity or SMB/global-share options requires the array stopped.** The whole form is rendered disabled with that sentence, instead of failing on Apply. NFS, FTP, Confirmations and notifications can be changed while started.

**SMB** is three tabs, two conditional: SMB Settings (Enable: No / Yes Workgroup / Yes Active Directory; hide dot files; multi-channel; enhanced macOS; NetBIOS; WSD plus options); Workgroup Settings (only if Workgroup mode; Local master only if NetBIOS is on); Active Directory (only in AD mode: join status, FQDN, short domain, login, password, Join and Leave disabled by current status, plus initial owner/group); SMB Extras (2048-char raw textarea saved to `/boot/config/smb-extra.conf`).

**NFS:** Enable, `fuse_remember` tunable with a live status beside it; a second form for max protocol (NFSv4 or NFSv3) and threads (8 to 128). **FTP:** Disabled/Enabled, space-separated user list; state read from whether port 21 is listening, not from a saved flag.

**Global Share Settings:** Enable disk shares (No, Yes, Auto), Enable user shares; Included, Excluded and **Emptying** disks (multi-selects; the three disable when user shares are off); Permit exclusive shares; hard-link and direct-IO tunables; FUSE file descriptors (clamped to the system maximum, shown beside it).

**Confirmations:** reboot/powerdown, array stop, and sleep if a plugin provides it.

**Mover Settings:** schedule (Disabled, Hourly, Daily, Weekly, Monthly), day, date, time, logging; Apply, Done, **Move now**. With no pool the button becomes **Empty now** with a notice; if user shares are off, a notice replaces the form. While running, the button disables and "Mover is running" shows beside it (from a pid file).

**TRIM:** same schedule pattern plus **Trim Now**, which opens the live-log modal.

**Parity Check schedule:** modes Disabled, Daily, Weekly, Monthly, Yearly, Custom. **Changing the mode re-submits the page at once** so the fields rebuild for that mode: Custom turns day and month into multi-selects and "Day of the month" into "Week of the month". After that re-render, Apply is pre-enabled and Done becomes Reset. Time is in 30-minute steps. Also: write corrections, Cumulative (not for Daily), frequency, duration. Page exists only if a parity or data disk is assigned.

**Date and Time:** date format, time format (forced by "System Setting"), time zone, live "Time state", Time sync (NTP, PTP, Manual) revealing one of three blocks. NTP: interval and 4 servers. PTP: transport, multicast/unicast, 4 servers (validated by transport: IPv4, IPv6 or MAC), interface, hardware or software clock (hardware option disabled when the NIC has no clock). Manual: date/time box. Apply only restarts time services if a time field changed: a display-format change alone just refreshes.

**Management Access:** Link to root user; Start page (any top-level task); Telnet and SSH each with a port field disabled when off; UPnP; Use SSL (No, Yes, **Strict only when a valid wildcard cert and working DNS exist**), with a warning that appears when changed; HTTP and HTTPS ports; Local TLD. Apply first validates ports: range (1 to 65535), duplicates among the four fields, and already-listening ports, each with its own named dialog. Below: **Local access URLs** computed from the SSL mode, showing what redirects where, which cert is used, and a self-signed warning; WireGuard and Tailscale URL lists if any exist; then one block per certificate (self-signed/user, CA-signed, Tailscale) with file, URL, whether it is valid for this name, issuer, expiry.

**Notification Settings:** position, date/time format, store to boot drive, System notifications (the master switch that enables the three class rows); update-check schedules for OS, plugins, Docker, language; Array status report schedule (hourly to monthly). Choosing any schedule reveals its Browser/Email/Agents checkboxes, hidden when "Never". Notices, Warnings and Alerts each have the same three checkboxes, stored as a bitmask (1 browser, 2 email, 4 agents). Buttons: Default, Apply, Done.

**SMTP:** Preset service fills server, port, TLS, STARTTLS and method (Gmail, Outlook, Yahoo, and others; Custom). Fields: sender, recipients, priority, subject prefix, server, port, SSL/TLS, STARTTLS, TLS cert, auth method, username/password (disabled when None). Sender is validated with a named error. **Test** is disabled until saved settings are complete and **disables as soon as the form changes**, so a test always uses saved values; it shows a 20-second countdown, then the result in place of the button.

**Notification Agents:** one titled section per agent from an XML definition, with its icon and a green Enabled or red Disabled status. Fields: Agent function, the agent's variables with inline help; title and message fields are multi-selects of Event, Subject, Timestamp, Description, Importance, Content, Link. Buttons Apply, Done, Delete and Test (only after saved; Test only when enabled). Apply writes a generated shell script to the flash under an enabled or disabled name.

**Display Settings** (fields): width, language, font size, terminal font size, terminal button in header, number format, number scaling, page view, Users menu placement, listing height, device name, WWN, array totals, array utilisation indicator, temperature unit, theme, used/free columns, header text/secondary/background colours (hex, validated), header description, banner on/off, custom banner drag-drop, banner gradient, favorites enabled. Buttons Default, Apply, Done.

**Console Settings:** local keyboard layout, screen blank time (Default, Disabled, 1 to 60 min), persistent bash history. **Power Mode:** radio buttons Best power efficiency, Balanced, Best performance, each mapped to a kernel governor and shown "(unavailable)" when absent; a notice appears when running virtualised. **Power Button:** Shut down (default), Reboot, Do nothing; plugins can add actions.

**CPU Pinning** (`CPUset`, three tabs): VM, Docker, Isolation. A grid with one row per VM or container and one checkbox column per thread, HT pairs stacked in the header, multiple header rows above 32 threads, with Intel core type as a tooltip. Apply is two-step: `UpdateOne.php` validates and reports what changed, then `UpdateTwo.php` applies per item while that row shows progress. Errors appear in a dialog and Done becomes Reset. Isolation is refused in safe mode and shows a reboot notice when changed. The Docker tab says "Docker service must be started" otherwise.

### 30.11 Tools, Plugins, Apps

- **Tools:** System Log, Log Viewer, Diagnostics (anonymise option, downloads a ZIP), Hardware Profile, New Config, New Permissions, Processes, System Devices, System Drivers, Open Terminal; webGUI: Language, PHP Settings, Page Map, Vars; About: Update/Downgrade OS, Credits, EULA, Registration.
- **New Config:** resets assignments so all disks appear new; multi-select to preserve Array slots and/or Pool slots. Text warns in capitals that it will make rebuilding a failed drive impossible. Disabled with "Array must be **Stopped**" while started; otherwise Apply needs "Yes, I want to do this"; afterwards it reads "Array has been **Reset** (please configure)".
- **New Permissions:** Target (Disks or Shares), then a multi-select of mounted data disks or shares; "Proceed?" dialog, then the live-log modal. Controls are disabled until the array starts.
- **Diagnostics:** page lists exactly what is collected and states what is included even when anonymised (server name, IP, share names). Anonymize is on by default. Download opens a dialog with a live log over a dedicated channel; on `_DONE_` the browser downloads the ZIP, and the server copy is deleted after the window regains focus. Named `<server>[-safemode]-diagnostics-YYYYMMDD-HHMM.zip`.
- **Open Terminal:** opens a popup terminal, then returns to the previous page after 5s; shows a manual button if the popup was blocked.
- **System Devices:** IOMMU groups with per-device VFIO checkboxes, Apply, reboot prompt.
- **System Log:** dropdown of log files (current, previous boot's, remote-host logs) only if more than one exists; coloured category checkboxes (Errors, Warnings, System, Array, Login, Text); a Master toggle; Download as a ZIP; max lines (default 5000); scrolls to the end after load.
- **Syslog Server settings:** local server (Enabled reveals protocol UDP/TCP/Both, port, share folder, file identifier, rotation), remote server, mirror to flash. Port range and already-in-use checks as in Management Access.
- **Language:** a User/Developer switch. User: installed list with trash icons, install from XML URL. Developer: pick a language, upload a ZIP; wrong extension gets a named error.
- **PHP Settings:** for plugin authors: error level, show on screen (disabled until a level is chosen), View Log, Clear Log, PHP Info, live log size.
- **Update OS / Downgrade OS:** rendered by compiled components (`unraid-update-os`, `unraid-downgrade-os`). Update adds a **Flash backup** button that zips the boot device and downloads it, showing "Insufficient free disk space" if it can't. Downgrade offers the previous version only, after a confirm that says a reboot is required; it also offers diagnostics plus a bug-report link.
- **UPS Settings:** a summary table (Model, Status, Charge, Runtime, Load...) visible only while the daemon runs, refreshing every 3s; a Details tab of key/value pairs. Fields: Start daemon, cable, custom cable (read-only unless Custom), type, device (read-only for USB), capacity override, shutdown triggers (battery %, runtime minutes, time on battery), turn off UPS after shutdown. Default, Apply, Done.
- **Plugins** (`Plugins`, `PluginInstall`, `PluginsStale`, `PluginsError`, `PluginHelpers`, full read): tabs Installed, Install Plugin, Plugin History (only if stale files exist), Install Errors (only if errors exist). Installed: icon, Plugin, Author, Version, Status, Uninstall; `Check For Updates` in the tab bar; `Update All Plugins` only when 2+ updates are pending; `Remove Selected` only when 2+ are ticked. The Remove control hides while an update is running on that row. The list updates per row by plugin id rather than rebuilding. Install: URL or file tree of `/boot`, Force Install (same or older version). **Before any install or update, pending alert text is checked and shown for confirmation.** All run in the live-log modal.
- **Apps:** Community Applications after a one-click install of its plugin.

### 30.12 Visual system (`P/styles/`)

CSS layers: `default-color-palette.css` (raw colours), `default-base.css` (layout), `default-dynamix.css` (widgets), `themes/{azure,black,gray,white}.css` (semantic variables). Default theme is **white**. **Black and white use a top nav bar; azure and gray use a left sidebar** (`.Theme--sidebar`, `.Theme--nav-top`).

| Token | Value |
|---|---|
| Brand orange / red | `#ff8c2f` / `#e22828` |
| Black background / table | `#1d1b1b` / `#212121` |
| Tile, title bar (black theme) | `#303030`, `#262626` |
| Text (black theme) | `#f2f2f2` |
| Green / link blue | `#17bf0b` / `#486dba` |
| Fonts | clear-sans (UI), bitstream, Font Awesome plus a custom `font-unraid` icon font |
| Root size | `html` 62.5%, so `1rem = 10px`; body 1.3rem, title bars 1.4rem, table headers 1.1rem |

**Theme files only redefine semantic variables** (`--text-color`, `--background-color`, `--table-background-color`, `--border-color`, `--header-*`, `--button-*`, `--shade-bg-color`...), which is how one component stylesheet serves all four themes. Black: page `gray-900`, table `gray-800`, text `gray-100`. White: page `gray-100`, table `gray-150`, text `gray-900`. Azure and gray use cyan-toned text and solid muted buttons.

**Buttons in black and white themes are outlines, not filled:** a 2px orange-to-red gradient border with orange text; on hover the gradient fills and text turns white. Azure and gray use a flat button. Tables have alternating rows and a subtle hover. Title bars are a full-width grey strip with an icon and right-aligned controls. Page `<title>` is `ServerName/PageName`.

### 30.13 Where Helix differs (checked against `webui/frontend/index.html`, 2.0.13)

Helix uses a 21-item left sidebar of hash routes with everything top-level. Unraid has about 10 top-level items with the rest in Settings and Tools grids.

Patterns worth adopting, highest value first:
1. **Array panel as a state machine:** only valid buttons, each with a sentence explaining it, reason shown when disabled.
2. **Global footer** with live array and operation status.
3. **Long jobs in a modal live log that survives closing**, with a banner and abort.
4. **One icon context menu** shared by Dashboard and list pages, built from container state.
5. **Apply disabled until a form changes; Done returns to the parent.**
6. **Shape-plus-colour status orbs** for disks, click to spin.
7. **Inline disk assignment** in the table while stopped.
8. **Share editor** with Primary/Secondary/Mover wording and on-demand size.
9. **Hide or explain features that cannot work now**, driven by state.
10. **Save a container's definition before touching the running one.**
11. **Group the sidebar:** move config-style pages into Settings and diagnostics into Tools grids; add Favorites.
12. **Dashboard:** drag-sortable tiles with a tile manager and padlock.
13. **One global Apply/Reset behaviour and one reboot-notice service** in the shell, instead of per-page code.
14. **Type-the-name confirmation** for erase and pool removal; a checkbox gate for New Config and format.
15. **Edge-triggered alerts** (§30.15) with a matching "returned to normal", so one problem sends one message.
16. **Dated backup on every boot-config write**, plus a raw view and restore.
17. **Refuse rather than fail:** render a form disabled with "Array must be Stopped", and check whether a VM or container uses something before changing it.

Mapping is a recommendation; none of this has been built yet.

### 30.14 Exact rules and behaviours (from the full `helptext.txt`)

**Validation limits**

| Field | Rule |
|---|---|
| Share name | Up to 40 chars, case-sensitive; starts with a letter; then letters, digits, `.`, `_`, `-`; cannot end in `.`. Reserved: homes, global, printers, flash, boot, user, user0, dev, remotes, RecycleBin, rootshare, addons, mirror, raidz*, draid, spare, log, disk*, parity* |
| Share comment | Up to 256 chars |
| Username | Up to 40 chars; starts lowercase letter or `_`; then lowercase, digits, `_`, `-`; may end in `$` |
| User description | Up to 64 chars; no `&`, `"`, `:` |
| Password | Up to 128 chars |
| User image | Scaled to 48x48; max 95 kB on add, 512 kB on edit |
| Banner image | Scaled to 1920x90; max 512 kB |
| Hostname | NetBIOS limit of 15 chars; alphanumeric, `-`, `.`; first and last alphanumeric |
| Container name | Min 2 chars; first `a-zA-Z0-9`; then also `.`, `-`, `_` |
| Container MAC | Valid unicast (first octet even) |
| VLAN | 1 to 4095 |
| Encryption passphrase | Up to 512 chars, printable ASCII advised; keyfile max 8 MB. Loss means data is unrecoverable; changing the key means reformatting. |
| Free-space values | Suffix `KB/MB/GB/TB` = powers of 1000, `K/M/G/T` = powers of 1024, none = 1024-byte blocks |
| Docker vDisk path | Must end `.img`; appdata path must end `/`. Invalid input is **marked red and not accepted**. |
| Docker PID limit | Default 2048; 0 is unlimited |

**Array and disks**
- Temperature shows `*` for spun-down drives, because reading it would wake them.
- Reads/Writes count I/O requests. Errors count *unrecoverable* errors only; a failed read is rebuilt from parity and written back; **any unrecoverable write error disables the disk**.
- Array needs at least one device; minimum 2 array slots. Assignment is written to config on each dropdown change. Hot-plugged disks appear after a Main page refresh while stopped.
- Default multi-device pool is btrfs raid1 (data and metadata).
- Default thresholds apply to all disks, overridable per disk. **0 disables a threshold. If warning >= critical, only critical fires.**
- SMART notifies on a rising RAW value or a NORMALIZED value reaching the maker's threshold, with a tolerance setting. Attributes with raw value above zero show orange.
- Self-tests only run while the disk is active; spinning down aborts them, so the spin-down timer should be off for an extended test.
- Each filesystem check pre-fills safe options: xfs `-n` (check only; clear to repair), btrfs `--readonly`, ext `-p`, ntfs `-d`, exfat `-p`. Checks can take hours; progress shows as rising read counters.

**Shares and mover**
- A share is simply a top-level directory across volumes; the view is the composite.
- Legacy use-cache meaning: **No** never uses cache, mover ignores. **Yes** writes cache, overflows to array, mover moves to array. **Only** must be on cache, fails when full, mover ignores. **Prefer** writes cache, overflows to array, mover moves back to cache.
- **Mover never moves files in use**, so Docker/VM data needs those services stopped. Hard links are preserved.
- Split levels: 'any' creates parents wherever the allocator picks; 'top N' splits only down to level N, deeper files follow the parent's disk; Manual only uses disks where the parent exists.
- Included/Excluded empty means all disks; each share's lists are a subset of the global ones.
- Exclusive shares require: Primary is a pool, Secondary none, share on a single volume. Benefit is bypassing FUSE.
- Disk shares Auto: only disks not participating in user shares may export. Copying between a disk share and a user share can lose data.
- SMB Case-sensitive: auto, Yes, Forced lower. Time Machine needs a volume size limit. NFS public rule is `*(rw,sec=sys,insecure,anongid=100,anonuid=99,no_root_squash)`, plus `async,no_subtree_check` by default. NFS rules are one host per line.
- Boot device is FAT, so SMB export only.

**Services**
- Docker starts automatically after the array starts once enabled. Stopped containers are changed at once; **running ones are stopped first and restarted** when pinning changes, in parallel. Pinning no cores means all cores; do not pin to isolated cores.
- Template types: Default (app store) and User-defined (saved on the boot device, used to rebuild).
- WebUI field substitutes `[IP]` and `[PORT:####]`. Only the host side of a port mapping should be changed.
- Stopping VM Manager attempts shutdown of all VMs, then kills after 60 seconds. On server shutdown: Stop or Hibernate, with a timeout.
- FTP setting is not persistent; listed users get full access. SSH host keys persist in `config/ssh`.
- Bonding modes 0 to 6, default 1 (active-backup). Bond and bridge can be combined; VLANs share the parent's MAC. IPv4/IPv6 assignment: Automatic, Static, or None (VLANs only); fields grey out on Automatic.
- `network-rules.cfg` maps MAC to ethN; deleting it restores auto-assignment.
- WireGuard: passive mode when peer endpoint empty, active when set; NAT on avoids router changes but breaks containers with custom IPs; firewall default rule "deny" blocks listed addresses, "allow" inverts; default keepalive suggestion 20s; peers viewed with an eye icon (disabled while unapplied changes exist), keys shown with a key icon.
- Use SSL/TLS: No, Yes, Strict; redirects follow from it. Self-signed cert regenerates when hostname or TLD changes.
- Syslog: local server mode, remote server, mirror to flash (wear warning), copy to flash on shutdown (default yes, appears as `syslog-previous`).
- Physical power button: Shut down (default), Reboot, Do nothing.

**Scheduling and notifications**
- Parity schedule presets: daily, weekly, monthly, yearly, custom; fields unused by the chosen preset are disabled; time in half-hour steps. Cumulative: daily or weekly continuation, with a duration in hours.
- Mover hourly schedule: the time field becomes an interval starting at minute 0.
- Notification classes: **notice** (informative), **warning** (future problem), **alert** (immediate). Each chooses Browser/Email/Agents. Disabled by default. Stored in RAM unless "Store notifications to boot drive". Detailed view shows popups with auto-close seconds (0 disables) and stacking by position; Summarized shows counts per category in the header.
- SMTP password stored unencrypted; UI says so.

**Display preferences**
- Width Boxed (max 1920px) or Unlimited. Font size is per device. Listing height Automatic or Fixed (scrolling window). Device name, WWN, totals, used/free display, temperature unit (changing it converts thresholds).
- Users menu position: header or inside Settings.

**System Devices:** selecting a device binds all devices in its IOMMU group to vfio-pci at boot. Greyed checkbox means in use by the OS; green orb means bound; retweet icon means FLR-capable; downgraded PCIe speed or width shows orange; filters switch "Show all" to "filtered".

### 30.15 Alerts and small endpoints

**`scripts/notify` (the one entry point for every alert):** arguments are event, subject, description, importance (`normal`, `warning`, `alert`), message, link, recipients, `-x` for a single ticket, `-t` email test, `-b` no browser. Each notification is one INI file named `<event>-<timestamp>.notify`, written to `archive/` always and to `unread/` if browser delivery is on. The class's bitmask decides browser (1), email (2), agents (4). Agents are scripts run with EVENT, SUBJECT, DESCRIPTION, IMPORTANCE and similar environment variables. `archive <file>` just deletes the unread copy. The folder is `/tmp/notifications` unless "Store to boot drive" is on. Schedules are written to cron by `notify cron-init`, run on Apply.

**`scripts/monitor` (runs every minute when System notifications are on; first ~250 lines read in full, the rest by its notify calls):** compares current state with remembered state in `monitor.ini` (RAM, copied to the flash if changed), so **it alerts on change, not every minute.** A warning repeats only if the value rises further; a "returned to normal" notice clears it.

| Check | Alert | Clears with |
|---|---|---|
| Temperature (per-disk, NVMe-reported, SSD or global thresholds) | Warning/Alert, repeats only when hotter | "returned to normal temperature" |
| SMART attribute (monitored set), raw or normalised, with tolerance; FAILING_NOW always counts | Warning naming the attribute | "returned to normal value"; Acknowledge is reset by any change |
| Disk utilisation | "high on usage" / "low on space" | "returned to normal utilization level" |
| Disk colour (only while the array is not stopped) | Red: Alert "in error state"; Yellow: Notice with status | "returned to normal operation" |
| Disk read errors | One array summary "array has errors" | "array turned good" |
| Parity, rebuild, clear running | Notice "started" | Notice "finished (N errors)" |
| Boot drive not mounted read-write | Alert | Notice "returned to normal operation" |
| ZFS pool status | Alert (not online) or Warning (errors) | Notice "recovered" |
| btrfs missing device / too many profiles | Warning | Cleared silently |
| Docker vDisk utilisation | Warning/Alert | Notice |

**`Boot.php`:** Reboot and Shutdown POST here; it sets or clears the safe-mode flag file, issues the command and renders a holding page showing live array status until the server is gone (then returns to Main).

**`Report.php`:** `config` checks that key config files parse (retrying once); `notice` lists reboot notices; `state` reports pool errors before Start.

**Parity pause:** `ParityControl.php` only records a timestamp list; `scripts/parity_control` is what cron calls to run `mdcmd nocheck pause` and `check resume` for cumulative checks, and it only acts on a running parity check.

**Docker `Events.php`:** start, pause, stop, resume, restart, remove_container, remove_image, remove_all (container first, image only if that worked), returning `{success}` or `{error}`. **Docker pages exist only while dockerd runs** (`Cond`), and AddContainer releases the PHP session lock so a long create doesn't block other tabs.

**Small features:** Favorites (heart on any Settings or Tools panel; trash on the Favorites page). Boot Device tab: GUID, TPM status, Boot device backup ZIP. Boot Mode: UEFI/Legacy display, Permit UEFI checkbox on flash boots, "change in BIOS" text on internal boot. System Drivers: table with search, All/In Use filter, per-module modprobe text editing, Rebuild Modules, reboot notice. Hardware Profile: Show Details, Upload to vendor. Processes: `ps -aux`. Vars: dump of all state arrays. Page Map: the whole menu tree. Tailscale: an installer stub for its plugin. VM Usage Statistics: only if enabled and libvirt runs; live table over its own channel. Connect: compiled settings plus registration, remote access, flash backup Activate/Deactivate/Delete, with a note that the backup isn't encrypted.

**File manager job bar:** while a job runs, the file-manager icon and progress text sit in the footer on every page. Minimising the dialog leaves the job running; clicking the icon reopens it.

**Coverage (2026-11-09):** Static reading only; nothing was exercised on a live Unraid server.
- **All 113 `.page` files in `dynamix/` plus those in the docker, VM, plugin, UPS, search and my.servers plugins were opened.** Fully read except as listed below. `helptext.txt`, `update.php`, `DockerContainers.php`, `CreateDocker.php`, `docker.js`, `VMMachines.php`, `vmmanager.js`, `device_list`, `Boot.php`, `Report.php`, `ParityControl.php`, `parity_control`, `notify`, `Events.php`, and `BodyInlineJS`/`HeadInlineJS` are also complete.
- **Read only in part:** `DisplaySettings.page` (field labels), `CPUvms.page` (to the start of its table scripts), `ManagementAccess.page` (certificate blocks at the end cut off), `OutgoingProxy.page` (first 90 lines), `SyslogSettings.page` and `PHPsettings.page` (first ~105 lines). Lines over about 170 characters were clipped when reading the larger pages.
- **Structure only, not line by line:** `WG0.page`/`WGX.page` (fields, dialogs and function list; `validateForm`, keypair and peer add/remove partly), `BootParameters.page` (layout and all action functions; not its config-parsing routines), `Connect.page`, `Custom.form.php` (fields and function names), the eth0 VLAN markup (same as the fields already listed), `VMajax.php` (action list), `monitor` after line ~250, `BrowseButton.page` after line ~130 (behaviour covered via `Browse.page`), `Control.php` after the `start` case, `update_container` (start only), theme CSS (variables only).
- **Not read:** other `include/*.php` endpoints (e.g. `Helpers.php`, `ShareList.php`, `SmartInfo.php`, `Browse.php`), `nchan/*` other than `device_list`, `default-base.css` and `default-dynamix.css`.
- **Not readable:** `emhttp` (compiled), so `cmd*` internals are inferred. The compiled Vue components (`unraid-*`: header, notifications, Update OS, API keys, log viewer, onboarding). Community Applications ships separately. Vendored libraries were skipped. The mover shell wrapper is not in this tree; it was read from `bzmodules` on 2026-11-10 and written up in §31. `libexec/unraid/shfs` and `/usr/libexec/unraid/move` are stripped binaries and were not disassembled.

*Section 30 added 2026-11-09 from static reading of the Unraid 7.3.4 web UI source; nothing was exercised on a live Unraid server.*


## Part C — Unraid management scripts (original CLAUDE.md §31.1–§31.2)
### 31.1 What was read

Section 30 is the web UI in `bzmodules-emhttp/`. The programs that perform the work live in the `bzmodules` squashfs (`unraid-extracted/bzmodules`, mounted as `/usr` on a running Unraid). These were extracted to a temp directory and read there:

| Path inside the squashfs | What it is |
|---|---|
| `local/sbin/mdcmd` | 12 lines. Read in full. |
| `local/sbin/mover` | The mover. Read in full. |
| `local/sbin/emhttp_event` | Plugin event dispatcher. Read in full. |
| `local/sbin/sdspin` | ATA spin up/down. Read in full. |
| `local/sbin/powerdown` | Deprecated. Read in full. |
| `local/etc/rc.d/rc.samba` | Samba start/reload. Read in full. |
| `local/etc/rc.d/rc.S.cont` | Boot continuation. First 120 lines. |
| `local/etc/rc.d/rc.M` | Multi-user start. First 120 lines. |

`libexec/unraid/shfs` is a stripped ELF. `/usr/libexec/unraid/move` (the program `mover` pipes file names into) was not extracted. `local/sbin/emhttp` is the compiled daemon. `diagnostics`, `emcmd`, `newperms` and `plugin` are symlinks into the emhttp tree already covered in §30. `rc.docker` (856 lines), `create_network_ini`, `monitor_interface`, `use_ssl` and `vfio-pci` were listed and not read.

### 31.2 How Unraid actually does these jobs

**Parity.** `mdcmd status` prints `/proc/mdstat`. Every other command is written to `/proc/mdcmd` for the custom `md` driver. There is no userspace parity engine. Helix's SnapRAID path is a separate design and this read does not change it.

**Array lifecycle hooks.** `emhttp` calls `emhttp_event` and waits until it returns. The argument is one of `startup`, `driver_loaded`, `starting`, `array_started`, `disks_mounted`, `svcs_restarted`, `docker_started`, `libvirt_started`, `started`, and the matching `stopping_libvirt`, `stopping_docker`, `stopping_svcs`, `unmounting_disks`, `stopping_array`, `stopped`. `poll_attributes` fires after every SMART poll. Plugins drop executables in `/usr/local/emhttp/plugins/*/event/<name>/`. Helix has no equivalent hook list; array start in `helix-array-start` mounts disks, starts mergerfs, mounts cache, starts Docker with its data-root on the cache, then runs `helix-shares-apply`.

**Early boot (`rc.S.cont`).** Restores `/etc/machine-id` from `/boot/config/machine-id`. Runs `vfio-pci` before udev and copies `vfio-pci.cfg` to `vfio-pci.boot`. Installs custom udev rules from `/boot/config/udev`. Mounts cgroup v2 unless the kernel command line says `unraidcgroup1`. Then `swapon -a`.

**`powerdown`** logs that it is deprecated, then `reboot` (`-r`) or `init 0`.

**Spin.** `sdspin <dev> up|down|status` is `hdparm -S0`, `-y`, or `-C`. ATA only. Exit 0 means up (or the action worked), 2 means standby. Helix drive spin is still the API in `routers/drives.py`; this was not reimplemented.

**Samba.** `rc.samba` rebuilds `/etc/samba/smb-names.conf` from `/boot/config/ident.cfg` on every start and reload: hostname as the NetBIOS name, comment, hide-dot-files, multi-channel, max open files, mDNS registration off (it clashes with emhttp). NetBIOS on forces the minimum protocol to NT1 and optionally a local master at os level 100; off forces SMB2. Active Directory writes a different block (realm, winbind, ACL inherit). Otherwise security is USER, `map to guest = Bad User`, `smbpasswd`, create mask 0777. Bind-to-interfaces is applied when set. Reload uses `smbcontrol all reload-config`. A restart happens only when the interface list changed. `secrets.tdb` is copied to and from the boot flash. Helix still generates its own Samba config in `helix-samba-apply` and does not grow this ident.cfg surface.

**Mover.** This is the behaviour Helix was missing. The script reads `/boot/config/shares/*.cfg`:

- `shareUseCache=yes` moves that share from its primary pool to the secondary (another pool, or the array disks `/mnt/diskN/<share>`).
- `shareUseCache=prefer` moves the other way, secondary back to primary.
- `only` and `no` are not moved.
- Top-level files on a pool or disk are never moved. Hidden top-level directories are skipped unless `dotglob` is turned on in the script (it ships commented out).
- A file `fuser` reports as open, or that backs a loop device, stays. The comment says a failed copy deletes the partial target and keeps the source, and a file that already exists on the target is left alone on both sides.
- A second pass deletes empty directories left behind.
- `shareUserEmptying` in `/boot/config/share.cfg` is a comma list of disks to empty. Anything still there is logged, truncated to 100 paths.
- The copy is not rsync. `find` prints paths into `/usr/libexec/unraid/move`.
- A pid file at `/var/run/mover.pid` refuses a second copy. `mover stop` signals the process tree.

Cache is not part of the user-share union. Helix already matches that: `helix-array-start` mounts cache at `/mnt/cacheN` and does not put those paths in the mergerfs branch list. Writing a moved file to `/mnt/user/<share>` therefore lands on a data disk. The bug was not "rsync copies the cache onto itself".

