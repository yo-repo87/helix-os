#!/bin/bash
# Hook 03: Install Helix OS web backend into the rootfs.
# Runs inside the chroot after package install.
set -euo pipefail

echo "==> [hook 03] Installing Helix OS backend"

HIVE_DIR="/opt/helix"
VENV_DIR="$HIVE_DIR/venv"
BACKEND_SRC="/build/webui/backend"
FRONTEND_SRC="/build/webui/frontend"

# ── Skip if the web UI source isn't present in the build dir ─────────────────
if [ ! -d "$BACKEND_SRC" ]; then
    echo "  SKIP: $BACKEND_SRC not found in chroot (copy webui/ into build/ before building)"
    exit 0
fi

# ── Install Python 3 venv if missing ─────────────────────────────────────────
python3 -m ensurepip --upgrade 2>/dev/null || true
apt-get install -y --no-install-recommends python3-venv python3-dev gcc 2>/dev/null || true

# ── Create venv + install deps ───────────────────────────────────────────────
mkdir -p "$HIVE_DIR"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip --quiet || echo "  WARN: pip self-upgrade failed (continuing)"
# Fatal: an image whose backend deps are missing boots to an nginx 502, so a
# failed install must abort the build rather than ship a dead web UI.
if ! "$VENV_DIR/bin/pip" install -r "$BACKEND_SRC/requirements.txt"; then
    echo "  ERROR: backend dependency install failed (check chroot DNS/network)." >&2
    exit 1
fi
echo "  backend deps installed"

# ── Copy app files ────────────────────────────────────────────────────────────
# Replace, don't merge (see build.sh): on a reused rootfs these already exist and
# `cp -r` would nest backend/backend, leaving the old code running.
rm -rf "$HIVE_DIR/backend" "$HIVE_DIR/frontend"
cp -r "$BACKEND_SRC"  "$HIVE_DIR/backend"
[ -d "$FRONTEND_SRC" ] && cp -r "$FRONTEND_SRC" "$HIVE_DIR/frontend"

chown -R helix:helix "$HIVE_DIR"

# Verify the ACTUAL app imports — exactly what `uvicorn main:app` does at runtime.
# This catches a router needing a dependency that isn't in requirements.txt
# (e.g. python-multipart for Form/upload routes), which otherwise only surfaces
# as an nginx 502 on the booted machine.
mkdir -p /tmp/helix-cfg-check
if ! ( cd "$HIVE_DIR/backend" && HELIX_CONFIG=/tmp/helix-cfg-check "$VENV_DIR/bin/python" -c "import main; assert main.app" ); then
    echo "  ERROR: helix backend app failed to import — aborting (missing dep or code error)." >&2
    exit 1
fi
echo "  backend app imports cleanly (uvicorn main:app will start)"

# ── Web server: OpenResty answering as "nginx" (CLAUDE.md §54) ────────────────
# OpenResty (nginx + LuaJIT, the base Nginx Proxy Manager needs) replaces Debian's nginx.
# Everything else keeps saying "nginx": the command, nginx.service, /etc/nginx, /run/nginx.pid,
# so helix-gen-cert, routers/proxy.py and routers/ssl.py need no changes.
OR_NGINX=/usr/local/openresty/nginx/sbin/nginx
[ -x "$OR_NGINX" ] || { echo "  ERROR: openresty is not installed (check packages-base.list / the repo in build.sh)" >&2; exit 1; }
# A reused rootfs may still have Debian's nginx; it owns /usr/sbin/nginx and nginx.service.
if dpkg-query -W -f='${Status}' nginx-common 2>/dev/null | grep -q installed; then
    apt-get purge -y nginx nginx-core nginx-common nginx-light nginx-full nginx-extras >/dev/null 2>&1 || true
fi
# The package's own unit would start a second server on :80 with OpenResty's sample config.
systemctl disable openresty 2>/dev/null || true
systemctl mask openresty 2>/dev/null || ln -sf /dev/null /etc/systemd/system/openresty.service

cat > /usr/sbin/nginx <<'NGXEOF'
#!/bin/sh
# Helix: OpenResty with Helix's paths. exec keeps the process name "nginx".
exec /usr/local/openresty/nginx/sbin/nginx -p /var/lib/nginx/ -c /etc/nginx/nginx.conf -e /var/log/nginx/error.log "$@"
NGXEOF
chmod 755 /usr/sbin/nginx

