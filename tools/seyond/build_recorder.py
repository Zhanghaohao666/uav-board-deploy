#!/usr/bin/env python3
"""Build a separate graceful recorder using an already configured Seyond SDK.

No SDK download, sensor/network configuration or replacement of the original tool.
The source and static libraries must be from the same tested SDK checkout.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys


def build(sdk, destination):
    sdk = sdk.resolve()
    destination = destination.resolve()
    source = sdk / 'apps/tools/get_pcd/get_pcd.cpp'
    libraries = [sdk / 'lib' / ('lib' + name + '.a') for name in
                 ('innolidarsdkclient', 'innolidarsdkcommon', 'innolidarutils')]
    for path in [source, *libraries]:
        if not path.is_file():
            raise ValueError('Missing configured SDK file: ' + str(path))
    destination.mkdir(parents=True, exist_ok=False)
    stage = destination / 'source'
    copied = stage / 'apps/tools/get_pcd/get_pcd.cpp'
    copied.parent.mkdir(parents=True)
    shutil.copy2(source, copied)
    helper = Path(__file__).with_name('apply_graceful_stop.py')
    subprocess.run([sys.executable, str(helper), str(stage)], check=True)
    binary = destination / 'seyond-get-pcd-record'
    command = ['g++', '-std=gnu++11', '-fPIC', '-O3', '-DNDEBUG',
               '-D_GLIBCXX_USE_CXX11_ABI=0', '-I' + str(sdk / 'apps'),
               '-I' + str(sdk), '-I' + str(sdk / 'src'), str(copied),
               '-o', str(binary), '-L' + str(sdk / 'lib'), '-Wl,-Bstatic',
               '-linnolidarsdkclient', '-linnolidarsdkcommon', '-linnolidarutils',
               '-Wl,-Bdynamic', '-lpthread']
    with (destination / 'build.log').open('wb') as log:
        subprocess.run(['nice', '-n', '10', *command], stdout=log,
                       stderr=subprocess.STDOUT, check=True, timeout=300)
    report = {'sdk': str(sdk), 'command': command, 'binary': str(binary),
              'sha256': {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in [source, copied, *libraries, binary]}}
    (destination / 'build.json').write_text(json.dumps(report, indent=2) + '\n')
    shutil.copy2(Path(__file__).with_name('SEYOND_LICENSE'), destination / 'SEYOND_LICENSE')
    print('Built:', binary)
    print('Select this binary via the sdk field in /etc/uav-lidar.json after validation.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sdk-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True,
                        help='new output directory on the data disk')
    args = parser.parse_args()
    build(args.sdk_root, args.output)
