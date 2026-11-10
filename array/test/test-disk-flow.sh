#!/bin/bash
# Regression test for the disk page and Format flow. No real disk is touched: 64 MB image files stand in.
# Needs: python3, the webui venv (webui/.venv), mkfs.ext4, blkid, wipefs and passwordless `sudo -n`
# (Erase uses sudo wipefs; Format runs helix-disk-format as root and is expected to report an error here,
# because an image file is not a block device).
set -u
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
D="$(mktemp -d)"
PORT="${DK_PORT:-8094}"
trap 'kill "$SRV" 2>/dev/null; rm -rf "$D"' EXIT
mkdir -p "$D/cfg" "$D/state" "$D/mnt/user"
for n in d1 d2 par cache; do truncate -s 64M "$D/$n.img"; done
cat > "$D/cfg/disks.cfg" <<EOF
[parity1]
device=$D/par.img

[disk1]
device=$D/d1.img
format=xfs

[disk2]
device=$D/d2.img
format=xfs

[cache1]
device=$D/cache.img
format=btrfs
EOF
echo stopped > "$D/state/array_state"
( cd "$REPO/webui/backend" && HELIX_CONFIG="$D/cfg" STATE_DIR="$D/state" MNT_BASE="$D/mnt" USER_SHARE="$D/mnt/user" \
  ARRAY_SBIN="$REPO/array/sbin" HELIX_MOVER_DIRECT=1 "$REPO/webui/.venv/bin/uvicorn" main:app --host 127.0.0.1 --port "$PORT" > "$D/server.log" 2>&1 ) &
SRV=$!
for _ in $(seq 1 30); do curl -sf "http://127.0.0.1:$PORT/api/health" >/dev/null && break; sleep 0.5; done
DK_DIR="$D" DK_PORT="$PORT" python3 -I "$REPO/array/test/test_disk_flow.py"
