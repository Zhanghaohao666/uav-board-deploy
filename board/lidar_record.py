#!/usr/bin/env python3
"""Manual E1X raw recording, owned by systemd and independent of video/SSH."""
import argparse
from contextlib import contextmanager
import datetime
import fcntl
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

MOUNT = Path('/data')
RUN = Path('/run/uav-lidar')
CONFIG = Path('/etc/uav-lidar.json')
UNIT = 'uav-lidar-record.service'
RESERVE = 10 * 1024 ** 3
NO_DATA_SECONDS = 12
ACTIVE = ('starting', 'recording', 'stopping')
DEFAULT = dict(lidar_ip='172.168.1.10', interface='eth1', port=8010,
               root='/data/uav-lidar', sdk='/usr/local/bin/seyond-get-pcd',
               segment_seconds=60, max_seconds=86400)


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


def boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(value, out, ensure_ascii=False, indent=2)
            out.write('\n'); out.flush(); os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


def read_json(path):
    try:
        result = json.loads(path.read_text())
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


def config():
    result = DEFAULT.copy()
    if CONFIG.exists():
        supplied = json.loads(CONFIG.read_text())
        if not isinstance(supplied, dict) or set(supplied) - set(DEFAULT):
            raise ValueError('雷达配置字段不正确：/etc/uav-lidar.json')
        result.update(supplied)
    try:
        address = ipaddress.IPv4Address(result['lidar_ip'])
        if address.is_multicast or address.is_unspecified or address.is_loopback:
            raise ValueError('address')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', result['interface']):
            raise ValueError('interface')
        for key, lower, upper in [('port', 1, 65535), ('segment_seconds', 5, 300),
                                  ('max_seconds', 5, 86400)]:
            if type(result[key]) is not int or not lower <= result[key] <= upper:
                raise ValueError(key)
        root = Path(result['root'])
        sdk = Path(result['sdk'])
        if (not root.is_absolute() or MOUNT not in root.parents or '..' in root.parts
                or not sdk.is_absolute() or '..' in sdk.parts):
            raise ValueError('path')
    except (ValueError, TypeError):
        raise ValueError('雷达配置中的 IP、网口、路径或分段参数不正确') from None
    return result


