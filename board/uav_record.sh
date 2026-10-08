#!/bin/bash
set -euo pipefail
if [ "$(id -u)" -eq 0 ]; then
    exec /usr/bin/python3 /opt/uav-switch/record.py "$@"
else
    exec sudo /usr/bin/python3 /opt/uav-switch/record.py "$@"
fi
