#!/bin/bash
# Complete application deployment after flashing the matching vendor BSP.
set -euo pipefail
bundle_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
    exec /usr/bin/python3 "$bundle_dir/deploy.py" "$@"
fi
if [[ "$EUID" != 0 ]]; then
    exec sudo --preserve-env=SSH_CONNECTION bash "$bundle_dir/deploy.sh" "$@"
fi
exec /usr/bin/python3 "$bundle_dir/deploy.py" "$@"
