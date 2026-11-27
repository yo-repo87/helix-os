# Helix OS — project notes for Claude

Helix OS is an open-source Unraid-style NAS/hypervisor OS: Debian 12 (bookworm) packed as a squashfs that boots from a
USB/SD/disk labelled `HELIXBOOT` and runs from RAM, with a FastAPI backend + single-page UI. It was designed by
reverse-reading Unraid 7.3.4; that reference (OS internals, every UI page, the mover script) lives in
**`docs/unraid-reference.md`** — read the relevant part before building a feature Unraid also has.

**Working agreement (from the user):** keep this file current in the same session a fix is made (what broke, why, how
it was fixed). Later notes win over earlier ones. **Never put passwords, tokens or API keys here** — the repo is public.
Test-box logins, API keys and PIA creds are given in chat only. Never paste Unraid source into the repo. Old detailed
history: `git show 50e47e5:CLAUDE.md` (former §§1–58) and `git show 6fbfe53:CLAUDE.md` (the long 3.3.9 notes).

---

## 1. Current state (2026-11-27) — read first

- **Released: 3.3.10** (2026-11-27, tag `v3.3.10` → `98e0819`, GitHub "Latest"): OTA amd64 + arm64,
  `helix-os-3.3.10.img.xz`, `helix-os-pi-3.3.10.img.xz`, no ISO. Adds the Processor card, free-form dashboard grid,
  mobile dashboard `/m` (§4.13) and the AMD VM-graphics add-in. 3.3.6 withdrawn, 3.3.7 never published, 3.3.5 amd64-only.
- **Tower (`.142`) runs 3.3.8** plus RAM-only hot patches (`proxy.py`, `helix-addin`, and §4.13's dashboard/mobile work;
  backups `/boot/config/backups/{cores-tile,dash-grid,mobile}-20261127/`). They vanish at reboot until 3.3.10 is applied.
  Its 3.3.8 updater has a 120 s limit: after a UI update, check `/boot/boot` for leftover `*.new` /
  `.helix-update-inflight` and finish with `helix-osupdate commit <v>` (after a checksum check) before rebooting.
  Array autostart is off: start it by hand after a reboot.
- **Checkouts:** dev = `/home/pi/Downloads/helix-os` on `.88` (remotes `origin` = GitHub, `build` = `.142`).
  Build checkout = **`/mnt/disk1/helix-work/src`** on `.142` (origin = bare repo `/mnt/disk1/helix-work/helix-os.git`);
  `build/tools/sync-to-build-host.sh` (run on `.88`) **hard-resets it**.
- **Use `/mnt/disk1/helix-work/...`, never `/mnt/user/helix-work/...`.** Through mergerfs, new and rewritten files land on
  another disk: on 2026-11-27 the checkout (even `.git/HEAD`) was split across disk1/disk2, so the disk1 path alone was
  broken. Consolidated onto disk1 (backup `/mnt/disk1/helix-work/disk2-src-backup-20261127.tgz`).
- `gitkey` (ignored, chmod 600) holds a `github_pat_` and a `ghp_` token with push to `yo-repo87/helix-os`; scripts take
  the first `github_pat_` line. Never print it. Publish with `build/builder/publish.sh` on `.142` (gh is in the builder image).
- **Machines:**
  - `192.168.50.142` = **Tower** (SSH `helix@`, `~/.ssh/helix_ed25519`): kernel 6.1.0-50, 28 GB, Ryzen 7 5700G,
    2× RTX 3050 (driver 580), iGPU used by the Android VM. Array disk1 `…CXYY4`, disk2 `…BJ9JN`, cache1 1 TB SSD
    (ext4); HELIXDATA `/dev/sdj3`. UI login `helix_admin` enforced. 4×10 TB pool `zpool` (ex-TrueNAS): host auto-import
    disabled (`/boot/config/zfs/pools.json.disabled-20261120`) — ask before re-enabling. VM backups
    `/boot/config/vm-backup-20261120/`. Site-specific extras in §8.
  - `192.168.50.12` — old QEMU test VM (3.3.2).
