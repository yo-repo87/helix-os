#!/bin/bash
# Helix OS — Live Boot Image Builder
#
# Usage: sudo ./build.sh [--arch amd64|arm64] [--suite bookworm] [--full]
#
# Outputs:
#   output/boot/vmlinuz          — kernel
#   output/boot/initrd.img       — custom initramfs
#   output/boot/filesystem.squashfs — root OS image
#   output/EFI/                  — UEFI bootloader (x86 only)
#   output/grub/grub.cfg         — GRUB config (x86 only)
#   output/config/               — USB config template
#
# To write to a USB drive:
#   sudo ./write-usb.sh /dev/sdX

set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────
ARCH="${ARCH:-amd64}"
SUITE="${SUITE:-bookworm}"
MIRROR="${MIRROR:-http://deb.debian.org/debian}"
FULL="${FULL:-no}"          # yes = include packages-full.list
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="$SCRIPT_DIR"
OUTPUT_DIR="$SCRIPT_DIR/../output"
ROOTFS_DIR="/tmp/helix-build/rootfs"
LOG="$OUTPUT_DIR/build.log"

# ── Parse args ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case $1 in
        --arch)   ARCH="$2";  shift 2 ;;
        --suite)  SUITE="$2"; shift 2 ;;
        --full)   FULL="yes";  shift  ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

[[ $EUID -eq 0 ]] || { echo "Must run as root (sudo $0)"; exit 1; }
command -v debootstrap >/dev/null || { echo "Install debootstrap first: apt install debootstrap"; exit 1; }
command -v mksquashfs  >/dev/null || { echo "Install squashfs-tools: apt install squashfs-tools"; exit 1; }

mkdir -p "$OUTPUT_DIR/boot" "$OUTPUT_DIR/grub" "$OUTPUT_DIR/EFI/BOOT" "$OUTPUT_DIR/config"
exec > >(tee "$LOG") 2>&1

echo "════════════════════════════════════════════════════════"
echo " Helix OS Build"
echo " Arch:   $ARCH"
echo " Suite:  $SUITE"
echo " Mirror: $MIRROR"
echo " Full:   $FULL"
echo " Output: $OUTPUT_DIR"
echo "════════════════════════════════════════════════════════"

# ── Step 1: debootstrap ───────────────────────────────────────────────────────
echo ""
echo "── Step 1/6: Bootstrap Debian $SUITE ($ARCH) ──"

if [ -d "$ROOTFS_DIR/etc" ]; then
    echo "  Existing rootfs found at $ROOTFS_DIR — skipping debootstrap."
    echo "  Delete $ROOTFS_DIR to force a fresh bootstrap."
else
    rm -rf "$ROOTFS_DIR"
    mkdir -p "$ROOTFS_DIR"
    debootstrap --arch="$ARCH" --variant=minbase "$SUITE" "$ROOTFS_DIR" "$MIRROR"
fi

# ── Step 2: Install packages ──────────────────────────────────────────────────
echo ""
echo "── Step 2/6: Installing packages ──"

# Architecture-specific kernel package
KERNEL_PKG="linux-image-${ARCH}"

# Build the combined package list (replace KERNEL_PKG_PLACEHOLDER with actual pkg)
# NOTE: space-separated — apt-get does not accept comma-separated package names
PKG_LIST=$(grep -v '^\s*#' "$BUILD_DIR/config/packages-base.list" \
         | grep -v '^\s*$' \
         | sed "s/KERNEL_PKG_PLACEHOLDER/$KERNEL_PKG/" \
         | tr '\n' ' ' | sed 's/ $//')

if [ "$FULL" = "yes" ]; then
    FULL_PKGS=$(grep -v '^\s*#' "$BUILD_DIR/config/packages-full.list" \
              | grep -v '^\s*$' | tr '\n' ' ' | sed 's/ $//')
    PKG_LIST="$PKG_LIST $FULL_PKGS"
fi

mount --bind /dev  "$ROOTFS_DIR/dev"
mount --bind /proc "$ROOTFS_DIR/proc"
mount --bind /sys  "$ROOTFS_DIR/sys"
mount -t devpts devpts "$ROOTFS_DIR/dev/pts" 2>/dev/null || true

cleanup_mounts() {
    umount "$ROOTFS_DIR/dev/pts" 2>/dev/null || true
    umount "$ROOTFS_DIR/dev"     2>/dev/null || true
    umount "$ROOTFS_DIR/proc"    2>/dev/null || true
    umount "$ROOTFS_DIR/sys"     2>/dev/null || true
}
trap cleanup_mounts EXIT

