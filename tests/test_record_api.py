import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'board'))
import record_http as api

class ApiTests(unittest.TestCase):
 def test_random_connection_codes_private_stable_and_not_shared(self):
  with tempfile.TemporaryDirectory() as folder:
   p=Path(folder)/'token';first=api.prepare_token(p)
   self.assertEqual(len(first),64);self.assertEqual(p.stat().st_mode & 0o777,0o600)
   self.assertEqual(api.prepare_token(p),first)
   self.assertNotEqual(api.prepare_token(Path(folder)/'other'),first)
   p.chmod(0o644)
   with self.assertRaisesRegex(ValueError,'权限'):api.prepare_token(p)
 def test_symlink_credentials_refused(self):
  with tempfile.TemporaryDirectory() as folder:
   p=Path(folder)/'token';p.symlink_to(Path(folder)/'missing')
   with self.assertRaisesRegex(ValueError,'符号'):api.prepare_token(p)
 def test_only_allowlisted_stream_names_no_shell_or_urls(self):
  self.assertEqual(api.selection({'streams':['board','board','preview']},'start'),['board','preview'])
  self.assertEqual(api.selection({'streams':['all']},'stop'),list(api.record.STREAMS))
  for body in [{'streams':['all']},{'streams':[]},{'streams':['board; reboot']},{'streams':['rtsp://anything']},{'streams':['board'],'command':'reboot'},{'streams':'board'},{'streams':[{}]}]:
   with self.assertRaises(ValueError):api.selection(body,'start')
 def test_bad_auth_never_calls_record_functions(self):
  handler=object.__new__(api.Handler);handler.server=SimpleNamespace(token='a'*64);handler.headers={'Authorization':'Bearer incorrect'}
  with patch.object(handler,'respond') as out,patch.object(api.record,'start') as start:
   handler.do_POST();self.assertEqual(out.call_args.args[0],401);start.assert_not_called()
 def test_snapshot_distinguishes_request_from_real_frames_and_disk_loss(self):
  from contextlib import nullcontext
  with tempfile.TemporaryDirectory() as folder:
   path=Path(folder)/'session';(path/'board').mkdir(parents=True)
   value={'session':'test','path':str(path),'requested':['board','infrared'],'started_at':'today','segment_seconds':300}
   (path/'board/status.json').write_text(json.dumps({'state':'正在录像','detail':'写入 20 帧','at':api.record.now()}))
   show='\n\n'.join('Id='+api.record.unit(k)+'\nActiveState=active' for k in ['board','infrared'])
   with patch.object(api.record,'locked',side_effect=nullcontext),patch.object(api.record,'current',return_value=value),patch.object(api.record,'available',return_value=['board','infrared']),patch.object(api.os.path,'ismount',return_value=True),patch.object(api.record.shutil,'disk_usage',return_value=SimpleNamespace(free=100*1024**3)),patch.object(api.record,'storage',return_value=100),patch.object(api.record,'ctl',return_value=subprocess.CompletedProcess([],0,show,'')):
    snap=api.snapshot();by_key={s['key']:s for s in snap['streams']}
    self.assertEqual(by_key['board']['phase'],'recording');self.assertEqual(by_key['infrared']['phase'],'waiting')
    self.assertEqual(by_key['preview']['phase'],'stopped')
    with patch.object(api.os.path,'ismount',return_value=False),patch.object(api.record,'storage',side_effect=ValueError('未挂载')):
     snap=api.snapshot();self.assertFalse(snap['storage']['mounted']);self.assertEqual(snap['streams'][0]['phase'],'waiting')
 def test_old_status_cannot_be_reported_as_current_recording(self):
  from contextlib import nullcontext
  with tempfile.TemporaryDirectory() as folder:
   path=Path(folder);(path/'board').mkdir()
   value={'session':'test','path':str(path),'requested':['board'],'started_at':'today','segment_seconds':300}
   (path/'board/status.json').write_text(json.dumps({'state':'正在录像','at':'2020-01-01T00:00:00+08:00'}))
   with patch.object(api.record,'locked',side_effect=nullcontext),patch.object(api.record,'current',return_value=value),patch.object(api.record,'available',return_value=['board']),patch.object(api.os.path,'ismount',return_value=True),patch.object(api.record.shutil,'disk_usage',return_value=SimpleNamespace(free=100*1024**3)),patch.object(api.record,'storage',return_value=100),patch.object(api.record,'ctl',return_value=subprocess.CompletedProcess([],0,'Id=uav-record@board.service\nActiveState=active','')):
    self.assertEqual(api.snapshot()['streams'][0]['phase'],'waiting')
