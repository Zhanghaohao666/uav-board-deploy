#!/bin/bash
set -euo pipefail
algorithm_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ "${1:-}" != --help && "${1:-}" != -h && "$EUID" != 0 ]]; then
    exec sudo bash "$algorithm_dir/install.sh" "$@"
fi
exec /usr/bin/python3 "$algorithm_dir/install.py" "$@"
