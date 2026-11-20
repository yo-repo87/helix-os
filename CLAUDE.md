# Helix OS — project notes for Claude

Helix OS is an open-source Unraid-style NAS/hypervisor OS: Debian 12 (bookworm) packed as a squashfs that boots from a
USB/SD/disk labelled `HELIXBOOT` and runs from RAM, with a FastAPI backend + single-page UI. It was designed by
reverse-reading Unraid 7.3.4; that reference (OS internals, every UI page, the mover script) now lives in
**`docs/unraid-reference.md`** — read the relevant part before building a feature Unraid also has.

**Working agreement (from the user):** keep this file current in the same session a fix is made (what broke, why, how
it was fixed). Later notes win over earlier ones. **Never put passwords, tokens or API keys here** — the repo is public.
Test-box logins, API keys and PIA creds are given in chat only. Never paste Unraid source into the repo. The full dated fix history (old §§1–58) is in git: `git show 50e47e5:CLAUDE.md`.

---

## 1. Current state (2026-11-20) — read first

- **Version** `build/config/VERSION` = **3.3.5**, tagged `v3.3.5`. Releases on GitHub `yo-repo87/helix-os` (public):
  v3.3.0, v3.3.1, v3.3.2, v3.3.4, v3.3.5. Only `main` + `feature/npm` remain as branches.
- **This checkout:** `/home/pi/Downloads/helix-os` on `.88`, remotes `origin` (GitHub) and `build` (`.142`, SSH). (Older notes refer to `/home/pi/Claude/UnraidProject`,
  remote `helix-os`, and a PAT in `./gitkey`; that file is not in this checkout — GitHub ops need a token from the user.)
- **Uncommitted:** SSH host-key persistence (`network/helix-ssh-hostkeys` + hook 03) and WebSocket login check
  (`authz.ws_problem`, `_WebSocketLogin` in `main.py`, `tests/test_ws_auth.py`); Proxmox VM import (§4.12: `services/pve_import.py`, `routers/pve.py`, `tests/test_pve_import.py`,
  `main.py`, `index.html` card), `launch.sh` trimmed, this file + `docs/unraid-reference.md`.
- **Machines:**
  - `192.168.50.142` (hostname `Tower`, SSH `helix@` with `~/.ssh/helix_ed25519`) — real server: kernel 6.1.0-50,
    28 GB, 2× RTX 3050. Array disk1 `…CXYY4`, disk2 `…BJ9JN`, cache1 = 1 TB SSD (ext4); `array.cfg autostart=no`, so
    start the array by hand after a reboot. The 4×10 TB TrueNAS pool `zpool` was used by a TrueNAS VM (disks passed by-id).
    That VM and the imported PlexMediaServer VM were **removed from VM Manager 2026-11-20 at the user's request**
    (definitions only; their LVM disks `pve/vm-101-disk-0` / `pve/vm-102-disk-0` and the pool disks untouched; XML in
    the backup folder below). VM Manager is now empty. The pool's host auto-import is
    still disabled (`/boot/config/zfs/pools.json.disabled-20261120`) — ask before re-enabling it. **Updated to 3.3.5 + rebooted 2026-11-20**; the live backend also has the
    WebSocket-login fix (§7) hot-patched in. VM definitions (TrueNAS, PlexMediaServer) are
    backed up in `/boot/config/vm-backup-20261120/`. UI login `helix_admin` is enforced. `dollartree_worker` set to
    `restart=no`. Site-specific `helix-gpu-guard` (Plex GPU watchdog) and an SSH-host-key restore snippet run from
    `/boot/config/go`.
  - `192.168.50.12` — QEMU test VM (3 GB, root SSH), was on 3.3.2. Disks: 2×14 TB (`QM00005`, `QM00007`), 32 GB, boot disk.
- **Public URL in use:** `https://helixos.arc-it.uk/` (= WebAuthn RP ID).

## 2. Repo layout

| Path | What |
|---|---|
| `webui/backend/` | FastAPI app (`main.py`, `config.py`, `routers/*`, `services/*`, `helpers/`, `data/` catalogs/profiles/add-ins/builtin-templates) — port 8082 |
| `webui/frontend/` | `index.html` (whole SPA, hash routes), `vms.js`, `vmform.js`, `monitor.js` |
| `webui/tests/` | offline Python tests (run with the venv python) + `vm_api_test.py` (live) |
| `array/sbin/` | `helix-array-{start,stop,status,autostart}`, `helix-array-lib.sh`, `helix-parity-*`, `helix-mover`, `helix-disk-format`, `helix-vm-{start,stop}`, `helix-zfs-ensure` |
| `shares/` | `helix-shares-apply`, `helix-samba-apply`, `helix-nfs-apply` |
| `network/` | `helix-apply-network`, `helix-console-banner`, `helix-cloudflared` |
| `checkmk/` | `helix-monitor`, `helix_local_check.py` |
| `proxy/` | Nginx Proxy Manager integration (`install-npm.sh`, `npm.env`, `nginx.conf`, `helix-npm-*`, tests) |
| `addins/helix-addin` | add-in installer (root) |
| `build/` | `build.sh`, `hooks/0{1,2,3}-*.sh`, `initramfs/{init,build-initramfs.sh}`, `config/{VERSION,packages-*.list}`, `make-{iso,pi-image,x86-image,update-bundle}.sh`, `publish-update.sh`, `boot-assets/`, `grub/`, `usb-layout/`, `tools/{vmsh.py,convert-portainer-catalog.py}` |
| `docs/unraid-reference.md` | Unraid internals + UI reference |

