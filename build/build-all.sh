#!/bin/bash
# Helix OS — Full Build: amd64 ISO + arm64 Pi image
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
section "Phase 2/4: Building amd64 ISO"
bash "$SCRIPT_DIR/make-iso.sh"
echo ""
echo "$(stamp) ISO: $(ls "$PROJECT_DIR"/helix-os-*.iso 2>/dev/null | head -1)"

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
echo "$(stamp) Pi image: $(ls "$PROJECT_DIR"/helix-os-pi-*.img.xz 2>/dev/null | head -1)"

# Restore amd64 output
mv "${OUTPUT_ORIG}.amd64.bak" "${OUTPUT_ORIG}.amd64"
mv "$PROJECT_DIR/output" "$PROJECT_DIR/output-arm64"

echo ""
section "All builds complete!"
echo ""
echo "  ISO (x86_64):      $(ls "$PROJECT_DIR"/helix-os-*.iso 2>/dev/null | head -1)"
echo "  Pi image (arm64):  $(ls "$PROJECT_DIR"/helix-os-pi-*.img.xz 2>/dev/null | head -1)"
echo ""
echo "Flash the ISO to a USB drive:"
echo "  sudo dd if=helix-os-*.iso of=/dev/sdX bs=4M status=progress conv=fsync"
echo ""
echo "Flash the Pi image with balenaEtcher, Raspberry Pi Imager, or:"
echo "  xzcat helix-os-pi-*.img.xz | sudo dd of=/dev/sdX bs=4M status=progress"
