#!/bin/bash
# Helix OS — Build Bootable ISO (x86_64)
#
# Usage: sudo ./make-iso.sh [--output helix-os.iso]
#
# Requires:
#   - output/ from build.sh (run build.sh first)
#   - xorriso  (apt install xorriso)
#   - syslinux-common  (apt install syslinux-common)  — BIOS boot
#   - grub-efi-amd64-bin  (apt install grub-efi-amd64-bin)  — UEFI boot
#
# The resulting ISO supports:
#   - BIOS boot (via isolinux/syslinux)
#   - UEFI boot (via embedded EFI image)
#   - Hybrid MBR — can be dd'd directly to a USB drive

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTPUT_DIR="$SCRIPT_DIR/../output"
ISO_STAGING="$SCRIPT_DIR/../iso-staging"
ISO_OUT="${1:-}"
VERSION="$(date +%Y%m%d)"

# ── Parse args ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case $1 in
        --output) ISO_OUT="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done
ISO_OUT="${ISO_OUT:-$SCRIPT_DIR/../helix-os-${VERSION}.iso}"

[[ $EUID -eq 0 ]] || { echo "Must run as root (sudo $0)"; exit 1; }

command -v xorriso >/dev/null || {
    echo "xorriso not found. Install it:"
    echo "  apt install xorriso syslinux-common syslinux-utils"
    exit 1
}

[ -f "$OUTPUT_DIR/boot/filesystem.squashfs" ] || {
    echo "output/boot/filesystem.squashfs not found."
    echo "Run build.sh first:  sudo ./build.sh"
    exit 1
}

echo "════════════════════════════════════════════════════════"
echo " Helix OS ISO Builder"
echo " Source:  $OUTPUT_DIR"
echo " Output:  $ISO_OUT"
echo "════════════════════════════════════════════════════════"

# ── Prepare staging directory ─────────────────────────────────────────────────
rm -rf "$ISO_STAGING"
mkdir -p "$ISO_STAGING/boot" \
         "$ISO_STAGING/isolinux" \
         "$ISO_STAGING/EFI/BOOT" \
         "$ISO_STAGING/grub"

# ── Copy Helix OS boot files ───────────────────────────────────────────────────
echo "Copying boot files..."
cp "$OUTPUT_DIR/boot/vmlinuz"             "$ISO_STAGING/boot/"
cp "$OUTPUT_DIR/boot/initrd.img"          "$ISO_STAGING/boot/"
cp "$OUTPUT_DIR/boot/filesystem.squashfs" "$ISO_STAGING/boot/"

# ── BIOS boot: isolinux ───────────────────────────────────────────────────────
echo "Setting up isolinux (BIOS boot)..."

# Find isolinux.bin and ldlinux.c32 from syslinux-common
ISOLINUX_BIN=""
for path in /usr/lib/ISOLINUX/isolinux.bin /usr/lib/syslinux/modules/bios/isolinux.bin; do
    [ -f "$path" ] && ISOLINUX_BIN="$path" && break
done

if [ -z "$ISOLINUX_BIN" ]; then
    echo "  WARN: isolinux.bin not found — BIOS boot disabled."
    echo "        Install: apt install isolinux syslinux-common"
else
    cp "$ISOLINUX_BIN" "$ISO_STAGING/isolinux/"

    # Required c32 modules
    for mod in ldlinux.c32 libcom32.c32 libutil.c32 vesamenu.c32; do
        for dir in /usr/lib/syslinux/modules/bios /usr/lib/syslinux; do
            [ -f "$dir/$mod" ] && cp "$dir/$mod" "$ISO_STAGING/isolinux/" && break
        done
    done

    # Boot splash background shown by vesamenu (1280x720 PNG).
    if [ -f "$SCRIPT_DIR/boot-assets/splash.png" ]; then
        cp "$SCRIPT_DIR/boot-assets/splash.png" "$ISO_STAGING/isolinux/splash.png"
        echo "  boot splash: $ISO_STAGING/isolinux/splash.png"
        # the same picture and a font for GRUB's menu (UEFI boot); without the font GRUB falls back to a plain text menu
        mkdir -p "$ISO_STAGING/grub/fonts"
        cp "$SCRIPT_DIR/boot-assets/splash.png" "$ISO_STAGING/grub/splash.png"
        [ -f /usr/share/grub/unicode.pf2 ] && cp /usr/share/grub/unicode.pf2 "$ISO_STAGING/grub/fonts/unicode.pf2"
    fi

    cat > "$ISO_STAGING/isolinux/isolinux.cfg" <<'EOF'
UI vesamenu.c32
MENU RESOLUTION 1280 720
TIMEOUT 50
DEFAULT helix

MENU TITLE Helix OS Boot Menu
MENU BACKGROUND /isolinux/splash.png
MENU COLOR sel    7;37;40 #ff0000 #cc6600
MENU COLOR title  0;31;40 #ffffa0 #000000

LABEL helix
    MENU LABEL Helix OS
    KERNEL /boot/vmlinuz
    APPEND initrd=/boot/initrd.img vga=791 quiet splash loglevel=3 vt.global_cursor_default=0 plymouth.ignore-serial-consoles boot=helix helix_label=HELIXBOOT

