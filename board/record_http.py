#!/usr/bin/env python3
"""Small authenticated API for the existing manual recorder. No shell input."""
import argparse
import datetime
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading

import record

TOKEN = Path('/etc/uav-record-api/token')
PORT = 9072


def prepare_token(path=TOKEN):
    if path.is_symlink():
        raise ValueError('连接码文件不能是符号链接')
    if not path.exists():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as out:
            out.write(secrets.token_hex(32) + '\n')
    if path.stat().st_mode & 0o077:
        raise ValueError('连接码文件权限必须为 600')
    value = path.read_text().strip()
    if not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('连接码文件格式不正确')
    return value


def snapshot():
    # Command mutations share record's cross-process lock, including CLI users.
    with record.locked():
        value = record.current()
        allowed = record.available()
        mounted = os.path.ismount(record.MOUNT)
        storage = {'mounted': mounted, 'free_bytes': 0, 'reserve_bytes': record.RESERVE, 'error': ''}
        if mounted:
            storage['free_bytes'] = record.shutil.disk_usage(record.MOUNT).free
        try:
            record.storage()
        except (ValueError, OSError) as error:
            storage['error'] = str(error)
        result = record.ctl('show', *[record.unit(key) for key in record.STREAMS],
                            '-p', 'Id', '-p', 'ActiveState')
        states = {}
        for block in result.stdout.split('\n\n'):
            fields = dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
            states[fields.get('Id', '')] = fields.get('ActiveState', 'inactive')
        streams = []
        for key, (label, _, _) in record.STREAMS.items():
            active = states.get(record.unit(key), 'inactive') in ('active', 'activating', 'deactivating')
            item = {'key': key, 'label': label, 'available': key in allowed,
                    'requested': bool(value and key in value['requested']), 'active': active,
                    'phase': 'waiting' if active else 'stopped',
                    'state': '等待视频' if active else '未录像', 'detail': '', 'updated_at': ''}
            if value and mounted and key in value['requested']:
                path = Path(value['path']) / key / 'status.json'
                try:
                    info = json.loads(path.read_text())
                    item['detail'] = info.get('detail', '')[:1000]
                    item['updated_at'] = info.get('at', '')
                    if active:
                        recent = (datetime.datetime.now().astimezone() -
                                  datetime.datetime.fromisoformat(info['at'])).total_seconds() < 15
                        if info.get('state') == '正在录像' and recent:
                            item.update(phase='recording', state='正在录像')
                        else:
                            item.update(phase='waiting', state='等待视频/重连')
                    elif info.get('state') == '录像停止':
                        item.update(phase='error', state='录像停止')
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            streams.append(item)
        return {'api_version': 1, 'storage': storage, 'streams': streams,
                'session': {'id': value['session'], 'path': value['path'],
                            'started_at': value['started_at'], 'segment_seconds': value['segment_seconds']}
                           if value else None}


def selection(body, action):
    if not isinstance(body, dict) or set(body) != {'streams'}:
        raise ValueError('请求必须只包含 streams 列表')
    keys = body['streams']
    if not isinstance(keys, list) or not 1 <= len(keys) <= len(record.STREAMS):
        raise ValueError('请至少选择一路视频')
    if action == 'stop' and keys == ['all']:
        return list(record.STREAMS)
    if any(not isinstance(key, str) or key not in record.STREAMS for key in keys):
        raise ValueError('无效的视频名称')
    return list(dict.fromkeys(keys))


class Server(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, token):
        self.token = token
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(address, Handler)
    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise
    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)
    def log_message(self, format, *args):
        # Never log headers, connection codes or body contents.
        return
    def respond(self, status, value):
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)
    def authorized(self):
        value = self.headers.get('Authorization', '')
        if not value.isascii() or not hmac.compare_digest(value, 'Bearer ' + self.server.token):
            self.respond(401, {'ok': False, 'error': '连接码不正确'})
            return False
        return True
    def do_GET(self):
        if not self.authorized():
            return
        if self.path != '/v1/status':
            self.respond(404, {'ok': False, 'error': '接口不存在'})
            return
        self.perform(lambda: None)
    def do_POST(self):
        if not self.authorized():
            return
        action = {'/v1/start': 'start', '/v1/stop': 'stop'}.get(self.path)
        if not action:
            self.respond(404, {'ok': False, 'error': '接口不存在'})
            return
        try:
            if self.headers.get('Transfer-Encoding'):
                raise ValueError('不支持分块请求')
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 4096:
                raise ValueError('请求长度不正确')
            body = json.loads(self.rfile.read(length))
            keys = selection(body, action)
        except (ValueError, OSError, TypeError) as error:
            self.respond(400, {'ok': False, 'error': str(error)})
            return
        if action == 'start':
            def operation():
                previous = record.current()
                record.start(keys, previous['segment_seconds'] if previous else 300)
        else:
            def operation():
                record.stop(keys)
        self.perform(operation)
    def perform(self, operation):
        try:
            operation()
            self.respond(200, {'ok': True, 'status': snapshot()})
        except ValueError as error:
            self.respond(409, {'ok': False, 'error': str(error)})
        except (OSError, subprocess.SubprocessError) as error:
            self.respond(503, {'ok': False, 'error': str(error)})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bind', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=PORT)
    parser.add_argument('--prepare-token', action='store_true')
    args = parser.parse_args()
    token = prepare_token()
    if not args.prepare_token:
        print('板端录像控制接口启动，端口 %d；录像仍需手动选择。' % args.port, flush=True)
        with Server((args.bind, args.port), token) as server:
            server.serve_forever()
