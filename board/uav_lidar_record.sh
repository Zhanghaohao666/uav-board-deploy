#!/bin/sh
if [ "$(id -u)" -ne 0 ]; then
    exec sudo /usr/bin/python3 /opt/uav-switch/lidar_record.py "$@"
fi
exec /usr/bin/python3 /opt/uav-switch/lidar_record.py "$@"
