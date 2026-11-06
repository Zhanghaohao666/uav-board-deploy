#!/bin/sh
# RK3576 Buildroot installer. Does not require Python, systemd or gzip.
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROFILE=auto
CHECK=0
START=1
BASE_ROOT=${UAV_GIMBAL_TEST_ROOT:-}
while [ "$#" -gt 0 ]; do
    case "$1" in
        --profile) PROFILE=${2:?profile required}; shift 2 ;;
        --check|--doctor) CHECK=1; shift ;;
        --no-start) START=0; shift ;;
        --help|-h) echo 'sh install.sh [--profile auto|old|new] [--check] [--no-start]'; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done
die() { echo "ERROR: $*" >&2; exit 1; }
case "$PROFILE" in auto|old|new) ;; *) die 'profile must be auto, old or new' ;; esac
if [ -n "$BASE_ROOT" ]; then
    [ "${UAV_GIMBAL_TESTING:-}" = 1 ] && [ "$START" = 0 ] || die 'Test root requires testing=1 and --no-start'
    case "$BASE_ROOT" in /tmp/*) ;; *) die 'Test root must be under /tmp' ;; esac
else
    [ "$(id -u)" = 0 ] || die 'Run as root on the gimbal'
    [ "$(uname -m)" = aarch64 ] || die 'Requires aarch64 RK3576'
    tr '\000' '\n' < /proc/device-tree/compatible | grep -qi rk3576 || die 'Requires matching RK3576 vendor BSP'
    [ -x /etc/init.d/rcS ] || die 'Requires vendor Buildroot init.d firmware'
    awk '$2=="/data" {ok=1} END {exit !ok}' /proc/mounts || die '/data must already be mounted; no formatting is performed'
    [ "$(df -Pk /data | awk 'NR==2 {print $4}')" -gt 524288 ] || die 'Less than 512 MiB free on /data'
fi
for command in sha256sum tar readlink awk sed cp mv; do command -v "$command" >/dev/null || die "Missing command: $command"; done
[ -s "$HERE/PAYLOAD.sha256" ] || die 'Download the complete role package, not only source code'
(cd "$HERE" && sha256sum -c PAYLOAD.sha256) >/dev/null || die 'Payload SHA256 validation failed'
if [ "$PROFILE" = auto ]; then
    sensor_names=$(cat "$BASE_ROOT"/sys/class/video4linux/v4l-subdev*/name 2>/dev/null || true)
    if echo "$sensor_names" | grep -q imx214; then PROFILE=new
    elif echo "$sensor_names" | grep -q imx290; then PROFILE=old
    else die 'No known visible camera found. Select the correct profile after checking vendor hardware.'; fi
fi
camera=2
source_fps=''
[ "$PROFILE" != new ] || { camera=0; source_fps=60; }
if [ -z "$BASE_ROOT" ]; then
    camname=$(cat /sys/class/video4linux/v4l-subdev*/name 2>/dev/null || true)
    case "$PROFILE" in new) expected_sensor=imx214 ;; old) expected_sensor=imx290 ;; esac
    echo "$camname" | grep -q "$expected_sensor" || die "Profile $PROFILE does not match visible sensor $expected_sensor"
    for command in flock setsid ip killall; do command -v "$command" >/dev/null || die "Missing command: $command"; done
    [ -d /home/iqfiles ] || die 'Preserve/install the manufacturer IQ files before application deployment'
    LD_LIBRARY_PATH="$HERE/runtime/lib:/home/lib:/data/libs/RK3576_ThirdLibs/lib:/data/libs/RK3576_Glibc200_Libs/lib" \
        /lib/ld-linux-aarch64.so.1 --list "$HERE/runtime/bin/$PROFILE/hust_tracker_3576" > /tmp/uav-gimbal-loader-check.txt 2>&1 || die 'Native loader/dependency check failed; see /tmp/uav-gimbal-loader-check.txt'
    grep -q 'not found' /tmp/uav-gimbal-loader-check.txt && die 'Unresolved dependency' || true
