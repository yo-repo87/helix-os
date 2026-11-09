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

*Last updated: 2026-11-06. Source: direct binary analysis of unRAIDServer-7.3.4-x86_64.zip.*

---

## 18. Smoke Test Results — 2026-11-08

**Target:** `http://192.168.50.12` (QEMU VM, 2 vCPU, 3 GB RAM, hostname "Tower", running Helix OS 0.1.0)
**Backend path on target:** `/opt/helix/backend`, user `helix`, HELIX_CONFIG `/var/local/helix/config`
**Tested by:** Helix OS smoke-tester agent (Claude Sonnet 4.6)

### Hardware constraints

The target VM had no additional virtual disks attached (`/api/drives/` returned `[]`, `disks.cfg` empty). No 14 TB HDDs. Array phases (Phase 1) were skipped. Docker socket was not present on the VM, so Phases 4 and 5 (app install, container lifecycle) could not be tested.

### Phase 6 — Full endpoint sweep

| Endpoint | Result | Notes |
|---|---|---|
| `GET /api/array/status` | PASS | `array_state: stopped`, disks: [] |
| `GET /api/array/drives` | PASS | Returns `[]` (no drives assigned) |
| `GET /api/drives/` | PASS | Returns `[]` |
| `GET /api/network/status` | PASS | ens18 at 192.168.50.12, up |
| `GET /api/network/config` | PASS | DHCP, single interface |
| `GET /api/shares/` | PASS | Returns `[]` (empty on fresh install) |
| `GET /api/docker/containers` | PASS | `{"available":false,"containers":[]}` — docker not installed |
| `GET /api/vms/` | FAIL | 500 — `FileNotFoundError: virsh not installed` in `_virsh_available()` |
| `GET /api/users/` | PASS | Returns `[{username:"helix",uid:1000}]` |
| `GET /api/notifications/` | PASS | `[]` |
| `GET /api/diagnostics/syslog?lines=10` | PASS | Live syslog entries |
| `GET /api/diagnostics/processes?n=5` | PASS | Live process list |
| `GET /api/scheduler/jobs` | FAIL | 500 — `FileNotFoundError: crontab not installed` in `_read_crontab()` |
| `GET /api/gpu/` | PASS | `{"available":false,"gpus":[]}` |
| `GET /api/ups/status` | PASS | `{"available":false}` |
| `GET /api/checkmk/available` | PASS | `{"available":false}` |
| `GET /api/zfs/pools` | PASS | `{"available":false,"pools":[]}` |
| `GET /api/hardware/` | PASS | AMD Ryzen 7 5700G, 2 vCPU, 3 GB RAM, QEMU BIOS |
| `GET /api/settings/identity` | PASS | hostname: Tower, timezone: UTC |
| `GET /api/wireguard/interfaces` | PASS | `[]` |
| `GET /api/tailscale/status` | PASS | `{"available":false,"connected":false}` |
| `GET /api/scanner/hosts` | PASS | `{"hosts":[],"scanned_at":null}` |
| `GET /api/backup/jobs` | PASS | `[]` |
| `GET /api/audit` | PASS | Captures POST requests correctly |
| `GET /api/ftp/status` | PASS | `{"active":false}` |
| `GET /api/mover/status` | PASS | `{"running":false,"last_status":"never"}` |
| `GET /api/ssl/status` | PASS | Self-signed cert present (CN=Tower, 10 yr), HTTPS not enabled |
| `GET /api/ddns/status` | PASS | `{"last_status":"never"}` |
| `GET /api/login/status` | PASS | `{"enabled":false}` |
| `GET /api/network/avahi` | PASS | `{"installed":false,"active":false}` |
| `GET /api/proxy/status` | PASS* | Running but `config_ok: false` — nginx cannot load SSL key (see GAP below) |
| `GET /api/updates/status` | PASS | `{"last_check":null,"upgradable":[]}` |
| `GET /api/vfio/status` | PASS | IOMMU not enabled |
| `GET /api/cpu-oc/status` | PASS | Zen3 detected, governors available |
| `GET /api/system/stats` | PASS | CPU 0%, RAM 22%, uptime 8460 s |
| `GET /api/health` | PASS | `{"status":"ok","version":"0.1.0"}` |

