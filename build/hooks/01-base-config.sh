#!/bin/bash
# Hook 01: Base system configuration.
# Runs inside a chroot of the new rootfs after debootstrap + package install.
set -euo pipefail

echo "==> [hook 01] Base system configuration"

# ── Locale & timezone ─────────────────────────────────────────────────────────
echo "en_US.UTF-8 UTF-8" > /etc/locale.gen
locale-gen
update-locale LANG=en_US.UTF-8

ln -sf /usr/share/zoneinfo/UTC /etc/localtime
echo "UTC" > /etc/timezone

# ── Hostname (overridden by /boot/config/ident.cfg at runtime) ───────────────
echo "Tower" > /etc/hostname
cat > /etc/hosts <<'EOF'
127.0.0.1   localhost
127.0.1.1   Tower
::1         localhost ip6-localhost ip6-loopback
EOF

# ── Root password (disabled — use SSH keys or the web UI) ────────────────────
passwd -l root

# ── Default user: helix (web UI runs as this, sudo without password) ───────────
# Create the user first, then add only to groups that actually exist in this
# image (the 'docker' group is absent in base builds and would fail useradd -G).
useradd -m -s /bin/bash helix 2>/dev/null || true
for grp in sudo docker; do
    getent group "$grp" >/dev/null 2>&1 && usermod -aG "$grp" helix 2>/dev/null || true
done
passwd -l helix 2>/dev/null || true
echo "helix ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/helix
chmod 440 /etc/sudoers.d/helix

# ── SSH: disable password auth, enable key auth ───────────────────────────────
sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/^#*PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config
sed -i 's/^#*PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
# (SSH keys are loaded from /boot/config/ssh/ at runtime by rc.helix-network)

# ── OS identity ───────────────────────────────────────────────────────────────
cp /build/config/os-release /etc/os-release

# Stamp the running OS version (read by the GitHub updater to compare releases).
if [ -f /build/config/VERSION ]; then
    tr -d '[:space:]' < /build/config/VERSION > /etc/helix-version
else
    sed -n 's/^VERSION_ID="\?\([^"]*\)"\?/\1/p' /etc/os-release > /etc/helix-version
fi

# ── /boot/config mount point ──────────────────────────────────────────────────
mkdir -p /boot
# The initramfs bind-mounts the USB here; add a placeholder fstab comment.
cat > /etc/fstab <<'EOF'
# /boot is bind-mounted from the USB by the Helix OS initramfs.
# Do not add an entry for it here — it is already mounted before init runs.
tmpfs   /tmp    tmpfs   defaults,nosuid,nodev   0 0
tmpfs   /run    tmpfs   defaults,nosuid,nodev   0 0
EOF

# ── /etc/unraid-emu (config model, mirrors /boot/config) ─────────────────────
# This is a symlink — actual config lives on USB at /boot/config/
ln -sf /boot/config /etc/helix-config

# ── motd ──────────────────────────────────────────────────────────────────────
cat > /etc/motd <<'EOF'

  ██╗  ██╗██╗██╗   ██╗███████╗     ██████╗ ███████╗
  ██║  ██║██║██║   ██║██╔════╝    ██╔═══██╗██╔════╝
  ███████║██║██║   ██║█████╗      ██║   ██║███████╗
  ██╔══██║██║╚██╗ ██╔╝██╔══╝      ██║   ██║╚════██║
  ██║  ██║██║ ╚████╔╝ ███████╗    ╚██████╔╝███████║
  ╚═╝  ╚═╝╚═╝  ╚═══╝  ╚══════╝     ╚═════╝ ╚══════╝

  Helix OS — Open-source NAS / server platform
  Config: /boot/config/   Web UI: http://this-host/

EOF

echo "==> [hook 01] Done."
