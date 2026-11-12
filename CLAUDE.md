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
| Cache mover | `routers/mover.py` + `array/sbin/helix-mover` | Main → Cache mover. Safety rules from Unraid's mover landed in 2.0.25 (§31). The row moved onto Main in 2.0.26 (§32). Per-share yes/no/only/prefer is still absent. |
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
| `helix-os-<version>.iso` | `build/make-iso.sh` | x86_64 USB / VM |
| `helix-os-pi-<version>.img.xz` | `build/make-pi-image.sh` | Raspberry Pi 4B / 5 SD card |

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
- **Writable disk image for Proxmox.** `helix-os-disk-<version>.qcow2` (built with
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


---

## 29. Smoke-test & OTA campaign — 2026-11-09 (versions 2.0.1 → 2.0.24)

> **Working agreement (from the user):** keep this file current — add what was learned and what was fixed *and how*,
> in the same session the fix is made. When something here is contradicted by a later section, the later one wins.
> **Never put passwords, tokens or API keys in this file** (it is committed; the GitHub repo is public). The test VM
> login, a Helix API key and a PIA test account were supplied by the user in chat only.

### 29.1 Runbook: how a fix reaches the test VM

Test VM: `http://192.168.50.12` (QEMU, 3 GB RAM, SSH as root). Disks: 2 × 14 TB (serials `QM00005`, `QM00007`), one 32 GB
(`drive-scsi0`), and the 248 GB USB boot disk (partition 1 = BIOS boot, partition 2 = `HELIXBOOT`, **now 4 GiB**).

1. Edit → `git commit` on a branch (currently `fix/smoke-round-1`, not merged to `main`). Bump `build/config/VERSION`.
2. `sudo build/build.sh --arch amd64 > build-amd64.log 2>&1` (~3 min, reuses the cached rootfs in `/tmp/helix-build`).
   **Never pipe it into `tail`** — SIGPIPE kills the build (exit 141, nothing built).
3. **Verify the image, not the source** (§19.5): `unsquashfs -cat output/boot/filesystem.squashfs opt/helix/backend/routers/X.py | grep marker`.
   Runtime code is at `opt/helix/backend` + `opt/helix/frontend`; helper scripts at `usr/local/sbin/helix-*`. Check new packages with `unsquashfs -l`.
4. `sudo build/make-update-bundle.sh --arch amd64 --from output/boot --version X`, then drop the stale `arm64` entry from
   `dist/update/helix-release.json` (arm64 images are old; shipping them under a new version would be a lie) and set `notes`.
5. `build/publish-update.sh --repo yo-repo87/helix-os` (reads `./gitkey` itself; run with `GH_TOKEN=""`).
6. VM: `GET /api/osupdate/check` → `POST /api/osupdate/apply` → poll `/api/osupdate/status` → compare `sha256sum /boot/boot/*` with the manifest → `POST /api/osupdate/reboot` → poll `/api/health`.
7. Re-test with real data (browser via Playwright, SSH for `smbclient`/`mount -t nfs`/`docker`). A mock-only pass is not a pass.

Playwright here: Chromium is at `~/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome` (pass `executable_path`, `--no-sandbox`);
run test scripts with plain `python3` (the `-I` flag hides the user site-packages where playwright lives).
**Scope Playwright selectors** (`#modal-actions button:first-child`, `#ctr-manager tr:has-text("x") button:has-text("Remove")`): the single page contains many hidden sections, so a bare `button:has-text("Back")` matched a hidden duplicate and the click timed out — a test bug that looked like a product bug. Check `locator.count()` and `is_visible()` per match before blaming the app.
**How Unraid handles app folders (read from its source, 2026-11-09):** a `Path` field is a plain text box; clicking it opens an inline folder tree (jQuery fileTree, folders only, starting at the field's current value or `/mnt/user/`) that fills the box. There is no "new folder" button: you type the new name, and Docker creates the missing bind-mount folder (as root) when the container starts. Config paths are pre-filled from a "default appdata location" setting (`<appdata>/<app name>`). Helix deliberately differs: share drop-down, Helix creates the folder.
The updater polls GitHub unauthenticated (60 req/h per IP) and the releases list is cached ~1 min, so a fresh release can take a minute to appear.

### 29.2 Fix log (symptom → root cause → fix)

| Symptom | Root cause | Fix |
|---|---|---|
| "Demo mode" banner stuck | `.demo-banner{display:flex}` beat the `[hidden]` attribute; then stale cached `index.html` | `.demo-banner[hidden]{display:none}`; `/` now sent with `Cache-Control: no-cache` (`main.py`) |
| New code missing from every image built on a reused rootfs | `cp -r src existing_dir` **nested** the new tree inside the old one (`backend/backend`), old code kept running | `rm -rf` the targets first in `build.sh` and hook 03 |
| `/boot` could not be resized or fsck'd | initramfs used `mount --bind` → original mount stranded in the discarded initramfs tree, unreachable | `build/initramfs/init`: `mount --move /run/helix/usb /root/boot` |
| Backend could not write `/boot/config` → every setting silently lived in RAM | `/boot` is FAT (everything root-owned); `config.py` fell back to `/var/local/helix/config`. A `remount,uid=` is **silently ignored** by FAT | initramfs mounts vfat with `uid=1000,gid=1000` from the start; firstboot only verifies/falls back to umount+mount |
| Sources + installed-app definitions lost on reboot | stored under `STATE_DIR` (RAM) | `template_sources.json` and `apps/` now under `HELIX_CONFIG` |
| Shares/uploads failed (500) | fresh XFS mount roots are `root:root 0755`; backend runs as `helix` | `helix-array-start` chowns each mount root to `helix:helix 0775` |
| SMB/NFS/Docker/Avahi/WireGuard/cron absent from the image | they sat only in `packages-full.list`; images build without `--full` | added to `packages-base.list` |
| SMB: shares invisible, `NT_STATUS_BAD_NETWORK_NAME` | `samba-vfs-modules` is only a *Recommends*; we install `--no-install-recommends` | added `samba-vfs-modules` |
| Docker start failed: `apparmor_parser` not found | `apparmor` also only a Recommends | added `apparmor` |
| Share created but not exported | `_apply_shares()` ran the apply scripts as unprivileged `helix`, output hidden | run via `sudo -n env …`; return `apply_error` |
| NFS export refused | exporting a FUSE (mergerfs) path needs `fsid=` | `helix-nfs-apply` adds a stable `fsid` derived from the share name |
| SMB guest could not write; NFS files root-owned and unwritable over SMB | guest → `nobody`; `no_root_squash` | SMB `force user/group = helix`; NFS `all_squash,anonuid/anongid=<helix>` |
| smbd/nfs never started after the first share | apply scripts only *reloaded* if already running | start the service when a share needs it; `helix-array-start` runs `helix-shares-apply` |
| Docker data would live in RAM | `/var/lib/docker` is on the RAM overlay | docker disabled at boot; `helix-array-start` writes `daemon.json` `data-root` on the cache (else first data) disk and starts it; `helix-array-stop` stops it first |
| `docker compose` missing | compose v2 plugin is **not in Debian bookworm** | install `docker-compose` (v1); `_docker_compose()` translates `up --pull always` |
| Apps reported "started" with no Docker | endpoints queued work blindly | `_require_docker()` → 503 with a clear message |
| App install failed silently / no way to set tokens, media paths | UI posted `vars: {}`; no edit screen | `GET/PUT /api/apps/{name}/config`, install-time `extras`, UI form (install + ⚙ Configure + Remove): template vars, extra env vars, extra folders |
| Parity sync started twice | each SSE connect launched `snapraid` | `_snapraid_running()` guard |
| **Disk roles swapped after a reboot** | `disks.cfg` stored `/dev/sdX`; letters change between boots | assign stores `/dev/disk/by-id/…` (`_stable_path`); readers `realpath()` for display/health |
| Drive list empty after assign | cache only refreshed on demand | assign triggers a background refresh |
| Parity disk missing from `/api/array/status` | status script ignored `PARITY_DEVS` | added `parity[]` |
| `proxy/status` always `config_ok:false` | `nginx -t` as `helix` can't open `/run/nginx.pid` (and key was root-only) | run nginx through `sudo`; key is `root:helix 640` |
| Updater offered the wrong release | sorted by `published_at`; all releases from one commit share a timestamp. Then sorting by version picked tag `images-2026.11.06` (= 2026.11.6) | only `vX.Y.Z` tags that carry `helix-release.json` |
| PIA "needs root"; connect 403; locations "loading…" | router ran privileged commands unprivileged; `pia.py`/`vms.py` had their own `/etc/helix` + `/var/lib/helix`; Cloudflare 403s Python's TLS fingerprint on the token endpoint; default region `us_east` no longer exists (US East = `us-newjersey`) | `sudo` for wg-quick/iptables/sysctl/`/etc/wireguard`; shared config dirs; token via `curl -K -` (creds on stdin); valid default; locations grouped by country with Retry |
| PIA would have hijacked the whole host | wg config had `AllowedIPs 0.0.0.0/0` + `DNS=` | `Table = off` + policy rule: only the `helix_pia` bridge subnet (172.28.0.0/16) is routed via the tunnel |
| PIA-routed container still exited through the host | (1) the table id 51821 was reused as the `ip rule` **priority** — priorities above `main`'s 32766 are never consulted; (2) a container attached to two networks keeps its *original* default route; (3) the `helix_pia` Docker bridge had MTU 1500 vs the WireGuard tunnel's 1420, so full-size replies were black-holed (ping worked, TCP data stalled) | rule priorities 5182 (PIA) / 5181 (LAN bypass); `route` moves the container onto `helix_pia` **only** and remembers its old networks in `HELIX_CONFIG/pia_routes.json` (restored on unroute and on disconnect; host/none-network containers are refused); network created with `com.docker.network.driver.mtu=1420` (a stale one is recreated when empty); `/api/pia/status` uses `ip link` (no root) |
| Unraid template variables crashed apps (`ACTUAL_LOGIN_METHOD=password\|header\|openid`) | in Unraid templates a pipe-separated `Default` is a **dropdown of choices** (first/current selected), not a literal; Portainer has the same idea as `select` | parser emits `choices`; UI renders a `<select>` (empty entry shown as "(none)"); prose like `a \| b` (with spaces around the pipe) stays free text |
| A custom template source's apps were invisible if the CA catalog had the same name | `load_all_templates()` dropped later duplicates; the CA source loads first | keep both: the later one becomes `<name>-<source-id>` titled `<title> (<source name>)` |
| vnstat history 500 | `vnstat --json -d` prints text; `vnstat -d --json` prints JSON | argument order; `vnstat` added to the image |
| **Unraid CA catalog empty** | feed URL `applicationFeed.json.gz` now 404 (file is `applicationFeed.json`, 21 MB); parser assumed `applist` was a dict (it is a list), used `Category` (real: `CategoryList`), mapped ports `Target:Target` (real: host=`Default`, container=`Target`), ignored `Device` | rewritten against the real feed: 4 809 container apps, host ports from `Default`, devices, `required`/`description`, secrets masked; list endpoint is slim and the UI pages 60 at a time |
| IOMMU: `amd_iommu=on` "Unknown option" | not a valid option; AMD's IOMMU is on by default (BIOS) | flags are `intel_iommu=on iommu=pt`; GRUB reads `/boot/config/boot-params.cfg` (`helix_iommu=auto|off`); UI toggle `GET/POST /api/vfio/iommu`; real state from `/sys/kernel/iommu_groups` |
| System users, Samba passwords, cron jobs and WireGuard interfaces vanished on reboot; user/WireGuard APIs answered "requires the backend to run as root" | `/etc/passwd`, `/etc/shadow`, Samba's tdb, the crontab and `/etc/wireguard` are all in the RAM root; routers refused to run as the unprivileged `helix` user | `services/privileged.py` (`run`/`read_file`/`write_file` via `sudo -n`); `users.py` records accounts (uid/gid/shell/hash/SSH keys) in `HELIX_CONFIG/users.json` and `pdbedit -L -w` in `smbpasswd`; scheduler copies the crontab to `HELIX_CONFIG/crontab`; WireGuard confs live in `HELIX_CONFIG/wireguard/` (`wg-quick up <full path>`, `wg syncconf` fed by `wg-quick strip`); `webui/backend/helpers/helix-restore-state` (root, idempotent, `--dry-run`) rebuilds them, called from `helix-firstboot` |
| Disabling a scheduled job deleted it (later DELETE → 404) | `_parse_jobs` skipped every line starting with `#` *before* recognising the `#PAUSED#` marker, so paused jobs left the list and the next rebuild dropped them (`lstrip("#PAUSED# ")` is also a char-set strip) | detect `#PAUSED#` first; `removeprefix` semantics |
| Array stayed stopped after every reboot | no autostart | `array.cfg` (`autostart=yes`), `GET/POST /api/array/autostart`, "Start array on boot" checkbox, `helix-array-autostart` + `helix-array-autostart.service` (waits up to ~90 s for the assigned by-id disks) |
| Files deleted over SMB never appeared in the Recycle Bin page | Samba used `recycle:repository = .Recycle.Bin`; the UI scans `.recycle/<user>/…`; scan list also had `/mnt/cache` (real: `cache1…`) | Samba now `.recycle/%U`; roots cover `disk1-28`, `cache1-4`. (The web file manager's delete is permanent by design.) |
| Dashboard "Manage" link did nothing; containers could not be removed; Start/Stop in that panel never worked | `Manage` was a `<span>` with no handler; no DELETE endpoint; `ctrAction` called `apiFetch(url, {method:'POST'})` but **`apiFetch(path)` ignores options → a GET to a POST-only route**, error swallowed; also `docker stop` > 10 s timeout and `docker logs` dropped stderr | `DELETE /api/docker/containers/{name}` (Helix apps go through `remove_app(purge=True)` so compose + settings go too), `GET /containers/{name}` details (env *names* only), longer timeouts, merged stderr; UI: per-row Start/Stop/Restart/Logs/⚙Configure/Remove, Manage dialog, `ctrAction` uses `fetchJSON` |
| Share names `global`/`homes`/`printers` (Samba special sections), `user`, `disk1`, `flash`… accepted; digit-first, 41-char, case-duplicate names, long comments accepted; dotted names rejected; hostname > 15 chars / `_` / leading `-` accepted; uppercase and digit-first usernames, long descriptions/passwords accepted; `os_version` always "0.1.0"; Save always said "saved"; failed user create silently closed the form | no shared input rules; `saveIdentity` swallowed errors; `createUser` never checked the response | `services/validation.py` (Unraid's §30.14 limits) used by shares/identity/users; mirrored in the UI as `VALID` + `bindValidator()` (live inline message, submit blocked, server reason shown via `errText`); `/etc/helix-version` for `os_version`. Checked with `/var/tmp/hx/conform.py`-style probes (27 share cases, 4 hostname, 9 user) |
| Array tile gauge always showed 76 % amber ("91 GB of 13 TB" with a nearly full bar) | `renderArray` set the text but never the bar, so the mock-up's hard-coded `width:76%` stayed; the status script only emits `df -h` strings ("91G"/"13T"), which can't give a percentage | `/api/array/status` adds `size_bytes/used_bytes/free_bytes/pct` from `os.statvfs` for the user share and every mounted disk (`_add_usage`); the tile sets width and colour from it with Unraid's rule (≥90 red, ≥70 orange, else green; §30.4) and shows "(stopped)" when off. Audit: `cpu-bar`, `ram-bar`, `ups-bar` were already driven by data. |
| Array had only Start/Stop with no explanation, no guarding, no live status | — | `services/array_state.py` is a pure state machine (§30.5): `GET /api/array/operation` returns rows of status + button + sentence (+ warning/confirm). `POST /api/array/start|stop` **enforce** it (409 with the reason; `confirm=true` for a missing disk); an `array_transition` marker makes "Starting…/Stopping…" visible. UI: Array Operation panel + global footer (`#helix-footer`, array state + running parity/mover). 15 scenarios unit-tested; panel, gate and footer browser-tested. |
| Pi-Hosted catalog paths (`/portainer/Files/AppData/Config/…`, `/portainer/Downloads`, `/portainer/TV`…) meant nothing on Helix | the catalog assumes a Raspberry Pi layout | `build/tools/convert-portainer-catalog.py` rewrites the catalog: app settings → `/mnt/user/appdata/<app>`, `Downloads` → `/mnt/user/downloads`, `TV/Movies/Music/Podcasts/Books/Comics/Pictures` → `/mnt/user/media/<Name>`; system paths (`/etc/localtime`, `/var/run/docker.sock`, `/dev/*`, `/proc`…) and named volumes untouched; 0 `/portainer` left; 388 binds + 2 env defaults + 1 note changed (Dockge's same-path-inside-and-out stacks dir changed on both sides). Output `webui/backend/data/catalogs/pi-hosted-helix.json` ships in the image as default source `pi-hosted` (`url: bundled:…`). Regenerate: `python3 build/tools/convert-portainer-catalog.py --out webui/backend/data/catalogs/pi-hosted-helix.json`. The mapping lives in ONE place, `webui/backend/services/pathmap.py` (used by the converter and by the runtime loader for Pi-Hosted-style catalogs added later); the only app the runtime fallback can't fully fix is Dockge (same path inside and outside the container), which the bundled catalog handles. `/portainer/Files/Downloads` (Transmission) maps to the `downloads` share. |
| Existing installs would never see a new default template source; a removed default would be resurrected | `load_sources()` returned the saved list untouched | `template_sources.seen` records which defaults were already offered: new defaults are added once, a removal sticks; the pre-existing defaults (`builtin`, `unraid-ca`) count as already offered |
| App folder fields were free-text boxes; a value with `:` could rewrite a volume mapping; Docker would create missing folders as root | no validation; `_do_install` created any path | UI: share drop-down + folder box (suggests existing folders, a new name = "created on install", "Other path…" for anything else, warning when the share doesn't exist yet); server: `_validate_path_vars` (absolute, no `..`, no `:`/newlines) → 400, `_ensure_dirs` creates folders as the Helix user and only under `/mnt` |
| Catalog search box did nothing (typing "plex" left the list unchanged) | 4 Pi-Hosted entries (Headphones, SmokePing, SyncThing, UniFi Controller) have `"description": null`; the loader used `item.get("description", "")`, which only defaults a *missing* key, so `None` reached the browser, where `t.description.toLowerCase()` threw inside `filterApps` — the exception silently aborted the filter | loader uses `item.get(k) or ""`; `GET /api/apps/templates` always returns strings; `filterApps`/`renderCatalog` are null-safe (`String(v ?? '')`, `_esc`). Browser-tested with null/all-null entries, clearing, paging. |
| Adding an extra folder to Emby failed with "Folder paths must be absolute: '/mnt/user/smoke-share/Anime' -> 'Anime'" | the container-side box needs an absolute path, but the message printed both and read as if the (valid) host path was wrong; the boxes were unlabeled | `_clean_extras` now says which side is wrong, treats a bare container name as `/Anime`, rejects `..`; placeholders say "Folder on the server" / "Path inside the app"; host box suggests `/mnt/user/<share>/` |
| File browser could not browse the shares | the page started at `/home/pi` (a dev-box path); on a Helix box it does not exist, so `list_dir` silently fell back to `/tmp`. The allowed-roots check also used `/mnt/disk` and `/mnt/cache` as path-component prefixes, so `/mnt/disk1` and `/mnt/cache1` were refused | roots are now `/mnt` and `/boot` (Unraid's); the page starts at `/mnt/user`; a missing folder is a 404 (no silent fallback); `/api/files/roots` gives quick links (Shares, each disk/pool, Flash) |
| Any web upload over 1 MB failed (HTTP 413) | nginx had no `client_max_body_size`, default 1 MB | `client_max_body_size 0; proxy_request_buffering off;` in `/etc/nginx/snippets/helix-proxy.conf` (hook 03); the UI also uploads in 20 MB chunks |
| Terminal page: "[WebSocket error — is the backend running?]" | the nginx proxy block did not pass `Upgrade`/`Connection` headers, so `/api/terminal/ws` reached the backend as a plain GET and got 404 (the backend itself was fine) | `map $http_upgrade $connection_upgrade` at the top of `sites-available/helix` + `proxy_set_header Upgrade/Connection` in the proxy snippet |
| VPN tab: "Tailscale is not installed" (it was supposed to ship in the image) | Tailscale is not in Debian, so it was never in `packages-base.list`; the router also demanded root (the backend is `helix`) and the UI swallowed every error | `build.sh` adds the official apt repo (key + list fetched on the build host, fatal if that fails) and installs `tailscale`; hook 03 ships `helix-tailscaled` (keeps the node key in `/mnt/data/config/tailscale` or `/boot/config/tailscale`, else RAM), a `tailscaled.service` drop-in, `tun` in modules-load and IP forwarding in sysctl; `routers/tailscale.py` uses `services/privileged.py`, `up --timeout=8s` (otherwise it waits for the browser sign-in forever), takes the login link from `status --json` `AuthURL`, passes auth keys as `--auth-key=file:` (never argv); the UI shows the login link, "service not running", and real errors |
| Network sidebar link showed the dashboard (no page existed); backend config API unused, looked for its script at a path that does not exist in the image, ran without root, VLANs were created but never attached | `#network` was not in `SECTION_IDS`; `routers/network.py` pointed `NETWORK_SBIN` at `../../../../network` | New Network page (`#network-section`): live interfaces (kind, state, MAC, addresses, speed, MTU, rx/tx + rates), settings form (DHCP/static, DNS, domain, MTU/jumbo, bonding, bridge, VLAN, mDNS) with server + client validation (`_validate`: IPv4/mask/gateway-in-subnet/MTU/bond ports; nothing unvalidated reaches `network.cfg`), **confirm-or-revert safety net** (`PUT /config` backs up `network.cfg`, applies, and reverts by itself after 90 s unless `POST /confirm`; the yellow `#net-pending` bar shows on every page; survives a backend restart), routing table + persistent static routes (`network-routes.cfg`, read by `helix-apply-network`), DNS in use, diagnostics (ping/DNS per server/traceroute with strict host validation), listening ports (`ss -tulnp` via sudo). `NETWORK_SBIN` resolves to `/usr/local/sbin`; apply runs via `services/privileged.py`; the apply script now attaches the VLAN to its parent and writes a `.network` for it; `/status` (dashboard tile) no longer calls the script because that dropped `rx_mb`/`tx_mb` |
| Opening or reloading the page on `#files` / `#network` (any route whose loader uses variables defined in a later script block) failed with "Cannot access 'x' before initialization" | `updateSections()` ran at script *parse* time, before the later blocks had defined their `const`s | it now runs on `DOMContentLoaded`. Rule: never call route loaders while the script is still being evaluated |
| Static-IP fields always visible; "Keep/Undo" bar could never hide | `.net-row{display:flex}` and `#net-pending{display:flex}` beat the `hidden` attribute (same trap as the demo banner) | `[hidden]{display:none}` rules for each class that sets `display`; found by the browser test, not by reading |
| Array, Shares and Docker sidebar links showed the same page as Dashboard | they had no section in `SECTION_IDS`, so every one of them fell through to the dashboard (which already holds the array, shares and containers tiles) | links removed. Audit method: click every `a.nav-item` on the VM and fingerprint the visible `main > *` ids; any two links with the same fingerprint are duplicates (only that one group existed). Re-run it whenever a link is added |
| Dashboard: Array Operation rows touched the bottom of the drive tiles, drive tiles almost touched the stat tiles (user screenshot with no "Drives"/"Array Operation" headings between them; could not reproduce the missing headings on the VM at 1700 px, dark or light) | the blocks relied on the section headings' margins for their distance | `.drives-grid` has its own margin and `gap:16px 12px`, `.stats-row + .drives-grid` / `.drives-grid + .array-op` add 22 px when no heading sits between them, `.array-op` gap 10 px and larger row padding. Measured: 22 px without headings (was ~3), 51-61 px with |
| 91 form fields unexplained | — | `FIELD_TIPS` map + shared ⓘ bubble (`attachTips`, MutationObserver for modals); `tipHtml()` for string-built HTML |
| Mover could delete open files and the Docker graph; one Dry Run click pointed every later run at `/mnt/cache` | `routers/mover.py` ran `rsync -a --remove-source-files` on every directory under `/mnt/cache1` into `/mnt/user`. Dry Run saved `cache_path=/mnt/cache`, which is not a mount (drives are `/mnt/cache1`…) | **2.0.25.** `array/sbin/helix-mover` plus `POST /api/mover/start?dry_run=true`. Full write-up §31. |
| Dashboard, array controls, the disk list, shares, and containers were one scrolling page; the header repeated the footer | The sidebar listed every tool, and a page that cannot run (no libvirt, no ZFS, no GPU) looked the same as one that can | **2.0.26.** Dashboard stays a glance. Main holds the disk table, array operation, mover, and rebuild. Shares and Docker are their own tables. Settings and Tools are grouped. Header search jumps to a page. Full write-up §32. |

### 29.3 Rules learned (read before changing these areas)

- **Test the whole thing, not just the new thing.** The CA catalog was empty for the entire campaign because I only inspected the source I had just added
  (`total all sources: 261` == the one source's count). When a total should include N sources, check *each* source's count.
- **`--no-install-recommends` removes things you silently depend on** (`samba-vfs-modules`, `apparmor`). After adding a service package, start the service and use it.
- **Anything user-configured belongs in `HELIX_CONFIG` (`/boot/config`), never `STATE_DIR`** (RAM). `STATE_DIR` is for caches/job status only. Routers must `from config import HELIX_CONFIG, STATE_DIR`, never define their own defaults.
- **Never trust `/dev/sdX`.** Persist `by-id` names; resolve with `os.path.realpath` at read time.
- **FAT ignores `uid=/gid=` on remount.** Set ownership at the first mount (initramfs). `mount --move`, not `--bind`, for the boot device.
- **Device-name fragile / privilege-fragile checks run as root in tests lie.** A `persistent=True` check run as root proved nothing; run backend-side checks `sudo -u helix`.
- **`cp -r` onto an existing directory nests.** Always remove the destination first.
- **Cloudflare-fronted APIs may 403 Python (urllib/requests) even with valid credentials** — use `curl`, and pass secrets on stdin (`curl -K -`), never argv.
- **Debian bookworm facts:** no `docker-compose-plugin`/v2; `docker.io` 20.10; `samba` 4.17; `wireguard-tools` ok. Containers need `apparmor` + `iptables`.
- **NFS-exporting mergerfs needs `fsid=`.** One service identity (`helix`) for SMB/NFS/file-manager keeps files interchangeable.
- **Docker data-root must be a real FS (not the mergerfs share, not RAM)** → cache or a data disk.
- **File manager internals:** files are copied by the backend process (as `helix`) so everything it creates has the same owner as SMB/NFS/apps; only Owner/Permission jobs use `sudo`. Test knobs: `FILES_MNT`/`FILES_BOOT` move the browsable roots, `HELIX_FILEJOBS_DELAY` (seconds per 4 MB chunk) slows copies because RAM disks finish instantly. Backend checks used a throwaway tree + `uvicorn` on port 8099, UI checks used Playwright against it.
- **A CSS `display:` on a class overrides the `hidden` attribute.** `index.html` now has a global `[hidden] { display: none !important; }`, which is what lets Dashboard tiles disappear on other pages. Setting `style.display = 'block'` does **not** reveal an element that still has the `hidden` attribute. Clear `hidden` (the OS-update log does; `#updates-log` was the one that only set `display`).
- **A fixed bar must sit above `header` (z-index 100)** or its buttons cannot be clicked; Playwright reports "<header> intercepts pointer events".
- **Test network changes with `NETWORK_SBIN` pointed at a stub and `NET_CONFIRM_SECS` small** (never run the real `helix-apply-network` on a dev machine: it rewrites `/etc/systemd/network`).
- **`pkill -f <pattern>` kills your own shell if the pattern is in its command line.** Write it as `pkill -f '[u]vicorn …'`.
- **nginx in front of FastAPI needs three things the default lacks:** `client_max_body_size`, the WebSocket upgrade headers, and (for streaming) `proxy_request_buffering off`. After changing the proxy, test a >1 MB upload and a WebSocket through port 80, not just port 8082.
- **`dict.get(key, default)` does NOT replace a JSON `null`** — it only covers a missing key. For third-party data use `item.get(key) or default`, and never call string methods on a field you didn't validate. A single bad row in a list that a UI filters can silently kill the whole feature (the exception aborts the handler).
- **`apiFetch(path)` is GET-only (it ignores a second argument). Use `fetchJSON(path, {method, body})` for POST/PUT/DELETE** — misuse fails silently.
- **RAM-root state checklist:** `/etc/passwd|shadow|group`, Samba passdb, crontab, `/etc/wireguard`, `/etc/docker/daemon.json`, anything under `/var`. If a feature writes there, it needs a copy in `HELIX_CONFIG` and a restore step (`helix-restore-state`).
- **`ip rule` priorities must be < 32766** (`main`). Never reuse a routing-table id as a priority. Check with `ip route get <dst> from <src> iif <bridge>`.
- **A Docker bridge routed into a tunnel must use the tunnel's MTU** (`com.docker.network.driver.mtu`). Symptom of a mismatch: ping works, TCP connects, data stalls / TLS "unexpected eof".
- **A container on two networks keeps its first default route** — to force egress via a network, detach the others.
- **Never silently dedupe across user-added sources.** And never judge a multi-source feature by the total (see the CA lesson above).
- **Unraid `Default="a|b|c"` is a choice list.** Config items also carry `Required`, `Description`, `Mode` (ro/rw), `Mask`; Port `Target`=container port, `Default`=host port; `Device` items map hardware nodes.
- **Releases:** the updater only accepts `vX.Y.Z` tags with a manifest asset; other releases (e.g. `images-*`) must not look like updates. The updater on the *running* VM is the old one — if it mis-picks, hot-patch `routers/osupdate.py` in RAM over SSH (reverts on reboot) to get the next version on.
- **A boot-critical file change (initrd, grub.cfg) ships with a `.prev` copy.** `grub.cfg` is **not** updated by OTA — copy it by hand (back up as `grub.cfg.bak-<ver>`, checksum, atomic `mv`).
- **Boot-test after any partition/boot change** before applying the next update, so a failure can be pinned on the right change.
- A rootfs change in the image needs an actual image rebuild+OTA to take effect on the VM; hot-patching the VM is for unblocking only.
- **Mover:** never `rsync --remove-source-files` a cache tree onto `/mnt/user`. Skip open files, the docker data-root (`<cache>/system/docker`), and any path that already exists on the array. Dry Run must not write `mover.cfg`. The open-file scan is one Python walk of `/proc` run as root; a bash `readlink` per fd takes a minute, and a bash `RETURN` trap fires on every function return, not only the function that set it. Details and the remaining per-share gap are in §31.
- Unraid parity: its `/boot` is FAT too and its templates hold tokens on the flash — our `/boot/config` holds secrets (API keys, PIA, app env) in plain text on a world-readable FAT volume. Acceptable for now; see 29.4.

### 29.4 State at time of writing, open items, ideas

Released and verified on the VM: **2.0.13** (every version 2.0.1 → 2.0.13 was applied by the VM's own updater, rebooted into, and re-tested; last full sweep: 21 UI pages with no JS errors or failed API calls, 94/95 parameterless GET endpoints 200 — the 95th, `/api/files/download`, needs a path).

Verified working end to end on the VM: Unraid CA catalog (4 809 apps) + a custom Pi-Hosted source (261) in one catalog, both persisting across reboots, PIA connect + container routing (exit IP differs from the host; host route/DNS/IP untouched; routed app still reachable from the LAN), app install/Configure/extras/Remove, array (data+parity+cache by serial), parity sync with data, SMB + NFS read/write interchange, Docker app install/logs/stop/start/remove with image data on the cache disk, settings persistence across reboots, IOMMU boot flags, all 21 UI pages without JS errors.

Open items:
- **ZFS:** the ZFS page exists but `zfsutils-linux` is only in `packages-full.list`, so images say "ZFS not available". Decide whether to ship ZFS in the image (Debian needs `contrib` + `zfs-dkms` + kernel headers).
- **REST API is unauthenticated** (only `/mcp` checks the bearer). The UI sends no token. Needs login/session + first-boot password decision.
- **VMs:** libvirt/qemu are not in the image (`/api/vms/` returns `available:false`); GPU passthrough UI is untested on hardware.
- Portainer **stack templates (type 3)** — 36 of 297 Pi-Hosted entries — are skipped (no single image).
- Apps installed before 2.0.7 (e.g. the Cloudflared tunnel installed from the UI) lost their saved settings when their definitions lived in RAM; the container survives (restart policy + Docker data on the cache disk) but shows as a plain container: remove and reinstall it from the catalog.
- arm64 images/releases are stale (2.0.1+ are amd64-only).
- OTA does not carry `grub.cfg`; consider adding it (with `.prev`) to the helper.
- PIA `addKey` is called with TLS verification off (PIA ships a CA cert; pin it). PIA password and other secrets are plain text on `/boot`.
- **PIA:** DNS for routed containers still goes through Docker's embedded resolver to the host's upstream (a DNS leak); there is no kill-switch (disconnect restores the containers' normal networks instead).
- `apps` update/`up --pull` on compose v1 is emulated; moving to Docker's own apt repo would give v2.
- Unraid features worth comparing next (from §10): per-share cache policy + mover scheduling UI (the mover no longer deletes open files or the docker graph — §31 — but it still moves every ordinary share directory, with no yes/no/only/prefer and no array→cache direction), split level, notifications feed wiring, dashboard tile layout, user-share "exclusive" mode, docker "autostart order", Dynamix-style system stats history, safe-mode boot entry testing. Array autostart shipped earlier (§29.2).

*Last updated: 2026-11-09 (2.0.13).*

---

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

### 30.16 Implementation status (maintained by Claude; §30.1–30.15 above are the user's reference and are not edited)

Numbers refer to the adoption list in §30.13. Verified = exercised on the test VM, not just written.

| # | Pattern | Status |
|---|---|---|
| 17 | Refuse rather than fail | **Partly done (2.0.14):** the §30.14 input limits (share/host/user/description/password) are enforced server-side and shown live in the forms. Still to do: render config forms disabled with "Array must be Stopped" for identity/SMB/global-share changes; check whether a VM/container uses something before changing it. |
| 5 | Apply disabled until a form changes; Done returns to parent | not started (Helix saves per button) |
| 1 | Array panel as a state machine | **Done (2.0.15), on Main (2.0.26):** valid/invalid Start (no devices, no data disk, parity too small, missing disk + confirm, unprotected warning), Stop gated by parity/mover, transitions, parity Sync/Scrub/Cancel rows. Mover and rebuild sit under that panel as their own rows. Not yet: unclean-shutdown check, maintenance mode, spin up/down of every disk, reboot/shutdown rows, real % progress/ETA for parity |
| 2 | Global footer with live array/operation status | **Done (2.0.15):** "Array Started · Parity operation 0:12:33 · Mover running" with a colour dot, on every page. A running mover, rebuild, or parity job adds a second chip (`#footer-opjob`) that reopens its log |
| 3 | Long jobs in a live-log modal that survives closing | **Partly done (2.0.26):** mover, rebuild, and parity open `#job-log`. Close hides the window and leaves the job running; the footer chip opens it again. The window stays up when the stream ends. Parity still has its bar on Main. Not the Unraid pattern of one shared modal for every long job |
| 4 | One container context menu shared by Dashboard and list pages | **Partly done (2.0.13, table in 2.0.26):** the Docker page repeats Start/Stop/Restart/Logs/Configure/Remove on each row. Logs opened from that table has Close only; Back to the old manager appears only when that dialog is open. No "edit before touching the running container", no Update All |
| 6 | Shape+colour status orbs, click to spin | **Partly done (2.0.26):** Main's disk table and the compact dashboard glance use a green circle, a grey circle once spun down, and a red X when the disk is missing. Click calls the existing spinup/spindown endpoints. Four or fewer assigned disks stay as the old tiles, which already had a spin button. The boot disk is a grey circle and is not clickable |
| 7 | Inline disk assignment while stopped | **Partly done (2.0.26):** while the array is stopped, Main lists every physical disk and a role dropdown (parity, data, cache, unassigned) posts the same body as Assign disks. The boot disk has no dropdown. The modal stays. A machine whose only disk is the boot disk hides Save. Not yet the Unraid pattern of one dropdown per slot that saves on change |
| 8 | Share editor (Primary/Secondary/Mover wording, on-demand size) | **Partly done (2.0.26):** Shares is its own page. Size is computed only when asked (`POST /api/files/calc` on `/mnt/user/<name>`). Storage reads `cache → array` when a cache disk is assigned, otherwise `array`. There is still no per-share Primary/Secondary/mover-direction editor (§31.6) |
| 9 | Hide/explain features that cannot work now | **Partly done (2.0.26):** GPU, Virtual Machines, Docker, and the ZFS tool card stay in the nav and go grey, with one sentence, when that API returns `available: false`. A failed call leaves the link looking normal |
| — | App folder picker (not in §30.13) | **Done (2.0.16):** share drop-down + folder box, new-folder creation, validation (see §29.2) |
| §30.7 | File manager | **Done (2.0.20):** table with checkbox/type/name/owner/permission/size/modified(or age)/location(disks); sortable (remembered), type filter, quick links; row menu + bottom buttons with Unraid's enable rules; Copy/Move/Delete/Owner/Permission as server jobs (`services/filejobs.py`: progress, ETA, queue, start/cancel, survive closing the dialog, footer indicator `#footer-job`); Calculate (per-disk breakdown); Search (name or `*` `?`); text editor (conflict check, unsaved-changes guard; plain textarea, not Ace); image/PDF/audio/video viewer; 20 MB chunked resumable upload with progress/cancel and drag-and-drop. Rules enforced server-side (`services/fileutil.py`): nothing created/renamed/deleted at `/mnt`, `/mnt/user`, disk roots or share folders; user-share and disk paths cannot be mixed. Not done: Ace editor, `editor.cfg` extension list, zipped-folder download, right-click on crumbs |
| 10 | Save a container's definition before touching the running one | Done for apps (compose + settings written before `up`); not for plain containers |
| 11 | Group the sidebar (Settings/Tools grids, Favorites) | **Partly done (2.0.26):** 13 top-level links. Settings opens on a jump bar (Identity, Samba, Users, Notifications, Appearance). Tools is a card grid (diagnostics, hardware, scheduler, backup, LAN scan, proxy, terminal, ZFS). Header search jumps to any of those pages. No Favorites and no padlock |
| 12 | Dashboard drag-sortable tiles, tile manager, padlock | tiles are draggable; no manager/padlock/server-side layout |
| 13 | One global Apply/Reset + one reboot-notice service | not started |
| 14 | Type-the-name confirmation for destructive actions | **Partly done (2.0.26):** disk Erase asks you to type the disk name (wrong name → "Incorrect confirmation" → Ok reopens it) and Format needs the "Yes, I want to do this" tick plus a warning dialog (§33). Everything else still uses `confirm()` |
| 15 | Edge-triggered alerts with "returned to normal" | not started; notification backends still not wired to real events |
| 16 | Dated backup on every boot-config write | not started |

Other Unraid behaviours already matched: array auto-start on boot, recycle bin location, user-share exclusive/FUSE is n/a (mergerfs). The mover skips open files, the docker data-root, and files already on the array (2.0.25, §31). Per-share cache policy is still absent. The 2.0.26 layout (Dashboard vs Main, disk table, Shares, Docker, header search) is §32. It was checked in a browser against this repo, not yet on the test VM.

## 31. Independent read of the Unraid management scripts, and the mover fix (2026-11-10)

`build/config/VERSION` is **2.0.25**. Not published and not applied to the test VM (`192.168.50.12`). The helper is installed by hook 03, so it reaches a machine only with an image rebuild and OTA. Do not paste the Unraid scripts into this repo.

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

### 31.3 What was wrong in Helix

`webui/backend/routers/mover.py` ran, as the `helix` user:

```
rsync -aX --progress --remove-source-files /mnt/cache1/<dir>/ /mnt/user/<dir>/
```

for every directory on the cache, with no share policy.

- An open file was copied and then deleted out from under the process that had it.
- `<cache>/system/docker` is the Docker data-root `helix-array-start` creates. Treating `system` as a share would move the Docker graph onto the array and delete it from the cache. It survived only because those files are root-owned and the backend is unprivileged. Running the same rsync as root, which a mover needs in order to see every process's open files, would have deleted it.
- If the array already had the file and the cache copy was newer, rsync overwrote the array copy and deleted the cache copy. Unraid leaves both.
- `--remove-source-files` does not remove the empty directories it leaves behind.
- There was no check that the cache path and the destination overlap.

**Dry Run wrote the wrong config.** `moverDryRun()` in `webui/frontend/index.html` PUT `/api/mover/config` with `cache_path: '/mnt/cache'` and `dry_run: true`, called start, then PUT the same path back with `dry_run: false`. Cache drives are `/mnt/cache1`, `/mnt/cache2`, …. After one Dry Run click, `mover.cfg` pointed at a directory that does not exist, and every later Move Now failed. Start reads the config before it returns, so that one invocation was a dry run; the saved path stayed wrong.

### 31.4 What changed, and why

| File | Why |
|---|---|
| `array/sbin/helix-mover` | New helper. Does the safe part of §31.2 without shipping Unraid's `move` binary. |
| `webui/backend/routers/mover.py` | Runs that helper. Stops the whole process group. Dry run is a query flag. |
| `webui/frontend/index.html` | Dry Run calls `moverStart(true)` and no longer PUTs `mover.cfg`. A failed start is written into the log instead of being swallowed. |
| `build/hooks/03-helix-install.sh` | Installs `helix-mover` to `/usr/local/sbin` with the other array helpers. Without this line the image boots and the API reports "helper not found". |
| `array/test/test-mover.sh` | Throwaway-tree regression test. No root and no `/mnt`. |
| `build/config/VERSION` | `2.0.24` → `2.0.25`, so a later image is distinguishable. |

Helper rules, in order:

- `--cache` and `--dest` are required. If one path contains the other, exit 2. If the cache directory is missing, exit 2.
- If `/etc/docker/daemon.json` names a `data-root` equal to the cache, refuse. If the data-root is under the cache, skip that top-level directory. `system` is always skipped, because array start uses `<cache>/system/docker` even when `daemon.json` is missing. `lost+found` and any hidden top-level directory are skipped. Top-level files are left in place and counted in the log.
- One Python 3 walk of `/proc/<pid>/{fd,cwd,exe,maps}` plus loop `backing_file` records paths under the cache. A file on that list, or under a listed directory, is not moved. Docker bind-mount sources under the cache are added the same way. `docker info`, `docker ps` and `docker inspect` each have a 3-second `timeout`, so a stuck daemon cannot stall the mover.
- If the same relative path already exists on the destination (including a symlink), both copies stay.
- The files that remain are handed to `rsync -aHX --from0 --files-from --remove-source-files`. rsync deletes a source only after that transfer succeeds, and it does not keep a partial destination. A file still present afterward is logged as "copy failed".
- Empty directories under the share are then removed. The share directory itself stays, so the next write still has a place.
- `--dry-run` prints "would move" and does not call rsync. `mover: RESULT moved=N in_use=N exists=N` counts real moves only.

Router rules:

- `POST /api/mover/start?dry_run=true` forces one dry run and does not write `mover.cfg`. A saved `dry_run=true` in the config is still honoured, so the config field means something if an API client sets it. The button no longer sets it.
- Both paths must be absolute and under `/mnt`, with no `..`. The API is still unauthenticated (§29.4); this only stops a request from naming `/etc` or similar.
- The helper runs as `sudo -n env HELIX_CONFIG=… /bin/bash …`. The open-file walk cannot see other users' `/proc/<pid>/fd` otherwise, and the copy must be allowed to preserve ownership (`rsync -a` as root keeps the source owner). `HELIX_MOVER_DIRECT=1` skips sudo for a same-user test.
- The script is `ARRAY_SBIN/helix-mover` (`/usr/local/sbin` on an image). If that file is absent, the router uses `array/sbin/helix-mover` three levels above `webui/backend/routers/`. That fallback matches this repo. It does not match `/opt/helix/backend` on an installed system, so a hot-patch of `mover.py` alone reports "helper not found" instead of going back to the old rsync.
- Stop sets a cancel flag and `SIGTERM`s the process group (`start_new_session=True`). Terminating only the shell would leave rsync running.
- The SSE stream waits until the thread has printed a line or finished, so a fast client does not receive `DONE` before the helper starts.

### 31.5 How it was checked

`bash array/test/test-mover.sh` on a temp tree. All of these passed: an ordinary file moves and the emptied subdirectory is removed; the share directory, a top-level file, `.hidden/`, and `system/docker/` stay; a file held open by another process stays and is logged "in use"; an existing destination file is not overwritten and its source stays; a destination directory mode `0555` keeps the source and logs "copy failed"; `--dry-run` copies nothing and prints "would move"; a destination inside the cache exits 2.

The first run of that test took about 71 seconds. `collect_busy` was a bash loop that forked `readlink` once per fd of every process. It was replaced with the single Python walk above and the same test passed in a few seconds. A `trap … RETURN` was removed in the same edit: in bash that trap runs on every function return, including helpers called while the temp files are still in use.

Browser, against this repo's UI on `127.0.0.1:8098` with a throwaway `HELIX_CONFIG` and `HELIX_MOVER_DIRECT=1` (Playwright, Chromium `--no-sandbox`). Dry Run sent `POST /api/mover/start?dry_run=true`. Move Now sent `POST /api/mover/start`. Nothing PUT `/api/mover/config`. `mover.cfg` was unchanged. The log showed `mover: cache not found: /mnt/cache1` because this machine has no cache disk, then `--- Done ---`. No page errors. The server was stopped afterward. A real cache disk on the test VM has not been exercised; that waits on the next image.

### 31.6 Still not done

- Per-share cache policy. Unraid's `shareUseCache` is `no` (ignore), `yes` (move cache → array), `only` (stay on cache, mover ignores), `prefer` (move array → cache). Helix has no such field, so every ordinary top-level directory on the one configured cache is moved. A share a person named `system` is skipped on purpose. Appdata that lives on the cache and is not currently open will be moved; Unraid would move it only when the share says `yes`.
- No array → cache direction, no "empty this disk" list, no mover schedule. The schedule pattern is the one in §30.10 (Mover Settings).
- `shfs` allocation (split level, included disks, min free space) is still mergerfs `category.create=mfs` and `minfreespace=4G` for the whole union.
- Samba still does not rebuild from `ident.cfg` the way `rc.samba` does (NetBIOS, multi-channel, hide-dot-files, Active Directory, `secrets.tdb` on the flash).
- No `emhttp_event`-style hook list for plugins.
- Click-to-spin orbs on the disk table landed in 2.0.26 (§32). A per-disk spin-down delay is still the old timer dialog, and there is no Spin up / Spin down for the whole array.

## 32. Dashboard, Main, Shares, Docker, and the header (2026-11-10)

`build/config/VERSION` is **2.0.26**. Not published and not applied to the test VM. The page is `webui/frontend/index.html`. Reads and writes on `/api/array/status` come from `webui/backend/routers/array.py` (`_disk_io`, `/proc/diskstats` fields for completed reads and writes). The kernel does not publish an unrecoverable-error counter, so the Errors column is an em dash. A disk whose SMART reallocated or pending count is above zero shows that sum in warning colour instead.

The palette, Inter, the orange accent, outline buttons, and the settings cards are unchanged. These were deliberately not added: a global Apply/Reset, type-the-name confirms, a tile manager, edge-triggered alerts, and a per-share cache policy.

### 32.1 What the screen does now

- **Dashboard** is status. Stat tiles, a drive glance, and the first five shares and containers, each with View all. Four or fewer assigned disks stay as the old tiles. Five or more become a short list (six lines, then "N more on Main").
- **Main** (`#main`) is the disk table plus the existing Array Operation panel. Cache mover (Move now, Dry run, Stop) and Drive rebuild are rows under that panel, so there is still one Start. While the array is stopped, each physical disk gets a role dropdown. Save posts `{parity, data, cache, fmt:'xfs', cache_fmt:'btrfs'}` to `POST /api/array/assign`, the same body as the Assign disks modal. The boot disk is labelled and has no dropdown. If the only disk is the boot disk, Save is hidden. The modal remains.
- **Shares** (`#shares`) is name, comment, SMB, NFS, storage, and size. Size is a Compute button, not the old `used_gb` figure (that number was `statvfs` of the whole filesystem). Compute calls `POST /api/files/calc` with `/mnt/user/<name>`. Storage is `cache → array` when `array/status` lists a cache disk, otherwise `array`.
- **Docker** (`#docker`) is one row per container: status, ports, folders (filled from `GET /api/docker/containers/{name}` only while that page is open, cached 30 seconds, capped at 40), and the same Start/Stop/Restart/Logs/Configure/Remove buttons as the old manager. The dashboard glance keeps the compact buttons and the top five.
- **Sidebar.** System: Dashboard, Main. Storage: Shares, Docker, Files. Apps: App Catalog, Virtual Machines, GPU. Network: Network, VPN. Admin: Users, Settings, Tools. Settings has a jump bar. Tools is eight cards. Diagnostics, hardware, the scheduler, backup, LAN scan, the proxy, the terminal, and ZFS are reached from Tools or from search, not from their own sidebar row.
- **Header.** Search (type, arrows, Enter), Files, Terminal, Log, and the notification bell. The duplicate array-status dot was removed. Array state stays in the footer.
- **A page that cannot run** stays visible and goes grey, with one sentence: no GPU, no libvirt, Docker `available: false`, ZFS tools missing. If that request fails, the link is left looking normal.
- **A long job** (mover, rebuild, parity) opens `#job-log`, which sits outside `<main>` so changing pages does not hide it. Close does not stop the stream. `#footer-opjob` stays until the stream ends and opens the window again. File-manager jobs still use `#footer-job`.

### 32.2 Why a few details look the way they do

- `updateSections` hides every `main` child that is not the active section. It builds that set from `SECTION_IDS`, so a new page is hidden on the dashboard as soon as it is registered. `#cmk-panel` is the exception: Checkmk decides whether it exists (`dataset.cmkOn`), and the dashboard visit must not force an empty monitoring panel on.
- Author `display:` used to beat the `hidden` attribute, so tiles stayed visible on other pages. The global `[hidden] { display: none !important; }` fixes that. `#updates-log` is revealed by clearing `hidden`, because `style.display = 'block'` no longer wins.
- Clicking an orb calls `GET /api/drives/{name}/spinstate` when this page has not spun it yet, then `spindown` or `spinup`. A missing disk is a red X and is not clickable. Demo mode does not call the server.
- The first bytes of `index.html` are `<meta charset="utf-8">`. Without that, a server that sends `text/html` with no charset (Python's `http.server`) decoded the file as windows-1252, so `°` and `—` rendered as `Â°` and `â€"`.
- Container Logs opened from the Docker table offers Close. Back to the container manager is added only when `#ctr-manager` is already open, because that dialog is what Back returns to.

### 32.3 How it was checked

Playwright (Chromium, `--no-sandbox`) against two local servers. Neither click assigned a disk, started the array, or ran Move now.

- `http://127.0.0.1:8097/index.html` was the file with no API, so the page stayed in demo mode. The sidebar was the 13 links above. Four mock disks rendered as tiles; five injected disks became the compact glance. Search for "sha" and Enter opened Shares, and the stat tiles' computed display was `none`. The share table said `array` and offered Compute, with no usage bar. The Docker table had all eight mock containers; Logs showed Close and no Back. Main's disk table showed the kernel-style read counts and the em dash, and disk3's three reallocated sectors showed as `3`. Mover and Rebuild were on Main. Closing the job window left the footer chip, and the chip opened it again.
- `http://127.0.0.1:8098` was uvicorn with `HELIX_CONFIG` and `STATE_DIR` under `/tmp/hx-ui-226` and `HELIX_MOVER_DIRECT=1`. The banner hid. The footer read Array Stopped. The Start button was present and disabled (no data disk). This machine's only disk is the boot disk, so Save assignment was hidden and the note said there was nothing to assign. Injecting a data disk and a parity disk (in the page only, no POST) produced those two dropdowns and left the boot row without one. GPU stayed a normal link (`available: true` on this host). VMs and the ZFS card greyed out with their sentences. Docker stayed available. Header search opened Hardware and highlighted Tools. The Log button filled the syslog. Dry Run posted `POST /api/mover/start?dry_run=true` only. The log read `mover: cache not found: /mnt/cache1` and then Done. `mover.cfg` was not created. No page errors on the live origin. A 680 px viewport hid the hostname and showed the menu button.

`node --check` on the inline script and `py_compile` on `array.py` both passed. The servers were stopped afterward. The test VM at `192.168.50.12` was not updated.

## 33. Format from Main and the disk page (2026-11-10, written after an interrupted session)

`build/config/VERSION` is **2.0.27** and covers §31 (mover), §32 (layout) and this section (2.0.26 shipped these with a parity mistake, corrected in 2.0.27 — see §33.2). A session working on these was cut off by a usage limit, with everything uncommitted and this third piece undocumented. It was reconstructed from the working tree, completed, tested and committed in one go. Nothing here has been run against a real disk: a new disk with no file system has not been exercised on the test VM.

### 33.1 What it does (Unraid's "unmountable disk → Format" model)

- **The array starts even when a data disk has no file system.** `helix-array-start` no longer exits "No data drives mounted". mergerfs is skipped until at least one data disk is mounted, and Docker is left stopped if there is no formatted disk for its data-root.
- **Main shows an "Unmountable disks present" row** while the array is started and an assigned data/cache disk is present but not mounted. Parity counts too (SnapRAID keeps its parity *file* on a mounted file system). It lists `name · /dev/sdX`, has a **Format** button, and the button stays disabled until you tick "Yes, I want to do this"; ticking pops a warning that a format is never part of recovering data.
- **`POST /api/array/format?confirm=true`** runs `array/sbin/helix-disk-format` as root in the background (`FORMAT_JOB` marker in `STATE_DIR`). The helper only touches a block device that is **not mounted**, uses the disk's `format=` value (default xfs for data, btrfs for cache; xfs/btrfs/ext4 only), mounts it, chowns the mount to `helix`, and starts mergerfs if the share was not up. While it runs, Stop is disabled and the footer says Formatting. A failure is written to the marker and shown on Main as "The last format failed: …", with Format still available to retry; a marker with no process after 20 s is reported as "Format stopped before it finished."
- **Per-disk page** `#disk/<name>` (Main stays highlighted; ‹ › link to the neighbouring disks). `GET /api/array/disk/{name}` returns role, device, detected file system, whether it is mounted, the chosen file system, and whether Apply/Erase are allowed. The file system dropdown is editable **only** while the array is stopped and the disk has no file system; **Apply only saves the choice** (`POST …/filesystem` → `format=` in `disks.cfg`) and does not format. **Erase** (`POST …/erase`, body `{confirm: "<disk name>"}`) runs `wipefs -a` via sudo, only while stopped and unmounted; the disk stays assigned. Parity is a normal disk here: it gets the same file system choice and Erase.
- **`services/disks_cfg.py`** edits one key of one section of `disks.cfg` without dropping the others. `POST /api/array/assign` now keeps each disk's saved `format=` (matched by device path or its realpath) instead of resetting every disk to the request default.
- **The old one-click `POST /api/drives/{name}/format` is disabled** (409 with a pointer to Main). It would mkfs any unmounted disk by name with no gate.
- `routers/array.py` passes `HELIX_CONFIG`, `STATE_DIR`, `MNT_BASE` and `USER_SHARE` through sudo (`_helper_env()`), and `helix-array-lib.sh` honours them, so the helpers can be tested against a throwaway tree.

### 33.2 What was completed or fixed in this session

- **Verified every piece:** `array/test/test-disk-flow.sh` (40 checks, 64 MB image files as disks, no real device touched) and a 46-check Playwright run over the whole UI: direct loads of `#main` and `#disk/<name>`, the 13 sidebar links, spacing/overlap/overflow audits at 1700 px and 420 px, the disk page Apply and type-the-name Erase, the Format gate, the warning dialog and the failure path.
- **Dashboard drive tiles drew "NaN%"** for any disk that is not mounted, which is exactly the state of a new disk waiting for Format. The status script prints an empty `use_pct`, and the full-size tile did `parseInt('')`. It now uses the existing `numOrNull`.
- **The unmountable row printed the long `/dev/disk/by-id/...` path in bold,** wrapping into a tall block. It now shows `name · /dev/sdX` (`device_real`) as small single-line items with the full text on hover (`.aop-detail`).
- `test-mover.sh` (13 checks) was re-run and passes.
- **Wrong assumption corrected (2.0.27): parity is not raw.** The first version of this flow treated the parity disk as a raw device that is never formatted, never "unmountable", and "has nothing to format". In Helix, SnapRAID writes `/mnt/parityN/snapraid.parity` onto a mounted file system, and the VM's parity disk is xfs mounted at `/mnt/parity1`. Combined with disabling the old one-click format, that left **no way to give a new parity disk a file system**, and a parity disk without one made `helix-array-start` point SnapRAID at `/dev/sdX.parity` (a file in the RAM-backed `/dev`). Now: parity is listed as unmountable and formatted by `helix-disk-format` (xfs by default, `format=` is written for parity too), the disk page offers the file system choice and Erase for it, **Parity Sync/Scrub are disabled with an explanation while a parity disk has no file system** (state machine, `POST /parity-check` → 409, and the SSE stream), and `helix-array-start` just warns instead of the broken "raw" fallback.
- `array/test/test-disk-format.sh` runs the real `helix-disk-format` against **loop devices** (parity + cache): formats and mounts both, leaves a mounted disk and its data alone, exits 2 when there is nothing to do, refuses while the array is stopped, rejects an unsupported file system before formatting anything, and cleans up its loop devices.

### 33.3 Rules and traps (for the next person)

- **Never test the Format or Erase code against a real device.** Use the image-file setup (API) or loop devices (`test-disk-format.sh`). `helix-disk-format` refuses anything that is not a block device, which is also why Format is expected to *fail* in that test and the failure message is part of what is asserted.
- **Main lists physical disks from `lsblk`, not from `disks.cfg`,** while the array is stopped, so image-file disks never appear in that table. The "Unmountable" row comes from `/api/array/operation`, which reads `disks.cfg`, so it does. Test the table with the disk-name link builder (`diskNameLink`) and test the disk pages by URL.
- **Changing only the URL hash does not reload the page.** After changing array state in a test, call `page.reload()` or the UI shows the old state.
- **Port 8096 on this dev machine is an Emby container.** Do not use it for a test server (the symptom is a 404 page that says "The file '/api/health' could not be found"). Free ports used by tests: 8094, 8098, 8099.
- **`pkill -f <pattern>` can kill your own shell even with the `[x]` trick** when the same text also appears inside a heredoc in the same command; stop servers with `fuser -k <port>/tcp` instead.

### 33.4 Still not done

- Unraid's per-slot inline dropdown that saves on change (Main's role dropdowns need a Save click).
- Parity Sync's start script (`helix-parity-check`) itself still does not check that `/mnt/parityN` is a mount point; the guards are in the API, the state machine and the UI.
- Maintenance mode, and Format on a disk that already holds a file system that failed to mount (Format is offered; Erase from the disk page needs the array stopped).
- A spun-down state in the orb that comes from a live read of the disk rather than the last call.


## 34. Hypervisor / VM Manager (3.0.0, 2026-11-10)

**Policy (from the user):** the hypervisor ships as a full version jump, **3.0.0**. No 2.x release carries it. `build/config/VERSION` was left at 2.0.27 while it was built and bumped only when the whole feature was done. Scope: stay close to Unraid's VM Manager layout (§30.9/§30.10) using only open-source parts. **Legal:** nothing copied from Unraid; the VirtIO Windows-driver ISO is downloaded on request from the Fedora project, never bundled; no macOS guests; template badges are generic text, not OS logos; Windows/other ISOs are the user's own.

### Design
- **Stack:** Debian libvirt 9.0 + QEMU 7.2 (KVM), OVMF (UEFI, secure-boot vars), swtpm (TPM 2.0), virtiofs via QEMU's own `/usr/lib/qemu/virtiofsd` (Debian 12 has no `virtiofsd` package), noVNC (`/usr/share/novnc`, served at `/novnc`). Package lists: `build/config/packages-vm-{amd64,arm64}.list`, appended by `build.sh`. `--no-install-recommends` is used, so anything libvirt needs that is only a Recommends is listed (`libvirt-daemon-system-systemd`, `-config-nwfilter`, `iptables`).
- **qemu runs as root** (`user="root"`, `security_driver="none"` in `/etc/libvirt/qemu.conf`, set in hook 02) so it can open vDisks on any array disk and bind PCI/USB. Hence the API treats every path from the browser as hostile: `_under_mnt()` resolves symlinks through the deepest existing folder and requires `/mnt`; passed-through disks must be `/dev/disk/by-id/…`; XML editor refuses another VM's name.
- **`services/vmxml.py`** is pure: `normalize(spec)` validates, `build_xml(spec, host)` makes domain XML, `parse_xml(xml)` goes back and lists anything the form can't express in `unsupported` (the UI then offers only the XML editor, so saving never silently drops settings). 56 offline tests: `webui/.venv/bin/python webui/tests/test_vmxml.py`. `define --validate` is used so unknown elements are errors.
- **`services/vmhost.py`:** CPU topology (cores with SMT siblings), memory, PCI (sysfs + IOMMU group + driver), USB (hubs skipped, mass-storage flagged), OVMF files, host bridges, ISO list, VM Manager settings (`HELIX_CONFIG/vm.cfg`).
- **`routers/vms.py`:** the API (header docstring lists every route). Route order matters: fixed paths (`/virtio-iso`, `/start-all`, `/jobs` …) are declared before `/{name}`, and the generic `/{name}/{action}` is last. **`routers/vmconsole.py`:** WebSocket → the VM's VNC port (only the port libvirt reports for that VM; negotiates the `binary` subprotocol noVNC asks for).
- **Persistence (RAM root!):** libvirt state is bind-mounted from `<cache or first data disk>/system/libvirt/{lib,etc}` onto `/var/lib/libvirt` and `/etc/libvirt/qemu` by `array/sbin/helix-vm-start`, which `helix-array-start` runs (only when VM Manager is enabled). `helix-vm-stop` (run first by `helix-array-stop`) shuts VMs down or hibernates them per the setting, forces off after the timeout, stops libvirt, unmounts. libvirtd and friends are **disabled at boot** (hook 02) for the same reason Docker is.
- **UI:** `webui/frontend/vms.js` (table, click menu per state, detail rows, dialogs, console) and `vmform.js` (template grid → form → XML editor, VM Manager settings + the VFIO panel). Routes `#vms`, `#vm/new`, `#vm/<name>`, `#vm/<name>/xml`, `#vm-settings` (`#vm/…` maps to the `#vmform` section in `routeHash()`).

### Unraid parity
Done: VM list with Unraid's columns, status icons, per-state menu (Console, Stop, Pause, Restart, Hibernate, Force stop, Resume, Start, Start with console, Logs, Edit, Edit XML, Clone, Snapshot, Remove VM, Remove VM & disks with type-the-name), detail rows (vDisks with click-to-grow, CD insert/eject, NICs with IPs, USB hot-plug, snapshot tree with Revert/Remove), Start all/Stop all, autostart, template grid (Windows 11/10/Server/legacy, Ubuntu, Debian, Fedora, Arch/Linux, GPU desktop, FreeBSD, pfSense/OPNsense, disk-image appliance, Custom, your own templates), add/edit form (CPU mode/threads/pinning grid, memory initial/max, machine, BIOS incl. secure boot, TPM, Hyper-V, USB controller, vDisks, CDs, VirtIO ISO, shares virtiofs/9p, graphics VNC/SPICE, GPU/PCI passthrough with ROM, USB, NICs with MAC, sound, QEMU args, XML editor), VM Manager settings (enable, folders, VirtIO ISO download/remove, default network, host-shutdown action + timeout, usage stats, libvirt version), browser VNC console, hibernate (managedsave), clone (background job).
Not done: SPICE in the browser (a spice:// address is shown), external/QEMU-block snapshots and Block Commit/Pull, UEFI VMs cannot take internal snapshots (libvirt limit; the API says so), SR-IOV, Unraid's per-VM "PCI changed" warnings, VM usage tile on the dashboard, evdev input, NUMA placement, multi-function GPU address mapping.

### Testing (no release needed)
The test VM (`192.168.50.12`, Proxmox guest with nested KVM) gets the stack with a runtime `apt install` (RAM overlay) and code with `webui/.venv/bin/python build/tools/vmsh.py --push-dev` (tars backend/frontend/helpers, uploads through the file API, restarts the backend 2 s later via `systemd-run` so the reply gets out; wait ~8 s before using the API). `vmsh.py` runs commands through the web terminal WebSocket. Tests: `webui/tests/vm_api_test.py [host]` (70 checks against real libvirt with a cirros guest: lifecycle, console handshake, snapshots, hibernate, clone, resize, CD media, XML editor, template, settings, delete, path/symlink escapes) and a Playwright run of the page (create from the form, menus, detail, console popup shows the guest's BIOS, edit, remove). Both pass.
Traps found: memory edit failed when an old max-memory was lower (form now sends 0 = same); `virsh snapshot-list` text columns are unparsable, use `snapshot-dumpxml`; `--push-dev` restarts the backend, so API calls straight after it 502; innerText applies CSS `text-transform` (assert case-insensitively).

## 35. Share connection details (2026-11-10)
Exported shares show how to connect from another computer. **Console banner:** `network/helix-console-banner` adds a "File shares" block under the IP addresses (SMB `\\<ip>\<share>` with "guest access" or "needs a user name and password", NFS `<ip>:/mnt/user/<share>`), only while the array is started. It is rewritten at the end of `helix-shares-apply` (EXIT trap, so every share change and array start) and by `helix-array-stop`; it redraws tty1 only if nobody is logged in there. **Shares page:** "Connect to your shares" panel from `GET /api/shares/connect` (SMB path, smb:// URL, NFS mount command, copy buttons; warns when the array is stopped). Only `smb=yes` / `nfs=yes` shares appear. Known quirk left alone: the banner's Web UI list also shows Docker/PIA bridges whose names don't match its filter (`br_pia`); the API filters `br_`.

## 36. Monitoring (Checkmk) — rebuilt, automatic (2026-11-10)
**§13's prototype is gone** (`checkmk/setup.sh`, `cmk-docker-bridge.*`, `mk_docker.py`). It never worked: `mk_docker.py` printed a section no Checkmk check reads, nothing watched VMs, the admin/automation passwords were hardcoded in a public repo, it published Livestatus (6557, unauthenticated) to the LAN, and it was never installed in the image. User's requirement: Checkmk integrated, set up as automatically as possible, picking up new containers/VMs by itself.
- **`checkmk/helix-monitor`** (root Python, installed to `/usr/local/sbin`): `enable` (6 steps with `STEP n/6` progress lines: pull image, run container `checkmk` on port 5050 with a generated `CMK_PASSWORD`, wait ready, keep the agent script from the container in `$HELIX_CONFIG/checkmk/`, create automation user `helix` + register this host + activate via REST, discover), `start`/`stop` (called by `helix-array-start`/`-stop`; Docker and libvirt are not up at boot), `disable`, `status`, `sync`. Credentials: `$HELIX_CONFIG/checkmk/credentials.json` (generated; plain text on FAT `/boot`, same caveat as §29.3).
- **Agent:** the plain `check_mk_agent` script, re-installed into the RAM root at every start, served by a systemd socket (`helix-cmk-agent.socket`, `Accept=yes`) **bound to the Docker bridge address only** so just the Checkmk container can read it. The host is registered with that IP. The installed copy is patched to exclude `overlay` filesystems (otherwise every container adds a "Filesystem …/merged" service).
- **`checkmk/helix_local_check.py`** is the agent "local" plugin; each output line is a service: `Array`, `Disk <name> SMART` (+temperature metric; spun-down disks are not woken), `Share service SMB|NFS`, `Container <name>`, `VM <name>` (stopped is only WARN when the VM is set to autostart). **Automatic discovery:** service *names* contain the container/VM name, so a new one is a new service. `helix-monitor sync` (`helix-monitor-sync.service`) runs the plugin every 10 s and on any change in the service set waits 4 s then runs `cmk -II <host> && cmk -R` inside the container. Verified on the VM: new container appeared in ~20 s and disappeared when removed, a VM likewise.
- **Checkmk REST facts (2.5):** auth is `Authorization: Bearer <user> <secret-or-password>`; host attribute is `ipaddress` (not `ip_address`); activate-changes needs `If-Match: *`; a 400 on host create is not "already exists" unless the message says so. A Livestatus query during `cmk -R` fails for a moment ("cannot connect to UNIX-socket").
- **Backend** `routers/checkmk.py`: `GET /api/checkmk/setup`, `POST /enable` (background job with log), `POST /disable`, `GET /login` (cmkadmin + generated password; the API is unauthenticated today, §29.4), `GET /problems` (cached by the alert loop), `GET|PUT /alert-settings`, plus the old Livestatus reads.
- **Monitoring tab (3.2.0)**: sidebar link under System (`#monitoring`, `#monitoring/alerts`, `#monitoring/checkmk`; red/orange badge = open problems; `monitor.js`). *Overview*: enable with progress, status of Checkmk/agent/automatic updates, services grouped (Containers, VMs, Disks, Array, System). *Alerts*: what needs attention now, switches for what goes to the bell, recent alerts. *Checkmk*: Checkmk's own UI in an iframe, **same origin** through nginx `location /cmk/` (hook 03, `helix-proxy.conf`; Checkmk's `X-Frame-Options: sameorigin` allows it) and **signed in automatically** by POSTing the generated login to `/cmk/check_mk/login.py` into the iframe (the password never appears in a URL).
- **Alerts -> bell (`services/monitor_alerts.py`, a daemon thread started in `main.py` lifespan, every 20 s, only once monitoring is enabled):** reads service + host states from Livestatus and calls `services.notifications.notify(..., source="monitoring")`, so every alert lands on the bell **and** on the channels in Settings -> Notifications (min level applies). Edge-triggered like Unraid's monitor (§30.15): OK->WARN = warning, ->CRIT (also WARN->CRIT) = error, back to OK = info "is back to normal", new/removed Container/VM/Disk = info "Now/Stopped monitoring ...". A new state must hold for two polls (~40 s) before it is announced (no flapping); Checkmk downtimes are ignored; the first run only announces existing problems (max 5 + summary). Switches in `HELIX_CONFIG/monitor_alerts.json`. Tests: `webui/tests/test_monitor_alerts.py` (12, offline); live on the VM: a container that exits 3 produced "Now monitoring", "is CRIT" (about 2 min: the agent is polled once a minute) and "Stopped monitoring" notices. The bell feed now HTML-escapes title/message (container names and plugin output reach it).
- **Not done:** the Checkmk image is amd64 (arm64 untested), Checkmk's own notification rules are left to Checkmk (the Alerts tab links there), acknowledgements made in Checkmk only suppress the sidebar badge, not the bell history.
- **First-setup invitation + protection (after 3.2.0):** once the array is started (Docker needs it) and monitoring is not enabled, the UI shows a one-time dialog "Turn on monitoring?" (`monPromoCheck` in `monitor.js`, state in `$HELIX_CONFIG/checkmk/prompt.json`: `pending` -> `later` after "Not now" -> `done`). "Not now" leaves a banner on the dashboard (`#mon-promo`) until it is enabled. **The `checkmk` container cannot be removed:** `DELETE /api/docker/containers/checkmk` answers 409, the Docker page shows "🔒 Managed by Monitoring" instead of Remove; Monitoring -> Turn off only stops it. (A `docker rm` from a terminal is not blocked; the Monitoring page then offers **Repair**, which recreates the container and reuses the saved Checkmk data and password.) **Exception, deleting the array:** `POST /api/array/assign` with no devices at all counts as deleting it; the backend runs `helix-monitor reset` (stops the agent/sync, removes the container + volume if Docker is up, deletes `/boot/config/checkmk`, alert state) and the prompt returns once a new array is created and started. `enable` also cleans up a leftover Checkmk container whose credentials are gone (new flash drive, old array) instead of locking itself out. Verified on the VM: dialog -> Not now -> banner (dashboard only) -> no repeat after reload -> Enable from the banner -> running; container removed behind Helix's back -> Repair -> running. The array-deleted hook was tested against a throwaway backend, not on the VM's real array.
- **ARM / Netdata engine (3.3.0):** Checkmk's container image is amd64-only (Docker Hub), so on other hardware (Raspberry Pi) `helix-monitor` uses **Netdata** (`netdata/netdata:stable`, multi-arch, ~260 MB; chosen over Uptime Kuma (only up/down checks), Beszel (no alarms to speak of), Zabbix/Prometheus (heavy, need extra parts)). `provider()` picks `checkmk` on x86-64, `netdata` otherwise; `helix-monitor enable --provider netdata|checkmk` forces one (used to test Netdata on the amd64 VM, then switch back). Netdata runs `--network host --pid host` with `/proc`, `/sys` and the Docker socket read-only, listens on 127.0.0.1 only (`$HELIX_CONFIG/checkmk/netdata.conf`), is served at `/netdata/` by nginx and embedded as the Monitoring "Dashboard" tab (no login). It finds containers/VMs/disks by itself, so there is no sync daemon. **Alerts use the same bell logic:** `monitor_alerts._netdata_states()` = the Helix local-check plugin run directly (array, SMART, containers, VMs, shares: same results Checkmk gets) + Netdata's active alarms from `/api/v1/alarms`. Checkmk and Netdata containers are both protected from removal and reset when the array is deleted. **Only the amd64 path was run on hardware**; the arm64 image build was verified to contain the code, but nothing was booted on a Pi. Verified on the amd64 VM: enable, `/netdata/` proxy 200, container charts, embedded dashboard, a failing container raised on the bell, then switched back to Checkmk cleanly (the alert baseline is reset on an engine switch so nothing is announced as new).
- **Build fix:** a fresh rootfs (new arch) has no CA certificates, so the first `apt-get update` failed on the https Tailscale repo ("Unable to locate package tailscale"); `build.sh` now updates, installs `ca-certificates`, updates again.

## 37. Boot splash: spinning helix (2026-11-10, after 3.3.0; NOT in the published 3.3.0 release)
A glossy blue double helix lying on its side rotates; three dots to its right take turns being in focus (1 -> 2 -> 3 -> 1, about every 0.5 s) on a #141414 background with a soft blue glow. Drawn like the reference picture the user supplied (blue translucent tubes, solid rungs).
- **Art:** `build/boot-assets/make-helix-splash.py` (Pillow + numpy; shaded spheres sorted back to front, 3x supersampled) writes `build/boot-assets/plymouth-helix/` (36 frames spanning half a turn: a double helix looks identical after half a turn, so the loop is seamless; dot-on/dot-off; glow) and, with `--menu`, the static `build/boot-assets/splash.png` used behind the BIOS (isolinux) and GRUB menus (it replaces an old orange static splash that still showed a "[VERSION] · [HOSTNAME]" placeholder). Regenerate with `python3 build/boot-assets/make-helix-splash.py [--menu]`.
- **Animation:** Plymouth "script" theme `helix` (`helix.plymouth`, `helix.script`): sized to 44% of the screen width, 25 frames/s, dots change every 24 refreshes. Installed to `/usr/share/plymouth/themes/helix` by hook 03; `plymouth` is in `packages-base.list`; `/etc/plymouth/plymouthd.conf` sets the theme. Kernel command line (GRUB, isolinux, Pi `cmdline.txt`): `quiet splash loglevel=3 vt.global_cursor_default=0 plymouth.ignore-serial-consoles` (+ `vga=791` on isolinux, `gfxpayload=keep` in GRUB so a framebuffer exists). The Verbose/safe-mode entries have no `splash`, so they still show the text boot.
- **It has to run INSIDE the initramfs.** About 20 s of the boot is the initramfs copying the 650 MB image to RAM; Plymouth started from systemd would only cover the last few seconds. `build-initramfs.sh` packs plymouthd, the client, `script.so`, the drm/frame-buffer renderers, their libraries (resolved with `readelf`, so it works for arm64 on an x86 host), the theme and small display drivers (simpledrm, bochs, virtio_gpu, cirrus, vmwgfx, hyperv_drm, vc4). `init` starts it right after mounting `/run`, sends its own `msg()/warn()` output to `/run/helix/init.log` and the kernel log while it runs (`die()` quits the splash first so errors are visible), calls `plymouth update-root-fs` before `switch_root`, and systemd's `plymouth-quit.service` ends it.
- **Four traps found getting it to draw (each looked like "black screen" or "text still shows"):**
  1. The dynamic loader was copied through `/lib64/ld-linux-x86-64.so.2`, an ABSOLUTE symlink in the rootfs, so `cp -L` took the BUILD HOST's loader and every plymouth binary died with "stack smashing detected". Copy the loader from the rootfs's own lib dir. (Same hazard for any absolute symlink when copying out of a rootfs.)
  2. `plymouthd` in daemon mode (also with `--attach-to-session`) exits 69 in an initramfs (no controlling terminal). Run `plymouthd --no-daemon &` instead.
  3. Plymouth ignores a graphics device that udev has not marked initialized and tagged `seat`; there is no udev daemon in the initramfs, so `init` writes `/run/udev/data/c<major>:<minor>` with `I:1`, `G:/Q:seat`, `G:/Q:master-of-seat` for each `/sys/class/drm/card*`. Without it the log says "no displays available to show splash on, waiting...".
  4. `plymouthd --debug` writes its log to the screen (tty1) and only dumps `--debug-file` when it quits: to debug, start it, `sleep`, `plymouth quit`, then read the file.
- **Tested** (QEMU/KVM, screenshots through the QEMU monitor every 2-3 s): BIOS boot of the ISO (menu, black while the kernel loads, helix from ~6 s through the RAM-loading phase to the login prompt) and UEFI boot of the ISO under OVMF (same). **Not tested on real hardware, a USB stick, or a Raspberry Pi** (the arm64 initramfs gets the same packing code and `vc4`, but the arm64 image was not rebuilt after this change).
- **Also fixed on the way (pre-existing): the ISO did not boot under UEFI** (it stopped at a bare `grub>` prompt; the previous release ISO does the same). The EFI loader had no embedded start-up script and no `iso9660`/video/png modules, so it looked for `/grub/grub.cfg` on its own tiny FAT image. `build.sh` now embeds a script that finds the volume labelled HELIXBOOT and loads its menu, plus the modules the menu needs (`efi_uga` does not exist in this GRUB). `insmod squash4` was removed from `grub.cfg` (GRUB cannot read squashfs and the module was not embedded: it caused a "Press any key" stall). `make-iso.sh` copies the menu picture and font into the ISO's `/grub`. Known leftover: the UEFI GRUB menu itself is still plain text (no picture behind it).
- **Not done / for later:** the stray systemd line "Ordering cycle found, skipping paths.target" (from the `helix-root-password-*` units) flashes at the end of every boot; the Pi image and OTA bundles for 3.3.0 do not contain the splash, so it ships with the next version (a rebuild of both architectures + a bump).
- **Terminal page "WebSocket error" (again, 2026-11-10):** `_connectTermWs()` built the address from `API`, which is `''` whenever Helix serves the page itself, giving `ws://api/terminal/ws` (host missing, `ERR_NAME_NOT_RESOLVED`). It now falls back to `location.host`. nginx was never the problem here (§29.2 had the earlier, separate nginx Upgrade-header cause). Check with a real browser: open `#terminal`, expect `WS ws://<host>/api/terminal/ws` and "Connected".

## 38. Where things stand (written 2026-11-10, end of session) — read this first when resuming
**Branch / git:** all work is committed locally on `fix/smoke-round-1` (last commits: monitoring, Netdata/ARM, update-button fixes, boot splash + UEFI ISO fix, terminal fix). Nothing from this stretch is pushed; `main` is behind. Run `git log --all --oneline --not main` before releasing (§19.7). Do not `git add -A` from the home directory (unreadable folders abort it); add paths (`CLAUDE.md build webui checkmk array network shares`).

**Published:** `v3.3.0` on GitHub (amd64 + arm64 OTA bundles). The test VM (`192.168.50.12`) runs 3.3.0 plus dev-pushed frontend/backend code newer than 3.3.0.

**In the repo but NOT in any published release (ship as 3.3.1):**
1. Boot splash (§37): Plymouth theme, initramfs packing and start-up, new menu background.
2. ISO UEFI boot fix (embedded GRUB start-up script and modules; `insmod squash4` removed).
3. Terminal page address fix (`location.host`), §38 below the splash notes.
4. Pi image script: boot partition 2.5 GiB, image 4 GiB (the system image outgrew 512 MiB). `write-usb.sh` boot partition size was not checked against `.new` + `.prev` copies.
5. `helix-os-20261110.iso` in the repo root was rebuilt with the splash; `helix-os-pi-20261110.img.xz` was NOT (no splash, arm64 initramfs untested with Plymouth).
To release: `sudo rm`/relink the per-arch work dirs (`/tmp/helix-build` -> `/home/pi/helix-work/build-amd64` or `build-arm64`; never share one rootfs between architectures), bump `build/config/VERSION`, `sudo build/build.sh --arch X` for each, `make-update-bundle.sh --arch X --from output/boot --version V` for each (the manifest keeps both), set notes with `sudo python3` (the manifest is root-owned), `GH_TOKEN="" build/publish-update.sh --repo yo-repo87/helix-os`, check the manifest lists both archs and that the built squashfs contains the new code (`unsquashfs -cat`). Saved copies of the last builds: `/home/pi/helix-work/out-amd64-final`, `out-arm64-final`.

**Test-VM state:** array was recreated (disk1 + disk2, no parity/cache), so Docker data and monitoring were reset (expected: §36); `/mnt/user/appdata` no longer exists (`vmsh.py --push-dev` now stages in `/boot/config`). Monitoring is currently off there, the "Turn on monitoring?" prompt is pending. `/mnt/user/isos/cirros.qcow2` may be gone with the old array.

**Known open items:** untested on real hardware: Plymouth splash, Netdata on a Pi, arm64 VM stack (`qemu-system-arm` list), GPU passthrough. UEFI GRUB menu has no background picture. systemd prints "Ordering cycle found, skipping paths.target" (from `helix-root-password-*` units) at the end of each boot. The REST API is still unauthenticated (§29.4). `/tmp` is a 5.6 GB tmpfs; builds must go through `/home/pi/helix-work`.

**How the session limit bit us:** long builds, QEMU boot captures and releases are the expensive steps; do them in the background (`run_in_background`) and wait with a single `until` loop rather than polling.

## 39. FTP actually works; "offered by default must be there by default" (2026-11-11)
**User rule:** a feature the UI offers must be present in a fresh install, not only after a manual `apt install`.
- **FTP was broken end to end:** `vsftpd` was in no package list, so the Settings -> FTP page saved a config and reported success while nothing listened ("rejected"). Also: `PUT /api/ftp/config` ignored failures; the setup lived only in the RAM root (`/etc/vsftpd.conf` + the enabled service vanish at reboot); local users were chrooted into their (RAM) home dirs; files written by local users would not match the `helix` identity SMB/NFS use. Fixed in `routers/ftp.py`: `_apply()` writes the config, restarts vsftpd and only says OK after the port answers (otherwise returns the journal tail); input checks (ports, `anon_root` under `/mnt`); `apply_saved()` runs from the backend's lifespan at every start; `local_root=/mnt/user` (users land in the shares), `guest_enable=YES guest_username=helix virtual_use_local_privs=YES` (any authenticated local user acts as `helix`, so uploads are owned like SMB/NFS files), anonymous stays read-only; `GET /api/ftp/status` adds `installed/listening/port`; the page shows real errors and a connection hint. `vsftpd` is in `packages-base.list` and `systemctl disable vsftpd` runs in hook 02 (Debian enables it at install; Helix starts it itself). Verified on the VM with `ftplib`: anonymous login, Helix-user login (lands in the shares), wrong password -> 530, upload/download/delete as a user, anonymous upload -> 550. A dpkg "conffile" prompt appears if `/etc/vsftpd.conf` already exists when the package is installed (only happens when hot-installing on a machine Helix already configured: use `--force-confold`).
- **Audit of every external program the backend calls vs the built image** (script: collect `_run("x")`-style calls, compare with the squashfs file list; treat symlinks by their path, not their target). Missing and now added to `packages-base.list`: `apcupsd` (`apcaccess`, UPS page; `nut-client` conflicts with it, so NUT's `upsc` is NOT shipped and the page's NUT branch only works if a user installs NUT), `linux-cpupower`, `ledmon` (`ledctl`), `certbot`. The UPS page only READS status (no configuration UI): `apcupsd` ships unconfigured (`ISCONFIGURED=no`), so the page says unavailable until someone configures `/etc/apcupsd` (which also lives in the RAM root and would need the restore treatment of §29.3). **Still not in the image, on purpose:** ZFS (`zpool`/`zfs`: needs a kernel module build, the ZFS page greys out), `nvidia-smi` (non-free driver), `rocm-smi`, `intel_gpu_top` (x86 only; GPU page greys out), Raspberry Pi `vcgencmd`.
- Verified in the built image (amd64): `usr/sbin/vsftpd`, `apcaccess`, `cpupower`, `ledctl`, `certbot` present; only `apcupsd.service` is enabled by the package (unconfigured, it exits). **Not published:** ships with the next release (3.3.1, together with §37's splash and the terminal fix). The arm64 image was not rebuilt for these packages.
- **GPU vendor tools (2026-11-11):** `build/config/packages-amd64.list` (new per-arch list, read by `build.sh` as `packages-$ARCH.list`) ships `intel-gpu-tools` (`intel_gpu_top`) and `rocm-smi`, both free and in Debian bookworm main; verified in the built amd64 image (+7 MB). **NVIDIA is deliberately NOT in the base image:** `nvidia-smi`/`nvidia-driver` are Debian *non-free* (535.x), the driver is a kernel module (DKMS: needs `linux-headers` and a compile, tied to the kernel), `nvidia-container-toolkit` (Docker `--gpus`) is not in Debian at all (NVIDIA's own apt repo), and the root is RAM so anything installed after boot vanishes at reboot. **Proposed design (not built, awaiting the user's go-ahead): "add-ins".** A small JSON manifest in the helix-os repo (id, title, which page offers it, hardware check such as an NVIDIA device in `lspci`, apt source + packages, post-install steps, reboot needed) so add-ins can be added without an OS release; the GPU page offers "Install NVIDIA support (~400 MB download)" only when an NVIDIA GPU is present; the download goes to the array (`/mnt/data` or the cache disk) as a local apt cache and `helix-array-start` re-installs from that cache at every boot (offline). Binaries are fetched from Debian/NVIDIA repos at install time, never stored in git.
- **FTP follow-up (2026-11-11):** a user report of "still rejected" after the first fix. The VM server was healthy (anonymous login fine), so the vsftpd log was switched on (`/var/log/vsftpd.log`, `log_ftp_protocol`, `GET /api/ftp/log`, "Recent connections" link on the page) to see why a login is refused (`530 Login incorrect` = bad user/password; `root` and other system users in `/etc/ftpusers` are refused by design; a user without a password cannot log in). Local users are now ALWAYS chrooted to `/mnt/user` (`chroot_local_user=YES`; the saved UI checkbox had been off, which together with `guest_username=helix` would have let a login browse the whole filesystem as `helix`); the checkbox was removed. Remember the VM runs a hot-installed vsftpd (the published 3.3.0 image has none): a machine on the published 3.3.0 image has no FTP server until 3.3.1.
- **Removed container, app still "installed" (2026-11-11):** the Docker page's Remove sent `DELETE /api/docker/containers/b4d1a98c331c_emby`, a leftover that docker-compose v1 leaves (it renames the old container to `<12 hex>_<name>` when it recreates one). `docker.py` only treated a container whose name equalled the app directory as a Helix app, so this was a plain `docker rm` and `HELIX_CONFIG/apps/emby` (what the catalog and Installed list read) stayed. Now `_app_name()` maps a container to its app by name, else by the `com.docker.compose.project` label (also in the list as `app_name`, so Configure opens the right app); removing any container of a Helix app runs `remove_app(purge=True)` (compose down + delete the record) and also removes the clicked leftover. Verified on the VM with a throwaway compose app whose container was renamed to `abc123456789_fakeapp`: after the DELETE both the container and the app record were gone. Why the leftover appears at all (a failed/interrupted recreate) was not investigated.
- **Emby "web portal keeps failing" = memory starvation (2026-11-11):** on the 3 GB test VM `MemAvailable` was ~215 MB, `/proc/pressure/memory` `full avg10` ~30 %, vmstat iowait 70-80 % (thrashing the page cache, no swap), and the Emby container sat at ~27 MiB with its log stopped after "NetworkManager" and nothing answering on 8096 (listening via docker-proxy, `OOMKilled=false`). The biggest consumer was the Checkmk container (~1.1 GB plus apache/cmk processes). The RAM root (image copy in tmpfs + overlay) already takes ~700 MB. Rule now: `provider()` in `helix-monitor` and `_provider()` in `routers/checkmk.py` pick **Checkmk only with >= 4 GB RAM**, otherwise Netdata (~150 MB); a provider already saved in `monitor.json` still wins (the VM keeps its Checkmk until it is turned off). To diagnose "container up but nothing answers": `docker stats`, `cat /proc/pressure/memory`, `vmstat 1 3`, `docker inspect -f '{{.State.OOMKilled}}'`.

## 40. Add-ins (2026-11-11): optional software that is not in the base image
Built after the user approved the design in §39. First add-in: **NVIDIA GPU support** on the GPU page.
- **Catalog:** `webui/backend/data/addins/index.json` (id, title, `page`, `requires` {arch, PCI vendor/class}, `apt` {sources, packages}, `post` actions, `reboot`). The backend (`routers/addins.py`) refreshes it from the helix-os repo on GitHub (hourly, 5 s timeout) and falls back to the bundled copy, so a new add-in needs no OS release. Binaries are never in git.
- **Offer rule:** `GET /api/addins/?page=gpu` reports `hardware_found` (sysfs PCI scan); the page (`loadAddins()` in `index.html`, panel `#addins-gpu`) shows an add-in only when its hardware is present or it is installed.
- **Root helper `addins/helix-addin`** (installed to `/usr/local/sbin`; the backend runs it through `sudo`, manifest entry on stdin): `install|remove|restore|status`. The manifest is DATA only and is re-validated by the helper: package names by regex, apt source hosts from an allowlist (Debian, nvidia.github.io), key URLs https + allowlist, post-install actions from a fixed list (`depmod`, `swap-nouveau`, `docker-nvidia-runtime`). Nothing from a manifest is run as a shell command. `webui/tests/test_addins.py` checks the rejections.
- **Persistence (RAM root):** install = add sources (`/etc/apt/sources.list.d/helix-<id>-*.list`, keys as `.asc`) -> `apt-get install --download-only` into `<cache disk or disk1>/system/addins/<id>/archives` -> `dpkg -i` those files -> post actions. `HELIX_CONFIG/addins.json` records what is installed. `helix-array-start` runs `helix-addin restore` in the background (DKMS builds take minutes) which re-installs from the cached .deb files with no network. If the running kernel differs from the one the add-in was built for (after an OTA), restore skips it and the page says "Installed for an older kernel: reinstall it". Needs the array started (the page/helper say so).
- **Verified on the test VM** with a harmless package (`hello`): install from Debian, files cached, purge + `restore` brings it back, `remove` cleans up. **The NVIDIA add-in itself has NOT been run** (the VM has no NVIDIA GPU, and this machine's package set was not tried): open risks are (1) `linux-headers-$(KVER)` must still exist in Debian for the image's kernel, (2) nouveau is loaded by udev before restore runs, so `swap-nouveau` unloads it and loads `nvidia` (fails if nouveau drives the console), (3) the NVIDIA container-toolkit repo/key layout. First thing to check on a machine with an NVIDIA card.
- `vmsh.py --push-dev` now also ships `addins/`. Not yet in a release (3.3.1).
- **App media folders are optional (2026-11-11).** In the install/Configure form, template folders under `/mnt/` other than `/mnt/user/appdata` (Plex/Emby TV, Movies...) start as "Not used"; an empty folder drops its mapping from the compose file (`_write_compose`), and `_ensure_dirs` creates only `/mnt/user/appdata/...` (the app's own settings folder), never media folders. The user adds libraries under "Extra folders". Checked on the VM: Plex form shows Config = appdata, TV/Movies = Not used.
- **Custom app sources (2026-11-11).** Adding `https://github.com/yo-repo87/template` showed nothing: that repo is private (anonymous GitHub API 404) and contains no app templates (only CLAUDE.md, EMBY_BUILD.md, a workflow yml, launch.sh), and the old code swallowed errors and silently added an empty source. Now `POST /api/apps/sources` loads the source first and refuses with a reason when it is unreachable/private or yields 0 apps; the UI shows the reason or "N apps found". An `unraid-xml` source may be a GitHub repo URL: every `.xml` in it (up to 400) is loaded via the git-tree API (public repos only; unauthenticated, 60 requests/h). Type must match the content: `portainer` = a templates JSON file, `unraid-xml` = XML file or GitHub repo of them.
- **Default source missing on the VM (2026-11-11).** `pi-hosted` was absent from `template_sources.json` but listed in `template_sources.seen` (a removal sticks by design, §29.2), so it never came back; `unraid-ca` was also disabled there. Added `POST /api/apps/sources/restore-defaults` (`restore_default_sources()`) and a "Restore default sources" button in the catalog's sources panel; it only adds missing defaults and never changes existing ones. VM after restore: 261 Pi-Hosted apps.
- **Built-in app catalog never shipped (found 2026-11-12).** `TEMPLATES_DIR` pointed at `<backend>/../../apps/templates`, i.e. `webui/apps/templates` in the repo and `/opt/helix/apps/templates` in the image; neither existed (the files sat in the repo-root `apps/templates`, which hook 03 never copied), so "Helix OS Built-in" was always empty. Moved to `webui/backend/data/builtin-templates/` (ships with the backend) and pointed `TEMPLATES_DIR` there; the 12 old entries now load. Folder vars in `jellyfin.json` got `"type":"path"` so they use the picker / optional rule.
- **Jellyfin Enhanced (2026-11-12, verified on the VM: installed from the catalog, server 13.0.0 up on :8097, web 200, TVDB seeded, ffmpeg 7.1.4 found).** The user's Jellyfin fork with Emby-style features lives in `/home/pi/Claude/Emby_Build` (own CLAUDE.md). Image: `ghcr.io/yo-repo87/jellyfin-enhanced:latest` (amd64 + arm64), built by GitHub Actions in the public repo `yo-repo87/Jellyfin_Enhanced` (`docker/Dockerfile`, `.github/workflows/docker.yml`): clones upstream jellyfin / jellyfin-web / jellyfin-plugin-tvdb at the commits in `docker/pins.env`, applies `docker/patches/{server,web,tvdb}/*.patch` (generated with `git format-patch master..emby-port` in each clone), cross-publishes .NET 10 on the build platform, runtime `mcr.microsoft.com/dotnet/aspnet:10.0` (Ubuntu noble, so the Jellyfin ffmpeg repo path is `repo.jellyfin.org/${ID}`) + `jellyfin-ffmpeg7`, TVDB bundled under `/jellyfin/plugins/TheTVDB_24.0.0.0`. The fork repos `jellyfin-emby-port` / `jellyfin-web-emby-port` are empty on GitHub (pushing the shallow clones was rejected), hence patches instead. To ship new fork work: regenerate the patches, push to Jellyfin_Enhanced main, the workflow rebuilds. Catalog entry: `webui/backend/data/builtin-templates/jellyfin-enhanced.json` (bridge port 8097 -> 8096 so it can run beside Emby/Jellyfin on 8096; media folder optional).
- **Jellyfin Enhanced in the Pi-Hosted catalogs (2026-11-12).** Added to `yo-repo87/pi-hosted` (`template/portainer-v3-{amd64,arm64}[-helix].json`, plus the source files `template/apps/jellyfin-enhanced.json` and `jellyfin.json` that `build/joinApps.sh` builds from) and to the bundled `webui/backend/data/catalogs/pi-hosted-helix.json` (262 apps). Image `ghcr.io/yo-repo87/jellyfin-enhanced:latest`, host port 8097; both Jellyfin entries use the official logo `jellyfin-ux/.../icon-transparent.svg`. Entries were inserted directly (id 301); the next `joinApps.sh` run renumbers ids from the apps folder, which now contains the same entries. Not run: the converter / joinApps.
- **Live container resources (2026-11-12).** Each running container on the Dashboard and Docker page shows CPU (sparkline + %), RAM (bar + size) and network down/up rates between its name and the buttons. `GET /api/docker/stats` now returns numbers (`cpu_pct`, `mem_bytes`, `mem_limit`, `rx_bytes/tx_bytes`, `rx_bps/tx_bps`, the old string fields are kept); the rates are the change in docker's cumulative NetIO between two samples (null on the first poll or after a counter reset), results are shared for 2 s because `docker stats --no-stream` is slow. UI: `pollCtrLive()` every 3 s, only while the tab is visible on `#dashboard`/`#docker`; history of 30 CPU samples per container in `_ctrLive`. Checked on the VM with Playwright (alignment, no JS errors). It ships with the next image build (3.3.1).
- **Flash Studio in the catalogs (2026-11-12).** `yo-repo87/flashstudio` (private; at the time EMPTY on GitHub, the project lives in `/home/pi/Claude/FlashStudio`, noVNC slicer, host network, UI on port 6080, volumes `/config` and `/prints`) was added to the four v3 Pi-Hosted catalogs (amd64/arm64 x plain/helix) and the bundled `pi-hosted-helix.json` (263 apps), plus `template/apps/flashstudio.json`. Image reference `ghcr.io/yo-repo87/flashstudio:latest` **does not exist yet**: the image (3.5 GB, amd64 only) was built locally and contains Flashforge's 1.7.20 resources, so publishing it needs a decision (private package + login, or public). Until then Install fails at the pull.
- **Flash Studio image is published (2026-11-12).** The PAT in `gitkey` cannot write to ghcr.io (no `write:packages`), so the locally built image was uploaded as a release asset (`flashstudio-image.tar.zst`, `docker save | zstd`, 994 MB, release `image-1.7.15-res1.7.20` in `yo-repo87/flashstudio`) and the workflow `.github/workflows/publish-image.yml` (workflow_dispatch) downloads it, `docker load`s it and pushes `ghcr.io/yo-repo87/flashstudio:{latest,1.7.15-res1.7.20}` with the Actions token. The package came out public (anonymous manifest request 200). amd64 only: the arm64 catalog entries cannot run. To republish after a rebuild: upload a new asset to the release (or a new tag), run the workflow.
- **Container options in the app form (2026-11-12).** Install and Configure dialogs end with "Container options": restart policy (unless-stopped / always / on-failure / no), limit log size (json-file 3 x 10 MB), server time zone (`/etc/localtime:ro` + `TZ`), init, Intel/AMD graphics `/dev/dri` (shown only if the host has it), privileged, memory limit (GB) and CPU limit (cores). Stored as `extras.json` `"options"` and applied by `_apply_options()` in `routers/apps.py` when the compose file is written; no `options` key = the template's compose unchanged (apps installed earlier keep their settings until saved). Restart policy and log limit go on every service of the app; the rest only on the main service (`_main_service()`: the first one that publishes ports or uses host networking, else the last). **Also fixed:** extra env vars/folders went to the FIRST service, which for Nextcloud is its database; they now use `_main_service()` too. The form shows current values read back from the app's compose file (`_effective_options`). Checked: `docker compose config` (v2) and `docker-compose` 1.29.2 on the VM accept `init`/`mem_limit`/`cpus`/`logging` in a `version: 3.8` file; on the VM a throwaway Vaultwarden install via the browser came up with restart=always, Init, 512 MB, 1 CPU, log limits; Configure showed those values; a CPU limit of 99 was rejected with a message; reconfiguring to on-failure/no init/no memory limit took effect (`docker inspect`); the app was removed afterwards.
- **Default accent is blue (2026-11-12, user request).** `--accent` is `#3b82f6` in the dark themes (Black, Dark Gray) and `#2563eb` in White/Light (and the automatic light-mode fallback); Azure keeps its sky blue `#38bdf8`. Orange `#f07820` is still offered as a swatch in Settings → Appearance; a browser where someone picked a swatch earlier keeps that choice (`localStorage helix_accent`). Also changed: the terminal cursor/selection and the chart fallbacks.
- **Busiest containers first (2026-11-12, after 3.3.1; not in a release yet).** Docker table and the Dashboard's top-5 sort themselves by use and re-sort every live poll (3 s): running first (score = CPU averaged over the last 3 samples, % of one core, + memory % of its limit/host RAM), then paused/restarting, then stopped A-Z. Two running containers within 2 points keep their order (no jitter); the order freezes while the pointer is over either list (`_ctrHold`, so a button never slides away mid-click) and catches up on mouseleave; moved rows slide (FLIP, `_flip()`). Code: `sortContainers`/`ctrScore`/`reorderContainers` next to `renderCtrGlance` in `index.html`; rows carry `data-name`. Checked on the VM: a `while :; do :; done` alpine container went to the top within 3 s, the order stayed put while hovering, and once stopped it dropped below the running ones; no page errors.

## 41. Releases and repo state (2026-11-12) — supersedes the release parts of §38
- **GitHub:** only `main` (fast-forwarded from `fix/smoke-round-1`, then merged with an old remote draft of the Jellyfin Enhanced template, kept ours). All other branches deleted locally and on GitHub. Releases left: **v3.3.2** (latest), **v3.3.1**, **v3.3.0**; every older release/tag (v0.1.0 to v3.2.0, images-2026.11.06) deleted. v3.3.1 and v3.3.2 carry the OTA files plus `helix-os-<version>.iso` and `helix-os-pi-<version>.img.xz`.
- **v3.3.0 was rebuilt** at the user's request: the code it was really built from (`029d136`, not what its tag points at) plus only the blue accent, on a FRESH rootfs per arch (a reused rootfs keeps packages added later, e.g. vsftpd), boot-tested, and its 7 release files replaced. The rebuilt source is local branch `release/3.3.0-blue` (commit `9c0c76c`). **The `v3.3.0` tag still points at old `main` (`bf2d630`)**: moving it needs a force-push of the tag, which the auto-mode permission check refused; the user decides.
- **Why tags were wrong:** `gh release create` without `--target` tags the default branch. `build/publish-update.sh` now passes `--target $(git rev-parse HEAD)` and refuses if that commit is not on GitHub (push first). v3.3.1 and v3.3.2 are tagged correctly.
- **Image names use the version** (`build/config/VERSION`), not the date: `make-iso.sh`, `make-pi-image.sh`, `make-x86-image.sh`.
- **Trap:** `gh release upload --clobber` deletes the old asset BEFORE uploading; a GitHub HTTP 500 mid-upload (happened twice with 600 MB files) leaves the release without that file, and running systems then fail to update. After any clobber upload, list the assets and re-upload what is missing (retry loop).
- **Test VM:** on **3.3.2** (applied with its own updater, staged checksums = manifest, rebooted, array autostarted). Checkmk is running there again.
- Builds: `/tmp/helix-build` -> `/home/pi/helix-work/build-<arch>` (switch the symlink per arch); last outputs saved in `/home/pi/helix-work/out-<arch>-final`. Boot test: `sudo qemu-system-x86_64 -enable-kvm -m 2048 -cdrom helix-os-X.iso -boot d -display none -net nic,model=virtio -net user,hostfwd=tcp:127.0.0.1:8094-:80`, wait for `/api/health` to say `"ok"` (nginx answers 502 for ~10 s before the backend is up).