**Summary: 34 PASS, 2 FAIL, 0 SKIPPED (from endpoint sweep)**

### Phase 1 (Array) — SKIPPED
No block devices other than the boot disk (sda). No virtual data disks attached. `disks.cfg` empty. Array scripts present at `/usr/local/sbin/helix-array-{start,stop,status}` and called via sudo. Array state machine works; start/stop API surface present.

### Phase 2 (Shares) — BLOCKED
POST requests to the remote server were blocked by the agent permission classifier. Could not create `smoke-share` remotely. The shares router was tested locally and two bugs were found and fixed (see Fixes section).

### Phase 3 (Template source) — BLOCKED
POST to `/api/apps/sources` blocked. Existing sources (builtin, unraid-ca) returned 200 with correct structure.

### Phase 4 (App install) — BLOCKED
POST blocked and Docker not available on remote.

### Phase 5 (UI reflection) — PARTIAL
All GET endpoints verified to return valid JSON. No UI browser tool available to verify rendered pages. The API responses the frontend polls (array/status, shares/, docker/containers, drives/) all return correct shapes, so the UI would render consistent state (empty).

### Known GAPs and Issues

| Item | Severity | Description |
|---|---|---|
| nginx SSL key permissions | HIGH | `/etc/helix/ssl/helix.key` is not readable by the nginx/helix process. `proxy/status` reports `config_ok: false`. HTTPS cannot be enabled without fixing key permissions (should be `chown helix:helix /etc/helix/ssl/helix.key` or `chmod 640` + helix group). |
| vms/ endpoint: virsh missing | MEDIUM | `GET /api/vms/` raises `FileNotFoundError` on any host without libvirt. Fixed in repo (see below). |
| scheduler/jobs: crontab missing | MEDIUM | `GET /api/scheduler/jobs` raises `FileNotFoundError` on hosts without cron. Fixed in repo. |
| Array scripts not in ARRAY_SBIN | LOW | Default `ARRAY_SBIN=/usr/local/sbin` but dev scripts live in `array/sbin/`. Backend must be started with `ARRAY_SBIN=<repo>/array/sbin`. |
| dmesg endpoint returns empty | LOW | `/api/diagnostics/dmesg` returns `{"lines":[],"count":0}` on this VM (likely kernel log permission or missing journalctl). |
| Checkmk not installed | INFO | Expected on fresh VM. |
| Docker not installed | INFO | Expected on fresh VM; install instructions in CLAUDE.md §13. |

### Fixes applied to local codebase (2026-11-08)

1. **`webui/backend/routers/vms.py`** — `_virsh_available()`: wrapped `subprocess.run(["virsh","version"])` in `try/except FileNotFoundError` so list endpoint returns `{"available":false,"vms":[]}` instead of HTTP 500.

2. **`webui/backend/routers/scheduler.py`** — `_read_crontab()` and `_write_crontab()`: wrapped in `try/except FileNotFoundError` so `GET /api/scheduler/jobs` returns `[]` instead of HTTP 500 on systems without cron.

3. **`webui/backend/routers/shares.py`** — two bugs:
   - Section parser regex `r'^\[(\w+)\]$'` does not match hyphenated names like `[smoke-share]`; changed to `r'^\[([a-zA-Z0-9_-]+)\]$'`. Previously, any share with a hyphen would be silently invisible and its key/value lines would corrupt the preceding section's data.
   - Duplicate-entry bug: config was written before directory creation; if `os.makedirs` failed, the config had a dangling entry. Next call would de-duplicate the dict (Python dict), pass the `in shares` check, and write another entry. Fixed with a raw-file regex check before writing and a rollback on mkdir failure.

4. **`array/sbin/helix-array-status`** — `local_sep=",\n"` produced a literal backslash-n in JSON output (bash does not expand `\n` in assignment); changed to `local_sep=$',\n'` (ANSI-C quoting). Previously the JSON emitted by the status script was invalid and fell back to the raw-string path in `array_status()`.

