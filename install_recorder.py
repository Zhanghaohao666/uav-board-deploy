#!/usr/bin/env python3
"""Add the manual recorder to an existing main board, without restarting video."""
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent


def install():
    if os.geteuid() != 0:
        raise ValueError('请使用 sudo python3 install_recorder.py')
    if not Path('/opt/uav-switch/manager.py').is_file() or not Path('/opt/uav-switch/config.json').is_file():
        raise ValueError('需要已经安装主控 uav-switch；新板请使用完整安装器')
    if not shutil.which('ffmpeg'):
        raise ValueError('缺少 ffmpeg，请先安装该依赖')
    destinations = [(ROOT / 'board/record.py', Path('/opt/uav-switch/record.py')),
                    (ROOT / 'board/uav_record.sh', Path('/opt/uav-switch/uav_record.sh')),
                    (ROOT / 'board/uav_record.sh', Path('/usr/local/bin/uav-record')),
                    (ROOT / 'board/uav-record@.service', Path('/etc/systemd/system/uav-record@.service'))]
    for source, target in destinations:
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_file() or target.read_bytes() != source.read_bytes():
                raise ValueError('已有不同内容，未覆盖：' + str(target))
    created = []
    try:
        for source, target in destinations:
            if target.exists():
                continue
            shutil.copy2(source, target); created.append(target)
            target.chmod(0o755 if target.name in ('uav-record', 'uav_record.sh') else 0o644)
        subprocess.run(['systemd-analyze', 'verify', '/etc/systemd/system/uav-record@.service'], check=True)
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
    except BaseException:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        subprocess.run(['systemctl', 'daemon-reload'], check=False)
        raise
    print('按需录像工具已安装：uav-record。未启用开机录像，未重启采集/转发。')


if __name__ == '__main__':
    install()
