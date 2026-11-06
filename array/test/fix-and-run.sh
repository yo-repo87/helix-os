#!/bin/bash
# Fixes the loop device sector size issue and runs the full test array.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Must run as root"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ARRAY_SBIN="$SCRIPT_DIR/../sbin"
IMG_DIR="/tmp/helix-test-imgs"
TEST_CFG="/tmp/helix-test-config"

echo "==> Cleaning up any existing loop devices and mounts..."
for mnt in /mnt/user /mnt/disk1 /mnt/disk2 /mnt/disk3 /mnt/parity1; do
    mountpoint -q "$mnt" 2>/dev/null && umount -l "$mnt" 2>/dev/null && echo "  unmounted $mnt" || true
done

# Detach all loop devices pointing at our images
for img in "$IMG_DIR"/*.img; do
    [ -f "$img" ] || continue
    while read -r dev _; do
        [ -n "$dev" ] && losetup -d "$dev" && echo "  detached $dev"
    done < <(losetup -j "$img" 2>/dev/null | cut -d: -f1 | while read -r d; do echo "$d"; done)
done

echo ""
echo "==> Creating sparse disk images (512MB each)..."
mkdir -p "$IMG_DIR" "$TEST_CFG"
for n in 1 2 3; do
    truncate -s 512M "$IMG_DIR/disk${n}.img"
    echo "  disk${n}.img"
done
truncate -s 512M "$IMG_DIR/parity1.img"
echo "  parity1.img"

echo ""
echo "==> Attaching loop devices with 512-byte sector size..."
DISK1=$(losetup --find --show --sector-size 512 "$IMG_DIR/disk1.img")
DISK2=$(losetup --find --show --sector-size 512 "$IMG_DIR/disk2.img")
DISK3=$(losetup --find --show --sector-size 512 "$IMG_DIR/disk3.img")
PARITY=$(losetup --find --show --sector-size 512 "$IMG_DIR/parity1.img")
echo "  disk1   → $DISK1"
echo "  disk2   → $DISK2"
echo "  disk3   → $DISK3"
echo "  parity1 → $PARITY"

echo ""
echo "==> Formatting data drives as XFS..."
mkfs.xfs -f "$DISK1" && echo "  $DISK1 → XFS OK"
mkfs.xfs -f "$DISK2" && echo "  $DISK2 → XFS OK"
mkfs.xfs -f "$DISK3" && echo "  $DISK3 → XFS OK"
echo "  $PARITY → (raw, no format — SnapRAID uses directly)"

echo ""
echo "==> Writing test config..."
mkdir -p "$TEST_CFG"
cat > "$TEST_CFG/disks.cfg" <<EOF
[parity1]
device=$PARITY

[disk1]
device=$DISK1
format=xfs

[disk2]
device=$DISK2
format=xfs

[disk3]
device=$DISK3
format=xfs

[sharedefaults]
included_disks=all
allocation=most_free
split_level=1
EOF

cat > "$TEST_CFG/shares.cfg" <<EOF
[media]
comment=Test media share
included_disks=all
allocation=most_free
split_level=2
smb=no
nfs=no
EOF

echo ""
echo "==> Starting array..."
HELIX_CONFIG="$TEST_CFG" bash "$ARRAY_SBIN/helix-array-start"

echo ""
echo "==> Creating test files..."
mkdir -p /mnt/user/media/movies /mnt/user/downloads
dd if=/dev/urandom bs=1M count=10 2>/dev/null > /mnt/user/media/movies/test-movie.mkv
dd if=/dev/urandom bs=1M count=5  2>/dev/null > /mnt/user/downloads/test-download.bin
echo "  Created test-movie.mkv (10MB) and test-download.bin (5MB)"

echo ""
echo "==> Syncing parity (SnapRAID)..."
HELIX_CONFIG="$TEST_CFG" bash "$ARRAY_SBIN/helix-parity-check"

echo ""
echo "==> Array status:"
echo ""
HELIX_CONFIG="$TEST_CFG" bash "$ARRAY_SBIN/helix-array-status"

echo ""
echo "════════════════════════════════════════════"
echo " Test array running! Key paths:"
echo "  /mnt/user/     ← merged user share"
echo "  /mnt/disk1-3   ← individual drives"
echo ""
echo " Teardown: sudo bash teardown-test-array.sh"
echo "════════════════════════════════════════════"
