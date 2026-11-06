#!/bin/bash
# make-update-bundle.sh — package a built OS image as a GitHub update bundle.
#
# Takes the three boot-image files for one architecture and stages them under
# dist/update/ with arch-prefixed names, then merges their checksums into a
# single dist/update/helix-release.json manifest that the in-OS updater reads.
#
# Run once per architecture (amd64, arm64), then run publish-update.sh.
#
# Usage:
#   sudo ./make-update-bundle.sh --arch amd64 --from ../output-amd64/boot
#   sudo ./make-update-bundle.sh --arch arm64 --from ../output-arm64/boot
#
# Options:
#   --arch <amd64|arm64>   (required)
#   --from <dir>           dir containing filesystem.squashfs, vmlinuz, initrd.img
#                          (default: ../output/boot)
#   --version <v>          (default: contents of build/config/VERSION)
#   --notes <text>         release notes (optional)
#   --channel <stable|beta> (default: stable)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR/.."
DIST="$PROJECT_DIR/dist/update"
FILES=(filesystem.squashfs vmlinuz initrd.img)

ARCH=""; FROM="$PROJECT_DIR/output/boot"; NOTES=""; CHANNEL="stable"
VERSION="$(tr -d '[:space:]' < "$SCRIPT_DIR/config/VERSION" 2>/dev/null || echo 0.0.0)"

while [[ $# -gt 0 ]]; do
    case $1 in
        --arch)    ARCH="$2"; shift 2 ;;
        --from)    FROM="$2"; shift 2 ;;
        --version) VERSION="$2"; shift 2 ;;
        --notes)   NOTES="$2"; shift 2 ;;
        --channel) CHANNEL="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

[[ "$ARCH" == amd64 || "$ARCH" == arm64 ]] || { echo "ERROR: --arch must be amd64 or arm64"; exit 1; }
for f in "${FILES[@]}"; do
    [ -f "$FROM/$f" ] || { echo "ERROR: $FROM/$f not found (build the $ARCH image first)"; exit 1; }
done

mkdir -p "$DIST"
echo "Packaging $ARCH update bundle v$VERSION from $FROM"

for f in "${FILES[@]}"; do
    cp -f "$FROM/$f" "$DIST/${ARCH}-${f}"
    echo "  + ${ARCH}-${f}"
done

# Merge this arch into the shared manifest (python3 keeps it dependency-free).
MANIFEST="$DIST/helix-release.json" \
ARCH="$ARCH" VERSION="$VERSION" NOTES="$NOTES" CHANNEL="$CHANNEL" DIST="$DIST" \
python3 - "${FILES[@]}" <<'PY'
import hashlib, json, os, sys
dist = os.environ["DIST"]; arch = os.environ["ARCH"]
manifest_path = os.environ["MANIFEST"]
files = sys.argv[1:]
try:
    m = json.load(open(manifest_path))
except (FileNotFoundError, json.JSONDecodeError):
    m = {}
m["version"] = os.environ["VERSION"]
m["channel"] = os.environ["CHANNEL"]
if os.environ.get("NOTES"):
    m["notes"] = os.environ["NOTES"]
m.setdefault("arch", {})
entry = {}
for f in files:
    p = os.path.join(dist, f"{arch}-{f}")
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    entry[f] = {"asset": f"{arch}-{f}", "sha256": h.hexdigest(), "size": os.path.getsize(p)}
m["arch"][arch] = entry
json.dump(m, open(manifest_path, "w"), indent=2)
print(f"  manifest updated: {manifest_path} (arch={arch}, version={m['version']})")
PY

echo "Done. Bundle staged in $DIST"
echo "Next: ./publish-update.sh   (uploads dist/update/* to a GitHub release)"
