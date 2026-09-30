#!/usr/bin/env python3
"""Split the verified legacy TTTracker V1.1 installation without changing models."""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

HERE = Path(__file__).resolve().parent
ROOT = Path('/home/dev/test1/rk3588_streamer')
BASE = Path('/opt/uav-front-streams')
CONFIG = Path('/etc/uav-front-streams')
UNITS = Path('/etc/systemd/system')
BACKUPS = Path('/var/backups/uav-front-streams')
OLD = 'uav-front-cameras.service'
SLOTS = ('algorithm', 'preview')
SCRIPT_HASH = 'eb0038dd2ddc27dbc7c8ae030a0b222ae6d9737f0c676f64a34dec8c215a01c9'
BINARY_HASH = '69ad4f397f22fcde20a4b4b7b21971e27c11daa309e8f8f5b503a688deb37ee4'


def ctl(*args, check=True):
    return subprocess.run(['systemctl', *args], check=check, text=True, capture_output=True)


def unit(slot):
    return 'uav-front-%s.service' % slot


def text(slot):
    return '''[Unit]
Description=Independent front camera %s
After=network.target downstream-static-proxy.service
Wants=downstream-static-proxy.service
RequiresMountsFor=/home/dev/test1/rk3588_streamer /mnt/data
StartLimitIntervalSec=0
[Service]
Type=simple
User=dev
SupplementaryGroups=video render
EnvironmentFile=/etc/uav-front-streams/%s.env
ExecStart=/usr/bin/python3 /opt/uav-front-streams/existing_front.py %s
Restart=always
RestartSec=5
KillMode=control-group
TimeoutStopSec=10
StandardOutput=journal
StandardError=journal
LogRateLimitIntervalSec=30s
LogRateLimitBurst=200
[Install]
WantedBy=multi-user.target
''' % (slot, slot, slot)


def preflight():
    for path in (BASE, CONFIG, *(UNITS / unit(s) for s in SLOTS)):
        if path.exists() or path.is_symlink():
            raise ValueError('已有独立服务配置，拒绝覆盖：' + str(path))
    for path, digest in ((ROOT / 'run_algorithm_board.sh', SCRIPT_HASH),
                         (ROOT / 'rk_streamer', BINARY_HASH)):
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('旧程序与已验证版本不一致，需人工适配：' + str(path))
    legacy = UNITS / OLD
    if not legacy.is_file() or legacy.is_symlink():
        raise ValueError('找不到原来的独立 unit 文件')
    if ctl('show', OLD, '-p', 'DropInPaths', '--value').stdout.strip():
        raise ValueError('旧服务有额外配置，需先核对')
    for other in ('uav-algorithm@front1.service', 'uav-algorithm@front2.service'):
        if ctl('is-active', other, check=False).stdout.strip() in ('active', 'activating'):
            raise ValueError('新板纯视频服务已经运行，拒绝重复采集')
    from existing_front import DEFAULTS
    targets = {str(Path(DEFAULTS[s]['device']).resolve()) for s in SLOTS}
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            for fd in (proc / 'fd').iterdir():
                try:
                    target = os.readlink(fd)
                except FileNotFoundError:
                    continue
                if target in targets and ('/' + OLD) not in (proc / 'cgroup').read_text():
                    raise ValueError('相机存在其他占用 PID ' + proc.name)
        except (FileNotFoundError, ProcessLookupError):
            pass


def restore(backup):
    state = json.loads((backup / 'state.json').read_text())
    for slot in SLOTS:
        ctl('disable', '--now', unit(slot), check=False)
        (UNITS / unit(slot)).unlink(missing_ok=True)
    (UNITS / OLD).unlink(missing_ok=True)
    shutil.copy2(backup / OLD, UNITS / OLD)
    shutil.copy2(backup / 'run_algorithm_board.sh', ROOT / 'run_algorithm_board.sh')
    for directory in (BASE, CONFIG):
        if directory.exists():
            shutil.rmtree(directory)
    ctl('daemon-reload')
    ctl('enable' if state['enabled'] else 'disable', OLD)
    if state['active']:
        ctl('start', OLD)
    print('已恢复旧配置：' + str(backup))


def migrate():
    preflight()
    backup = BACKUPS / datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    backup.mkdir(parents=True)
    shutil.copy2(UNITS / OLD, backup / OLD)
    shutil.copy2(ROOT / 'run_algorithm_board.sh', backup / 'run_algorithm_board.sh')
    state = {'enabled': ctl('is-enabled', OLD, check=False).stdout.strip() == 'enabled',
             'active': ctl('is-active', OLD, check=False).stdout.strip() == 'active'}
    (backup / 'state.json').write_text(json.dumps(state))
    print('回退备份：' + str(backup), flush=True)
    try:
        BASE.mkdir(); CONFIG.mkdir()
        for name in ('existing_front.py', 'runtime.py'):
            shutil.copy2(HERE / name, BASE / name)
        from existing_front import DEFAULTS
        for slot in SLOTS:
            item = DEFAULTS[slot]
            (CONFIG / (slot + '.env')).write_text(
                'HOST=192.168.10.1\nDEVICE=%s\nBITRATE=%d\nROTATE=%d\n' %
                (item['device'], item['bitrate'], item['rotate']))
            (UNITS / unit(slot)).write_text(text(slot))
        ctl('disable', '--now', OLD)
        (UNITS / OLD).unlink()
        (UNITS / OLD).symlink_to('/dev/null')
        # Old scripts must not accidentally create a second owner of both cameras.
        (ROOT / 'run_algorithm_board.sh').write_text(
            '#!/bin/bash\necho "采集已拆分，请使用 sudo systemctl start uav-front-algorithm uav-front-preview" >&2\nexit 1\n')
        ctl('daemon-reload')
        for slot in SLOTS:
            ctl('enable', '--now', unit(slot))
        (BASE / 'migration.json').write_text(json.dumps({'backup': str(backup)}, indent=2) + '\n')
    except BaseException:
        restore(backup)
        raise
    print('两路已分别启用；红外保持原独立服务。仍需分别解码验收，不以 active 代替出图。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--apply', action='store_true')
    action.add_argument('--rollback', type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('请使用 sudo；不带参数只做预检查')
    with open('/run/lock/uav-front-streams.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.rollback:
            backup = args.rollback.resolve()
            if backup.parent != BACKUPS or not (backup / 'state.json').is_file():
                parser.error('必须指定本工具生成的备份目录')
            restore(backup)
        elif args.apply:
            migrate()
        else:
            preflight()
            print('预检查通过；执行 --apply 将短暂停止两路前视并分别启用新服务。')


if __name__ == '__main__':
    main()
