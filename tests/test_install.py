import copy, importlib.util, json, stat, subprocess, tempfile, unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('installer',ROOT/'install.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
C=json.loads((ROOT/'config.example.json').read_text())
def iface(name,*addresses):
 return dict(ifname=name,flags=['UP'],link_type='ether',addr_info=[dict(family='inet',local=a.split('/')[0],prefixlen=int(a.split('/')[1])) for a in addresses])
class InstallerTests(unittest.TestCase):
 def test_fresh_cif_format_negotiation_is_allowed(self):
  empty=subprocess.CompletedProcess([],0,"Width/Height : 0/0\nPixel Format : ''\n",'')
  formats=subprocess.CompletedProcess([],0,"[0]: 'NV12' (Y/CbCr 4:2:0)",'')
  with patch.object(m,'run',side_effect=[empty,formats]):self.assertTrue(m.board_camera_format_ready('/dev/video11',True))
  with patch.object(m,'run',return_value=empty) as call:
   self.assertFalse(m.board_camera_format_ready('/dev/video11',False));self.assertEqual(call.call_count,1)
 def test_bad_or_unknown_camera_format_is_rejected(self):
  for current,formats in [(subprocess.CompletedProcess([],1,'','No such device'),None),(subprocess.CompletedProcess([],0,"Width/Height : 1280/720\nPixel Format : 'UYVY'",''),None),(subprocess.CompletedProcess([],0,"Width/Height : 0/0\nPixel Format : ''",''),subprocess.CompletedProcess([],0,"'YUYV'",'')),(subprocess.CompletedProcess([],0,"Width/Height : 0/0\nPixel Format : ''",''),subprocess.CompletedProcess([],1,"'NV12'",'device error'))]:
   with self.subTest(current=current,formats=formats),patch.object(m,'run',side_effect=[current,formats]):self.assertFalse(m.board_camera_format_ready('/dev/video11',True))
 def test_dma32_required_not_generic_heap(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp);(root/'system').touch();(root/'system-dma32').touch()
   with self.assertRaisesRegex(ValueError,'DMA32'):m.dma32_check(root)
   with patch.object(Path,'stat',return_value=SimpleNamespace(st_mode=stat.S_IFCHR)),patch.object(m.os,'access',return_value=True):
    m.dma32_check(root)
   with patch.object(Path,'stat',return_value=SimpleNamespace(st_mode=stat.S_IFCHR)),patch.object(m.os,'access',return_value=False):
    with self.assertRaisesRegex(ValueError,'DMA32'):m.dma32_check(root)
 def test_same_interface_rejected(self):
  c=copy.deepcopy(C);c['mk22_iface']=c['payload_iface']
  with self.assertRaises(ValueError):m.validate_config(c)
 def test_subnet_collision_rejected(self):
  for address in ['192.168.10.5/24','192.168.144.5/24','127.0.0.2/8','192.168.2.255/24']:
   c=copy.deepcopy(C);c['mk22_address']=address
   with self.assertRaises(ValueError):m.validate_config(c)
 def test_multiple_interfaces_not_guessed(self):
  with self.assertRaises(ValueError):m.choose('MK22',['eth1','eth2'],interactive=False)
  self.assertEqual(m.choose('MK22',['eth1','eth2'],specified='eth2',interactive=False),'eth2')
 def test_wifi_not_selected(self):
  self.assertEqual(m.ethernet_names([iface('eth0'),iface('wlx123'),iface('docker0')]),['eth0'])
 def test_ffmpeg_timeout_versions(self):
  self.assertEqual(m.timeout_option('  -stimeout <int> socket I/O\n -timeout <int> listen seconds'),'-stimeout')
  self.assertEqual(m.timeout_option(' -timeout <int64> socket I/O in microseconds'),'-timeout')
  with self.assertRaises(ValueError):m.timeout_option(' -timeout <int> listen seconds')
 def test_service_user_without_injection(self):
  c=copy.deepcopy(C);c['service_user']='pilot'
  self.assertIn('User=pilot\n',m.render_units(c)[m.TEMPLATE])
  c['service_user']='dev\nExecStart=/bin/false'
  with self.assertRaises(ValueError):m.validate_config(c)
 def test_camera_path_rejected(self):
  c=copy.deepcopy(C);c['board_camera']='/dev/../etc/passwd'
  with self.assertRaises(ValueError):m.validate_config(c)
 def test_rollback_only_owned_addresses(self):
  old=[iface('eth0','192.168.10.1/24'),iface('eth1'),iface('eth2','192.168.144.1/24'),iface('wlan0','192.168.1.40/24')]
  new=[iface('eth0','192.168.10.1/24','192.168.144.1/24'),iface('eth1','192.168.2.36/24'),iface('eth2'),iface('wlan0','192.168.1.41/24')]
  with patch.object(m,'interfaces',return_value=new),patch.object(m,'run',return_value=subprocess.CompletedProcess([],0,'','')) as call:
   self.assertEqual(m.restore_network(old,C),[])
  commands=[x.args[0] for x in call.call_args_list]
  self.assertIn(['ip','addr','del','192.168.2.36/24','dev','eth1'],commands)
  self.assertIn(['ip','addr','add','192.168.144.1/24','dev','eth2'],commands)
  self.assertFalse(any('wlan0' in cmd or 'flush' in cmd or 'route' in cmd for cmd in commands))
 def test_install_failure_cleans_up(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp);units=root/'units';units.mkdir();base=root/'opt';state=root/'state.json';launcher=root/'launcher'
   before=[iface('eth0'),iface('eth1'),iface('eth2')]
   def runner(cmd,**kw):
    if cmd==['systemctl','start',m.BOOT]:raise subprocess.CalledProcessError(1,cmd)
    return subprocess.CompletedProcess(cmd,0,'','')
   with patch.multiple(m,BASE=base,UNITS=units,STATE=state,LAUNCHER=launcher,BACKUPS=root/'backups'),patch.object(m.os,'geteuid',return_value=0),patch.object(m,'doctor',return_value=[]),patch.object(m,'run',side_effect=runner),patch.object(m,'interfaces',return_value=before),patch.object(m.manager,'ownership_check'),patch.object(m,'check_duplicate_address'),patch.object(m,'restore_network',return_value=[]) as restore:
    with self.assertRaisesRegex(RuntimeError,'安装失败'):m.install(C)
    restore.assert_called_once_with(before,C)
    self.assertFalse(base.exists());self.assertFalse(launcher.exists());self.assertFalse(state.exists());self.assertEqual(list(units.iterdir()),[])
 def test_successful_install_and_repeat_do_not_restart(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp);units=root/'units';units.mkdir();base=root/'opt';state=root/'state.json';launcher=root/'launcher'
   before=[iface('eth0'),iface('eth1'),iface('eth2')]
   def runner(cmd,**kw):return subprocess.CompletedProcess(cmd,0,'','')
   with patch.multiple(m,BASE=base,UNITS=units,STATE=state,LAUNCHER=launcher,BACKUPS=root/'backups'),patch.object(m.os,'geteuid',return_value=0),patch.object(m,'doctor',return_value=[]),patch.object(m,'run',side_effect=runner) as call,patch.object(m,'interfaces',return_value=before),patch.object(m.manager,'ownership_check'),patch.object(m,'check_duplicate_address'),patch.object(m.time,'sleep'):
    m.install(C)
    self.assertTrue((base/m.RECEIPT).is_file());self.assertTrue(launcher.is_file())
    self.assertTrue(m.same_install(C));call.reset_mock();m.install(C);call.assert_not_called()
    (base/'manager.py').write_text('tampered')
    self.assertFalse(m.same_install(C))
 def test_existing_deployment_not_overwritten(self):
  with tempfile.TemporaryDirectory() as temp:
   base=Path(temp)/'opt';base.mkdir();marker=base/'keep';marker.write_text('existing')
   with patch.object(m,'BASE',base):
    with self.assertRaisesRegex(ValueError,'已有部署'):m.assert_fresh()
   self.assertEqual(marker.read_text(),'existing')
 def test_fresh_systemd_inventory_without_matches(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp)
   with patch.multiple(m,BASE=root/'opt',STATE=root/'state',LAUNCHER=root/'bin/uav-switch',UNITS=root/'units'),patch.object(m,'run',return_value=subprocess.CompletedProcess([],1,'','')) as call:
    m.assert_fresh()
    self.assertEqual(call.call_count,4)
 def test_unit_inventory_failure_is_not_no_matches(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp)
   for result in [subprocess.CompletedProcess([],1,'','Failed to connect to bus'),subprocess.CompletedProcess([],2,'',''),subprocess.CompletedProcess([],0,'uav-video.service enabled','')]:
    with self.subTest(result=result),patch.multiple(m,BASE=root/'opt',STATE=root/'state',LAUNCHER=root/'bin/uav-switch',UNITS=root/'units'),patch.object(m,'run',return_value=result):
     with self.assertRaises(ValueError):m.assert_fresh()
 def test_same_install_no_restart(self):
  with patch.object(m.os,'geteuid',return_value=0),patch.object(m,'same_install',return_value=True),patch.object(m,'run') as call:
   m.install(C);call.assert_not_called()
 def test_duplicate_address_restores_link(self):
  infos=[dict(iface('eth1'),flags=[])]
  def runner(cmd,**kw):return subprocess.CompletedProcess(cmd,1 if cmd[0]=='arping' else 0,'','duplicate' if cmd[0]=='arping' else '')
  with patch.object(m,'interfaces',return_value=infos),patch.object(m,'run',side_effect=runner) as call:
   with self.assertRaises(ValueError):m.check_duplicate_address(C)
  self.assertEqual(call.call_args_list[-1].args[0],['ip','link','set','dev','eth1','down'])
 def test_unlisted_program_file_rejected(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp);(root/'board').mkdir();(root/'board/extra.py').write_text('pass');(root/'MANIFEST.json').write_text('{}')
   with patch.object(m,'ROOT',root):
    with self.assertRaisesRegex(ValueError,'缺少'):m.verify_manifest()
if __name__=='__main__':unittest.main()
