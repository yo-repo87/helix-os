#!/bin/bash
# test-npm-storage.sh: helix-npm-storage against an ext4 loop image standing in for HELIXDATA
# (CLAUDE.md §54.2). Needs Docker; runs in a throwaway privileged debian:bookworm container.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/../.." && pwd)
docker run --rm -i --privileged -v "$REPO/proxy:/t:ro" debian:bookworm bash -s <<'IN'
set -u; P=/t/helix-npm-storage; pass=0; fail=0
ok(){ if eval "$2"; then pass=$((pass+1)); echo "PASS $1"; else fail=$((fail+1)); echo "FAIL $1"; fi; }
st(){ grep -q "^mode=$1" /run/helix/npm-storage; }
mkdir -p /mnt/data
sh $P >/dev/null; ok "no HELIXDATA -> ram" "st ram && ! mountpoint -q /data"
mount -t tmpfs tmpfs /mnt/data; sh $P >/dev/null; ok "tmpfs -> ram" "st ram"; umount /mnt/data
truncate -s 64M /hd.img; mkfs.ext4 -q -L HELIXDATA /hd.img
L=$(losetup -f --show /hd.img)
mount -o ro $L /mnt/data; sh $P >/dev/null; ok "read-only ext4 -> ram" "st ram && grep -q read-only /run/helix/npm-storage"; umount /mnt/data
mount $L /mnt/data
mkdir -p /etc/letsencrypt; echo pre > /etc/letsencrypt/pre.txt     # written to RAM before the service ran
sh $P >/dev/null
ok "ext4 -> persistent" "st persistent"
ok "state file readable by the backend (644)" "[ \"\$(stat -c %a /run/helix/npm-storage)\" = 644 ]"
ok "/data is a mount" "mountpoint -q /data"
ok "/etc/letsencrypt is a mount" "mountpoint -q /etc/letsencrypt"
ok "earlier RAM content merged" "[ \"\$(cat /etc/letsencrypt/pre.txt)\" = pre ]"
ok "data dir root:www-data 2750" "[ \"\$(stat -c %a:%U:%G /mnt/data/npm/data)\" = 2750:root:www-data ]"
ok "npm dir root 700" "[ \"\$(stat -c %a /mnt/data/npm)\" = 700 ]"
ok "logs -> RAM symlink" "[ \"\$(readlink /data/logs)\" = /var/log/npm ] && [ -d /var/log/npm ]"
echo db > /data/database.sqlite; mkdir -p /etc/letsencrypt/archive/x; ln -s ../../archive/x/cert1.pem /etc/letsencrypt/archive/x/l 2>/dev/null; echo log > /data/logs/a.log
ok "files land on HELIXDATA" "[ -f /mnt/data/npm/data/database.sqlite ] && [ -L /mnt/data/npm/letsencrypt/archive/x/l ]"
ok "logs do not" "[ ! -e /mnt/data/npm/data/logs/a.log ] || [ -L /mnt/data/npm/data/logs ]"
sh $P >/dev/null; ok "rerun idempotent" "st persistent && [ \$(grep -c ' /data ' /proc/mounts) = 1 ]"
# "Reboot": drop the binds and RAM dirs, remount HELIXDATA, run again.
umount /data /etc/letsencrypt; umount /mnt/data; rm -rf /var/log/npm /etc/letsencrypt/*; rm -f /run/helix/npm-storage
mount $L /mnt/data; sh $P >/dev/null
ok "after reboot: data back" "[ \"\$(cat /data/database.sqlite)\" = db ] && [ -f /etc/letsencrypt/pre.txt ]"
ok "after reboot: RAM log dir recreated" "[ -d /var/log/npm ]"
echo "== $pass passed, $fail failed"; umount /data /etc/letsencrypt /mnt/data; losetup -d $L
IN
