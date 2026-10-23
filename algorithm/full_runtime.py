#!/usr/bin/env python3
"""Runtime for the complete, verified TTTracker application on a fresh BSP."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import runtime as raw

CONFIG = Path('/etc/uav-front-streams/full.json')
UNITS = {'algorithm': 'uav-front-algorithm.service', 'preview': 'uav-front-preview.service',
         'infrared': 'mino17-streamer.service'}
NETWORK = 'uav-algorithm-network.service'


def validate(c):
    raw.validate(c)
    for key in ('release', 'data_root'):
        value = c[key]
        if not isinstance(value, str) or not value.startswith('/') or any(x in value for x in ('\n', '\r', ' ', '%', ':')) or '..' in Path(value).parts:
            raise ValueError('运行路径无效：' + key)
    try:
        Path(c['release']).relative_to(Path(c['data_root']) / 'uav-algorithm/releases')
    except ValueError:
        raise ValueError('运行目录必须位于数据盘 releases 下')
    if c.get('network_mode') not in ('direct', 'nm-new', 'nm-reuse'):
        raise ValueError('网络模式无效')
    for name in c['library_dirs']:
        if not name.startswith('stream/') or '..' in Path(name).parts or ':' in name:
            raise ValueError('运行库路径无效')
    return c


def load():
    return validate(json.loads(CONFIG.read_text()))


def network(c):
    if c['network_mode'] == 'nm-new':
        # The installer has checked that this is not the SSH/default-route port.
        # Initially disabled in its file so --no-start cannot activate the port
        # when NetworkManager loads a new profile; enable only on actual start.
        raw.run(['nmcli', 'connection', 'modify', 'uuid', c['network_uuid'], 'connection.autoconnect', 'yes'])
        raw.run(['nmcli', 'connection', 'up', 'uuid', c['network_uuid'], 'ifname', c['iface']], timeout=40)
    # A reused NM profile owns the address. Never add a transient address that NM
    # could delete later; let this independent network service retry at boot.
    if c['network_mode'] == 'nm-reuse' and not raw.has_address(c, raw.addresses()):
        raw.run(['nmcli', 'connection', 'up', 'uuid', c['network_uuid'], 'ifname', c['iface']], timeout=40)
    raw.network(c)


def build(c, slot):
    release = Path(c['release'])
    root = release / 'stream'
    tracker = root / 'TTTracker_V1.1_publish'
    env = dict(os.environ)
    # Do not inherit arbitrary development library paths from the caller.
    env['LD_LIBRARY_PATH'] = ':'.join(str(release / name) for name in c['library_dirs'])
    env.update(UAV_LOW_ENABLE='1', UAV_LOW_BIND=str(ipaddress.IPv4Interface(c['address']).ip),
               UAV_LOW_PEER=c['host'], UAV_LOW_CPUS='0-3')
    if slot == 'infrared':
        item = c['infrared']
        env.pop('TRACKER_MULTI_OBJECT', None)
        return ([str(root / 'mino17_streamer'), '--device', item['device'], '--host', c['host'],
                 '--port', '5604', '--bitrate', str(item['bitrate']), '--rotate', str(item['rotate'])], env, root)
    item = c['front1' if slot == 'algorithm' else 'front2']
    fmt, port = ('rgb', 5602) if slot == 'algorithm' else ('yuv', 5603)
    if slot == 'algorithm':
        env.update(TRACKER_MULTI_OBJECT='1', UAV_TRACKER_CPUS='6,7', OPENCV_FOR_THREADS_NUM='8')
    else:
        for key in ('TRACKER_MULTI_OBJECT', 'UAV_TRACKER_CPUS', 'OPENCV_FOR_THREADS_NUM'):
            env.pop(key, None)
    args = [str(root / 'rk_streamer'), '--host', c['host'], '--format', fmt,
            '--codec', 'h264', '--rc', 'cbr', '--fps', '30', '--bitrate', str(item['bitrate']),
            '--cam', item['device'], '1920x1080', str(port),
            'input=nv12,format=%s,bitrate=%d,fps=30,gop=15,rot=%d' % (fmt, item['bitrate'], item['rotate'])]
    return args, env, tracker / 'build' if slot == 'algorithm' else root


def prepare(c, slot):
    """Only touch this service's camera; a missing sibling never blocks it."""
    key = {'algorithm': 'front1', 'preview': 'front2', 'infrared': 'infrared'}[slot]
    item = c[key]
    if not item['enabled']:
        return False
    last = None
    while True:
        try:
            if not os.path.ismount(c['data_root']):
                raise ValueError('数据盘尚未挂载，等待重试')
            if not raw.has_address(c, raw.addresses()):
                raise ValueError('板间地址尚未就绪，等待重试')
            route = json.loads(raw.run(['ip', '-j', 'route', 'get', c['host']]).stdout)
            if not route or route[0].get('dev') != c['iface']:
                raise ValueError('到主控的发送路由不正确，等待重试')
            if not Path(item['device']).exists():
                raise ValueError('相机未接入：' + item['device'])
            # Factory MIPI graph can revert to its default format at each boot.
            if slot != 'infrared':
                raw.run(['v4l2-ctl', '-d', item['device'], '--set-fmt-video=width=1920,height=1080,pixelformat=NV12'])
            state, detail = raw.inspect_camera(key, item)
            if state != 'ready':
                raise ValueError(detail)
            return True
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            detail = str(exc)
            if detail != last:
                print('%s：%s；仅本路等待（30 秒检查一次）' % (slot, detail), flush=True)
                last = detail
            time.sleep(30)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('network', 'stream', 'status', 'restart', 'stop', 'start', 'addresses'))
    parser.add_argument('slot', nargs='?', choices=tuple(UNITS) + ('all',))
    args = parser.parse_args()
    c = load()
    if args.action == 'network':
        network(c)
    elif args.action == 'stream':
        if args.slot not in UNITS:
            parser.error('stream 需要 algorithm、preview 或 infrared')
        if prepare(c, args.slot):
            argv, env, cwd = build(c, args.slot)
            os.chdir(cwd)
            print('启动完整算法板 %s：%s' % (args.slot, ' '.join(argv)), flush=True)
            raw.supervise(['stdbuf', '-oL', '-eL'] + argv, env)
    elif args.action in ('restart', 'stop', 'start'):
        if os.geteuid() != 0:
            raise ValueError('服务操作需要 sudo')
        slots = tuple(UNITS) if args.slot in (None, 'all') else (args.slot,)
        raw.run(['systemctl', args.action] + [UNITS[x] for x in slots], timeout=60)
    else:
        print('算法控制 TCP：%s:9000（主控转发给地面站为 :9001）' % str(ipaddress.IPv4Interface(c['address']).ip))
        for slot, port in (('algorithm', 5602), ('preview', 5603), ('infrared', 5604)):
            print('%s：RTP/H264 → %s:%d；主控 RTSP /%s' % (slot, c['host'], port, slot))
        if args.action == 'status':
            for unit in (NETWORK, *UNITS.values()):
                print(unit, raw.run(['systemctl', 'is-active', unit], check=False).stdout.strip())
            print('应用目录：' + c['release'])
            print('日志：journalctl -u uav-front-algorithm -u uav-front-preview -u mino17-streamer')
            print('服务 active 不代表相机已出图；缺席相机将独立等待，需在地面站解码验收。')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
