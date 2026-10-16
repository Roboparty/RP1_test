#!/usr/bin/env bash
# Required only to build HMI from source; released .deb installs its runtime dependencies.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
. /etc/os-release
[[ $ID == ubuntu && $VERSION_ID == 24.04 && $(dpkg --print-architecture) == amd64 ]] || exit 1
apt-get update
apt-get install -y python3-venv python3-dev build-essential cmake ninja-build ccache \
  libboost-system-dev libspdlog-dev libfmt-dev libeigen3-dev pybind11-dev \
  libxcb-cursor0 libxcb-icccm4 libxcb-keysyms1 libxcb-shape0 libxcb-xinerama0 \
  libxkbcommon-x11-0 libegl1 libgl1 libfontconfig1 libdbus-1-3 fonts-noto-cjk \
  patchelf binutils dpkg-dev
