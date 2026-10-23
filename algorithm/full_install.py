#!/usr/bin/env python3
"""Install the complete tested tracking application onto a fresh vendor BSP.

This is intentionally separate from install.py (raw cameras only). It never
overwrites a previous deployment, stops an unrelated camera owner or reformats
a disk. Same package/configuration installs are a no-op.
"""
import argparse
import datetime
import fcntl
import grp
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid

import full_runtime as full
import runtime as rt
import runtime_bundle as bundle

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BASE = Path('/opt/uav-front-streams')
CONFIG_DIR = Path('/etc/uav-front-streams')
UNITS = Path('/etc/systemd/system')
LAUNCHER = Path('/usr/local/bin/uav-algorithm')
NM_FILE = Path('/etc/NetworkManager/system-connections/uav-algorithm-payload.nmconnection')
DROPIN = UNITS / 'downstream-static-proxy.service.d/95-uav-full-algorithm.conf'
BACKUPS = Path('/var/backups/uav-algorithm-full')
CONFLICTS = ('uav-front-cameras.service', 'uav-algorithm@front1.service',
             'uav-algorithm@front2.service', 'uav-algorithm@infrared.service',
             'uav-switch-boot.service', 'uav-switch@board.service')


def ctl(*args, **kwargs):
    return rt.run(['systemctl', *args], **kwargs)