mkdir -p /etc/nginx/conf.d /etc/nginx/sites-available /etc/nginx/sites-enabled /etc/nginx/snippets \
         /var/log/nginx /var/lib/nginx
chown www-data:www-data /var/lib/nginx 2>/dev/null || true
# /var/log is RAM; recreate the directories on every boot in case something empties them.
mkdir -p /etc/tmpfiles.d
cat > /etc/tmpfiles.d/helix-nginx.conf <<'TMPEOF'
d /var/log/nginx 0755 root adm -
d /var/lib/nginx 0755 www-data www-data -
TMPEOF

# Nginx Proxy Manager, built here (Node, admin UI, certbot + DNS plugins; CLAUDE.md §54).
# Also installs NPM's nginx includes and mime.types, which the nginx.conf below uses.
bash /build/proxy/install-npm.sh /build/proxy
install -m 0644 /build/proxy/nginx.conf /etc/nginx/nginx.conf
install -m 0644 /build/proxy/helix-lan-only.conf /etc/nginx/helix-lan-only.conf
mkdir -p /usr/local/lib/helix
install -m 0644 /build/proxy/npm.env /usr/local/lib/helix/npm.env
install -m 0755 /build/proxy/helix-npm-setup /usr/local/sbin/helix-npm-setup
install -m 0755 /build/proxy/helix-npm-storage /usr/local/sbin/helix-npm-storage
# The folders nginx.conf includes (/data/..., caches); with no storage state NPM stays off.
mkdir -p /data
NPM_STATE_FILE=/nonexistent /usr/local/sbin/helix-npm-setup

# Same shape as Debian's nginx.service: the ExecStartPre config test is what
# helix-gen-cert.service is ordered before.
cat > /etc/systemd/system/nginx.service <<'UNITEOF'
[Unit]
Description=Helix web server (OpenResty)
After=network-online.target remote-fs.target nss-lookup.target
Wants=network-online.target

[Service]
Type=forking
PIDFile=/run/nginx.pid
ExecStartPre=/usr/sbin/nginx -t -q -g 'daemon on; master_process on;'
ExecStart=/usr/sbin/nginx -g 'daemon on; master_process on;'
ExecReload=/usr/sbin/nginx -g 'daemon on; master_process on;' -s reload
ExecStop=-/sbin/start-stop-daemon --quiet --stop --retry QUIT/5 --pidfile /run/nginx.pid
TimeoutStopSec=5
KillMode=mixed

[Install]
WantedBy=multi-user.target
UNITEOF
systemctl enable nginx 2>/dev/null || ln -sf /etc/systemd/system/nginx.service /etc/systemd/system/multi-user.target.wants/nginx.service