- **Public URL:** `https://helixos.arc-it.uk/` (= WebAuthn RP ID).

## 2. Repo layout

| Path | What |
|---|---|
| `webui/backend/` | FastAPI app (`main.py`, `config.py`, `routers/*`, `services/*`, `helpers/`, `data/` catalogs/profiles/add-ins/builtin-templates) — port 8082 |
| `webui/frontend/` | `index.html` (desktop SPA, hash routes), `mobile.html` (`/m`), `vms.js`, `vmform.js`, `monitor.js`, `firewall.js` |
| `webui/tests/` | offline Python tests (run with the venv python) + `vm_api_test.py` (live) |
| `array/sbin/` | `helix-array-{start,stop,status,autostart}`, `helix-array-lib.sh`, `helix-parity-*`, `helix-mover`, `helix-disk-format`, `helix-vm-{start,stop}`, `helix-zfs-ensure`, `helix-data-partition` |
| `shares/` | `helix-shares-apply`, `helix-samba-apply`, `helix-nfs-apply` |
| `network/` | `helix-apply-network`, `helix-console-banner`, `helix-cloudflared`, `helix-firewall` |
| `checkmk/` | `helix-monitor`, `helix_local_check.py` |
| `proxy/` | Nginx Proxy Manager integration (`install-npm.sh`, `npm.env`, `nginx.conf`, `helix-npm-*`, tests) |
| `addins/helix-addin` | add-in installer (root) |
| `build/` | `build.sh`, `hooks/0{1,2,3}-*.sh`, `initramfs/`, `config/{VERSION,packages-*.list}`, `make-*.sh`, `publish-update.sh`, `builder/`, `tools/{vmsh.py,sync-to-build-host.sh,…}` |
| `docs/unraid-reference.md` | Unraid internals + UI reference |

Excluded from git: Unraid zip/extracted trees, images, `gitkey`, `output/`, build scratch.

## 3. Runtime architecture

- **Boot:** GRUB (UEFI, embedded script finds `HELIXBOOT`) / isolinux (BIOS) → kernel + busybox initramfs → copies
  `filesystem.squashfs` to RAM → overlay root → `switch_root`. Args include `boot=helix helix_label=HELIXBOOT quiet splash`.
  Plymouth runs inside the initramfs.
- **Persistence:** root is RAM; nothing in `/etc`, `/var`, `/usr` survives a reboot.
  - `HELIX_CONFIG` = first writable of `/mnt/data/config` (HELIXDATA ext4) → `/boot/config` (FAT, `uid=1000`) →
    `$STATE_DIR/config` (RAM fallback). `STATE_DIR=/var/local/helix` (RAM): caches and job status only.
  - Array/pools: Docker data-root, libvirt state, add-in .deb caches, app data. HELIXDATA: NPM, Let's Encrypt, tunnel secrets.
- **RAM-root state restored** by `helpers/helix-restore-state` (from `helix-firstboot`): users/passwd/shadow, Samba
  passdb, crontab, `/etc/wireguard`, Docker `daemon.json`. New features writing to the RAM root need the same.
- **Backend** runs as `helix` (NOPASSWD sudo); privileged work via `services/privileged.py` (`sudo -n`) or helpers run as
  `sudo -n env HELIX_CONFIG=… /bin/bash <script>`. OpenResty on :80/:443 → 127.0.0.1:8082; fallback HTTPS **:8443**.
- **Array start** (Docker, libvirt, monitoring, add-in restore, ZFS import are disabled at boot): mount disks (by-id,
  partition-aware) → mergerfs `/mnt/user` (own systemd scope) → `/mnt/cacheN` → Docker (`data-root` on the disk that
  already has `system/docker/containers`, else cache, else disk1) → shares → VMs → monitoring → add-ins.

## 4. Subsystems — key facts

### 4.1 Array & disks
- mergerfs (`category.create=mfs`, `minfreespace=4G`) + SnapRAID (parity **file** at `/mnt/parityN/snapraid.parity`;
  parity disks are formatted like data disks). Cache is not in the union.
