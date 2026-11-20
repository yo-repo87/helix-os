#!/bin/bash
# Helix OS — flashable x86_64 disk image for a USB flash drive (or a VM disk): helix-os-<version>.img.xz
#
# Usage: sudo build/make-x86-image.sh [--output helix-os-<version>.img.xz] [--size-mb 4608]
#
# This is what goes on a flash drive for a real install (balenaEtcher / Raspberry Pi Imager / dd). It is NOT the
# ISO: an ISO written to a stick boots from a read-only CD file system, so settings are not kept and updates
# cannot install. The ISO is for virtual machines (attach as a CD) and trying Helix out.
#
# Layout = exactly what write-usb.sh writes to a real stick (it runs that script on a loop device):
#   FAT32 HELIXBOOT 4 GiB (system image + room for an update's .new/.prev copies + /config)
#   ext4  HELIXDATA (small in the image; helix-firstboot grows it to fill the drive at first boot)
#   GRUB for BIOS (MBR) and UEFI (EFI/BOOT/BOOTX64.EFI).
# Requires: output/ from build.sh --arch amd64; parted, dosfstools, e2fsprogs, grub-pc-bin, xz-utils.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR/.."
VERSION="$(cat "$SCRIPT_DIR/config/VERSION")"
OUTPUT_DIR="${HELIX_OUTPUT:-$PROJECT_DIR/output}"
OUT="$PROJECT_DIR/helix-os-$VERSION.img.xz"
SIZE_MB=4608           # 4 GiB boot + 512 MiB data (grown at first boot)

while [[ $# -gt 0 ]]; do
    case $1 in
        --output)  OUT="$2"; shift 2 ;;
        --size-mb) SIZE_MB="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

[[ $EUID -eq 0 ]] || { echo "Must run as root"; exit 1; }
for f in boot/vmlinuz boot/initrd.img boot/filesystem.squashfs grub/grub.cfg EFI/BOOT/BOOTX64.EFI; do
    [ -f "$OUTPUT_DIR/$f" ] || { echo "ERROR: $OUTPUT_DIR/$f missing — run build.sh --arch amd64 first"; exit 1; }
done
for cmd in parted mkfs.fat mkfs.ext4 grub-install xz losetup; do
    command -v "$cmd" >/dev/null || { echo "ERROR: $cmd not found"; exit 1; }
done
[ -d /usr/lib/grub/i386-pc ] || { echo "ERROR: grub-pc-bin is not installed (BIOS boot)"; exit 1; }

IMG="$(mktemp -p "${TMPDIR:-/var/tmp}" helix-x86-XXXXXX.img)"
LOOP=""
cleanup() { [ -n "$LOOP" ] && losetup -d "$LOOP" 2>/dev/null; rm -f "$IMG"; }
trap cleanup EXIT

echo "Creating a ${SIZE_MB} MiB image..."
truncate -s "${SIZE_MB}M" "$IMG"
LOOP="$(losetup -fP --show "$IMG")"
HELIX_ASSUME_YES=1 HELIX_REQUIRE_GRUB=1 HELIX_OUTPUT="$OUTPUT_DIR" bash "$SCRIPT_DIR/write-usb.sh" "$LOOP"
sync
losetup -d "$LOOP"; LOOP=""

echo "Compressing to $OUT ..."
xz -T0 -6 -c "$IMG" > "$OUT.tmp"
mv "$OUT.tmp" "$OUT"
sha256sum "$OUT" | tee "$OUT.sha256"
echo "Done: $OUT"
echo "Flash it to a USB drive with balenaEtcher or: xzcat $(basename "$OUT") | sudo dd of=/dev/sdX bs=4M conv=fsync status=progress"
