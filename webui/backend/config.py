import os
import shutil

ARRAY_SBIN    = os.getenv("ARRAY_SBIN",   "/usr/local/sbin")
STATE_DIR     = os.getenv("STATE_DIR",    "/var/local/helix")
MNT_BASE      = os.getenv("MNT_BASE",     "/mnt")
USER_SHARE    = os.getenv("USER_SHARE",   "/mnt/user")
DOCKER_SOCKET = os.getenv("DOCKER_SOCKET", "/var/run/docker.sock")
LOG_PATH      = os.getenv("HELIX_LOG",    "/var/log/helix-array.log")

# Persistent ext4 data partition (HELIXDATA) mounted by helix-firstboot.
DATA_MNT      = os.getenv("HELIX_DATA",   "/mnt/data")


def _is_writable(d: str) -> bool:
    try:
        os.makedirs(d, exist_ok=True)
        t = os.path.join(d, ".helix-wtest")
        with open(t, "w"):
            pass
        os.remove(t)
        return True
    except Exception:
        return False


# Choose the config location (first usable wins):
#   1. $HELIX_CONFIG            — explicit override (dev/tests)
#   2. $DATA_MNT/config         — persistent ext4 data partition, when mounted
#   3. /boot/config            — the boot medium, when writable (USB/disk boot)
#   4. $STATE_DIR/config       — in-RAM fallback for a read-only boot (ISO/CD)
# The RAM fallback is seeded from /boot/config so existing settings carry over.
HELIX_CONFIG_PERSISTENT = True
_env = os.getenv("HELIX_CONFIG")
if _env:
    HELIX_CONFIG = _env
elif os.path.ismount(DATA_MNT) and _is_writable(os.path.join(DATA_MNT, "config")):
    HELIX_CONFIG = os.path.join(DATA_MNT, "config")
elif _is_writable("/boot/config"):
    HELIX_CONFIG = "/boot/config"
else:
    HELIX_CONFIG = os.path.join(STATE_DIR, "config")
    HELIX_CONFIG_PERSISTENT = False
    try:
        os.makedirs(HELIX_CONFIG, exist_ok=True)
        if os.path.isdir("/boot/config"):
            for fn in os.listdir("/boot/config"):
                src = os.path.join("/boot/config", fn)
                dst = os.path.join(HELIX_CONFIG, fn)
                if os.path.isfile(src) and not os.path.exists(dst):
                    try:
                        shutil.copy2(src, dst)
                    except Exception:
                        pass
    except Exception:
        HELIX_CONFIG = "/boot/config"

DISKS_CFG   = f"{HELIX_CONFIG}/disks.cfg"
SHARES_CFG  = f"{HELIX_CONFIG}/shares.cfg"
NETWORK_CFG = f"{HELIX_CONFIG}/network.cfg"
ARRAY_STATE = f"{STATE_DIR}/array_state"
