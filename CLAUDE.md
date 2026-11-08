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

## 12. Emulation Plan — What We Build

We want to replicate the Unraid experience on existing Linux hardware (any x86 server or Raspberry Pi) without proprietary components.

### Component mapping

| Unraid feature | Emulation technology |
|---------------|---------------------|
| Boot from USB, run from RAM | Live-boot squashfs (see Section 16) |
| bzroot / bzmodules / bzfirmware | Standard distro packages packed as squashfs |
| emhttp management daemon | **Custom FastAPI backend** (`webui/backend/`) |
| Parity array (shfs + custom md) | **mergerfs** (user shares) + **SnapRAID** (parity) |
| Storage pools | ZFS (OpenZFS), Btrfs, XFS — native Linux |
| Docker UI | Custom UI over Docker socket (`routers/docker.py`) |
| VM management | Custom UI over libvirt (`routers/vms.py`) |
| Samba shares | Samba — config generated by `helix-samba-apply` |
| NFS shares | nfs-kernel-server — config by `helix-nfs-apply` |
| Web UI | FastAPI backend + single-page HTML frontend |
| Plugin system | Docker-compose-based "apps" |
| Avahi/mDNS | avahi-daemon (identical) |
| WireGuard VPN | wireguard-tools + UI in `routers/wireguard.py` |
| Flash backup | Git + rclone to any cloud provider |
| Helix API | FastAPI REST (`webui/backend/main.py`, port 8082) |
| UPS monitoring | apcupsd or NUT |
| Drive monitoring | smartmontools polled by `services/drive_history.py` |
| Parity check / rebuild | SnapRAID sync/scrub via `routers/array.py` |

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
2. **Boot:** Live-boot squashfs from USB (FAT32 labeled `HELIXBOOT`), run-from-RAM
3. **Web UI:** Custom FastAPI (port 8082) + single-page HTML frontend, proxied by nginx on port 80
4. **Configuration store:** `/boot/config/` on the USB FAT32 (same pattern as Unraid), env var `HELIX_CONFIG`
5. **Init:** systemd units — `helix-backend.service` starts the FastAPI backend
6. **Array scripts:** `array/sbin/helix-array-{start,stop,status}`, `helix-parity-{check,rebuild}`

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

## 15. Completed Feature Implementations