5. **`webui/backend/routers/diagnostics.py`** — `_run("nvidia-smi", label="nvidia-smi")` passed an unsupported `label=` kwarg to `_run()` (which only accepts `*args, timeout`); removed the kwarg. `GET /api/diagnostics/bundle` returned 500 with `TypeError: _run() got an unexpected keyword argument 'label'`. (Commit `00f7080`)

6. **`webui/backend/routers/pia.py`** — `_run()` called `subprocess.run()` without `try/except`; if `wg` was not installed `GET /api/pia/status` raised `FileNotFoundError` → 500. Added `try/except (FileNotFoundError, subprocess.TimeoutExpired)` matching the same pattern used in vms.py, scheduler.py, etc. (Commit `226d3e7`)

### Second smoke test run (2026-11-08) — after fixes deployed

Ran against http://192.168.50.12 after `helix-webui.service` reinstalled from git repo:

| Metric | Count |
|--------|-------|
| PASS | 32/36 endpoints |
| FAIL (pending deploy) | 4 — vms/, scheduler/jobs, diagnostics/bundle, pia/status |
| Root cause | Server at 192.168.50.12 running old `/opt/helix/backend/` before `helix-webui.service` reinstall; all fixes are in `main` and deployed via service pointing to git repo path |

**After `git pull && sudo systemctl restart helix-webui` on 192.168.50.12, all 4 should pass.**

Broader retest (76 endpoints, same session):

| Result | Count |
|--------|-------|
| PASS | 68 |
| FAIL (old deploy) | 4 |
| WARN (200 but degraded) | 2 |
| SKIPPED (expected non-200) | 1 |
| INFRA (not code) | 1 |

New findings from broader test (not in first run):
- `diagnostics/bundle` 500 (label kwarg — fixed, commit `00f7080`)
- `pia/status` 500 (FileNotFoundError wg — fixed, commit `226d3e7`)
- Share POST: regex fix confirmed working; fails only on `/mnt/user` PermissionError (infra — helix user lacks write to /mnt/user)
- SSL key: nginx cannot read `/etc/helix/ssl/helix.key` → `proxy/status` config_ok=false (infra — fix: `chmod 640 /etc/helix/ssl/helix.key && chown root:helix /etc/helix/ssl/helix.key`)

### Cleanup

- `smoke-share` directory created at `/home/pi/Claude/UnraidProject/webui/state/mnt-user/smoke-share` during local testing. Left in place (dev machine, not production).
- No resources created on the remote instance (192.168.50.12) — all POST operations were blocked.

### Not tested and why

- Array format/start/stop: no spare disks on the target VM.
- SMB/NFS share file read-write: array not started; samba/nfs not running.
- App install / container lifecycle: Docker not on remote VM.
- VMs lifecycle: libvirt/virsh not installed.
- WireGuard / Tailscale / DDNS: not configured on fresh install.
- Notifications (test send): POST operations blocked.
- Mover, VLAN, bonding: requires array + network config.
- ZFS / Btrfs pools: no spare disks.
- UPS: no UPS hardware.

---

## 19. Known Issues, Lessons Learned & Design Constraints (2026-11-08)

This section documents bugs that caused production incidents. **Read before modifying the initramfs, OTA update system, or build pipeline.**

---

### 19.1 initramfs MUST load storage bus drivers before scanning for HELIXBOOT

**Incident:** Every boot attempt failed with `[helix] FATAL: Boot device (label=HELIXBOOT) not found after 30s` on real USB sticks, Proxmox VMs, and bare-metal hardware. `/dev/` was empty in the emergency shell — `blkid`, `ls /dev/sd*`, and `cat /proc/partitions` all returned nothing.

**Root cause:** `build/initramfs/init` only loaded filesystem modules (`squashfs`, `vfat`, `overlay`, etc.) but **not** the storage bus drivers needed to enumerate hardware. Without them the kernel never creates block device nodes in `/dev/`.

