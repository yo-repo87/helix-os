#!/bin/bash
# Push the current branch to the build host (192.168.50.142) over SSH and fast-forward its clean
# build checkout. All image builds run there (CLAUDE.md §5), never on the dev machine.
#   build/tools/sync-to-build-host.sh            # sync the current branch
# Layout on the build host: bare repo $W/helix-os.git, build checkout $W/src (never edit it there).
set -euo pipefail
HOST=${HELIX_BUILD_HOST:-helix@192.168.50.142}
KEY=${HELIX_BUILD_KEY:-$HOME/.ssh/helix_ed25519}
W=${HELIX_BUILD_WORK:-/mnt/disk1/helix-work}
BRANCH=$(git rev-parse --abbrev-ref HEAD)
cd "$(git rev-parse --show-toplevel)"

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "note: uncommitted changes are NOT synced (commit first):" >&2
    git status --short --untracked-files=no >&2
fi
export GIT_SSH_COMMAND="ssh -i $KEY -o BatchMode=yes"
git push -q "ssh://$HOST$W/helix-os.git" "$BRANCH" --tags
ssh -i "$KEY" -o BatchMode=yes "$HOST" \
    "set -e; cd $W/src && git fetch -q origin && git checkout -q $BRANCH && git reset -q --hard origin/$BRANCH && git log --oneline -1"
