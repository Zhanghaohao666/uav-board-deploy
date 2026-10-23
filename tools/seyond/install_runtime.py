#!/usr/bin/env python3
"""Install the pinned E1X recorder and dedicated network on a flashed BSP image.

prepare_install() is read-only. apply_plan() returns a rollback receipt so the
top-level installer can include this operation in its own transaction.
"""
import argparse
from contextlib import ExitStack
import datetime
import fcntl
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / 'tools/seyond/runtime/linux-aarch64'
RUNTIME = Path('/opt/uav-lidar-runtime')
LIDAR_CONFIG = Path('/etc/uav-lidar.json')
NETWORK_CONFIG = Path('/etc/uav-lidar-network.json')
UNIT = 'uav-lidar-network.service'
UNIT_PATH = Path('/etc/systemd/system') / UNIT
NM_PROFILE = Path('/etc/NetworkManager/system-connections/uav-e1x.nmconnection')
NM_DISPATCHER = Path('/etc/NetworkManager/dispatcher.d/91-uav-e1x')
UDEV_RULE = Path('/etc/udev/rules.d/91-uav-e1x.rules')
BACKUPS = Path('/var/backups/uav-lidar-runtime')
NETWORK_SCRIPT = Path('/opt/uav-switch/lidar_network.py')
CONTROL_LOCK = Path('/run/uav-lidar/control.lock')
spec = importlib.util.spec_from_file_location('uav_lidar_network', ROOT / 'board/lidar_network.py')
network = importlib.util.module_from_spec(spec)
spec.loader.exec_module(network)


def run(args, check=True, **kwargs):
    return subprocess.run(args, check=check, capture_output=True, text=True,
                          timeout=kwargs.pop('timeout', 15), **kwargs)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()


def verify_runtime(runtime_dir, check_host=True):
    directory = Path(runtime_dir)
    manifest = json.loads((directory / 'manifest.json').read_text())
    if (manifest.get('schema') != 1 or manifest.get('file') != 'get_pcd_record'
            or manifest.get('arch') != 'aarch64'
            or manifest.get('sdk_version') != '3.103.13'
            or not re.fullmatch('[0-9a-f]{64}', manifest.get('sha256', ''))
            or manifest.get('tcp_initialization_fix') is not True
            or manifest.get('graceful_stop') is not True):
        raise ValueError('雷达采集工具清单不匹配')
    source = directory / manifest['file']
    if source.is_symlink() or not source.is_file() or digest(source) != manifest['sha256']:
        raise ValueError('雷达采集工具 SHA256 校验失败')
    data = source.read_bytes()
    if (len(data) < 64 or data[:6] != b'\x7fELF\x02\x01'
            or struct.unpack_from('<H', data, 18)[0] != 183
            or b'UAV_GRACEFUL_STOP_VERSION=1' not in data
            or b'UAV_RECORD_STOPPED' not in data):
        raise ValueError('采集工具不是经过验证的 ARM64 正常停止版本')
    license_file = ROOT / 'tools/seyond/SEYOND_LICENSE'
    if not license_file.is_file() or b'Redistribution' not in license_file.read_bytes():
        raise ValueError('缺少厂家 SDK 再分发许可证')
    if check_host:
        if os.uname().machine not in ('aarch64', 'arm64'):
            raise ValueError('雷达预编译工具仅支持 ARM64 厂家镜像')
        linked = run(['ldd', str(source)], check=False)
        if linked.returncode or 'not found' in linked.stdout + linked.stderr:
            raise ValueError('雷达工具依赖不兼容：' + linked.stdout + linked.stderr)
        # --version exits without opening the sensor or writing point clouds.
        version = run([str(source), '--version'], check=False)
        if version.returncode or manifest.get('sdk_version', '') not in version.stdout + version.stderr:
            raise ValueError('雷达工具版本运行检查失败：' + version.stdout + version.stderr)
    return manifest, source


