#!/bin/bash
# publish-update.sh — publish the staged update bundle to a GitHub release.
#
# Reads dist/update/helix-release.json (produced by make-update-bundle.sh),
# creates a release tagged v<version>, and uploads every dist/update/* asset
# (the arch images + the manifest). The in-OS updater then finds it.
#
# Auth: uses $GH_TOKEN, else the repo-root ./gitkey file. Needs a token with
# write access to the target repo.
#
# Usage:
#   ./publish-update.sh                      # defaults: repo from manifest/below
#   ./publish-update.sh --repo yo-repo87/helix-os --prerelease
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR/.."
DIST="$PROJECT_DIR/dist/update"
MANIFEST="$DIST/helix-release.json"
REPO="yo-repo87/helix-os"
PRERELEASE=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --repo)       REPO="$2"; shift 2 ;;
        --prerelease) PRERELEASE="--prerelease"; shift ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

command -v gh >/dev/null || { echo "ERROR: gh CLI not found"; exit 1; }
[ -f "$MANIFEST" ] || { echo "ERROR: $MANIFEST not found — run make-update-bundle.sh first"; exit 1; }

# Token: env first, then repo-root gitkey.
if [ -z "${GH_TOKEN:-}" ]; then
    if [ -f "$PROJECT_DIR/gitkey" ]; then
        GH_TOKEN="$(cat "$PROJECT_DIR/gitkey")"
    else
        echo "ERROR: set GH_TOKEN or provide ./gitkey"; exit 1
    fi
fi
export GH_TOKEN

VERSION="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$MANIFEST")"
NOTES="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("notes",""))' "$MANIFEST")"
TAG="v$VERSION"
# Tag the commit these images were built from (without --target GitHub tags the default
# branch, which once left v3.3.0 pointing at old code). The commit must be on GitHub.
TARGET="$(git -C "$PROJECT_DIR" rev-parse HEAD)"
if ! gh api "repos/$REPO/commits/$TARGET" >/dev/null 2>&1; then
    echo "ERROR: commit $TARGET is not on GitHub yet; push the branch first"; exit 1
fi

echo "Publishing $REPO release $TAG"
ASSETS=("$MANIFEST")
for f in "$DIST"/amd64-* "$DIST"/arm64-*; do
    [ -f "$f" ] && ASSETS+=("$f")
done
echo "Assets:"; printf '  %s\n' "${ASSETS[@]##*/}"

if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
    echo "Release $TAG exists — uploading/clobbering assets."
    gh release upload "$TAG" --repo "$REPO" --clobber "${ASSETS[@]}"
else
    gh release create "$TAG" --repo "$REPO" $PRERELEASE \
        --target "$TARGET" \
        --title "Helix OS $TAG" \
        --notes "${NOTES:-Helix OS $TAG}" \
        "${ASSETS[@]}"
fi

echo "Published: https://github.com/$REPO/releases/tag/$TAG"
