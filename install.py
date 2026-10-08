#!/usr/bin/env python3
"""Install the UAV relay/capture stack on a fresh RK3588 downward/main board."""
import argparse
import datetime
import fcntl
import grp
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
BASE = Path('/opt/uav-switch')
UNITS = Path('/etc/systemd/system')
STATE = Path('/var/lib/uav-switch/active.json')
LAUNCHER = Path('/usr/local/bin/uav-switch')
RECEIPT = 'installation.json'
BACKUPS = Path('/var/backups/uav-switch')
BOOT = 'uav-switch-boot.service'
TEMPLATE = 'uav-switch@.service'
PACKAGES = ['python3', 'ffmpeg', 'socat', 'iproute2', 'iputils-ping', 'iputils-arping',
            'psmisc', 'v4l-utils', 'gstreamer1.0-tools', 'gstreamer1.0-plugins-base',
            'gstreamer1.0-plugins-good', 'libgstrtspserver-1.0-0', 'libatomic1']
spec = importlib.util.spec_from_file_location('manager', ROOT / 'board/manager.py')
manager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager)


def run(command, check=True, **kwargs):
    return subprocess.run(command, check=check, text=True, capture_output=True, **kwargs)


def verify_manifest():
    manifest = json.loads((ROOT / 'MANIFEST.json').read_text())
    for name, digest in manifest.items():
        path = ROOT / name
        if path.is_symlink() or ROOT not in path.resolve().parents:
            raise ValueError('不允许的安装包路径：' + name)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('安装包校验失败：' + name)
    required = {'install.py', 'install.sh', 'board/manager.py', 'board/board_menu.py',
                'board/uav_switch.sh', 'board/' + BOOT, 'board/' + TEMPLATE}
    required.update(str(p.relative_to(ROOT)) for p in (ROOT / 'board').rglob('*') if p.is_file() and '__pycache__' not in p.parts)
    if not required.issubset(manifest):
        raise ValueError('校验清单缺少程序文件')


def platform_check():
    if os.uname().machine not in ('aarch64', 'arm64'):
        raise ValueError('仅支持 RK3588 ARM64 主控/下视板，不能在电脑或 RK3576 云台上安装')
    compatible = Path('/proc/device-tree/compatible')
    if not compatible.exists() or b'rockchip,rk3588' not in compatible.read_bytes():
        raise ValueError('没有识别到 RK3588 BSP')
    if not Path('/run/systemd/system').is_dir():
        raise ValueError('需要 systemd 系统')


def choose(label, choices, specified=None, preferred=None, interactive=True):
    choices = list(dict.fromkeys(choices))
    if specified is not None:
        if specified not in choices:
            raise ValueError(label + '不可用：' + specified + '；可选：' + ', '.join(choices))
        return specified
    if len(choices) == 1:
        print(label + '：' + choices[0])
        return choices[0]
    if not choices:
        raise ValueError('没有找到' + label)
    if not interactive:
        raise ValueError(label + '有多个候选，请显式指定：' + ', '.join(choices))
    print(label + '：')
    for index, item in enumerate(choices, 1):
        print('  {} {}'.format(index, item))
    default = choices.index(preferred) + 1 if preferred in choices else 1
    while True:
        answer = input('选择编号 [{}]：'.format(default)).strip() or str(default)
        if answer.isdigit() and 1 <= int(answer) <= len(choices):
            return choices[int(answer) - 1]


def interfaces():
    return json.loads(run(['ip', '-j', 'address', 'show']).stdout)


def ethernet_names(infos):
    return [i['ifname'] for i in infos if i.get('link_type') == 'ether'
            and not (Path('/sys/class/net') / i['ifname'] / 'wireless').exists()
            and not i['ifname'].startswith(('wl', 'docker', 'veth', 'br-', 'virbr'))]


def discover_rgb():
    candidates = []
    # Some RealSense by-id names collide between interfaces. Check actual format,
    # never infer RGB solely from video-index0 or copy another camera's serial.
    paths = list(Path('/dev/v4l/by-id').glob('*RealSense*'))
    paths += list(Path('/dev/v4l/by-path').glob('*usb*-video-index*'))
    seen = set()
    for path in sorted(paths):
        real = str(path.resolve())
        if real in seen:
            continue
        info = run(['v4l2-ctl', '-d', str(path), '--all'], check=False).stdout
        if 'RealSense' not in info and 'RealSense' not in path.name:
            continue
        formats = run(['v4l2-ctl', '-d', str(path), '--list-formats-ext'], check=False).stdout
        if "'YUYV'" in formats and '640x480' in formats:
            candidates.append(str(path)); seen.add(real)
    return candidates