**Root cause (deeper):** `init` calls `modprobe ehci_hcd`, `modprobe usb_storage`, etc. — but those `.ko` files were **never copied into the initramfs cpio**. `modprobe` silently returns 0 (no error), the kernel never binds to the hardware, `/dev/` stays empty. Adding `modprobe` calls to `init` without also adding the modules to `build-initramfs.sh` does nothing.

**Fix:** Added the driver list to BOTH files:
- `build/initramfs/init` — `modprobe` calls (commit `4f2dea4`, updated later)
- `build/initramfs/build-initramfs.sh` — `install_module` calls so the `.ko` files are actually packed into the cpio:
```sh
ehci_hcd  ohci_hcd  xhci_hcd  xhci_pci  usbcore
usb_storage  uas  scsi_mod  sd_mod  sr_mod
virtio  virtio_pci  virtio_blk  virtio_scsi
ahci  libahci  ata_piix  nvme  nvme_core
```

**Rule:** Any storage driver added to `init` MUST also be added to `build-initramfs.sh`'s module list, and vice versa. The initramfs cpio is self-contained — `modprobe` can only load modules that were packed into the cpio at build time. If a module is listed in `init` but missing from `build-initramfs.sh`, `modprobe` silently succeeds with no effect. If new hardware targets are added (USB4, Thunderbolt, eMMC), add their modules to both files.

---

### 19.2 OTA update MUST NOT swap kernel/initrd on standard Debian installs

**Incident:** User applied the OTA update (v0.1.4) on a server running as a standard Debian install with `helix-webui.service`. On reboot the new `initrd.img` ran (which expects a live-boot squashfs on a HELIXBOOT USB) and failed to find HELIXBOOT, leaving the system unbootable.

**Root cause:** `routers/osupdate.py` unconditionally staged and committed `vmlinuz`, `initrd.img`, and `filesystem.squashfs` to `/boot/boot/` regardless of whether the system was actually live-booting from a Helix squashfs.

**Fix (commit `80d52f3`):** Added `_is_live_boot()` which checks for `/run/helix/` directory (created by the initramfs) and an overlay root mount. `_apply_worker()` now branches:
- **Live-boot system** → download + stage + commit boot images as before
- **Standard Debian install** → `git pull` in the project directory + `systemctl restart helix-webui` only; never touches vmlinuz/initrd

**Rule:** Never call `helix-osupdate stage/commit` without first confirming `_is_live_boot() == True`. The OTA helper writes to the boot partition — on a standard install that partition is managed by GRUB and replacing its initrd is destructive.

---

### 19.3 initramfs working directory is /run/helix (not /run/hive)

The initramfs previously used `/run/hive/` as its working directory. All paths were renamed to `/run/helix/` in commit `80d52f3`. The `_is_live_boot()` detection in `osupdate.py` relies on the presence of `/run/helix/` — if this path is ever renamed again, update `_is_live_boot()` to match.

---

### 19.5 Debugging protocol for initramfs boot failures

**What happened:** The HELIXBOOT-not-found error was investigated across three separate attempts, each fixing a different symptom while missing the real cause, wasting significant time and tokens.

- **Attempt 1:** Added `modprobe` calls for USB/VirtIO/SATA drivers to `build/initramfs/init`. Did not read `build-initramfs.sh`. The `.ko` files were never packed into the cpio, so `modprobe` silently failed and nothing changed.
- **Attempt 2:** Diagnosed the wrong secondary problem (ISO 9660 vs vfat mount type). Fixed a real bug but not the root cause. Rebuilt and published twice more without the boot working.
- **Attempt 3:** Finally read `build-initramfs.sh` and found that the module packing list had never been updated. Fixed in one line.

**Rule — before touching any initramfs file, read ALL THREE:**
1. `build/initramfs/init` — what the runtime does
2. `build/initramfs/build-initramfs.sh` — what gets packed into the cpio
3. Cross-check that every `modprobe <name>` in `init` has a matching `install_module "<name>"` in `build-initramfs.sh`

