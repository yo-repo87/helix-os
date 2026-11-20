#!/bin/bash
# helix-data-partition on loop-device "sticks" (never a real drive). Run in the builder container on the build host:
#   docker run --rm --privileged -v /dev:/dev -v $PWD:/src helix-builder:bookworm bash /src/array/test/test-data-partition.sh
set -u
H=/src/array/sbin/helix-data-partition
export HELIX_DATA_LABEL=HXTEST$$ HELIX_BOOT_MNT=/tmp/tb HELIX_DATA=/tmp/td
P=0; F=0; LOOPS=()
ok()  { echo "PASS $1"; P=$((P+1)); }
bad() { echo "FAIL $1"; F=$((F+1)); }
J() { python3 -c "import json,sys; d=json.load(sys.stdin); print(d$1)"; }
stick() {   # stick <gpt|msdos> <total MiB> <boot end MiB> [extra partitions]
    umount /tmp/tb 2>/dev/null; rm -f /tmp/stick.img; truncate -s "$2M" /tmp/stick.img
    L=$(losetup -fP --show /tmp/stick.img); LOOPS+=("$L")
    parted -s "$L" mklabel "$1"
    if [ "$1" = gpt ]; then parted -s "$L" mkpart biosboot 1MiB 2MiB; parted -s "$L" set 1 bios_grub on
         parted -s "$L" mkpart HELIXBOOT fat32 2MiB "$3MiB"; BP=${L}p2
    else parted -s "$L" mkpart primary fat32 1MiB "$3MiB"; BP=${L}p1; fi
    for extra in "${@:4}"; do parted -s "$L" mkpart primary ext4 $extra; done
    partx -u "$L" 2>/dev/null; sleep 1
    mkfs.fat -F32 -n HELIXBOOT "$BP" >/dev/null; mkdir -p /tmp/tb/config; mount "$BP" /tmp/tb; mkdir -p /tmp/tb/config

}
cleanup() { umount /tmp/tb 2>/dev/null; for l in "${LOOPS[@]}"; do losetup -d "$l" 2>/dev/null; done; rm -f /tmp/stick.img; }
trap cleanup EXIT

stick gpt 3000 300; st=$($H status)
[ "$(echo "$st" | J "['can_create']")" = True ] && ok "GPT stick with 2.7 GB free: can create" || bad "GPT can_create: $st"
echo "$st" | J "['reason']" | grep -q "older layout" && ok "reason explains the old layout and free space" || bad "reason: $st"
out=$($H create) && ok "create succeeded" || bad "create: $out"
blkid -L "$HELIX_DATA_LABEL" >/dev/null && ok "new partition found by its label" || bad "label not found"
parted -s "$L" unit MiB print | grep -qE "^ 1 .*1\.00MiB +2\.00MiB" && parted -s "$L" unit MiB print | grep -qE "^ 2 .*2\.00MiB +300MiB" \
    && ok "partitions 1 and 2 unchanged" || bad "existing partitions changed: $(parted -s $L unit MiB print)"
[ "$(blkid -o value -s TYPE ${L}p3)" = ext4 ] && ok "partition 3 is ext4" || bad "type"
mountpoint -q /tmp/tb && ok "boot partition still mounted" || bad "boot unmounted"
ls /tmp/tb/config/partitions-*.txt >/dev/null 2>&1 && ok "layout recorded on the boot partition" || bad "no layout record"
st=$($H status); [ "$(echo "$st" | J "['can_create']")" = False ] && echo "$st" | J "['reason']" | grep -q "exists" \
    && ok "second run: refuses (one exists)" || bad "second run: $st"
$H create >/dev/null 2>&1 && bad "create ran twice" || ok "create refuses when one exists"

stick msdos 3000 300; $H create >/dev/null && [ "$(blkid -o value -s TYPE ${L}p2)" = ext4 ] \
    && ok "MBR stick: data partition added as partition 2" || bad "MBR create"

stick msdos 3000 300 "300MiB 400MiB" "400MiB 500MiB" "500MiB 600MiB"
st=$($H status); echo "$st" | J "['reason']" | grep -q "4 partitions" && [ "$(echo "$st" | J "['can_create']")" = False ] \
    && ok "MBR stick with 4 partitions: refused with a reason" || bad "4 partitions: $st"

stick gpt 1500 300; st=$($H status); echo "$st" | J "['reason']" | grep -q "no unused space" \
    && ok "less than 2 GiB free: refused" || bad "small: $st"

umount /tmp/tb; rm -f /tmp/iso.img; mkdir -p /tmp/isosrc && echo x > /tmp/isosrc/f && xorriso -as mkisofs -o /tmp/iso.img /tmp/isosrc >/dev/null 2>&1
IL=$(losetup -f --show /tmp/iso.img); LOOPS+=("$IL"); mount -o ro "$IL" /tmp/tb
st=$($H status); echo "$st" | J "['reason']" | grep -q "read-only media" && ok "ISO boot: says read-only media" || bad "iso: $st"
umount /tmp/tb

echo "$P passed, $F failed"; [ $F -eq 0 ]
