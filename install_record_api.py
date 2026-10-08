#!/usr/bin/env python3
"""Add the ground-station recorder API; no video service restarts."""
from pathlib import Path
import shutil
import subprocess
import install_recorder

ROOT = Path(__file__).resolve().parent
def install():
    install_recorder.install()
    files = [(ROOT / 'board/record_http.py', Path('/opt/uav-switch/record_http.py')),
             (ROOT / 'board/uav-record-api.service', Path('/etc/systemd/system/uav-record-api.service'))]
    for source, target in files:
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_file() or target.read_bytes() != source.read_bytes():
                raise ValueError('已有不同内容，未覆盖：' + str(target))
    created = []
    installed = subprocess.run(['systemctl', 'is-enabled', 'uav-record-api.service'], capture_output=True).returncode == 0
    running = subprocess.run(['systemctl', 'is-active', 'uav-record-api.service'], capture_output=True).returncode == 0
    if not running and subprocess.run(['fuser', '-n', 'tcp', '9072'], capture_output=True).stdout.strip():
        raise ValueError('录像控制端口 9072 已占用，未启动接口')
    try:
        for source, target in files:
            if not target.exists():
                shutil.copy2(source, target); created.append(target)
        subprocess.run(['systemd-analyze', 'verify', '/etc/systemd/system/uav-record-api.service'], check=True)
        subprocess.run(['python3', '/opt/uav-switch/record_http.py', '--prepare-token'], check=True)
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', 'enable', 'uav-record-api.service'], check=True)
        subprocess.run(['systemctl', 'start', 'uav-record-api.service'], check=True)
        subprocess.run(['systemctl', 'is-active', 'uav-record-api.service'], check=True)
    except BaseException:
        if not running: subprocess.run(['systemctl', 'stop', 'uav-record-api.service'], check=False)
        if not installed: subprocess.run(['systemctl', 'disable', 'uav-record-api.service'], check=False)
        for path in reversed(created): path.unlink(missing_ok=True)
        subprocess.run(['systemctl', 'daemon-reload'], check=False)
        raise
    print('板端录像接口已启动并开机自启，端口 9072。未开始录像，未重启视频。')
    print('地面站首次连接需输入主控 IP 和连接码：sudo cat /etc/uav-record-api/token')

if __name__ == '__main__': install()
