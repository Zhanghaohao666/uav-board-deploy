#!/usr/bin/env python3
"""On-demand, independent H.264 recording. Never enabled at boot."""
import argparse
from collections import deque
from contextlib import contextmanager
import datetime
import fcntl
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

BASE = Path(__file__).resolve().parent
MOUNT = Path('/data')
DEST = MOUNT / 'uav-recordings'
STATE = Path('/run/uav-record/current.json')
LOCK = Path('/run/lock/uav-record.lock')
MODE_STATE = Path('/var/lib/uav-switch/active.json')
RESERVE = 10 * 1024 ** 3
STREAMS = {
    'board': ('下视 MIPI', 8554, 'board'),
    'rgb': ('D455 彩色', 8554, 'rgb'),
    'algorithm': ('前视算法', 8554, 'algorithm'),
    'preview': ('前视预览', 8554, 'preview'),
    'infrared': ('算法板红外', 8554, 'infrared'),
    'gimbal': ('云台可见光', 8555, 'gimbal'),
    'gimbal_ir': ('云台红外', 8556, 'gimbal_ir'),
}


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + '.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(value, out, ensure_ascii=False, indent=2)
            out.write('\n'); out.flush(); os.fsync(out.fileno())
        os.chmod(name, 0o644)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def locked():
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open('a') as out:
        fcntl.flock(out, fcntl.LOCK_EX)
        yield


def storage():
    if not os.path.ismount(MOUNT):
        raise ValueError('数据盘未挂载：%s；不会写入系统盘' % MOUNT)
    if DEST.is_symlink() or MOUNT.resolve() not in DEST.resolve().parents:
        raise ValueError('录像目录必须位于数据盘内')
    free = shutil.disk_usage(MOUNT).free
    if free < RESERVE:
        raise ValueError('数据盘剩余 %.1f GiB，低于保留的 %.0f GiB，停止录像；未删除历史文件' %
                         (free / 1024 ** 3, RESERVE / 1024 ** 3))
    return free


def available():
    config = json.loads((BASE / 'config.json').read_text())
    mode = json.loads(MODE_STATE.read_text())['mode'] if MODE_STATE.exists() else config['default_mode']
    selected = ['board'] + (['rgb'] if config['d455_enabled'] else [])
    if mode in ('algorithm', 'dual'):
        selected += ['algorithm', 'preview', 'infrared']
    if mode in ('gimbal', 'dual'):
        selected += ['gimbal', 'gimbal_ir']
    return selected


def parse_selection(value, allowed):
    tokens = value.replace('，', ',').replace(',', ' ').split()
    if tokens in (['all'], ['全部']):
        return list(allowed)
    result = []
    names = list(STREAMS)
    for token in tokens:
        key = names[int(token) - 1] if token.isdigit() and 1 <= int(token) <= len(names) else token
        if key not in allowed:
            raise ValueError('无效或当前模式未启用的视频：' + token)
        if key not in result:
            result.append(key)
    if not result:
        raise ValueError('至少选择一路视频')
    return result


def unit(key):
    return 'uav-record@%s.service' % key


def ctl(*args, check=True):
    return subprocess.run(['systemctl', *args], check=check, text=True, capture_output=True, timeout=45)


def active(key):
    return ctl('is-active', unit(key), check=False).stdout.strip() in ('active', 'activating', 'deactivating')


def current():
    if not STATE.exists():
        return None
    value = json.loads(STATE.read_text())
    path = Path(value['path'])
    if path.is_symlink() or DEST.resolve() not in path.resolve().parents:
        raise ValueError('任务目录越界')
    if not set(value['requested']).issubset(STREAMS):
        raise ValueError('任务流编号不正确')
    return value


def persist_session(value, event):
    path = Path(value['path']) / 'session.json'
    document = json.loads(path.read_text()) if path.exists() else {
        'session': value['session'], 'started_at': value['started_at'], 'streams': {}, 'events': []}
    for key in value['requested']:
        _, port, mount = STREAMS[key]
        document['streams'][key] = {'label': STREAMS[key][0],
                                    'url': 'rtsp://127.0.0.1:%d/%s' % (port, mount)}
    document['events'].append({'at': now(), **event})
    atomic(path, document)


