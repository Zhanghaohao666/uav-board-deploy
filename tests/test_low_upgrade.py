import importlib.util,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('low_update',ROOT/'update_low_streams.py')
u=importlib.util.module_from_spec(spec);spec.loader.exec_module(u)

class LowUpgradeTests(unittest.TestCase):
 def run_case(self,fail=False):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);base=root/'installed';base.mkdir();state=root/'state.json'
   state.write_text('{"mode":"dual"}');(base/'config.json').write_text('{"default_mode":"algorithm","keep":"unchanged"}')
   for rel in u.FILES.values():
    p=base/rel;p.parent.mkdir(exist_ok=True,parents=True);p.write_bytes(b'previous executable')
   calls=[];start_count=0
   def call(argv,check=True):
    nonlocal start_count
    calls.append(argv)
    if argv[1]=='is-active':return SimpleNamespace(returncode=0 if argv[-1] in ['uav-switch@board.service','uav-switch@relay.service'] else 3)
    if argv[1]=='start':
     start_count+=1
     if fail and start_count==1:raise RuntimeError('injected service start failure')
    return SimpleNamespace(returncode=0)
   with patch.object(u,'BASE',base),patch.object(u,'STATE',state),patch.object(u,'BACKUPS',root/'backups'),patch.object(u,'installer_module',return_value=SimpleNamespace(verify_manifest=lambda:None,platform_check=lambda:None)),patch.object(u.os.path,'ismount',return_value=True),patch.object(u,'call',side_effect=call),patch.object(u.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='',stderr='')):
    if fail:
     with self.assertRaisesRegex(RuntimeError,'injected'):u.update()
    else:u.update()
   for source,rel in u.FILES.items():self.assertEqual((base/rel).read_bytes(),b'previous executable' if fail else (ROOT/source).read_bytes())
   self.assertEqual(state.read_text(),'{"mode":"dual"}')
   self.assertEqual(json.loads((base/'config.json').read_text())['keep'],'unchanged')
   for argv in calls:
    self.assertEqual(argv[0],'systemctl')
    if argv[1] in ['start','stop']:self.assertEqual(set(argv[2:]),{'uav-switch@board.service','uav-switch@relay.service'})
   for p in (root/'backups').rglob('rk_streamer'):self.assertEqual(p.read_bytes(),b'previous executable')
 def test_upgrade_preserves_mode_network_and_only_restarts_running_video(self):self.run_case()
 def test_failed_restart_restores_programs_and_original_running_units(self):self.run_case(True)

if __name__=='__main__':unittest.main()
