#!/bin/sh
# Run from the backup directory; restore files and original startup state.
set -eu
BACKUP=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=${UAV_GIMBAL_TEST_ROOT:-}
[ -f "$BACKUP/before.tar" ] && [ -f "$BACKUP/managed.list" ] || exit 1
if [ -z "$ROOT" ]; then
    [ "$(id -u)" = 0 ] || exit 1
    if [ -f /etc/cfg/uav-gimbal-installation ]; then /home/start_tracker.sh stop || true; fi
else
    [ "${UAV_GIMBAL_TESTING:-}" = 1 ] || exit 1
    case "$ROOT" in /tmp/*) ;; *) exit 1 ;; esac
fi
for p in $(cat "$BACKUP/managed.list"); do rm -f "$ROOT/$p";done
tar -xf "$BACKUP/before.tar" -C "${ROOT:-/}"
release=$(cat "$BACKUP/release")
current="$ROOT/data/uav-gimbal/current"
if [ -L "$current" ] && [ "$(readlink "$current")" = "$release" ]; then rm "$current";fi
if [ "$(cat "$BACKUP/model-created")" = 1 ] && [ -L "$ROOT/model" ] && [ "$(readlink "$ROOT/model")" = "$release/model" ]; then rm "$ROOT/model";fi
case "$release" in "$ROOT"/data/uav-gimbal/releases/*)
    if [ -d "$release" ] && [ ! -L "$release" ] && [ ! -e "$BACKUP/restored-release" ]; then
        mv "$release" "$BACKUP/restored-release"
    fi ;;
    *) echo 'Unexpected runtime path; retained for manual inspection.' >&2 ;;
esac
rm -f "$ROOT/etc/cfg/uav-gimbal-installation"
echo 'Original application/startup files restored; no application restarted automatically.'