def nm_profile(c):
    """Reuse an existing suitable profile; otherwise return a new owned keyfile."""
    active = run(['systemctl', 'is-active', 'NetworkManager.service'], False).returncode == 0
    if not active:
        if run(['systemctl', 'is-active', 'systemd-networkd.service'], False).returncode == 0:
            raise ValueError('该镜像由 systemd-networkd 管理；请先提供专用雷达 .network 配置再部署')
        # Do not compete with a configured ifupdown interface, including include files.
        candidates = [Path('/etc/network/interfaces')]
        candidates += list(Path('/etc/network/interfaces.d').glob('*'))
        for path in candidates:
            if path.is_file() and re.search(r'^\s*iface\s+' + re.escape(c['interface']) + r'\s', path.read_text(), re.M):
                raise ValueError('雷达网口已有 ifupdown 配置，需先确认专用静态配置：' + str(path))
        if run(['systemctl', 'is-enabled', 'NetworkManager.service'], False).returncode == 0:
            raise ValueError('NetworkManager 已配置但未启动；请先启动网络管理器再安装')
        return None, None
    rows = run(['nmcli', '-t', '--escape', 'no', '-f', 'UUID,TYPE', 'connection', 'show']).stdout.splitlines()
    matches = []
    saved_addresses = []
    own = network.inspect(c)
    live_addresses = {'%s/%s' % (a['local'], a['prefixlen']) for a in own.get('addr_info', []) if a.get('family') == 'inet'}
    network_target = ipaddress.IPv4Interface(c['host_cidr']).network
    for row in rows:
        identifier, _, kind = row.partition(':')
        if kind not in ('802-3-ethernet', 'ethernet'):
            continue
        fields = run(['nmcli', '-g', 'connection.interface-name,ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.routes,connection.autoconnect',
                      'connection', 'show', 'uuid', identifier]).stdout.splitlines()
        fields += [''] * (6 - len(fields))
        iface, method, addresses, gateway, routes, autoconnect = fields[:6]
        if iface != c['interface']:
            continue
        parsed = [str(ipaddress.IPv4Interface(a.strip())) for a in addresses.split(',') if a.strip() and a.strip() != '--']
        if gateway not in ('', '--') or routes not in ('', '--'):
            raise ValueError('雷达网口的持久连接包含网关或静态路由，拒绝覆盖：' + identifier)
        if any(ipaddress.IPv4Interface(a).network != network_target for a in parsed):
            raise ValueError('雷达网口已有其他网段的持久配置，拒绝覆盖：' + identifier)
        saved_addresses.extend(parsed)
        if method == 'manual' and c['host_cidr'] in parsed and autoconnect == 'yes' and live_addresses.issubset(parsed):
            matches.append(identifier)
    if matches:
        return matches[0], None
    # A fresh dedicated profile avoids changing vendor files or unrelated connections.
    identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, 'uav-e1x://' + c['interface'] + '/' + c['host_cidr']))
    lines = ['[connection]', 'id=uav-e1x', 'uuid=' + identifier, 'type=ethernet',
             'interface-name=' + c['interface'], 'autoconnect=true', 'autoconnect-priority=999',
             '', '[ethernet]', '', '[ipv4]', 'method=manual', 'never-default=true', 'may-fail=false']
    addresses = [c['host_cidr']]
    for value in saved_addresses + sorted(live_addresses):
        if value not in addresses:
            addresses.append(value)
    lines += ['address%d=%s' % (i, value) for i, value in enumerate(addresses, 1)]
    lines += ['', '[ipv6]', 'method=disabled', '']
    return identifier, '\n'.join(lines).encode()