def parent_installer():
    spec = importlib.util.spec_from_file_location('main_full_parent', ROOT / 'install.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def mounted_data_root(requested=None):
    choices = [Path(requested)] if requested else [Path('/data'), Path('/mnt/data')]
    for path in choices:
        if path.is_symlink() or not path.is_absolute() or any(x in str(path) for x in (' ', '%', ':', '\n', '\r')) or '..' in path.parts:
            raise ValueError('数据盘路径必须是无空白的真实挂载目录')
        if os.path.ismount(path) and path.stat().st_dev != Path('/').stat().st_dev:
            return path
    raise ValueError('没有已挂载的数据盘；请先挂载厂家数据分区，不能把模型和日志写入系统盘。可指定 --data-root')


def config(args, digest, manifest):
    data = mounted_data_root(args.data_root)
    c = {'iface': args.iface, 'address': args.address, 'host': args.host, 'user': args.user,
         'data_root': str(data), 'release': str(data / 'uav-algorithm/releases' / digest[:20]),
         'library_dirs': manifest['library_dirs'], 'network_mode': 'direct', 'network_uuid': ''}
    for slot, bitrate in (('front1', 5000), ('front2', 3000), ('infrared', 1500)):
        c[slot] = {'enabled': slot != 'infrared' or not args.without_infrared,
                   'device': getattr(args, slot), 'rotate': getattr(args, slot + '_rotate'), 'bitrate': bitrate}
    if args.infrared == '/dev/v4l/by-id/usb-CHIPUP_Mino17_Mino17-video-index0':
        candidates = sorted(Path('/dev/v4l/by-id').glob('*Mino17*video-index0'))
        if len(candidates) > 1:
            raise ValueError('发现多个红外相机，请使用 --infrared 指定')
        if candidates:
            c['infrared']['device'] = str(candidates[0])
    return full.validate(c)


def target_route(address):
    rows = json.loads(rt.run(['ip', '-j', 'route', 'get', address]).stdout)
    return rows[0].get('dev') if rows else None


def assert_network_safe(c):
    iface = c['iface']
    if not Path('/sys/class/net', iface).exists() or Path('/sys/class/net', iface, 'wireless').exists():
        raise ValueError('板间网口必须是存在的有线网口')
    infos = rt.addresses()
    network = ipaddress.IPv4Interface(c['address']).network
    for item in infos:
        if item['ifname'] == iface:
            continue
        for addr in item.get('addr_info', []):
            if addr.get('family') == 'inet' and ipaddress.IPv4Interface('%s/%s' % (addr['local'], addr['prefixlen'])).network.overlaps(network):
                raise ValueError('另一网口已有板间子网，拒绝添加冲突路由：' + item['ifname'])
    defaults = json.loads(rt.run(['ip', '-j', 'route', 'show', 'default']).stdout)
    if any(row.get('dev') == iface for row in defaults):
        raise ValueError('所选网口承载默认路由；请从 Wi-Fi/另一网口连接并选择专用板间口，未修改网络')
    peers = set()
    if os.environ.get('SSH_CONNECTION'):
        peers.add(os.environ['SSH_CONNECTION'].split()[0])
    # sudo may remove SSH_CONNECTION. Inspect established server-side SSH sockets
    # as well, without printing peer credentials or unrelated process arguments.
    sockets = rt.run(['ss', '-H', '-tn', 'state', 'established']).stdout
    for line in sockets.splitlines():
        fields = line.split()
        if len(fields) >= 4:
            local, peer = fields[-2:]
            if local.rsplit(':', 1)[-1] == '22':
                peers.add(peer.rsplit(':', 1)[0].strip('[]'))
    for peer in peers:
        try:
            ipaddress.ip_address(peer)
        except ValueError:
            continue
        if target_route(peer) == iface:
            raise ValueError('所选网口承载当前 SSH；拒绝改动，请通过 Wi-Fi/其他管理网口安装')
    rt.has_address(c, infos)


def network_plan(c):
    assert_network_safe(c)
    if not shutil.which('nmcli'):
        return c
    env = dict(os.environ, LC_ALL='C')
    state = rt.run(['nmcli', '-g', 'GENERAL.STATE', 'device', 'show', c['iface']], check=False, env=env)
    if state.returncode != 0:
        raise ValueError('无法读取 NetworkManager 网口管理状态；未修改网络')
    if state.stdout.strip().startswith('10 '):
        return c
    connection = rt.run(['nmcli', '-g', 'GENERAL.CON-UUID', 'device', 'show', c['iface']], env=env).stdout.strip()
    if re.fullmatch('[0-9a-fA-F-]{36}', connection):
        def prop(name):
            return rt.run(['nmcli', '-g', name, 'connection', 'show', 'uuid', connection], env=env).stdout.strip()
        if (prop('ipv4.method') == 'manual' and c['address'] in prop('ipv4.addresses').split(',')
                and prop('ipv4.never-default') == 'yes' and not prop('ipv4.gateway')):
            c.update(network_mode='nm-reuse', network_uuid=connection)
            return c
    c.update(network_mode='nm-new', network_uuid=str(uuid.uuid5(uuid.NAMESPACE_URL,
        'uav-algorithm:' + c['iface'] + ':' + c['address'])))
    if rt.run(['nmcli', 'connection', 'show', 'uuid', c['network_uuid']], check=False, env=env).returncode == 0:
        raise ValueError('已存在同名部署网络连接；拒绝覆盖，请先检查现有安装')
    return c


def nm_text(c):
    return '''[connection]
id=uav-algorithm-payload
uuid=%s
type=ethernet
interface-name=%s
autoconnect=false
autoconnect-priority=500
[ethernet]
[ipv4]
method=manual
address1=%s
never-default=true
ignore-auto-dns=true
dad-timeout=3000
[ipv6]
method=disabled
''' % (c['network_uuid'], c['iface'], c['address'])


def camera_owners(c):
    targets = {str(Path(c[s]['device']).resolve()) for s in rt.SLOTS if c[s]['enabled']}
    owners = []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            for fd in (proc / 'fd').iterdir():
                try:
                    if os.readlink(fd) in targets:
                        owners.append(proc.name)
                        break
                except FileNotFoundError:
                    pass
        except (FileNotFoundError, ProcessLookupError):
            pass
        except PermissionError as exc:
            raise ValueError('无法完整检查相机占用，必须使用 root') from exc
    return owners


def fresh_check(c):
    paths = [BASE, CONFIG_DIR, LAUNCHER, DROPIN, UNITS / full.NETWORK,
             *(UNITS / unit for unit in full.UNITS.values()), Path('/opt/uav-algorithm'),
             Path(c['data_root']) / 'uav-algorithm']
    if c['network_mode'] == 'nm-new':
        paths.append(NM_FILE)
    for path in paths:
        if path.exists() or path.is_symlink():
            raise ValueError('发现已有部署，未覆盖或停止任何服务：' + str(path))
    for unit in (full.NETWORK, *full.UNITS.values(), *CONFLICTS):
        if unit in (full.NETWORK, *full.UNITS.values()):
            fragment = ctl('show', unit, '--property=FragmentPath', '--value', check=False).stdout.strip()
            if fragment:
                raise ValueError('已有同名服务文件，未覆盖：' + unit)
        active = ctl('is-active', unit, check=False).stdout.strip()
        enabled = ctl('is-enabled', unit, check=False).stdout.strip()
        if active in ('active', 'activating', 'reloading', 'deactivating') or enabled in ('enabled', 'enabled-runtime', 'linked', 'linked-runtime'):
            raise ValueError('已有服务启用/运行，拒绝抢占：' + unit)
    owners = camera_owners(c)
    if owners:
        raise ValueError('相机已经被占用 PID ' + ','.join(owners))
    # A stale standalone tracker/control server can otherwise prevent algorithm
    # startup even when it does not currently own a camera. Never stop it here.
    listeners = rt.run(['ss', '-H', '-ltn']).stdout
    if any(re.search(r':9000\s', line) for line in listeners.splitlines()):
        raise ValueError('算法控制 TCP 9000 已被占用，未停止已有程序')


def ensure_tools(install=False):
    packages = {'ip': 'iproute2', 'ss': 'iproute2', 'arping': 'iputils-arping',
                'v4l2-ctl': 'v4l-utils', 'stdbuf': 'coreutils', 'ldd': 'libc-bin'}
    missing = sorted({pkg for program, pkg in packages.items() if not shutil.which(program)})
    if missing:
        if not install or not shutil.which('apt-get'):
            raise ValueError('缺少系统工具（离线安装请先在厂家镜像准备）：' + ', '.join(missing))
        rt.run(['apt-get', 'update'], timeout=300)
        rt.run(['apt-get', 'install', '-y', '--no-install-recommends', *missing], timeout=300,
               env=dict(os.environ, DEBIAN_FRONTEND='noninteractive'))


def check_runtime(stage, manifest, user=None):
    env = dict(os.environ, LD_LIBRARY_PATH=':'.join(str(stage / x) for x in manifest['library_dirs']), LC_ALL='C')
    account = pwd.getpwnam(user) if user else None
    def demote():
        os.initgroups(user, account.pw_gid)
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)
    for name in sorted(bundle.REQUIRED):
        path = stage / name
        with path.open('rb') as stream:
            header = stream.read(20)
        if len(header) < 20 or header[:6] != b'\x7fELF\x02\x01' or int.from_bytes(header[18:20], 'little') != 183:
            raise ValueError('不是 Linux ARM64 可执行程序：' + name)
        options = {'preexec_fn': demote} if account else {}
        result = rt.run(['ldd', str(path)], check=False, env=env, **options)
        if result.returncode != 0 or 'not found' in result.stdout + result.stderr:
            raise ValueError('当前厂家镜像缺少/不兼容运行依赖：%s\n%s%s' % (name, result.stdout, result.stderr))


