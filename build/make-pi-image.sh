#!/bin/bash
# Helix OS — Build Raspberry Pi SD Card Image
#
# Usage: sudo ./make-pi-image.sh [--output helix-os-pi.img.xz]
#
# Creates a raw disk image (.img.xz) that can be flashed to an SD card
# or USB drive with balenaEtcher, Raspberry Pi Imager, or dd.
#
# Supports: Raspberry Pi 4B, Pi 5 (aarch64 / ARM64)
# Boot method: live-boot (squashfs loaded into RAM at startup)
#
# Requires:
#   - output/ from: sudo ./build.sh --arch arm64
#   - parted, dosfstools, mtools (apt install parted dosfstools mtools)
#   - xz-utils (apt install xz-utils)
#   - Internet access for first run (downloads Pi firmware ~8MB)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTPUT_DIR="$SCRIPT_DIR/../output"
WORK_DIR="/tmp/helix-pi-build"
VERSION="$(date +%Y%m%d)"
IMG_NAME="helix-os-pi-${VERSION}.img"
IMG_PATH="$SCRIPT_DIR/../$IMG_NAME"
IMG_SIZE_MB=2048       # 2GB image
BOOT_SIZE_MB=512       # FAT32 boot partition

# ── Parse args ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case $1 in
        --output) IMG_PATH="$2"; IMG_NAME="$(basename "$2")"; shift 2 ;;
        --size)   IMG_SIZE_MB="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done
XZ_PATH="${IMG_PATH%.img}.img.xz"
XZ_PATH="${IMG_PATH%.xz}.xz"
# Normalize: strip .xz if passed, we always add it
IMG_PATH="${IMG_PATH%.xz}"
XZ_PATH="${IMG_PATH}.xz"

[[ $EUID -eq 0 ]] || { echo "Must run as root (sudo $0)"; exit 1; }

for cmd in parted mkfs.fat mtools xz; do
    command -v $cmd >/dev/null || {
        echo "Missing: $cmd"
        echo "Install: apt install parted dosfstools mtools xz-utils"
        exit 1
    }
done

[ -f "$OUTPUT_DIR/boot/filesystem.squashfs" ] || {
    echo "output/boot/filesystem.squashfs not found."
    echo "Run: sudo ./build.sh --arch arm64"
    exit 1
}

# Check this is an arm64 build
SQUASHFS_ARCH=$(file "$OUTPUT_DIR/boot/vmlinuz" 2>/dev/null | grep -o 'ARM\|AArch64\|aarch64' | head -1 || true)
if [ -z "$SQUASHFS_ARCH" ] && [ -f "$OUTPUT_DIR/boot/vmlinuz" ]; then
    echo "  WARN: Could not confirm arm64 kernel — proceeding anyway."
fi

echo "════════════════════════════════════════════════════════"
echo " Helix OS Raspberry Pi Image Builder"
echo " Source:  $OUTPUT_DIR"
echo " Output:  $XZ_PATH"
echo " Image:   ${IMG_SIZE_MB}MB raw / ${BOOT_SIZE_MB}MB boot partition"
echo "════════════════════════════════════════════════════════"

mkdir -p "$WORK_DIR"
FIRMWARE_DIR="$WORK_DIR/firmware"
MNT="$WORK_DIR/mnt"
mkdir -p "$FIRMWARE_DIR" "$MNT"

# ── Download Pi firmware (first run only; cached in WORK_DIR) ─────────────────
# We download only the minimal files needed for Pi 4/5 boot from a fixed release tag.
FIRMWARE_TAG="1.20240529"
FIRMWARE_BASE="https://github.com/raspberrypi/firmware/raw/${FIRMWARE_TAG}/boot"
FIRMWARE_CACHE="$WORK_DIR/firmware_${FIRMWARE_TAG}.tar"

download_firmware() {
    echo "Downloading Raspberry Pi firmware (tag ${FIRMWARE_TAG})..."
    # Core files needed for Pi 4/5 (ARM64)
    local files=(
        "start4.elf" "start4cd.elf"
        "fixup4.dat" "fixup4cd.dat"
        "bcm2711-rpi-4-b.dtb"
        "bcm2712-rpi-5-b.dtb"
        "bcm2711-rpi-cm4.dtb"
        "overlays/disable-bt.dtbo"
        "overlays/miniuart-bt.dtbo"
    )

    mkdir -p "$FIRMWARE_DIR/overlays"
    local failed=0
    for f in "${files[@]}"; do
        local dir
        dir="$(dirname "$f")"
        mkdir -p "$FIRMWARE_DIR/$dir"
        curl -fsSL "${FIRMWARE_BASE}/${f}" -o "$FIRMWARE_DIR/${f}" 2>/dev/null || {
            echo "  WARN: Could not download ${f} (non-fatal)"
            failed=1
        }
    done
    return $failed
}

if [ ! -f "$FIRMWARE_DIR/start4.elf" ]; then
    # Try online download first
    download_firmware || {
        echo ""
        echo "Firmware download failed or partial — checking for local raspi-firmware package..."
        # Try to get from installed package or cache
        for pkg_path in \
            /usr/lib/raspi-firmware \
            /usr/share/raspi-firmware \
            /lib/firmware/raspberrypi/bootloader; do
            if [ -f "$pkg_path/start4.elf" ]; then
                echo "  Using firmware from $pkg_path"
                cp -r "$pkg_path/." "$FIRMWARE_DIR/"
                break
            fi
        done
    }
fi