### Web UI / Dashboard (webui/) — completed 2026-11-05
All features implemented in `webui/frontend/index.html` (artifact: https://claude.ai/artifact/LhTs8i8k3cQCWhbKnCjVVy) and `webui/backend/`.

| Feature | Backend | Frontend |
|---------|---------|---------|
| Themes: Black, Dark Gray, Azure, White, Light | CSS tokens | Settings → Appearance |
| API Keys | `routers/apikeys.py` | Settings → API Keys |
| Dynamic DNS | `routers/ddns.py` | Settings → DDNS |
| FTP server | `routers/ftp.py` | Settings → FTP |
| OS Updates | `routers/updates.py` | Settings → OS Updates (SSE stream) |
| VFIO GPU passthrough | `routers/vfio.py` | VMs → GPU Passthrough panel |
| Cache mover | `routers/mover.py` | Array Controls → Cache Mover |
| Hardware profile | `routers/hardware.py` | Hardware page |
| Drive spin-down / identify / format / pre-clear / timer | `routers/drives.py` | Drive tile buttons |
| Avahi/mDNS toggle | `routers/network.py` | Network settings |
| File browser + Recycle Bin | `routers/files.py` | Files page + Recycle Bin panel |
| GPU AI compute | `gpu/setup-ai-gpu.sh` | GPU → Docker flags modal |
| GPU overclocking + OC profiles | `routers/gpu.py` | GPU → Overclock tab |
| HTTPS / SSL management | `routers/ssl.py` | Settings → HTTPS/SSL |
| Custom banner image | frontend only | Settings → Custom Header |
| Drive Rebuild | `routers/array.py` | Array Controls → Drive Rebuild |
| TOTP 2FA | `routers/auth.py` | Users → Two-Factor Authentication |
| Backup Jobs | `routers/backup.py` | Backup page |
| ZFS Pool UI | `routers/zfs.py` | ZFS Pools page |
| Network Traffic History | `routers/vnstat.py` | VPN page → Traffic History chart |
| Reverse Proxy Manager | `routers/proxy.py` | Rev. Proxy page |
| Audit Log | `routers/audit.py` + middleware | Settings → Audit Log |
| Multi-user Login | `routers/login.py` | Settings → Login Accounts |
| Wake-on-LAN | `routers/wol.py` | VPN page → WoL card |
| Drive Health Trending | `services/drive_history.py` | Drive tile → 📈 history modal |
| PWA Manifest | `main.py` | manifest link + theme-color meta |
| LAN Device Scanner | `routers/scanner.py` | LAN Scan page |
| CPU Overclocking + profiles | `routers/cpu_oc.py` | Hardware → CPU Overclock tab |

### Extended OC Profile Libraries — added 2026-11-06

**CPU profiles** (`webui/backend/data/cpu_profiles.json`) — expanded to 85 profiles:
- AMD Threadripper 1000/2000 (Zen/Zen+), 3000/Pro (Zen 2), 5000/Pro (Zen 3), 7000/Pro (Zen 4)
- AMD EPYC Naples 7001 (Zen 1), Rome 7002 (Zen 2), Milan 7003 (Zen 3), Genoa/Bergamo 9004/8004 (Zen 4)
- Intel 11th Gen Rocket Lake (fixed — was incorrectly mapped to Comet Lake)
- Intel Core X HEDT / Skylake-X / Cascade Lake-X
- Intel Xeon W Workstation, Intel Xeon Scalable server
- Detection: `_detect_generation()` in `cpu_oc.py` checks CPU name string for threadripper/epyc before CPUID to resolve family number overlap with desktop parts

**GPU profiles** (`webui/backend/data/gpu_profiles.json`) — expanded to 72 profiles:
- NVIDIA GTX 700/600 (Kepler), RTX 5000 Blackwell (2025), RTX Professional A-series, Data Center (T4/V100/A100/H100/L40S)
- AMD R9 200/300 (Hawaii/Tahiti/Fiji), RX 9000 RDNA 4 (2025)
- Intel Arc Battlemage B-series (2024)

### Project Rename — completed 2026-11-06
All files renamed from `hive-*` → `helix-*`. All internal references (`HIVE_CONFIG` → `HELIX_CONFIG`, `hive_log` → `helix_log`, etc.) updated across 60+ files. See git log for full scope.

---

## 16. Build System — Helix OS Installer Images

### Overview
`build/` contains scripts to produce two distributable images from a Debian bookworm base:

| Output | Script | Target |
|--------|--------|--------|
| `helix-os-YYYYMMDD.iso` | `build/make-iso.sh` | x86_64 USB / VM |
| `helix-os-pi-YYYYMMDD.img.xz` | `build/make-pi-image.sh` | Raspberry Pi 4B / 5 SD card |

### Build pipeline

```
build/build.sh [--arch amd64|arm64]
    └── debootstrap bookworm → /tmp/helix-build/rootfs
    └── apt-get install (packages-base.list)
    └── run hooks: build/hooks/01-base-config.sh
                  build/hooks/02-services.sh
                  build/hooks/03-helix-install.sh   ← installs webui/
    └── mksquashfs (zstd -9) → output/boot/filesystem.squashfs
    └── copy kernel → output/boot/vmlinuz
    └── build-initramfs.sh → output/boot/initrd.img
    └── grub-mkimage → output/EFI/BOOT/BOOTX64.EFI  (amd64 only)
```

### ISO (x86_64 — BIOS + UEFI hybrid)
```
build/make-iso.sh
    Requires: output/ from build.sh --arch amd64
    Tools: xorriso, isolinux, grub-efi-amd64-bin
    Boot: isolinux (BIOS) + embedded efi.img (UEFI) + isohybrid MBR
    Volume label: HELIXBOOT
    Boot params: boot=helix helix_label=HELIXBOOT
```

### Pi image (arm64)
```
build/make-pi-image.sh
    Requires: output/ from build.sh --arch arm64
    Tools: parted, mkfs.fat, xz
    Firmware: downloads start4.elf / fixup4.dat / DTBs from github.com/raspberrypi/firmware
    Layout: 2GB raw image, 512MB FAT32 HELIXBOOT partition
    Compatible: Raspberry Pi 4B, Pi 5 (bcm2711 / bcm2712 DTBs included)
    Output: .img.xz (compressed with xz -T0 --fast)
```

### Cross-compilation (arm64 on x86_64)
- Requires `qemu-user-binfmt` package (Ubuntu 26.04)
- binfmt registered with `F` flag — qemu-aarch64 loaded from host, no copy into chroot needed
- debootstrap runs directly with `--arch arm64`; second stage executes transparently

### Known build issues fixed
- `build.sh` was generating comma-separated package list; `apt-get install` requires space-separated. Fixed: `tr '\n' ','` → `tr '\n' ' '`
- chroot subshell lacked `set -e`; apt errors were silently swallowed. Fixed: added `set -e` to chroot `-c` string.

### Running the build
```bash
cd /home/pi/Claude/UnraidProject

# amd64 ISO
sudo bash build/build.sh --arch amd64 2>&1 | tee build-amd64.log
sudo bash build/make-iso.sh 2>&1 | tee iso.log

# arm64 Pi image (separate rootfs)
sudo rm -rf /tmp/helix-build
sudo bash build/build.sh --arch arm64 2>&1 | tee build-arm64.log
sudo bash build/make-pi-image.sh 2>&1 | tee pi.log
```

---

## 17. Remaining / Next Steps

The core web UI and backend are feature-complete. Remaining work:

1. **Complete the ISO / Pi image builds** — build scripts ready, package list bug fixed (2026-11-06); need a successful end-to-end build run
2. **Live-boot initramfs** — `build/initramfs/init` needs testing: squashfs mount → overlayfs → pivot_root flow
3. **Checkmk integration** — `checkmk/setup.sh` verified on host; wire Livestatus queries into the dashboard
4. **Notifications** — email (SMTP) and webhook (Discord/Slack/Telegram) backends not yet wired to real events
5. **Plugin/app store** — Docker-compose-based app catalog UI not yet built
6. **SHA256 image verification** — equivalent of Unraid's boot-time hash check not yet in initramfs

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
- **GitHub repo:** `github.com/yo-repo87/helix-os` — **private**. Auth PAT is in
  `./gitkey` (gitignored). Git remote `helix-os` points here; `origin` still points
  at the old `template` repo and is unused.
- **Branches:** `main` (project baseline) · `helix-osupdate` (OTA feature, **PR #1**,
  mergeable — merge it to land OTA + this section on `main`).
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
- Images verified structurally (arch, modules, bootloader) but **not boot-tested** on
  hardware/VM. A headless QEMU smoke test of the ISO is a good next step.
- OTA validated live except the physical **reboot-into-new-image** step.
- A **private** repo needs a read-only token pasted into Settings → Helix OS Update
  (stored at `/boot/config/osupdate.cfg`) for a running Helix OS to fetch updates.
- Merge **PR #1** to bring OTA + §16/§17 onto `main`.

*Last updated: 2026-11-06. Source: direct binary analysis of unRAIDServer-7.3.4-x86_64.zip.*
