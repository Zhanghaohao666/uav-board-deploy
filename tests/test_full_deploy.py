import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('full_deploy_test',ROOT/'deploy.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
C=json.loads((ROOT/'config.example.json').read_text());C['mk22_iface']='usb-mk22'

class DeployTests(unittest.TestCase):
 def test_role_arguments_remain_unambiguous(self):
  a,x=m.arguments(['--role','algorithm','--no-start','--offline','--iface','eth0','--user','dev'])
  self.assertEqual(a.role,'algorithm');self.assertTrue(a.no_start and a.offline)
  self.assertEqual(x,['--iface','eth0','--user','dev'])
  with self.assertRaises(ValueError):m.choose_role(None,True)
 def test_offline_does_not_fetch(self):
  with tempfile.TemporaryDirectory() as d,patch.object(m.urllib.request,'urlopen') as call:
   p=Path(d)/'a.tar.gz';p.write_bytes(b'opaque archive')
   s={'file':'a.tar.gz','sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
   self.assertEqual(m.runtime_asset(s,p,True),p)
   p.write_bytes(b'changed')
   with self.assertRaisesRegex(ValueError,'SHA256'):m.runtime_asset(s,p,True)
   call.assert_not_called()
 def test_download_destination_escape_rejected(self):
  with self.assertRaises(ValueError):m.runtime_asset({'file':'../a.tar.gz','sha256':'0'*64},offline=True)
 def test_offline_missing_packages_are_not_installed(self):
  with patch.object(m,'run',return_value=subprocess.CompletedProcess([],1,'','')),patch.object(m.subprocess,'run') as install:
   with self.assertRaisesRegex(ValueError,'离线镜像缺少'):m.dependencies('algorithm',True)
   install.assert_not_called()
 def test_repeat_checks_radar_not_only_video_receipt(self):
  a,_=m.arguments(['--role','main'])
  with tempfile.TemporaryDirectory() as d:
   base=Path(d);f=base/'radar';f.write_bytes(b'tool')
   r={'spec':m.deployment_spec(a,C),'files':{str(f):hashlib.sha256(f.read_bytes()).hexdigest()},'enabled':['uav-lidar-network.service']}
   (base/'full-deployment.json').write_text(json.dumps(r))
   with patch.object(m.main_installer,'BASE',base),patch.object(m.storage,'preflight',return_value={'existing_logs':True}),patch.object(m,'run',return_value=subprocess.CompletedProcess([],0,'','')):
    m.check_repeat(a,C)
    a.without_lidar=True
    with self.assertRaisesRegex(ValueError,'参数与上次不同'):m.check_repeat(a,C)
    a.without_lidar=False;f.unlink()
    with self.assertRaisesRegex(ValueError,'缺失'):m.check_repeat(a,C)
 def test_incomplete_rollback_is_reported(self):
  with self.assertRaisesRegex(RuntimeError,'回退未完整'):
   m.undo_checked(lambda _: {'restored':False,'errors':['log mount busy']},{})
 def pipeline(self,fail=None,no_start=False):
  events=[]
  a,_=m.arguments(['--role','main']+(['--no-start'] if no_start else []))
  radar=types.SimpleNamespace(prepare_install=lambda **k:{'files':[]},
   apply_plan=lambda p,**k:events.append(('lidar_apply',k)) or ('radar_receipt'),
   rollback=lambda p:events.append(('lidar_undo',p)))
  def app(c,start):events.append(('app',start))
  def runner(cmd,**kwargs):
   events.append(('command',cmd))
   if fail=='start' and cmd==['systemctl','start',m.main_installer.BOOT]:raise RuntimeError('start failed')
   return subprocess.CompletedProcess(cmd,0,'{}','')
  with tempfile.TemporaryDirectory() as d:
   with patch.object(m,'module',return_value=radar),patch.object(m.main_installer,'arguments',return_value=types.SimpleNamespace()),patch.object(m.main_installer,'make_config',return_value=copy.deepcopy(C)),patch.object(m.main_installer,'same_install',return_value=False),patch.object(m.main_installer,'assert_fresh'),patch.object(m.main_installer,'doctor',return_value=[]),patch.object(m.main_installer,'interfaces',return_value=[]),patch.object(m.main_installer,'install',side_effect=app),patch.object(m.main_installer,'BASE',Path(d)),patch.object(m.main_network,'CONF',Path(d)/'absent'),patch.object(m.main_network,'preflight',return_value={}),patch.object(m.main_network,'apply',side_effect=lambda p,**k:events.append(('net_apply',k)) or 'net_receipt'),patch.object(m.main_network,'rollback',side_effect=lambda p:events.append(('net_undo',p))),patch.object(m,'storage_plan',return_value={}),patch.object(m.storage,'apply',side_effect=lambda p:events.append(('storage_apply',p)) or 'disk_receipt'),patch.object(m.storage,'rollback',side_effect=lambda p:events.append(('storage_undo',p))),patch.object(m,'rollback_main',side_effect=lambda *p:events.append(('app_undo',None))),patch.object(m,'run',side_effect=runner):
    if fail:
     with self.assertRaisesRegex(RuntimeError,'start failed'):m.install_main(a,[])
    else:
     m.install_main(a,[])
     self.assertTrue((Path(d)/'full-deployment.json').is_file())
  return events
 def test_pipeline_stages_without_start_then_adds_lidar_before_boot(self):
  e=self.pipeline();names=[i[0] for i in e]
  self.assertLess(names.index('app'),names.index('lidar_apply'))
  self.assertEqual(e[names.index('app')],('app',False))
  self.assertNotIn('app_undo',names)
 def test_start_failure_rolls_back_reverse_order(self):
  e=self.pipeline('start');self.assertEqual([x[0] for x in e if x[0].endswith('undo')],['lidar_undo','app_undo','net_undo','storage_undo'])
 def test_no_start_changes_no_live_app_network(self):
  e=self.pipeline(no_start=True)
  self.assertIn(('net_apply',{'start':False}),e)
  self.assertIn(('lidar_apply',{'start':False}),e)
  self.assertFalse(any(x[0]=='command' and x[1][:2]==['systemctl','start'] for x in e))

if __name__=='__main__':unittest.main()
