#!/bin/bash
# Helix OS — Write to USB drive
#
# Usage: sudo ./write-usb.sh /dev/sdX
#
# Creates:
#   Partition 1: FAT32 (512MB) labeled HELIXBOOT — boot files + config
#
# WARNING: This WIPES the target device. Double-check your device path.

set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Must run as root (sudo $0 /dev/sdX)"; exit 1; }

DEVICE="${1:-}"
[ -n "$DEVICE" ] || { echo "Usage: $0 /dev/sdX"; exit 1; }
[ -b "$DEVICE" ] || { echo "Not a block device: $DEVICE"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTPUT_DIR="$SCRIPT_DIR/../output"

[ -f "$OUTPUT_DIR/boot/filesystem.squashfs" ] || {
    echo "Output not found. Run build.sh first."
    exit 1
}

LABEL="HELIXBOOT"
PART="${DEVICE}1"; PART2="${DEVICE}2"
# Handle nvme/mmc/loop naming (nvme0n1 → nvme0n1p1, mmcblk0 → mmcblk0p1)
if [[ "$DEVICE" =~ nvme|mmcblk|loop ]]; then PART="${DEVICE}p1"; PART2="${DEVICE}p2"; fi

echo "════════════════════════════════════════════════════════"
echo " Helix OS USB Writer"
echo " Device:  $DEVICE"
echo " Partition: $PART"
echo " Label:   $LABEL"
echo "════════════════════════════════════════════════════════"
echo ""

# Show what's currently on the device so the user can confirm
lsblk "$DEVICE"
echo ""
read -rp "This will WIPE $DEVICE. Type 'yes' to continue: " confirm
[ "$confirm" = "yes" ] || { echo "Aborted."; exit 0; }

# ── Unmount any existing mounts ───────────────────────────────────────────────
umount "${DEVICE}"* 2>/dev/null || true

# ── Partition: one FAT32 partition spanning the drive ────────────────────────
echo "Partitioning $DEVICE (2 GiB boot + HELIXDATA for the rest)..."
parted -s "$DEVICE" mklabel msdos
parted -s "$DEVICE" mkpart primary fat32 1MiB 2049MiB
parted -s "$DEVICE" set 1 boot on
# Use the leftover space as an ext4 data partition (persistent config + appdata).
parted -s "$DEVICE" mkpart primary ext4 2049MiB 100%

# ── Format ───────────────────────────────────────────────────────────────────
echo "Formatting $PART as FAT32 (label=$LABEL)..."
sleep 1  # wait for kernel to register partitions
partprobe "$DEVICE" 2>/dev/null || true
mkfs.fat -F32 -n "$LABEL" "$PART"
echo "Formatting $PART2 as ext4 (label=HELIXDATA)..."
mkfs.ext4 -F -q -L HELIXDATA "$PART2"

# ── Mount + copy files ───────────────────────────────────────────────────────
MNT="/tmp/helix-usb-write"
mkdir -p "$MNT"
mount "$PART" "$MNT"

echo "Copying boot files..."
mkdir -p "$MNT/boot" "$MNT/grub" "$MNT/EFI/BOOT" "$MNT/config"

cp "$OUTPUT_DIR/boot/vmlinuz"             "$MNT/boot/" 2>/dev/null || echo "  SKIP vmlinuz (not found)"
cp "$OUTPUT_DIR/boot/initrd.img"          "$MNT/boot/"
echo -n "  Copying filesystem.squashfs (this may take a while)... "
cp "$OUTPUT_DIR/boot/filesystem.squashfs" "$MNT/boot/"
echo "done."

cp "$OUTPUT_DIR/grub/grub.cfg"            "$MNT/grub/"
cp "$OUTPUT_DIR/EFI/BOOT/BOOTX64.EFI"    "$MNT/EFI/BOOT/" 2>/dev/null || true
cp -r "$OUTPUT_DIR/config/"*             "$MNT/config/"

# Pi files (if present)
[ -f "$OUTPUT_DIR/config.txt"  ] && cp "$OUTPUT_DIR/config.txt"  "$MNT/"
[ -f "$OUTPUT_DIR/cmdline.txt" ] && cp "$OUTPUT_DIR/cmdline.txt" "$MNT/"

# ── Install GRUB to MBR (BIOS boot) ──────────────────────────────────────────
if command -v grub-install >/dev/null; then
    echo "Installing GRUB to MBR..."
    grub-install --target=i386-pc \
        --boot-directory="$MNT" \
        --no-floppy \
        "$DEVICE" 2>/dev/null || echo "  SKIP GRUB MBR install (grub-pc not available)"
fi

sync
umount "$MNT"

echo ""
echo "════════════════════════════════════════════════════════"
echo " Done! Helix OS written to $DEVICE"
echo ""
echo " USB structure:"
echo "   /boot/vmlinuz              — kernel"
echo "   /boot/initrd.img           — initramfs"
echo "   /boot/filesystem.squashfs  — OS (~in RAM at boot)"
echo "   /grub/grub.cfg             — boot menu"
echo "   /config/                   — persistent configuration"
echo ""
echo " Edit /config/ident.cfg and /config/network.cfg"
echo " before first boot."
echo "════════════════════════════════════════════════════════"
