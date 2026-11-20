# Helix OS builder: every tool the image build needs, so builds on the build host (192.168.50.142, whose root
# filesystem is RAM) never depend on packages installed on the host. Run it through build/builder/run.sh.
FROM debian:bookworm
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        debootstrap squashfs-tools xorriso isolinux syslinux-common syslinux-utils \
        grub-common grub-pc-bin grub-efi-amd64-bin grub-efi-arm64-bin dosfstools mtools \
        parted fdisk e2fsprogs xz-utils zstd cpio kmod busybox-static binutils file \
        curl wget ca-certificates gnupg python3 rsync git util-linux procps qemu-utils \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