- `disks.cfg` stores by-id paths. **Slots are stable** (`services/disk_slots.py`): kept disks keep their number, new ones
  get the next free, missing ones kept unless in `remove_missing`, only additions while started, every write backed up to
  `disks.cfg.history/`. Mounts ≠ `disks.cfg` → mismatch row, Stop needs confirm.
- `services/array_state.py` = state machine behind `/api/array/operation`; start/stop enforce it (409 with reason).
- Format (`helix-disk-format`, `POST /api/array/format?confirm=true`) only touches unmounted disks with no partition
  table/signature; partitioned disks must be Erased (disk page, array stopped, type the name).
- Autostart: `array.cfg autostart=yes` + `helix-array-autostart.service`.
- Mover (root): skips open files, docker data-root/`system`, hidden/top-level files, paths already on the array.
  **No per-share cache policy yet.**

### 4.2 Shares
SMB `force user/group = helix`, recycle `.recycle/%U`; NFS needs `fsid=` + `all_squash` to `helix`. One identity
(`helix`) for SMB, NFS, FTP and the file manager. `path=` shares only under `/mnt` (not `/mnt/user`) and only while
mounted. Names validated by `services/validation.py`.

### 4.3 ZFS
`helix-zfs-ensure` (boot service + "Install ZFS now"): restore saved debs from `HELIX_CONFIG/zfs/cache/<kernel>`, else
build zfs-dkms from bookworm-backports. Import with `-N -R /mnt`; only pools in `HELIX_CONFIG/zfs/pools.json`
auto-import. Remove = export only. One share per dataset; a dashboard tile per pool.

### 4.4 Docker & apps
- Bookworm has compose **v1** only (`docker-compose`). Apps = compose project in `HELIX_CONFIG/apps/<name>` +
  `extras.json` + `vars.json`, regenerated on every Install/Configure (hand edits are lost).
- Sources: Unraid CA feed (~4.8k), bundled Pi-Hosted (paths remapped by `services/pathmap.py`), built-in, user sources.
  Vars `{{VAR}}` (also `$VAR`), `a|b|c` = dropdown, media folders optional, options (restart, log limit, TZ, init,
  `/dev/dri`, privileged, mem/cpu) in `extras.json` `options`.
- `checkmk`/netdata containers can't be removed (409). Live per-container stats sort lists busiest-first.
- Before adding a catalog image, check it is pullable anonymously (ghcr token + `tags/list`).
  `jellyfin-plex-enhancement` was never built (6 dead templates).

### 4.5 VMs
libvirt 9 + QEMU 7.2, OVMF, swtpm, virtiofs, noVNC `/novnc`. qemu runs as root, so browser paths are checked
(`_under_mnt`, by-id disks). `services/vmxml.py` (pure; unsupported XML → editor only), `routers/vms.py` (fixed routes
before `/{name}`; port-less graphics like `egl-headless` handled), `routers/vmconsole.py`. libvirt state bind-mounted from
`<disk>/system/libvirt`. Proxmox import (`services/pve_import.py`, `routers/pve.py`, committed `36c0e39`): scans LVM/ZFS/dir
storage and pmxcfs `config.db`; still to do: record imports, re-activate VG at array start, bridge mapping, hostpci.

### 4.6 Monitoring
`helix-monitor` picks Checkmk (x86-64, ≥4 GB; :5050, `/cmk/`) else Netdata (`/netdata/`). `helix_local_check.py`
services; `services/monitor_alerts.py` → bell, edge-triggered with a 2-poll debounce.

### 4.7 Network & remote access
- Network page: confirm-or-revert 90 s; systemd-networkd; DHCP fallback unit. WireGuard confs in `HELIX_CONFIG`;
  Tailscale state persistent; PIA routes only the `helix_pia` bridge (MTU 1420).
