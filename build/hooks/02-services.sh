#!/bin/bash
# Hook 02: systemd service configuration.
# Runs inside the chroot after package install.
set -euo pipefail

echo "==> [hook 02] Configuring systemd services"

# ── Enable core services ──────────────────────────────────────────────────────
# Guard each enable: a unit may be absent on a minbase image (e.g. a package
# that provides it isn't installed), and that must not abort the build.
for unit in ssh systemd-networkd systemd-resolved; do
    systemctl enable "$unit" 2>/dev/null || echo "  (skip enable $unit: unit not present)"
done

# ── Disable services that conflict with our model ────────────────────────────
# We configure networking from /boot/config/network.cfg, not NetworkManager.
systemctl disable NetworkManager      2>/dev/null || true
systemctl disable wpa_supplicant      2>/dev/null || true
# cloud-init is useless on bare metal
systemctl disable cloud-init          2>/dev/null || true
systemctl disable cloud-config        2>/dev/null || true
systemctl disable cloud-final         2>/dev/null || true
systemctl disable cloud-init-local    2>/dev/null || true
# Docker's data-root must live on the array/cache, never in the RAM root, so it is
# started by helix-array-start (after the drives are mounted), not at boot.
systemctl disable docker.service docker.socket containerd.service 2>/dev/null || true

# ── Write the helix-startup service (runs /boot/config/go like Unraid) ────────
cat > /etc/systemd/system/helix-startup.service <<'EOF'
[Unit]
Description=Helix OS Startup Script (/boot/config/go)
After=network.target local-fs.target
ConditionFileIsExecutable=/boot/config/go

[Service]
Type=oneshot
ExecStart=/boot/config/go
RemainAfterExit=yes
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl enable helix-startup

# ── Write helix-config-network service (applies network.cfg at boot) ───────────
cat > /etc/systemd/system/helix-config-network.service <<'EOF'
[Unit]
Description=Helix OS Network Configuration
Before=network.target
After=local-fs.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/helix-apply-network
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

systemctl enable helix-config-network

# ── Make network-online.target complete promptly ──────────────────────────────
# nginx (and other services) order After=network-online.target. The default
# systemd-networkd-wait-online waits for ALL managed links, so one down/absent
# interface can stall the web UI for ~120s after boot. --any lets a single link
# coming online satisfy it, with a bounded timeout.
mkdir -p /etc/systemd/system/systemd-networkd-wait-online.service.d
cat > /etc/systemd/system/systemd-networkd-wait-online.service.d/helix.conf <<'WOEOF'
[Service]
ExecStart=
ExecStart=/lib/systemd/systemd-networkd-wait-online --any --timeout=30
WOEOF

# ── Keep the root password on the boot device, as Unraid does ─────────────────
# Only root's entry is stored: restoring a whole shadow file could drop service
# accounts added by a later OS update.
cat > /usr/local/sbin/helix-root-password <<'PWEOF'
#!/bin/sh
set -u
STORE=/boot/config/shadow

stored_line() { grep -m1 '^root:' "$STORE" 2>/dev/null; }

case "${1:-}" in
restore)
    line=$(stored_line) || exit 0
    usermod -p "$(printf '%s' "$line" | cut -d: -f2)" root
    ;;
save)
    line=$(grep -m1 '^root:' /etc/shadow) || exit 1
    [ "$(stored_line | cut -d: -f2)" = "$(printf '%s' "$line" | cut -d: -f2)" ] \
        && [ -e "$STORE" ] && exit 0
    if [ ! -w /boot/config ]; then
        echo "boot device is read-only; root password will not persist"
        exit 0
    fi
    umask 077
    printf '%s\n' "$line" > "$STORE.tmp" && mv -f "$STORE.tmp" "$STORE" && sync
    ;;
*)
    echo "usage: helix-root-password restore|save" >&2
    exit 2
    ;;
esac
PWEOF
chmod 0755 /usr/local/sbin/helix-root-password

cat > /etc/systemd/system/helix-root-password-restore.service <<'EOF'
[Unit]
Description=Restore root password from /boot/config/shadow
After=local-fs.target
Before=systemd-user-sessions.service ssh.service helix-root-password-save.path

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/helix-root-password restore

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/helix-root-password-save.service <<'EOF'
[Unit]
Description=Save root password to /boot/config/shadow

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/helix-root-password save
EOF

cat > /etc/systemd/system/helix-root-password-save.path <<'EOF'
[Unit]
Description=Watch /etc/shadow for root password changes

[Path]
PathChanged=/etc/shadow

[Install]
WantedBy=multi-user.target
EOF

systemctl enable helix-root-password-restore.service helix-root-password-save.path

echo "==> [hook 02] Done."
