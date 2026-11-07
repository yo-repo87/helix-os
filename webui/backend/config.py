import os
import shutil

ARRAY_SBIN    = os.getenv("ARRAY_SBIN",   "/usr/local/sbin")
STATE_DIR     = os.getenv("STATE_DIR",    "/var/local/helix")
MNT_BASE      = os.getenv("MNT_BASE",     "/mnt")
USER_SHARE    = os.getenv("USER_SHARE",   "/mnt/user")
DOCKER_SOCKET = os.getenv("DOCKER_SOCKET", "/var/run/docker.sock")
LOG_PATH      = os.getenv("HELIX_LOG",    "/var/log/helix-array.log")


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


# Prefer the persistent boot config (/boot/config). When the boot medium is
# read-only — e.g. the ISO booted as a CD-ROM, where the initramfs bind-mounts a
# read-only /boot — /boot can't be written, so fall back to a writable in-RAM
# directory seeded from it. Settings then work for the running session but do not
# persist across reboots; boot Helix from a writable USB/disk to persist them.
_PREF = os.getenv("HELIX_CONFIG", "/boot/config")
if _is_writable(_PREF):
    HELIX_CONFIG = _PREF
    HELIX_CONFIG_PERSISTENT = True
else:
    HELIX_CONFIG = os.path.join(STATE_DIR, "config")
    HELIX_CONFIG_PERSISTENT = False
    try:
        os.makedirs(HELIX_CONFIG, exist_ok=True)
        if os.path.isdir(_PREF):
            for fn in os.listdir(_PREF):
                src = os.path.join(_PREF, fn)
                dst = os.path.join(HELIX_CONFIG, fn)
                if os.path.isfile(src) and not os.path.exists(dst):
                    try:
                        shutil.copy2(src, dst)
                    except Exception:
                        pass
    except Exception:
        HELIX_CONFIG = _PREF  # last resort; writes will surface clear errors

DISKS_CFG   = f"{HELIX_CONFIG}/disks.cfg"
SHARES_CFG  = f"{HELIX_CONFIG}/shares.cfg"
NETWORK_CFG = f"{HELIX_CONFIG}/network.cfg"
ARRAY_STATE = f"{STATE_DIR}/array_state"