Excluded from git: Unraid zip/extracted trees, images, `gitkey`, `output/`, build scratch.

## 3. Runtime architecture (what runs where)

- **Boot:** GRUB (UEFI, embedded start-up script finds `HELIXBOOT`) / isolinux (BIOS) → kernel + custom initramfs
  (busybox) → copies `filesystem.squashfs` to RAM → overlay root → `switch_root`. Kernel args include
  `boot=helix helix_label=HELIXBOOT quiet splash`. Plymouth splash runs **inside** the initramfs.
- **Persistence:** root is RAM; nothing in `/etc`, `/var`, `/usr` survives a reboot. Persistent places:
  - `HELIX_CONFIG` = first writable of `/mnt/data/config` (HELIXDATA ext4 partition) → `/boot/config` (FAT, mounted
    `uid=1000,gid=1000` from the initramfs) → `$STATE_DIR/config` (RAM fallback, e.g. ISO booted as CD).
  - `STATE_DIR=/var/local/helix` (RAM): caches and job status only.
  - Array/pools: Docker data-root, libvirt state, add-in .deb caches, app data.
  - HELIXDATA (`/mnt/data`, ext4): NPM data, Let's Encrypt, Cloudflare tunnel secrets.
- **RAM-root state that must be restored** (`webui/backend/helpers/helix-restore-state`, run by `helix-firstboot`):
  users/passwd/shadow, Samba passdb, crontab, `/etc/wireguard`, Docker `daemon.json`. Any new feature writing to the
  RAM root needs a copy in `HELIX_CONFIG` + a restore step.
- **Backend** runs as unprivileged `helix` (NOPASSWD sudo). Privileged work goes through `services/privileged.py`
  (`sudo -n`) or helpers run as `sudo -n env HELIX_CONFIG=… /bin/bash <script>`. nginx (OpenResty) on :80/:443 proxies
  to 127.0.0.1:8082; fallback HTTPS on **:8443** always reaches Helix.
- **Services disabled at boot and started by array start** (they need disks): Docker, libvirt, Checkmk/Netdata, add-in
  restore, ZFS pool import. `helix-array-start`: mount disks (by-id, partition-aware) → mergerfs `/mnt/user` (own
  systemd scope) → cache `/mnt/cacheN` → Docker (`data-root` on the disk that already has `system/docker/containers`,
  else cache, else disk1) → `helix-shares-apply` → VMs → monitoring → add-ins.

## 4. Subsystems — key facts

### 4.1 Array & disks
- mergerfs (`/mnt/user`, `category.create=mfs`, `minfreespace=4G`) + SnapRAID (parity **file** on a mounted fs at
  `/mnt/parityN/snapraid.parity` — parity disks are formatted like data disks). Cache `/mnt/cacheN` is not in the union.
- `disks.cfg` stores `/dev/disk/by-id/…`; resolve with `realpath` at read time. **Slot numbers are stable**
  (`services/disk_slots.py`): a kept disk keeps its slot, new disks get the next free number, missing disks are kept
  unless named in `remove_missing`, while started only additions are allowed, every write backs up to
  `disks.cfg.history/` (newest 30). Main shows a mismatch row and Stop needs confirm if mounts ≠ `disks.cfg`.
- `services/disks_cfg.py` edits one key without dropping others. `services/array_state.py` is the Unraid-style
  state machine behind `/api/array/operation`; `POST /api/array/start|stop` enforce it (409 with reason).
- File system may be on the whole disk or its only partition (`fs_device`/`fs_type` in the lib). Format
  (`helix-disk-format`, `POST /api/array/format?confirm=true`) only touches unmounted disks with **no** partition table
  or signature; partitioned disks show "Disk did not mount" and must be Erased (disk page, array stopped, type the name).
  The old one-click `POST /api/drives/{name}/format` returns 409.
- Array autostart: `array.cfg autostart=yes` + `helix-array-autostart.service` (waits ~90 s for by-id disks).
- Mover (`helix-mover`, run as root): skips open files (one Python `/proc` walk), docker data-root/`system`, hidden and
  top-level files, and paths already on the array; rsync `--remove-source-files` per file list; dry run is
  `POST /api/mover/start?dry_run=true` and never writes `mover.cfg`. **No per-share cache policy yet.**
- Deleting the whole array (`assign` with no devices) also runs `helix-monitor reset`.