if [ -d /etc/logrotate.d ]; then
cat > /etc/logrotate.d/nginx <<'LREOF'
/var/log/nginx/*.log /var/log/npm/*.log {
    daily
    rotate 3
    missingok
    notifempty
    compress
    delaycompress
    sharedscripts
    postrotate
        [ -s /run/nginx.pid ] && kill -USR1 "$(cat /run/nginx.pid)" 2>/dev/null || true
    endscript
}
LREOF
fi
echo "  web server: $("$OR_NGINX" -v 2>&1 | head -1)"

# ── Nginx config: reverse proxy to FastAPI backend (HTTP :80 + HTTPS :443) ────
# Shared proxy block, included by both the HTTP and HTTPS servers.
mkdir -p /etc/nginx/snippets
cat > /etc/nginx/snippets/helix-proxy.conf <<'PROXYEOF'
# NPM's log format ("proxy", the http-level access log) prints $server; NPM's own server blocks
# declare it, so the Helix UI must too or nginx rejects the config when no NPM host exists yet.
set $server "127.0.0.1";
set $port   "8082";
# SSE / streaming: disable buffering
proxy_buffering off;
proxy_cache off;
# Uploads: nginx's default 1 MB cap made every larger file upload fail with HTTP 413, and
# buffering the body to disk first would double the I/O. Stream it straight to the backend.
client_max_body_size 0;
proxy_request_buffering off;

# Checkmk's own web UI (Monitoring -> Checkmk shows it inside Helix). Same origin as Helix, so it can be framed.
# Netdata's dashboard (the monitoring engine on ARM boards, where Checkmk has no image). It listens on 127.0.0.1 only.
location /netdata/ {
    proxy_pass         http://127.0.0.1:19999/;
    proxy_http_version 1.1;
    proxy_set_header   Host $http_host;
    proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header   Upgrade $http_upgrade;
    proxy_set_header   Connection $connection_upgrade;
    proxy_read_timeout 3600s;
    gzip off;
}

location /cmk/ {
    proxy_pass         http://127.0.0.1:5050/cmk/;
    proxy_http_version 1.1;
    proxy_set_header   Host $http_host;
    proxy_set_header   X-Real-IP $remote_addr;
    proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header   X-Forwarded-Proto $scheme;
    proxy_read_timeout 300s;
}

location / {
    proxy_pass         http://127.0.0.1:8082;
    proxy_http_version 1.1;
    proxy_set_header   Host $host;
    proxy_set_header   X-Real-IP $remote_addr;
    proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header   X-Forwarded-Proto $scheme;
    # WebSockets (the Terminal page): without these two headers nginx forwards a plain GET,
    # the backend answers 404 and the browser reports "WebSocket error".
    proxy_set_header   Upgrade $http_upgrade;
    proxy_set_header   Connection $connection_upgrade;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
}
PROXYEOF

cat > /etc/nginx/sites-available/helix <<'NGINXEOF'
# "Connection: upgrade" only for WebSocket requests, "close" otherwise (standard nginx recipe).
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

# HTTP — served as-is (no forced HTTPS redirect, so http:// keeps working).
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    include snippets/helix-proxy.conf;
}

# HTTPS (:443) is dropped into helix-https.d by helix-gen-cert once a cert
# exists. The wildcard matches zero files without error, so a missing or failed
# cert can NEVER stop nginx (and the HTTP UI) from starting.
include /etc/nginx/helix-https.d/*.conf;
NGINXEOF
mkdir -p /etc/nginx/helix-https.d

# Remove default site, enable ours
rm -f /etc/nginx/sites-enabled/default
ln -sf /etc/nginx/sites-available/helix /etc/nginx/sites-enabled/helix

# ── Tailscale (installed from the official repo by build.sh) ────────────────────
# The root filesystem is RAM, so tailscaled's default state dir (/var/lib/tailscale) would give this
# machine a brand-new identity on every boot. The wrapper keeps the node key on persistent storage,
# picking the same place the web UI keeps its config (HELIXDATA, then the boot drive).
cat > /usr/local/sbin/helix-tailscaled <<'TSEOF'
#!/bin/sh
dir=/var/lib/tailscale
for d in /mnt/data/config /boot/config; do
    if [ -d "$d" ] && [ -w "$d" ]; then dir="$d/tailscale"; break; fi
done
mkdir -p "$dir" && chmod 700 "$dir" 2>/dev/null
exec /usr/sbin/tailscaled --state="$dir/tailscaled.state" --socket=/run/tailscale/tailscaled.sock --port=41641
TSEOF
chmod 755 /usr/local/sbin/helix-tailscaled
mkdir -p /etc/systemd/system/tailscaled.service.d
cat > /etc/systemd/system/tailscaled.service.d/helix.conf <<'TSEOF'
[Unit]
After=helix-firstboot.service network-online.target
Wants=network-online.target
[Service]
ExecStart=
ExecStart=/usr/local/sbin/helix-tailscaled
TSEOF
echo tun > /etc/modules-load.d/helix-tun.conf          # /dev/net/tun for the tailscale0 interface
printf 'net.ipv4.ip_forward=1\nnet.ipv6.conf.all.forwarding=1\n' > /etc/sysctl.d/99-helix-forwarding.conf   # exit node / subnet routing (Docker turns this on anyway)
systemctl enable tailscaled 2>/dev/null || ln -sf /lib/systemd/system/tailscaled.service /etc/systemd/system/multi-user.target.wants/tailscaled.service
[ -x /usr/sbin/tailscaled ] && [ -x /usr/bin/tailscale ] || { echo "  ERROR: tailscale binaries missing from the image" >&2; exit 1; }
echo "  tailscale $(/usr/bin/tailscale version 2>/dev/null | head -1) installed"

# ── Self-signed TLS cert for HTTPS (443) ─────────────────────────────────────
# Generated per-machine at first boot (persisted on /boot/config when writable;
# regenerated in RAM for a read-only ISO/CD boot). nginx references the copy in
# /etc so a cert always exists even when /boot is read-only.
cat > /usr/local/sbin/helix-gen-cert <<'CERTEOF'
#!/bin/sh
# helix-gen-cert — ensure a self-signed cert exists and, only then, enable the
# nginx :443 server block. Ordered Before=nginx.service so it runs before nginx's
# own `nginx -t`. No `set -e`: a cert failure must leave nginx serving HTTP, not
# abort and take the whole web UI down.
set -u
ETC_DIR=/etc/helix/ssl
PERSIST_DIR=/boot/config/ssl
HTTPS_DIR=/etc/nginx/helix-https.d
mkdir -p "$ETC_DIR" "$HTTPS_DIR" 2>/dev/null || true

have_cert() { [ -s "$ETC_DIR/helix.crt" ] && [ -s "$ETC_DIR/helix.key" ]; }

if ! have_cert; then
    if [ -s "$PERSIST_DIR/helix.crt" ] && [ -s "$PERSIST_DIR/helix.key" ]; then
        # Reuse a persisted per-machine cert (survives reboots / updates).
        cp "$PERSIST_DIR/helix.crt" "$ETC_DIR/helix.crt" 2>/dev/null || true
        cp "$PERSIST_DIR/helix.key" "$ETC_DIR/helix.key" 2>/dev/null || true
    else
        CN="$(hostname 2>/dev/null || echo helix)"
        SAN="DNS:$CN,DNS:$CN.local,DNS:localhost,IP:127.0.0.1"
        for ip in $(ip -o -4 addr show scope global 2>/dev/null | awk '{split($4,a,"/");print a[1]}'); do
            SAN="$SAN,IP:$ip"
        done
        openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
            -keyout "$ETC_DIR/helix.key" -out "$ETC_DIR/helix.crt" \
            -subj "/CN=$CN" -addext "subjectAltName=$SAN" >/dev/null 2>&1 || true
        # Persist to /boot/config when writable (skipped on read-only media).
        if have_cert && mkdir -p "$PERSIST_DIR" 2>/dev/null && [ -w "$PERSIST_DIR" ]; then
            cp "$ETC_DIR/helix.crt" "$PERSIST_DIR/helix.crt" 2>/dev/null || true
            cp "$ETC_DIR/helix.key" "$PERSIST_DIR/helix.key" 2>/dev/null || true
        fi
    fi
fi
# nginx's master reads the key as root, but the unprivileged backend runs
# `nginx -t` for proxy/status and needs read access too (group helix).
if getent group helix >/dev/null 2>&1; then
    chgrp helix "$ETC_DIR/helix.key" 2>/dev/null || true
    chmod 640 "$ETC_DIR/helix.key" 2>/dev/null || true
else
    chmod 600 "$ETC_DIR/helix.key" 2>/dev/null || true
fi
chmod 644 "$ETC_DIR/helix.crt" 2>/dev/null || true

# Enable the HTTPS server block ONLY if we actually have a cert.
if have_cert; then
    cat > "$HTTPS_DIR/https.conf" <<HTTPSCONF
server {
    listen 443 ssl default_server;
    listen [::]:443 ssl default_server;
    server_name _;
    ssl_certificate     $ETC_DIR/helix.crt;
    ssl_certificate_key $ETC_DIR/helix.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_session_cache   shared:HelixSSL:1m;
    include snippets/helix-proxy.conf;
}
HTTPSCONF
else
    rm -f "$HTTPS_DIR/https.conf" 2>/dev/null || true
fi
exit 0
CERTEOF
chmod 0755 /usr/local/sbin/helix-gen-cert

# Generate the cert in its OWN oneshot service ordered Before=nginx.service, so
# the cert exists before nginx runs its built-in `ExecStartPre=nginx -t` config
# test. (A drop-in ExecStartPre would run AFTER nginx's own test and so fail it,
# leaving nginx down → connection refused.)
cat > /etc/systemd/system/helix-gen-cert.service <<'GCEOF'
[Unit]
Description=Helix OS — generate self-signed TLS cert for nginx HTTPS
After=local-fs.target
Before=nginx.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/helix-gen-cert

[Install]
WantedBy=multi-user.target
GCEOF

# Make nginx explicitly wait for the cert service too (belt and suspenders).
mkdir -p /etc/systemd/system/nginx.service.d
cat > /etc/systemd/system/nginx.service.d/helix-ssl.conf <<'DROPEOF'
[Unit]
Wants=helix-gen-cert.service
After=helix-gen-cert.service
DROPEOF

systemctl enable helix-gen-cert 2>/dev/null || echo "  (helix-gen-cert enable deferred)"

# ── Validate the HTTPS config at build time ──────────────────────────────────
# openssl is required for the cert; fail loudly if it's missing from the image.
command -v openssl >/dev/null || { echo "  ERROR: openssl not installed — add it to packages-base.list" >&2; exit 1; }
# Mint a throwaway cert (this also writes helix-https.d/https.conf), validate the
# full config with nginx -t, then remove the cert + https drop-in so each machine
# mints its own at first boot (via helix-gen-cert.service, ordered before nginx).
/usr/local/sbin/helix-gen-cert
if [ ! -s /etc/nginx/helix-https.d/https.conf ]; then
    echo "  ERROR: helix-gen-cert did not produce the HTTPS server block (openssl failure?)." >&2
    exit 1
fi
if ! nginx -t 2>&1; then
    echo "  ERROR: nginx config invalid — aborting." >&2
    exit 1
fi
# Also confirm nginx -t still passes with NO cert present (HTTP-only fallback).
rm -f /etc/nginx/helix-https.d/https.conf \
      /etc/helix/ssl/helix.crt /etc/helix/ssl/helix.key \
      /boot/config/ssl/helix.crt /boot/config/ssl/helix.key 2>/dev/null || true
if ! nginx -t 2>&1; then
    echo "  ERROR: nginx config invalid without the HTTPS drop-in — aborting." >&2
    exit 1
fi
echo "  HTTPS validated (with cert + HTTP-only fallback); cert minted per-machine at first boot"

# ── systemd service ──────────────────────────────────────────────────────────
cat > /etc/systemd/system/helix-backend.service <<'SVCEOF'
[Unit]
Description=Helix OS Web Backend (FastAPI)
After=network.target helix-firstboot.service
Wants=network.target

[Service]
Type=simple
User=helix
Group=helix
WorkingDirectory=/opt/helix/backend
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/helix/venv/bin/uvicorn main:app --host 127.0.0.1 --port 8082 --workers 1
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl enable nginx
systemctl enable helix-backend

# ── Privileged OS-update helper (swaps the boot image; invoked via sudo) ──────
if [ -f "$BACKEND_SRC/helpers/helix-restore-state" ]; then
    install -m 0755 "$BACKEND_SRC/helpers/helix-restore-state" /usr/local/sbin/helix-restore-state
    echo "  installed /usr/local/sbin/helix-restore-state"
fi
if [ -f "$BACKEND_SRC/helpers/helix-osupdate" ]; then
    install -m 0755 "$BACKEND_SRC/helpers/helix-osupdate" /usr/local/sbin/helix-osupdate
    echo "  installed /usr/local/sbin/helix-osupdate"
fi

# ── Network bring-up helper (applies /boot/config/network.cfg at boot) ────────
# helix-config-network.service (hook 02) runs this; without it NO networkd
# units are written and the box never requests DHCP.
if [ -f /build/network/helix-apply-network ]; then
    install -m 0755 /build/network/helix-apply-network /usr/local/sbin/helix-apply-network
    echo "  installed /usr/local/sbin/helix-apply-network"
fi

# ── Array + share management helpers (called by the backend, ARRAY_SBIN=/usr/local/sbin) ──
# These live in the repo but were never installed, so /api/array/* and share
# apply failed on the real image. Install every runtime helper to /usr/local/sbin.
for helper in \
    /build/array/sbin/helix-array-lib.sh \
    /build/array/sbin/helix-array-start /build/array/sbin/helix-array-stop \
    /build/array/sbin/helix-array-status /build/array/sbin/helix-array-autostart \
    /build/array/sbin/helix-parity-check /build/array/sbin/helix-parity-rebuild \
    /build/array/sbin/helix-disk-format \
    /build/array/sbin/helix-mover \
    /build/array/sbin/helix-vm-start /build/array/sbin/helix-vm-stop \
    /build/shares/helix-shares-apply /build/shares/helix-samba-apply /build/shares/helix-nfs-apply \
    /build/gpu/setup-ai-gpu.sh /build/checkmk/helix-monitor /build/addins/helix-addin; do
    if [ -f "$helper" ]; then
        install -m 0755 "$helper" "/usr/local/sbin/$(basename "$helper")"
        echo "  installed /usr/local/sbin/$(basename "$helper")"
    fi
done
# Boot splash: the animated helix (Plymouth theme). The kernel command line has "quiet splash".
if [ -d /build/boot-assets/plymouth-helix ]; then
    rm -rf /usr/share/plymouth/themes/helix
    install -d /usr/share/plymouth/themes/helix
    cp /build/boot-assets/plymouth-helix/* /usr/share/plymouth/themes/helix/
    install -d /etc/plymouth
    printf '[Daemon]\nTheme=helix\nShowDelay=0\nDeviceTimeout=8\n' > /etc/plymouth/plymouthd.conf
    echo "  installed boot splash theme (helix)"
fi

# Checkmk agent "local" plugin (turns containers/VMs/disks/array into Checkmk services); helix-monitor copies it into the agent at start
install -d /usr/local/lib/helix
install -m 0755 /build/checkmk/helix_local_check.py /usr/local/lib/helix/helix_local_check.py

# ── Persistent data partition (HELIXDATA) setup ───────────────────────────────
# The boot image is tiny vs a modern USB/SD; write-usb.sh / make-pi-image.sh put
# the leftover space in an ext4 partition labelled HELIXDATA. This one-shot grows
# it to fill the device (Pi: SD larger than the image) and mounts it at /mnt/data
# for persistent config + appdata. No-op on a read-only ISO/CD boot.
cat > /usr/local/sbin/helix-firstboot <<'FBEOF'
#!/bin/sh
set -u
LABEL=HELIXDATA
DATA_MNT=/mnt/data
log() { echo "[helix-firstboot] $*"; }

# ── Boot-partition ownership ──────────────────────────────────────────────────
# /boot is FAT (no unix owners): everything is root-owned, so the unprivileged web
# backend could not write /boot/config and silently fell back to RAM, losing every
# setting on reboot. Re-mount it owned by helix (no-op on a read-only ISO/CD boot).
if grep -q ' /boot vfat ' /proc/mounts && touch /boot/.helix-wtest 2>/dev/null; then
    rm -f /boot/.helix-wtest
    HU=$(id -u helix 2>/dev/null); HG=$(id -g helix 2>/dev/null)
    # A FAT `remount,uid=` is silently ignored by the kernel (and exits 0), so check
    # the real owner and, if it did not take, do a clean umount + mount instead.
    if [ -n "$HU" ] && [ -n "$HG" ] && [ "$(stat -c %u /boot 2>/dev/null)" != "$HU" ]; then
        BDEV=$(findmnt -n -o SOURCE /boot 2>/dev/null)
        OPTS="rw,noatime,uid=$HU,gid=$HG,fmask=0022,dmask=0022,codepage=437,iocharset=ascii,shortname=mixed,utf8,errors=remount-ro"
        if [ -n "$BDEV" ] && umount /boot 2>/dev/null; then
            if mount -t vfat -o "$OPTS" "$BDEV" /boot 2>/dev/null; then
                log "/boot re-mounted owned by helix ($HU:$HG)"
            else
                log "WARN: owner mount failed; restoring plain /boot mount"
                mount -t vfat -o rw,noatime "$BDEV" /boot 2>/dev/null || log "ERROR: could not re-mount /boot"
            fi
        else
            log "WARN: /boot busy or device unknown; config stays on RAM until next boot"
        fi
    fi
fi

# ── SSH authorized keys ───────────────────────────────────────────────────────
# Load *.pub from /boot/config/ssh/ into the helix user's authorized_keys. Runs before the HELIXDATA early-exit so
# keys work on a read-only ISO/CD boot too. On a writable boot medium /boot/config
# persists, so keys survive reboots.
HELIX_HOME=$(getent passwd helix 2>/dev/null | cut -d: -f6)
[ -n "$HELIX_HOME" ] || HELIX_HOME=/home/helix
if ls /boot/config/ssh/*.pub >/dev/null 2>&1; then
    mkdir -p "$HELIX_HOME/.ssh"
    AK="$HELIX_HOME/.ssh/authorized_keys"; touch "$AK"
    for pub in /boot/config/ssh/*.pub; do
        while IFS= read -r line; do
            [ -n "$line" ] || continue
            case "$line" in \#*) continue ;; esac
            grep -qxF "$line" "$AK" 2>/dev/null || { echo "$line" >> "$AK"; log "added SSH key from $(basename "$pub")"; }
        done < "$pub"
    done
    chmod 700 "$HELIX_HOME/.ssh"; chmod 600 "$AK" 2>/dev/null || true
    id helix >/dev/null 2>&1 && chown -R helix:helix "$HELIX_HOME/.ssh" 2>/dev/null || true
fi

# ── Restore accounts, Samba passwords and cron jobs (RAM state saved in /boot/config) ─
if [ -x /usr/local/sbin/helix-restore-state ]; then
    HELIX_CONFIG=/boot/config /usr/local/sbin/helix-restore-state 2>&1 | while IFS= read -r l; do log "$l"; done || true
fi

DATA_PART=$(blkid -L "$LABEL" 2>/dev/null) || DATA_PART=""
[ -n "$DATA_PART" ] || { log "no $LABEL partition (ISO/CD or legacy layout) — skipping."; exit 0; }

# Grow the partition to fill the disk, then its filesystem (idempotent/safe).
PK=$(lsblk -no PKNAME "$DATA_PART" 2>/dev/null | head -1)
PNUM=$(printf '%s' "$DATA_PART" | grep -oE '[0-9]+$')
if [ -n "$PK" ] && [ -n "$PNUM" ] && command -v parted >/dev/null 2>&1; then
    parted -s "/dev/$PK" resizepart "$PNUM" 100% >/dev/null 2>&1 || true
    command -v partprobe >/dev/null 2>&1 && partprobe "/dev/$PK" >/dev/null 2>&1 || true
fi
e2fsck -p -f "$DATA_PART" >/dev/null 2>&1 || true
resize2fs "$DATA_PART" >/dev/null 2>&1 || true

mkdir -p "$DATA_MNT"
mountpoint -q "$DATA_MNT" || mount "$DATA_PART" "$DATA_MNT" 2>/dev/null \
    || { log "mount of $DATA_PART failed"; exit 0; }

mkdir -p "$DATA_MNT/config" "$DATA_MNT/appdata"
# Seed persistent config from the boot medium the first time.
if [ -d /boot/config ] && [ -z "$(ls -A "$DATA_MNT/config" 2>/dev/null)" ]; then
    cp -a /boot/config/. "$DATA_MNT/config/" 2>/dev/null || true
fi
id helix >/dev/null 2>&1 && chown -R helix:helix "$DATA_MNT/config" "$DATA_MNT/appdata" 2>/dev/null || true
log "HELIXDATA ready at $DATA_MNT (persistent config + appdata)."
exit 0
FBEOF
chmod 0755 /usr/local/sbin/helix-firstboot

cat > /etc/systemd/system/helix-firstboot.service <<'FBSVC'
[Unit]
Description=Helix OS — mount/grow HELIXDATA persistent data partition
After=local-fs.target
Before=helix-backend.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/helix-firstboot

[Install]
WantedBy=multi-user.target
FBSVC
systemctl enable helix-firstboot 2>/dev/null || echo "  (helix-firstboot enable deferred)"

# Nginx Proxy Manager (CLAUDE.md §54). helix-npm-prepare: /data and /etc/letsencrypt on HELIXDATA
# (helix-npm-storage), then the folders/configs nginx needs and, with persistent storage only,
# NPM's admin server block + environment (helix-npm-setup). It never fails the boot: without
# HELIXDATA nginx and the Helix UI run as before and NPM stays off.
rm -f /etc/systemd/system/helix-npm-storage.service /etc/systemd/system/multi-user.target.wants/helix-npm-storage.service \
      /etc/systemd/system/nginx.service.d/helix-npm-storage.conf
cat > /etc/systemd/system/helix-npm-prepare.service <<'NSSVC'
[Unit]
Description=Helix OS — prepare Nginx Proxy Manager (storage on HELIXDATA, nginx folders)
After=helix-firstboot.service local-fs.target
Before=nginx.service helix-npm.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/helix-npm-storage
ExecStart=/usr/local/sbin/helix-npm-setup

[Install]
WantedBy=multi-user.target
NSSVC
systemctl enable helix-npm-prepare 2>/dev/null || ln -sf /etc/systemd/system/helix-npm-prepare.service /etc/systemd/system/multi-user.target.wants/helix-npm-prepare.service
mkdir -p /etc/systemd/system/nginx.service.d
cat > /etc/systemd/system/nginx.service.d/helix-npm-prepare.conf <<'NSDROP'
[Unit]
Wants=helix-npm-prepare.service
After=helix-npm-prepare.service
NSDROP
# NPM's backend. Root: NPM writes nginx configs and reloads nginx, and its admin can add custom
# nginx config (Lua runs in the root master), so NPM is root-equivalent whatever user it runs as.
# If it stops, nginx keeps serving every host it already has.
cat > /etc/systemd/system/helix-npm.service <<'NPMSVC'
[Unit]
Description=Nginx Proxy Manager (Helix OS)
After=nginx.service helix-npm-prepare.service network-online.target
Wants=network-online.target
ConditionPathExists=/run/npm/enabled

[Service]
Type=simple
EnvironmentFile=/usr/local/lib/helix/npm.env
EnvironmentFile=/run/npm/env
Environment=NODE_ENV=production
Environment=NODE_OPTIONS=--openssl-legacy-provider
Environment=HOME=/root
Environment=PATH=/opt/certbot/bin:/opt/npm/node/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
WorkingDirectory=/opt/npm/backend
ExecStart=/opt/npm/node/bin/node --abort_on_uncaught_exception --max_old_space_size=250 index.js
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
NPMSVC
systemctl enable helix-npm 2>/dev/null || ln -sf /etc/systemd/system/helix-npm.service /etc/systemd/system/multi-user.target.wants/helix-npm.service

# "Start array on boot" (Unraid parity): runs after config + users are restored and the
# network is up; a no-op unless autostart=yes in /boot/config/array.cfg.
cat > /etc/systemd/system/helix-array-autostart.service <<'ASSVC'
[Unit]
Description=Helix OS — start the array on boot (if enabled)
After=helix-firstboot.service local-fs.target systemd-udev-settle.service
Wants=systemd-udev-settle.service

[Service]
Type=oneshot
RemainAfterExit=yes
TimeoutStartSec=300
Environment=HELIX_CONFIG=/boot/config
ExecStart=/usr/local/sbin/helix-array-autostart

[Install]
WantedBy=multi-user.target
ASSVC
systemctl enable helix-array-autostart 2>/dev/null || echo "  (helix-array-autostart enable deferred)"
echo "  installed /usr/local/sbin/helix-firstboot + service"

# Fallback: DHCP on any wired interface, so the machine is reachable out of the
# box even if helix-apply-network hasn't run or network.cfg is empty. The
# config-driven units are written as 10-helix-*.network and take precedence
# (lower prefix wins) over this 99- fallback.
mkdir -p /etc/systemd/network
cat > /etc/systemd/network/99-helix-dhcp-fallback.network <<'NETEOF'
[Match]
Name=en* eth*
Type=ether

[Network]
DHCP=yes
IPv6AcceptRA=yes
NETEOF
echo "  installed DHCP fallback /etc/systemd/network/99-helix-dhcp-fallback.network"

# ── Console banner: show IP + web UI URL before the login prompt ──────────────
if [ -f /build/network/helix-console-banner ]; then
    install -m 0755 /build/network/helix-console-banner /usr/local/sbin/helix-console-banner
    cat > /etc/systemd/system/helix-console-banner.service <<'SVCEOF'
[Unit]
Description=Helix OS console IP banner (/etc/issue)
After=network-online.target systemd-networkd.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/helix-console-banner
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
SVCEOF
    systemctl enable helix-console-banner 2>/dev/null || true
    echo "  installed /usr/local/sbin/helix-console-banner + service"
fi

# ── State directory ───────────────────────────────────────────────────────────
# STATE_DIR is /var/local/helix (config.py): the backend (running as 'helix')
# writes the SMART cache, array_state and audit log here, and uses
# /var/local/helix/config as the writable config fallback on read-only boot.
# /var/local is root-only by default, so create + chown these now or the
# unprivileged backend can't write them (drive cache stays empty, etc.).
mkdir -p /var/local/helix/config
chown -R helix:helix /var/local/helix

echo "==> [hook 03] Helix OS backend installed at $HIVE_DIR"
