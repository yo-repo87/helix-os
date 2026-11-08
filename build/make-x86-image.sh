#!/bin/bash
# Helix OS — Build x86_64 Disk Image (USB / Proxmox)
#
# Usage: sudo ./make-x86-image.sh [--output helix-os-amd64.img.xz]
#
# Creates a raw disk image (.img.xz) with a FAT32 partition labeled HELIXBOOT.
# Unlike the ISO, this image has a real FAT32 /boot that is writable, so
# config changes survive reboots — the same live-boot model as the Pi image.
#
# USE THIS instead of the ISO for:
#   - Writing to a physical USB drive (dd to /dev/sdX)
#   - Importing into Proxmox as a VM disk (qm importdisk ...)
#   - Any scenario where the initramfs looks for a FAT32 HELIXBOOT device
#
# The ISO is for virtual CD-ROM boot and does not have a writable /boot.
#
# Requires: output/ from: sudo ./build.sh --arch amd64
# Tools: parted, dosfstools, grub-efi-amd64-bin, grub-pc-bin, xz-utils

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTPUT_DIR="$SCRIPT_DIR/../output"
WORK_DIR="/tmp/helix-x86-build"
VERSION="$(date +%Y%m%d)"
IMG_NAME="helix-os-amd64-${VERSION}.img"
IMG_PATH="$SCRIPT_DIR/../$IMG_NAME"
IMG_SIZE_MB=2048       # 2 GB image
BOOT_SIZE_MB=512       # FAT32 boot partition (holds squashfs + kernel)

while [[ $# -gt 0 ]]; do
    case $1 in
        --output) IMG_PATH="${2%.xz}"; IMG_NAME="$(basename "$IMG_PATH")"; shift 2 ;;
        --size)   IMG_SIZE_MB="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done
XZ_PATH="${IMG_PATH}.xz"

[[ $EUID -eq 0 ]] || { echo "Must run as root (sudo $0)"; exit 1; }

for cmd in parted mkfs.fat grub-install xz; do
    command -v $cmd >/dev/null || {
        echo "Missing: $cmd"
        echo "Install: apt install parted dosfstools grub-efi-amd64-bin grub-pc-bin xz-utils"
        exit 1
    }
done

[ -f "$OUTPUT_DIR/boot/filesystem.squashfs" ] || {
    echo "output/boot/filesystem.squashfs not found."
    echo "Run: sudo ./build.sh --arch amd64"
    exit 1
}

echo "════════════════════════════════════════════════════════"
echo " Helix OS x86_64 Disk Image Builder"
echo " Source:  $OUTPUT_DIR"
echo " Output:  $XZ_PATH"
echo " Image:   ${IMG_SIZE_MB}MB raw / ${BOOT_SIZE_MB}MB FAT32 boot partition"
echo "════════════════════════════════════════════════════════"

mkdir -p "$WORK_DIR"
MNT="$WORK_DIR/mnt"
mkdir -p "$MNT"

# ── Create raw disk image ─────────────────────────────────────────────────────
echo ""
echo "Creating ${IMG_SIZE_MB}MB raw disk image..."
rm -f "$IMG_PATH"
dd if=/dev/zero of="$IMG_PATH" bs=1M count="$IMG_SIZE_MB" status=progress

# ── Partition: GPT with one FAT32 boot partition + tiny BIOS boot partition ──
echo "Partitioning (GPT)..."
parted -s "$IMG_PATH" mklabel gpt
# 1 MiB gap, then BIOS boot partition (for GRUB legacy BIOS)
parted -s "$IMG_PATH" mkpart "'BIOS boot'" 1MiB 2MiB
parted -s "$IMG_PATH" set 1 bios_grub on
# FAT32 HELIXBOOT partition (UEFI + live-boot)
parted -s "$IMG_PATH" mkpart "'HELIXBOOT'" fat32 2MiB "${BOOT_SIZE_MB}MiB"
parted -s "$IMG_PATH" set 2 esp on

# ── Loop device ───────────────────────────────────────────────────────────────
LOOP_DEV=$(losetup -f --show -P "$IMG_PATH")
BOOT_PART="${LOOP_DEV}p2"

cleanup() {
    umount "$MNT/boot/efi"  2>/dev/null || true
    umount "$MNT"           2>/dev/null || true
    losetup -d "$LOOP_DEV"  2>/dev/null || true
}
trap cleanup EXIT

sleep 1
[ -b "$BOOT_PART" ] || {
    echo "ERROR: Partition device $BOOT_PART not found after losetup"
    exit 1
}

