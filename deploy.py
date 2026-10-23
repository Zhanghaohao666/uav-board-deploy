#!/usr/bin/env python3
"""Install main-board or complete tracking application on the matching vendor BSP."""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import install as main_installer
from common import board_storage as storage
from common import main_network

def module(name, relative):
    spec=importlib.util.spec_from_file_location(name, ROOT/relative)
    out=importlib.util.module_from_spec(spec);spec.loader.exec_module(out)
    return out

def run(argv, check=True, **kw):
    return subprocess.run(argv, check=check, text=True, capture_output=True, **kw)

def choose_role(value, yes):
    if value:return value
    if yes or not sys.stdin.isatty():raise ValueError('请明确 --role main 或 --role algorithm')
    print('选择本板角色：\n  1 下视主控板（视频转发、录像、雷达）\n  2 完整算法板（检测跟踪、前视、红外）')
    while True:
        value=input('选择 [1/2]：').strip()
        if value in ('1','2'):return 'main' if value=='1' else 'algorithm'

def dependencies(role, offline):
    packages = (main_installer.PACKAGES + ['gstreamer1.0-plugins-bad'] if role=='main' else
        ['python3','iproute2','iputils-arping','v4l-utils','psmisc','coreutils','libatomic1','libwebp7','libpng16-16'])
    packages += ['rsync','logrotate','rsyslog','util-linux']
    missing=[]
    for package in sorted(set(packages)):
        r=run(['dpkg-query','-W','-f=${Status}',package],check=False)
        if r.returncode or r.stdout.strip()!='install ok installed':missing.append(package)
    if missing and offline:raise ValueError('离线镜像缺少依赖，请先准备：'+' '.join(missing))
    if missing:
        print('补齐用户态依赖：'+' '.join(missing),flush=True)
        subprocess.run(['apt-get','update'],check=True)
        subprocess.run(['apt-get','install','-y','--no-upgrade','--no-install-recommends',*missing],
            check=True,env=dict(os.environ,DEBIAN_FRONTEND='noninteractive'))

def runtime_asset(spec, override=None, offline=False):
    name=spec['file']; digest=spec['sha256']
    if Path(name).name!=name or not name.endswith('.tar.gz') or len(digest)!=64 or any(x not in '0123456789abcdef' for x in digest):
        raise ValueError('无效的算法运行包发布清单')
    path=Path(override) if override else ROOT/'runtime'/name
    if not path.is_file():
        if override or offline:raise ValueError('缺少离线算法运行包：'+str(path))
        expected='https://github.com/Zhanghaohao666/uav-board-deploy/releases/download/'
        url=spec['url']
        if not url.startswith(expected) or not url.endswith('/'+name):raise ValueError('不允许的运行包来源')
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_name(path.name+'.part')
        print('下载完整算法运行包…',flush=True)
        try:
            with urllib.request.urlopen(url,timeout=60) as response,temporary.open('wb') as f:
                count=0
                while True:
                    chunk=response.read(1024*1024)
                    if not chunk:break
                    count+=len(chunk)
                    if count>512*1024**2:raise ValueError('运行包超过大小限制')
                    f.write(chunk)
            if hashlib.sha256(temporary.read_bytes()).hexdigest()!=digest:raise ValueError('算法运行包SHA256校验失败')
            os.replace(temporary,path)
        finally:temporary.unlink(missing_ok=True)
    if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise ValueError('算法运行包SHA256校验失败：'+str(path))
    return path

def storage_plan(role,args):
    try:return storage.preflight(role,args.data_root,args.data_uuid)
    except ValueError:
        if args.data_uuid or args.yes or not sys.stdin.isatty():raise
        choices=storage.available_partitions()
        if not choices:raise
        print('没有合适的已挂载数据盘。可选择以下已有 ext4 分区挂载（不会格式化）：')
        for i,item in enumerate(choices,1):print(i,item['device'],item['size'],'UUID='+item['uuid'])
        value=input('选择分区编号（直接回车取消）：').strip()
        if not value.isdigit() or not 1<=int(value)<=len(choices):raise ValueError('未选择数据分区，未安装')
        args.data_uuid=choices[int(value)-1]['uuid']
        return storage.preflight(role,args.data_root,args.data_uuid)

def rollback_main(c,before):
    # Only called for a new application successfully installed by this transaction.
    for unit in ['uav-record-api.service',main_installer.BOOT]:run(['systemctl','disable','--now',unit],check=False)
    run(['systemctl','stop',*main_installer.manager.names(main_installer.manager.services(c,c['default_mode']))],check=False)
    main_installer.restore_network(before,c)
    for name in main_installer.render_units(c):(main_installer.UNITS/name).unlink(missing_ok=True)
    for name in ['uav-switch','uav-record','uav-lidar-record']:(main_installer.LAUNCHER.parent/name).unlink(missing_ok=True)
    shutil.rmtree(main_installer.BASE)
    main_installer.STATE.unlink(missing_ok=True)
    run(['systemctl','daemon-reload'],check=False)