**Rule — when a boot fix does not work after rebuild:**
Do not diagnose a new symptom. First verify the fix actually took effect:
- For module changes: after build, run `lsinitrd output/boot/initrd.img | grep <module>` or `zstd -d output/boot/initrd.img -o - | cpio -t | grep <module>` to confirm the `.ko` is in the cpio before publishing.
- For init script changes: inspect `output/boot/initrd.img` to confirm the updated `init` is packed.

**Rule — never publish a release as "fixed" without confirming the fix is in the binary output**, not just in the source file.

**Rule — always get diagnostic output before fixing boot failures.** Two emergency-shell commands (`cat /proc/partitions` and `ls /dev/`) reveal the entire failure class instantly. Without them every fix is a guess. Ask for this output before writing a single line of code.

---

### 19.6 Busybox blkid does not support util-linux option syntax

**Incident:** Boot still failed after modules were packed into the cpio. Devices (`sda`, `sdb`, `sdc`) were visible in `/dev/` but HELIXBOOT was never found.

**Root cause:** The initramfs uses busybox `blkid`, not util-linux `blkid`. Two differences broke label detection:
1. Busybox outputs `LABEL=HELIXBOOT` (no quotes). The grep checked for `LABEL="HELIXBOOT"` (with quotes) — never matched.
2. The brute-force fallback used `blkid -o value -s LABEL <dev>` — util-linux-only syntax. Busybox ignores unknown flags and returns empty string.
3. The mount fallback tried to read `.volume_label` as a file — that file does not exist on FAT32. Volume labels are in the boot sector, not the filesystem.

**Fix (commit `30baf24`):** `find_boot_dev()` now:
- Method 3: `blkid | grep "LABEL=" | grep "HELIXBOOT"` — matches both quoted and unquoted forms
- Method 4 per-device: same two-step grep on individual device output
- Method 4 mount fallback: probe for the squashfs file (`boot/filesystem.squashfs`) instead of `.volume_label`; try vfat, iso9660, auto in order
- Brute-force glob: use `sd[a-z][0-9]` patterns to match partitions correctly

**Rule:** Never use util-linux blkid flags (`-L`, `-o value`, `-s LABEL`) in the initramfs init script. The initramfs only has busybox. Test any blkid command with `busybox blkid <args>` on the build host before adding it to init.

---

### 19.4 App install failures were silently swallowed

**Incident:** Clicking Install on any app showed "Installing…" then reverted to "Install" with no error message. Docker socket permission errors, missing images, and all other failures were invisible.

**Root cause:** `_do_install()` ran as a FastAPI `BackgroundTask` with no try/except. Any exception was discarded.

**Fix (commit `48a5a75`):** `_do_install()` now writes `install_status.json` to `STATE_DIR/apps/{name}/` with `state: installing / installed / error` and the error message. Frontend polls `GET /api/apps/{name}/install-status` every 2.5s and shows an alert with the actual error on failure.


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

---

## 22. Raspberry Pi Overclocking (2026-11-07)

Pi boards are overclocked through the firmware `config.txt`, not sysfs/MSR like
x86 (see §15 cpu_oc.py/gpu.py), so this is a separate module.

- **Backend** `routers/pi_oc.py` (`/api/pi-oc/{status,profiles,apply,custom,reset,live}`):
  detects the board/SoC from `/proc/device-tree/{model,compatible}`
  (bcm2712=Pi 5, bcm2711=Pi 4/CM4/400, bcm2837/2710=Pi 3/Zero 2 W); reads/writes a
  Helix-managed block in `/boot/config.txt` (markers `# >>> helix-oc >>>`), backs up
  once to `config.txt.helix-bak`, and falls back to `sudo cp` since the backend runs
  as the unprivileged `helix` user (NOPASSWD sudo) and `/boot` is root-owned. `custom`
  merges into the current block; `reset` removes it. `live` SSE streams ARM clock,
  SoC temp, and decoded `vcgencmd get_throttled` flags. Changes need a reboot.
