#!/usr/bin/env python3
"""Upgrade an existing main board's video binaries; preserve network/mode/recorder."""
import argparse, datetime, fcntl, hashlib, importlib.util, json, os
from pathlib import Path
import shutil, subprocess, tempfile

ROOT=Path(__file__).resolve().parent
BASE=Path('/opt/uav-switch')
BACKUPS=Path('/data/uav-backups')
STATE=Path('/var/lib/uav-switch/active.json')
FILES={'board/manager.py':'manager.py','board/bin/rk_streamer':'bin/rk_streamer',
       'board/bin/rtsp_relay_mino17':'bin/rtsp_relay_mino17',
       'board/bin/rtsp_relay_gimbal':'bin/rtsp_relay_gimbal'}

def call(argv,check=True):
    return subprocess.run(argv,check=check,capture_output=True,text=True,timeout=45)

def atomic_file(source,target):
    target=Path(target)
    fd,name=tempfile.mkstemp(prefix='.'+target.name+'.low-',dir=target.parent)
    os.close(fd);tmp=Path(name)
    try:
        shutil.copy2(source,tmp)
        os.chmod(tmp,0o755)
        with tmp.open('rb') as handle:os.fsync(handle.fileno())
        os.replace(tmp,target)
    finally:
        if tmp.exists():tmp.unlink()

def installer_module():
    spec=importlib.util.spec_from_file_location('uav_installer',ROOT/'install.py')
    installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)
    return installer

def update(no_start=False):
    installer=installer_module()
    installer.verify_manifest();installer.platform_check()
    if not os.path.ismount('/data'):raise ValueError('/data 数据盘没有挂载，拒绝把备份写到系统盘')
    for rel in FILES.values():
        if not (BASE/rel).is_file():raise ValueError('没有识别到完整主控部署：'+str(BASE/rel))
    changes={source:target for source,target in FILES.items()
             if hashlib.sha256((ROOT/source).read_bytes()).digest()!=hashlib.sha256((BASE/target).read_bytes()).digest()}
    if not changes:
        print('主控低清程序已经是本版本，未重启服务。');return
    config=json.loads((BASE/'config.json').read_text())
    mode=json.loads(STATE.read_text())['mode'] if STATE.exists() else config['default_mode']
    if mode not in ('algorithm','gimbal','dual'):raise ValueError('未知当前模式')
    env=dict(os.environ,LD_LIBRARY_PATH=str(BASE/'lib'))
    for source in FILES:
        if '/bin/' not in source:continue
        result=subprocess.run(['ldd',str(ROOT/source)],env=env,capture_output=True,text=True)
        if result.returncode or 'not found' in result.stdout:raise ValueError('新程序缺少运行库：'+source+'\n'+result.stdout+result.stderr)
    units=['uav-switch@'+s+'.service' for s in ['board','d455','relay','visible-relay','infrared-relay']]
    active=[u for u in units if call(['systemctl','is-active',u],check=False).returncode==0]
    backup=BACKUPS/('low_stream_'+datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
    backup.mkdir(parents=True)
    for rel in changes.values():
        target=backup/rel;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(BASE/rel,target)
    (backup/'upgrade.json').write_text(json.dumps({'mode':mode,'active':active,'files':changes},indent=2)+'\n')
    try:
        if active and not no_start:call(['systemctl','stop']+active)
        for source,target in changes.items():atomic_file(ROOT/source,BASE/target)
        if active and not no_start:call(['systemctl','start']+active)
    except Exception:
        if active and not no_start:call(['systemctl','stop']+active,check=False)
        for rel in changes.values():atomic_file(backup/rel,BASE/rel)
        if active and not no_start:call(['systemctl','start']+active,check=False)
        raise
    (BASE/'low-stream-upgrade.json').write_text(json.dumps({'version':'v1.4.0','backup':str(backup),'mode':mode,'restarted':not no_start},indent=2)+'\n')
    print('已更新主控视频程序；模式和网络保持原配置：'+mode)
    print('备份：'+str(backup))
    print('当前进程保留，低清将在下次服务重启/开机生效。' if no_start else '已恢复原来运行的视频服务；载荷端也需要匹配的低清程序。')

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--no-start',action='store_true');args=p.parse_args()
    if os.geteuid()!=0:p.error('请使用 sudo python3 update_low_streams.py')
    with open('/run/uav-switch.lock','w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);update(args.no_start)

if __name__=='__main__':main()