def unit_files(c):
    groups = [g for g in ('video', 'render') if g in {x.gr_name for x in grp.getgrall()}]
    common = '''After=network.target uav-algorithm-network.service
Wants=uav-algorithm-network.service
RequiresMountsFor=%s
StartLimitIntervalSec=0
[Service]
Type=simple
User=%s
SupplementaryGroups=%s
WorkingDirectory=%s/stream
Restart=on-failure
RestartSec=30
KillMode=control-group
TimeoutStopSec=10
StandardOutput=journal
StandardError=journal
LogRateLimitIntervalSec=30s
LogRateLimitBurst=200
''' % (c['data_root'], c['user'], ' '.join(groups), c['release'])
    contents = {}
    for slot, name in full.UNITS.items():
        text = '[Unit]\nDescription=UAV complete algorithm board ' + slot + '\n' + common
        if slot == 'algorithm':
            text += 'CPUAffinity=4 5\n'
        text += 'ExecStart=/usr/bin/python3 /opt/uav-front-streams/full_runtime.py stream ' + slot
        text += '\n[Install]\nWantedBy=multi-user.target\n'
        contents[UNITS / name] = text
    contents[UNITS / full.NETWORK] = '''[Unit]
Description=UAV algorithm payload network
After=network.target NetworkManager.service downstream-static-proxy.service
StartLimitIntervalSec=0
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/uav-front-streams/full_runtime.py network
RemainAfterExit=yes
Restart=on-failure
RestartSec=30
TimeoutStartSec=60
[Install]
WantedBy=multi-user.target
'''
    contents[LAUNCHER] = '#!/bin/sh\nexec /usr/bin/python3 /opt/uav-front-streams/full_runtime.py "$@"\n'
    # Prevent a factory service from later replacing the explicitly chosen
    # board-to-board address. The original unit and its enabled state survive.
    if ctl('cat', 'downstream-static-proxy.service', check=False).returncode == 0:
        contents[DROPIN] = '[Service]\nExecStart=\nExecStart=/usr/bin/true\n'
    if c['network_mode'] == 'nm-new':
        contents[NM_FILE] = nm_text(c)
    return contents


