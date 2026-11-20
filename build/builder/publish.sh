#!/bin/bash
# Publish the built release from the build host (192.168.50.142) with the builder container's gh.
# Run as `helix` in the synced build checkout after build/builder/run.sh, with the token in ./gitkey (ignored by git):
#   build/builder/publish.sh [publish-update.sh args]
# The commit being released must already be on GitHub (publish-update.sh checks).
set -euo pipefail
SRC="$(cd "$(dirname "$0")/../.." && pwd)"
[ -f "$SRC/gitkey" ] || { echo "ERROR: put the GitHub token in $SRC/gitkey first"; exit 1; }
sudo -n docker build -q -t helix-builder:bookworm "$SRC/build/builder" >/dev/null
# git inside the container must trust the bind-mounted checkout (owned by another uid).
sudo -n docker run --rm -v "$SRC:/src" helix-builder:bookworm \
    bash -c 'git config --global --add safe.directory /src && bash /src/build/publish-update.sh "$@"' _ "$@"
