#!/bin/sh
# RK3576 policy measured on this gimbal: keep an 800MHz ceiling while active.
# Save and restore the driver policy; do not change voltages or thermal limits.
set -eu
node=/sys/class/devfreq/27700000.npu
saved=/run/hust_tracker-npu-policy
restore_policy() {
    [ -d "$saved" ] || return 0
    # Raise the ceiling before restoring the original floor, if necessary.
    cat "$saved/max_freq" > "$node/max_freq"
    cat "$saved/min_freq" > "$node/min_freq"
    cat "$saved/governor" > "$node/governor"
    rm -rf "$saved"
}
case "${1:-}" in
    apply)
        [ -d "$node" ] || exit 1
        if [ ! -d "$saved" ]; then
            mkdir -m 700 "$saved"
            for key in governor min_freq max_freq; do cat "$node/$key" > "$saved/$key"; done
        fi
        trap 'restore_policy' EXIT
        original_max=$(cat "$saved/max_freq")
        ceiling=800000000
        [ "$original_max" -ge "$ceiling" ] || ceiling=$original_max
        # Do not exceed an existing administrator limit.
        floor=$(cat "$node/min_freq")
        [ "$floor" -le "$ceiling" ] || echo "$ceiling" > "$node/min_freq"
        echo "$ceiling" > "$node/max_freq"
        echo performance > "$node/governor"
        trap - EXIT
        echo "hust_tracker NPU: governor=$(cat "$node/governor") max=$(cat "$node/max_freq")"
        ;;
    restore) restore_policy ;;
    *) echo "Usage: $0 {apply|restore}" >&2; exit 2 ;;
esac
