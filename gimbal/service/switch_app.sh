#!/bin/sh
# Persistent, mutually exclusive modes for this new gimbal.
set -eu
MODE_FILE=/etc/cfg/hust-app-mode
stop_factory() {
    for name in apps_daemon RkTrackerApp MagicUpdate rkaiq_3A_server; do killall -TERM "$name" 2>/dev/null || true; done
    sleep 2
    for name in apps_daemon RkTrackerApp MagicUpdate rkaiq_3A_server; do killall -KILL "$name" 2>/dev/null || true; done
}
disable_factory_boot() {
    if [ -f /etc/init.d/S99AppStart ]; then mv /etc/init.d/S99AppStart /etc/init.d/_99AppStart; fi
}
status() {
    echo "Selected mode: $(cat "$MODE_FILE" 2>/dev/null || echo hust)"
    /home/start_tracker.sh status || true
    ps -eo pid,comm,args | grep -E '[h]ust_tracker|[R]kTrackerApp|[a]pps_daemon|[r]kaiq_3A' || true
    netstat -lnt | grep -E ':554 |:9000 |:20002 |:12350 ' || true
}
case "${1:-status}" in
    hust)
        disable_factory_boot
        stop_factory
        echo hust > "$MODE_FILE"
        /home/start_tracker.sh restart
        status
        ;;
    factory)
        /home/start_tracker.sh stop
        stop_factory
        echo factory > "$MODE_FILE"
        if [ -f /etc/init.d/_99AppStart ]; then mv /etc/init.d/_99AppStart /etc/init.d/S99AppStart; fi
        test -x /home/work/LinuxRk3576/StartApp.sh
        nohup /home/work/LinuxRk3576/StartApp.sh >/tmp/factory-start.log 2>&1 </dev/null &
        sleep 4
        status
        ;;
    stop)
        disable_factory_boot
        echo stopped > "$MODE_FILE"
        /home/start_tracker.sh stop
        stop_factory
        status
        ;;
    status) status ;;
    *) echo "Usage: $0 {hust|factory|stop|status}";exit 2 ;;
esac
