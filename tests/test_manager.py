import importlib.util,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
B=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('manager',B/'board/manager.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
C=json.loads((B/'config.example.json').read_text())
def iface(name,*ips):return {'ifname':name,'addr_info':[{'family':'inet','local':v.split('/')[0],'prefixlen':int(v.split('/')[1])} for v in ips]}
def dual():return [iface(C['payload_iface'],'192.168.10.1/24'),iface(C['extension_iface'],'192.168.144.1/24'),iface(C['mk22_iface'],C['mk22_address']),iface('wlan0','192.168.1.104/24')]
class ManagerTests(unittest.TestCase):
 def test_expected_stream_counts(self):
  self.assertEqual([len(m.endpoint_list(C,x)) for x in m.MODES],[5,4,7])
 def test_two_control_targets(self):
  cmd=m.services(C,'dual');self.assertEqual(cmd['algo-control'][-1],'TCP:192.168.10.2:9000');self.assertEqual(cmd['gimbal-control'][-1],'TCP:192.168.144.108:9000')
 def test_single_profiles_only_start_selected_payload(self):
  self.assertNotIn('gimbal-control',m.services(C,'algorithm'));self.assertNotIn('algo-control',m.services(C,'gimbal'))
 def test_common_capture_commands_identical(self):
  for key in ('board','d455'):
   self.assertEqual(m.services(C,'dual')[key],m.services(C,'algorithm')[key]);self.assertEqual(m.services(C,'dual')[key],m.services(C,'gimbal')[key])
 def test_dual_network_no_address_changes(self):
  self.assertTrue(all(x[1]=='link' for x in m.netplan(C,'dual',dual())))
 def test_dual_to_single_moves_only_gimbal_address(self):
  p=m.netplan(C,'gimbal',dual());addr=[x for x in p if x[1]=='addr'];self.assertEqual(addr,[['ip','addr','del','192.168.144.1/24','dev',C['extension_iface']],['ip','addr','add','192.168.144.1/24','dev','eth0']])
 def test_single_without_extension_works(self):
  infos=[iface('eth0','192.168.10.1/24','192.168.144.1/24'),iface(C['mk22_iface'],C['mk22_address'])]
  self.assertTrue(all(x[1]=='link' for x in m.netplan(C,'algorithm',infos)))
 def test_dual_without_extension_refused_before_mutation(self):
  with self.assertRaises(ValueError):m.netplan(C,'dual',[i for i in dual() if i['ifname']!=C['extension_iface']])
 def test_switch_back_to_dual(self):
  infos=[iface('eth0','192.168.10.1/24','192.168.144.1/24'),iface(C['extension_iface']),iface(C['mk22_iface'],C['mk22_address'])]
  addr=[x for x in m.netplan(C,'dual',infos) if x[1]=='addr'];self.assertEqual(addr[0][2],'del');self.assertEqual(addr[1],['ip','addr','add','192.168.144.1/24','dev',C['extension_iface']])
 def test_unknown_subnet_owner_refused(self):
  with self.assertRaises(ValueError):m.netplan(C,'dual',dual()+[iface('other','192.168.144.5/24')])
 def test_no_wifi_or_default_route_mutations(self):
  for mode in m.MODES:
   self.assertFalse(any('wlan0' in p or 'flush' in p or 'route' in p for p in m.netplan(C,mode,dual())))
 def test_rtp_rtcp_ports_distinct(self):
  cmd=m.services(C,'dual');ports=[]
  for k in ('visible-ingest','infrared-ingest'):
   p=int(cmd[k][-1].rsplit(':',1)[1]);ports += [p,p+1]
  self.assertEqual(len(set(ports)),4);self.assertTrue(set(ports).isdisjoint({5602,5603,5604,5702,5703}))
 def test_reading_mode_does_not_change_default(self):
  with tempfile.TemporaryDirectory() as d,patch.object(m,'STATE',Path(d)/'active.json'):
   m.atomic(m.STATE,{'mode':'gimbal'});self.assertEqual(m.current(C),'gimbal');self.assertEqual(C['default_mode'],'dual')
 def test_atomic_state(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'s.json';m.atomic(p,{'mode':'dual'});m.atomic(p,{'mode':'algorithm'});self.assertEqual(json.loads(p.read_text()),{'mode':'algorithm'});self.assertFalse(p.with_suffix('.tmp').exists())
 def test_failed_network_step_rolls_back_addresses_and_mode(self):
  from types import SimpleNamespace
  calls=[]
  def fake(cmd,**kw):
   calls.append(cmd)
   if cmd==['ip','-j','address','show']:return SimpleNamespace(stdout=json.dumps(dual()))
   if cmd[:3]==['ip','addr','add'] and cmd[-1]=='eth0':raise OSError('injected add failure')
   return SimpleNamespace(stdout='',returncode=0)
  with tempfile.TemporaryDirectory() as d,patch.object(m,'STATE',Path(d)/'active.json'),patch.object(m,'run',side_effect=fake),patch.object(m,'ownership_check'),patch.object(m.os,'geteuid',return_value=0):
   with self.assertRaises(OSError):m.change(C,'gimbal')
   self.assertEqual(m.current(C),'dual');self.assertIn(['ip','addr','add','192.168.144.1/24','dev',C['extension_iface']],calls)
   # Neither shared camera was stopped during a failed payload switch.
   for cmd in calls:
    if cmd[:2]==['systemctl','stop']:self.assertNotIn('uav-switch@board.service',cmd);self.assertNotIn('uav-switch@d455.service',cmd)
if __name__=='__main__':unittest.main(verbosity=2)