- **NPM:** OpenResty (wrapper keeps `nginx` name/paths), NPM 2.17.0 + certbot with DNS plugins built in; data on
  HELIXDATA; API 127.0.0.1:3781, admin :81 LAN-only; Helix is always `default_server`.
- **Cloudflare Tunnel:** one hostname → one local port; management ports and Helix's hostname refused; enable needs
  `EXPOSE TO INTERNET` + enforced login; default Access policy is "Everyone" (**not** an IdP login); secrets only in
  `/mnt/data/cloudflared`.

### 4.8 Authentication
- UI accounts, passkeys, recovery hashes: `HELIX_CONFIG/login_users.json` (**not** `users.json`). Session cookie
  `helix_login` → `STATE_DIR/login_sessions.json`.
- Enforced when `require_login` exists, any account exists, or TOTP is on. Middleware: 401 → CSRF 403 (double-submit
  `helix_csrf` / `X-CSRF-Token`) → route. Loopback without `X-Forwarded-For` is exempt (tests must send it). Open paths
  are a short list in `services/authz.py` (incl. `/` and `/m`).
- WebAuthn RP ID `helixos.arc-it.uk`; unavailable on bare IP/`.local`/HTTP. WebSockets are checked separately
  (`_WebSocketLogin`, same session + Origin = Host).

### 4.9 Add-ins
Catalog `data/addins/index.json` is fetched live from GitHub `main` (bundled fallback), so catalog changes only take
effect once on `main` — and only after the image ships a `helix-addin` that knows any new post action.
`helix-addin install|adopt|remove|restore [id]|status`; records before post actions; `.deb` cache per add-in on the
array, restored at array start. Add-ins: NVIDIA (v2, incl. libcuda1/libnvcuvid1/libnvidia-encode1), `amd-vm-graphics`
(firmware-amd-graphics + qemu-system-gui pinned to the image's QEMU, post action `amdgpu-bind`; uncommitted-branch work,
see §1). Never add a vendor's own apt source next to an add-in's (Signed-By conflict).

### 4.10 OTA updates
Only `vX.Y.Z` releases with `helix-release.json`; downloads per-arch squashfs/vmlinuz/initrd, verifies sha256, stages
`*.new`, keeps `*.prev` (initramfs falls back to it). Only on live-boot systems (`/run/helix/`). `grub.cfg` not updated by OTA.

### 4.11 Firewall
nftables table `inet helix` only (never `flush ruleset`; `nftables.service` masked). Container ports filtered in forward
by `ct original proto-dst`. Engine `services/firewall.py`, root helper `network/helix-firewall`, page `firewall.js`.
Modes off/learn/enforce (first boot = learn); SSH + UI always allowed from LAN/VPN; enforcing changes revert after 90 s
unless Keep; console escape `sudo helix-firewall off`. Extras: bans, blocklists, country rules, rate limits,
per-container outbound; notifications via `services/firewall_events.py`. Never use protocol names in rules.
Tests: `webui/tests/test_firewall*.py`, `network/test/test-firewall-live.sh`.

### 4.12 Other features
File manager, FTP (vsftpd chrooted to `/mnt/user`), MCP `POST /mcp` (bearer = API key), Pi/CPU/GPU overclock, UPS
(read-only), scheduler, backups, LAN scanner, audit log, DDNS, WoL, self-signed HTTPS, data-partition creation on old
sticks (`helix-data-partition`), Claude Code for the `helix` user on `/mnt/user/appdata/claude-code`.

### 4.13 Dashboard & mobile (2026-11-27, uncommitted)
- **Processor card** (`GET /api/system/cpu`: per-thread % from `cpu_times` deltas, threads grouped by core, MHz, temp):
  one row per core `CPU n - HT m`, polled every 2 s on the dashboard.
- **Customize = free drag + resize** (Homarr-style, plain JS `DG*` in `index.html`): 12 columns × 24 px rows, cards
  packed upward, resize from the bottom-right corner; layout saved on the server (`/api/settings/dashboard-layout` →
  `HELIX_CONFIG/dashboard_layout.json`), Reset available; below 900 px cards just stack. A `MutationObserver` picks up
  cards added/hidden by polls. (Fixed on the way: the ZFS poll deleted the firewall card, which shares `.zfs-tile`.)