def timeout_option(help_text):
    if re.search(r'^\s*-stimeout\s', help_text, re.M):
        return '-stimeout'
    if re.search(r'^\s*-timeout\s.*microseconds', help_text, re.M):
        return '-timeout'
    raise ValueError('ffmpeg 缺少可识别的 RTSP 微秒超时选项')


def make_config(args):
    if args.config:
        return validate_config(json.loads(Path(args.config).read_text()))
    interactive = not args.yes and sys.stdin.isatty()
    mode = choose('部署方案', list(manager.MODES), args.mode, 'algorithm', interactive)
    infos = interfaces()
    nets = ethernet_names(infos)
    for info in infos:
        if info['ifname'] in nets:
            print('网卡 {}  MAC {}  IPv4 {}'.format(info['ifname'], info.get('address', ''),
                  ','.join(a['local'] for a in info.get('addr_info', []) if a.get('family') == 'inet')))
    payload = choose('板间网口', nets, args.payload_iface, 'eth0', interactive)
    mk_candidates = [n for n in nets if n != payload]
    mk = choose('MK22 网口', mk_candidates, args.mk22_iface, None, interactive)
    extension = 'unused0'
    if mode == 'dual' or args.extension_iface:
        extension = choose('云台扩展网口', [n for n in nets if n not in (payload, mk)],
                           args.extension_iface, None, interactive)
    address = args.mk22_address
    if address is None:
        if not interactive:
            raise ValueError('请用 --mk22-address 指定主控图传地址，例如 192.168.2.36/24')
        address = input('本板 MK22 地址 [192.168.2.36/24]（同一网络多板须不同）：').strip() or '192.168.2.36/24'
    user = args.user or os.environ.get('SUDO_USER')
    if not user or user == 'root':
        try:
            pwd.getpwnam('dev'); user = 'dev'
        except KeyError:
            if not interactive:
                raise ValueError('请用 --user 指定运行采集服务的现有普通用户')
            user = input('运行采集服务的现有用户名：').strip()
    board_camera = args.board_camera or '/dev/v4l/by-path/platform-rkcif-mipi-lvds2-video-index0'
    d455 = not args.without_d455
    rgb = ''
    if d455:
        rgb = args.d455_rgb or choose('D455 彩色设备', discover_rgb(), interactive=interactive)
    result = run(['/usr/bin/ffmpeg', '-hide_banner', '-h', 'demuxer=rtsp'])
    return validate_config(dict(default_mode=mode, payload_iface=payload, extension_iface=extension,
        mk22_iface=mk, mk22_address=address, service_user=user, board_camera=board_camera,
        d455_enabled=d455, d455_depth_enabled=False, d455_rgb=rgb, d455_depth='',
        ffmpeg_timeout_option=timeout_option(result.stdout + result.stderr)))


def validate_config(c):
    if c.get('default_mode') not in manager.MODES:
        raise ValueError('无效部署方案')
    for key in ('payload_iface', 'extension_iface', 'mk22_iface'):
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', c.get(key, '')):
            raise ValueError('无效网卡名：' + key)
    if len({c[k] for k in ('payload_iface', 'extension_iface', 'mk22_iface')}) != 3:
        raise ValueError('板间、扩展、图传网口不能重复')
    ip = ipaddress.IPv4Interface(c['mk22_address'])
    if ip.ip in (ip.network.network_address, ip.network.broadcast_address):
        raise ValueError('图传地址不能是网络或广播地址')
    for subnet in ('192.168.10.0/24', '192.168.144.0/24', '127.0.0.0/8'):
        if ip.network.overlaps(ipaddress.IPv4Network(subnet)):
            raise ValueError('图传网段与板间或回环网段重叠')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]*', c.get('service_user', '')):
        raise ValueError('无效服务用户名')
    if type(c.get('d455_enabled')) is not bool or c.get('d455_depth_enabled') is not False:
        raise ValueError('本包仅支持 D455 彩色预览，d455_depth_enabled 必须为 false')
    for key in ['board_camera'] + (['d455_rgb'] if c['d455_enabled'] else []):
        path = c.get(key, '')
        if not path.startswith('/dev/') or '\n' in path or '..' in Path(path).parts:
            raise ValueError('无效相机路径：' + key)
    if c.get('ffmpeg_timeout_option') not in ('-stimeout', '-timeout'):
        raise ValueError('无效 ffmpeg 超时选项')
    return c


