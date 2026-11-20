#!/bin/bash
# Runs INSIDE the builder container (see run.sh). Builds one or both architectures from a fresh rootfs and stages
# the OTA bundle. Per-arch scratch lives on the host disk (/work), never shared between architectures.
#   build-release.sh [amd64] [arm64]      (default: both)
#   build-release.sh amd64-image arm64    reuse /work/out-amd64 (system already built): only redo the USB image + bundle
set -euo pipefail
cd /src
V="$(cat build/config/VERSION)"
ARCHES=("$@"); [ ${#ARCHES[@]} -gt 0 ] || ARCHES=(amd64 arm64)
mkdir -p /work
rm -rf dist/update                                     # a fresh manifest for this version only

for ARCH in "${ARCHES[@]}"; do
    if [ "$ARCH" = amd64-image ]; then
        echo "════ amd64: reusing /work/out-amd64 ════"
        [ -f /work/out-amd64/boot/filesystem.squashfs ] || { echo "ERROR: no /work/out-amd64"; exit 1; }
        rm -rf output; cp -a /work/out-amd64 output
        bash build/make-x86-image.sh
        bash build/make-update-bundle.sh --arch amd64 --from output/boot --version "$V"
        continue
    fi
    echo "════ $ARCH: fresh rootfs ════"
    mkdir -p "/work/build-$ARCH"
    rm -rf "/work/build-$ARCH/rootfs"                   # a reused rootfs keeps packages added earlier
    ln -sfn "/work/build-$ARCH" /tmp/helix-build
    HELIX_NPM_CACHE=/work/npm-cache bash build/build.sh --arch "$ARCH"
    if [ "$ARCH" = amd64 ]; then
        bash build/make-x86-image.sh                    # the ISO is no longer a release product (make-iso.sh is kept)
    else
        bash build/make-pi-image.sh
    fi
    bash build/make-update-bundle.sh --arch "$ARCH" --from output/boot --version "$V"
    rm -rf "/work/out-$ARCH"; cp -a output "/work/out-$ARCH"
done

echo "════ result ════"
ls -la helix-os-"$V"* helix-os-pi-"$V"* 2>/dev/null || true
cd dist/update && sha256sum ./* && cat helix-release.json