if [ ! -f "$FIRMWARE_DIR/start4.elf" ]; then
    echo ""
    echo "ERROR: Could not obtain Raspberry Pi firmware files."
    echo ""
    echo "Options:"
    echo "  1. Install the firmware package: apt install raspi-firmware"
    echo "  2. Ensure internet access for automatic download"
    echo "  3. Manually copy start4.elf, fixup4.dat, bcm2711-rpi-4-b.dtb"
    echo "     to: $FIRMWARE_DIR/"
    exit 1
fi

# ── Create raw disk image ─────────────────────────────────────────────────────
echo ""
echo "Creating ${IMG_SIZE_MB}MB raw disk image..."
rm -f "$IMG_PATH"
dd if=/dev/zero of="$IMG_PATH" bs=1M count="$IMG_SIZE_MB" status=progress

# ── Partition: one FAT32 boot partition ──────────────────────────────────────
echo "Partitioning..."
parted -s "$IMG_PATH" mklabel msdos
parted -s "$IMG_PATH" mkpart primary fat32 1MiB "${BOOT_SIZE_MB}MiB"
parted -s "$IMG_PATH" set 1 boot on

# ── Loop device ───────────────────────────────────────────────────────────────
LOOP_DEV=$(losetup -f --show -P "$IMG_PATH")
LOOP_PART="${LOOP_DEV}p1"

cleanup() {
    umount "$MNT" 2>/dev/null || true
    losetup -d "$LOOP_DEV" 2>/dev/null || true
}
trap cleanup EXIT

# Wait for partition device node
sleep 1
[ -b "$LOOP_PART" ] || {
    # Fallback: manually calculate offset
    PART_OFFSET=$((1 * 1024 * 1024))  # 1MiB start
    LOOP_PART=$(losetup -f --show -o "$PART_OFFSET" "$IMG_PATH")
}

# ── Format FAT32 ──────────────────────────────────────────────────────────────
echo "Formatting FAT32 (label=HELIXBOOT)..."
mkfs.fat -F32 -n "HELIXBOOT" "$LOOP_PART"

# ── Mount and populate ────────────────────────────────────────────────────────
mount "$LOOP_PART" "$MNT"

echo "Copying Pi firmware..."
cp "$FIRMWARE_DIR"/start4*.elf  "$MNT/" 2>/dev/null || true
cp "$FIRMWARE_DIR"/fixup4*.dat  "$MNT/" 2>/dev/null || true
for dtb in "$FIRMWARE_DIR"/*.dtb; do [ -f "$dtb" ] && cp "$dtb" "$MNT/"; done
if [ -d "$FIRMWARE_DIR/overlays" ]; then
    mkdir -p "$MNT/overlays"
    cp "$FIRMWARE_DIR/overlays/"*.dtbo "$MNT/overlays/" 2>/dev/null || true
fi

echo "Copying Helix OS boot files..."
mkdir -p "$MNT/boot" "$MNT/config"
cp "$OUTPUT_DIR/boot/vmlinuz"             "$MNT/boot/"
cp "$OUTPUT_DIR/boot/initrd.img"          "$MNT/boot/"
echo -n "  Copying filesystem.squashfs (this may take a while)... "
cp "$OUTPUT_DIR/boot/filesystem.squashfs" "$MNT/boot/"
echo "done."

# ── Pi boot config ────────────────────────────────────────────────────────────
cp "$SCRIPT_DIR/pi/config.txt" "$MNT/config.txt"

cat > "$MNT/cmdline.txt" <<'EOF'
console=serial0,115200 console=tty1 boot=helix helix_label=HELIXBOOT quiet splash
EOF

# ── Config template ───────────────────────────────────────────────────────────
[ -d "$OUTPUT_DIR/config" ] && cp -r "$OUTPUT_DIR/config/"* "$MNT/config/" 2>/dev/null || true

# Write a default go script if not present
[ -f "$MNT/config/go" ] || cat > "$MNT/config/go" <<'EOF'
#!/bin/bash
# Helix OS startup script — runs at every boot.
# This file is on the FAT32 boot partition (persistent).

# Start the web backend
systemctl start helix-backend nginx 2>/dev/null || true
EOF
chmod +x "$MNT/config/go"

SQUASH_SIZE=$(du -sh "$MNT/boot/filesystem.squashfs" | cut -f1)
echo "  Squashfs: $SQUASH_SIZE"

sync
umount "$MNT"
losetup -d "$LOOP_DEV"
trap - EXIT

# ── Compress with xz ──────────────────────────────────────────────────────────
echo ""
echo "Compressing image (xz -T0 --fast) — this may take a few minutes..."
rm -f "$XZ_PATH"
xz -T0 --fast --keep "$IMG_PATH"          # produces "${IMG_PATH}.xz"
# xz always appends .xz; only move if the desired name differs (avoids a
# "same file" error when XZ_PATH already equals "${IMG_PATH}.xz").
if [ "${IMG_PATH}.xz" != "$XZ_PATH" ]; then
    mv -f "${IMG_PATH}.xz" "$XZ_PATH"
fi
rm -f "$IMG_PATH"

SIZE=$(du -sh "$XZ_PATH" | cut -f1)

echo ""
echo "════════════════════════════════════════════════════════"
echo " Raspberry Pi image built successfully!"
echo ""
echo " File:   $XZ_PATH"
echo " Size:   $SIZE (compressed)"
echo ""
echo " Flash to SD card / USB with:"
echo "   balenaEtcher (GUI) — select the .xz file directly"
echo ""
echo "   Raspberry Pi Imager → 'Use custom' → select the .xz"
echo ""
echo "   Or manually with dd:"
echo "     xzcat $XZ_PATH | sudo dd of=/dev/sdX bs=4M status=progress"
echo ""
echo " Compatible devices: Raspberry Pi 4B, Pi 5"
echo " (For Pi 3, also copy arm64 DTBs for bcm2837)"
echo "════════════════════════════════════════════════════════"