- **Mobile dashboard `/m`** (`mobile.html`, phones/tablets only; `index.html` redirects mobile UAs incl. iPadOS, `/m`
  sends desktops back; "Open full site" per tab). 12 cards (needs-attention summary, system, array with start/stop,
  pools, containers/VMs with actions, notifications, GPU, network, shares, UPS, firewall), each Compact/Detailed,
  reorder by drag or ▲/▼, hide/show. **Per-user layout** `/api/mobile/layout` → `HELIX_CONFIG/mobile_layouts.json`
  (keyed by login user, `_default` without login). Own sign-in screen (password + TOTP; passkeys only on the full site).
- Tested with Playwright (desktop, iPhone 13, iPad emulation); not yet on a real phone.

## 5. Build, test, release

### Build (always on `.142`, never on `.88`)
Sync with `build/tools/sync-to-build-host.sh` (commit first). Build inside the privileged bookworm **builder container**
(host root is RAM, don't apt-install build tools on the host); register arm64 emulation per boot with
`docker run --privileged --rm tonistiigi/binfmt --install arm64`.
```bash
# per-arch scratch: /tmp/helix-build → /mnt/disk1/helix-work/build-<arch> (never share a rootfs between arches)
sudo build/build.sh --arch amd64 > build-amd64.log 2>&1     # never pipe into tail (SIGPIPE kills it)
sudo build/build.sh --arch arm64 && sudo build/make-pi-image.sh
```
- Hook 03 installs every helper and fails the build if `import main` or `nginx -t` fails.
- **Verify the image, not the source:** `unsquashfs -cat output/boot/filesystem.squashfs opt/helix/... | grep …`.
- NPM build cache: `HELIX_NPM_CACHE=/mnt/disk1/helix-work/npm-cache` (keyed on npm.env/scripts/arch/libs; any pin change misses).

### Images
- `helix-os-<v>.img.xz` (x86) = flash to USB for a real install (FAT32 HELIXBOOT 4 GiB + ext4 HELIXDATA, GRUB BIOS+UEFI).
- `helix-os-pi-<v>.img.xz` = Pi SD/USB. No ISO any more (`make-iso.sh` kept). Boot partitions are 4 GiB to hold
  current + `.new` + `.prev`.

### Release (OTA)
1. Bump `build/config/VERSION`; commit and push. 2. Build both arches; keep each `output/boot` separate.
3. `build/make-update-bundle.sh --arch <a> --from <out>/boot --version X.Y.Z` per arch.
4. **Verify every manifest sha256 against the files** (wrong hashes shipped twice).
5. Boot-test the `.img.xz` as a USB stick in QEMU, BIOS and UEFI, including **creating a login account and logging in**:
   `sudo qemu-system-x86_64 -enable-kvm -m 3072 -drive if=none,id=u,format=raw,file=t.img -device qemu-xhci -device usb-storage,drive=u,bootindex=0 -display none -net nic,model=virtio -net user,hostfwd=tcp:127.0.0.1:8094-:80`
   then wait for `/api/health` (502 for ~10 s). Kill qemu afterwards (pidfile is root-only).
6. `build/builder/publish.sh` on `.142` (tags `--target HEAD`, verifies hashes, uploads, re-checks assets).
7. Apply on a test box via `/api/osupdate/{check,apply,status,reboot}`.
Before a release run `git log --all --oneline --not main` and `git worktree list` — unmerged work was lost once.

### Dev / test
- Throwaway backend: `uvicorn main:app --app-dir webui/backend --host 127.0.0.1 --port 8094` with temp `HELIX_CONFIG`,
  `STATE_DIR`, `HELIX_TUNNEL_DIR`. Free ports 8094/8098/8099; **8096 is Emby**. Stop with `fuser -k <port>/tcp`.
- Tests: `webui/tests/test_*.py` (venv python), `array/test/*.sh` (loop devices — **never real disks**), `proxy/test/*.sh`.
- Browser tests on `.142`: `docker run --rm --network host -v <dir>:/out mcr.microsoft.com/playwright/python:v1.48.0-jammy
  sh -c "pip install -q playwright==1.48.0; python /out/test.py"` (device emulation via `p.devices['iPhone 13']`).
- `vmsh.py --host <ip> --push-dev` pushes dev code (restarts the backend). Commit by path; never `git add -A`.

## 6. Open items

- Commit/push the uncommitted 2026-11-27 work and ship 3.3.10 (see §1); then Tower loses its RAM-only patches safely.
- Plex sees the AMD iGPU (bound for the Android VM) as its `default` GPU, so "Auto" hardware transcoding may use it
  instead of an RTX 3050: set Plex Settings → Transcoder → Hardware transcoding device (user decides).
- Apps can't express `runtime: nvidia` from the UI (the "NVIDIA graphics" app option exists since 3.3.6; Plex still uses
  the gpu-guard golden compose).
