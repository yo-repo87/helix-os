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

echo "==> [hook 02] Done."
