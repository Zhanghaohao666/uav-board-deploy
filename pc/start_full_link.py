#!/usr/bin/env python3
"""PC console for the explicitly selected UAV test mode."""
import argparse,getpass,json,shlex,sys
from pathlib import Path
try:import paramiko
except ImportError:
 print('缺少 paramiko，请运行：python -m pip install -r requirements.txt');sys.exit(1)
LABEL={'algorithm':'单网口 · 算法板','gimbal':'单网口 · 云台','dual':'双网口 · 并行测试'}
WIRE={'algorithm':'算法板接主控 eth0，MK22 接线保持原样。','gimbal':'云台接主控 eth0，MK22 接线保持原样。','dual':'算法板接 eth0，云台接扩展网口，MK22 接线保持原样。'}

def display(obj):
 if 'error' in obj:print('失败：'+obj['error']);return
 if 'applied_mode' in obj:
  print('已应用：'+LABEL[obj['applied_mode']]);print('开机默认：'+LABEL[obj['default_mode']]);print(obj['video_validation'])
 elif 'mode' in obj:
  print('当前模式：'+LABEL[obj['mode']]+'；开机默认：'+LABEL[obj['default_mode']]);print(obj['network'])
  for name,state in obj['services'].items():print('  '+name+': '+state)
  for peer,ok in obj['peer_reachable'].items():print('  '+peer+(' 可达' if ok else ' 未响应，请检查接线/供电'))
 elif 'streams' in obj:
  for row in obj['streams']:
   print(('通过 ' if row['decoded'] else '失败 ')+row['name']+' '+row['url'])
   if not row['decoded']:print('  '+row['detail'].strip())
  print('全部画面验证通过。' if obj['all_decoded'] else '部分画面未通过；服务运行不等于视频已经出图。')
 elif 'default_mode' in obj:print('开机默认已保存：'+LABEL[obj['default_mode']])
 for title,url in obj.get('urls',[]):print(title+'：'+url)

class Board:
 def __init__(self,hosts,user,password):
  self.client=None;self.password=password
  known=Path.home()/'.uav-switch-known-hosts';known.touch(exist_ok=True)
  errors=[]
  for host in hosts:
   c=paramiko.SSHClient();c.load_system_host_keys();c.load_host_keys(str(known));c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
   try:
    c.connect(host,username=user,password=password,timeout=5,auth_timeout=8,banner_timeout=8,look_for_keys=False,allow_agent=False)
    c.save_host_keys(str(known));c.get_transport().set_keepalive(10);self.client=c;print('已连接主控：'+host);break
   except Exception as e:errors.append(host+': '+str(e));c.close()
  if self.client is None:raise RuntimeError('\n'.join(errors))
 def execute(self,action,value=None,save=False):
  argv=['/usr/bin/python3','/opt/uav-switch/manager.py',action]
  if value:argv.append(value)
  if save:argv.append('--save-default')
  sudo=action in ('switch','save-default')
  command=('sudo -S -p "" ' if sudo else '')+shlex.join(argv)
  stdin,stdout,stderr=self.client.exec_command(command,timeout=120)
  if sudo:stdin.write(self.password+'\n');stdin.flush()
  stdin.channel.shutdown_write()
  out=stdout.read().decode('utf-8',errors='replace');err=stderr.read().decode('utf-8',errors='replace');code=stdout.channel.recv_exit_status()
  try:obj=json.loads(out or err)
  except ValueError:raise RuntimeError((err+'\n'+out).strip())
  display(obj)
  if code and action!='verify':raise RuntimeError('板端操作未完成，请查看上面的错误')
  return code
 def close(self):self.client.close()

def main():
 try:sys.stdout.reconfigure(encoding='utf-8')
 except (AttributeError,OSError):pass
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--host',help='默认先尝试192.168.1.104，再尝试192.168.2.36');p.add_argument('--user',default='dev');p.add_argument('--mode',choices=LABEL);p.add_argument('--save-default',action='store_true');p.add_argument('--status',action='store_true');p.add_argument('--verify',action='store_true');a=p.parse_args()
 if a.save_default and not a.mode:p.error('--save-default 需要配合 --mode')
 if sum(bool(x) for x in (a.mode,a.status,a.verify))>1:p.error('--mode、--status、--verify 只能选一个')
 password=getpass.getpass('主控 SSH 密码（仅本次使用，不保存）：')
 board=Board([a.host] if a.host else ['192.168.1.104','192.168.2.36'],a.user,password)
 try:
  if a.status:return board.execute('status')
  if a.verify:return board.execute('verify')
  if a.mode:
   print(WIRE[a.mode]);board.execute('switch',a.mode,a.save_default);return board.execute('verify')
  while True:
   print('\n1 算法板单网口\n2 云台单网口\n3 双网口并行\n4 查看状态与流地址\n5 验证视频出图\n6 将当前模式设为开机默认\n0 退出')
   choice=input('请选择：').strip()
   if choice=='0':return 0
   try:
    if choice in ('1','2','3'):
     mode={'1':'algorithm','2':'gimbal','3':'dual'}[choice];print(WIRE[mode]);print('本次是临时切换；相关视频/控制会短暂重连。')
     if input('接线就绪后输入 y 执行：').strip().lower()!='y':continue
     board.execute('switch',mode);board.execute('verify')
    elif choice=='4':board.execute('status')
    elif choice=='5':board.execute('verify')
    elif choice=='6':board.execute('save-default')
   except Exception as e:print('操作失败：'+str(e))
 finally:board.close()
if __name__=='__main__':
 try:sys.exit(main())
 except (KeyboardInterrupt,EOFError):print('\n已退出。')
 except Exception as e:print('错误：'+str(e));sys.exit(1)