- Per-share cache policy, mover schedule, split level; Samba from `ident.cfg`; global Apply/Reset; dated boot-config backups.
- Not on real hardware: RAID-card drivers, Pi boot/splash, Netdata on arm64, GPU passthrough. Cloudflare tunnel never
  used for real. Mobile `/m` not tried on a real phone.
- arm64 images lag; UEFI GRUB menu has no background; systemd "Ordering cycle … paths.target" from `helix-root-password-*`.
- Secrets (API keys, PIA, app env) are plain text on FAT `/boot`; PIA has a DNS leak + no kill-switch.
- `array/test/test-partitioned-disks.sh` was lost and never rewritten.

## 7. Rules & traps

**Boot / initramfs**
- Every `modprobe x` in `initramfs/init` needs a matching `install_module x` in `build-initramfs.sh`; a missing module
  "loads" silently. Busybox `blkid` only (no `-L`/`-o value`). Get `cat /proc/partitions` + `ls /dev` before guessing.
- Plymouth in the initramfs: copy the loader from the rootfs, `plymouthd --no-daemon &`, fake udev `seat` tags.
- `mount --move` (not `--bind`) for the boot device; FAT ignores `uid=` on remount. BIOS GRUB on a loop device needs a
  `device.map`. Boot-test after any partition/boot change; boot-critical files ship with a `.prev`.

**Build / packaging**
- `--no-install-recommends` drops things you depend on; start and use every new service in a built image. Anything the
  UI offers must be in the image.
- `cp -r src existing_dir` nests. Package lists skip whole-line comments only. Fresh rootfs needs `ca-certificates`.
- `apt-get install --download-only` skips installed packages — use `apt-get download`. Edit JSON catalogs as text.

**Backend**
- User config → `HELIX_CONFIG`, never `STATE_DIR`. Never trust `/dev/sdX`; never derive slot names from list order.
- Long-lived processes started from the backend need their own `systemd-run` scope/unit (a backend restart kills its cgroup).
- Test privilege behaviour as `sudo -u helix`. `x.get(k) or d` for JSON nulls. Secrets to curl via stdin (`-K -`).
- `ip rule` priorities < 32766; tunnel bridges need the tunnel MTU.
- **Never insert code between a decorator and its function** (3.3.6 shipped a login route that made no session).
  Run regression checks with `PYTHONDONTWRITEBYTECODE=1`.

**Frontend**
- Global `[hidden]{display:none!important}`: reveal by removing `hidden`. A class's own `display` beats a later
  `display:none` of lower specificity — test that toggled bars really hide.
- `apiFetch(path)` is GET-only; use `fetchJSON` for writes. `openModal` functions live in `window._modalFns`.
- Route loaders run on `DOMContentLoaded`. Escape anything from containers/plugins (`_esc`).

**nginx**: needs `client_max_body_size 0`, WebSocket Upgrade headers, `proxy_request_buffering off`; test a >1 MB
upload and a WebSocket through :80. Order cert generation as a unit `Before=nginx`.

