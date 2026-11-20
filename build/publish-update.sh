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
#   ./publish-update.sh --update-only          # OTA files only, no flash images (not for normal releases)
#
# Before anything is uploaded it checks that every architecture in the manifest has its three files with matching
# sha256 (3.3.4 shipped wrong hashes; 3.3.5 listed arm64 without the arm64 files), and that the flash images exist:
#   amd64 -> helix-os-<v>.img.xz (USB drive)   arm64 -> helix-os-pi-<v>.img.xz   (no ISO since 3.3.6)
# After uploading it compares the release's asset list and sizes with what was sent and re-uploads anything missing
# (gh release upload --clobber deletes first; a GitHub 500 mid-upload has left releases without a file).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR/.."
DIST="$PROJECT_DIR/dist/update"
MANIFEST="$DIST/helix-release.json"
REPO="yo-repo87/helix-os"
PRERELEASE=""
UPDATE_ONLY=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --repo)       REPO="$2"; shift 2 ;;
        --prerelease) PRERELEASE="--prerelease"; shift ;;
        --update-only) UPDATE_ONLY=1; shift ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

command -v gh >/dev/null || { echo "ERROR: gh CLI not found"; exit 1; }
[ -f "$MANIFEST" ] || { echo "ERROR: $MANIFEST not found — run make-update-bundle.sh first"; exit 1; }

# Token: env first, then repo-root gitkey. gitkey may hold labels and several tokens: the first fine-grained
# (github_pat_) token wins, else the first classic (ghp_) one.
if [ -z "${GH_TOKEN:-}" ]; then
    if [ -f "$PROJECT_DIR/gitkey" ]; then
        GH_TOKEN="$(grep -oE '^[[:space:]]*github_pat_[A-Za-z0-9_]+' "$PROJECT_DIR/gitkey" | head -1 | tr -d '[:space:]')"
        [ -n "$GH_TOKEN" ] || GH_TOKEN="$(grep -oE '^[[:space:]]*ghp_[A-Za-z0-9]+' "$PROJECT_DIR/gitkey" | head -1 | tr -d '[:space:]')"
        [ -n "$GH_TOKEN" ] || { echo "ERROR: no github_pat_/ghp_ token found in ./gitkey"; exit 1; }
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
# ── every manifest entry: file present, sha256 matches ──
ARCHES="$(python3 - "$MANIFEST" "$DIST" <<'PY'
import hashlib, json, os, sys
m = json.load(open(sys.argv[1])); dist = sys.argv[2]; bad = []
for arch, files in (m.get("arch") or {}).items():
    for key in ("filesystem.squashfs", "vmlinuz", "initrd.img"):
        e = files.get(key)
        if not e:
            bad.append(f"{arch}: {key} missing from the manifest"); continue
        p = os.path.join(dist, e["asset"])
        if not os.path.isfile(p):
            bad.append(f"{arch}: {e['asset']} listed but not in {dist}"); continue
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != e["sha256"]:
            bad.append(f"{arch}: {e['asset']} sha256 does not match the manifest")
        if os.path.getsize(p) != e.get("size", os.path.getsize(p)):
            bad.append(f"{arch}: {e['asset']} size does not match the manifest")
if bad:
    print("\n".join("ERROR: " + b for b in bad), file=sys.stderr); sys.exit(1)
print(" ".join(sorted((m.get("arch") or {}).keys())))
PY
)" || exit 1
[ -n "$ARCHES" ] || { echo "ERROR: the manifest lists no architecture"; exit 1; }
echo "Manifest OK for: $ARCHES"

ASSETS=("$MANIFEST")
for a in $ARCHES; do
    for f in "$DIST/$a"-*; do [ -f "$f" ] && ASSETS+=("$f"); done
done
if [ -z "$UPDATE_ONLY" ]; then
    want=()
    [[ " $ARCHES " == *" amd64 "* ]] && want+=("helix-os-$VERSION.img.xz")
    [[ " $ARCHES " == *" arm64 "* ]] && want+=("helix-os-pi-$VERSION.img.xz")
    for f in "${want[@]}"; do
        [ -f "$PROJECT_DIR/$f" ] || { echo "ERROR: $f not found in $PROJECT_DIR (build it, or pass --update-only)"; exit 1; }
        ASSETS+=("$PROJECT_DIR/$f")
    done
fi
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

# ── verify: every asset present on GitHub with the same size; re-upload what is missing ──
for attempt in 1 2 3 4; do
    missing=()
    online="$(gh release view "$TAG" --repo "$REPO" --json assets --jq '.assets[] | "\(.name) \(.size)"')"
    for f in "${ASSETS[@]}"; do
        grep -qx "$(basename "$f") $(stat -c %s "$f")" <<<"$online" || missing+=("$f")
    done
    [ ${#missing[@]} -eq 0 ] && break
    echo "Missing or incomplete on GitHub (attempt $attempt): ${missing[*]##*/}"
    [ $attempt -eq 4 ] && { echo "ERROR: upload incomplete; run this script again"; exit 1; }
    gh release upload "$TAG" --repo "$REPO" --clobber "${missing[@]}" || sleep 20
done
echo "All ${#ASSETS[@]} assets verified on GitHub."
echo "Published: https://github.com/$REPO/releases/tag/$TAG"