chroot "$ROOTFS_DIR" /bin/bash -c "
    set -e
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y --no-install-recommends $PKG_LIST
    apt-get clean
    rm -rf /var/lib/apt/lists/*
"

# ── Step 3: Run customization hooks ──────────────────────────────────────────
echo ""
echo "── Step 3/6: Running customization hooks ──"

mkdir -p "$ROOTFS_DIR/build/config"
cp -r "$BUILD_DIR/config/"* "$ROOTFS_DIR/build/config/"

# Copy webui/ for hook 03-helix-install.sh (if present)
WEBUI_SRC="$SCRIPT_DIR/../webui"
if [ -d "$WEBUI_SRC" ]; then
    echo "  Copying webui/ into chroot..."
    cp -r "$WEBUI_SRC" "$ROOTFS_DIR/build/webui"
fi

for hook in "$BUILD_DIR/hooks/"*.sh; do
    echo "  Running: $(basename "$hook")"
    chmod +x "$hook"
    cp "$hook" "$ROOTFS_DIR/tmp/$(basename "$hook")"
    chroot "$ROOTFS_DIR" /bin/bash "/tmp/$(basename "$hook")"
done

cleanup_mounts
trap - EXIT

# ── Step 4: Pack squashfs ─────────────────────────────────────────────────────
echo ""
echo "── Step 4/6: Building squashfs ──"

mksquashfs "$ROOTFS_DIR" "$OUTPUT_DIR/boot/filesystem.squashfs" \
    -comp zstd -Xcompression-level 9 \
    -noappend \
    -wildcards \
    -ef "$BUILD_DIR/config/squashfs-excludes.txt" 2>/dev/null \
    || mksquashfs "$ROOTFS_DIR" "$OUTPUT_DIR/boot/filesystem.squashfs" \
         -comp zstd -Xcompression-level 9 -noappend

SIZE=$(du -sh "$OUTPUT_DIR/boot/filesystem.squashfs" | cut -f1)
echo "  filesystem.squashfs: $SIZE"

# ── Step 5: Copy kernel + build initramfs ────────────────────────────────────
echo ""
echo "── Step 5/6: Building boot files ──"

# Copy kernel from rootfs
VMLINUZ=$(ls "$ROOTFS_DIR/boot/vmlinuz-"* 2>/dev/null | sort -V | tail -1)
if [ -n "$VMLINUZ" ]; then
    cp "$VMLINUZ" "$OUTPUT_DIR/boot/vmlinuz"
    echo "  Kernel: $(basename "$VMLINUZ")"
else
    echo "  WARN: No kernel found in rootfs. Install linux-image-* in packages-base.list."
fi

# Build our custom initramfs.
# Pull busybox + kernel modules from the *target rootfs* so the initramfs is
# architecture-correct (critical for arm64) and matches the image's kernel.
KERNEL_VERSION=$(ls "$ROOTFS_DIR/lib/modules/" 2>/dev/null | sort -V | tail -1 || true)
export KERNEL_VERSION
export ROOTFS="$ROOTFS_DIR"
bash "$BUILD_DIR/initramfs/build-initramfs.sh"

# ── Step 6: Install GRUB (x86 only) ──────────────────────────────────────────
echo ""
echo "── Step 6/6: Preparing boot files ──"

cp "$BUILD_DIR/grub/grub.cfg" "$OUTPUT_DIR/grub/grub.cfg"

if [ "$ARCH" = "amd64" ]; then
    # Install GRUB EFI + BIOS images if available
    if command -v grub-mkimage >/dev/null; then
        grub-mkimage -O x86_64-efi -o "$OUTPUT_DIR/EFI/BOOT/BOOTX64.EFI" \
            -p /grub \
            part_gpt part_msdos fat search search_label normal \
            linux echo configfile gzio 2>/dev/null || \
            echo "  WARN: grub-mkimage failed — install grub-efi-amd64-bin"
    fi
elif [ "$ARCH" = "arm64" ]; then
    cp "$BUILD_DIR/pi/config.txt"  "$OUTPUT_DIR/config.txt"
    cp "$BUILD_DIR/pi/cmdline.txt" "$OUTPUT_DIR/cmdline.txt"
    echo "  Pi boot files copied."
fi

# Copy USB config template
cp -r "$BUILD_DIR/usb-layout/config/"* "$OUTPUT_DIR/config/"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════"
echo " Build complete!"
echo ""
echo " Output directory: $OUTPUT_DIR"
echo " ├── boot/"
echo " │   ├── vmlinuz                   (kernel)"
echo " │   ├── initrd.img                (initramfs)"
echo " │   └── filesystem.squashfs       (root OS)"
echo " ├── grub/grub.cfg                 (boot menu)"
echo " ├── EFI/BOOT/BOOTX64.EFI          (UEFI loader, x86 only)"
echo " └── config/                        (USB config template)"
echo ""
echo " Next: sudo ./write-usb.sh /dev/sdX"
echo "════════════════════════════════════════════════════════"
