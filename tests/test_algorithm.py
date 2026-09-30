import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'algorithm'))
import runtime as r
spec = importlib.util.spec_from_file_location('algorithm_install', ROOT / 'algorithm/install.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
C = dict(iface='eth0', address='192.168.10.2/24', host='192.168.10.1', user='dev')
for i, slot in enumerate(r.SLOTS):
    C[slot] = dict(enabled=True, device='/dev/video%d' % i, rotate=0, bitrate=1500 if slot == 'infrared' else 3000)


def iface(name, address):
    ip, prefix = address.split('/')
    return dict(ifname=name, addr_info=[dict(family='inet', local=ip, prefixlen=int(prefix))])


def success(cmd, **kwargs):
    return subprocess.CompletedProcess(cmd, 0, '', '')


class AlgorithmTests(unittest.TestCase):
    def test_raw_commands_keep_relay_ports_and_no_rgb_or_tracker(self):
        for slot in ('front1', 'front2'):
            cmd = r.command(C, slot)
            self.assertIn(str(r.PORTS[slot]), cmd)
            self.assertEqual(cmd[cmd.index('--format') + 1], 'yuv')
            self.assertNotIn('rgb', ' '.join(cmd))
            self.assertNotIn('tracker', ' '.join(cmd))
        cmd = r.command(C, 'infrared')
        self.assertEqual(cmd[cmd.index('--port') + 1], '5604')

    def test_reject_duplicate_device_aliases(self):
        c = copy.deepcopy(C)
        with patch.object(Path, 'resolve', return_value=Path('/dev/video22')):
            with self.assertRaisesRegex(ValueError, '同一个'):r.validate(c)

    def test_rotation_and_address_validation(self):
        for field, value in [('address', '192.168.10.0/24'), ('address', '192.168.10.2/16'),
                             ('host', '192.168.10.2'), ('host', '192.168.10.255'), ('host', '192.168.144.1')]:
            c = copy.deepcopy(C); c[field] = value
            with self.assertRaises(ValueError):r.validate(c)
        c = copy.deepcopy(C); c['infrared']['rotate'] = 90
        with self.assertRaises(ValueError):r.validate(c)
        c['infrared']['rotate'] = 180; c['front1']['rotate'] = 90
        self.assertEqual(r.validate(c), c)

    def test_preserve_existing_addresses_and_routes(self):
        infos = [iface('eth0', '192.168.10.2/24'), iface('wlan0', '192.168.1.90/24')]
        def runner(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0, '[{"dev":"eth0"}]' if 'route' in cmd else '', '')
        with patch.object(r, 'addresses', return_value=infos), patch.object(Path, 'exists', return_value=True), patch.object(r, 'run', side_effect=runner) as call:
            r.network(C)
        commands = [x.args[0] for x in call.call_args_list]
        self.assertFalse(any('flush' in x or 'del' in x or 'add' in x or 'arping' in x for x in commands))
        self.assertFalse(any('wlan0' in x for x in commands))

    def test_network_duplicate_does_not_add_address(self):
        def runner(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 1 if cmd[0] == 'arping' else 0, '', '')
        with patch.object(r, 'addresses', return_value=[]), patch.object(Path, 'exists', return_value=True), patch.object(Path, 'read_text', return_value='1'), patch.object(r, 'run', side_effect=runner) as call:
            with self.assertRaisesRegex(ValueError, '冲突'):r.network(C)
        self.assertFalse(any('add' in x.args[0] for x in call.call_args_list))

    def test_network_other_interface_overlap_rejected(self):
        with patch.object(r, 'addresses', return_value=[iface('eth1', '192.168.10.3/24')]), patch.object(Path, 'exists', return_value=True), patch.object(r, 'run') as call:
            with self.assertRaisesRegex(ValueError, '另一网口'):r.network(C)
            call.assert_not_called()

    def test_missing_ir_is_optional_but_front_required(self):
        def check(slot, item):return ('missing', 'absent') if slot == 'infrared' else ('ready', item['device'])
        with patch.object(r, 'inspect_camera', side_effect=check):m.cameras(C, require_front=True)
        with patch.object(r, 'inspect_camera', return_value=('missing', 'absent')):
            with self.assertRaisesRegex(ValueError, '缺少前视'):m.cameras(C, require_front=True)

    def test_usb_uses_advertised_format_without_changing_current_mode(self):
        from types import SimpleNamespace
        info = "[0]: 'UYVY' (UYVY 4:2:2)\n Size: Discrete 1280x520\n[1]: 'MJPG'\n Size: Discrete 640x480\n"
        result = subprocess.CompletedProcess([], 0, info, '')
        with patch.object(Path, 'exists', return_value=True), patch.object(Path, 'stat', return_value=SimpleNamespace(st_mode=stat.S_IFCHR)), patch.object(Path, 'resolve', return_value=Path('/dev/video3')), patch.object(r, 'run', return_value=result) as call:
            self.assertEqual(r.inspect_camera('infrared', C['infrared'])[0], 'ready')
            self.assertIn('--list-formats-ext', call.call_args.args[0])
            result.stdout = "[0]: 'UYVY'\n Size: Discrete 640x480\n[1]: 'MJPG'\n Size: Discrete 1280x520\n"
            with self.assertRaises(ValueError):r.inspect_camera('infrared', C['infrared'])

    def test_active_manual_camera_refuses_install(self):
        with patch.object(Path, 'exists', return_value=False), patch.object(r, 'run', side_effect=success), patch.object(m, 'camera_owners', return_value=['123:rk_streamer']):
            with self.assertRaisesRegex(ValueError, '正在被使用'):m.assert_fresh(C)

    def test_enabled_legacy_refuses_even_when_inactive(self):
        def runner(cmd, **kw):return subprocess.CompletedProcess(cmd, 0, 'enabled' if 'is-enabled' in cmd else 'inactive', '')
        with patch.object(Path, 'exists', return_value=False), patch.object(r, 'run', side_effect=runner):
            with self.assertRaisesRegex(ValueError, '已有采集'):m.assert_fresh(C)

    def test_watchdog_terminates_process_without_frames(self):
        with self.assertRaisesRegex(ValueError, '没有有效'):
            r.supervise([sys.executable, '-c', 'import time; time.sleep(20)'], os.environ.copy(), stall_seconds=0.1)

    def test_worker_exit_reported_as_failure(self):
        with self.assertRaisesRegex(ValueError, '已退出'):
            r.supervise([sys.executable, '-c', 'print("no frames")'], os.environ.copy(), stall_seconds=2)

    def fixture_install(self, root, fail=False):
        units = root / 'units'; units.mkdir()
        base = root / 'opt'
        launcher = root / 'launcher'
        dropin = units / 'factory.d/90-uav-algorithm.conf'
        patches = patch.multiple(m, BASE=base, UNITS=units, LAUNCHER=launcher, DROPIN=dropin, BACKUPS=root / 'backups')
        def runner(cmd, **kw):
            if fail and cmd[:2] == ['systemctl', 'start'] and m.NETWORK in cmd:
                raise subprocess.CalledProcessError(1, cmd)
            return success(cmd, **kw)
        return patches, runner, base, launcher, dropin

    def test_install_rollback_preserves_preexisting_ip_and_factory_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            patches, runner, base, launcher, dropin = self.fixture_install(root, fail=True)
            keep = root / 'units/factory.service'; keep.write_text('original')
            with patches, patch.object(m, 'assert_fresh'), patch.object(m, 'deps'), patch.object(m, 'check_network'), patch.object(m, 'cameras'), patch.object(r, 'addresses', return_value=[iface('eth0', '192.168.10.2/24')]), patch.object(r, 'network'), patch.object(r, 'run', side_effect=runner) as call:
                with self.assertRaises(subprocess.CalledProcessError):m.install(C, 'digest', False)
            self.assertFalse(base.exists()); self.assertFalse(launcher.exists()); self.assertFalse(dropin.exists())
            self.assertEqual(keep.read_text(), 'original')
            self.assertFalse(any(x.args[0][:3] == ['ip', 'address', 'del'] for x in call.call_args_list))

    def test_no_start_and_repeat_do_not_touch_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            patches, runner, base, launcher, dropin = self.fixture_install(Path(tmp))
            with patches, patch.object(m, 'assert_fresh'), patch.object(m, 'deps'), patch.object(m, 'check_network'), patch.object(m, 'cameras'), patch.object(r, 'addresses', return_value=[]), patch.object(r, 'network') as network, patch.object(r, 'run', side_effect=runner) as call:
                m.install(C, 'digest', True)
                network.assert_not_called()
                self.assertFalse(any(x.args[0][:2] == ['systemctl', 'start'] for x in call.call_args_list))
                self.assertTrue((base / 'installation.json').exists())
                call.reset_mock()
                m.install(C, 'digest', True)
                call.assert_not_called()
                (base / 'runtime.py').write_text('damaged')
                with self.assertRaisesRegex(ValueError, '已有不同'):m.install(C, 'digest', True)


if __name__ == '__main__':unittest.main()