### 4.2 Shares
- `shares.cfg` sections; names validated by `services/validation.py` (Unraid limits). SMB `force user/group = helix`,
  recycle `.recycle/%U`; NFS needs `fsid=` for mergerfs, `all_squash` to `helix`. One identity (`helix`) for SMB, NFS,
  FTP and the file manager.
- `path=` shares (e.g. ZFS datasets) honoured only under `/mnt`, not `/mnt/user`, and only while a real mount.
- Console banner + Shares page show connection strings (`GET /api/shares/connect`).

### 4.3 ZFS
- `helix-zfs-ensure` (boot service + "Install ZFS now" on the ZFS page): ready → restore saved debs from
  `HELIX_CONFIG/zfs/cache/<kernel>` → else bookworm-backports zfs-dkms build, pack modules, save copy. Logs
  `/var/log/helix-zfs.log`. Not run at image build. Fails clearly if Debian no longer has headers for the kernel.
- Import with `-N -R /mnt` (TrueNAS-style); only pools recorded in `HELIX_CONFIG/zfs/pools.json` auto-import at array
  start. Remove = export only (never destroy). Import/Share dialog makes one share per dataset. Dashboard has a tile
  per pool (usable space from `zfs list`).

### 4.4 Docker & apps
- Bookworm only has compose v1 (`docker-compose`). Apps = compose project in `HELIX_CONFIG/apps/<name>` + `extras.json`
  + `vars.json`; regenerated on every Install/Configure (anything hand-edited in the compose is lost — GPU apps need
  the missing "NVIDIA GPU" option, see §6).
- Template sources: Unraid CA feed (`applicationFeed.json`, ~4.8k apps), bundled Pi-Hosted (`data/catalogs/pi-hosted-helix.json`,
  paths remapped by `services/pathmap.py`), built-in (`data/builtin-templates/`), user sources (validated on add;
  `unraid-xml` may be a public GitHub repo). Defaults offered once (`template_sources.seen`); "Restore default sources".
- Template vars: `{{VAR}}` (also `$VAR`/`${VAR}` for declared keys), Unraid `a|b|c` = dropdown, media folders under
  `/mnt` other than appdata are optional ("Not used"), extra folders/env go on `_main_service()`. Container options
  (restart, log limit, TZ, init, `/dev/dri`, privileged, mem/cpu) in `extras.json` `options`.
- `_require_docker()` → 503; `_require_user_share()` → 409 when `/mnt/user` isn't mounted. Removing any container of
  an app (incl. compose `<hex>_<name>` leftovers) removes the app. `checkmk`/netdata containers can't be removed (409).
- Live CPU/RAM/net per container (`/api/docker/stats`), lists sort busiest-first.
- Before adding a catalog image, check it is pullable anonymously:
  `curl "https://ghcr.io/token?service=ghcr.io&scope=repository:<owner>/<pkg>:pull"` then `/v2/<owner>/<pkg>/tags/list`.
  (`jellyfin-plex-enhancement` was never built — those six templates are dead until it is.) Jellyfin Enhanced image is
  built by Actions in `yo-repo87/Jellyfin_Enhanced` from patches; Flash Studio image via a release-asset → ghcr workflow.

### 4.5 VMs
- libvirt 9 + QEMU 7.2, OVMF, swtpm, virtiofs, noVNC at `/novnc`; package lists `packages-vm-{amd64,arm64}.list`.
  qemu runs as root, so every path from the browser is checked (`_under_mnt`, by-id for block disks).
- `services/vmxml.py` (pure, `normalize`/`build_xml`/`parse_xml`, unsupported XML → editor only), `services/vmhost.py`,
  `routers/vms.py` (fixed routes before `/{name}`), `routers/vmconsole.py` (WebSocket → VNC).
- libvirt state bind-mounted from `<disk>/system/libvirt` by `helix-vm-start`; `helix-vm-stop` shuts down/hibernates.
- Not done: SPICE in browser, external snapshots, SR-IOV, NUMA, dashboard VM tile.

### 4.6 Monitoring
- `helix-monitor` picks **Checkmk** (x86-64 and ≥4 GB RAM; container on :5050, nginx `/cmk/`, auto sign-in iframe)
  else **Netdata** (`/netdata/`). Saved provider in `monitor.json` wins. Agent socket bound to the Docker bridge only.
- `helix_local_check.py` gives Array / Disk SMART / Container / VM / Share services; `helix-monitor sync` re-discovers
  on change. `services/monitor_alerts.py` → bell + notification channels, edge-triggered, 2-poll debounce.
- First start of the array prompts "Turn on monitoring?" (`checkmk/prompt.json`).

### 4.7 Network, VPN, remote access
- Network page with confirm-or-revert (90 s) on `PUT /config`; `helix-apply-network` writes systemd-networkd units;
  `99-helix-dhcp-fallback.network` guarantees DHCP. Static routes in `network-routes.cfg`.
- WireGuard confs in `HELIX_CONFIG/wireguard/`; Tailscale from its apt repo, state on persistent storage; PIA routes only
  the `helix_pia` bridge (rule priorities 5181/5182, bridge MTU 1420, container moved onto that network only).