def start(keys, segment=300):
    if not 5 <= segment <= 3600:
        raise ValueError('分段时长需为 5～3600 秒')
    if not keys or not set(keys).issubset(available()):
        raise ValueError('请选择当前方案中的视频')
    storage()
    if not shutil.which('ffmpeg'):
        raise ValueError('缺少 ffmpeg')
    with locked():
        previous = current()
        running = [key for key in STREAMS if active(key)]
        if running and not previous:
            raise ValueError('存在无任务记录的录像服务，请先停止全部录像')
        if previous and not running:
            persist_session(previous, {'action': 'previous_session_finished'})
            previous = None
        if previous:
            if segment != previous['segment_seconds']:
                raise ValueError('同一任务不能改变分段时长；先停止再开始新任务')
            value = dict(previous)
            value['requested'] = list(previous['requested'])
        else:
            session = datetime.datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6]
            path = DEST / datetime.datetime.now().strftime('%Y%m%d') / session
            path.mkdir(parents=True)
            value = {'session': session, 'path': str(path), 'started_at': now(),
                     'segment_seconds': segment, 'requested': []}
        added = [key for key in keys if key not in running]
        for key in keys:
            if key not in value['requested']:
                value['requested'].append(key)
            (Path(value['path']) / key).mkdir(exist_ok=True)
        persist_session(value, {'action': 'start_requested', 'streams': keys})
        atomic(STATE, value)
        try:
            for key in added:
                ctl('start', unit(key))
        except BaseException:
            for key in added:
                ctl('stop', unit(key), check=False)
            if previous:
                atomic(STATE, previous)
            else:
                STATE.unlink(missing_ok=True)
            persist_session(value, {'action': 'start_failed', 'streams': added})
            raise
        print('已请求录像：' + '、'.join(STREAMS[k][0] for k in keys))
        print('保存目录：' + value['path'])
        print('请用状态查看实际写入；无输入一路独立等待，退出菜单不会停止录像。')
        return value


def stop(keys):
    if not keys or not set(keys).issubset(STREAMS):
        raise ValueError('无效流编号')
    with locked():
        # Stop services even if the SSD has become unavailable.
        for key in keys:
            ctl('stop', unit(key))
        value = current()
        if value:
            if os.path.ismount(MOUNT):
                persist_session(value, {'action': 'stop_requested', 'streams': keys})
            value['requested'] = [key for key in value['requested'] if key not in keys]
            if value['requested']:
                atomic(STATE, value)
            else:
                STATE.unlink(missing_ok=True)
        print('已停止：' + '、'.join(STREAMS[k][0] for k in keys))
        print('已有录像保留，不自动删除。')


def status():
    value = current()
    if not value:
        print('没有当前录像任务；开机默认不录像。')
        for key in STREAMS:
            if active(key):
                print(STREAMS[key][0] + '：有运行中的录像服务，请停止后检查')
        return
    print('任务：' + value['session'] + '\n保存目录：' + value['path'])
    for key in value['requested']:
        state = ctl('is-active', unit(key), check=False).stdout.strip()
        detail = '正在启动/等待视频'
        path = Path(value['path']) / key / 'status.json'
        if os.path.ismount(MOUNT) and path.exists():
            info = json.loads(path.read_text())
            detail = info.get('state', '') + '；' + info.get('detail', '')
        print('%s [%s] %s' % (STREAMS[key][0], state, detail))
    try:
        print('数据盘可用 %.1f GiB；保留 %.0f GiB' % (storage() / 1024 ** 3, RESERVE / 1024 ** 3))
    except ValueError as error:
        print(str(error))


def timeout_option():
    help_text = subprocess.check_output(['/usr/bin/ffmpeg', '-hide_banner', '-h', 'demuxer=rtsp'],
                                        stderr=subprocess.STDOUT, text=True)
    if '-stimeout' in help_text:
        return '-stimeout'
    if '-timeout' in help_text and 'microseconds' in help_text:
        return '-timeout'
    raise ValueError('无法识别 FFmpeg RTSP 超时选项')


def command(key, folder, segment, option):
    _, port, mount = STREAMS[key]
    attempt = datetime.datetime.now().strftime('%H%M%S') + '_' + uuid.uuid4().hex[:8]
    return ['/usr/bin/ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'warning',
            '-rtsp_transport', 'tcp', option, '10000000',
            '-analyzeduration', '200000', '-probesize', '32768',
            '-i', 'rtsp://127.0.0.1:%d/%s' % (port, mount),
            '-map', '0:v:0', '-an', '-c:v', 'copy', '-n',
            '-progress', 'pipe:1', '-nostats', '-f', 'segment',
            '-segment_time', str(segment), '-segment_format', 'matroska',
            '-reset_timestamps', '1', '-segment_list', str(folder / (attempt + '.csv')),
            str(folder / (attempt + '_%05d.mkv'))]