def deployment_spec(args,c):
    return {'schema':1,'config':c,'lidar':None if args.without_lidar else
        {'interface':args.lidar_iface,'host_cidr':args.lidar_address,'lidar_ip':args.lidar_ip}}

def check_repeat(args,c):
    path=main_installer.BASE/'full-deployment.json'
    if not path.is_file():raise ValueError('已有主控缺少完整部署记录，不能当作全新板覆盖；请使用相应升级工具')
    receipt=json.loads(path.read_text())
    if receipt.get('spec')!=deployment_spec(args,c):raise ValueError('完整部署参数与上次不同，保持原状；修改网口/雷达配置需明确迁移')
    for name,digest in receipt['files'].items():
        p=Path(name)
        if not p.is_file() or p.is_symlink() or hashlib.sha256(p.read_bytes()).hexdigest()!=digest:
            raise ValueError('已有完整部署文件缺失或改变，未覆盖：'+name)
    plan=storage.preflight('main',args.data_root or '/data',args.data_uuid)
    if plan.get('persistence') or not plan.get('existing_logs'):
        raise ValueError('数据盘开机挂载或日志配置已改变，未静默覆盖')
    for name in receipt['enabled']:
        if run(['systemctl','is-enabled',name],check=False).returncode:
            raise ValueError('原开机配置已改变，未自动覆盖：'+name)

