#!/usr/bin/env python3
"""Explicit payload mode manager; shared cameras stay running across mode changes."""
import argparse, concurrent.futures, fcntl, ipaddress, json, os, re, socket, subprocess, sys, time
from pathlib import Path
BASE=Path(__file__).resolve().parent
CONFIG=BASE/'config.json'
STATE=Path('/var/lib/uav-switch/active.json')
MODES=('algorithm','gimbal','dual')
OWNED={'192.168.10.1/24','192.168.144.1/24'}

def run(cmd, check=True, timeout=25):
 return subprocess.run(cmd,text=True,capture_output=True,check=check,timeout=timeout)
def config():
 c=json.loads(CONFIG.read_text())
 for key in ('payload_iface','extension_iface','mk22_iface'):
  if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}',c[key]):raise ValueError('Invalid interface '+key)
 if len({c[k] for k in ('payload_iface','extension_iface','mk22_iface')})!=3:raise ValueError('Interface roles must differ')
 if c['default_mode'] not in MODES:raise ValueError('Invalid default mode')
 if ipaddress.IPv4Interface(c['mk22_address']).network.overlaps(ipaddress.IPv4Network('192.168.144.0/24')) or ipaddress.IPv4Interface(c['mk22_address']).network.overlaps(ipaddress.IPv4Network('192.168.10.0/24')):raise ValueError('MK22 subnet overlaps payload')
 return c
def atomic(path,obj):
 path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n');os.replace(tmp,path)
def current(c):
 return json.loads(STATE.read_text())['mode'] if STATE.exists() else c['default_mode']
def services(c,mode):
 if mode not in MODES:raise ValueError('Unknown mode')
 b=BASE/'bin';cmd={
 'board':[str(b/'rk_streamer'),'--host','127.0.0.1','--format','yuv','--codec','h264','--rc','cbr','--fps','30','--cam',c['board_camera'],'1280x720','5702','input=nv12,bitrate=3000,fps=30'],
 'relay':[str(b/'rtsp_relay_mino17'),'--port','8554','--mount','/board','5702','h264']}
 if c['d455_enabled']:
  cmd['d455']=[str(b/'rk_streamer'),'--host','127.0.0.1','--format','yuv','--codec','h264','--rc','cbr','--fps','30','--cam',c['d455_rgb'],'640x480','5703','input=yuyv,bitrate=2000,fps=15']
  cmd['relay']+=['--mount','/rgb','5703','h264']
  if c.get('d455_depth_enabled',False):
   cmd['d455']+=['--cam',c['d455_depth'],'640x480','5704','input=z16,bitrate=1500,fps=30,depth_min=300,depth_max=5000']
   cmd['relay']+=['--mount','/depth','5704','h264']
 if mode in ('algorithm','dual'):
  for mount,port in [('algorithm',5602),('preview',5603),('infrared',5604)]:cmd['relay']+=['--mount','/'+mount,str(port),'h264']
  cmd['algo-control']=['/usr/bin/socat','TCP-LISTEN:9001,fork,reuseaddr','TCP:192.168.10.2:9000']
 if mode in ('gimbal','dual'):
  cmd['gimbal-control']=['/usr/bin/socat','TCP-LISTEN:9000,fork,reuseaddr','TCP:192.168.144.108:9000']
  for key,path,rtp,rtsp,mount in [('visible',0,5612,8555,'gimbal'),('infrared',1,5610,8556,'gimbal_ir')]:
   cmd[key+'-ingest']=['/usr/bin/ffmpeg','-nostdin','-hide_banner','-loglevel','warning','-rtsp_transport','tcp',c.get('ffmpeg_timeout_option','-stimeout'),'5000000','-i',f'rtsp://192.168.144.108:554/live/{path}','-map','0:v:0','-c:v','copy','-bsf:v','dump_extra','-an','-f','rtp',f'rtp://127.0.0.1:{rtp}']
   cmd[key+'-relay']=[str(b/'rtsp_relay_gimbal'),'--port',str(rtsp),'--mount','/'+mount,str(rtp),'h264']
 return cmd

