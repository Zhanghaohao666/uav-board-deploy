#!/usr/bin/env python3
"""Independent front-camera worker for the existing TTTracker V1.1 board."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

import runtime

DEFAULTS = {
    'algorithm': {'device': '/dev/v4l/by-path/platform-rkcif-mipi-lvds4-video-index0',
                  'port': 5602, 'format': 'rgb', 'bitrate': 5000, 'rotate': 90},
    'preview': {'device': '/dev/v4l/by-path/platform-rkcif-mipi-lvds-video-index0',
                'port': 5603, 'format': 'yuv', 'bitrate': 3000, 'rotate': 270},
}


def build(slot, supplied=None):
    env = dict(os.environ if supplied is None else supplied)
    item = DEFAULTS[slot]
    root = Path(env.get('UAV_STREAM_ROOT', '/home/dev/test1/rk3588_streamer'))
    tracker = root / 'TTTracker_V1.1_publish'
    device = env.get('DEVICE', item['device'])
    bitrate = int(env.get('BITRATE', item['bitrate']))
    rotate = int(env.get('ROTATE', item['rotate']))
    if rotate not in (0, 90, 180, 270) or not 128 <= bitrate <= 20000:
        raise ValueError('ROTATE 或 BITRATE 无效')
    if not device.startswith('/dev/') or '..' in Path(device).parts:
        raise ValueError('DEVICE 必须是 /dev 下的相机路径')
    host = env.get('HOST', '192.168.10.1')
    # Keep the matching runtime used by the existing, validated application.
    libraries = [tracker / 'libs/opencv-3.4.15/lib', tracker / 'libs/opencv_deps',
                 tracker / 'libs/rknn/aarch64-linux-gnu',
                 Path('/mnt/data/csj/TTTracker_V1.1/TTTracker_V1.1_make/tracker/detector/rknn_model_zoo_2.3.0/3rdparty/rknpu2/Linux/aarch64')]
    env['LD_LIBRARY_PATH'] = ':'.join(map(str, libraries)) + ':' + env.get('LD_LIBRARY_PATH', '')
    if slot == 'algorithm':
        env['TRACKER_MULTI_OBJECT'] = '1'
    else:
        env.pop('TRACKER_MULTI_OBJECT', None)
    cwd = tracker / 'build' if slot == 'algorithm' else root
    argv = [str(root / 'rk_streamer'), '--host', host, '--format', item['format'],
            '--codec', 'h264', '--rc', 'cbr', '--fps', '30', '--bitrate', str(bitrate),
            '--cam', device, '1920x1080', str(item['port']),
            'input=nv12,format=%s,bitrate=%d,fps=30,gop=15,rot=%d' % (item['format'], bitrate, rotate)]
    return argv, env, cwd


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('slot', choices=DEFAULTS)
    args = parser.parse_args()
    argv, env, cwd = build(args.slot)
    # Inspect only this worker's camera. The other camera is never a prerequisite.
    item = {'device': argv[argv.index('--cam') + 1]}
    state, detail = runtime.inspect_camera('front1', item)
    if state != 'ready':
        raise ValueError(detail + '；仅本路等待重试')
    if args.slot == 'algorithm':
        cwd.mkdir(exist_ok=True)
        for name, minimum in [('yolov5s_3588_visible_20250226.rknn', 9751616),
                              ('yolov5s_3588_rgb_SearchArea_320_20250410_ley.rknn', 9402304)]:
            model = cwd.parent / 'model' / name
            if not model.exists() or model.stat().st_size < minimum:
                print('WARNING: 模型缺失/不完整，检测不可用：' + str(model), flush=True)
    os.chdir(cwd)
    print('独立启动 %s：%s' % (args.slot, ' '.join(argv)), flush=True)
    runtime.supervise(['stdbuf', '-oL', '-eL'] + argv, env)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
