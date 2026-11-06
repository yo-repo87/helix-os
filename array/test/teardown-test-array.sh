#!/bin/bash
# teardown-test-array.sh — Stop and clean up the test array.

set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Must run as root"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ARRAY_SBIN="$SCRIPT_DIR/../sbin"
IMG_DIR="/tmp/helix-test-imgs"
TEST_CFG="/tmp/helix-test-config"

echo "Stopping test array..."
HELIX_CONFIG="$TEST_CFG" bash "$ARRAY_SBIN/helix-array-stop" 2>/dev/null || true

echo "Unmounting any remaining mounts..."
for mnt in /mnt/disk1 /mnt/disk2 /mnt/disk3 /mnt/parity1 /mnt/user; do
    mountpoint -q "$mnt" 2>/dev/null && umount "$mnt" 2>/dev/null && echo "  unmounted $mnt" || true
done

echo "Detaching loop devices..."
for img in "$IMG_DIR"/*.img; do
    [ -f "$img" ] || continue
    dev=$(losetup -j "$img" 2>/dev/null | cut -d: -f1 | head -1)
    if [ -n "$dev" ]; then
        losetup -d "$dev" && echo "  detached $dev ($img)"
    fi
done

echo "Removing image files..."
rm -rf "$IMG_DIR" "$TEST_CFG"
rm -f /var/local/helix/array_state /etc/snapraid.conf

echo "Done."
