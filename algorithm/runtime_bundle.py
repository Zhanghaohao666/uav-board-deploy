#!/usr/bin/env python3
"""Validate and safely unpack the separately distributed, offline ARM64 runtime."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile

REQUIRED = {'stream/rk_streamer', 'stream/tracker_worker', 'stream/mino17_streamer'}
MODEL_NAMES = {'best_rk3588_zym_20261016.rknn',
               'yolov5s_3588_rgb_SearchArea_320_20250410_ley.rknn'}
LABELS = {'stream/TTTracker_V1.1_publish/model/coco_2_labels_list_per_car.txt',
          'stream/TTTracker_V1.1_publish/model/coco_3_labels_list.txt'}
MAX_BYTES = 4 * 1024 ** 3
MAX_FILES = 20000


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def safe_path(name):
    if not isinstance(name, str) or not name or '\\' in name or any(ord(c) < 32 for c in name):
        raise ValueError('运行包路径无效')
    path = PurePosixPath(name)
    if path.is_absolute() or str(path) != name or any(p in ('.', '..') for p in path.parts):
        raise ValueError('运行包路径越界：' + name)
    if path.parts[0] != 'stream':
        raise ValueError('运行包文件必须位于 stream：' + name)
    return path


def validate_manifest(manifest):
    if not isinstance(manifest, dict) or manifest.get('schema') != 1 or manifest.get('architecture') != 'aarch64':
        raise ValueError('不支持的算法运行包格式/架构')
    files = manifest.get('files')
    if not isinstance(files, dict) or not REQUIRED.issubset(files) or len(files) > MAX_FILES:
        raise ValueError('运行包缺少实际可执行程序或文件列表无效')
    total = 0
    for name, item in files.items():
        safe_path(name)
        if name.endswith(('.a', '.o', '.pem', '.key')):
            raise ValueError('运行包不应包含静态构建产物或密钥：' + name)
        if not isinstance(item, dict) or not re.fullmatch('[0-9a-f]{64}', str(item.get('sha256', ''))):
            raise ValueError('运行包文件 SHA256 无效：' + name)
        size = item.get('size')
        if type(size) is not int or size <= 0 or size > MAX_BYTES or item.get('mode') not in (0o644, 0o755):
            raise ValueError('运行包大小/权限无效：' + name)
        total += size
    if total > MAX_BYTES:
        raise ValueError('运行包解压大小超限')
    for name in REQUIRED:
        if files[name]['mode'] != 0o755:
            raise ValueError('运行包可执行权限缺失：' + name)
    if not LABELS.issubset(files):
        raise ValueError('运行包缺少检测模型对应的标签文件')
    models = manifest.get('models')
    if not isinstance(models, list) or not MODEL_NAMES.issubset({Path(x).name for x in models if isinstance(x, str)}):
        raise ValueError('运行包缺少已验证的检测/搜索模型')
    for name in models:
        safe_path(name)
        if name not in files or not name.startswith('stream/TTTracker_V1.1_publish/model/'):
            raise ValueError('模型路径/清单无效')
    dirs = manifest.get('library_dirs')
    if not isinstance(dirs, list) or not dirs:
        raise ValueError('缺少运行库目录')
    for name in dirs:
        safe_path(name)
        if not any(f.startswith(name + '/') for f in files):
            raise ValueError('运行库目录为空：' + name)
    return manifest


def read_manifest(archive, expected_sha):
    if not re.fullmatch('[0-9a-f]{64}', str(expected_sha)) or sha256(archive) != expected_sha:
        raise ValueError('算法运行包 SHA256 不一致；未安装/运行其中程序')
    with tarfile.open(archive, 'r:gz') as tar:
        members = tar.getmembers()
        if len(members) > MAX_FILES * 2:
            raise ValueError('运行包条目过多')
        names = set()
        for member in members:
            name = member.name.rstrip('/') if member.isdir() else member.name
            if name in names:
                raise ValueError('运行包重复路径：' + name)
            names.add(name)
            if name != 'runtime-manifest.json':
                safe_path(name)
            if not (member.isfile() or member.isdir()):
                raise ValueError('运行包不允许软链接、硬链接或设备节点：' + name)
        info = tar.getmember('runtime-manifest.json')
        if not info.isfile() or info.size > 4 * 1024 * 1024:
            raise ValueError('运行包清单过大/无效')
        manifest = validate_manifest(json.load(tar.extractfile(info)))
        actual = {m.name for m in members if m.isfile()} - {'runtime-manifest.json'}
        if actual != set(manifest['files']):
            raise ValueError('运行包内容与清单不一致')
        for name, item in manifest['files'].items():
            if tar.getmember(name).size != item['size']:
                raise ValueError('运行包文件大小不一致：' + name)
    return manifest


def extract(archive, expected_sha, destination):
    """No tar.extractall: never follow any archive or destination symlink."""
    manifest = read_manifest(archive, expected_sha)
    destination = Path(destination)
    if destination.exists() and (destination.is_symlink() or any(destination.iterdir())):
        raise ValueError('解压目标必须是全新空目录')
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, 'r:gz') as tar:
            for name, item in manifest['files'].items():
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(name) as source, target.open('xb') as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                if sha256(target) != item['sha256']:
                    raise ValueError('运行包内部文件校验失败：' + name)
                target.chmod(item['mode'])
        (destination / 'runtime-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
        # tempfile.mkdtemp is 0700. Releases are root-owned but the non-root
        # camera service must be able to traverse them after the atomic move.
        destination.chmod(0o755)
        (destination / 'runtime-manifest.json').chmod(0o644)
        for path in destination.rglob('*'):
            if path.is_dir():
                path.chmod(0o755)
    except BaseException:
        shutil.rmtree(destination)
        raise
    return manifest


def verify_tree(directory, manifest):
    directory = Path(directory)
    for name, item in manifest['files'].items():
        target = directory / name
        if target.is_symlink() or not target.is_file() or target.stat().st_size != item['size'] or (target.stat().st_mode & 0o777) != item['mode'] or sha256(target) != item['sha256']:
            return False
    return True