def prepare_install(interface='eth1', host_cidr='172.168.1.100/24', lidar_ip='172.168.1.10',
                    runtime_dir=None, reserved_ifaces=(), check_host=True):
    c = network.validate(dict(interface=interface, host_cidr=host_cidr, lidar_ip=lidar_ip,
                              reserved_ifaces=list(reserved_ifaces)))
    before = network.inspect(c)
    manifest, source = verify_runtime(runtime_dir or BUNDLE, check_host=check_host)
    previous_config = json.loads(LIDAR_CONFIG.read_text()) if LIDAR_CONFIG.exists() else {}
    if not isinstance(previous_config, dict):
        raise ValueError('已有 /etc/uav-lidar.json 格式错误')
    target = RUNTIME / manifest['sha256'][:16] / 'get_pcd_record'
    reused = False
    for candidate in [previous_config.get('sdk', ''), '/usr/local/bin/seyond-get-pcd-record', str(target)]:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK) and digest(Path(candidate)) == manifest['sha256']:
            target = Path(candidate)
            reused = True
            break
    identifier, keyfile = nm_profile(c)
    if identifier:
        c['nm_uuid'] = identifier
    files = []
    def add(path, data, mode=0o644):
        files.append({'path': str(path), 'data': data, 'mode': mode})
    if not reused:
        add(target, source.read_bytes(), 0o755)
        add(target.parent / 'manifest.json', json_bytes(manifest))
        add(target.parent / 'SEYOND_LICENSE', (ROOT / 'tools/seyond/SEYOND_LICENSE').read_bytes())
    updated = dict(previous_config, sdk=str(target), interface=interface, lidar_ip=lidar_ip)
    add(LIDAR_CONFIG, json_bytes(updated))
    add(NETWORK_CONFIG, json_bytes(c))
    add(NETWORK_SCRIPT, (ROOT / 'board/lidar_network.py').read_bytes())
    add(UNIT_PATH, (ROOT / 'board' / UNIT).read_bytes())
    # A oneshot intentionally becomes inactive after completing. Device/link events
    # can start it again without resetting existing IP addresses or restarting video.
    add(UDEV_RULE, ('ACTION=="add", SUBSYSTEM=="net", KERNEL=="%s", TAG+="systemd", ENV{SYSTEMD_WANTS}+="%s"\n' % (interface, UNIT)).encode())
    if keyfile is not None:
        add(NM_PROFILE, keyfile, 0o600)
    if identifier:
        dispatcher = '#!/bin/sh\n[ "$1" = "%s" ] || exit 0\ncase "$2" in\n up|dhcp4-change|reapply) /bin/systemctl --no-block start %s ;;\nesac\nexit 0\n' % (interface, UNIT)
        add(NM_DISPATCHER, dispatcher.encode(), 0o755)
    return {'files': files, 'network': c, 'before_interface': before,
            'sdk': str(target), 'runtime_reused': reused, 'runtime_sha256': manifest['sha256'],
            'nm_profile_created': keyfile is not None, 'service': UNIT}