def render_units(c):
    return {BOOT: (ROOT / 'board' / BOOT).read_text(),
            'uav-record@.service': (ROOT / 'board/uav-record@.service').read_text(),
            TEMPLATE: (ROOT / 'board' / TEMPLATE).read_text().replace('User=dev\n', 'User=' + c['service_user'] + '\n')}


def doctor(c):
    errors = []
    try:
        platform_check()
        manager.netplan(c, c['default_mode'], interfaces())
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        errors.append(str(error))
    for group in ('video', 'render'):
        try: grp.getgrnam(group)
        except KeyError: errors.append('缺少系统设备组：' + group)
    try: pwd.getpwnam(c['service_user'])
    except KeyError: errors.append('用户不存在：' + c['service_user'])
    for name in ('ffmpeg', 'socat', 'fuser', 'v4l2-ctl', 'gst-inspect-1.0', 'arping'):
        if not shutil.which(name): errors.append('缺少命令：' + name)
    for dev in ['/dev/mpp_service', '/dev/rga', c['board_camera']] + ([c['d455_rgb']] if c['d455_enabled'] else []):
        if not Path(dev).exists(): errors.append('设备不存在：' + dev)
    env = dict(os.environ, LD_LIBRARY_PATH=str(ROOT / 'board/lib'))
    for path in (ROOT / 'board/bin').iterdir():
        result = run(['ldd', str(path)], check=False, env=env)
        if result.returncode or 'not found' in result.stdout:
            errors.append(path.name + ' 动态库缺失：' + result.stdout + result.stderr)
    if shutil.which('gst-inspect-1.0'):
        for plugin in ('udpsrc', 'rtph264depay', 'rtph264pay', 'rtpjitterbuffer', 'h264parse'):
            if run(['gst-inspect-1.0', plugin], check=False).returncode:
                errors.append('缺少 GStreamer 插件：' + plugin)
    if shutil.which('v4l2-ctl'):
        if Path(c['board_camera']).exists():
            result = run(['v4l2-ctl', '-d', c['board_camera'], '--get-fmt-video'], check=False)
            if result.returncode or "'NV12'" not in result.stdout:
                errors.append('下视 MIPI 当前未配置为 NV12；请先完成同型号 BSP 相机初始化')
        if c['d455_enabled'] and Path(c['d455_rgb']).exists():
            result = run(['v4l2-ctl', '-d', c['d455_rgb'], '--list-formats-ext'], check=False)
            if "'YUYV'" not in result.stdout or '640x480' not in result.stdout:
                errors.append('D455 节点没有 YUYV 640x480 彩色格式')
    if shutil.which('ffmpeg'):
        result = run(['/usr/bin/ffmpeg', '-hide_banner', '-h', 'demuxer=rtsp'], check=False)
        try:
            if timeout_option(result.stdout + result.stderr) != c['ffmpeg_timeout_option']:
                errors.append('配置中的 ffmpeg 超时选项与本机不符')
        except ValueError as error: errors.append(str(error))
    return errors


def assert_fresh():
    paths = [BASE, STATE, LAUNCHER, LAUNCHER.with_name('uav-record')] + [UNITS / name for name in (BOOT, TEMPLATE, 'uav-record@.service')]
    for path in paths:
        if path.exists() or path.is_symlink():
            raise ValueError('发现已有部署，保持原状：' + str(path) + '；切换请运行 uav-switch')
    # Do not compete with old boot jobs even when they happen to be stopped.
    for pattern in ('uav-video*', 'uav-gimbal*', 'uav-fixed*', 'mino17-streamer*'):
        result = run(['systemctl', 'list-unit-files', pattern, '--no-legend', '--no-pager'])
        if result.stdout.strip():
            raise ValueError('发现旧服务，请先迁移，不自动覆盖：' + result.stdout.strip())


