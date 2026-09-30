#!/usr/bin/env python3
"""One-time raw video deployment for compatible RK3588 algorithm boards."""
import argparse
import datetime
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys

import runtime as rt

ROOT = Path(__file__).resolve().parents[1]
BASE = rt.BASE
UNITS = Path('/etc/systemd/system')
NETWORK = 'uav-algorithm-network.service'
TEMPLATE = 'uav-algorithm@.service'
FACTORY = 'downstream-static-proxy.service'
DROPIN = UNITS / (FACTORY + '.d/90-uav-algorithm.conf')
LAUNCHER = Path('/usr/local/bin/uav-algorithm')
BACKUPS = Path('/var/backups/uav-algorithm')
LEGACY = ('uav-front-cameras.service', 'uav-front-algorithm.service',
          'uav-front-preview.service', 'mino17-streamer.service', 'uav-switch-boot.service')


def parent_installer():
    spec = importlib.util.spec_from_file_location('main_installer', ROOT / 'install.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_bundle():
    parent_installer().verify_manifest()
    manifest = json.loads((ROOT / 'MANIFEST.json').read_text())
    needed = {'algorithm/install.py', 'algorithm/install.sh', 'algorithm/runtime.py', 'algorithm/bin/mino17_streamer'}
    if not needed.issubset(manifest):
        raise ValueError('安装包缺少算法板程序校验项')
    return hashlib.sha256((ROOT / 'MANIFEST.json').read_bytes()).hexdigest()


def config(args):
    c = {'iface': args.iface, 'address': args.address, 'host': args.host, 'user': args.user}
    for slot in rt.SLOTS:
        c[slot] = {'enabled': slot != 'infrared' or not args.without_infrared,
                   'device': getattr(args, slot), 'rotate': getattr(args, slot + '_rotate'),
                   'bitrate': 1500 if slot == 'infrared' else 3000}
    # Discover the connected USB serial rather than copying a different board's ID.
    if args.infrared == DEFAULT_IR:
        candidates = sorted(Path('/dev/v4l/by-id').glob('*Mino17*video-index0'))
        if len(candidates) > 1:
            raise ValueError('发现多个红外相机，请指定 --infrared')
        if len(candidates) == 1:
            c['infrared']['device'] = str(candidates[0])
    return rt.validate(c)


def camera_owners(c):
    devices = {str(Path(c[s]['device']).resolve()) for s in rt.SLOTS if c[s]['enabled']}
    owners = []
    for process in Path('/proc').iterdir():
        if not process.name.isdigit():
            continue
        try:
            for fd in (process / 'fd').iterdir():
                try:
                    if os.readlink(fd) in devices:
                        # Name/PID only, never dump process arguments (may contain credentials).
                        owners.append('%s:%s' % (process.name, (process / 'comm').read_text().strip()))
                        break
                except FileNotFoundError:
                    pass
        except (FileNotFoundError, ProcessLookupError):
            pass
        except PermissionError as e:
            raise ValueError('无法完整检查相机占用，请以 root 运行') from e
    return owners


def assert_fresh(c):
    for p in (BASE, LAUNCHER, UNITS / NETWORK, UNITS / TEMPLATE, DROPIN):
        if p.exists():
            raise ValueError('已有安装文件，拒绝覆盖：' + str(p))
    for unit in LEGACY:
        state = rt.run(['systemctl', 'is-active', unit], check=False).stdout.strip()
        enabled = rt.run(['systemctl', 'is-enabled', unit], check=False).stdout.strip()
        if state in ('active', 'activating', 'reloading') or enabled in ('enabled', 'enabled-runtime', 'linked', 'linked-runtime'):
            raise ValueError('已有采集/主控服务：%s；不能与本工具同时占用相机' % unit)
    owners = camera_owners(c)
    if owners:
        raise ValueError('相机正在被使用，未停止任何程序：' + ', '.join(owners))


def check_network(c):
    if not Path('/sys/class/net', c['iface']).exists() or Path('/sys/class/net', c['iface'], 'wireless').exists():
        raise ValueError('需要存在的有线板间网口：' + c['iface'])
    if shutil.which('nmcli'):
        nm = rt.run(['nmcli', '-g', 'GENERAL.STATE', 'device', 'show', c['iface']], check=False)
        if nm.returncode == 0 and not nm.stdout.strip().startswith('10 '):
            raise ValueError('该网口由 NetworkManager 管理；本版本只支持厂家 BSP 的 unmanaged 板间口，未修改网络')
    # Do not change configuration here. Actual duplicate-IP check runs at activation.
    rt.has_address(c, rt.addresses())


def deps(install):
    packages = {'ip': 'iproute2', 'arping': 'iputils-arping', 'v4l2-ctl': 'v4l-utils', 'stdbuf': 'coreutils'}
    missing = sorted({pkg for binary, pkg in packages.items() if not shutil.which(binary)})
    if missing:
        if not install:
            raise ValueError('缺少系统工具：' + ', '.join(missing))
        if not shutil.which('apt-get'):
            raise ValueError('需要 apt-get 或手动安装：' + ', '.join(missing))
        subprocess.run(['apt-get', 'update'], check=True)
        subprocess.run(['apt-get', 'install', '-y', '--no-install-recommends'] + missing,
                       check=True, env=dict(os.environ, DEBIAN_FRONTEND='noninteractive'))
    env = rt.environment(ROOT / 'board')
    for binary in (ROOT / 'board/bin/rk_streamer', ROOT / 'algorithm/bin/mino17_streamer'):
        info = rt.run(['ldd', str(binary)], check=False, env=env)
        if info.returncode != 0 or 'not found' in info.stdout + info.stderr:
            raise ValueError('BSP/编码库不匹配：%s\n%s%s' % (binary.name, info.stdout, info.stderr))
        rt.run([str(binary), '--help'], env=env)


def cameras(c, require_front):
    for slot in rt.SLOTS:
        if not c[slot]['enabled']:
            print(slot + '：禁用')
            continue
        state, detail = rt.inspect_camera(slot, c[slot])
        print('%s [%s] %s' % (slot, state, detail))
        if state == 'missing' and require_front and slot != 'infrared':
            raise ValueError('缺少前视相机；请检查 --front1/--front2 与驱动')
    print('缺席相机只影响自己的服务，其余各路独立启动；缺席的服务会等待重试。')


def unit_texts(c):
    # Verified Linux account; no user-provided shell commands in systemd files.
    import grp
    groups = [g for g in ('video', 'render') if g in {x.gr_name for x in grp.getgrall()}]
    network = '''[Unit]
Description=RK3588 algorithm board static payload network
Wants=network-online.target
After=network-online.target downstream-static-proxy.service
StartLimitIntervalSec=0
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/uav-algorithm/runtime.py network
RemainAfterExit=yes
Restart=on-failure
RestartSec=5
TimeoutStartSec=45
[Install]
WantedBy=multi-user.target
'''
    worker = '''[Unit]
Description=RK3588 raw video %%i
Wants=uav-algorithm-network.service
After=uav-algorithm-network.service
StartLimitIntervalSec=0
[Service]
Type=simple
User=%s
SupplementaryGroups=%s
WorkingDirectory=/opt/uav-algorithm
ExecStart=/usr/bin/python3 /opt/uav-algorithm/runtime.py stream %%i
Restart=on-failure
RestartSec=5
KillMode=control-group
TimeoutStopSec=10
[Install]
WantedBy=multi-user.target
''' % (c['user'], ' '.join(groups))
    dropin = '''# Generated by uav-board-deploy algorithm role. Original unit retained.
[Service]
ExecStart=
ExecStart=/usr/bin/python3 /opt/uav-algorithm/runtime.py network
'''
    return network, worker, dropin


def install(c, bundle_digest, no_start):
    receipt = BASE / 'installation.json'
    if receipt.exists():
        previous = json.loads(receipt.read_text())
        hashes = previous.get('files', {})
        intact = bool(hashes) and all(Path(p).is_file() and hashlib.sha256(Path(p).read_bytes()).hexdigest() == h for p, h in hashes.items())
        if previous['bundle'] == bundle_digest and rt.load(BASE / 'config.json') == c and intact:
            print('同版本同配置已安装，未重启或改动运行服务。查看状态：sudo uav-algorithm status')
            return
        raise ValueError('已有不同版本/配置。本安装器仅用于新板，未覆盖；请先备份并安排维护')
    assert_fresh(c)
    deps(install=True)
    check_network(c)
    cameras(c, require_front=False)
    network, worker, dropin = unit_texts(c)
    units = [NETWORK] + ['uav-algorithm@%s.service' % s for s in rt.SLOTS if c[s]['enabled']]
    original_ip = rt.has_address(c, rt.addresses())
    backup = BACKUPS / datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    backup.mkdir(parents=True)
    (backup / 'addresses.json').write_text(json.dumps(rt.addresses(), indent=2))
    (backup / 'routes.txt').write_text(rt.run(['ip', 'route', 'show', 'table', 'all']).stdout)
    factory = rt.run(['systemctl', 'cat', FACTORY], check=False)
    (backup / 'factory-network-unit.txt').write_text(factory.stdout)
    created = []
    try:
        BASE.mkdir()
        (BASE / 'bin').mkdir()
        shutil.copytree(ROOT / 'board/lib', BASE / 'lib')
        for source, target in [(ROOT / 'board/bin/rk_streamer', BASE / 'bin/rk_streamer'),
                               (ROOT / 'algorithm/bin/mino17_streamer', BASE / 'bin/mino17_streamer'),
                               (ROOT / 'algorithm/runtime.py', BASE / 'runtime.py')]:
            shutil.copy2(source, target)
            target.chmod(0o755)
        (BASE / 'config.json').write_text(json.dumps(c, ensure_ascii=False, indent=2) + '\n')
        (BASE / 'config.json').chmod(0o644)
        contents = {UNITS / NETWORK: network, UNITS / TEMPLATE: worker,
                    LAUNCHER: '#!/bin/sh\nexec /usr/bin/python3 /opt/uav-algorithm/runtime.py "$@"\n'}
        if factory.returncode == 0:
            contents[DROPIN] = dropin
        for path, text in contents.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            created.append(path)
            path.write_text(text)
            path.chmod(0o755 if path == LAUNCHER else 0o644)
        rt.run(['systemctl', 'daemon-reload'])
        rt.run(['systemctl', 'enable'] + units)
        if not no_start:
            # Synchronous validation before starting video. No restart of factory units.
            rt.network(c)
            rt.run(['systemctl', 'start', NETWORK], timeout=50)
            rt.run(['systemctl', 'start'] + units[1:])
        installed = [p for p in BASE.rglob('*') if p.is_file()] + created
        hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in installed}
        receipt.write_text(json.dumps({'bundle': bundle_digest, 'backup': str(backup), 'files': hashes,
                                       'enabled': units, 'started': not no_start}, indent=2) + '\n')
    except BaseException:
        rt.run(['systemctl', 'stop'] + units, check=False, timeout=60)
        rt.run(['systemctl', 'disable'] + units, check=False)
        if not original_ip and rt.has_address(c, rt.addresses()):
            rt.run(['ip', 'address', 'del', c['address'], 'dev', c['iface']], check=False)
        for path in created:
            path.unlink(missing_ok=True)
        shutil.rmtree(BASE, ignore_errors=True)
        rt.run(['systemctl', 'daemon-reload'], check=False)
        raise
    print('安装完成。%s；没有启动检测、跟踪或 TCP 控制程序。' % ('已启用下次开机自启，当前未启动' if no_start else '已启动并启用开机自启'))
    print('配置：/opt/uav-algorithm/config.json；状态：sudo uav-algorithm status')
    print('请在主控验收 /algorithm、/preview、/infrared 的实际画面；服务启动不代表三路均已解码。')