# ── Format FAT32 ──────────────────────────────────────────────────────────────
echo "Formatting FAT32 (label=HELIXBOOT)..."
mkfs.fat -F32 -n "HELIXBOOT" "$BOOT_PART"

# ── Mount and populate ────────────────────────────────────────────────────────
mount "$BOOT_PART" "$MNT"
mkdir -p "$MNT/boot" "$MNT/config"

echo "Copying Helix OS boot files..."
cp "$OUTPUT_DIR/boot/vmlinuz"    "$MNT/boot/"
cp "$OUTPUT_DIR/boot/initrd.img" "$MNT/boot/"
echo -n "  Copying filesystem.squashfs... "
cp "$OUTPUT_DIR/boot/filesystem.squashfs" "$MNT/boot/"
echo "done."

# Default config/go
[ -f "$MNT/config/go" ] || cat > "$MNT/config/go" <<'EOF'
#!/bin/bash
# Helix OS startup script — runs at every boot (writable, persists on disk).
systemctl start helix-backend nginx 2>/dev/null || true
EOF
chmod +x "$MNT/config/go"

# ── Install GRUB (BIOS + UEFI) ───────────────────────────────────────────────
echo "Installing GRUB..."
mkdir -p "$MNT/EFI/BOOT" "$MNT/grub"

BOOT_CMDLINE="boot=helix helix_label=HELIXBOOT quiet"

cat > "$MNT/grub/grub.cfg" <<EOF
set timeout=3
set default=0

menuentry "Helix OS" {
    search --no-floppy --label --set=root HELIXBOOT
    linux  /boot/vmlinuz $BOOT_CMDLINE
    initrd /boot/initrd.img
}

menuentry "Helix OS (safe mode)" {
    search --no-floppy --label --set=root HELIXBOOT
    linux  /boot/vmlinuz $BOOT_CMDLINE helixsafemode
    initrd /boot/initrd.img
}
EOF

# UEFI GRUB binary
grub-mkimage \
    -O x86_64-efi \
    -o "$MNT/EFI/BOOT/BOOTX64.EFI" \
    -p "/grub" \
    part_gpt part_msdos fat ext2 squash4 \
    normal linux initrd search search_label \
    echo all_video gfxterm gfxterm_background \
    configfile ls reboot halt 2>/dev/null || \
grub-mkimage \
    -O x86_64-efi \
    -o "$MNT/EFI/BOOT/BOOTX64.EFI" \
    -p "/grub" \
    part_gpt part_msdos fat \
    normal linux initrd search search_label \
    configfile ls reboot halt

# BIOS GRUB — install to the disk MBR and BIOS boot partition
grub-install \
    --target=i386-pc \
    --boot-directory="$MNT" \
    --no-floppy \
    "$LOOP_DEV" 2>/dev/null || true

sync
SQUASH_SIZE=$(du -sh "$MNT/boot/filesystem.squashfs" | cut -f1)
echo "  Squashfs: $SQUASH_SIZE"
umount "$MNT"
losetup -d "$LOOP_DEV"
trap - EXIT

# ── Compress with xz ──────────────────────────────────────────────────────────
echo ""
echo "Compressing image (xz -T0 --fast)..."
rm -f "$XZ_PATH"
xz -T0 --fast "$IMG_PATH"
# xz replaces the source file with ${IMG_PATH}.xz
[ "${IMG_PATH}.xz" != "$XZ_PATH" ] && mv -f "${IMG_PATH}.xz" "$XZ_PATH"

SIZE=$(du -sh "$XZ_PATH" | cut -f1)

echo ""
echo "════════════════════════════════════════════════════════"
echo " Helix OS x86_64 disk image built successfully!"
echo ""
echo " File: $XZ_PATH"
echo " Size: $SIZE (compressed)"
echo ""
echo " Flash to USB drive:"
echo "   xzcat $XZ_PATH | sudo dd of=/dev/sdX bs=4M status=progress"
echo "   (replace sdX with your drive — check with lsblk first!)"
echo ""
echo " Import into Proxmox as a VM disk:"
echo "   xz -d $XZ_PATH"
echo "   qm importdisk <vmid> ${IMG_PATH%.xz}.img local-lvm --format raw"
echo "   Then: VM → Hardware → unused disk → Edit → Boot Order → enable"
echo ""
echo " NOTE: This image has a writable FAT32 /boot — config changes persist."
echo " Use this instead of the ISO when booting from USB or a VM disk."
echo "════════════════════════════════════════════════════════"