def addrmap(infos):
 return {i['ifname']:{a['local']+'/'+str(a['prefixlen']) for a in i.get('addr_info',[]) if a.get('family')=='inet'} for i in infos}
def desired(c,mode):
 d={c['payload_iface']:{'192.168.10.1/24'},c['mk22_iface']:{c['mk22_address']}}
 d.setdefault(c['extension_iface'],set())
 d[c['extension_iface'] if mode=='dual' else c['payload_iface']].add('192.168.144.1/24')
 return d

def netplan(c,mode,infos):
 have=addrmap(infos);want=desired(c,mode)
 required={c['payload_iface'],c['mk22_iface']} | ({c['extension_iface']} if mode=='dual' else set())
 for n in required:
  if n not in have:raise ValueError('所需网卡不存在：'+n)
 # Refuse other interfaces owning either subnet, or unexpected overlapping addresses.
 for n,addresses in have.items():
  for a in addresses:
   network=ipaddress.IPv4Interface(a).network
   for iface,ips in want.items():
    for ip in ips:
     if not network.overlaps(ipaddress.IPv4Interface(ip).network):continue
     transferable = a in OWNED and n in (c['payload_iface'],c['extension_iface'])
     if not transferable and not (n==iface and a==ip):raise ValueError('网段冲突：'+n+' '+a)
 deletions=[];additions=[]
 for n in (c['payload_iface'],c['extension_iface']):
  for a in sorted(have.get(n,set()) & OWNED - want[n]):deletions.append(['ip','addr','del',a,'dev',n])
 for n in sorted(required):
  additions.append(['ip','link','set','dev',n,'up'])
  for a in sorted(want[n]-have.get(n,set())):additions.append(['ip','addr','add',a,'dev',n])
 return deletions+additions

def names(keys):return ['uav-switch@'+k+'.service' for k in keys]
def endpoint_list(c,mode):
 ip=str(ipaddress.IPv4Interface(c['mk22_address']).ip)
 rows=[('下视MIPI',f'rtsp://{ip}:8554/board')]
 if c['d455_enabled']:rows.append(('D455彩色',f'rtsp://{ip}:8554/rgb'))
 if c.get('d455_depth_enabled'):rows.append(('D455深度',f'rtsp://{ip}:8554/depth'))
 if mode in ('algorithm','dual'):rows += [(n,f'rtsp://{ip}:8554/{n}') for n in ('algorithm','preview','infrared')]
 if mode in ('gimbal','dual'):rows += [('吊舱可见光',f'rtsp://{ip}:8555/gimbal'),('吊舱红外',f'rtsp://{ip}:8556/gimbal_ir')]
 return rows

def ownership_check(c,mode):
 ports=[8554]+([9001] if mode=='algorithm' else [8555,8556,9000] if mode=='gimbal' else [8555,8556,9000,9001])
 pids=set()
 for port in ports:
  r=run(['fuser','-n','tcp',str(port)],check=False)
  pids.update(int(x) for x in r.stdout.split() if x.isdigit())
 for dev in [c['board_camera']]+([c['d455_rgb']] if c['d455_enabled'] else []):
  r=run(['fuser',dev],check=False);pids.update(int(x) for x in r.stdout.split() if x.isdigit())
 for pid in pids:
  try:group=Path(f'/proc/{pid}/cgroup').read_text()
  except (FileNotFoundError,ProcessLookupError):continue
  if 'uav-switch@' not in group:raise ValueError(f'端口或相机被工具以外的进程占用，PID={pid}；请明确停止该进程后再切换')