def same_install(c):
    path = BASE / RECEIPT
    if not path.is_file(): return False
    receipt = json.loads(path.read_text())
    if receipt.get('config') != c or not (BASE / 'config.json').is_file(): return False
    if json.loads((BASE / 'config.json').read_text()) != c: return False
    for rel, digest in receipt.get('files', {}).items():
        source = ROOT / rel
        target = BASE / Path(rel).relative_to('board')
        if not source.is_file() or not target.is_file(): return False
        if hashlib.sha256(source.read_bytes()).hexdigest() != digest: return False
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest: return False
    if not receipt.get('files'): return False
    return all((UNITS / name).exists() and (UNITS / name).read_text() == content
               for name, content in render_units(c).items()) and LAUNCHER.is_file() and LAUNCHER.read_bytes() == (ROOT / 'board/uav_switch.sh').read_bytes() and LAUNCHER.with_name('uav-record').is_file() and LAUNCHER.with_name('uav-record').read_bytes() == (ROOT / 'board/uav_record.sh').read_bytes()


def check_duplicate_address(c):
    existing = manager.addrmap(interfaces()).get(c['mk22_iface'], set())
    if c['mk22_address'] in existing: return
    # ARP DAD temporarily brings up an unconfigured link, then restores its state.
    info = next(i for i in interfaces() if i['ifname'] == c['mk22_iface'])
    was_down = 'UP' not in info.get('flags', [])
    if was_down: run(['ip', 'link', 'set', 'dev', c['mk22_iface'], 'up'])
    try:
        result = run(['arping', '-D', '-c', '2', '-w', '3', '-I', c['mk22_iface'],
                      str(ipaddress.IPv4Interface(c['mk22_address']).ip)], check=False)
    finally:
        if was_down: run(['ip', 'link', 'set', 'dev', c['mk22_iface'], 'down'])
    if result.returncode:
        raise ValueError('图传地址冲突或地址探测失败：' + result.stdout + result.stderr)


def restore_network(before, c):
    errors = []
    after = manager.addrmap(interfaces())
    old = manager.addrmap(before)
    controlled = manager.OWNED | {c['mk22_address']}
    for iface in (c['payload_iface'], c['extension_iface'], c['mk22_iface']):
        for operation, values in [('del', after.get(iface, set()) - old.get(iface, set())),
                                  ('add', old.get(iface, set()) - after.get(iface, set()))]:
            for address in sorted(values & controlled):
                result = run(['ip', 'addr', operation, address, 'dev', iface], check=False)
                if result.returncode: errors.append(result.stderr)
    return errors