def atomic_write(path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.')
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def rollback(receipt):
    """Undo only this installation's owned files/address; never erase other IPs."""
    if receipt.get('rolled_back'):
        return
    run(['systemctl', 'stop', UNIT], False)
    if not receipt['was_enabled']:
        run(['systemctl', 'disable', UNIT], False)
    # Delete the newly loaded profile before restoring an older file at the same
    # path; deleting after restoration could otherwise remove the restored file.
    if receipt.get('nm_loaded') and receipt.get('nm_profile_created'):
        run(['nmcli', 'connection', 'delete', 'uuid', receipt['network']['nm_uuid']], False)
    for entry in reversed(receipt['applied']):
        path = Path(entry['path'])
        if entry['existed']:
            saved = Path(entry['saved'])
            atomic_write(path, saved.read_bytes(), entry['mode'])
        else:
            path.unlink(missing_ok=True)
    if receipt.get('network_attempted'):
        c = receipt['network']
        old_addresses = {'%s/%s' % (a['local'], a['prefixlen']) for a in receipt['before_interface'].get('addr_info', []) if a.get('family') == 'inet'}
        if c['host_cidr'] not in old_addresses:
            run(['ip', 'address', 'del', c['host_cidr'], 'dev', c['interface']], False)
        if 'UP' not in receipt['before_interface'].get('flags', []):
            run(['ip', 'link', 'set', 'dev', c['interface'], 'down'], False)
    if receipt.get('nm_loaded'):
        run(['nmcli', 'connection', 'reload'], False)
    if receipt.get('network_attempted') and receipt.get('previous_nm_uuid'):
        run(['nmcli', '--wait', '8', 'connection', 'up', 'uuid', receipt['previous_nm_uuid'],
             'ifname', receipt['network']['interface']], False)
    run(['systemctl', 'daemon-reload'], False)
    run(['udevadm', 'control', '--reload-rules'], False)
    receipt['rolled_back'] = True


def apply_plan(plan, start=True):
    if os.geteuid() != 0:
        raise ValueError('需要 sudo 安装雷达环境')
    CONTROL_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with CONTROL_LOCK.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with ExitStack() as stack:
            previous = json.loads(LIDAR_CONFIG.read_text()) if LIDAR_CONFIG.exists() else {}
            legacy = Path(previous.get('root', '/data/uav-lidar')) / '.uav-lidar-record.lock'
            if legacy.exists():
                old_lock = stack.enter_context(legacy.open('a'))
                try:
                    fcntl.flock(old_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ValueError('已有旧版点云采集任务，请先停止再更新环境') from None
            return _apply_locked(plan, start)


def _apply_locked(plan, start=True):
    if os.geteuid() != 0:
        raise ValueError('需要 sudo 安装雷达环境')
    before_interface = network.inspect(plan['network'])
    if plan['runtime_reused'] and (not Path(plan['sdk']).is_file() or digest(Path(plan['sdk'])) != plan['runtime_sha256']):
        raise ValueError('预检后既有雷达采集工具发生变化，请重新执行预检')
    active = run(['systemctl', 'show', 'uav-lidar-record.service', '-p', 'ActiveState', '--value'], False).stdout.strip()
    if active in ('active', 'activating', 'deactivating'):
        raise ValueError('雷达正在采集，请先停止再更新环境')
    for path in Path('/proc').glob('[0-9]*/exe'):
        try:
            if Path(os.readlink(path).removesuffix(' (deleted)')).name in ('get_pcd', 'get_pcd_record', 'seyond-get-pcd', 'seyond-get-pcd-record'):
                raise ValueError('已有 SDK 点云采集进程，请先停止再更新环境')
        except OSError:
            pass
    backup = BACKUPS / datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    backup.mkdir(parents=True, mode=0o700)
    receipt = dict(backup=str(backup), applied=[], network=plan['network'],
                   before_interface=before_interface, network_attempted=False,
                   nm_loaded=False, nm_profile_created=plan['nm_profile_created'],
                   was_enabled=run(['systemctl', 'is-enabled', UNIT], False).returncode == 0)
    if plan['network'].get('nm_uuid'):
        previous = run(['nmcli', '-g', 'GENERAL.CON-UUID', 'device', 'show', plan['network']['interface']], False)
        value = previous.stdout.strip()
        if previous.returncode == 0 and re.fullmatch(r'[0-9a-fA-F-]{36}', value):
            receipt['previous_nm_uuid'] = value
    try:
        for entry in plan['files']:
            path = Path(entry['path'])
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise ValueError('安装目标不是普通文件：' + str(path))
            if path.is_file() and path.read_bytes() == entry['data'] and path.stat().st_mode & 0o777 == entry['mode']:
                continue
            record = dict(path=str(path), existed=path.exists(), mode=path.stat().st_mode & 0o777 if path.exists() else 0)
            if path.exists():
                saved = backup / str(path).lstrip('/')
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, saved)
                record['saved'] = str(saved)
            atomic_write(path, entry['data'], entry['mode'])
            receipt['applied'].append(record)
        run(['systemd-analyze', 'verify', str(UNIT_PATH)])
        run(['systemctl', 'daemon-reload'])
        run(['udevadm', 'control', '--reload-rules'])
        if plan['nm_profile_created']:
            receipt['nm_loaded'] = True
            run(['nmcli', 'connection', 'load', str(NM_PROFILE)])
        run(['systemctl', 'enable', UNIT])
        if start:
            receipt['network_attempted'] = True
            run(['systemctl', 'start', UNIT], timeout=25)
        (backup / 'receipt.json').write_bytes(json_bytes(receipt))
        return receipt
    except BaseException:
        rollback(receipt)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interface', default='eth1')
    parser.add_argument('--host-cidr', default='172.168.1.100/24')
    parser.add_argument('--lidar-ip', default='172.168.1.10')
    parser.add_argument('--runtime-dir', type=Path, default=BUNDLE)
    parser.add_argument('--reserved-interface', action='append', default=[])
    parser.add_argument('--no-start', action='store_true')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    plan = prepare_install(args.interface, args.host_cidr, args.lidar_ip, args.runtime_dir, args.reserved_interface)
    print(json.dumps({k: v for k, v in plan.items() if k != 'files'}, ensure_ascii=False, indent=2))
    if not args.check:
        result = apply_plan(plan, not args.no_start)
        print('雷达工具和专用网络自启已安装；没有启动或启用开机采集。备份：' + result['backup'])


if __name__ == '__main__':
    main()
