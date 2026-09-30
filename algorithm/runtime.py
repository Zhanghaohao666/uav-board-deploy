#!/usr/bin/env python3
"""Raw video transport for the RK3588 algorithm board; no tracking/control."""
import argparse
import ipaddress
import json
import os
import re
import selectors
from pathlib import Path
import stat
import subprocess
import sys
import time

BASE = Path('/opt/uav-algorithm')
SLOTS = ('front1', 'front2', 'infrared')
PORTS = dict(zip(SLOTS, (5602, 5603, 5604)))


def run(argv, check=True, **kwargs):
    return subprocess.run(argv, check=check, text=True, capture_output=True,
                          timeout=kwargs.pop('timeout', 20), **kwargs)


def validate(c):
    iface = c['iface']
    if not isinstance(iface, str) or not iface or any(x not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-' for x in iface):
        raise ValueError('网口名称无效')
    address = ipaddress.IPv4Interface(c['address'])
    host = ipaddress.IPv4Address(c['host'])
    if address.network.prefixlen != 24 or address.ip in (address.network.network_address, address.network.broadcast_address):
        raise ValueError('算法板地址须为有效的 /24 主机地址')
    if host not in address.network or host in (address.ip, address.network.network_address, address.network.broadcast_address):
        raise ValueError('主控目的 IP 须为同一 /24 子网内的另一有效主机地址')
    seen = set()
    for slot in SLOTS:
        item = c[slot]
        if not isinstance(item['enabled'], bool):
            raise ValueError('enabled 必须是布尔值')
        device = item['device']
        if not isinstance(device, str) or not device.startswith('/dev/') or '..' in Path(device).parts or '\n' in device:
            raise ValueError('相机路径必须在 /dev 下')
        if item['enabled']:
            real = str(Path(device).resolve())
            if real in seen:
                raise ValueError('两路不能使用同一个相机节点：' + device)
            seen.add(real)
        rotations = (0, 180) if slot == 'infrared' else (0, 90, 180, 270)
        if type(item['rotate']) is not int or item['rotate'] not in rotations:
            raise ValueError(slot + ' 旋转参数无效')
        if type(item['bitrate']) is not int or not 128 <= item['bitrate'] <= 20000:
            raise ValueError('码率范围为 128～20000 kbps')
    return c


def load(path=None):
    return validate(json.loads(Path(path or BASE / 'config.json').read_text()))


def addresses():
    return json.loads(run(['ip', '-j', 'address', 'show']).stdout)


def has_address(c, infos):
    addr = ipaddress.IPv4Interface(c['address'])
    found = False
    for info in infos:
        for a in info.get('addr_info', []):
            if a.get('local') == str(addr.ip):
                if info['ifname'] != c['iface'] or a.get('prefixlen') != addr.network.prefixlen:
                    raise ValueError('目标 IP 已配置在其他网口或掩码不同，拒绝覆盖')
                found = True
    return found


def network(c):
    if not Path('/sys/class/net', c['iface']).exists():
        raise ValueError('网口不存在：' + c['iface'])
    infos = addresses()
    # Overlapping routes on a second interface make source selection ambiguous.
    target = ipaddress.IPv4Interface(c['address'])
    for info in infos:
        if info['ifname'] == c['iface']:
            continue
        for a in info.get('addr_info', []):
            if a.get('family') == 'inet' and ipaddress.IPv4Interface('%s/%s' % (a['local'], a['prefixlen'])).network.overlaps(target.network):
                raise ValueError('另一网口占用目标子网：' + info['ifname'])
    present = has_address(c, infos)
    run(['ip', 'link', 'set', 'dev', c['iface'], 'up'])
    if not present:
        # Link negotiation can take several seconds after boot.
        carrier = Path('/sys/class/net', c['iface'], 'carrier')
        for _ in range(10):
            try:
                if carrier.read_text().strip() == '1':
                    break
            except OSError:
                pass
            time.sleep(1)
        else:
            raise ValueError('网线尚未连接；等待网络服务重试')
        check = run(['arping', '-D', '-I', c['iface'], '-c', '3', '-w', '4', str(target.ip)], check=False)
        if check.returncode != 0:
            raise ValueError('目标 IP 冲突或 ARP 检查失败：' + check.stdout + check.stderr)
        run(['ip', 'address', 'add', c['address'], 'dev', c['iface']])
    route = json.loads(run(['ip', '-j', 'route', 'get', c['host'], 'from', str(target.ip)]).stdout)
    if not route or route[0].get('dev') != c['iface']:
        raise ValueError('到主控的路由未通过选定板间网口')
    print('板间地址就绪：%s %s → %s（未清空其他地址或默认路由）' % (c['iface'], c['address'], c['host']), flush=True)


def command(c, slot, base=BASE):
    item = c[slot]
    if slot == 'infrared':
        return [str(base / 'bin/mino17_streamer'), '--device', item['device'],
                '--host', c['host'], '--port', str(PORTS[slot]),
                '--bitrate', str(item['bitrate']), '--rotate', str(item['rotate'])]
    return [str(base / 'bin/rk_streamer'), '--host', c['host'], '--format', 'yuv',
            '--codec', 'h264', '--transport', 'rtp', '--rc', 'cbr', '--fps', '30',
            '--bitrate', str(item['bitrate']), '--gop', '15', '--cam', item['device'],
            '1920x1080', str(PORTS[slot]),
            'input=nv12,format=yuv,rot=%d' % item['rotate']]


def inspect_camera(slot, item):
    path = Path(item['device'])
    if not path.exists():
        return 'missing', '相机未接入：' + str(path)
    if not stat.S_ISCHR(path.stat().st_mode):
        raise ValueError('不是字符设备：' + str(path))
    # MIPI needs a configured media graph; USB can negotiate its advertised mode.
    # Both queries are read-only and do not change a busy camera's current format.
    option = '--list-formats-ext' if slot == 'infrared' else '--get-fmt-video'
    info = run(['v4l2-ctl', '-d', str(path), option]).stdout
    expected = ('UYVY', 1280, 520) if slot == 'infrared' else ('NV12', 1920, 1080)
    if slot == 'infrared':
        # Do not accept a size belonging to a different pixel-format section.
        match = re.search(r"'UYVY'([^']*)", info, re.S)
        valid = match and re.search(r'\b1280x520\b', match.group(1))
    else:
        valid = expected[0] in info and re.search(r'Width/Height\s*:\s*%d\s*/\s*%d\b' % expected[1:], info)
    if not valid:
        raise ValueError('%s 格式不符合该 BSP 的预期 %s %dx%d；请先配置相机驱动/媒体链路\n%s' % (slot, *expected, info))
    return 'ready', str(path.resolve())


def environment(base=BASE):
    env = os.environ.copy()
    env['LD_LIBRARY_PATH'] = str(base / 'lib') + ':' + env.get('LD_LIBRARY_PATH', '')
    return env


def supervise(argv, env, stall_seconds=60):
    """Restart even if the vendor process survives a dead capture thread."""
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env) as child:
        selector = selectors.DefaultSelector()
        selector.register(child.stdout, selectors.EVENT_READ)
        heartbeat = time.monotonic()
        pending = b''
        try:
            while child.poll() is None:
                if time.monotonic() - heartbeat > stall_seconds:
                    raise ValueError('连续 %s 秒没有有效采集帧率报告，停止本路并等待 systemd 重试' % stall_seconds)
                if not selector.select(timeout=min(1, stall_seconds)):
                    continue
                block = os.read(child.stdout.fileno(), 65536)
                if not block:
                    break
                pending += block
                lines = pending.split(b'\n')
                pending = lines.pop()[-65536:]
                for raw in lines:
                    line = raw.decode('utf-8', errors='replace')
                    print(line, flush=True)
                    match = re.search(r'(?:实时 |\[infrared\] )([0-9.]+) fps', line)
                    if match and float(match.group(1)) > 0:
                        heartbeat = time.monotonic()
            raise ValueError('采集程序已退出，返回码：%s' % child.wait(timeout=5))
        finally:
            selector.close()
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('network', 'stream', 'status'))
    p.add_argument('slot', nargs='?', choices=SLOTS)
    p.add_argument('--config')
    a = p.parse_args()
    c = load(a.config)
    if a.action == 'network':
        network(c)
    elif a.action == 'stream':
        if a.slot is None:
            p.error('stream 需要 front1/front2/infrared')
        if not c[a.slot]['enabled']:
            return
        if not has_address(c, addresses()):
            raise ValueError('板间静态 IP 尚未就绪，等待重试')
        route = json.loads(run(['ip', '-j', 'route', 'get', c['host']]).stdout)
        if not route or route[0].get('dev') != c['iface']:
            raise ValueError('到主控的实际发送路由不正确，等待重试')
        state, detail = inspect_camera(a.slot, c[a.slot])
        if state != 'ready':
            raise ValueError(detail)
        argv = command(c, a.slot)
        print('启动纯视频：' + ' '.join(argv), flush=True)
        supervise(['stdbuf', '-oL', '-eL'] + argv, environment())
    else:
        print(json.dumps(c, ensure_ascii=False, indent=2))
        for slot in ('network',) + SLOTS:
            unit = 'uav-algorithm-network.service' if slot == 'network' else 'uav-algorithm@%s.service' % slot
            out = run(['systemctl', 'is-active', unit], check=False)
            print(unit, out.stdout.strip())
        print(run(['ip', '-br', 'address', 'show', c['iface']]).stdout)
        print('进程运行状态不等于已出图；请在主控/地面站实际解码验收。')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
