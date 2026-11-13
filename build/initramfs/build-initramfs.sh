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
              mdev blockdev udevadm; do
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
    local name="$1" line rel src base pat
    [ -f "$SRC_MODS/modules.dep" ] || return 0
    # Kernel module FILE names use '-' where module names use '_' (e.g.
    # usb_storage -> usb-storage.ko, xhci_hcd -> xhci-hcd.ko). modprobe treats
    # them interchangeably, so match either when scanning modules.dep.
    pat=$(printf '%s' "$name" | sed 's/[_-]/[_-]/g')
    line=$(grep -m1 -E "(^|/)${pat}\.ko([.][a-z]+)?:" "$SRC_MODS/modules.dep") || {
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
    # Filesystems + the storage/controller drivers needed to even SEE the boot
    # device. Debian ships these as modules, so without them here the kernel
    # cannot find HELIXBOOT on a VM disk/CD (VirtIO/SATA/NVMe), USB stick, or SD
    # card. install_module() silently skips any name absent from this arch's
    # modules.dep, so arch-specific entries (e.g. sdhci_iproc) are harmless.
    for modname in \
        squashfs overlay loop \
        vfat exfat isofs nls_cp437 nls_ascii nls_utf8 nls_iso8859_1 \
        ext4 mbcache jbd2 crc32c_generic libcrc32c crc16 \
        scsi_mod sd_mod sr_mod cdrom \
        virtio virtio_ring virtio_pci virtio_mmio virtio_blk virtio_scsi \
        ahci libahci libata ata_piix ata_generic ata_sil ata_sis pata_acpi \
        nvme nvme_core \
        usb_common usbcore ehci_hcd ehci_pci ohci_hcd ohci_pci uhci_hcd \
        xhci_hcd xhci_pci usb_storage uas \
        mmc_core mmc_block sdhci sdhci_pci sdhci_acpi sdhci_pltfm \
        sdhci_of_arasan sdhci_iproc bcm2835_sdhost dw_mmc dw_mmc_pltfm \
        aesni_intel cryptd crc32c_intel crc32c \
        megaraid_sas mvsas qla2xxx jmb38x_ms firewire_ohci hid_generic \
        usbhid hv_storvsc hv_netvsc xen_blkfront xen_netfront \
        virtio_net bnx2 bnx2x ixgbe i40e mlx4_core mlx4_en mlx5_core \
        ipv6 nf_conntrack nf_nat; do
        install_module "$modname"
    done
    # Fresh dependency index for the flattened module set.
    depmod -b "$WORK_DIR" "$KERNEL_VERSION" 2>/dev/null || true
    echo "  Installed modules: $(ls "$MODS_DIR"/*.ko 2>/dev/null | wc -l)"
else
    echo "  WARN: $SRC_MODS not found — initramfs will have NO kernel modules!"
fi

# ── Boot splash: plymouth, its plugins, libraries, theme and display drivers ─────────────
# Skipped when plymouth is not in the rootfs; init then falls back to the plain text boot.
if [ -n "${ROOTFS:-}" ] && [ -x "$ROOTFS/usr/sbin/plymouthd" ]; then
    TRIPLET=$(ls "$ROOTFS/usr/lib" | grep -m1 -E '^(x86_64|aarch64)-linux-gnu$' || true)
    echo "  boot splash: plymouth ($TRIPLET)"
    copy_rel() { mkdir -p "$WORK_DIR/$(dirname "$1")"; cp -aL "$ROOTFS/$1" "$WORK_DIR/$1"; }
    add_libs() {              # copy every shared library $1 needs (recursively), from the target rootfs
        local f="$1" l p d
        for l in $(readelf -d "$f" 2>/dev/null | sed -n 's/.*Shared library: \[\(.*\)\]/\1/p'); do
            p=""
            for d in lib/$TRIPLET usr/lib/$TRIPLET lib usr/lib; do [ -e "$ROOTFS/$d/$l" ] && { p="$d/$l"; break; }; done
            [ -n "$p" ] || continue
            [ -e "$WORK_DIR/$p" ] && continue
            copy_rel "$p"; add_libs "$ROOTFS/$p"
        done
    }
    for bin in usr/sbin/plymouthd usr/bin/plymouth; do copy_rel "$bin"; add_libs "$ROOTFS/$bin"; done
    ln -sf ../usr/sbin/plymouthd sbin/plymouthd 2>/dev/null; ln -sf ../usr/bin/plymouth bin/plymouth 2>/dev/null
    # the dynamic loader (path differs per architecture)
    interp=$(readelf -l "$ROOTFS/usr/sbin/plymouthd" | sed -n 's/.*Requesting program interpreter: \(.*\)\]/\1/p')
    # Copy the loader from the rootfs's own library directory. $interp (/lib64/ld-linux-...) is an ABSOLUTE symlink in the
    # rootfs, so following it from the build host would pick up the HOST's loader, which does not match the image's libc
    # ("stack smashing detected" the moment any plymouth binary starts).
    if [ -n "$interp" ]; then
        ibase=$(basename "$interp"); isrc=""
        for d in lib/$TRIPLET usr/lib/$TRIPLET lib usr/lib; do [ -f "$ROOTFS/$d/$ibase" ] && { isrc="$ROOTFS/$d/$ibase"; break; }; done
        [ -n "$isrc" ] || { echo "  ERROR: loader $ibase not found in rootfs"; exit 1; }
        mkdir -p "$WORK_DIR$(dirname "$interp")"; cp -aL "$isrc" "$WORK_DIR$interp"
    fi
    PLY_PLUGINS="usr/lib/$TRIPLET/plymouth"
    for so in "$PLY_PLUGINS/script.so" "$PLY_PLUGINS/renderers/drm.so" "$PLY_PLUGINS/renderers/frame-buffer.so"; do
        [ -f "$ROOTFS/$so" ] && { copy_rel "$so"; add_libs "$ROOTFS/$so"; }
    done
    mkdir -p usr/share/plymouth/themes etc/plymouth
    cp -a "$ROOTFS/usr/share/plymouth/plymouthd.defaults" usr/share/plymouth/ 2>/dev/null || true
    cp -a "$ROOTFS/usr/share/plymouth/themes/helix" usr/share/plymouth/themes/ 2>/dev/null || true
    cp -a "$ROOTFS/etc/plymouth/plymouthd.conf" etc/plymouth/ 2>/dev/null || true
    # display drivers (small ones: the firmware framebuffer via simpledrm, plus virtual-machine and Pi GPUs). Big
    # desktop GPU drivers are not needed: they load later, after the system has started.
    for modname in simpledrm drm drm_kms_helper drm_shmem_helper drm_dma_helper bochs virtio_gpu cirrus vmwgfx hyperv_drm vc4; do
        install_module "$modname"
    done
    depmod -b "$WORK_DIR" "$KERNEL_VERSION" 2>/dev/null || true
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
