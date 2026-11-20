#!/bin/bash
# Helix OS — Full Build: amd64 USB image + arm64 Pi image (an ISO for VMs: build/make-iso.sh by hand)
# Runs both builds sequentially, logs everything to build-all.log
#
# Usage: sudo ./build-all.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR/.."
LOG="$PROJECT_DIR/build-all.log"
ROOTFS_DIR="/tmp/helix-build/rootfs"

[[ $EUID -eq 0 ]] || { echo "Must run as root (sudo $0)"; exit 1; }

stamp() { date '+[%H:%M:%S]'; }

section() {
    echo ""
    echo "$(stamp) ══════════════════════════════════════════════════"
    echo "$(stamp)  $1"
    echo "$(stamp) ══════════════════════════════════════════════════"
}

section "Helix OS Full Build starting"
echo "Log: $LOG"

# ── PHASE 1: amd64 rootfs + squashfs ──────────────────────────────────────────
section "Phase 1/4: Building amd64 rootfs (debootstrap + packages)"
rm -rf "$ROOTFS_DIR"
bash "$SCRIPT_DIR/build.sh" --arch amd64

# ── PHASE 2: Build ISO from amd64 output ──────────────────────────────────────
section "Phase 2/4: Building amd64 USB flash-drive image"
bash "$SCRIPT_DIR/make-x86-image.sh"

# Save the amd64 squashfs so the arm64 build doesn't clobber it
mkdir -p "$PROJECT_DIR/output-amd64"
cp -r "$PROJECT_DIR/output/." "$PROJECT_DIR/output-amd64/"
echo "$(stamp) amd64 output saved to output-amd64/"

# ── PHASE 3: arm64 rootfs + squashfs ──────────────────────────────────────────
section "Phase 3/4: Building arm64 rootfs (cross-compile via qemu binfmt)"
rm -rf "$ROOTFS_DIR"
# Use a separate output dir for arm64
OUTPUT_ORIG="$PROJECT_DIR/output"
mv "$OUTPUT_ORIG" "${OUTPUT_ORIG}.amd64.bak"
bash "$SCRIPT_DIR/build.sh" --arch arm64
echo "$(stamp) arm64 rootfs done."

# ── PHASE 4: Build Pi image from arm64 output ─────────────────────────────────
section "Phase 4/4: Building Raspberry Pi image"
bash "$SCRIPT_DIR/make-pi-image.sh"
echo ""
echo "$(stamp) Pi image: $PROJECT_DIR/helix-os-pi-$(cat "$SCRIPT_DIR/config/VERSION").img.xz"

# Restore amd64 output
mv "${OUTPUT_ORIG}.amd64.bak" "${OUTPUT_ORIG}.amd64"
mv "$PROJECT_DIR/output" "$PROJECT_DIR/output-arm64"

echo ""
section "All builds complete!"
echo ""
V="$(cat "$SCRIPT_DIR/config/VERSION")"
echo "  USB image (x86_64): $PROJECT_DIR/helix-os-$V.img.xz      <- flash this to a USB drive for a real install"
echo "  Pi image (arm64):   $PROJECT_DIR/helix-os-pi-$V.img.xz   <- flash this to an SD card / USB for a Pi"
echo ""
echo "Flash with balenaEtcher / Raspberry Pi Imager, or:"
echo "  xzcat helix-os-$V.img.xz | sudo dd of=/dev/sdX bs=4M status=progress conv=fsync"