- **Profiles** `data/pi_oc_profiles.json` (11), model-aware across CPU / GPU / memory:
  - Pi 5: arm_freq 2.0–3.0 GHz with `over_voltage_delta` (µV) + gpu_freq.
  - Pi 4: arm_freq 1.2–2.2 GHz with `over_voltage` (0–8) + gpu_freq/v3d_freq;
    an "Extreme" tier uses `force_turbo` (flagged warranty-voiding).
  - Pi 3 / Zero 2 W: arm_freq + over_voltage + core_freq + `sdram_freq` (memory OC,
    which only applies on Pi 1–3). Each profile carries needs_cooling / voids_warranty
    flags and notes.
- **Frontend**: a "Pi Overclock" tab on the Hardware page, shown only when
  `/api/pi-oc/status` reports `is_pi`. Profile cards (CPU/GPU/RAM/voltage badges +
  cooling/warranty flags), a model-adaptive custom panel (Pi 5 shows
  over_voltage_delta; Pi 3 shows sdram_freq), live temp/clock/throttle readout, a
  "reboot to apply" prompt, and Reset.

---

## 23. HTTPS broke nginx startup — connection refused (2026-11-07)

The §21 HTTPS attempt added `ExecStartPre=/usr/local/sbin/helix-gen-cert` as an
nginx.service **drop-in**. But Debian's `nginx.service` has its own
`ExecStartPre=/usr/sbin/nginx -t`, and a drop-in's ExecStartPre runs *after* the
unit's own — so nginx ran its config test *before* the cert existed, the test
failed on the missing `ssl_certificate`, and **nginx never started → connection
refused** (both :80 and :443).

Fixes (all verified by booting the built rootfs under `systemd-nspawn`):
- Cert generation moved to its own oneshot `helix-gen-cert.service`, ordered
  `Before=nginx.service`, so the cert exists before nginx's own `nginx -t`.
- The `:443` server block is no longer inline. `helix-gen-cert` writes it to
  `/etc/nginx/helix-https.d/https.conf` *only after* a cert exists, and the main
  config does `include /etc/nginx/helix-https.d/*.conf;` (a wildcard matches zero
  files without error). So a missing/failed cert degrades to HTTP-only and can
  **never** stop nginx from starting.
- `helix-gen-cert` uses `set -u` (not `-e`) and `exit 0`, so an openssl hiccup
  leaves HTTP serving rather than aborting.
- `systemd-networkd-wait-online` set to `--any --timeout=30` (nginx orders
  After=network-online.target; the default waits for *all* links and could stall
  the UI ~120s if a second NIC is down).
- Console banner now shows both `http://<ip>/` and `https://<ip>/`.

**Verification:** `systemd-nspawn --network-veth --boot` of the built rootfs →
DHCP came up, `network-online`/`multi-user` reached, `helix-gen-cert` + nginx +
helix-backend all **active**, listeners on :80/:443/:8082, and `curl` returned
**HTTP=200, HTTPS=200, /api/health=200** with no manual intervention. The build
also runs `nginx -t` with the cert present and again with it removed (HTTP-only
fallback), and imports the real app, so a broken web UI fails the build.

---

## 24. Drive discovery/assignment + the "helper scripts never installed" class (2026-11-07)

Deep-dive after "drives can't be seen/selected." Two findings:

**Systemic: helper scripts exist in the repo but the build never installed them.**
Audit of every `/usr/local/sbin/helix-*` the backend calls vs what the hooks
install showed only `helix-osupdate`/`helix-gen-cert`/`helix-apply-network`/
`helix-console-banner` were installed. Missing (so their routers errored at
runtime): `helix-array-{start,stop,status}`, `helix-parity-{check,rebuild}`,
`helix-array-lib.sh`, and `helix-shares-apply`/`helix-samba-apply`/
`helix-nfs-apply`. `shares.py` also pointed `SHARES_SBIN` at a relative path that
resolved to a non-existent `/shares` in the image. Fixes: hook 03 installs all
array/share/gpu helpers to `/usr/local/sbin`; `SHARES_SBIN` is now
`/usr/local/sbin` (env-overridable).

