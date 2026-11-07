# Unraid OS Deep Dive — Findings & Emulation Plan

**Source:** Unraid 7.3.4 (Linux 6.18.58-Unraid) — decompiled from `unRAIDServer-7.3.4-x86_64.zip`  
**Purpose:** Understand Unraid internals so we can emulate its capabilities with open-source technology.

---

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

## 12. Emulation Plan — What We Build

We want to replicate the Unraid experience on existing Linux hardware (this Raspberry Pi / any x86 server) without proprietary components.

### Component mapping

| Unraid feature | Emulation technology |
|---------------|---------------------|
| Boot from USB, run from RAM | Standard Debian/Ubuntu Live or PXE, tmpfs overlayfs |
| bzroot / bzmodules / bzfirmware | Standard distro packages, squashfs optionally |
| emhttp management daemon | **Custom — build in Python/Go or use Cockpit** |
| Parity array (shfs + custom md) | **mergerfs** (user shares) + **SnapRAID** (parity) |
| Storage pools | ZFS (OpenZFS), Btrfs, XFS — native Linux |
| Docker UI | Portainer or custom UI over Docker socket |
| VM management | Cockpit-machines or virt-manager or custom over libvirt |
| Samba shares | Samba (identical package) |
| NFS shares | nfs-kernel-server (identical) |
| Web UI | Cockpit (base) + custom panels OR full custom Next.js/FastAPI |
| Plugin system | Ansible roles / apt packages / Docker containers |
| Avahi/mDNS | avahi-daemon (identical) |
| WireGuard VPN | wireguard-tools + wg-easy UI |
| Flash backup | Git + rclone to any cloud provider |
| Unraid API | Custom FastAPI GraphQL or REST |
| UPS monitoring | apcupsd or NUT |
| Drive monitoring | smartmontools + Prometheus + Grafana |
| Parity check / rebuild | SnapRAID sync/scrub commands |

### The critical insight: mergerfs + SnapRAID = Unraid array
```
/mnt/disk1   /mnt/disk2   /mnt/disk3   (independent XFS volumes)
      ↓             ↓            ↓
           mergerfs → /mnt/user     (unified share view, like shfs)
           SnapRAID → parity file on /mnt/parity  (one large drive)
```
This gives us mixed-size drive support, per-file access (no striping), and single/dual drive failure recovery — the exact same behavior as Unraid's array.

