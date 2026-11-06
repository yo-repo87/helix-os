import os

HELIX_CONFIG  = os.getenv("HELIX_CONFIG", "/boot/config")
ARRAY_SBIN    = os.getenv("ARRAY_SBIN",   "/usr/local/sbin")
STATE_DIR     = os.getenv("STATE_DIR",    "/var/local/helix")
MNT_BASE      = os.getenv("MNT_BASE",     "/mnt")
USER_SHARE    = os.getenv("USER_SHARE",   "/mnt/user")
DOCKER_SOCKET = os.getenv("DOCKER_SOCKET","/var/run/docker.sock")
LOG_PATH      = os.getenv("HELIX_LOG",    "/var/log/helix-array.log")

DISKS_CFG   = f"{HELIX_CONFIG}/disks.cfg"
SHARES_CFG  = f"{HELIX_CONFIG}/shares.cfg"
NETWORK_CFG = f"{HELIX_CONFIG}/network.cfg"
ARRAY_STATE = f"{STATE_DIR}/array_state"