DEFAULT_IR = '/dev/v4l/by-id/usb-CHIPUP_Mino17_Mino17-video-index0'


def main():
    p = argparse.ArgumentParser(description='RK3588 算法板纯视频一键安装（不含检测/跟踪）')
    p.add_argument('action', nargs='?', choices=('install', 'doctor'), default='install')
    p.add_argument('--iface', default='eth0')
    p.add_argument('--address', default='192.168.10.2/24')
    p.add_argument('--host', default='192.168.10.1')
    p.add_argument('--user', default=os.environ.get('SUDO_USER') or 'dev')
    p.add_argument('--front1', default='/dev/v4l/by-path/platform-rkcif-mipi-lvds4-video-index0')
    p.add_argument('--front2', default='/dev/v4l/by-path/platform-rkcif-mipi-lvds-video-index0')
    p.add_argument('--infrared', default=DEFAULT_IR)
    for slot in rt.SLOTS:
        p.add_argument('--' + slot + '-rotate', type=int, default=0)
    p.add_argument('--without-infrared', action='store_true')
    p.add_argument('--no-start', action='store_true', help='仅启用下次开机自启，不改变当前 IP 或启动服务')
    a = p.parse_args()
    if os.geteuid() != 0:
        raise ValueError('请使用 sudo 运行，以完整检查相机占用')
    digest = verify_bundle()
    parent_installer().platform_check()
    c = config(a)
    account = pwd.getpwnam(c['user'])
    if account.pw_uid == 0 or not c['user'].replace('_', '').replace('-', '').isalnum():
        raise ValueError('请选择普通服务用户，例如 --user dev')
    if a.action == 'doctor':
        deps(install=False)
        check_network(c)
        cameras(c, require_front=False)
        owners = camera_owners(c)
        print('相机占用：' + (', '.join(owners) if owners else '无'))
        if not (BASE / 'installation.json').exists():
            assert_fresh(c)
        print('只读预检查完成；未安装、改 IP、取图或发流。')
    else:
        with open('/run/lock/uav-algorithm-install.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            install(c, digest, a.no_start)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as e:
        print('未完成：' + str(e), file=sys.stderr)
        sys.exit(1)