LABEL verbose
    MENU LABEL Helix OS (verbose)
    KERNEL /boot/vmlinuz
    APPEND initrd=/boot/initrd.img boot=helix helix_label=HELIXBOOT

LABEL safe
    MENU LABEL Helix OS (safe mode)
    KERNEL /boot/vmlinuz
    APPEND initrd=/boot/initrd.img boot=helix helix_label=HELIXBOOT helixsafemode

LABEL memtest
    MENU LABEL Memory Test (Memtest86+)
    KERNEL /boot/memtest86+.bin
EOF

    # Copy memtest if available
    for mpath in /boot/memtest86+.bin /boot/memtest86+x64.bin; do
        [ -f "$mpath" ] && cp "$mpath" "$ISO_STAGING/boot/" && break
    done
fi

# ── UEFI boot: EFI image ──────────────────────────────────────────────────────
echo "Setting up EFI boot (UEFI)..."

# Try to use the pre-built EFI image from build.sh
if [ -f "$OUTPUT_DIR/EFI/BOOT/BOOTX64.EFI" ]; then
    cp "$OUTPUT_DIR/EFI/BOOT/BOOTX64.EFI" "$ISO_STAGING/EFI/BOOT/"
elif command -v grub-mkimage >/dev/null; then
    echo "  Building GRUB EFI image..."
    grub-mkimage \
        -O x86_64-efi \
        -o "$ISO_STAGING/EFI/BOOT/BOOTX64.EFI" \
        -p /grub \
        part_gpt part_msdos fat search search_label iso9660 \
        normal linux echo configfile gzio all_video \
        2>/dev/null || echo "  WARN: grub-mkimage failed"
else
    echo "  WARN: No UEFI bootloader available — UEFI boot disabled."
    echo "        Install: apt install grub-efi-amd64-bin"
fi

# Copy GRUB config
cp "$SCRIPT_DIR/grub/grub.cfg" "$ISO_STAGING/grub/grub.cfg"

# ── Build EFI FAT image (embedded in ISO) ────────────────────────────────────
# xorriso needs an EFI system partition as a FAT image for UEFI boot.
if [ -f "$ISO_STAGING/EFI/BOOT/BOOTX64.EFI" ]; then
    EFI_IMG="$ISO_STAGING/efi.img"
    dd if=/dev/zero of="$EFI_IMG" bs=1M count=4 2>/dev/null
    mkfs.fat -F12 "$EFI_IMG"
    mmd -i "$EFI_IMG" ::/EFI ::/EFI/BOOT
    mcopy -i "$EFI_IMG" "$ISO_STAGING/EFI/BOOT/BOOTX64.EFI" ::/EFI/BOOT/BOOTX64.EFI
fi

# ── config/ template ──────────────────────────────────────────────────────────
mkdir -p "$ISO_STAGING/config"
[ -d "$OUTPUT_DIR/config" ] && cp -r "$OUTPUT_DIR/config/"* "$ISO_STAGING/config/" 2>/dev/null || true

# ── Assemble ISO with xorriso ─────────────────────────────────────────────────
echo "Building ISO..."

XORRISO_ARGS=(
    -as mkisofs
    -iso-level 3
    -volid "HELIXBOOT"
    -appid "Helix OS"
    -publisher "Helix OS Project"
    -preparer "Helix OS make-iso.sh"
)

# BIOS boot support
if [ -f "$ISO_STAGING/isolinux/isolinux.bin" ]; then
    XORRISO_ARGS+=(
        -b isolinux/isolinux.bin
        -c isolinux/boot.cat
        -no-emul-boot
        -boot-load-size 4
        -boot-info-table
    )
fi

# UEFI boot support
if [ -f "$ISO_STAGING/efi.img" ]; then
    XORRISO_ARGS+=(
        -eltorito-alt-boot
        -e efi.img
        -no-emul-boot
    )
fi

# Hybrid MBR (allows dd to USB)
XORRISO_ARGS+=(
    -isohybrid-mbr /usr/lib/ISOLINUX/isohdpfx.bin
    -isohybrid-gpt-basdat
)

xorriso "${XORRISO_ARGS[@]}" \
    -output "$ISO_OUT" \
    "$ISO_STAGING" 2>&1 | grep -v "^xorriso : UPDATE" || \
xorriso -as mkisofs \
    -iso-level 3 \
    -volid "HELIXBOOT" \
    -output "$ISO_OUT" \
    "$ISO_STAGING"

SIZE=$(du -sh "$ISO_OUT" | cut -f1)

echo ""
echo "════════════════════════════════════════════════════════"
echo " ISO built successfully!"
echo ""
echo " File:    $ISO_OUT"
echo " Size:    $SIZE"
echo ""
echo " Flash to USB with:"
echo "   sudo dd if=$ISO_OUT of=/dev/sdX bs=4M status=progress conv=fsync"
echo "   # or use balenaEtcher, Rufus, or Ventoy"
echo ""
echo " The ISO is a hybrid image — it boots from USB (dd) or"
echo " from a virtual CD-ROM drive in a VM."
echo "════════════════════════════════════════════════════════"
