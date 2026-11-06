#!/bin/bash
# Builds the custom Helix OS initramfs (cpio archive).
# Must run as root. Outputs: ../output/boot/initrd.img
#
# Requires on host: busybox-static, util-linux (blkid, switch_root),
#                   kmod, zstd or gzip

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTPUT_DIR="$SCRIPT_DIR/../../output/boot"
WORK_DIR="/tmp/helix-initramfs"
KERNEL_VERSION="${KERNEL_VERSION:-$(ls /lib/modules/ | sort -V | tail -1)}"

echo "==> Building initramfs for kernel $KERNEL_VERSION"
echo "==> Output: $OUTPUT_DIR/initrd.img"

# ── Clean slate ───────────────────────────────────────────────────────────────
rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR"
mkdir -p "$OUTPUT_DIR"

cd "$WORK_DIR"

# ── Directory skeleton ────────────────────────────────────────────────────────
mkdir -p bin sbin usr/bin usr/sbin lib lib64 lib/modules \
         dev proc sys run root tmp \
         run/helix/usb run/helix/ro run/helix/rw run/helix/img \
         etc/udev

# ── busybox (static, from the target rootfs → architecture-correct) ──────────
# Pulling busybox from the rootfs guarantees the initramfs matches the image
# architecture (amd64 OR arm64). A static build needs no bundled libraries.
find_rootfs_busybox() {
    local p
    for p in bin/busybox usr/bin/busybox sbin/busybox; do
        [ -x "$ROOTFS/$p" ] && { echo "$ROOTFS/$p"; return 0; }
    done
    return 1
}

BUSYBOX=""
if [ -n "${ROOTFS:-}" ]; then
    BUSYBOX=$(find_rootfs_busybox || true)
fi
if [ -n "$BUSYBOX" ]; then
    echo "  busybox: $BUSYBOX (from rootfs — arch-correct)"
else
    # Fallback: host busybox. Only valid when host arch == image arch.
    BUSYBOX=$(command -v busybox-static 2>/dev/null || command -v busybox 2>/dev/null) \
        || die "busybox not found — add busybox-static to packages-base.list"
    echo "  WARN: using HOST busybox ($BUSYBOX) — valid only if host arch == image arch"
fi

# Warn if busybox is dynamically linked (its libs would be missing in initramfs)
if ldd "$BUSYBOX" >/dev/null 2>&1; then
    echo "  WARN: busybox appears dynamically linked — initramfs may miss libraries"
fi

cp "$BUSYBOX" bin/busybox
chmod +x bin/busybox

# Applet symlinks — busybox provides every tool the init script needs.
for applet in sh ash ls cat cp mv rm mkdir rmdir ln echo printf \
              mount umount sleep uname dmesg du grep sed cut head tr \
              mknod modprobe insmod switch_root pivot_root blkid findfs \
              mdev udevadm; do
    ln -sf busybox "bin/$applet" 2>/dev/null || true
done
ln -sf ../bin/busybox sbin/switch_root 2>/dev/null || true
ln -sf ../bin/busybox sbin/modprobe    2>/dev/null || true

# ── Kernel modules (from the target rootfs, with dependency closure) ─────────
MODS_DIR="lib/modules/$KERNEL_VERSION"
mkdir -p "$MODS_DIR"
if [ -n "${ROOTFS:-}" ] && [ -d "$ROOTFS/lib/modules/$KERNEL_VERSION" ]; then
    SRC_MODS="$ROOTFS/lib/modules/$KERNEL_VERSION"
else
    SRC_MODS="/lib/modules/$KERNEL_VERSION"
fi

install_module() {
    # Copy a module AND its dependency closure (modules.dep lists transitive
    # deps per line), decompressing .ko.xz/.zst/.gz so busybox can load them.
    local name="$1" line rel src base
    [ -f "$SRC_MODS/modules.dep" ] || return 0
    line=$(grep -m1 -E "(^|/)${name}\.ko([.][a-z]+)?:" "$SRC_MODS/modules.dep") || {
        echo "  (module not found: $name)"; return 0; }
    for rel in $(echo "$line" | tr ':' ' '); do
        src="$SRC_MODS/$rel"
        [ -f "$src" ] || continue
        base=$(basename "$rel")
        case "$base" in
            *.ko.xz)  xz   -dc  "$src" > "$MODS_DIR/${base%.xz}"  ;;
            *.ko.zst) zstd -dcq "$src" > "$MODS_DIR/${base%.zst}" ;;
            *.ko.gz)  gzip -dc  "$src" > "$MODS_DIR/${base%.gz}"  ;;
            *.ko)     cp "$src" "$MODS_DIR/$base" ;;
        esac
    done
}

if [ -d "$SRC_MODS" ]; then
    echo "  Modules from: $SRC_MODS"
    for modname in squashfs overlay loop vfat nls_cp437 nls_ascii nls_utf8 isofs \
                   crc32c_generic libcrc32c; do
        install_module "$modname"
    done
    # Fresh dependency index for the flattened module set.
    depmod -b "$WORK_DIR" "$KERNEL_VERSION" 2>/dev/null || true
    echo "  Installed modules: $(ls "$MODS_DIR"/*.ko 2>/dev/null | wc -l)"
else
    echo "  WARN: $SRC_MODS not found — initramfs will have NO kernel modules!"
fi

# ── udev minimal rules (so blkid can find devices by label) ──────────────────
cat > etc/udev/udev.conf <<'EOF'
udev_log=err
EOF

# ── Our init script ───────────────────────────────────────────────────────────
cp "$SCRIPT_DIR/init" init
chmod +x init

# ── /dev: minimal static nodes (udev will fill the rest) ─────────────────────
mknod -m 622 dev/console c 5 1  2>/dev/null || true
mknod -m 666 dev/null    c 1 3  2>/dev/null || true
mknod -m 666 dev/zero    c 1 5  2>/dev/null || true
mknod -m 444 dev/random  c 1 8  2>/dev/null || true
mknod -m 444 dev/urandom c 1 9  2>/dev/null || true
mknod -m 666 dev/tty     c 5 0  2>/dev/null || true
mknod -m 666 dev/tty0    c 4 0  2>/dev/null || true

# ── Pack ──────────────────────────────────────────────────────────────────────
echo "==> Packing initramfs..."
find . | cpio -H newc -o --quiet | zstd -9 -T0 > "$OUTPUT_DIR/initrd.img"

SIZE=$(du -sh "$OUTPUT_DIR/initrd.img" | cut -f1)
echo "==> Done: $OUTPUT_DIR/initrd.img ($SIZE)"