def install_main(args,extra):
    lidar=module('lidar_setup','tools/seyond/install_runtime.py')
    cli=main_installer.arguments(['install',*extra]+(['--yes'] if args.yes else []))
    cli.allow_missing_cameras=True
    # eth1 is the current vendor board's dedicated T1/J16, never silently MK22.
    cli.reserved_ifaces=() if args.without_lidar else (args.lidar_iface,)
    c=main_installer.make_config(cli)
    if not args.without_lidar and args.lidar_iface in (c['payload_iface'],c['mk22_iface'],c['extension_iface']):
        raise ValueError('雷达口不能与板间/MK22/云台扩展口共用；请重新选择实际网口')
    if main_installer.same_install(c):
        check_repeat(args,c)
        print('主控相同版本与配置已安装，未改动或重启。需要升级旧板时请使用对应升级工具。')
        return
    main_installer.assert_fresh()
    problems=main_installer.doctor(c)
    if problems:raise ValueError('\n'.join(problems))
    net=main_network.preflight(c)
    disk=storage_plan('main',args)
    radar=None if args.without_lidar else lidar.prepare_install(interface=args.lidar_iface,
        host_cidr=args.lidar_address,lidar_ip=args.lidar_ip,
        reserved_ifaces=(c['payload_iface'],c['mk22_iface'],c['extension_iface']))
    print(json.dumps({'role':'main','config':c,'lidar_interface':None if radar is None else args.lidar_iface},ensure_ascii=False,indent=2))
    if args.doctor:
        print('预检查通过；未安装、未改动网络和服务。');return
    receipts=[];app_installed=False
    before=main_installer.interfaces()
    try:
        receipts.append((storage.rollback,storage.apply(disk)))
        receipts.append((main_network.rollback,main_network.apply(net,start=not args.no_start)))
        main_installer.install(c,start=False);app_installed=True
        if radar:receipts.append((lidar.rollback,lidar.apply_plan(radar,start=not args.no_start)))
        if not args.no_start:
            run(['systemctl','start','uav-record-api.service'])
            run(['systemctl','start',main_installer.BOOT],timeout=120)
        managed=[Path(f['path']) for f in radar['files']] if radar else []
        if main_network.CONF.is_file():managed.append(main_network.CONF)
        receipt={'spec':deployment_spec(args,c),'files':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in managed},
            'enabled':[main_installer.BOOT,'uav-record-api.service']+(['uav-lidar-network.service'] if radar else [])}
        (main_installer.BASE/'full-deployment.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
        print('完整主控部署完成。开机启动视频与控制接口，录像/雷达点云均按需开始。')
        print('检查：uav-switch；雷达：sudo uav-lidar-record status；数据录制也可由地面站操作。')
    except BaseException:
        errors=[]
        # Lidar's receipt can include a file inside /opt/uav-switch: undo it first.
        while receipts and receipts[-1][0]==lidar.rollback:
            undo,receipt=receipts.pop()
            try:undo_checked(undo,receipt)
            except Exception as error:errors.append(str(error))
        if app_installed:
            try:rollback_main(c,before)
            except Exception as error:errors.append(str(error))
        for undo,receipt in reversed(receipts):
            try:undo_checked(undo,receipt)
            except Exception as error:errors.append(str(error))
        if errors:print('回退需检查：'+'; '.join(errors),file=sys.stderr)
        raise
    if not args.no_start:
        try:
            r=run(['/usr/bin/python3',str(main_installer.BASE/'manager.py'),'verify'],check=False,timeout=65)
            print(r.stdout or r.stderr)
            if r.returncode:print('部分外设未接入或尚未出图，请按上面的逐路结果验收；不影响其他路安装。')
        except subprocess.TimeoutExpired:
            print('出图检查超时；安装已完成，请运行 uav-switch 单独验证，不撤销已装服务。')

def install_algorithm(args,extra):
    spec=json.loads((ROOT/'algorithm/runtime-release.json').read_text())
    asset=runtime_asset(spec,args.runtime_bundle,args.offline)
    sys.path.insert(0,str(ROOT/'algorithm'))
    full=module('full_algorithm_setup','algorithm/full_install.py')
    full.bundle.read_manifest(asset,spec['sha256'])
    # Refuse old capture owners before altering logging or installing libraries.
    old=full.BASE/'installation-full.json'
    if full.BASE.exists() and not old.is_file():raise ValueError('已有算法部署，保持原状；完整安装入口用于新镜像')
    disk=storage_plan('algorithm',args)
    data_root=disk['data_root'] if 'data_root' in disk else disk['root']
    command=['/usr/bin/python3',str(ROOT/'algorithm/full_install.py'),
        'doctor' if args.doctor else 'install','--bundle',str(asset),'--sha256',spec['sha256'],
        '--data-root',data_root,*extra]
    if args.no_start:command.append('--no-start')
    parsed = full.arguments(command[2:])
    full.preflight_arguments(parsed)
    receipt=None
    try:
        if not args.doctor:receipt=storage.apply(disk)
        subprocess.run(command,check=True)
    except BaseException:
        if receipt:
            try:undo_checked(storage.rollback,receipt)
            except Exception as e:print('日志/存储回退需检查：'+str(e),file=sys.stderr)
        raise

def undo_checked(undo,receipt):
    result=undo(receipt)
    if isinstance(result,dict) and (result.get('restored') is False or result.get('errors')):
        raise RuntimeError('回退未完整完成：'+json.dumps(result,ensure_ascii=False))

def arguments(argv=None):
    p=argparse.ArgumentParser(description=__doc__,epilog='角色参数继续传给对应安装器；主控支持 --mode/--mk22-iface 等，算法支持 --iface/--front1 等。')
    p.add_argument('--role',choices=['main','algorithm','algorithm-video'])
    p.add_argument('--yes',action='store_true')
    p.add_argument('--doctor',action='store_true',help='只读预检查，不安装依赖')
    p.add_argument('--offline',action='store_true',help='不联网下载和补装依赖')
    p.add_argument('--no-start',action='store_true',help='应用服务下次开机启动；存储和日志准备立即生效')
    p.add_argument('--data-root')
    p.add_argument('--data-uuid',help='显式选择已有 ext4 分区 UUID；不会格式化')
    p.add_argument('--runtime-bundle',help='算法运行包本地路径')
    p.add_argument('--without-lidar',action='store_true')
    p.add_argument('--lidar-iface',default='eth1')
    p.add_argument('--lidar-address',default='172.168.1.100/24')
    p.add_argument('--lidar-ip',default='172.168.1.10')
    return p.parse_known_args(argv)

def main(argv=None):
    args,extra=arguments(argv)
    if os.geteuid()!=0:raise ValueError('请运行 sudo bash deploy.sh')
    main_installer.verify_manifest();main_installer.platform_check()
    args.role=choose_role(args.role,args.yes)
    if args.role=='algorithm-video':
        command=['bash',str(ROOT/'algorithm/install.sh')]+(['doctor'] if args.doctor else [])+extra
        if args.no_start:command.append('--no-start')
        if args.offline:
            sys.path.insert(0,str(ROOT/'algorithm'))
            legacy=module('legacy_algorithm_setup','algorithm/install.py')
            legacy.deps(install=False)
            command=['/usr/bin/python3',str(ROOT/'algorithm/install.py')]+(['doctor'] if args.doctor else [])+extra
            if args.no_start:command.append('--no-start')
        subprocess.run(command,check=True);return
    # Never modify system packages on an unrelated deployment.
    if args.role=='main' and main_installer.BASE.exists() and not (main_installer.BASE/main_installer.RECEIPT).is_file():
        raise ValueError('已有主控部署，保持原状；此入口用于新板，不能覆盖旧服务')
    if args.role=='algorithm' and Path('/opt/uav-front-streams').exists() and not Path('/opt/uav-front-streams/installation-full.json').is_file():
        raise ValueError('已有算法部署，保持原状；此入口用于新板')
    with open('/run/uav-full-deploy.lock','w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if not args.doctor:dependencies(args.role,args.offline)
        if args.role=='main':install_main(args,extra)
        else:install_algorithm(args,extra)

if __name__=='__main__':
    try:main()
    except (ValueError,RuntimeError,OSError,KeyError,subprocess.SubprocessError) as e:
        print('未完成：'+str(e),file=sys.stderr);sys.exit(1)
    except (KeyboardInterrupt,EOFError):print('已取消。',file=sys.stderr);sys.exit(1)