### Architecture decisions
1. **Base OS:** Debian 12 (bookworm) — stable, well-supported, all packages available
2. **Boot:** Standard boot (not USB-only RAM boot needed for initial emulation)
3. **Web UI:** We'll build custom — a Next.js frontend + FastAPI backend talking to system daemons over sockets/CLI
4. **Configuration store:** `/boot/config/` equivalent → `/etc/unraid-emu/` (plain INI/TOML files, git-tracked)
5. **Init:** systemd (not SysVinit — we don't need Slackware compatibility)

---

## 11. Files & Directories of Interest (Extracted)

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

## 12. Next Steps

1. **Set up mergerfs + SnapRAID** on this machine to prove the array concept
2. **Build a minimal web UI** (dashboard + array status + share management)
3. **Wire up Docker management** via the Docker socket
4. **Wire up Samba/NFS** config generation from our UI
5. **Port the config model** — replicate the `/boot/config/` INI-based config structure
6. **Implement drive health** — smartctl polling + display
7. **Plugin/app system** — Docker-compose-based "apps" like Unraid's Community Applications
8. **Notifications + UPS** — apcupsd / NUT integration

---

---

## 13. Monitoring Integration — Checkmk Automated Setup

### Why Checkmk Community Edition

Checkmk Community (CRE) is GPL v2, free, no host limit in practice, and ships a full monitoring stack (agent, check plugins, alerting, dashboards). It is better suited for homelab-scale infrastructure monitoring than Prometheus/Grafana for the operational use case (service up/down, drive health, container health).

### Architecture

```
┌─────────────────────────────────────────────────────┐
│  Host machine                                        │
│  ├── checkmk-agent  (port 6556)                      │
│  │     auto-discovers: CPU, RAM, disks, NFS, Samba,  │
│  │     systemd services, running processes            │
│  ├── mk_docker.py plugin (in agent plugin dir)       │
│  │     discovers all running containers as services  │
│  └── docker-event-bridge daemon (Python, ~100 lines) │
│        watches: docker events --filter type=container│
│        on start  → REST API: create host + discover  │
│        on destroy → REST API: delete host            │
└─────────────────────────────────────────────────────┘
                    │ REST API (port 5000)
                    ▼
┌─────────────────────────────────────────────────────┐
│  Checkmk container  (checkmk/check-mk-community)    │
│  ├── Web UI          http://<host>:5000/cmk/         │
│  ├── REST API        /cmk/api/1.0/                   │
│  └── Livestatus TCP  port 6557 → our dashboard       │
└─────────────────────────────────────────────────────┘
```

### What auto-discovers without user intervention

| Item | Auto? | Mechanism |
|------|-------|-----------|
| Host CPU / RAM / swap | Yes | checkmk-agent |
| Disk usage + I/O | Yes | checkmk-agent |
| NFS mounts | Yes | checkmk-agent |
| Samba status | Yes | checkmk-agent |
| systemd service health | Yes | checkmk-agent |
| Network interfaces | Yes | checkmk-agent |
| Docker containers (health/status) | Yes | mk_docker.py + event bridge |
| New containers on first run | Yes | event bridge daemon |
| SMART drive health | Partial | mk_smartmon.py plugin |
| HTTP endpoint checks | Manual | user adds in Checkmk UI |

### Checkmk REST API endpoints used by event bridge

```
POST   /cmk/api/1.0/domain-types/host_config/collections/all
         body: {"host_name": "<container_id>", "folder": "/"}

POST   /cmk/api/1.0/domain-types/service_discovery_run/collections/all
         body: {"host_name": "<container_id>", "mode": "fix_all"}

POST   /cmk/api/1.0/domain-types/activation_run/collections/all
         body: {"sites": ["cmk"], "force_foreign_changes": true}

DELETE /cmk/api/1.0/objects/host_config/<container_id>
```

### Docker event bridge daemon (sketch)

```python
#!/usr/bin/env python3
# /usr/local/sbin/cmk-docker-bridge
import subprocess, json, requests, os

CMK = os.getenv("CMK_URL", "http://localhost:5000/cmk/api/1.0")
AUTH = ("automation", os.getenv("CMK_AUTOMATION_SECRET"))

def api(method, path, data=None):
    r = getattr(requests, method)(f"{CMK}{path}", json=data, auth=AUTH,
                                   headers={"Content-Type":"application/json"})
    return r.status_code, r.json() if r.content else {}

def register(name):
    api("post", "/domain-types/host_config/collections/all",
        {"host_name": name, "folder": "/"})
    api("post", "/domain-types/service_discovery_run/collections/all",
        {"host_name": name, "mode": "fix_all"})
    api("post", "/domain-types/activation_run/collections/all",
        {"sites": ["cmk"], "force_foreign_changes": True})

def deregister(name):
    api("delete", f"/objects/host_config/{name}")
    api("post", "/domain-types/activation_run/collections/all",
        {"sites": ["cmk"], "force_foreign_changes": True})

proc = subprocess.Popen(
    ["docker","events","--filter","type=container",
     "--filter","event=start","--filter","event=destroy","--format","{{json .}}"],
    stdout=subprocess.PIPE)
for line in proc.stdout:
    ev = json.loads(line)
    name = ev["Actor"]["Attributes"].get("name","")
    if ev["Action"] == "start":   register(name)
    if ev["Action"] == "destroy": deregister(name)
```

### Dashboard integration via Livestatus

Pull live status into our web UI over Livestatus TCP (port 6557):

```python
import socket, json

def livestatus_query(query: str) -> list[dict]:
    s = socket.socket()
    s.connect(("localhost", 6557))
    s.sendall((query + "\nResponseHeader: fixed16\nOutputFormat: json\n\n").encode())
    # read 16-byte header, then body
    header = s.recv(16).decode()
    length = int(header[6:15])
    data = b""
    while len(data) < length:
        data += s.recv(4096)
    return json.loads(data)

# Example: get all service states
services = livestatus_query(
    "GET services\nColumns: host_name description state plugin_output\n")
```

### Setup steps (automated, runs once at first boot)

1. `docker run -d --name checkmk -p 5000:5000 -p 6557:6557 checkmk/check-mk-community:2.5.0-latest`
2. Install `checkmk-agent` on host + open port 6556
3. Copy `mk_docker.py` to `/usr/lib/check_mk_agent/plugins/`
4. Create automation user in Checkmk → save secret to `/etc/unraid-emu/checkmk_secret`
5. Register the host machine itself via REST API (one-time)
6. Start `cmk-docker-bridge` as a systemd service
7. Configure Livestatus TCP in Checkmk (`omd config set LIVESTATUS_TCP on`)

---

## 14. OS Name — Helix OS

**Decision: The project is named Helix OS.** (Note: "Hive OS" was rejected — that name is taken by a GPU mining OS.)

Community vote artifact (archived): https://claude.ai/artifact/ANsuepjJkGamUBsFxV5fNj

Seed names considered: Forge, Nexus, Bastion, Polaris, Stratum, Alloy, Cobalt, Manifold, Prism, **Helix**, Canopy, Shard, Lynx, Crate.

---

---

## 15. Completed Feature Implementations (2026-11-05)

### Web UI / Dashboard (webui/)
All features implemented in `webui/frontend/index.html` (artifact: https://claude.ai/artifact/LhTs8i8k3cQCWhbKnCjVVy) and `webui/backend/`.

| Feature | Backend | Frontend |
|---------|---------|---------|
| Themes: Black, Dark Gray, Azure, White, Light | CSS tokens | Settings → Appearance (swatches + accent picker, persisted to localStorage) |
| API Keys | `routers/apikeys.py` | Settings → API Keys (create, list, revoke) |
| Dynamic DNS | `routers/ddns.py` | Settings → DDNS (DuckDNS, Cloudflare, No-IP, custom) |
| FTP server | `routers/ftp.py` | Settings → FTP (vsftpd config gen, start/stop) |
| OS Updates | `routers/updates.py` | Settings → OS Updates (check apt, apply with live SSE stream) |
| VFIO GPU passthrough | `routers/vfio.py` | VMs → GPU Passthrough panel (IOMMU status, assign/unassign to VM) |
| Cache mover | `routers/mover.py` | Array Controls → Cache Mover (Move Now, Dry Run, Stop, SSE log) |
| Hardware profile | `routers/hardware.py` | Hardware page (CPU/memory/board, PCI devices, governor) |
| Drive spin-down | `routers/drives.py` | Drive tile 💤 button |
| Drive identify | `routers/drives.py` | Drive tile 📍 button (ledctl or model/serial info) |
| Drive format | `routers/drives.py` | Drive tile ⚙ button (XFS/Btrfs/ext4, safety check) |
| Drive pre-clear | `routers/drives.py` | Drive tile (background dd zero-fill with progress) |
| Drive spin-down timer | `routers/drives.py` | Drive tile ⏱ button (hdparm -S value picker) |
| Avahi/mDNS toggle | `routers/network.py` | Network settings |
| File browser | `routers/files.py` | Files page (browse, upload, mkdir, delete, rename) |
| GPU AI compute | `gpu/setup-ai-gpu.sh` | GPU → Docker flags modal |
| GPU overclocking | `routers/gpu.py` | GPU → Overclock tab (power, clocks, fan, perf, reset) |

### New backend routers added (2026-11-05)
- `webui/backend/routers/apikeys.py` — API key management
- `webui/backend/routers/ddns.py` — DDNS (DuckDNS/Cloudflare/No-IP/custom)
- `webui/backend/routers/ftp.py` — vsftpd config generation
- `webui/backend/routers/updates.py` — apt OS updates with SSE streaming
- `webui/backend/routers/vfio.py` — VFIO/IOMMU GPU passthrough for VMs
- `webui/backend/routers/mover.py` — cache mover (rsync with SSE)
- `webui/backend/routers/hardware.py` — CPU/memory/board/PCI/governor

### HTTPS / SSL management (DONE 2026-11-05)
- `webui/backend/routers/ssl.py` — self-signed gen, PEM upload, Let's Encrypt (certbot), nginx conf gen
- Frontend: Settings → HTTPS/SSL card (status, generate, upload, Let's Encrypt, enable/disable toggle)

### Custom banner image (DONE 2026-11-05)
- Frontend only: Settings → Custom Header card (header background color picker + banner image upload via FileReader, persisted to localStorage)

### Drive Rebuild (DONE 2026-11-05)
- `GET /api/array/rebuild/stream` — SSE stream of `snapraid fix -e` output
- `POST /api/array/rebuild/stop` — terminate running rebuild
- Frontend: Array Controls → Drive Rebuild section with live log and Stop button

### TOTP 2FA (DONE 2026-11-05)
- `webui/backend/routers/auth.py` — pure-Python TOTP (no pyotp dependency), session tokens, setup/enable/disable/verify/logout
- Frontend: Users page → Two-Factor Authentication card (QR setup via qrcode.js, 6-digit confirm, disable-with-code)

### Recycle Bin Browse (DONE 2026-11-05)
- `GET /api/files/recycle` — scans all share `.recycle` dirs, returns flat file list sorted by date
- `POST /api/files/recycle/restore` — moves file back to share root (adds `_restored` suffix on collision)
- `POST /api/files/recycle/purge` — permanently deletes one file or empties all recycle bins
- Frontend: "🗑 Recycle Bin" button in Files header → slide-in panel with restore/delete/empty-all actions

### Root-required setup (DONE 2026-11-05)
- Ran: `sudo bash -c "bash array/test/fix-and-run.sh && bash checkmk/setup.sh"`
- Array live test: loop devices, XFS format, parity sync verified
- Checkmk agent: installed from container deb, mk_docker.py installed, cmk-docker-bridge systemd service enabled

### Additional Features (DONE 2026-11-05)

#### 1. Backup Jobs
- `webui/backend/routers/backup.py` — rsync / rclone jobs with SSE log streaming
- Full CRUD: create/edit/delete, run now, stop, live log
- Frontend: Backup page (nav), job cards with run/stop/edit/delete, job create modal

#### 2. ZFS Pool UI
- `webui/backend/routers/zfs.py` — pool list/create/destroy, scrub start/stop, snapshot CRUD, rollback
- Frontend: ZFS Pools page (nav), pool cards with health badge, snapshot expand table, create pool modal

#### 3. Network Traffic History
- `webui/backend/routers/vnstat.py` — vnstat hourly/daily/monthly stats + SSE live samples
- Frontend: VPN page → Network Traffic History card with Canvas bar chart (RX/TX, period selector)

#### 4. Reverse Proxy Manager
- `webui/backend/routers/proxy.py` — nginx route CRUD, config generation, reload
- Frontend: Rev. Proxy page (nav), route table with SSL/WS flags, apply/reload button

#### 5. Audit Log
- `webui/backend/routers/audit.py` — middleware logs all POST/PUT/DELETE to `STATE_DIR/audit.jsonl`
- `AuditMiddleware` registered in `main.py`
- Frontend: Settings → Audit Log card (last 100 entries, clear button)

#### 6. Multi-user Login
- `webui/backend/routers/login.py` — PBKDF2-SHA256 passwords, 7-day sessions, role-based (admin/viewer)
- If no accounts exist → open access; first account enables gating
- Frontend: Settings → Login Accounts card (create/delete/change password)

#### 7. Wake-on-LAN
- `webui/backend/routers/wol.py` — raw UDP magic packet via Python socket, saved device list
- Frontend: VPN page → Wake-on-LAN card (saved devices + direct MAC send)
- LAN scan results → "Add WoL" button to save discovered devices

#### 8. Drive Health Trending
- `webui/backend/services/drive_history.py` — hourly SMART poll, 7-day rolling history in `STATE_DIR/drive_history.json`
- `history_poll_loop()` started in lifespan alongside drive health poll
- `GET /api/drives/{name}/history` endpoint added to drives router
- Frontend: Drive tiles → 📈 button opens history modal with Canvas line chart (temp + reallocated sectors)

#### 9. PWA Manifest
- `GET /manifest.json` endpoint in `main.py` returns Web App Manifest
- Frontend: manifest link + theme-color meta injected dynamically on load
- Enables "Add to Home Screen" on mobile and standalone window on desktop

#### 10. LAN Device Scanner
- `webui/backend/routers/scanner.py` — nmap `-sn` ping sweep with SSE streaming, host/MAC/vendor parsing
- Results cached in `STATE_DIR/scanner_hosts.json`
- Frontend: LAN Scan page (nav), subnet input, live log stream, host table with "Add WoL" per row

#### 11. GPU OC Profiles
- `webui/backend/data/gpu_profiles.json` — 50+ profiles covering NVIDIA GTX 900/1000/1600, RTX 2000/3000/4000, AMD RX 400/500/Vega/RDNA1/RDNA2/RDNA3, Intel Arc/iGPU; Eco/Balanced/Performance/OC variants per family
- `webui/backend/routers/gpu.py` — added `GET /api/gpu/profiles` (filtered per detected GPU), `POST /api/gpu/overclock/{idx}/profile` (apply profile by id; calculates actual W/MHz from % × current state)
- Frontend: GPU → Overclock tab now shows "Compatible OC Profiles" above manual controls; one-click Apply button per profile; result message per GPU

#### 12. CPU Overclocking
- `webui/backend/routers/cpu_oc.py` — CPU detection (vendor/model/generation via `/proc/cpuinfo` + CPUID), profile matching, apply/custom/reset/live SSE
- `webui/backend/data/cpu_profiles.json` — 40+ profiles for AMD Zen/Zen+/Zen2/Zen3/Zen4, FX Bulldozer/Piledriver, Intel 2nd–14th gen (Sandy Bridge through Raptor Lake); each profile has Eco / Balanced / Performance / OC variants
- Implementation: kernel governor via `/sys/devices/system/cpu/cpu*/cpufreq/`, Intel RAPL power limits via powercap sysfs, AMD boost via `/sys/devices/system/cpu/cpufreq/boost`, Intel P-state via intel_pstate sysfs, C-states via `cpupower idle-set`
- Frontend: Hardware page → "CPU Overclock" tab — CPU status card with live SSE per-core frequency bars + temp, profile cards with Apply button, custom settings form, reset button

**All features complete as of 2026-11-05.**

---

## 16. Over-the-Air OS Updates from GitHub (2026-11-06)

Helix OS updates itself by swapping the boot-image set on the boot partition and
rebooting — the same model as Unraid's USB `bz*` replacement. Because the OS runs
from a read-only squashfs in RAM, **settings** live on `/boot/config/`, and
**apps + data** live on the array/pools, an update never touches user data.

### How it works
1. A release is published to the GitHub repo (`yo-repo87/helix-os`) with a
   `helix-release.json` manifest asset plus per-arch boot files named
   `<arch>-filesystem.squashfs`, `<arch>-vmlinuz`, `<arch>-initrd.img`.
2. The in-OS updater reads `/etc/helix-version`, queries the GitHub Releases API,
   and compares versions. For a **private** repo it uses a read-only token saved
   in `/boot/config/osupdate.cfg` (set via the UI); it drops the auth header on
   GitHub's S3 asset redirect.
3. On apply it downloads the three files for the machine's architecture, verifies
   sha256 against the manifest, then the privileged helper stages them on `/boot`
   as `*.new`, rotates the current set to `*.prev`, and swaps in the new set.
4. Reboot loads the new image. If a swap was interrupted or the new squashfs fails
   to mount, the initramfs automatically boots the `*.prev` image. Manual rollback
   is available in the UI.

### Components
- `webui/backend/routers/osupdate.py` — API: `/api/osupdate/{current,check,apply,stream,status,rollback,reboot,settings}`
- `webui/backend/helpers/helix-osupdate` — privileged `/boot` swap helper (installed to `/usr/local/sbin`, run via sudo); the only thing that writes to the boot device
- `build/initramfs/init` — `.prev` fallback on interrupted/failed update
- Frontend: Settings → "Helix OS Update" card (check / update with live log / reboot / rollback / repo+token settings)
- `/etc/helix-version` stamped from `build/config/VERSION` at build time

### Publishing an update (maintainer side)
```bash
# Build the images first (build/build-all.sh), then per architecture:
sudo build/make-update-bundle.sh --arch amd64 --from output-amd64/boot --version 0.1.1
sudo build/make-update-bundle.sh --arch arm64 --from output-arm64/boot --version 0.1.1
GH_TOKEN=<token> build/publish-update.sh --repo yo-repo87/helix-os   # creates release v0.1.1
```
Bump `build/config/VERSION` for each release so running systems detect the new version.

---

## 17. Project State & Quick Resume (2026-11-06)

Run **`./launch.sh`** to resume the most recent Claude Code session for this project.

### Where things live
- **GitHub repo:** `github.com/yo-repo87/helix-os` — **public** (made public
  2026-11-06). Auth PAT is in `./gitkey` (gitignored) and used for pushes/releases;
  `gh auth setup-git` wires it into git. Git remote `helix-os` points here; `origin`
  still points at the old `template` repo and is unused.
- **Branches:** `main` (baseline + OTA, **PR #1 merged** 2026-11-06) ·
  `fix/live-boot-storage-drivers` (live-boot driver fix, **PR #2** — see §18).
- **Releases:** `images-2026.11.06` (full installer images) · `v0.1.0` (OTA update
  bundle: per-arch boot files + `helix-release.json`).
- **Flashable images** (repo root, not in git — too large):
  `helix-os-20261106.iso` (x86_64, UEFI+BIOS) and
  `helix-os-pi-20261106.img.xz` (arm64, Pi 4/5). Also GitHub Release assets.
- **Excluded from git:** the proprietary Unraid 7.3.4 source (`unRAIDServer-*.zip`,
  `unraid-extracted/`, `bzroot-fs/`, `bzmodules-emhttp/`), the images, and `gitkey`.
- **Build scratch:** `/tmp/helix-build` → `/home/pi/helix-work/build` (symlinked off
  the small tmpfs so debootstrap/squashfs don't OOM on this RAM-tight host where
  `llama-server` holds ~6.5 GB).

### Build recipes
```bash
sudo build/build-all.sh          # amd64 ISO + arm64 Pi image (debootstrap+squashfs; arm64 via qemu)
# individually:
sudo build/build.sh --arch amd64 && sudo build/make-iso.sh
sudo build/build.sh --arch arm64 && sudo build/make-pi-image.sh
```
The custom initramfs (`build/initramfs/`) sources a static busybox + kernel modules
from the *target rootfs* so it is architecture-correct for both amd64 and arm64.

### Run the web UI locally (dev)
```bash
cd webui && sudo ./install.sh                 # creates .venv, installs helix-webui.service
# or ad-hoc:
webui/.venv/bin/uvicorn main:app --app-dir webui/backend --host 0.0.0.0 --port 8082
```

### Publish an OS update (see §16)
```bash
# bump build/config/VERSION, build images, then per arch:
sudo build/make-update-bundle.sh --arch amd64 --from output-amd64/boot --version X.Y.Z
sudo build/make-update-bundle.sh --arch arm64 --from output-arm64/boot --version X.Y.Z
GH_TOKEN=$(cat gitkey) build/publish-update.sh --repo yo-repo87/helix-os
```

### GitHub ops
```bash
GH_TOKEN=$(cat gitkey) gh <command>           # e.g. gh pr view 1 --repo yo-repo87/helix-os
```
Note: inside a git worktree the shell sandbox refuses `export GH_TOKEN=$(cat …)`; run
GitHub commands from the main checkout, or let the scripts read `./gitkey` themselves.

### Open caveats / next steps
- OTA validated live except the physical **reboot-into-new-image** step.
- A read-only token pasted into Settings → Helix OS Update (stored at
  `/boot/config/osupdate.cfg`) lets a running Helix OS fetch updates (needed while
  the repo/release is private; harmless when public).

---

## 18. Live-Boot Device-Driver Fix (2026-11-06)

First boot attempt (Proxmox) hung at `[helix] Searching for boot device
(label=HELIXBOOT, timeout=30s)…`. Root cause: the live-boot initramfs bundled only
filesystem modules (squashfs/overlay/vfat/isofs) and **no block-device controller
drivers**, so the kernel never saw the HELIXBOOT device on a VM disk/CD, USB stick,
or SD card. A second latent bug hard-mounted the boot device as `vfat`, which fails
on the iso9660 ISO.

**Fixed** (`build/initramfs/`, PR #2 → `fix/live-boot-storage-drivers`):
- `build-initramfs.sh`: bundle SCSI/SATA/NVMe/VirtIO/USB/MMC drivers. `install_module`
  now matches `modules.dep` with `-`/`_` treated interchangeably — kernel module
  **file** names use `-` (`usb-storage.ko`, `xhci-hcd.ko`, `sdhci-iproc.ko`) while
  module names use `_`, so the first attempt silently skipped every dash-named driver
  (USB + SD among them).
- `init`: `modprobe` the controllers before the device scan and let probing settle;
  mount the boot device by auto-detect then `iso9660`/`vfat` fallback; treat a
  read-only boot medium (ISO/CD) as non-fatal; dump visible block devices on failure;
  rename the leftover `[hive]` tag / `/run/hive` paths to `[helix]` / `/run/helix`.

Rebuilt both images and verified the initramfs now contains the drivers
(amd64: usb-storage, uas, xhci/ehci/ohci/uhci-hcd, sr_mod, ahci, nvme, virtio_blk,
sdhci — 59 modules). Covers boot from CD/DVD, USB, SATA, VirtIO, NVMe, and SD.
Build scratch was also moved off the `nodev` tmpfs (`/tmp/helix-build` →
`/var/tmp/helix-build`) so the arm64 cross-build's `mkinitramfs` can create device
nodes under qemu.

---

## 19. Networking Bring-Up + Console IP Banner (2026-11-06)

**No network / no DHCP lease.** `helix-config-network.service` runs
`/usr/local/sbin/helix-apply-network`, but that script was never installed into
the image — `build.sh` copied only `webui/` into the chroot, and no hook installed
the `network/` helpers. The service failed at boot, so no systemd-networkd
`.network` units were written and the box never requested DHCP (nothing appeared
on the router). The shipped `/boot/config/network.cfg` was also in a different
format (`IFACE=`/`DHCP=`) than `helix-apply-network` parses (`[network]` INI with
`IFNAME`/`USE_DHCP`), so its values were ignored. NIC drivers were never the
problem — the squashfs carries Debian's full module set (virtio_net, e1000/e1000e,
r8169, igb, tg3, vmxnet3, … 448 net drivers).

Fixes:
- `build.sh` copies the helper trees (`network/`, `array/`, `shares/`, `gpu/`)
  into the chroot.
- `03-helix-install.sh` installs `/usr/local/sbin/helix-apply-network` and ships
  `99-helix-dhcp-fallback.network` (DHCP on `en*`/`eth*`) so the box is reachable
  out of the box even if the config-driven path hasn't run; the config-driven
  `10-helix-*.network` units take precedence.
- `build/usb-layout/config/network.cfg` rewritten to the `[network]` INI format.

**Console IP banner.** New `/usr/local/sbin/helix-console-banner` +
`helix-console-banner.service` (after `network-online.target`) write `/etc/issue`
with the hostname, each interface's IPv4, and the web UI URL (`http://<ip>/`)
*before* the login prompt, and restart `getty@tty1` so the address shows
immediately. Helix OS has no console password, so this is how you find the box.

---

## 20. Web UI 502 — Backend Dependency Install Fix (2026-11-06)

The web UI returned **502 Bad Gateway** (nginx up, backend down). Cause: during the
build, `systemd-resolved`'s postinst repoints `/etc/resolv.conf` at a stub that
doesn't exist inside the chroot, so `pip install` in `03-helix-install.sh` had no
DNS (`Temporary failure in name resolution` → `No matching distribution found for
fastapi`). `apt` had already downloaded its packages, so only `pip` broke. The
result: no `fastapi`/`uvicorn` in `/opt/helix/venv`, `helix-backend.service`
couldn't start, and nginx proxying to `127.0.0.1:8082` returned 502.

Fixes:
- `build.sh` writes a working `/etc/resolv.conf` (1.1.1.1/8.8.8.8) in the chroot
  before apt and again before the hooks run, then restores the systemd-resolved
  symlink for the shipped image.
- `03-helix-install.sh` now treats a failed `pip install` (or a failed
  `import fastapi, uvicorn`) as **fatal** — the build aborts instead of shipping
  an image whose web UI 502s.

---

## 21. Web UI 502 (round 2) + HTTPS (2026-11-07)

**Still 502 after the deps fix.** With fastapi/uvicorn installed, the app still
failed to import: routers use FastAPI Form/upload routes, which require
`python-multipart` — not in `requirements.txt`. FastAPI raises at route-build
time, so `uvicorn main:app` crashed and nginx returned 502. The earlier build
check only imported `fastapi, uvicorn`, not the app, so it missed this.

Fixes:
- `requirements.txt`: add `python-multipart`.
- `03-helix-install.sh`: the build-time check now imports the **actual app**
  (`cd /opt/helix/backend && python -c "import main; assert main.app"`), exactly
  what the service does — so any missing router dep fails the build, not the boot.

**HTTPS (self-signed).** nginx now serves both **:80 (HTTP)** and **:443 (HTTPS)**;
no forced redirect, so `http://` still works. `/usr/local/sbin/helix-gen-cert`
mints a per-machine self-signed cert (RSA-2048, 10 yr, SAN = hostname + current
IPs) at first boot, run as `nginx.service` `ExecStartPre` so the cert always
exists before nginx binds 443. It persists to `/boot/config/ssl/` when writable
and regenerates in RAM on a read-only ISO/CD boot. `openssl` added to
packages-base; the build validates the config with `nginx -t`. Browsers show a
one-time self-signed warning; real certs can be managed later via Settings →
HTTPS/SSL (`routers/ssl.py`). Let's Encrypt is not used (no public domain on a
LAN-by-IP appliance).

*Last updated: 2026-11-07. Source: direct binary analysis of unRAIDServer-7.3.4-x86_64.zip.*