def receipt_intact(c, digest, installer_digest):
    receipt = BASE / 'installation-full.json'
    if not receipt.exists():
        return False
    previous = json.loads(receipt.read_text())
    if previous.get('bundle') != digest or previous.get('installer') != installer_digest or previous.get('config') != c:
        raise ValueError('已有不同版本/配置的完整部署；本安装器只用于新镜像，未覆盖')
    if not all(Path(p).is_file() and bundle.sha256(p) == h for p, h in previous.get('files', {}).items()):
        raise ValueError('已有部署文件被修改；拒绝静默覆盖')
    manifest = json.loads((Path(c['release']) / 'runtime-manifest.json').read_text())
    if not bundle.verify_tree(c['release'], manifest):
        raise ValueError('已有算法程序/模型校验失败；未重启任何服务')
    if c['network_mode'] == 'nm-new':
        # NetworkManager legitimately rewrites its keyfile on activation. Check
        # semantic values, not the transient serialized keyfile checksum.
        for name, value in (('ipv4.method', 'manual'), ('ipv4.addresses', c['address']),
                            ('ipv4.never-default', 'yes'), ('connection.interface-name', c['iface'])):
            actual = rt.run(['nmcli', '-g', name, 'connection', 'show', 'uuid', c['network_uuid']],
                            env=dict(os.environ, LC_ALL='C')).stdout.strip()
            if actual != value:
                raise ValueError('已有板间网络配置被修改，未覆盖：' + name)
    return True


