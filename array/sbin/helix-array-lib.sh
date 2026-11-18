#!/bin/bash
# Helix OS Array Library — sourced by array management scripts.
# Not run directly.

HELIX_CONFIG="${HELIX_CONFIG:-/boot/config}"
DISKS_CFG="$HELIX_CONFIG/disks.cfg"
SHARES_CFG="$HELIX_CONFIG/shares.cfg"
SNAPRAID_CONF="/etc/snapraid.conf"
# The backend passes these. A shell run with none set keeps the image defaults.
MNTBASE="${MNT_BASE:-/mnt}"
USER_SHARE="${USER_SHARE:-$MNTBASE/user}"
STATE_DIR="${STATE_DIR:-/var/local/helix}"
ARRAY_STATE="$STATE_DIR/array_state"   # "started" | "stopped"

# ── Parse a simple INI file ───────────────────────────────────────────────────
# cfg_get FILE SECTION KEY → prints value
cfg_get() {
    local file="$1" section="$2" key="$3"
    awk -F= -v sec="[$section]" -v k="$key" '
        /^\[/        { in_sec = ($0 == sec) }
        in_sec && $1 ~ "^[[:space:]]*"k"[[:space:]]*$" {
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", $2); print $2; exit
        }
    ' "$file"
}

# cfg_sections FILE → prints all section names (without brackets)
cfg_sections() {
    grep -oP '(?<=^\[)[^\]]+' "$1" 2>/dev/null
}

# ── Where a disk's file system lives ─────────────────────────────────────────
# Helix's own Format puts the file system on the whole disk, but a disk prepared by
# Unraid, TrueNAS or any partitioning tool has it on a partition (sdb1). disks.cfg
# stores the whole disk, so every mount/status check goes through these.
#
# fs_device DEV → the device that holds the file system: DEV itself, else its only
#                 partition that has one. Prints DEV when neither has one.
fs_device() {
    local dev; dev=$(readlink -f "$1")
    if [ -n "$(blkid -s TYPE -o value "$dev" 2>/dev/null)" ]; then echo "$dev"; return; fi
    local parts; parts=$(lsblk -nrpo NAME,TYPE,FSTYPE "$dev" 2>/dev/null | awk '$2=="part" && $3!="" {print $1}')
    if [ "$(printf '%s\n' "$parts" | grep -c .)" = "1" ]; then echo "$parts"; else echo "$dev"; fi
}

# fs_type DEV → the file system type found by fs_device ("" if none).
fs_type() { blkid -s TYPE -o value "$(fs_device "$1")" 2>/dev/null || true; }

# disk_in_use DEV → true when the disk has a partition table, any partition, or a file
# system signature anywhere: Format must never run on it (mkfs on the whole disk would
# destroy partitions and their data). Erase on the disk page clears it first.
disk_in_use() {
    local dev; dev=$(readlink -f "$1")
    [ -n "$(blkid -o value -s TYPE "$dev" 2>/dev/null)$(blkid -o value -s PTTYPE "$dev" 2>/dev/null)" ] && return 0
    [ -n "$(lsblk -nrpo NAME,TYPE "$dev" 2>/dev/null | awk '$2=="part"')" ] && return 0
    return 1
}

# system_disk SUBDIR → the mounted disk whose system/SUBDIR (docker, libvirt) already holds
# data, so containers and VMs are found again after disks are reassigned or reordered.
# Without existing data: the first mounted cache disk, else the first mounted data disk.
# Needs load_disk_config. Prints nothing when no disk is mounted.
system_disk() {
    local sub="$1" m first=""
    for m in "${CACHE_MNTS[@]:-}" "${DATA_MNTS[@]:-}"; do
        [ -n "$m" ] && mountpoint -q "$m" || continue
        [ -n "$first" ] || first="$m"
        if [ -n "$(ls -A "$m/system/$sub" 2>/dev/null | head -1)" ]; then
            # docker: only containers count. Docker builds an empty skeleton (image/overlay2, network,
            # ...) the first time it starts on a disk, which would otherwise win over the real data.
            if [ "$sub" != docker ] || [ -n "$(ls -A "$m/system/docker/containers" 2>/dev/null | head -1)" ]; then
                echo "$m"; return
            fi
        fi
    done
    echo "$first"
}

# zfs_import_recorded → import the pools recorded on the ZFS page ($HELIX_CONFIG/zfs/pools.json),
# each with its altroot, and mount them. Used by array start and by helix-zfs-ensure (which
# installs ZFS at boot when the image lacks it, after array start already skipped the pools).
zfs_import_recorded() {
    local pools_file="$HELIX_CONFIG/zfs/pools.json" pool altroot ropt
    command -v zpool &>/dev/null && [ -s "$pools_file" ] || return 0
    [ -e /dev/zfs ] || modprobe zfs 2>/dev/null || { helix_log "WARN: ZFS kernel module did not load"; return 0; }
    # One "<pool> <altroot>" line per pool (altroot /mnt for TrueNAS-style pools, "-" for none).
    # -f is needed every boot: the root is in RAM, so the hostid differs from the last import.
    while read -r pool altroot; do
        [ -n "$pool" ] || continue
        zpool list -H -o name "$pool" &>/dev/null && continue
        ropt=(); [ "$altroot" != "-" ] && ropt=(-R "$altroot")
        if zpool import -f -d /dev/disk/by-id "${ropt[@]}" "$pool" 2>/dev/null; then
            helix_log "Imported ZFS pool: $pool"
        else
            helix_log "WARN: Could not import ZFS pool $pool"
        fi
    done < <(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
if isinstance(d, list): d = {n: {} for n in d}
for n, v in d.items(): print(n, (v or {}).get("altroot") or "-")' "$pools_file" 2>/dev/null)
    zfs mount -a 2>/dev/null || true
}

# start_user_share BRANCHES → mount the mergerfs user share at $USER_SHARE.
# mergerfs is a FUSE daemon. Started directly it would live in the cgroup of whoever ran
# array start, which is usually helix-backend.service (KillMode=control-group): every backend
# restart (OTA, crash, code push) then killed it and /mnt/user silently became an empty
# folder in the RAM root. A transient systemd scope gives it a life of its own.
start_user_share() {
    local branches="$1"
    local opts=(-o defaults,allow_other,use_ino,cache.files=partial
                -o dropcacheonclose=true,category.create=mfs
                -o minfreespace=4G,fsname=helix-user)
    mkdir -p "$USER_SHARE"
    if command -v systemd-run >/dev/null && [ -d /run/systemd/system ]; then
        systemd-run --scope --quiet --collect --unit="helix-user-share-$(date +%s)" \
            -p Description="Helix OS user share (mergerfs $USER_SHARE)" \
            mergerfs "${opts[@]}" "$branches" "$USER_SHARE"
    else
        mergerfs "${opts[@]}" "$branches" "$USER_SHARE"
    fi
    mountpoint -q "$USER_SHARE"
}

# ── Discover data disks from disks.cfg ───────────────────────────────────────
# Sets globals:
#   PARITY_DEVS   — array of parity device paths
#   DATA_DISKS    — array of disk section names (disk1, disk2, ...)
#   DATA_DEVS     — matching device paths
#   DATA_MNTS     — matching mount points (/mnt/disk1, /mnt/disk2, ...)
load_disk_config() {
    PARITY_DEVS=()
    DATA_DISKS=()
    DATA_DEVS=()
    DATA_MNTS=()
    CACHE_DISKS=()
    CACHE_DEVS=()
    CACHE_MNTS=()

    while IFS= read -r section; do
        [[ "$section" =~ ^parity ]] && {
            dev=$(cfg_get "$DISKS_CFG" "$section" "device")
            [ -n "$dev" ] && PARITY_DEVS+=("$dev")
            continue
        }
        [[ "$section" =~ ^cache ]] && {
            dev=$(cfg_get "$DISKS_CFG" "$section" "device")
            [ -n "$dev" ] || continue
            CACHE_DISKS+=("$section")
            CACHE_DEVS+=("$dev")
            CACHE_MNTS+=("$MNTBASE/$section")
            continue
        }
        [[ "$section" =~ ^disk ]] && {
            dev=$(cfg_get "$DISKS_CFG" "$section" "device")
            [ -n "$dev" ] || continue
            DATA_DISKS+=("$section")
            DATA_DEVS+=("$dev")
            DATA_MNTS+=("$MNTBASE/$section")
            continue
        }
    done < <(cfg_sections "$DISKS_CFG")
}

# ── Generate /etc/snapraid.conf from disks.cfg ───────────────────────────────
generate_snapraid_conf() {
    load_disk_config

    {
        echo "# Auto-generated by helix-array — do not edit manually."
        echo "# Source: $DISKS_CFG"
        echo ""

        # Parity files (one per parity drive)
        local idx=1
        for dev in "${PARITY_DEVS[@]}"; do
            mnt="$MNTBASE/parity$idx"
            if [ $idx -eq 1 ]; then
                echo "parity $mnt/snapraid.parity"
            else
                echo "$([ $idx -eq 2 ] && echo 'q' || echo "${idx}")parity $mnt/snapraid.parity"
            fi
            idx=$((idx+1))
        done
        echo ""

        # Content file (redundant copies — one per data disk + one local)
        echo "content /var/snapraid/content"
        for mnt in "${DATA_MNTS[@]}"; do
            echo "content $mnt/.snapraid.content"
        done
        echo ""

        # Data drives
        for i in "${!DATA_DISKS[@]}"; do
            echo "data ${DATA_DISKS[$i]} ${DATA_MNTS[$i]}/"
        done
        echo ""

        # Exclude SnapRAID's own content files from sync
        echo "exclude /.snapraid.content"
        echo "exclude /tmp/"
        echo "exclude /lost+found/"

    } > "$SNAPRAID_CONF"
}

# ── Logging ───────────────────────────────────────────────────────────────────
helix_log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [helix-array] $*" | tee -a /var/log/helix-array.log; }
helix_err() { helix_log "ERROR: $*" >&2; }

# ── Ensure directories exist ─────────────────────────────────────────────────
ensure_dirs() {
    mkdir -p "$STATE_DIR" /var/snapraid /var/log
    for mnt in "${DATA_MNTS[@]}" "${CACHE_MNTS[@]}" "${MNTBASE}/parity1" "${MNTBASE}/parity2" "$USER_SHARE"; do
        mkdir -p "$mnt"
    done
}
