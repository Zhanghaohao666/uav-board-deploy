import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'board'))
import lidar_record as lidar
import record_http as api


def packet(kind=16, count=1, size=100):
    data = bytearray(size)
    struct.pack_into('<H', data, 0, 0x176a)
    struct.pack_into('<I', data, 10, size)
    data[38] = kind
    data[39:42] = count.to_bytes(3, 'little')
    return bytes(data)


class LidarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / 'data'; self.data.mkdir()
        self.c = dict(lidar.DEFAULT, root=str(self.data / 'uav-lidar'))
        self.patches = contextlib.ExitStack(); self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.multiple(lidar, MOUNT=self.data, RUN=self.root / 'run',
                                                 CONFIG=self.root / 'config.json'))
        self.patches.enter_context(patch.object(lidar, 'config', return_value=self.c))
        self.patches.enter_context(patch.object(lidar.os.path, 'ismount', return_value=True))
        self.patches.enter_context(patch.object(lidar.shutil, 'disk_usage', return_value=SimpleNamespace(free=20 * 1024 ** 3)))

    def value(self, seconds=10):
        Path(self.c['root']).mkdir(exist_ok=True)
        return dict(session='example', config=self.c, seconds=seconds, boot_id=lidar.boot_id(),
                    phase='starting', detail='', path='', bytes=0, started_at=lidar.now(),
                    error='', monotonic=time.monotonic())

    def fake_sdk(self):
        sdk = self.root / 'fake-sdk'
        sdk.write_text('''#!/usr/bin/env python3
import os,sys,struct,time,signal
def arg(key):return sys.argv[sys.argv.index(key)+1]
mode=os.environ.get('FAKE_LIDAR_MODE','complete')
path=arg('--output-filename')
def packet(kind,count):
 d=bytearray(100);struct.pack_into('<H',d,0,0x176a);struct.pack_into('<I',d,10,len(d));d[38]=kind;d[39:42]=count.to_bytes(3,'little');return d
out=open(path,'wb',buffering=0);out.write(packet(101,0))
if mode!='empty':out.write(packet(16,1))
def stop(signum,frame):
 out.write(packet(16,1));out.close()
 print('x'*200000,flush=True)
 print('UAV_RECORD_STOPPED signal=%d frame_boundary=1 writer_drained=1'%signum,flush=True)
 sys.exit(0)
signal.signal(signal.SIGTERM,stop)
if mode=='hang':
 while True:
  print('frame idx:1, point_count:5',flush=True);time.sleep(.05)
time.sleep(.15)
print('frame_counter = '+str(0 if mode=='early' else int(arg('--frame-number'))*int(arg('--file-number'))),flush=True)
out.close()
''')
        sdk.chmod(0o755); self.c['sdk'] = str(sdk)

    def run_capture(self, value, stop=None):
        with patch.object(lidar, 'prerequisites'), patch.object(lidar, 'legacy_busy', return_value=False), patch.object(lidar, 'frame_rate', return_value=10):
            lidar.capture(value, [] if stop is None else stop)

    def test_packets_require_actual_points_and_complete_packet_not_angle_table(self):
        path = self.root / 'raw.inno_pc'
        path.write_bytes(packet(101, 1) + packet(16, 0) + packet(16, 2)[:60])
        observer = lidar.RawObserver(); observer.inspect([path])
        self.assertEqual(observer.point_packets, 0)
        with path.open('ab') as f: f.write(packet(16, 2)[60:])
        observer.inspect([path]); self.assertEqual(observer.point_packets, 1)
        observer.inspect([path]); self.assertEqual(observer.point_packets, 1)

    def test_missing_radar_does_not_start_process_or_write_session(self):
        with patch.object(lidar, 'service_active', return_value=False), patch.object(lidar, 'prerequisites', side_effect=ValueError('没有物理链路')), patch.object(lidar, 'ctl') as ctl:
            with self.assertRaisesRegex(ValueError, '物理链路'): lidar.start()
            ctl.assert_not_called()
            self.assertFalse((lidar.RUN / 'current.json').exists())

    def test_duplicate_start_is_idempotent_without_rechecking_device_or_overwriting(self):
        with patch.object(lidar, 'service_active', return_value=True), patch.object(lidar, 'prerequisites') as pre, patch.object(lidar, 'ctl') as ctl:
            lidar.start(); pre.assert_not_called(); ctl.assert_not_called()

    def test_legacy_lock_excludes_new_recording(self):
        import fcntl
        self.value()
        with (Path(self.c['root']) / '.uav-lidar-record.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(lidar, 'service_active', return_value=False), patch.object(lidar, 'prerequisites'), patch.object(lidar, 'legacy_busy', return_value=False), patch.object(lidar, 'ctl') as ctl:
                with self.assertRaisesRegex(ValueError, '已有点云'): lidar.start()
                ctl.assert_not_called()

    def test_long_sdk_executable_is_detected_without_linux_comm_truncation(self):
        with patch.object(lidar.Path,'glob',return_value=[Path('/proc/123/exe')]),patch.object(lidar.os,'readlink',return_value='/usr/local/bin/seyond-get-pcd-record'):
            self.assertTrue(lidar.legacy_busy())
        with patch.object(lidar.Path,'glob',return_value=[Path('/proc/123/exe')]),patch.object(lidar.os,'readlink',return_value='/usr/bin/python3'):
            self.assertFalse(lidar.legacy_busy())

    def test_reboot_does_not_resume_or_report_old_recording(self):
        value = self.value(); value.update(phase='recording', boot_id='previous-boot')
        lidar.atomic(Path(self.c['root']) / 'latest.json', value)
        with patch.object(lidar, 'service_active', return_value=False), patch.object(lidar, 'prerequisites'):
            snap = lidar.snapshot()
            self.assertFalse(snap['active']); self.assertEqual(snap['phase'], 'error')
            self.assertIn('重启', snap['error'])
        with self.assertRaisesRegex(ValueError, '有效手动'): lidar.worker()

    def test_stale_heartbeat_and_service_with_old_state_still_allow_stop(self):
        value = self.value(); value.update(phase='starting', monotonic=time.monotonic()-30)
        lidar.persist(value)
        with patch.object(lidar, 'service_active', return_value=True), patch.object(lidar, 'prerequisites'):
            snap=lidar.snapshot(); self.assertTrue(snap['active']); self.assertEqual(snap['phase'],'error')
        value['boot_id']='old';lidar.persist(value)
        with patch.object(lidar, 'service_active', return_value=True), patch.object(lidar, 'prerequisites'):
            self.assertTrue(lidar.snapshot()['active'])

    def test_stop_is_idempotent_and_sets_marker_for_worker(self):
        with patch.object(lidar, 'service_active', return_value=False), patch.object(lidar, 'ctl') as ctl:
            lidar.stop(); ctl.assert_not_called()
        value=self.value();lidar.persist(value)
        with patch.object(lidar, 'service_active', return_value=True), patch.object(lidar, 'ctl',return_value=SimpleNamespace(returncode=0)):
            lidar.stop()
            self.assertEqual(lidar.read_json(lidar.RUN / 'stop.json')['session'],value['session'])

    def test_disk_mount_and_ten_gib_reserve_are_enforced(self):
        with patch.object(lidar.os.path,'ismount',return_value=False):
            with self.assertRaisesRegex(ValueError,'未挂载'):lidar.storage(self.c)
        with patch.object(lidar.shutil,'disk_usage',return_value=SimpleNamespace(free=9*1024**3)):
            with self.assertRaisesRegex(ValueError,'10 GiB'):lidar.storage(self.c)

    def test_healthy_finite_capture_with_one_flush_is_success(self):
        self.fake_sdk();value=self.value()
        self.run_capture(value)
        self.assertEqual(value['phase'],'stopped');self.assertTrue(value['complete'])
        self.assertGreater(value['bytes'],0);self.assertEqual(value['saved_point_packets'],1)

    def test_sdk_exit_zero_with_timeout_or_angle_only_is_error(self):
        self.fake_sdk()
        for mode in ('early','empty'):
            with patch.dict(os.environ,FAKE_LIDAR_MODE=mode):
                value=self.value()
                with self.assertRaisesRegex(ValueError,'提前退出'):self.run_capture(value)
                self.assertFalse(value['complete'])

    def test_user_stop_drains_full_pipe_and_marks_short_final_segment(self):
        self.fake_sdk();value=self.value();stop=[]
        timer=threading.Timer(.35,lambda:stop.append(signal.SIGTERM));timer.start()
        try:
            with patch.dict(os.environ,FAKE_LIDAR_MODE='hang'):self.run_capture(value,stop)
        finally:timer.cancel()
        self.assertEqual(value['phase'],'stopped');self.assertTrue(value['graceful_frame_boundary'])
        self.assertFalse(value['complete']);self.assertFalse(value['segments'][-1]['complete'])
        self.assertTrue(value['segments'][-1]['closed_at_frame_boundary'])

    def test_duration_not_multiple_of_segment_stops_at_time_not_rounded_full_segments(self):
        self.fake_sdk();value=self.value(seconds=1)
        with patch.dict(os.environ,FAKE_LIDAR_MODE='hang'):self.run_capture(value)
        self.assertEqual(value['phase'],'stopped');self.assertEqual(value['stop_reason'],'duration_limit')
        self.assertLess(value['elapsed_seconds'],4)

    def test_rotating_log_is_bounded(self):
        log=lidar.LimitedLog(self.root)
        for _ in range(40):log.write(b'x'*200000)
        self.assertLessEqual(sum(p.stat().st_size for p in self.root.glob('capture.log*')),2*log.LIMIT)

    def test_http_lidar_body_must_be_empty_and_authenticated(self):
        handler=object.__new__(api.Handler)
        handler.server=SimpleNamespace(token='a'*64);handler.path='/v1/lidar/start'
        for body in ({'lidar_ip':'1.2.3.4'},[],None):
            raw=json.dumps(body).encode();handler.rfile=io.BytesIO(raw)
            handler.headers={'Authorization':'Bearer '+'a'*64,'Content-Length':str(len(raw))}
            with patch.object(handler,'respond') as respond,patch.object(handler,'perform') as perform:
                handler.do_POST();self.assertEqual(respond.call_args.args[0],400);perform.assert_not_called()
        handler.rfile=io.BytesIO(b'{}');handler.headers={'Authorization':'Bearer '+'a'*64,'Content-Length':'2'}
        with patch.object(handler,'perform') as perform:
            handler.do_POST();self.assertIs(perform.call_args.args[0],lidar.start)
        handler.headers={}
        with patch.object(handler,'respond') as respond,patch.object(handler,'perform') as perform:
            handler.do_POST();self.assertEqual(respond.call_args.args[0],401);perform.assert_not_called()


class InstallerTests(unittest.TestCase):
    def test_activating_capture_blocks_upgrade_before_mutating(self):
        spec=importlib.util.spec_from_file_location('lidar_installer_active',ROOT/'install_lidar_recorder.py')
        m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);(p/'record.py').write_text('old');(p/'record_http.py').write_text('old-api')
            with patch.multiple(m,BASE=p,LOCK=p/'lock'),patch.object(m.os,'geteuid',return_value=0),patch.object(m,'run',return_value=SimpleNamespace(returncode=0,stdout='activating\n')):
                with self.assertRaisesRegex(ValueError,'正在运行'):m.install(True)
            self.assertEqual((p/'record_http.py').read_text(),'old-api')

    def test_failure_before_first_replace_preserves_unvisited_existing_files(self):
        spec=importlib.util.spec_from_file_location('lidar_installer_early',ROOT/'install_lidar_recorder.py')
        m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);base=p/'opt';base.mkdir();units=p/'units';units.mkdir();bin=p/'bin';bin.mkdir()
            (base/'record.py').write_text('old-video');(base/'record_http.py').write_text('old-api')
            (bin/'uav-lidar-record').write_text('old-cli');(units/'uav-lidar-record.service').write_text('old-service')
            with patch.multiple(m,BASE=base,BIN=bin,UNITS=units,BACKUPS=p/'backups',LOCK=p/'lock'),patch.object(m.os,'geteuid',return_value=0),patch.object(m,'legacy_busy',return_value=False),patch.object(m,'run',return_value=SimpleNamespace(returncode=1)),patch.object(m.os,'replace',side_effect=OSError('disk error')):
                with self.assertRaisesRegex(OSError,'disk error'):m.install(upgrade=True)
            self.assertEqual((base/'record_http.py').read_text(),'old-api')
            self.assertEqual((bin/'uav-lidar-record').read_text(),'old-cli')
            self.assertEqual((units/'uav-lidar-record.service').read_text(),'old-service')
            self.assertFalse((base/'lidar_record.py').exists())

    def test_upgrade_failure_restores_files_without_touching_sdk_or_network(self):
        spec=importlib.util.spec_from_file_location('lidar_installer',ROOT/'install_lidar_recorder.py')
        m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);base=p/'opt';base.mkdir();units=p/'units';units.mkdir();bin=p/'bin';bin.mkdir()
            (base/'record.py').write_text('old-video');(base/'record_http.py').write_text('old-api')
            (bin/'uav-lidar-record').write_text('old-cli')
            commands=[]
            def run(args,check=True):
                commands.append(args)
                if args[0]=='systemd-analyze':raise subprocess.CalledProcessError(1,args)
                return SimpleNamespace(returncode=1 if args[0]=='pgrep' or args[:2]==['systemctl','is-active'] else 0)
            with patch.multiple(m,BASE=base,BIN=bin,UNITS=units,BACKUPS=p/'backups',LOCK=p/'lock'),patch.object(m.os,'geteuid',return_value=0),patch.object(m,'legacy_busy',return_value=False),patch.object(m,'run',side_effect=run):
                with self.assertRaises(subprocess.CalledProcessError):m.install(upgrade=True)
            self.assertEqual((base/'record_http.py').read_text(),'old-api')
            self.assertEqual((bin/'uav-lidar-record').read_text(),'old-cli')
            self.assertFalse((base/'lidar_record.py').exists())
            self.assertEqual((base/'record.py').read_text(),'old-video')
            self.assertFalse(any(args[0] in ('ip','apt','curl') for args in commands))


if __name__=='__main__':unittest.main()
