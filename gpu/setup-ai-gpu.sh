#!/usr/bin/env bash
# gpu/setup-ai-gpu.sh
# Install AI/compute GPU drivers for AMD (ROCm) and Intel (Level Zero / OpenCL).
# NVIDIA: install nvidia-container-toolkit separately; this script skips it.
#
# Usage: sudo bash setup-ai-gpu.sh [--amd] [--intel] [--all]
# With no flags, auto-detects which GPUs are present and installs for those.

set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Must run as root"; exit 1; }

DO_AMD=false
DO_INTEL=false

# ── Argument handling ────────────────────────────────────────────────────────
if [[ $# -eq 0 ]]; then
    # Auto-detect
    if find /sys/class/drm/*/device -name vendor -exec grep -lq 0x1002 {} \; 2>/dev/null | head -1 | grep -q .; then
        DO_AMD=true; echo "==> AMD GPU detected"
    fi
    if find /sys/class/drm/*/device -name vendor -exec grep -lq 0x8086 {} \; 2>/dev/null | head -1 | grep -q .; then
        DO_INTEL=true; echo "==> Intel GPU detected"
    fi
    if ! $DO_AMD && ! $DO_INTEL; then
        echo "No AMD or Intel GPU detected. Exiting."
        echo "  (For NVIDIA, install nvidia-container-toolkit from https://docs.nvidia.com/datacenter/cloud-native/)"
        exit 0
    fi
else
    for arg in "$@"; do
        case "$arg" in
            --amd)   DO_AMD=true ;;
            --intel) DO_INTEL=true ;;
            --all)   DO_AMD=true; DO_INTEL=true ;;
            *) echo "Unknown flag: $arg"; exit 1 ;;
        esac
    done
fi

DISTRO_ID=$(. /etc/os-release && echo "$ID")
DISTRO_VER=$(. /etc/os-release && echo "$VERSION_ID")
ARCH=$(dpkg --print-architecture 2>/dev/null || uname -m)

apt_install() {
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@"
}

# ── AMD ROCm ─────────────────────────────────────────────────────────────────
install_rocm() {
    echo ""
    echo "══════════════════════════════════════════"
    echo " Installing AMD ROCm (AI/compute stack)"
    echo "══════════════════════════════════════════"

    # ROCm repo key + sources
    apt-get update -qq
    apt_install wget gnupg2 lsb-release

    wget -qO /tmp/rocm.gpg \
        "https://repo.radeon.com/rocm/rocm.gpg.key"
    gpg --dearmor < /tmp/rocm.gpg > /usr/share/keyrings/rocm-archelix-keyring.gpg

    # Determine codename
    CODENAME=$(lsb_release -cs 2>/dev/null || echo "jammy")
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/rocm-archelix-keyring.gpg] \
https://repo.radeon.com/rocm/apt/6.2 ${CODENAME} main" \
        > /etc/apt/sources.list.d/rocm.list

    echo "Package: *" > /etc/apt/preferences.d/rocm-pin
    echo "Pin: release o=repo.radeon.com" >> /etc/apt/preferences.d/rocm-pin
    echo "Pin-Priority: 600" >> /etc/apt/preferences.d/rocm-pin

    apt-get update -qq

    # Core ROCm user-space (no dkms needed if amdgpu is in-kernel, which it is on 6.x)
    apt_install rocm-hip-runtime rocm-opencl-runtime rocminfo rocm-smi-lib

    # Add current user (and pi if exists) to render + video groups
    for u in pi "${SUDO_USER:-}"; do
        [[ -z "$u" ]] && continue
        id "$u" &>/dev/null && usermod -aG render,video "$u" && echo "    Added $u to render, video groups"
    done

    # Verify /dev/kfd exists (needed for HIP/ROCm containers)
    if [[ ! -e /dev/kfd ]]; then
        echo "    WARNING: /dev/kfd not found — amdgpu kernel module may not be loaded."
        echo "    Try: modprobe amdgpu && echo amdgpu >> /etc/modules"
    else
        echo "    /dev/kfd present ✓"
    fi

    echo ""
    echo "    ROCm installed. Docker GPU flags for AMD:"
    echo "      --device=/dev/kfd --device=/dev/dri/renderD128"
    echo "      --group-add render --group-add video"
    echo ""
    echo "    Example AI container:"
    echo "      docker run --device=/dev/kfd --device=/dev/dri/renderD128 \\"
    echo "        --group-add render --group-add video \\"
    echo "        rocm/pytorch:latest"
}

# ── Intel GPU Compute (Level Zero + OpenCL) ──────────────────────────────────
install_intel_gpu() {
    echo ""
    echo "══════════════════════════════════════════"
    echo " Installing Intel GPU compute drivers"
    echo "══════════════════════════════════════════"

    apt-get update -qq
    apt_install gpg-agent wget

    # Intel GPU compute repo
    wget -qO /tmp/intel-gpu.gpg \
        "https://repositories.intel.com/gpu/intel-graphics.key"
    gpg --dearmor < /tmp/intel-gpu.gpg \
        > /usr/share/keyrings/intel-graphics.gpg

    CODENAME=$(lsb_release -cs 2>/dev/null || echo "jammy")
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/intel-graphics.gpg] \
https://repositories.intel.com/gpu/ubuntu ${CODENAME} unified" \
        > /etc/apt/sources.list.d/intel-gpu.list

    apt-get update -qq

    # Level Zero (inference / IPEX / OpenVINO runtime)
    apt_install intel-opencl-icd intel-level-zero-gpu level-zero \
                intel-media-va-driver-non-free libmfx1 libmfxgen1 libvpl2 \
                libegl-mesa0 libegl1-mesa-dev libgbm1 libgl1-mesa-dri

    # Add user to render group
    for u in pi "${SUDO_USER:-}"; do
        [[ -z "$u" ]] && continue
        id "$u" &>/dev/null && usermod -aG render "$u" && echo "    Added $u to render group"
    done

    # Verify render node
    if ls /dev/dri/renderD* &>/dev/null; then
        echo "    Render nodes: $(ls /dev/dri/renderD* | tr '\n' ' ') ✓"
    else
        echo "    WARNING: no /dev/dri/renderD* nodes found"
    fi

    echo ""
    echo "    Intel GPU compute installed. Docker flags:"
    echo "      --device=/dev/dri/renderD128 --group-add render"
    echo ""
    echo "    Example AI containers:"
    echo "      OpenVINO:  docker run --device=/dev/dri/renderD128 \\"
    echo "                   --group-add render \\"
    echo "                   openvino/ubuntu22_dev:latest"
    echo "      PyTorch:   docker run --device=/dev/dri/renderD128 \\"
    echo "                   --group-add render \\"
    echo "                   intel/intel-extension-for-pytorch:latest"
}

# ── Run selected installs ─────────────────────────────────────────────────────
$DO_AMD   && install_rocm
$DO_INTEL && install_intel_gpu

echo ""
echo "════════════════════════════════════════════════"
echo " GPU AI/compute setup complete!"
echo ""
echo " Quick reference — Docker device flags:"
if $DO_AMD; then
    echo "   AMD ROCm:   --device=/dev/kfd --device=/dev/dri/renderD128"
    echo "               --group-add render --group-add video"
fi
if $DO_INTEL; then
    echo "   Intel:      --device=/dev/dri/renderD128 --group-add render"
fi
echo "   NVIDIA:     --gpus all  (via nvidia-container-toolkit)"
echo "════════════════════════════════════════════════"