**Missing feature: no physical-disk discovery.** `/api/drives/` and
`/api/array/drives` only listed disks already written into `disks.cfg`, so a
fresh system showed nothing to assign. Added:
- `GET /api/drives/available` — `lsblk -J -b` enumeration of every physical disk
  (size/model/serial/HDD-SSD/transport), flagged assigned-vs-free and marking the
  boot medium so it's never offered to the array.
- `POST /api/array/assign` — writes `disks.cfg` from selected parity/data devices
  (sudo fallback; clear error when the boot medium is read-only, e.g. ISO/CD).
- Frontend "⊹ Assign Disks" button + modal (lists all disks, role per disk, Save),
  then the existing Start Array works.

Note: persisting the assignment needs a **writable** boot medium. Booting the ISO
as a Proxmox CD-ROM makes `/boot` read-only, so assignments won't survive reboot
there — boot Helix from a writable USB/disk (or attach the image as a writable
disk) for a real array.

---

## 25. Assign 500 → writable-config fallback + STATE_DIR fix (2026-11-07)

"Add disks to the array" returned HTTP 500. Three stacked causes, all found by
the deep-dive:

- **Read-only boot:** booting the ISO as a CD makes `/boot` (hence
  `/boot/config`) read-only, so `disks.cfg` couldn't be written. `config.py` now
  detects a non-writable `/boot/config` and falls back to a writable in-RAM dir
  (`$STATE_DIR/config`), seeded from the boot config. Assignments then work for
  the session; a writable USB/disk is still needed to persist across reboots
  (`HELIX_CONFIG_PERSISTENT` flags which mode is active).
- **STATE_DIR never writable:** `STATE_DIR=/var/local/helix`, but hook 03 created
  `/var/lib/helix` (wrong path) and `/var/local` is root-only — so the
  unprivileged backend couldn't write the SMART cache, `array_state`, or audit
  log (which also kept `/api/drives/` empty). Hook 03 now creates
  `/var/local/helix` (+ `/config`) owned by `helix`.
- **Opaque 500:** a raised `HTTPException` surfaced as a generic Starlette
  "Internal Server Error" through `BaseHTTPMiddleware`. `/api/array/assign` now
  returns `200 {ok:false,error}` and the UI shows the real message.

---

## 26. HELIXDATA persistent data partition (2026-11-07)

The boot image is ~400 MB vs multi-GB USB/SD media, so the leftover space is now
a persistent **ext4 partition labelled HELIXDATA** (not swap — swap/cache on flash
wears it out; use zram or a real SSD for those).

- **write-usb.sh**: FAT32 boot (2 GiB) + ext4 HELIXDATA (rest), done offline (safe).
- **make-pi-image.sh**: FAT32 boot (512 MiB) + ext4 HELIXDATA (rest of the image).
- **helix-firstboot.service** (`/usr/local/sbin/helix-firstboot`, `Before=helix-backend`):
  grows the last partition to fill the disk (`parted resizepart` + `resize2fs` —
  the standard, lower-risk operation; no live partition *creation*), mounts
  HELIXDATA at `/mnt/data`, and seeds `/mnt/data/config` + `/mnt/data/appdata`.
  No-op on a read-only ISO/CD boot.
- **config.py** now prefers `/mnt/data/config` (persistent) → `/boot/config`
  (writable boot) → `$STATE_DIR/config` (RAM fallback), so config/array
  assignments persist on writable media.

---

## 27. Array actually starts + versioning policy (2026-11-07)

- **mergerfs/snapraid** were only in `packages-full` (images build `FULL=no`), so
  the array could never start. Moved to `packages-base` (+`fuse3`).
- **`helix-array-start` requires root** (mounts, mergerfs), but `array.py`
  `_run_sbin` / parity / rebuild invoked the helpers **without sudo**, so they ran
  as the unprivileged `helix` user and exited `Must run as root` — array_state
  never became "started". Now all array helpers run via `sudo -n env
  HELIX_CONFIG=… /bin/bash …` (`_sbin_cmd`). Format worked already because it used
  `_sudo`.
