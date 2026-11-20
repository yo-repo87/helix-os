#!/bin/bash
# Build Helix OS on the build host (192.168.50.142) inside the builder container. Run on that host as `helix`
# from the synced build checkout (/mnt/disk1/helix-work/src):
#   build/builder/run.sh                 both architectures
#   build/builder/run.sh amd64           one
# Output: images in the checkout root, OTA bundle in dist/update, logs in /mnt/disk1/helix-work/logs.
set -euo pipefail
SRC="$(cd "$(dirname "$0")/../.." && pwd)"
WORK="${HELIX_BUILD_WORK:-/mnt/disk1/helix-work}"
IMAGE=helix-builder:bookworm
mkdir -p "$WORK/logs"

sudo -n docker build -q -t "$IMAGE" "$SRC/build/builder" >/dev/null
# arm64 on x86: register qemu binfmt (with the F flag, so the chroot needs no emulator inside). Lost at reboot.
if [[ " ${*:-amd64 arm64} " == *" arm64 "* ]] && [ ! -e /proc/sys/fs/binfmt_misc/qemu-aarch64 ]; then
    sudo -n docker run --privileged --rm tonistiigi/binfmt --install arm64 >/dev/null
fi

LOG="$WORK/logs/build-$(date +%Y%m%d-%H%M%S).log"
NAME="helix-build-$$"
echo "Building (log: $LOG, container $NAME)"
# Stopping this script (systemctl stop helix-build) must stop the container too: `docker run` exiting does not.
trap 'sudo -n docker kill "$NAME" >/dev/null 2>&1; exit 143' TERM INT
rc=0
sudo -n docker run --rm --name "$NAME" --privileged -v /dev:/dev -v "$SRC:/src" -v "$WORK:/work" "$IMAGE" \
    bash /src/build/builder/build-release.sh "$@" >"$LOG" 2>&1 &
wait $! || rc=$?
sudo -n chown -R "$(id -u):$(id -g)" "$SRC" "$WORK" 2>/dev/null || true
tail -25 "$LOG"
exit $rc
