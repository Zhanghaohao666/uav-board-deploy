#!/usr/bin/env python3
"""Add/update lidar recording control without changing network or video services."""
import argparse
from contextlib import ExitStack
import datetime
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from board.lidar_record import legacy_busy, config as lidar_config

ROOT = Path(__file__).resolve().parent
BASE = Path('/opt/uav-switch')
BIN = Path('/usr/local/bin')
UNITS = Path('/etc/systemd/system')
BACKUPS = Path('/var/backups/uav-lidar-installer')
LOCK = Path('/run/uav-lidar/control.lock')


def run(args, check=True):
    return subprocess.run(args, check=check, capture_output=True, text=True, timeout=20)


def install(upgrade=False):
    if os.geteuid() != 0: raise ValueError('请使用 sudo python3 install_lidar_recorder.py --upgrade')
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with ExitStack() as stack:
            legacy_path = Path(lidar_config()['root']) / '.uav-lidar-record.lock'
            # Legacy CLI can hold this lock before its SDK process has spawned.
            # Existing locks are sufficient; do not create /data paths here.
            if legacy_path.exists():
                legacy_lock = stack.enter_context(legacy_path.open('a'))
                try: fcntl.flock(legacy_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ValueError('已有旧版点云采集任务，请先结束再升级') from None
            return install_locked(upgrade)


def install_locked(upgrade):
    if not (BASE / 'record.py').is_file() or not (BASE / 'record_http.py').is_file():
        raise ValueError('需要已有主控录像 API；新板请使用完整安装器')
    service = run(['systemctl', 'show', 'uav-lidar-record.service', '-p', 'ActiveState', '--value'], False)
    if getattr(service, 'stdout', '').strip() in ('active', 'activating', 'deactivating'):
        raise ValueError('雷达采集正在运行，请先停止再升级')
    if legacy_busy():
        raise ValueError('仍有厂家点云采集进程，请先结束再升级')
    entries = [('lidar_record.py', BASE / 'lidar_record.py'),
               ('record_http.py', BASE / 'record_http.py'),
               ('uav_lidar_record.sh', BASE / 'uav_lidar_record.sh'),
               ('uav_lidar_record.sh', BIN / 'uav-lidar-record'),
               ('uav-lidar-record.service', UNITS / 'uav-lidar-record.service')]
    changed = []
    for source, target in entries:
        data = (ROOT / 'board' / source).read_bytes()
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ValueError('安装目标不是普通文件：' + str(target))
        if target.exists() and target.read_bytes() == data: continue
        if target.exists() and not upgrade:
            raise ValueError('发现旧版本；使用 --upgrade 自动备份后更新：' + str(target))
        changed.append((source, target, data))
    if not changed:
        print('雷达录像控制已是当前版本，未重启任何服务'); return
    backup = BACKUPS / datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    backup.mkdir(parents=True, mode=0o700)
    existed = set()
    applied = []
    running = run(['systemctl', 'is-active', 'uav-record-api.service'], False).returncode == 0
    try:
        for source, target, data in changed:
            saved = backup / str(target).lstrip('/')
            if target.exists():
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, saved); existed.add(target)
            fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=target.name + '.')
            try:
                with os.fdopen(fd, 'wb') as out: out.write(data)
                os.chmod(temporary, 0o755 if source.endswith('.sh') else 0o644)
                os.replace(temporary, target)
                applied.append(target)
            finally:
                if os.path.exists(temporary): os.unlink(temporary)
        run(['systemd-analyze', 'verify', str(UNITS / 'uav-lidar-record.service')])
        run(['systemctl', 'daemon-reload'])
        # No acquisition is enabled or started. An API restart leaves both
        # video recordings and systemd-owned lidar acquisition independent.
        if running:
            run(['systemctl', 'restart', 'uav-record-api.service'])
            run(['systemctl', 'is-active', 'uav-record-api.service'])
    except BaseException:
        for target in reversed(applied):
            saved = backup / str(target).lstrip('/')
            if target in existed: shutil.copy2(saved, target)
            else: target.unlink(missing_ok=True)
        run(['systemctl', 'daemon-reload'], False)
        if running: run(['systemctl', 'restart', 'uav-record-api.service'], False)
        raise
    print('雷达按需录制已安装，原文件备份：' + str(backup))
    print('未开始采集、未配置开机采集、未重启视频。厂家 SDK 需预先安装。')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--upgrade', action='store_true', help='备份并覆盖已有版本')
    args = parser.parse_args()
    install(args.upgrade)