fi
model_created=0
if [ -e "$BASE_ROOT/model" ] || [ -L "$BASE_ROOT/model" ]; then
    for p in "$HERE"/runtime/model/*; do
        n=$(basename "$p")
        [ -f "$BASE_ROOT/model/$n" ] || die 'Existing /model differs; preserved without changes'
        [ "$(sha256sum "$p" | awk '{print $1}')" = "$(sha256sum "$BASE_ROOT/model/$n" | awk '{print $1}')" ] || die 'Existing /model differs; preserved without changes'
    done
else model_created=1; fi
echo "Profile=$PROFILE CamID=$camera; IP=192.168.144.108; peer=192.168.144.1"
echo 'Factory IQ, device certificates, SSH identity, serial number and existing calibration are preserved.'
if [ "$CHECK" = 1 ]; then echo 'Preflight passed; no application/network changes made.'; exit 0; fi
version=$(cat "$HERE/VERSION")
case "$version" in v[0-9]*.[0-9]*.[0-9]*) ;; *) die 'Invalid release version' ;; esac
digest=$(sha256sum "$HERE/PAYLOAD.sha256" | awk '{print $1}')
receipt="$BASE_ROOT/etc/cfg/uav-gimbal-installation"
release="$BASE_ROOT/data/uav-gimbal/releases/$version-$PROFILE"
if [ -f "$receipt" ]; then
    if [ "$(cat "$receipt")" = "$version $PROFILE $digest" ] && \
       [ -f "$BASE_ROOT/home/hust_tracker_3576" ] && \
       [ "$(sha256sum "$BASE_ROOT/home/hust_tracker_3576" | awk '{print $1}')" = "$(sha256sum "$HERE/runtime/bin/$PROFILE/hust_tracker_3576" | awk '{print $1}')" ]; then
        echo 'Same package/profile already installed; no service was restarted.'; exit 0
    fi
    die 'Existing managed installation differs; retained. Restore its backup or use a reviewed upgrade.'
fi
[ ! -e "$release" ] && [ ! -L "$release" ] || die 'Release target already exists'
current="$BASE_ROOT/data/uav-gimbal/current"
[ ! -e "$current" ] && [ ! -L "$current" ] || die 'Current runtime path exists without an installation receipt'
stamp=$(date +%Y%m%d_%H%M%S)
backup="$BASE_ROOT/data/uav-gimbal/backups/$stamp-$$"
mkdir -p "$backup" "$BASE_ROOT/etc/cfg" "$BASE_ROOT/usr/local/bin" "$BASE_ROOT/data/uav-gimbal/releases"
paths='home/hust_tracker_3576 home/start_tracker.sh home/switch_app.sh home/tracker_npu_policy.sh home/recap_servo.env home/uav_gimbal_log_guard.sh etc/init.d/S99hust_tracker etc/init.d/S98uav_gimbal_network etc/init.d/S99AppStart etc/init.d/_99AppStart etc/cfg/hust-app-mode etc/cfg/uav-gimbal.env usr/local/bin/uav-gimbal'
: > "$backup/present.list"
for p in $paths; do
    [ ! -L "$BASE_ROOT/$p" ] || die "Managed target is a symlink: $p"
    [ ! -d "$BASE_ROOT/$p" ] || die "Managed target is a directory: $p"
    if [ -e "$BASE_ROOT/$p" ]; then echo "$p" >> "$backup/present.list"; fi
done
(cd "${BASE_ROOT:-/}" && tar -cf "$backup/before.tar" -T "$backup/present.list")
cp "$HERE/restore.sh" "$backup/restore.sh"
printf '%s\n' "$paths" > "$backup/managed.list"
printf '%s\n' "$model_created" > "$backup/model-created"
printf '%s\n' "$release" > "$backup/release"
if [ -z "$BASE_ROOT" ]; then
    # Install is explicitly activating a replacement; stop known camera owners.
    for name in apps_daemon RkTrackerApp MagicUpdate rkaiq_3A_server; do killall -TERM "$name" 2>/dev/null || true; done
    for p in /proc/[0-9]*/exe; do
        target=$(readlink "$p" 2>/dev/null || true)
        case "$target" in /home/hust_tracker_3576|'/home/hust_tracker_3576 (deleted)') pid=${p#/proc/};kill -TERM "${pid%/exe}" 2>/dev/null || true ;; esac
    done
    sleep 3
    for name in apps_daemon RkTrackerApp MagicUpdate rkaiq_3A_server hust_tracker_3576; do killall -KILL "$name" 2>/dev/null || true; done
    if [ -x /home/tracker_npu_policy.sh ]; then /home/tracker_npu_policy.sh restore || true; fi
fi
rollback_on_error() {
    code=$?
    [ "$code" = 0 ] || sh "$backup/restore.sh" --no-start || echo "Recovery required: $backup" >&2
    exit "$code"
}
trap rollback_on_error EXIT
mkdir "$release"
cp -R "$HERE/runtime/." "$release/"
chmod 755 "$release" "$release/bin" "$release/bin/$PROFILE" "$release/bin/$PROFILE/hust_tracker_3576"
ln -s "$release" "$current"
if [ "$model_created" = 1 ]; then ln -s "$release/model" "$BASE_ROOT/model"; fi
mkdir -p "$BASE_ROOT/data/uav-gimbal/logs"
for p in S99hust_tracker S98uav_gimbal_network; do cp "$HERE/service/$p" "$BASE_ROOT/etc/init.d/$p";chmod 755 "$BASE_ROOT/etc/init.d/$p";done
for p in start_tracker.sh switch_app.sh tracker_npu_policy.sh uav_gimbal_log_guard.sh; do cp "$HERE/service/$p" "$BASE_ROOT/home/$p";chmod 755 "$BASE_ROOT/home/$p";done
cp "$HERE/runtime/bin/$PROFILE/hust_tracker_3576" "$BASE_ROOT/home/hust_tracker_3576"
chmod 755 "$BASE_ROOT/home/hust_tracker_3576"
if [ ! -e "$BASE_ROOT/home/recap_servo.env" ]; then
    cp "$HERE/service/recap_servo.env.example" "$BASE_ROOT/home/recap_servo.env"
    echo 'New calibration defaults copied from the tested old unit; verify direction/rate/limits on the physical new gimbal.'
fi
cp "$HERE/service/uav-gimbal" "$BASE_ROOT/usr/local/bin/uav-gimbal";chmod 755 "$BASE_ROOT/usr/local/bin/uav-gimbal"
printf 'export UAV_GIMBAL_CAMERA_ID=%s\nunset HUST_RGB_SOURCE_FPS\n' "$camera" > "$BASE_ROOT/etc/cfg/uav-gimbal.env"
[ -z "$source_fps" ] || echo 'export HUST_RGB_SOURCE_FPS=60' >> "$BASE_ROOT/etc/cfg/uav-gimbal.env"
echo hust > "$BASE_ROOT/etc/cfg/hust-app-mode"
if [ -f "$BASE_ROOT/etc/init.d/S99AppStart" ]; then mv "$BASE_ROOT/etc/init.d/S99AppStart" "$BASE_ROOT/etc/init.d/_99AppStart";fi
echo "$version $PROFILE $digest" > "$receipt"
if [ "$START" = 1 ]; then
    /etc/init.d/S98uav_gimbal_network start
    /home/start_tracker.sh start
fi
trap - EXIT
echo "Installed $version ($PROFILE). Backup: $backup"
echo 'Check: uav-gimbal status; restart: uav-gimbal restart; original app: uav-gimbal factory'
