#!/bin/bash
# Helix OS — Write to USB drive
#
# Usage: sudo ./write-usb.sh /dev/sdX
#
# Creates:
#   Partition 1: FAT32 (4 GiB) labeled HELIXBOOT — boot files + config. 4 GiB holds the running system image plus
#                the .new and .prev copies an OS update needs (~1 GB each and growing).
#   Partition 2: ext4 labeled HELIXDATA — the rest of the drive (helix-firstboot grows it to fill a bigger drive).
#
# Also used by make-x86-image.sh on a loop device to build the flashable helix-os-<version>.img.xz:
#   HELIX_ASSUME_YES=1   no confirmation prompt      HELIX_REQUIRE_GRUB=1  a failed BIOS GRUB install is fatal
#   HELIX_BOOT_MIB=4096  boot partition size (MiB)   HELIX_OUTPUT=<dir>    build output to copy from
#
# WARNING: This WIPES the target device. Double-check your device path.

set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Must run as root (sudo $0 /dev/sdX)"; exit 1; }

DEVICE="${1:-}"
[ -n "$DEVICE" ] || { echo "Usage: $0 /dev/sdX"; exit 1; }
[ -b "$DEVICE" ] || { echo "Not a block device: $DEVICE"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# OUTPUT_DIR defaults to ../output but can be overridden (e.g. to build an amd64
# disk image from boot files extracted elsewhere): HELIX_OUTPUT=/path write-usb.sh …
OUTPUT_DIR="${HELIX_OUTPUT:-$SCRIPT_DIR/../output}"

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
if [ "${HELIX_ASSUME_YES:-}" != 1 ]; then
    read -rp "This will WIPE $DEVICE. Type 'yes' to continue: " confirm
    [ "$confirm" = "yes" ] || { echo "Aborted."; exit 0; }
fi
BOOT_MIB="${HELIX_BOOT_MIB:-4096}"
BOOT_END=$((BOOT_MIB + 1))

# ── Unmount any existing mounts ───────────────────────────────────────────────
umount "${DEVICE}"* 2>/dev/null || true

# ── Partition: one FAT32 partition spanning the drive ────────────────────────
echo "Partitioning $DEVICE (${BOOT_MIB} MiB boot + HELIXDATA for the rest)..."
parted -s "$DEVICE" mklabel msdos
parted -s "$DEVICE" mkpart primary fat32 1MiB "${BOOT_END}MiB"
parted -s "$DEVICE" set 1 boot on
# Use the leftover space as an ext4 data partition (persistent config + appdata).
parted -s "$DEVICE" mkpart primary ext4 "${BOOT_END}MiB" 100%

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
    if ! grub-install --target=i386-pc --boot-directory="$MNT" --no-floppy "$DEVICE"; then
        [ "${HELIX_REQUIRE_GRUB:-}" = 1 ] && { echo "ERROR: GRUB install for BIOS boot failed"; umount "$MNT"; exit 1; }
        echo "  SKIP GRUB MBR install (grub-pc not available)"
    fi
elif [ "${HELIX_REQUIRE_GRUB:-}" = 1 ]; then
    echo "ERROR: grub-install not found (apt install grub-pc-bin)"; umount "$MNT"; exit 1
fi
# Menu background and font (same as the ISO's GRUB menu).
[ -f "$SCRIPT_DIR/boot-assets/splash.png" ] && cp "$SCRIPT_DIR/boot-assets/splash.png" "$MNT/grub/splash.png"
[ -f /usr/share/grub/unicode.pf2 ] && { mkdir -p "$MNT/grub/fonts"; cp /usr/share/grub/unicode.pf2 "$MNT/grub/fonts/"; }

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
