#!/usr/bin/env python3
"""Local console for the installed UAV switch manager; standard library only."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

MANAGER = Path(__file__).resolve().with_name('manager.py')
LABEL = {'algorithm': '算法板单网口', 'gimbal': '云台单网口', 'dual': '双网口并行测试'}
WIRE = {
    'algorithm': '算法板接主控 eth0。',
    'gimbal': '云台接主控 eth0。',
    'dual': '算法板接 eth0，云台接已配置的扩展网口。',
}


def display(obj):
    if 'error' in obj:
        print('操作失败：' + obj['error'])
        return
    if 'mode' in obj:
        print('当前模式：' + LABEL[obj['mode']])
        print('开机默认：' + LABEL[obj['default_mode']])
        print(obj['network'].rstrip())
        for name, state in obj['services'].items():
            print('  {}：{}'.format(name, state))
        for peer, reachable in obj['peer_reachable'].items():
            print('  {}：{}'.format(peer, '可达' if reachable else '未响应，请检查接线和供电'))
        print('控制端口：云台 9000；算法板 9001。地面站需选择对应目标。')
    elif 'applied_mode' in obj:
        print('已切换：' + LABEL[obj['applied_mode']])
        print('开机默认：' + LABEL[obj['default_mode']])
        print(obj['video_validation'])
    elif 'streams' in obj:
        for row in obj['streams']:
            print('{} {} {}'.format('通过' if row['decoded'] else '失败', row['name'], row['url']))
            if not row['decoded']:
                print('  ' + row['detail'].strip())
        print('全部画面通过。' if obj['all_decoded'] else '部分画面未通过；服务运行不等于视频已出图。')
    elif 'default_mode' in obj:
        print('开机默认已保存：' + LABEL[obj['default_mode']])
    for name, url in obj.get('urls', []):
        print(name + '：' + url)


def execute(action, mode=None):
    command = ['/usr/bin/python3', str(MANAGER), action]
    if mode is not None:
        command.append(mode)
    mutating = action in ('switch', 'save-default')
    if mutating and os.geteuid() != 0:
        # Let sudo read the terminal itself; do not store a password.
        if subprocess.run(['sudo', '-v']).returncode:
            print('未取得管理员权限，未执行修改。')
            return False
        command = ['sudo', '-n'] + command
    if mutating:
        print('正在应用配置，请等待完成……', flush=True)
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN) if mutating else None
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding='utf-8', errors='replace')
    finally:
        if mutating:
            signal.signal(signal.SIGINT, previous)
    try:
        display(json.loads(result.stdout or result.stderr))
    except ValueError:
        print((result.stderr + '\n' + result.stdout).strip() or '程序未返回结果。')
    return result.returncode == 0


def main():
    print('板端视频方案切换工具（本机运行）')
    if not MANAGER.is_file():
        print('未找到同目录的 manager.py，请先完整部署板端程序。')
        return 1
    execute('status')
    while True:
        print('\n1 算法板单网口\n2 云台单网口\n3 双网口并行测试'
              '\n4 查看状态与流地址\n5 验证视频出图'
              '\n6 将当前模式保存为开机默认\n0 退出（视频服务继续运行）')
        choice = input('请选择：').strip()
        if choice == '0':
            return 0
        if choice in ('1', '2', '3'):
            mode = {'1': 'algorithm', '2': 'gimbal', '3': 'dual'}[choice]
            print(WIRE[mode] + ' MK22 接线保持原样。')
            print('相关视频和控制连接会短暂重连；此次切换不改变开机默认。')
            if input('接线就绪后输入 y 切换，其他输入返回：').strip().lower() == 'y':
                if execute('switch', mode):
                    print('正在验证各路画面……', flush=True)
                    execute('verify')
        elif choice == '4':
            execute('status')
        elif choice == '5':
            print('正在验证各路画面……', flush=True)
            execute('verify')
        elif choice == '6':
            execute('save-default')
        else:
            print('请输入 0～6。')


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print('\n已退出菜单，视频服务继续运行。')
    except OSError as error:
        print('执行失败：' + str(error), file=sys.stderr)
        sys.exit(1)