def close_child(child):
    if child.poll() is not None:
        return
    child.send_signal(signal.SIGINT)
    try:
        child.wait(timeout=7)
    except subprocess.TimeoutExpired:
        child.terminate()
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill(); child.wait()


def worker(key):
    if key not in STREAMS:
        raise ValueError('无效录像流')
    value = current()
    if not value or key not in value['requested']:
        raise ValueError('未手动选择本路录像')
    folder = Path(value['path']) / key
    storage()
    folder.mkdir(exist_ok=True)
    option = timeout_option()
    stopping = False
    def stop_signal(signum, frame):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop_signal)
    signal.signal(signal.SIGINT, stop_signal)
    def report(state, detail):
        # SSD loss must never redirect status writes into the root filesystem.
        if os.path.ismount(MOUNT):
            atomic(folder / 'status.json', {'state': state, 'detail': detail, 'at': now()})
    try:
        while not stopping:
            storage()
            report('等待视频', '无输入或断流只影响本路')
            argv = command(key, folder, value['segment_seconds'], option)
            print('%s 开始接收：%s' % (key, now()), flush=True)
            child = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            selector = selectors.DefaultSelector()
            selector.register(child.stdout, selectors.EVENT_READ)
            pending = b''; last_frame = time.monotonic(); last_report = 0; frames = 0
            errors = deque(maxlen=10)
            try:
                while child.poll() is None and not stopping:
                    storage()
                    if time.monotonic() - last_frame > 30:
                        errors.append('连续 30 秒未接收视频，重新连接本路')
                        break
                    if not selector.select(timeout=1):
                        continue
                    data = os.read(child.stdout.fileno(), 65536)
                    if not data:
                        break
                    pending += data
                    lines = pending.split(b'\n'); pending = lines.pop()[-65536:]
                    for raw in lines:
                        line = raw.decode(errors='replace').strip()
                        if line.startswith('frame='):
                            number = int(line[6:].strip())
                            if number > frames:
                                frames = number; last_frame = time.monotonic()
                                if last_frame - last_report > 3:
                                    report('正在录像', '本次连接已写入 %d 帧；分段 %d 秒' %
                                           (frames, value['segment_seconds']))
                                    last_report = last_frame
                        elif '=' not in line and line:
                            errors.append(line)
            finally:
                selector.close(); close_child(child); child.stdout.close()
            if not stopping:
                report('等待重连', ' | '.join(errors)[-1000:] or '输入结束，5 秒后重试')
                for _ in range(5):
                    if stopping:
                        break
                    time.sleep(1)
        report('已停止', '已请求关闭文件；历史录像保留')
    except (ValueError, OSError) as error:
        print('录像停止：' + str(error), flush=True)
        try:
            report('录像停止', str(error))
        except OSError:
            pass
        # Disk problems require another explicit start; no boot/space retry loop.
        return


def menu():
    print('按需录像工具：默认不录像；文件保存在 %s；每路独立。' % DEST)
    while True:
        print('\n1 开始/添加录像\n2 停止指定路\n3 停止全部录像\n4 查看录像状态\n0 退出（正在录像的服务继续运行）')
        choice = input('请选择：').strip()
        try:
            if choice == '0':
                return
            if choice in ('1', '2'):
                allowed = available() if choice == '1' else list(STREAMS)
                for index, (key, item) in enumerate(STREAMS.items(), 1):
                    print('%d %s%s' % (index, item[0], '' if key in allowed else '（当前模式未启用）'))
                selection = input('输入编号，多选用逗号/空格；all 表示全部；空行返回：').strip()
                if not selection:
                    continue
                keys = parse_selection(selection, allowed)
                start(keys) if choice == '1' else stop(keys)
            elif choice == '3':
                stop(list(STREAMS))
            elif choice == '4':
                status()
            else:
                print('请输入 0～4')
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            print('操作失败：' + str(error))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', choices=('start', 'stop', 'status', 'worker'))
    parser.add_argument('streams', nargs='*')
    parser.add_argument('--segment-seconds', type=int, default=300)
    args = parser.parse_args()
    if os.geteuid() != 0 and args.action != 'status':
        parser.error('请使用 sudo 或安装后的 uav-record 入口')
    if args.action is None:
        menu()
    elif args.action == 'status':
        status()
    elif args.action == 'worker':
        if len(args.streams) != 1:
            parser.error('worker 需要唯一的流名称')
        worker(args.streams[0])
    elif args.action == 'start':
        start(parse_selection(' '.join(args.streams), available()), args.segment_seconds)
    else:
        stop(parse_selection(' '.join(args.streams), list(STREAMS)))


if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print('\n已退出菜单；正在录像的服务继续运行。')
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