def install(c, archive, digest, manifest, installer_digest, no_start=False, install_deps=False):
    if receipt_intact(c, digest, installer_digest):
        print('同版本同配置已安装，未重启或改动服务。sudo uav-algorithm status')
        return
    ensure_tools(install=install_deps)
    fresh_check(c)
    data = Path(c['data_root'])
    needed = sum(x['size'] for x in manifest['files'].values()) * 2 + 2 * 1024 ** 3
    if shutil.disk_usage(data).free < needed:
        raise ValueError('数据盘空间不足，需给安装及后续日志保留至少 2GiB')
    # Stage and ldd run before any service/network/configuration mutation.
    stage = Path(tempfile.mkdtemp(prefix='.uav-algorithm-stage-', dir=data))
    created = []
    units = [full.NETWORK, full.UNITS['algorithm'], full.UNITS['preview']]
    if c['infrared']['enabled']:
        units.append(full.UNITS['infrared'])
    old_ip = rt.has_address(c, rt.addresses())
    nm_loaded = False
    old_nm = ''
    try:
        bundle.extract(archive, digest, stage)
        check_runtime(stage, manifest, c['user'])
        contents = unit_files(c)
        backup = BACKUPS / datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        backup.mkdir(parents=True)
        (backup / 'network-before.json').write_text(json.dumps(rt.addresses(), indent=2))
        (backup / 'routes-before.txt').write_text(rt.run(['ip', 'route', 'show', 'table', 'all']).stdout)
        (backup / 'factory-unit-before.txt').write_text(ctl('cat', 'downstream-static-proxy.service', check=False).stdout)
        if c['network_mode'] == 'nm-new':
            old_nm = rt.run(['nmcli', '-g', 'GENERAL.CON-UUID', 'device', 'show', c['iface']]).stdout.strip()
        (backup / 'transaction.json').write_text(json.dumps({'config': c, 'created_paths': list(map(str, contents)),
             'old_nm_connection': old_nm, 'old_ip_present': old_ip, 'bundle': digest}, indent=2))
        app_data = data / 'uav-algorithm'
        app_data.mkdir(); app_data.chmod(0o755); created.append(app_data)
        release = Path(c['release']); release.parent.mkdir(); release.parent.chmod(0o755)
        shutil.move(str(stage), str(release))
        account = pwd.getpwnam(c['user'])
        build = release / 'stream/TTTracker_V1.1_publish/build'
        build.mkdir(exist_ok=True)
        # Only SDK's working/output directory is writable by its service account.
        os.chown(build, account.pw_uid, account.pw_gid)
        BASE.mkdir(); BASE.chmod(0o755); created.append(BASE)
        for name in ('runtime.py', 'full_runtime.py'):
            shutil.copy2(HERE / name, BASE / name)
        CONFIG_DIR.mkdir(); CONFIG_DIR.chmod(0o755); created.append(CONFIG_DIR)
        (CONFIG_DIR / 'full.json').write_text(json.dumps(c, ensure_ascii=False, indent=2) + '\n')
        (CONFIG_DIR / 'full.json').chmod(0o644)
        for path, text in contents.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            # Fresh paths only; do not replace an unexpected racing writer.
            with path.open('x') as stream:
                created.append(path)
                stream.write(text)
            path.chmod(0o600 if path == NM_FILE else 0o755 if path == LAUNCHER else 0o644)
        if c['network_mode'] == 'nm-new':
            nm_loaded = True
            rt.run(['nmcli', 'connection', 'load', str(NM_FILE)])
        ctl('daemon-reload')
        ctl('enable', *units)
        if not no_start:
            # Missing network/cameras are handled by each independent service.
            # --no-block avoids making an unplugged payload fail installation.
            ctl('start', '--no-block', *units)
        hashes = {str(p): bundle.sha256(p) for root in (BASE, CONFIG_DIR) for p in root.rglob('*') if p.is_file()}
        hashes.update({str(p): bundle.sha256(p) for p in contents if p != NM_FILE})
        receipt = {'bundle': digest, 'installer': installer_digest, 'config': c, 'files': hashes,
                   'backup': str(backup), 'enabled': units, 'started': not no_start}
        (BASE / 'installation-full.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
        print('完整算法应用已安装：检测跟踪、预览、红外各自独立，已配置开机自启。')
        print('程序/模型：' + str(release))
        print('部署前状态备份：' + str(backup))
        print('检查：sudo uav-algorithm status；重启：sudo uav-algorithm restart algorithm')
        print('相机未接时等待，不影响其他路。仍需用实际画面/跟踪命令验收。')
    except BaseException:
        # Do not touch units when failure occurred during staging/prechecks.
        if created:
            owned_units = [name for name in units if UNITS / name in created]
            if owned_units:
                ctl('stop', *owned_units, check=False, timeout=60)
                ctl('disable', *owned_units, check=False)
            if nm_loaded:
                rt.run(['nmcli', 'connection', 'delete', 'uuid', c['network_uuid']], check=False)
                if re.fullmatch('[0-9a-fA-F-]{36}', old_nm) and not no_start:
                    rt.run(['nmcli', 'connection', 'up', 'uuid', old_nm], check=False, timeout=40)
            elif not old_ip and rt.has_address(c, rt.addresses()):
                rt.run(['ip', 'address', 'del', c['address'], 'dev', c['iface']], check=False)
            for path in reversed(created):
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
            ctl('daemon-reload', check=False)
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', default='install', choices=('install', 'doctor'))
    parser.add_argument('--bundle', type=Path, default=HERE / 'runtime-bundle.tar.gz')
    parser.add_argument('--sha256', help='来自发布包的运行包 SHA256；默认读取旁边 runtime-bundle.sha256')
    parser.add_argument('--data-root')
    parser.add_argument('--iface', default='eth0')
    parser.add_argument('--address', default='192.168.10.2/24')
    parser.add_argument('--host', default='192.168.10.1')
    parser.add_argument('--user', default='dev')
    parser.add_argument('--front1', default='/dev/v4l/by-path/platform-rkcif-mipi-lvds4-video-index0')
    parser.add_argument('--front2', default='/dev/v4l/by-path/platform-rkcif-mipi-lvds-video-index0')
    parser.add_argument('--infrared', default='/dev/v4l/by-id/usb-CHIPUP_Mino17_Mino17-video-index0')
    for slot in rt.SLOTS:
        parser.add_argument('--' + slot + '-rotate', type=int, default=0)
    parser.add_argument('--without-infrared', action='store_true')
    parser.add_argument('--no-start', action='store_true', help='配置下次开机自启；不改变当前 IP、不启动程序')
    parser.add_argument('--install-deps', action='store_true', help='若缺基础工具，允许 apt 安装；完整离线 BSP 通常不需要')
    return parser.parse_args(argv)


def preflight_arguments(args):
    """Validate explicit inputs and existing service ownership before storage work.

    Does not require the data partition to be mounted yet. The unified installer
    calls this before any common storage/log setup; full checks run again later.
    """
    c = {'iface': args.iface, 'address': args.address, 'host': args.host, 'user': args.user,
         'network_mode': 'direct', 'data_root': args.data_root or '/data'}
    for slot, bitrate in (('front1', 5000), ('front2', 3000), ('infrared', 1500)):
        c[slot] = {'enabled': slot != 'infrared' or not args.without_infrared,
                   'device': getattr(args, slot), 'rotate': getattr(args, slot + '_rotate'), 'bitrate': bitrate}
    rt.validate(c)
    account = pwd.getpwnam(args.user)
    if account.pw_uid == 0 or not re.fullmatch('[a-z_][a-z0-9_-]*', args.user):
        raise ValueError('服务用户须为普通 Linux 用户，例如 dev')
    if args.data_root:
        path = Path(args.data_root)
        if not path.is_absolute() or path.is_symlink() or '..' in path.parts or any(x in str(path) for x in (' ', '%', ':', '\n', '\r')):
            raise ValueError('数据盘路径必须是无空白的真实挂载目录')
    if (BASE / 'installation-full.json').exists():
        return  # Installed receipt/config/runtime will be checked by main().
    fresh_check(c)


def main():
    args = arguments()
    if os.geteuid() != 0:
        raise ValueError('请以 sudo 运行，完整检查相机占用和服务状态')
    parent = parent_installer()
    parent.verify_manifest()
    parent.platform_check()
    installer_digest = bundle.sha256(ROOT / 'MANIFEST.json')
    digest = args.sha256 or args.bundle.with_name('runtime-bundle.sha256').read_text().split()[0]
    manifest = bundle.read_manifest(args.bundle, digest)
    c = config(args, digest, manifest)
    account = pwd.getpwnam(c['user'])
    if account.pw_uid == 0 or not re.fullmatch('[a-z_][a-z0-9_-]*', c['user']):
        raise ValueError('服务用户须为普通 Linux 用户，例如 dev')
    # Existing full installation reuses its proven network plan, without any
    # reload/activation calls or a new randomly-generated connection UUID.
    old = BASE / 'installation-full.json'
    if old.exists():
        previous = json.loads(old.read_text())['config']
        c.update(network_mode=previous['network_mode'], network_uuid=previous['network_uuid'])
        if receipt_intact(c, digest, installer_digest):
            print('同版本同配置已安装；未重启或改动。sudo uav-algorithm status')
            return
    ensure_tools(install=args.install_deps and args.action == 'install')
    c = network_plan(c)
    with open('/run/lock/uav-algorithm-install.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == 'doctor':
            fresh_check(c)
            stage = Path(tempfile.mkdtemp(prefix='.uav-algorithm-doctor-', dir=c['data_root']))
            try:
                bundle.extract(args.bundle, digest, stage)
                check_runtime(stage, manifest, c['user'])
            finally:
                shutil.rmtree(stage, ignore_errors=True)
            print('只读预检查通过；未启动程序、取图或修改网络。选定网络模式：' + c['network_mode'])
        else:
            install(c, args.bundle, digest, manifest, installer_digest, args.no_start, args.install_deps)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError, json.JSONDecodeError, tarfile.TarError) as exc:
        print('未完成：' + str(exc), file=sys.stderr)
        sys.exit(1)