- **NPM:** OpenResty replaces Debian nginx (`/usr/sbin/nginx` wrapper keeps the name/paths). NPM 2.17.0 +
  Node + certbot venv with 16 DNS plugins (+powerdns/google wheels) built into the image; data on HELIXDATA
  (`/mnt/data/npm` bound to `/data` + `/etc/letsencrypt`), disabled when no HELIXDATA. API on 127.0.0.1:3781, admin
  :81 LAN-only. Helix is always nginx `default_server`. `routers/proxy.py` drives NPM's API; refuses hosts pointing
  at Helix itself. Not yet boot-tested in a real image.
- **Cloudflare Tunnel:** remotely-managed, one hostname → one `127.0.0.1:port`, default Access app ("Everyone"
  policy — this is NOT an IdP login), management ports `{22,80,81,443,8082,8443,3781,5050,6556}` and Helix's own
  hostname refused, `NO ACCESS` typed to skip Access. Enable requires typing `EXPOSE TO INTERNET`, login enforced and a
  credential; failure → disabled (fail closed). Secrets only in `/mnt/data/cloudflared` (0600), never `/boot`.
  Helper `network/helix-cloudflared` pins 2026.11.0 with sha256.

### 4.8 Authentication
- UI accounts + passkeys + recovery hashes: `HELIX_CONFIG/login_users.json`. **Not** `users.json` (that's Samba/system
  users and its writer drops unknown keys). Sessions: cookie `helix_login` → `STATE_DIR/login_sessions.json`
  (the old `helix_session` cookie is not the gate).
- Enforced when `HELIX_CONFIG/require_login` exists, any login account exists, or TOTP is on. Middleware in `main.py`:
  401 → CSRF 403 (double-submit `helix_csrf` cookie / `X-CSRF-Token`) → route. Loopback without `X-Forwarded-For` is
  exempt (tests must send that header). Only a short list of open paths (`services/authz.py`) — never open all of
  `/api/login` or `/api/auth`. `/mcp` has its own bearer check.
- TOTP checked inside `POST /api/login`; recovery codes typed in the same field. WebAuthn (py_webauthn) RP ID
  `helixos.arc-it.uk` (`webauthn.json`); unavailable on bare IP / `.local` / non-HTTPS, with a reason. Changing RP ID
  with keys registered needs confirm.
- WebSockets (terminal = shell with sudo, VM console) are **not** seen by the HTTP middleware: `_WebSocketLogin` (ASGI)
  applies `authz.ws_problem` — same session rule + Origin hostname must match Host. `vmsh.py` (web terminal over the
  LAN) therefore needs a session once login is enforced; use SSH instead.
- Frontend: `fetch` wrapper (before `vms.js`) adds credentials + CSRF and raises `#login-gate` (z 240, outside `<main>`).

### 4.9 Add-ins
- Catalog `webui/backend/data/addins/index.json`, fetched live from `main` on GitHub (hourly) with bundled fallback —
  catalog changes only take effect once on `main`. `addins/helix-addin install|adopt|remove|restore|status`:
  manifest is data only (package regex, apt host allowlist, fixed post actions). Records **before** post actions;
  cache built with `apt-get download` (dependency closure minus what the image ships) into
  `<disk>/system/addins/<id>/archives` (atomic swap); restore at array start, skipped if kernel changed.
- NVIDIA add-in v2 includes libcuda1/libnvcuvid1/libnvidia-encode1/nvidia-persistenced. On `.142` it was adopted
  (119 pkgs cached). Never add a vendor's own apt source next to the add-in's (Signed-By conflict).

### 4.10 OTA updates
- `routers/osupdate.py` + `helpers/helix-osupdate`: only `vX.Y.Z` releases carrying `helix-release.json`; downloads
  `<arch>-{filesystem.squashfs,vmlinuz,initrd.img}`, verifies sha256, stages `*.new`, rotates `*.prev`, initramfs falls
  back to `.prev`. **Only on live-boot systems** (`/run/helix/` exists); a plain Debian install gets `git pull` +
  service restart instead. `grub.cfg` is not updated by OTA.