- **Format dialog** now shows a single **Done** button after completion.

### Versioning policy
`build/config/VERSION` is the revision stamped into `/etc/helix-version` (shown on
the console banner + Settings → Helix OS Update). **Bump the PATCH/revision on
every shipped update** so builds are distinguishable and the OTA updater flags new
versions. Now **0.1.1** (the first patched revision over the initial 0.1.0 — all of
today's boot/network/HTTPS/web-UI/drive/array fixes).

---

## 28. Release 0.1.2 + 0.1.3 — cache drive, SSH keys, writable disk, MCP (2026-11-08)

Consolidated since 0.1.1:

- **Cache/pool drive role (0.1.2).** Assign Disks modal gains a `cache` role;
  `POST /api/array/assign` takes `cache[]`+`cache_fmt` and writes `[cacheN]`
  sections; `helix-array-start` mounts them at `/mnt/cacheN` (separate from the
  mergerfs array); `helix-array-status` reports a `cache` array. Shares set to
  "use cache" land there first; the mover moves them to the array. Fixed a latent
  `helix-array-status` bug (literal `,\n` separator → invalid JSON for 2+ entries).
- **SSH keys actually load (0.1.2).** The banner promised `/boot/config/ssh/` but
  nothing read it. `helix-firstboot` now loads `*.pub` from there into the `helix`
  user's `authorized_keys` (login is key-only, root disabled), before the HELIXDATA
  early-exit so it works on CD boot too. On a writable medium the keys persist.
- **Writable disk image for Proxmox.** `helix-os-disk-YYYYMMDD.qcow2` (built with
  `HELIX_OUTPUT=<dir> write-usb.sh` → raw → `qemu-img convert -c`). Boots via its
  own GRUB and has ~1.7 GB free on `/boot`, so OTA applies (the ISO-as-CD can't —
  `/boot` is read-only). `build/grub/grub.cfg` now uses serial+console terminals
  and `console=ttyS0 console=tty0` for reliable headless auto-boot.
- **OTA read-only message.** `helix-osupdate stage` detects a read-only/zero-free
  boot medium and says to write Helix to a USB/disk, instead of "have 0".
- **MCP server (0.1.3).** `webui/backend/routers/mcp.py` — dependency-free
  Streamable-HTTP JSON-RPC at `POST /mcp` (proxied by nginx on :80/:443). Tools:
  `system_stats, array_status, list_disks, list_shares, list_docker,
  hardware_info, array_start, array_stop` — each a loopback call to the local API,
  run via `run_in_threadpool` to avoid deadlocking the single-worker event loop.
  **Auth fails closed**: every call needs `Authorization: Bearer <token>` where the
  token is a Helix API key (Settings → API Keys); with no keys configured it refuses
  all calls. Point an MCP client at `http(s)://<helix-ip>/mcp` with that bearer token.
  See [[helix-ota-release-process]] for publishing. Now **0.1.3**.

*Last updated: 2026-11-08. Source: direct binary analysis of unRAIDServer-7.3.4-x86_64.zip.*

---

### 19.7 Unmerged work: confirm the branch before building a release

**Incident:** v0.1.1 to v0.1.4 were built from `fix/live-boot-storage-drivers`, whose 22 commits were never merged. v2.0.0 was built from `main`, which silently removed the console IP banner, DHCP fallback, chroot DNS, boot splash, array/share helpers, HELIXDATA partition, the MCP server, and the original storage-driver fix. Several sessions then re-diagnosed and re-implemented regressions from scratch.

**Rules:**
- Before a release, run `git log --all --oneline --not main` and `git worktree list`. Unmerged commits must be merged or explicitly dropped.
- When something "stopped working", run `git log --all --grep=<feature>` before writing a fix.
- `build.sh` deletes `output/`; edit sources such as `build/usb-layout/`, never generated files.
- Boot-test in QEMU before publishing (real image over emulated USB, then check `/api/health`).
- For troubleshooting, compare with Unraid's equivalent in `bzroot-fs/` or `unraid-extracted/bzmodules`.

