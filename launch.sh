#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Helix OS — session launcher / quick resume
#
# Running ./launch.sh resumes the most recent Claude Code conversation for this
# project (falling back to a fresh session if there is nothing to continue).
#
#   Project : Helix OS — open-source Unraid-style NAS platform
#   Repo    : github.com/yo-repo87/helix-os   (PRIVATE; PAT in ./gitkey, gitignored)
#   Remotes : helix-os -> this repo   |   origin -> template (unused)
#
# ── Session state (last updated 2026-11-06) ─────────────────────────────────
#   • Project code pushed to the private repo (branch: main).
#   • Flashable images built and in this folder:
#       helix-os-20261106.iso        x86_64, UEFI+BIOS hybrid ISO
#       helix-os-pi-20261106.img.xz  arm64, Raspberry Pi 4/5
#     Also published as GitHub Release `images-2026.11.06`.
#   • Over-the-air GitHub self-update feature on branch `helix-osupdate`
#     (PR #1, mergeable). OTA update bundle published as Release `v0.1.0`
#     (amd64 + arm64 + helix-release.json manifest).
#   • On-disk build area: /tmp/helix-build -> /home/pi/helix-work/build
#     (symlinked off tmpfs to avoid OOM on this RAM-tight host).
#
# ── Common commands ──────────────────────────────────────────────────────────
#   sudo build/build-all.sh                                  # build both images
#   (cd webui && sudo ./install.sh)                          # install web UI service
#   webui/.venv/bin/uvicorn main:app --app-dir webui/backend --port 8082   # dev run
#   build/make-update-bundle.sh --arch amd64 --from output-amd64/boot --version X.Y.Z
#   build/make-update-bundle.sh --arch arm64 --from output-arm64/boot --version X.Y.Z
#   GH_TOKEN=$(cat gitkey) build/publish-update.sh           # publish update release vX.Y.Z
#   GH_TOKEN=$(cat gitkey) gh ...                            # any GitHub CLI op
#
# ── Known caveats ────────────────────────────────────────────────────────────
#   • Images verified structurally (arch, modules, bootloader) but not yet
#     boot-tested on hardware/VM.
#   • OTA update flow validated live (check/download/verify/swap/rollback) except
#     the physical reboot-into-new-image step, which needs a running Helix box.
#   • A private repo requires a read-only token in Settings → Helix OS Update
#     (saved to /boot/config/osupdate.cfg) for the running OS to fetch updates.
#
# Full details: see CLAUDE.md (§16 OTA updates, §17 project state & resume).
# ─────────────────────────────────────────────────────────────────────────────
cd "$(dirname "$0")"

echo "Helix OS — resuming Claude Code session (repo: yo-repo87/helix-os)…"
claude --continue --dangerously-skip-permissions "$@" \
  || claude --dangerously-skip-permissions "$@"