### 4.11a Firewall (3.3.6, core built 2026-11-20)
- nftables, one Helix-owned table `inet helix` (input + forward hooks at priority filter-10, i.e. before Docker's).
  Never `flush ruleset`; `nftables.service` is masked (Debian's would wipe Docker/PIA rules). Container ports are
  filtered in forward only for DNATed traffic into Docker bridges (`docker0`, `br-*`, `br_*`; VM bridge `br0` untouched),
  matched on `ct original proto-dst` (the published port). Container→container / →outside untouched.
- Engine `services/firewall.py` (pure: validation, `generate`, `parse_ss`, `learn_rules`, `audit`, `parse_log`), root
  helper `network/helix-firewall` (`boot|apply [--revert-after N]|confirm|revert|off|status`, checks with `nft -c`, one
  atomic transaction), API `routers/firewall.py`, page `webui/frontend/firewall.js` (`#firewall`: Overview, Rules,
  Exposure, Blocked). Config `HELIX_CONFIG/firewall.json`; pending change `STATE_DIR/firewall.pending.json`.
- Modes off / learn / enforce. First boot without a config = learn (user decision). SSH + web UI (22/80/443/8443)
  always allowed from LAN (private ranges by default) and VPN (`wg*`, `tailscale0`, 100.64/10). Enforcing changes undo
  themselves after 90 s (`systemd-run` timer running the same helper) unless Keep; the API refuses a change that would
  lock out the requesting client (409). Console escape: `sudo helix-firewall off`.
- Drops logged (`helix-fw-drop:` / `helix-fw-learn:`, 10/s) → `journalctl -k`.
- Traps: never use protocol names in rules (`ipv6-icmp` needs /etc/protocols → whole ruleset rejected); `ss -u` lists
  client sockets too — ephemeral-range UDP owned by a process is skipped, kernel listeners (WireGuard, NFS) kept.
- **Extras (enforce only):** bans (`firewall-bans.json` in HELIX_CONFIG, timed nft sets, re-applied with their remaining
  time; `helix-firewall watch` = `helix-firewall-watch.service` follows sshd + helix-backend journal, `/var/log/vsftpd.log`,
  `/var/log/samba/auth.log` (`helix-samba-apply` sets `log level = 1 auth_audit:3@/var/log/samba/auth.log`); LAN/VPN never
  banned unless allowed); web UI failed logins are printed as `helix-auth: login failed user= ip=` with the nginx peer
  (X-Real-IP / last X-Forwarded-For, never the client-supplied first entry). Blocklists (FireHOL 1, Spamhaus DROP/v6,
  custom https) and countries (ipdeny.com aggregated zones) downloaded by `helix-firewall update-lists`
  (`helix-firewall-lists.timer` daily, or the page) into `HELIX_CONFIG/firewall-lists|firewall-geo`; LAN/VPN ranges are
  removed from every list (FireHOL 1 contains RFC1918). Country block / allow-only (LAN/VPN exempt). SYN-flood and ping
  limits; per-rule new-connection limit per source. Per-container outbound (lan / none / vpn = only via `pia0`, a kill
  switch) by container IPv4 (docker inspect at apply; the watcher re-applies on container start/die).
- Tests: `webui/tests/test_firewall.py` (offline, ~100 checks), `network/test/test-firewall-live.sh` (real packets in a
  privileged container's own netns on `.142`: 45 checks incl. bans, watcher, blocklist, countries, rate limit, outbound),
  Playwright runs of the page (21 + 16 checks against a throwaway backend with a stand-in `nft`; real list downloads).
  **Not yet tested:** the 90 s undo timer on real systemd, a booted image, IPv6 container outbound (sets are IPv4 only).

### 4.11 Other features (brief)
File manager (server-side jobs, chunked upload, rules in `services/fileutil.py`), FTP (vsftpd, chrooted to
`/mnt/user`, users act as `helix`, anon read-only, applied at backend start), MCP server `POST /mcp` (bearer = API
key, fails closed), Pi overclock via `config.txt` block, CPU/GPU OC profiles, UPS read-only (apcupsd), scheduler,
backup jobs, LAN scanner, audit log, DDNS, WoL, HTTPS self-signed cert (`helix-gen-cert.service` before nginx, HTTPS
block only written once a cert exists), Claude Code for the `helix` user stored on `/mnt/user/appdata/claude-code`
(`/etc/profile.d/helix-claude.sh`).

### 4.12 Proxmox VM import (uncommitted)
`services/pve_import.py` scans LVM (`lvs`, or raw PV metadata when lvm2 is absent — over-reports deleted LVs), ZFS
zvols and dir storage; groups by `source:container:vmid`; reads VM confs from pmxcfs `config.db` on the `root` LV;
`import_spec` → vmxml spec (block disks as `/dev/disk/by-id/dm-name-…`). `routers/pve.py`: `/api/pve/{scan,confs,import}`,
503 without lvm2/hypervisor. VMs page card "Import from Proxmox". Proven live on `.142` (VMs 102, 103 booted) — RAM-only.
`lvm2` + thin tools ship from 3.3.6 (auto-activation off). To finish: record imports + re-activate their VG at array
start, bridge mapping, hostpci re-attach.

## 5. Build, test, release

### Build
**All builds (amd64 and arm64) run on `192.168.50.142`, never on this dev machine (`192.168.50.88`)** — user rule,
2026-11-20. Sync with **`build/tools/sync-to-build-host.sh`** (commit first; it pushes over SSH to the bare repo
`/mnt/disk1/helix-work/helix-os.git` and hard-resets the build checkout **`/mnt/disk1/helix-work/src`** — never edit
there). Scratch `/mnt/disk1/helix-work`. Run backend tests there with `/opt/helix/venv/bin/python webui/tests/test_*.py`.
The older checkout `/mnt/user/docker-projects/HelixOS/UnraidProject` (branch `feature/npm`) holds someone's uncommitted
migration scripts and notes — leave it alone. Its root is RAM, so build tools must not be
apt-installed on the host (they vanish at reboot): build inside a privileged bookworm **builder container** (repo +
scratch mounted; to be created), and register arm64 emulation per boot with
`docker run --privileged --rm tonistiigi/binfmt --install arm64`. The commands below run inside that container.
```bash
# per-arch scratch: point /tmp/helix-build at /mnt/disk1/helix-work/build-<arch>
# (never share one rootfs between architectures; a reused rootfs keeps packages added earlier)
sudo build/build.sh --arch amd64 > build-amd64.log 2>&1     # never pipe into tail (SIGPIPE kills it)
sudo build/make-iso.sh                                      # helix-os-<VERSION>.iso
sudo build/build.sh --arch arm64 && sudo build/make-pi-image.sh   # arm64 via qemu-user-binfmt (slow)
```
- `build.sh`: debootstrap → resolv.conf fix for the chroot → apt (packages-base + packages-<arch> + VM lists;
  Tailscale and OpenResty repos; `--no-install-recommends`) → hooks 01–03 → squashfs (zstd) → initramfs → GRUB EFI.
- Hook 03 installs every helper in `array/ shares/ network/ checkmk/ proxy/ addins/` and fails the build if the app
  can't be imported (`python -c "import main"`) or `nginx -t` fails.
- **Verify the image, not the source:** `unsquashfs -cat output/boot/filesystem.squashfs opt/helix/backend/routers/X.py | grep …`;
  for initramfs changes check the cpio contents.

### What each image is for (users ask)
- `helix-os-<v>.img.xz` (x86, `make-x86-image.sh` = `write-usb.sh` on a loop device): **flash to a USB drive for a real
  install**. FAT32 HELIXBOOT 4 GiB + ext4 HELIXDATA (grown at first boot), GRUB BIOS + UEFI.
- `helix-os-pi-<v>.img.xz` (`make-pi-image.sh`): SD card / USB for a Pi. Same 4 GiB boot partition.
- `helix-os-<v>.iso`: **no longer released (user decision 2026-11-20)**; it was only for testing on Proxmox.
  `make-iso.sh` is kept. Written to a stick an ISO boots read-only iso9660: settings not kept, OTA cannot install.
  For a VM use the `.img.xz` as a disk (e.g. Proxmox `qm importdisk`, or QEMU `-drive if=none,id=u,file=… -device usb-storage,drive=u`).
  The user flashes with balenaEtcher / Rufus (both take `.img.xz` directly).
- Boot partitions must hold three system images (current, `.new`, `.prev`, ~1 GB each) → 4 GiB everywhere. The old
  512 MiB x86 image and 2/2.5 GiB layouts silently broke as the image grew; v3.3.1–3.3.5 shipped no x86 `.img.xz`.
- **v3.3.5's release lists arm64 in its manifest but lacks the arm64 squashfs and the Pi image** — Pis cannot update
  to it. 3.3.6 supersedes it; fixing 3.3.5 itself needs a GitHub token. `publish-update.sh` now refuses such a release.

### Release (OTA)
1. Bump `build/config/VERSION` (patch on every shipped update). Commit and **push** first.
2. Build both arches on `.142`; keep each arch's `output/boot` separate (`/mnt/disk1/helix-work/out-<arch>-final`).
3. `sudo build/make-update-bundle.sh --arch <a> --from <out>/boot --version X.Y.Z` per arch.
4. **Verify every manifest sha256 against `sha256sum dist/update/*`** (v3.3.4 shipped with wrong hashes twice).
5. `build/publish-update.sh --repo yo-repo87/helix-os`: tags `--target HEAD` (refuses unpushed commits), verifies every
   manifest file's sha256, requires the flash images (`.img.xz` for amd64, `-pi-…img.xz` for arm64), uploads,
   then re-checks the release's asset list/sizes and re-uploads anything missing.
7. Boot-test in QEMU before publishing, from the flash image as a USB stick (both BIOS and UEFI/OVMF):
   `xz -dc helix-os-X.img.xz > t.img; sudo qemu-system-x86_64 -enable-kvm -m 3072 -drive if=none,id=u,format=raw,file=t.img -device qemu-xhci -device usb-storage,drive=u,bootindex=0 -display none -net nic,model=virtio -net user,hostfwd=tcp:127.0.0.1:8094-:80,hostfwd=tcp:127.0.0.1:2222-:22`
   then wait for `/api/health` = ok (502 for ~10 s first). The qemu `-pidfile` is root-owned 600: read it with `sudo` to
   kill qemu, and confirm no `qemu-system-x86_64` is left afterwards.
   `make-iso.sh` needs `xorriso isolinux syslinux-common syslinux-utils grub-efi-amd64-bin dosfstools mtools` (builder container).
8. Apply on a test box: `/api/osupdate/{check,apply,status,reboot}`, compare staged checksums, re-test with real data.

Before a release also run `git log --all --oneline --not main` and `git worktree list` — unmerged work was lost once.

### Dev / test
- Run locally: `webui/.venv/bin/uvicorn main:app --app-dir webui/backend --host 127.0.0.1 --port 8094` with throwaway
  `HELIX_CONFIG` / `STATE_DIR` (and `HELIX_TUNNEL_DIR`, `HELIX_MOVER_DIRECT=1`, `NETWORK_SBIN`=stub, small
  `NET_CONFIRM_SECS` as needed). Free ports: 8094, 8098, 8099. **8096 is Emby.** Stop servers with `fuser -k <port>/tcp`, not `pkill -f`.
- Push dev code to a box: `python3 build/tools/vmsh.py --host <ip> --push-dev` (SSH as `helix`; restarts the backend;
  wait ~8 s). `vmsh.py --host <ip> 'cmd'` runs a command.
  Hot-pushed code and hot-installed packages vanish at reboot.
- Tests: `webui/tests/test_*.py` (offline; venv python), `array/test/test-{mover,disk-flow,disk-format}.sh`
  (loop devices/image files — **never real disks**), `proxy/test/*.sh` (Docker), `webui/tests/vm_api_test.py <host>`.
- Playwright: Chromium at `~/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome` (`--no-sandbox`, plain `python3`).
  Scope selectors (the page has many hidden duplicates); `page.reload()` after state changes. Not every machine has a browser.
- Commit by path; never `git add -A`.

## 6. Open items

- 3.3.5 not yet applied/boot-tested on `.142`; NPM/OpenResty, Plymouth, tunnel never booted in a real image.
- gpu-guard golden compose on `.142` still maps the deleted `/tv` folder.
- `plymouthd` never exiting after boot (blocked PID 1 on `.142`) is fixed in the repo for 3.3.6 (plymouth-start masked +
  reaper); confirm on the first booted 3.3.6 image.
- Per-share cache policy (yes/no/only/prefer), mover schedule, split level; Samba from `ident.cfg`; notifications not
  wired to real events beyond monitoring; global Apply/Reset; dated backups of boot config.
- Not on real hardware: RAID-card drivers (3.3.4), Pi boot/splash, Netdata on arm64, GPU passthrough.
- arm64 images lag; UEFI GRUB menu has no background; systemd "Ordering cycle … paths.target" from `helix-root-password-*`.
- Secrets (API keys, PIA, app env) are plain text on FAT `/boot`; PIA has DNS leak + no kill-switch.
- `array/test/test-partitioned-disks.sh` was lost and never rewritten.
- SSH host keys: images ≤3.3.5 ship build-time keys (same private key on every install, new key every OTA). Fixed in
  the repo (`helix-ssh-hostkeys.service`, keys kept in `/boot/config/ssh-host/`); needs the next image.

## 6a. Plan for 3.3.6 (agreed 2026-11-20)

**Committed (2026-11-20, `1a37569`…`a4a2ec0`), needs the image build:** SSH host keys per machine
(`helix-ssh-hostkeys`); WebSocket login check (terminal/VM console); `helix-addin` restore via `apt-get --no-download`
(Pre-Depends); Proxmox VM import; plymouth-start masked + `helix-plymouth-reaper`; `lvm2` + thin tools with LVM
auto-activation off; `daemon.json` merged at array start; add-in restore + monitoring start in their own systemd units
(`helix-addin-restore`, `helix-monitor-start`); `/boot/config/modprobe.d` copied in before udev
(`helix-modprobe-restore`, NVIDIA add-in saves its nouveau blacklist there); "NVIDIA graphics" app option; VFIO per
PCI address (`/boot/config/vfio-pci.cfg`, `helix-vfio-bind` before udev, live handover refused while the GPU is in
use — `services/vfio_bind.py`); `helix-monitor start` waits for Docker + Repair offered when the array is up; ZFS
available drives by real whole disk with "holds data" warnings; `helix-parity-check` refuses an unmounted parity
location; `vmsh.py` over SSH. Not yet exercised on a booted image.

**New: firewall** (user request). `.142` today: iptables all ACCEPT, no `nft`, ~50 services on 0.0.0.0, most of them
Docker-published ports (which bypass a host INPUT firewall).
- nftables (add `nftables` package), one Helix-owned table `inet helix` (input + forward hooks; never edit Docker's/PIA's
  rules; filter container traffic by `ct original` dport after DNAT). Config `HELIX_CONFIG/firewall.json`, applied at
  boot and on change by a root helper; IPv6 parity.
- Core: default-deny inbound with allow rules (service/port × LAN|VPN|subnet|IP|any) and presets from what is listening
  + app ports; Docker-aware; zones (LAN, VPN wg*/tailscale0, containers, other) with UI/SSH/API LAN+VPN-only by
  default; apply = confirm-or-revert in 90 s, current client's LAN address always allowed, console `helix-firewall off`;
  brute-force bans via `fail2ban` (nftables action) for web UI (new login rate limit + failure log), SSH, SMB, FTP;
  exposure audit (port, process, who can reach it, warnings); blocked-traffic log + dashboard tile.
- **Built 2026-11-20 (see §4.11a). Also in 3.3.6 (user chose "everything"):** blocklists (FireHOL level1 / Spamhaus DROP, auto-update, kept as nft sets),
  GeoIP country blocking (free country CIDR lists, cached on persistent storage), connection rate limits + SYN-flood
  protection, per-container egress rules (e.g. "only via the VPN" / "LAN only" / "no internet").
- **First run = learn, then enforce (user decision):** monitor mode creates LAN-scoped allow rules for everything currently
  listening, logs would-be drops, and the user reviews and switches to enforce. Nothing breaks on upgrade.

## 7. Rules & traps (learned the hard way)

**Boot / initramfs**
- Read `initramfs/init` **and** `build-initramfs.sh` together: every `modprobe x` needs a matching `install_module x`
  (module files use `-`, names `_`). `modprobe` of a missing module silently succeeds.
- Initramfs has busybox `blkid` only: no `-L`/`-o value`/`-s`; output `LABEL=X` unquoted.
- Get `cat /proc/partitions` + `ls /dev` from the emergency shell before guessing at a boot failure.
- Plymouth in the initramfs: copy the loader from the rootfs (absolute symlinks pull the host's), run
  `plymouthd --no-daemon &`, fake udev `seat` tags for DRM cards.
- Use `mount --move` (not `--bind`) for the boot device; FAT ignores `uid=` on remount — set it at first mount.
- Boot-critical changes ship with a `.prev`; boot-test after any partition/boot change.

**Build / packaging**
- `--no-install-recommends` drops things you depend on (`samba-vfs-modules`, `apparmor`, libvirt bits). Start and use
  every new service in a built image. Something the UI offers must be in the image ("offered by default must be there").
- `cp -r src existing_dir` nests — `rm -rf` the destination first.
- Package lists: only whole-line comments are skipped.
- A fresh rootfs has no CA certs; install `ca-certificates` before https repos.
- `apt-get install --download-only` skips installed packages — use `apt-get download` to build caches.
- Editing JSON catalogs: edit text in place, don't round-trip through `json.dump` (huge diffs).

**Backend**
- User config → `HELIX_CONFIG`, never `STATE_DIR`. Routers import `HELIX_CONFIG, STATE_DIR` from `config`.
- Never trust `/dev/sdX`; slot names are addresses — never derive them from list order.
- Any long-lived process started from the backend (FUSE, daemons, long jobs) needs its own `systemd-run --scope/--unit`,
  or a backend restart kills it (`/mnt/user` vanished this way).
- Privilege checks: test backend behaviour as `sudo -u helix`, not root.
- `dict.get(k, d)` doesn't replace JSON `null` — use `x.get(k) or d` for third-party data.
- Cloudflare-fronted APIs may 403 Python's TLS — use `curl -K -` with secrets on stdin, never argv.
- `ip rule` priorities < 32766; tunnel bridges need the tunnel MTU; a container on two networks keeps its first route.
- Never silently dedupe across sources; check each source's count, not the total.
- Mover: never rsync-delete a cache tree onto `/mnt/user`.

**Frontend (`index.html`)**
- Global `[hidden]{display:none!important}`; reveal by removing `hidden`, not by setting `style.display`.
- `apiFetch(path)` is GET-only; use `fetchJSON(path,{method,body})` for writes.
- `openModal` buttons: functions are stored in `window._modalFns` — don't paste functions into attribute strings.
- Route loaders run on `DOMContentLoaded`, never at parse time. Fixed bars need z-index above the header (100).
- Container names/plugin output reaching HTML must be escaped (`_esc`).

**nginx**
- In front of FastAPI it needs `client_max_body_size 0`, WebSocket Upgrade headers, `proxy_request_buffering off`.
  Test a >1 MB upload and a WebSocket through :80. WebSocket URLs fall back to `location.host`.
- A drop-in `ExecStartPre` runs after the unit's own — order cert generation as a separate unit `Before=nginx`.

**Ops**
- `pkill -f pattern` can kill your own shell. Long builds/boots: run in the background and wait once.
- Diagnose "container up but nothing answers" with `docker stats`, `/proc/pressure/memory`, `vmstat 1 3`, OOMKilled.
- Never test Format/Erase against a real device.
- Every WebSocket route needs auth too — HTTP middleware doesn't cover them. Test with an unauthenticated client.
- Never ship generated host keys/secrets in the image; generate per machine at first boot and persist to the flash.
- GPU passthrough binds by PCI address, never `options vfio-pci ids=` (that takes every identical card).
- Never unbind a GPU from `nvidia` while it's in use (Plex, containers, nvidia-persistenced) — the unbind deadlocks
  holding the device lock, and anything reading that device's sysfs hangs until reboot. Stop users first, use libvirt
  `managed='yes'`/`virsh nodedev-detach`, and wrap manual rebinds in `timeout`.
