#!/bin/bash
set -euo pipefail
bundle_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# Only installation may install system packages. doctor/configure are read-only.
action="${1:-install}"
if [[ "$action" == --help || "$action" == -h ]]; then
    exec /usr/bin/python3 "$bundle_dir/install.py" "$@"
fi
if [[ "$action" == install || "$action" == --* ]]; then
    if [[ "$EUID" != 0 ]]; then
        exec sudo bash "$bundle_dir/install.sh" "$@"
    fi
    /usr/bin/python3 - "$bundle_dir" <<'PY'
import importlib.util, sys
from pathlib import Path
p=Path(sys.argv[1])/'install.py'
s=importlib.util.spec_from_file_location('installer',p);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
m.verify_manifest();m.platform_check()
# Refuse takeover before changing packages on an existing non-matching deployment.
if not (m.BASE/m.RECEIPT).is_file():m.assert_fresh()
PY
    # Do not upgrade the BSP/kernel. Install missing user-space packages only.
    missing=()
    for package in python3 ffmpeg socat iproute2 iputils-ping iputils-arping psmisc v4l-utils gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad libgstrtspserver-1.0-0 libatomic1; do
        if ! dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -qx 'install ok installed'; then
            missing+=("$package")
        fi
    done
    if ((${#missing[@]})); then
        apt-get update
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"
    fi
fi
exec /usr/bin/python3 "$bundle_dir/install.py" "$@"