def install(c, start=True):
    if os.geteuid() != 0: raise ValueError('安装需要 sudo')
    if same_install(c):
        print('相同版本与配置已经安装；保留现有服务和开机状态，不重复启动。')
        return
    assert_fresh()
    problems = doctor(c)
    if problems: raise ValueError('\n'.join(problems))
    manager.ownership_check(c, c['default_mode'])
    # UDP sources are also reserved; a foreign receiver must not be stolen.
    for port in (5602, 5603, 5604, 5610, 5611, 5612, 5613, 5702, 5703):
        if run(['fuser', '-n', 'udp', str(port)], check=False).stdout.strip():
            raise ValueError('UDP 端口已被占用：' + str(port))
    check_duplicate_address(c)
    before = interfaces()
    backup = BACKUPS / datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    backup.mkdir(parents=True)
    (backup / 'network-before.json').write_text(json.dumps(before, indent=2))
    (backup / 'config.json').write_text(json.dumps(c, indent=2))
    files = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in (ROOT / 'board').rglob('*') if p.is_file() and '__pycache__' not in p.parts}
    created = []
    copied = False
    previous_signal = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        copied = True
        shutil.copytree(ROOT / 'board', BASE, ignore=shutil.ignore_patterns('__pycache__'))
        (BASE / 'config.json').write_text(json.dumps(c, ensure_ascii=False, indent=2) + '\n')
        for path in (BASE / 'bin').iterdir(): path.chmod(0o755)
        (BASE / 'uav_switch.sh').chmod(0o755)
        for name, content in render_units(c).items():
            path = UNITS / name
            path.write_text(content); created.append(path)
        shutil.copy2(BASE / 'uav_switch.sh', LAUNCHER); created.append(LAUNCHER)
        LAUNCHER.chmod(0o755)
        recorder_launcher = LAUNCHER.with_name('uav-record')
        shutil.copy2(BASE / 'uav_record.sh', recorder_launcher); created.append(recorder_launcher)
        recorder_launcher.chmod(0o755)
        run(['systemd-analyze', 'verify'] + [str(UNITS / n) for n in render_units(c)])
        run(['systemctl', 'daemon-reload'])
        run(['systemctl', 'enable', BOOT])
        if start:
            run(['systemctl', 'start', BOOT], timeout=110)
            time.sleep(2)
            for key in manager.services(c, c['default_mode']):
                if run(['systemctl', 'is-active', 'uav-switch@' + key + '.service'], check=False).returncode:
                    raise ValueError('服务启动失败：' + key)
        (BASE / RECEIPT).write_text(json.dumps({'config': c, 'files': files, 'backup': str(backup)}, indent=2))
    except Exception as error:
        failures = []
        for command in (['systemctl', 'disable', '--now', BOOT],
                        ['systemctl', 'stop'] + manager.names(manager.services(c, c['default_mode']))):
            result = run(command, check=False)
            if result.returncode: failures.append(result.stderr)
        failures += restore_network(before, c)
        for path in created: path.unlink(missing_ok=True)
        if copied and BASE.exists(): shutil.rmtree(BASE)
        STATE.unlink(missing_ok=True)
        run(['systemctl', 'daemon-reload'], check=False)
        raise RuntimeError('安装失败，已尝试撤销本次部署：' + str(error) +
                           ('\n回退异常：' + '\n'.join(failures) if failures else '')) from error
    finally:
        signal.signal(signal.SIGINT, previous_signal)
    print('安装完成；开机默认：' + c['default_mode'] + '；板端菜单：uav-switch')
    print('安装记录：' + str(backup))
    if start:
        result = run(['/usr/bin/python3', str(BASE / 'manager.py'), 'verify'], check=False, timeout=65)
        print(result.stdout or result.stderr)
        if result.returncode:
            print('安装与自启已完成，但部分视频未出图；请检查相机和载荷接线，运行 uav-switch 选择 5 复查。')
    else:
        print('尚未启动服务；下次开机生效，也可 sudo systemctl start ' + BOOT)


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', choices=['install', 'doctor', 'configure'], default='install')
    parser.add_argument('--mode', choices=manager.MODES)
    parser.add_argument('--payload-iface')
    parser.add_argument('--mk22-iface')
    parser.add_argument('--extension-iface')
    parser.add_argument('--mk22-address')
    parser.add_argument('--user')
    parser.add_argument('--board-camera')
    parser.add_argument('--d455-rgb')
    parser.add_argument('--without-d455', action='store_true')
    parser.add_argument('--config', help='使用已经生成的 JSON 配置')
    parser.add_argument('--output', default='board-config.json')
    parser.add_argument('--yes', action='store_true', help='无交互；不明确的硬件选择仍会报错')
    parser.add_argument('--no-start', action='store_true', help='安装并启用下次开机，不立即启动')
    return parser.parse_args()


def main():
    args = arguments()
    verify_manifest()
    platform_check()
    c = make_config(args)
    print(json.dumps(c, ensure_ascii=False, indent=2))
    if args.action == 'configure':
        Path(args.output).write_text(json.dumps(c, ensure_ascii=False, indent=2) + '\n')
        return 0
    if args.action == 'doctor':
        problems = doctor(c)
        print('\n'.join(problems) if problems else '硬件、依赖、网络配置检查通过（未安装、未改变运行状态）。')
        return 1 if problems else 0
    if os.geteuid() != 0: raise ValueError('请用 sudo bash install.sh 执行安装')
    with open('/run/uav-switch-install.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with open('/run/uav-switch.lock', 'w') as mode_lock:
            # Do not hold this while systemd invokes manager.boot (same lock).
            fcntl.flock(mode_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if not same_install(c): assert_fresh()
        install(c, not args.no_start)
    return 0


if __name__ == '__main__':
    try: sys.exit(main())
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print('失败：' + str(error), file=sys.stderr); sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        print('\n已取消。', file=sys.stderr); sys.exit(1)
