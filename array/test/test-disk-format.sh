#!/bin/bash
# Runs the real helix-disk-format against LOOP devices backed by image files. Never touches a real disk.
# Needs root via passwordless `sudo -n` (losetup, mkfs, mount). Skips cleanly if that is not available.
set -u
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
sudo -n true 2>/dev/null || { echo "SKIP: needs passwordless sudo"; exit 0; }
D="$(mktemp -d)"
LOOPS=()
cleanup() {
    for m in "$D"/mnt/parity1 "$D"/mnt/cache1; do sudo -n umount "$m" 2>/dev/null || true; done
    for l in "${LOOPS[@]:-}"; do [ -n "$l" ] && sudo -n losetup -d "$l" 2>/dev/null || true; done
    sudo -n rm -rf "$D"
}
trap cleanup EXIT
pass=0; fail=0
ok()  { echo "PASS: $1"; pass=$((pass+1)); }
bad() { echo "FAIL: $1"; fail=$((fail+1)); }

mkdir -p "$D/cfg" "$D/state" "$D/mnt/user"
for n in par cache; do truncate -s 64M "$D/$n.img"; done
PAR=$(sudo -n losetup -f --show "$D/par.img");   LOOPS+=("$PAR")
CAC=$(sudo -n losetup -f --show "$D/cache.img"); LOOPS+=("$CAC")
cat > "$D/cfg/disks.cfg" <<EOF
[parity1]
device=$PAR
format=ext4

[cache1]
device=$CAC
format=ext4
EOF
echo started > "$D/state/array_state"

run() { sudo -n env HELIX_CONFIG="$D/cfg" STATE_DIR="$D/state" MNT_BASE="$D/mnt" USER_SHARE="$D/mnt/user" bash "$REPO/array/sbin/helix-disk-format" 2>&1; }

OUT=$(run); RC=$?
[ "$RC" -eq 0 ] && ok "format run exits 0" || bad "format run exit $RC: $OUT"
[ "$(sudo -n blkid -s TYPE -o value "$PAR")" = ext4 ] && ok "parity disk got ext4 (the chosen format=)" || bad "parity not formatted"
mountpoint -q "$D/mnt/parity1" && ok "parity mounted at /mnt/parity1" || bad "parity not mounted"
mountpoint -q "$D/mnt/cache1" && ok "cache mounted at /mnt/cache1" || bad "cache not mounted"
echo "$OUT" | grep -q "No data disk is mounted" && ok "no data disk: user share stays down, said so" || bad "missing no-data-disk note"

# a mounted disk must be left alone (data written must survive a second run)
sudo -n sh -c "echo keep > '$D/mnt/parity1/marker'"
OUT=$(run); RC=$?
[ "$RC" -eq 2 ] && ok "second run formats nothing (exit 2)" || bad "second run exit $RC: $OUT"
[ "$(sudo -n cat "$D/mnt/parity1/marker" 2>/dev/null)" = keep ] && ok "data on a mounted disk survives a second run" || bad "mounted disk was touched"

# refuses when the array is not started
echo stopped > "$D/state/array_state"
OUT=$(run); RC=$?
[ "$RC" -ne 0 ] && echo "$OUT" | grep -q "not started" && ok "refuses while the array is stopped" || bad "ran while stopped: $OUT"
echo started > "$D/state/array_state"

# a bad file system name is rejected before anything is formatted
sudo -n umount "$D/mnt/cache1"; sudo -n wipefs -aq "$CAC"
sed -i 's/^format=ext4$/format=zfs/' "$D/cfg/disks.cfg"
OUT=$(run); RC=$?
[ "$RC" -ne 0 ] && echo "$OUT" | grep -q "bad filesystem" && ok "rejects an unsupported file system" || bad "accepted bad fs: $OUT"
[ -z "$(sudo -n blkid -s TYPE -o value "$CAC")" ] && ok "nothing was formatted for the bad request" || bad "formatted despite bad fs"

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
