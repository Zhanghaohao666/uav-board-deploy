#!/bin/sh
# Append-mode application log; retain a tail before truncating the active file.
set -eu
pid=${1:?tracker PID required}
case "$pid" in *[!0-9]*|'') exit 2 ;; esac
log=/data/uav-gimbal/logs/tracker.log
while kill -0 "$pid" 2>/dev/null; do
    target=$(readlink "/proc/$pid/exe" 2>/dev/null || true)
    case "$target" in /home/hust_tracker_3576|'/home/hust_tracker_3576 (deleted)') ;; *) exit 0 ;; esac
    if [ -f "$log" ] && [ "$(wc -c < "$log")" -gt 16777216 ]; then
        tail -c 1048576 "$log" > "$log.previous"
        : > "$log"
    fi
    sleep 60
done
