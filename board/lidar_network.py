#!/usr/bin/env python3
"""Bring up only the explicitly selected E1X Ethernet link; never start capture."""
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys

CONFIG = Path('/etc/uav-lidar-network.json')
MAIN_CONFIG = Path('/opt/uav-switch/config.json')


def run(command, check=True, **kwargs):
    return subprocess.run(command, check=check, capture_output=True, text=True,
                          timeout=kwargs.pop('timeout', 10), **kwargs)


def validate(config):
    c = dict(config)
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', c.get('interface', '')):
        raise ValueError('无效雷达网口')
    host = ipaddress.IPv4Interface(c['host_cidr'])
    radar = ipaddress.IPv4Address(c['lidar_ip'])
    if (host.network.prefixlen > 30 or host.ip in (host.network.network_address, host.network.broadcast_address)
            or host.ip.is_loopback or host.ip.is_multicast or host.ip.is_unspecified
            or radar not in host.network or radar in (host.ip, host.network.network_address,
                                                       host.network.broadcast_address)):
        raise ValueError('主机雷达地址与雷达必须是同网段的不同有效地址')
    c['host_cidr'] = str(host)
    c['lidar_ip'] = str(radar)
    if c.get('nm_uuid') and not re.fullmatch(r'[0-9a-fA-F-]{36}', c['nm_uuid']):
        raise ValueError('无效 NetworkManager UUID')
    return c


def inspect(c, infos=None, routes=None, reserved=()):
    """Reject use of payload/MK22/management links before any network mutation."""
    c = validate(c)
    iface = c['interface']
    protected = set(reserved) | set(c.get('reserved_ifaces', []))
    if MAIN_CONFIG.is_file():
        main = json.loads(MAIN_CONFIG.read_text())
        protected.update(main.get(k) for k in ('payload_iface', 'mk22_iface', 'extension_iface'))
    if iface in protected or iface == 'lo' or (Path('/sys/class/net') / iface / 'wireless').exists():
        raise ValueError('雷达网口不能占用板间、MK22、Wi-Fi 或回环接口：' + iface)
    # ip -4 addr omits interfaces without an IPv4 address. A fresh unplugged J16
    # is exactly that case; enumerate all links, then filter address families.
    infos = infos if infos is not None else json.loads(run(['ip', '-j', 'address', 'show']).stdout)
    routes = routes if routes is not None else json.loads(run(['ip', '-j', '-4', 'route', 'show', 'table', 'all']).stdout)
    own = next((i for i in infos if i.get('ifname') == iface), None)
    if own is None:
        raise ValueError('雷达网口不存在：' + iface)
    if own.get('link_type') not in ('ether', None) or own.get('master') or own.get('linkinfo', {}).get('info_kind'):
        raise ValueError('雷达网口不是独立以太网接口：' + iface)
    target = ipaddress.IPv4Interface(c['host_cidr'])
    for info in infos:
        for addr in info.get('addr_info', []):
            if addr.get('family') != 'inet':
                continue
            existing = ipaddress.IPv4Interface('%s/%s' % (addr['local'], addr['prefixlen']))
            if info['ifname'] == iface:
                if existing.network != target.network:
                    raise ValueError('选定雷达接口已有其他网段地址，拒绝覆盖：' + str(existing))
            elif existing.network.overlaps(target.network):
                raise ValueError('雷达网段与其他接口重叠：' + info['ifname'])
    for route in routes:
        destination = route.get('dst', 'default')
        if route.get('dev') == iface and (destination == 'default' or route.get('gateway')):
            raise ValueError('选定雷达网口承担默认路由或网关路由，拒绝接管')
        if destination != 'default' and route.get('dev') != iface:
            try:
                if ipaddress.IPv4Network(destination, strict=False).overlaps(target.network):
                    raise ValueError('雷达网段已有其他接口的路由：' + destination)
            except ipaddress.AddressValueError:
                continue
    return own


def apply(c):
    c = validate(c)
    own = inspect(c)
    iface = c['interface']
    # Activate only the checked dedicated NM profile; never restart NM or other links.
    # With no carrier NM may refuse activation. The address is still prepared, and
    # the persistent profile plus dispatcher handles a later cable insertion.
    carrier = Path('/sys/class/net') / iface / 'carrier'
    try:
        plugged = carrier.read_text().strip() == '1'
    except OSError:
        plugged = False
    if c.get('nm_uuid') and plugged:
        current = run(['nmcli', '-g', 'GENERAL.CON-UUID', 'device', 'show', iface], check=False)
        if current.returncode or current.stdout.strip() != c['nm_uuid']:
            run(['nmcli', '--wait', '8', 'connection', 'up', 'uuid', c['nm_uuid'], 'ifname', iface], timeout=10)
            own = inspect(c)
    run(['ip', 'link', 'set', 'dev', iface, 'up'])
    addresses = {'%s/%s' % (a['local'], a['prefixlen']) for a in own.get('addr_info', []) if a.get('family') == 'inet'}
    added = c['host_cidr'] not in addresses
    if added:
        run(['ip', 'address', 'add', c['host_cidr'], 'dev', iface])
    return {'interface': iface, 'address': c['host_cidr'], 'address_added': added,
            'carrier': plugged, 'capture_started': False}


def main():
    if os.geteuid() != 0:
        raise ValueError('雷达网络配置需要 root')
    c = validate(json.loads(CONFIG.read_text()))
    if not (Path('/sys/class/net') / c['interface']).exists():
        print('雷达网口暂未枚举，等待设备事件；不启动采集')
        return 0
    print(json.dumps(apply(c), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print('雷达网络未配置：' + str(error), file=sys.stderr)
        sys.exit(1)