**Ops**
- `pkill -f pattern` can kill your own shell (also through `adb shell`). Long jobs: background + wait once.
- Every WebSocket route needs auth. Never ship generated host keys/secrets in the image.
- GPU passthrough binds by PCI address. Never unbind a GPU from `nvidia` while in use (deadlocks until reboot).
- **Through `/mnt/user` (mergerfs), files of one folder can end up on different disks.** Work on git checkouts and
  builds via `/mnt/diskN/...` paths.
- Tower's host has no compose v2 plugin (`docker compose`); use `docker-compose`. Anything hand-installed into the RAM
  root (plugins, packages) is gone after a reboot.

## 8. Tower site-specific setup (not part of the image)

- **Plex GPU** (`plex-helix-os`, both RTX 3050s): `/boot/config/plex-gpu/` holds `helix-gpu-guard.py` (watchdog,
  installed at boot by `install-gpu-guard.sh` from `/boot/config/go`; timer every 60 s; Checkmk services "GPU host",
  "GPU docker runtime", "Plex GPU passthrough", "Plex GPU auto-fix"), `docker-compose.golden.yml` (known-good Plex
  compose; the guard restores it and recreates Plex, max 3×/hour, falls back to `docker-compose` v1) and
  `init/10-nvidia-links.sh` (container start hook). **2026-11-27 fix:** after the move to driver 580 the hook kept
  pointing `libcuda.so.1` etc. at 0-byte leftovers of the old Debian 535 `nvidia/current` folder in the container, so
  Plex saw 0 GPUs; the hook now removes such links and runs `ldconfig` when the real libraries are in the standard
  path, and only links non-empty files otherwise. `/tv` mapping removed (user request). Backups `*.bak-<ts>` there.
- **AMD iGPU for VMs** (`30:00.0`, Cezanne): add-in `amd-vm-graphics` adopted (cache
  `/mnt/cache1/system/addins/amd-vm-graphics`). Until an image ships the new `helix-addin`,
  `/boot/config/addins-bridge/amd-bridge.sh` (from `/boot/config/go`) installs the newer helper and re-binds the GPU
  after array start; it exits by itself once the image's helper knows `amdgpu-bind` — then delete it and its go line.
  A VM with egl-headless can autostart before the render node exists.
- **Android VM `Android`** (Bliss OS 16.9.7 GApps, Android 13; 6 vCPU, 6 GB, 64 GB qcow2 SATA, q35 + OVMF, NAT network,
  guest adb root at 192.168.122.34:5555): 3D via virgl on the iGPU (`accel3d` + `egl-headless` with
  `rendernode=/dev/dri/renderD128`; `virsh screenshot` fails on GL displays — grab frames over VNC). `vmport off` so
  VNC drives the USB tablet. **Touch bridge** (`/mnt/user/appdata/android-vm/touchbridge/`, installed into the guest's
  `/android-2024-10-11/system.img` as `/system/bin/helix-touchbridge` + `/system/etc/init/helix-touchbridge.rc`, needs the
  guest SELinux permissive): grabs the tablet and replays it as "Helix Touchscreen" (left = finger, right = Back, wheel =
  swipe) plus a motion-only "Helix Pointer" for the arrow; W/A/S/D/arrows = second finger joystick at (640,620) r90 while
  `io.supercent.weaponrpg` is in front. Portrait games stay upright via `wm set-ignore-orientation-request true` +
  `wm fixed-to-user-rotation enabled` + `user_rotation 0` (persist in Android). Edit system.img only with the VM off
  (`qemu-nbd` → sda2 → loop-mount). Shut down with `adb shell reboot -p` (Android ignores `virsh shutdown` with a game open).
- Old long notes and the Unraid deep-dive live on in git (see top); the checkout
  `/mnt/user/docker-projects/HelixOS/UnraidProject` (branch `feature/npm`, stale) holds someone's migration scripts — leave them.