def change(c,mode,save=False):
 if os.geteuid()!=0:raise ValueError('切换需要管理员权限')
 before=json.loads(run(['ip','-j','address','show']).stdout)
 plan=netplan(c,mode,before) # Validate before stopping anything.
 ownership_check(c,mode)
 old=current(c);oldcmd=services(c,old);newcmd=services(c,mode)
 stop=[k for k,v in oldcmd.items() if k not in newcmd or newcmd[k]!=v]
 # Gimbal network can move while its commands stay identical; restart that leg.
 if (old=='dual')!=(mode=='dual'):
  stop+= [k for k in oldcmd if k.startswith(('visible-','infrared-')) or k=='gimbal-control']
 stop=sorted(set(stop));inverse=[]
 try:
  if stop:run(['systemctl','stop']+names(stop))
  for cmd in plan:
   run(cmd)
   if cmd[1]=='addr':inverse.insert(0,cmd[:2]+(['add'] if cmd[2]=='del' else ['del'])+cmd[3:])
  atomic(STATE,{'mode':mode,'changed_at':time.strftime('%Y-%m-%d %H:%M:%S')})
  run(['systemctl','start']+names(newcmd),timeout=40)
  if save:c['default_mode']=mode;atomic(CONFIG,c)
 except Exception:
  # Roll back only commands actually completed, not arbitrary NIC state.
  run(['systemctl','stop']+names([k for k in newcmd if k not in oldcmd or k in stop]),check=False)
  for cmd in inverse:run(cmd,check=False)
  atomic(STATE,{'mode':old,'rollback':True})
  run(['systemctl','start']+names(oldcmd),check=False,timeout=40)
  raise
 return {'applied_mode':mode,'default_mode':c['default_mode'],'video_validation':'尚未做出图验证，请运行 verify','urls':endpoint_list(c,mode)}

def status(c):
 mode=current(c);keys=services(c,mode)
 checks={k:run(['systemctl','is-active','uav-switch@'+k+'.service'],check=False).stdout.strip() for k in keys}
 peers=['192.168.10.2'] if mode=='algorithm' else ['192.168.144.108'] if mode=='gimbal' else ['192.168.10.2','192.168.144.108']
 reach={p:run(['ping','-c','1','-W','1',p],check=False).returncode==0 for p in peers}
 return {'mode':mode,'default_mode':c['default_mode'],'services':checks,'peer_reachable':reach,'network':run(['ip','-br','-4','addr']).stdout,'urls':endpoint_list(c,mode),'control_ports':{'algorithm':9001,'gimbal':9000}}

def verify(c):
 def one(item):
  title,url=item
  try:
   r=run(['/usr/bin/ffmpeg','-nostdin','-hide_banner','-loglevel','error','-rtsp_transport','tcp',c.get('ffmpeg_timeout_option','-stimeout'),'3000000','-i',url,'-an','-frames:v','1','-f','null','-'],check=False,timeout=12)
   return {'name':title,'url':url,'decoded':r.returncode==0,'detail':r.stderr[-1200:]}
  except subprocess.TimeoutExpired:return {'name':title,'url':url,'decoded':False,'detail':'抓帧超时'}
 with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(one,endpoint_list(c,current(c))))
 return {'all_decoded':all(r['decoded'] for r in results),'streams':results}

def main():
 p=argparse.ArgumentParser();p.add_argument('action',choices=['switch','boot','status','verify','save-default','worker']);p.add_argument('value',nargs='?');p.add_argument('--save-default',action='store_true');a=p.parse_args();c=config()
 if a.action=='worker':
  cmd=services(c,current(c)).get(a.value)
  if not cmd:raise ValueError('当前模式不包含此服务')
  os.execv(cmd[0],cmd)
 if a.action in ('switch','boot','save-default'):
  if os.geteuid()!=0:raise ValueError('需要管理员权限')
  with open('/run/uav-switch.lock','w') as lock:
   try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
   except BlockingIOError:raise ValueError('另一项切换正在执行，请稍后再试')
   if a.action=='save-default':
    c['default_mode']=current(c);atomic(CONFIG,c);result={'default_mode':c['default_mode']}
   else:
    mode=c['default_mode'] if a.action=='boot' else a.value
    if mode not in MODES:raise ValueError('请选择 algorithm、gimbal 或 dual')
    result=change(c,mode,a.save_default)
 elif a.action=='status':result=status(c)
 else:result=verify(c)
 print(json.dumps(result,ensure_ascii=False,indent=2))
 if a.action=='verify' and not result['all_decoded']:return 2
 return 0
if __name__=='__main__':
 try:sys.exit(main())
 except Exception as e:print(json.dumps({'error':str(e)},ensure_ascii=False),file=sys.stderr);sys.exit(1)
