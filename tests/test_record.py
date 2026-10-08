import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('video_record',ROOT/'board/record.py');r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
class RecordingTests(unittest.TestCase):
 def test_selection_multiple_numbers_names_deduplicated_and_modes(self):
  self.assertEqual(r.parse_selection('1,3，preview 3',list(r.STREAMS)),['board','algorithm','preview'])
  self.assertEqual(r.parse_selection('all',['board','rgb']),['board','rgb'])
  for value in ['0','8','../../etc','', 'algorithm']:
   with self.assertRaises(ValueError):r.parse_selection(value,['board'])
 def test_unmounted_disk_refuses_before_directory_or_service_changes(self):
  with patch.object(r.os.path,'ismount',return_value=False),patch.object(r,'available',return_value=['board']),patch.object(r,'ctl') as ctl:
   with self.assertRaisesRegex(ValueError,'未挂载'):r.start(['board'])
   ctl.assert_not_called()
 def test_full_disk_refuses_without_deleting_files(self):
  with patch.object(r.os.path,'ismount',return_value=True),patch.object(r.shutil,'disk_usage',return_value=SimpleNamespace(free=1)):
   with self.assertRaisesRegex(ValueError,'未删除'):r.storage()
 def test_record_copy_and_local_urls_no_encoder(self):
  for key in r.STREAMS:
   cmd=r.command(key,Path('/data/test'),300,'-stimeout')
   self.assertEqual(cmd[cmd.index('-c:v')+1],'copy')
   self.assertTrue(cmd[cmd.index('-i')+1].startswith('rtsp://127.0.0.1:'))
   self.assertNotIn('libx264',cmd)
   self.assertEqual(cmd[cmd.index('-segment_time')+1],'300')
 def fixture(self,folder):
  mount=Path(folder)/'data';mount.mkdir();dest=mount/'uav-recordings'
  stack=ExitStack()
  for name,value in dict(MOUNT=mount,DEST=dest,STATE=Path(folder)/'run/current.json',LOCK=Path(folder)/'lock').items():stack.enter_context(patch.object(r,name,value))
  stack.enter_context(patch.object(r.os.path,'ismount',return_value=True));stack.enter_context(patch.object(r.shutil,'disk_usage',return_value=SimpleNamespace(free=100*1024**3)))
  stack.enter_context(patch.object(r.shutil,'which',return_value='/usr/bin/ffmpeg'));stack.enter_context(patch.object(r,'available',return_value=list(r.STREAMS)));stack.enter_context(patch.object(r,'active',return_value=False))
  return stack
 def test_partial_start_failure_stops_only_new_workers_and_keeps_files(self):
  with tempfile.TemporaryDirectory() as folder,self.fixture(folder):
   calls=[]
   def ctl(*args,check=True):
    calls.append(args)
    if args==('start',r.unit('infrared')):raise RuntimeError('injected')
    return subprocess.CompletedProcess(args,0,'','')
   with patch.object(r,'ctl',side_effect=ctl):
    with self.assertRaisesRegex(RuntimeError,'injected'):r.start(['board','infrared'])
   self.assertFalse(r.STATE.exists())
   self.assertIn(('stop',r.unit('board')),calls)
   self.assertIn(('stop',r.unit('infrared')),calls)
   self.assertNotIn(('stop',r.unit('gimbal')),calls)
   self.assertEqual(len(list(r.DEST.rglob('session.json'))),1)
 def test_adding_stream_keeps_running_worker_and_stop_preserves_files(self):
  with tempfile.TemporaryDirectory() as folder,self.fixture(folder):
   with patch.object(r,'ctl',return_value=subprocess.CompletedProcess([],0,'','')) as ctl:
    first=r.start(['board']);ctl.reset_mock()
    with patch.object(r,'active',side_effect=lambda k:k=='board'):
     second=r.start(['board','preview'])
    self.assertEqual(first['session'],second['session'])
    self.assertNotIn(('start',r.unit('board')),[x.args for x in ctl.call_args_list])
    r.stop(['preview']);self.assertEqual(r.current()['requested'],['board'])
    r.stop(['board']);self.assertFalse(r.STATE.exists())
    self.assertTrue((Path(first['path'])/'session.json').exists())
 def test_stop_still_works_when_mount_disappears(self):
  with tempfile.TemporaryDirectory() as folder,self.fixture(folder),patch.object(r,'ctl',return_value=subprocess.CompletedProcess([],0,'','')) as ctl:
   value=r.start(['board'])
   with patch.object(r.os.path,'ismount',return_value=False):r.stop(['board'])
   self.assertIn(('stop',r.unit('board')),[x.args for x in ctl.call_args_list])
   self.assertFalse(r.STATE.exists())
 def test_workers_are_manual_and_not_stopped_by_video_switch(self):
  text=(ROOT/'board/uav-record@.service').read_text()
  self.assertNotIn('[Install]',text);self.assertNotIn('PartOf=',text)
  self.assertIn('KillMode=mixed',text)
  self.assertIn('ConditionPathIsMountPoint=/data',text)
 def test_worker_needs_manual_selection(self):
  with patch.object(r,'current',return_value=None):
   with self.assertRaisesRegex(ValueError,'未手动'):r.worker('board')
