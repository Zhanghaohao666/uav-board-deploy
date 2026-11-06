#!/usr/bin/env python3
"""Build three audited role artifacts; never archive arbitrary board directories."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def regular_files(root):
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('No links in distributable input: ' + str(path))
        if not path.is_file():
            continue
        if path.suffix in ('.pem', '.key', '.cert', '.nglic', '.ngreq') or path.name in ('token', 'id_rsa', 'id_ed25519'):
            raise ValueError('Device identity/secret must not be distributed: ' + str(path))
        yield path


def archive_tree(source, output, prefix, gzip=True):
    with tarfile.open(output, 'w:gz' if gzip else 'w') as tar:
        for path in regular_files(source):
            info = tar.gettarinfo(str(path), arcname=prefix + '/' + str(path.relative_to(source)))
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            info.mtime = 0
            with path.open('rb') as stream:
                tar.addfile(info, stream)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--version', required=True)
    parser.add_argument('--algorithm-runtime', type=Path, required=True)
    parser.add_argument('--gimbal-runtime', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'v\d+\.\d+\.\d+', args.version):
        raise ValueError('Use a fixed semantic release tag')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    spec = json.loads((ROOT / 'algorithm/runtime-release.json').read_text())
    if spec['version'] != args.version or sha(args.algorithm_runtime) != spec['sha256']:
        raise ValueError('Algorithm archive does not match the versioned runtime descriptor')
    if args.algorithm_runtime.resolve() != (output / spec['file']).resolve():
        shutil.copy2(args.algorithm_runtime, output / spec['file'])
    with tempfile.TemporaryDirectory(prefix='uav-role-release-') as temp:
        temp = Path(temp)
        gimbal = temp / 'gimbal'
        gimbal.mkdir()
        for name in ('install.sh', 'restore.sh', 'README.md'):
            shutil.copy2(ROOT / 'gimbal' / name, gimbal / name)
        for name in ('service', 'source'):
            shutil.copytree(ROOT / 'gimbal' / name, gimbal / name)
        shutil.copytree(args.gimbal_runtime, gimbal / 'runtime')
        (gimbal / 'VERSION').write_text(args.version + '\n')
        manifest = {str(p.relative_to(gimbal)): sha(p) for p in regular_files(gimbal)}
        (gimbal / 'PAYLOAD.sha256').write_text(''.join(value + '  ' + name + '\n'
                                                      for name, value in sorted(manifest.items())))
        gimbal_asset = output / ('uav-gimbal-deploy-' + args.version + '.tar')
        archive_tree(gimbal, gimbal_asset, 'uav-gimbal-deploy', gzip=False)
        # bootstrap.sh is excluded from the role tar, avoiding a digest cycle.
        bootstrap = ROOT / 'gimbal/bootstrap.sh'
        text = bootstrap.read_text()
        text = re.sub(r'^VERSION=.*$', 'VERSION=' + args.version, text, flags=re.M)
        text = re.sub(r'^ARCHIVE_SHA256=.*$', 'ARCHIVE_SHA256=' + sha(gimbal_asset), text, flags=re.M)
        bootstrap.write_text(text)
        descriptor = dict(schema=1, version=args.version, file=gimbal_asset.name,
                          sha256=sha(gimbal_asset), bytes=gimbal_asset.stat().st_size,
                          url='https://github.com/Zhanghaohao666/uav-board-deploy/releases/download/' + args.version + '/' + gimbal_asset.name)
        (ROOT / 'gimbal/runtime-release.json').write_text(json.dumps(descriptor, indent=2) + '\n')
        subprocess.run(['python3', str(ROOT / 'make_release.py')], check=True)
        generic = ROOT.parent / 'uav-board-deploy.tar.gz'
        shutil.copy2(generic, output / generic.name)
        source = temp / 'source'
        with tarfile.open(generic, 'r:gz') as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    raise ValueError('Source bundle must only contain regular files')
                parts = Path(member.name).parts
                if parts[0] != 'uav-board-deploy' or '..' in parts:
                    raise ValueError('Unexpected source path')
                dest = source.joinpath(*parts[1:])
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(tar.extractfile(member).read())
                dest.chmod(member.mode)
        archive_tree(source, output / ('uav-mainboard-deploy-' + args.version + '.tar.gz'), 'uav-mainboard-deploy')
        (source / 'runtime').mkdir()
        shutil.copy2(args.algorithm_runtime, source / 'runtime' / spec['file'])
        archive_tree(source, output / ('uav-algorithm-deploy-' + args.version + '.tar.gz'), 'uav-algorithm-deploy')
        with zipfile.ZipFile(output / ('uav-pc-switch-' + args.version + '.zip'), 'w', zipfile.ZIP_DEFLATED) as z:
            for p in regular_files(ROOT / 'pc'):
                z.write(p, 'uav-pc-switch/' + str(p.relative_to(ROOT / 'pc')))
    names = ['uav-board-deploy.tar.gz', 'uav-mainboard-deploy-' + args.version + '.tar.gz',
             'uav-algorithm-deploy-' + args.version + '.tar.gz', 'uav-gimbal-deploy-' + args.version + '.tar',
             spec['file'], 'uav-pc-switch-' + args.version + '.zip']
    inventory = {name: {'sha256': sha(output / name), 'bytes': (output / name).stat().st_size} for name in names}
    (output / 'SHA256SUMS').write_text(''.join(v['sha256'] + '  ' + n + '\n' for n, v in inventory.items()))
    (output / 'role-artifacts.json').write_text(json.dumps(inventory, indent=2) + '\n')
    print(json.dumps(inventory, indent=2))


if __name__ == '__main__':
    main()