@contextmanager
def control_lock():
    RUN.mkdir(parents=True, exist_ok=True)
    with (RUN / 'control.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def ctl(*args):
    return subprocess.run(['systemctl', *args], capture_output=True, text=True, timeout=8)


def service_active():
    value = ctl('show', UNIT, '-p', 'ActiveState', '--value')
    return value.returncode == 0 and value.stdout.strip() in ('active', 'activating', 'deactivating')


def storage(c):
    root = Path(c['root'])
    if not os.path.ismount(MOUNT):
        raise ValueError('/data 数据盘未挂载，不能把点云写入系统盘')
    if root.is_symlink() or MOUNT.resolve() not in root.resolve().parents:
        raise ValueError('雷达保存目录必须位于 /data 数据盘内')
    if root.exists() and root.stat().st_dev != MOUNT.stat().st_dev:
        raise ValueError('雷达保存目录不在 /data 的同一数据盘')
    if shutil.disk_usage(MOUNT).free < RESERVE:
        raise ValueError('数据盘余量不足 10 GiB，已停止采集；历史数据不会自动删除')


def prerequisites(c):
    storage(c)
    if not os.access(c['sdk'], os.X_OK) or not Path(c['sdk']).is_file():
        raise ValueError('缺少厂家采集工具 seyond-get-pcd，请安装兼容 E1X 的 SDK 工具')
    try:
        carrier = Path('/sys/class/net') / c['interface'] / 'carrier'
        if carrier.read_text().strip() != '1':
            raise ValueError('雷达网口 %s 没有物理链路，请检查供电和接线' % c['interface'])
    except OSError:
        raise ValueError('雷达网口 %s 不存在或尚未连通' % c['interface']) from None
    route = subprocess.run(['ip', '-j', 'route', 'get', c['lidar_ip']],
                           capture_output=True, text=True, timeout=3)
    try:
        routes = json.loads(route.stdout)
        good = route.returncode == 0 and routes[0].get('dev') == c['interface']
    except (ValueError, IndexError, TypeError):
        good = False
    if not good:
        raise ValueError('雷达 %s 的路由未指向 %s；请先配置雷达网口' % (c['lidar_ip'], c['interface']))


def frame_rate(c):
    # Never use system proxy settings for a device on the board's direct link.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = 'http://%s:%d/command/?get_frame_rate' % (c['lidar_ip'], c['port'])
    try:
        with opener.open(url, timeout=3) as response:
            value = float(response.read(128).decode().strip())
        if not math.isfinite(value) or not 0 < value <= 200:
            raise ValueError('frame rate')
        return value
    except (OSError, ValueError):
        raise ValueError('雷达没有返回有效帧率，请确认已通电且 %s:%d 可访问' %
                         (c['lidar_ip'], c['port'])) from None


def legacy_busy():
    # Linux comm is limited to 15 characters; pgrep -x misses the dedicated
    # seyond-get-pcd-record binary. Resolve executable names instead.
    for entry in Path('/proc').glob('[0-9]*/exe'):
        try:
            name = Path(os.readlink(entry).removesuffix(' (deleted)')).name
            if name in ('get_pcd', 'get_pcd_record', 'seyond-get-pcd', 'seyond-get-pcd-record'):
                return True
        except OSError:
            pass
    return False


def current(c):
    value = read_json(RUN / 'current.json')
    # /run is cleared by a reboot. Persisted last session is only for display,
    # never a request to resume recording after a reboot.
    if not value and os.path.ismount(MOUNT):
        value = read_json(Path(c['root']) / 'latest.json')
    return value


def persist(value):
    atomic(RUN / 'current.json', value)
    c = value['config']
    # Never fall back to rootfs after data mount loss.
    if os.path.ismount(MOUNT) and Path(c['root']).resolve().is_relative_to(MOUNT.resolve()):
        atomic(Path(c['root']) / 'latest.json', value)
        if value.get('path'):
            atomic(Path(value['path']) / 'session.json', value)


def snapshot():
    base = dict(supported=True, phase='idle', active=False, detail='', path='', bytes=0,
                started_at='', lidar_ip=DEFAULT['lidar_ip'], interface=DEFAULT['interface'],
                available=False, error='')
    try:
        c = config()
        base.update(lidar_ip=c['lidar_ip'], interface=c['interface'])
        value = current(c)
        active = service_active()
        if value:
            base.update({k: value[k] for k in ('phase', 'detail', 'path', 'bytes', 'started_at', 'error')
                         if k in value})
            same_boot = value.get('boot_id') == boot_id()
            recent = same_boot and 0 <= time.monotonic() - value.get('monotonic', 0) < 12
            if not same_boot and base['phase'] in ACTIVE:
                base.update(phase='error', error='上次采集因重启中断，已有文件保留；末段可能不完整',
                            detail='开机不会自动恢复点云采集')
            elif base['phase'] in ACTIVE and not active:
                base.update(phase='error', error='采集进程已停止，已有文件保留；末段可能不完整')
            elif active and base['phase'] in ACTIVE and not recent:
                base.update(phase='error', error='采集状态已超过 12 秒未更新，请停止后检查')
            base['active'] = active
            if base['active'] and read_json(RUN / 'stop.json').get('session') == value.get('session'):
                base.update(phase='stopping', detail='正在停止采集并保留已写入的数据')
        elif active:
            base.update(active=True, phase='error', error='存在采集服务但状态丢失，请先停止')
        try:
            prerequisites(c)
            base['available'] = True
            if base['phase'] == 'idle': base['detail'] = '准备就绪，开始后验证雷达数据；最长连续采集 24 小时'
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            if not base['error']: base['error'] = str(error)
            if base['phase'] == 'idle': base['detail'] = str(error)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        base.update(phase='error', error=str(error))
    return base


def start(seconds=None):
    with control_lock():
        c = config()
        if service_active():
            return  # Idempotent: never replace an in-flight session.
        prerequisites(c)
        if legacy_busy():
            raise ValueError('已有厂家 SDK 采集进程，请先结束原采集任务')
        if seconds is None: seconds = c['max_seconds']
        if type(seconds) is not int or not 1 <= seconds <= c['max_seconds']:
            raise ValueError('采集时长必须为 1～%d 秒' % c['max_seconds'])
        root = Path(c['root'])
        root.mkdir(parents=True, exist_ok=True)
        # Compatible with the pre-existing standalone recorder's lock.
        with (root / '.uav-lidar-record.lock').open('a') as lock:
            try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('已有点云采集任务，请先结束原采集任务') from None
        value = dict(session=uuid.uuid4().hex, config=c, seconds=seconds,
                     boot_id=boot_id(), phase='starting', detail='正在连接雷达并等待原始点云',
                     active=True, path='', bytes=0, started_at=now(), error='', monotonic=time.monotonic())
        persist(value)
        result = ctl('start', UNIT)
        if result.returncode:
            value.update(phase='error', active=False, error='无法启动雷达采集服务：' + result.stderr[-300:])
            persist(value)
            raise ValueError(value['error'])


def stop():
    with control_lock():
        c = config()
        if not service_active(): return
        value = current(c)
        if value:
            value.update(phase='stopping', detail='正在停止采集并保留已写入的数据', monotonic=time.monotonic())
            # Worker exclusively updates persisted session; marker does not race
            # with its current.json writes and is polled in addition to SIGTERM.
            atomic(RUN / 'stop.json', {'session': value['session']})
        result = ctl('stop', '--no-block', UNIT)
        if result.returncode: raise ValueError('无法停止雷达采集服务：' + result.stderr[-300:])


def files(folder):
    return sorted((p for p in folder.glob('*.inno_pc') if p.is_file()),
                  key=lambda p: int(re.search(r'-(\d+)\.inno_pc$', p.name).group(1))
                  if re.search(r'-(\d+)\.inno_pc$', p.name) else -1)


class LimitedLog:
    """Keep at most 2 MiB per session while continuously draining SDK output."""
    LIMIT = 1024 * 1024
    def __init__(self, folder): self.path = folder / 'capture.log'
    def write(self, chunk):
        if self.path.exists() and self.path.stat().st_size + len(chunk) > self.LIMIT:
            os.replace(self.path, self.path.with_suffix('.log.1'))
        with self.path.open('ab') as out: out.write(chunk[-self.LIMIT:])


class RawObserver:
    """Inspect complete packets, excluding the compact E1X angle table (type101)."""
    def __init__(self):
        self.offsets = {}
        self.point_packets = 0
    def inspect(self, paths):
        for path in paths:
            offset = self.offsets.get(str(path), 0)
            size = path.stat().st_size
            if offset >= size: continue
            with path.open('rb') as source:
                while offset + 54 <= size:
                    source.seek(offset)
                    header = source.read(54)
                    magic = struct.unpack_from('<H', header, 0)[0]
                    packet_size = struct.unpack_from('<I', header, 10)[0]
                    if magic not in (0x176a, 0x186b) or not 26 <= packet_size <= 10 * 1024 ** 2:
                        raise ValueError('原始点云包头异常：' + path.name)
                    if offset + packet_size > size: break
                    count = int.from_bytes(header[39:42], 'little')
                    if magic == 0x176a and header[38] == 16 and count > 0 and packet_size > 54:
                        self.point_packets += 1
                    offset += packet_size
            self.offsets[str(path)] = offset


def stop_child(child, log):
    """Drain stdout during graceful shutdown so a full pipe cannot block flush."""
    if child is None: return b''
    if child.poll() is None: child.terminate()
    deadline = time.monotonic() + 8
    tail = b''
    while time.monotonic() < deadline:
        try:
            chunk = os.read(child.stdout.fileno(), 65536)
            if chunk:
                log.write(chunk); tail = (tail + chunk)[-8192:]
            elif child.poll() is not None: break
        except BlockingIOError:
            if child.poll() is not None: break
            time.sleep(0.02)
    if child.poll() is None:
        child.kill(); child.wait(timeout=2)
    return tail


def capture(value, requested_stop):
    c = value['config']
    prerequisites(c)
    if legacy_busy(): raise ValueError('已有厂家 SDK 采集进程')
    fps = frame_rate(c)
    frames = max(1, math.ceil(min(c['segment_seconds'], value['seconds']) * fps))
    count = max(1, math.ceil(value['seconds'] * fps / frames))
    root = Path(c['root'])
    folder = Path(tempfile.mkdtemp(prefix=datetime.datetime.now().strftime('%Y%m%d_%H%M%S_'), dir=root))
    value.update(path=str(folder), frame_rate=fps, segment_frames=frames, max_files=count,
                 format='inno_pc', angular_table='vendor SDK embeds table in each compact E1X segment',
                 segments=[], complete=False)
    cmd = [c['sdk'], '--lidar-ip', c['lidar_ip'], '--lidar-port', str(c['port']), '--use-tcp',
           '--frame-start', '1', '--frame-number', str(frames), '--file-number', str(count),
           '--output-filename', str(folder / 'raw.inno_pc')]
    value['command'] = cmd
    persist(value)
    child = None
    start_time = time.monotonic()
    last_growth = start_time
    last_received = start_time
    last_bytes = 0
    observer = RawObserver()
    log_pending = b''
    reason = ''
    log = LimitedLog(folder)
    selector = selectors.DefaultSelector()
    try:
        child = subprocess.Popen(cmd, cwd=folder, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 start_new_session=True, bufsize=0)
        os.set_blocking(child.stdout.fileno(), False)
        selector.register(child.stdout, selectors.EVENT_READ)
        last_report = 0
        while True:
            for key, _ in selector.select(timeout=0.25):
                chunk = os.read(key.fd, 65536)
                if chunk:
                    log.write(chunk)
                    log_pending += chunk
                    lines = log_pending.split(b'\n')
                    log_pending = lines.pop()[-1024:]
                    for line in lines:
                        frame = re.search(rb'frame idx:\d+, point_count:(\d+)', line)
                        if frame and int(frame.group(1)) > 0: last_received = time.monotonic()
                else: selector.unregister(key.fileobj)
            t = time.monotonic()
            if requested_stop or read_json(RUN / 'stop.json').get('session') == value['session']:
                reason = 'user_stop'; break
            if t - last_report < 1 and child.poll() is None: continue
            last_report = t
            storage(c)
            output = files(folder)
            size = sum(p.stat().st_size for p in output)
            if size > last_bytes:
                observer.inspect(output)
                last_growth = t; last_bytes = size
            # The SDK buffers 2 MiB in memory. A live point-count log can explain
            # delayed flush, but cannot alone be presented as saved point data.
            receiving = observer.point_packets > 0 and t - max(last_growth, last_received) < 3
            value.update(bytes=size, monotonic=t, updated_at=now(),
                         saved_point_packets=observer.point_packets,
                         phase='recording' if receiving else 'starting',
                         detail='正在保存雷达原始点云' if receiving else
                         '已接收点云，等待 SDK 分段写盘' if last_received > start_time else '等待有效点云')
            persist(value)
            if child.poll() is not None:
                reason = 'sdk_exit'; break
            if t - start_time >= value['seconds']:
                reason = 'duration_limit'; break
            if t - max(last_growth, last_received) > NO_DATA_SECONDS:
                raise ValueError('连续 12 秒没有收到点云，已停止；请检查雷达供电、网线和数据输出')
            if t - last_growth > c['segment_seconds'] + 15:
                raise ValueError('超过一个分段时长仍没有数据写盘，已停止；请查看 capture.log')
    finally:
        stop_tail = stop_child(child, log)
        selector.close()
        if child and child.stdout: child.stdout.close()
        output = files(folder)
        observer.inspect(output)
        tail = b''
        if log.path.exists():
            with log.path.open('rb') as source:
                source.seek(max(0, log.path.stat().st_size - 65536))
                tail = source.read()
        counts = re.findall(rb'\bframe_counter = (\d+)', tail)
        recorded_frames = int(counts[-1]) if counts else None
        natural = bool(child and child.returncode == 0 and reason == 'sdk_exit'
                       and recorded_frames == frames * count and observer.point_packets > 0)
        if natural: reason = 'limit_complete'
        graceful = re.search(rb'UAV_RECORD_STOPPED signal=\d+ frame_boundary=1 writer_drained=1', stop_tail) is not None
        value.update(bytes=sum(p.stat().st_size for p in output), returncode=child.returncode if child else None,
                     saved_point_packets=observer.point_packets,
                     recorded_frames=recorded_frames,
                     ended_at=now(), elapsed_seconds=round(time.monotonic() - start_time, 2),
                     stop_reason=reason, complete=natural, graceful_frame_boundary=graceful,
                     segments=[{'file': p.name, 'bytes': p.stat().st_size,
                                'complete': natural or index < len(output) - 1,
                                'closed_at_frame_boundary': natural or graceful or index < len(output) - 1}
                               for index, p in enumerate(output)])
        # The vendor SDK lacks a signal handler. Native completed segments are
        # flushed on rotation; interrupted final segments remain explicitly partial.
    if reason == 'sdk_exit':
        raise ValueError('厂家采集程序提前退出或未收到完整点云（退出码 %s，帧数 %s/%s）；请查看 capture.log' %
                         (child.returncode, value.get('recorded_frames'), frames * count))
    value.update(phase='stopped', active=False, error='', monotonic=time.monotonic(),
                 detail='采集已结束，达到设定时长' if reason == 'limit_complete'
                 else '已达到设定时长；最后分段为提前结束段' if reason == 'duration_limit'
                 else '已停止并写盘；最后分段为提前结束段' if value.get('graceful_frame_boundary')
                 else '已停止；完成的分段已保留，最后一段可能不完整')


def worker():
    value = read_json(RUN / 'current.json')
    if not value or value.get('boot_id') != boot_id() or value.get('phase') != 'starting':
        raise ValueError('没有当前开机的有效手动采集请求；不会自动采集')
    requested_stop = []
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda signum, frame: requested_stop.append(signum))
    try:
        with (Path(value['config']['root']) / '.uav-lidar-record.lock').open('a') as lock:
            try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise ValueError('已有点云采集任务') from None
            capture(value, requested_stop)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        value.update(phase='error', active=False, error=str(error), detail='采集已停止，已有文件保留',
                     ended_at=now(), monotonic=time.monotonic())
    finally:
        persist(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', default='status', help='start / stop / status；数字秒数兼容原采集命令')
    args = parser.parse_args()
    try:
        if args.action == 'worker': worker(); return 0
        if args.action == 'start': start()
        elif args.action == 'stop': stop()
        elif args.action.isdecimal():
            start(int(args.action))
            try:
                while service_active(): time.sleep(1)
            except KeyboardInterrupt: stop()
        elif args.action != 'status': parser.error('请使用 start、stop、status 或秒数')
        print(json.dumps(snapshot(), ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print('雷达采集失败：' + str(error), file=sys.stderr); return 1


if __name__ == '__main__':
    sys.exit(main())
