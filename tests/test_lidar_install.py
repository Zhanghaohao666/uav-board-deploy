import contextlib
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('lidar_install', ROOT / 'tools/seyond/install_runtime.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
network = installer.network


def eth(name='eth1', addresses=(), up=True):
    return dict(ifname=name, link_type='ether', flags=['UP'] if up else [],
                addr_info=[dict(family='inet', local=a.split('/')[0], prefixlen=int(a.split('/')[1])) for a in addresses])


class LidarInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        paths = {name: self.root / name.lower() for name in (
            'RUNTIME', 'LIDAR_CONFIG', 'NETWORK_CONFIG', 'UNIT_PATH', 'NM_PROFILE',
            'NM_DISPATCHER', 'UDEV_RULE', 'BACKUPS', 'NETWORK_SCRIPT', 'CONTROL_LOCK')}
        self.stack.enter_context(patch.multiple(installer, **paths))
        self.stack.enter_context(patch.object(network, 'MAIN_CONFIG', self.root / 'main.json'))
        self.infos = [eth(), eth('eth0', ['192.168.10.1/24']), eth('wlan0', ['192.168.1.104/24'])]
        self.routes = []
        self.commands = []
        self.nm_active = False
        self.profiles = {}
        self.active_state = 'inactive'
        self.current_nm_uuid = '--'
        self.fail_command = None
        self.stack.enter_context(patch.object(installer, 'run', side_effect=self.fake_run))
        self.stack.enter_context(patch.object(network, 'run', side_effect=self.fake_run))
        self.stack.enter_context(patch.object(installer.os, 'geteuid', return_value=0))

    def fake_run(self, args, check=True, **kwargs):
        self.commands.append(args)
        code, output = 0, ''
        if args[:4] == ['ip', '-j', 'address', 'show']:
            output = json.dumps(self.infos)
        elif args[:5] == ['ip', '-j', '-4', 'address', 'show']:
            output = json.dumps([i for i in self.infos if i.get('addr_info')])
        elif args[:5] == ['ip', '-j', '-4', 'route', 'show']:
            output = json.dumps(self.routes)
        elif args[:2] == ['systemctl', 'is-active']:
            code = 0 if args[-1] == 'NetworkManager.service' and self.nm_active else 3
        elif args[:2] == ['systemctl', 'is-enabled']:
            code = 1
        elif args[:2] == ['systemctl', 'show']:
            output = self.active_state + '\n'
        elif args[:4] == ['nmcli', '-t', '--escape', 'no']:
            output = '\n'.join(p + ':802-3-ethernet' for p in self.profiles)
        elif args[:2] == ['nmcli', '-g'] and args[2].startswith('connection.interface-name,'):
            output = self.profiles[args[-1]]
        elif args[:3] == ['nmcli', '-g', 'GENERAL.CON-UUID']:
            output = self.current_nm_uuid + '\n'
        if self.fail_command and args[:len(self.fail_command)] == self.fail_command:
            code = 1
        if check and code:
            raise subprocess.CalledProcessError(code, args, output=output, stderr='test failure')
        return subprocess.CompletedProcess(args, code, output, '')

    def plan(self, **kwargs):
        return installer.prepare_install(check_host=False, reserved_ifaces=['eth0', 'eth2'], **kwargs)

    def test_pinned_real_arm64_payload_sha_and_markers(self):
        manifest, source = installer.verify_runtime(installer.BUNDLE, check_host=False)
        self.assertEqual(manifest['sha256'], installer.digest(source))
        self.assertEqual(manifest['sdk_version'], '3.103.13')

    def test_altered_runtime_is_rejected_before_writes(self):
        bundle = self.root / 'bundle'
        shutil.copytree(installer.BUNDLE, bundle)
        with (bundle / 'get_pcd_record').open('ab') as out:
            out.write(b'changed')
        with self.assertRaisesRegex(ValueError, 'SHA256'):
            self.plan(runtime_dir=bundle)
        self.assertFalse(installer.LIDAR_CONFIG.exists())

    def test_no_carrier_is_allowed_and_no_sensor_contact_is_attempted(self):
        plan = self.plan()
        receipt = installer.apply_plan(plan)
        self.assertTrue(installer.LIDAR_CONFIG.exists())
        self.assertIn(['systemctl', 'enable', installer.UNIT], self.commands)
        self.assertIn(['systemctl', 'start', installer.UNIT], self.commands)
        self.assertFalse(any('uav-lidar-record.service' in c and c[1] in ('start', 'enable', 'restart') for c in self.commands))
        self.assertFalse(any('curl' in c or 'ping' in c for c in self.commands))
        self.assertTrue(Path(receipt['backup'], 'receipt.json').exists())

    def test_main_payload_mk22_and_extension_are_all_excluded(self):
        for key in ('payload_iface', 'mk22_iface', 'extension_iface'):
            network.MAIN_CONFIG.write_text(json.dumps({key: 'eth1'}))
            with self.assertRaisesRegex(ValueError, '不能占用'):
                self.plan()
        network.MAIN_CONFIG.unlink()
        with self.assertRaisesRegex(ValueError, '不能占用'):
            installer.prepare_install(reserved_ifaces=['eth1'], check_host=False)

    def test_existing_management_ip_is_not_overwritten(self):
        self.infos[0] = eth(addresses=['192.168.1.104/24'])
        with self.assertRaisesRegex(ValueError, '其他网段'):
            self.plan()
        self.assertFalse(any(c[:3] == ['ip', 'address', 'add'] for c in self.commands))

    def test_default_route_and_other_link_subnet_conflicts_are_rejected(self):
        self.routes = [{'dst': 'default', 'dev': 'eth1', 'gateway': '172.168.1.1'}]
        with self.assertRaisesRegex(ValueError, '默认路由'):
            self.plan()
        self.routes = []
        self.infos[1] = eth('eth0', ['172.168.1.2/24'])
        with self.assertRaisesRegex(ValueError, '其他接口重叠'):
            self.plan()

    def test_bound_bridge_and_nonexistent_interface_are_rejected(self):
        self.infos[0]['master'] = 'br0'
        with self.assertRaisesRegex(ValueError, '不是独立'):
            self.plan()
        with self.assertRaisesRegex(ValueError, '不存在'):
            self.plan(interface='eth9')

    def test_existing_matching_nm_static_profile_is_reused_without_rewriting(self):
        self.nm_active = True
        identifier = '11111111-1111-1111-1111-111111111111'
        self.profiles[identifier] = 'eth1\nmanual\n172.168.1.100/24\n\n\nyes\n'
        plan = self.plan()
        self.assertEqual(plan['network']['nm_uuid'], identifier)
        self.assertFalse(plan['nm_profile_created'])
        self.assertNotIn(str(installer.NM_PROFILE), [f['path'] for f in plan['files']])

    def test_new_nm_profile_preserves_saved_and_live_same_subnet_addresses(self):
        self.nm_active = True
        self.infos[0] = eth(addresses=['172.168.1.98/24'])
        self.profiles['11111111-1111-1111-1111-111111111111'] = 'eth1\nmanual\n172.168.1.99/24\n\n\nyes\n'
        plan = self.plan()
        keyfile = next(f for f in plan['files'] if f['path'] == str(installer.NM_PROFILE))
        self.assertEqual(keyfile['mode'], 0o600)
        for value in ('172.168.1.100/24', '172.168.1.99/24', '172.168.1.98/24', 'never-default=true'):
            self.assertIn(value.encode(), keyfile['data'])
        self.assertNotIn(b'gateway=', keyfile['data'])

    def test_offline_conflicting_nm_profile_is_rejected(self):
        self.nm_active = True
        identifier = '11111111-1111-1111-1111-111111111111'
        self.profiles[identifier] = 'eth1\nmanual\n192.168.2.36/24\n\n\nyes\n'
        with self.assertRaisesRegex(ValueError, '持久配置'):
            self.plan()

    def test_existing_exact_runtime_is_reused_and_other_lidar_settings_preserved(self):
        existing = self.root / 'old-sdk'
        shutil.copy2(installer.BUNDLE / 'get_pcd_record', existing)
        existing.chmod(0o755)
        installer.LIDAR_CONFIG.write_text(json.dumps({'sdk': str(existing), 'segment_seconds': 120, 'root': '/data/custom-lidar'}))
        plan = self.plan()
        self.assertTrue(plan['runtime_reused'])
        self.assertEqual(plan['sdk'], str(existing))
        config = json.loads(next(f for f in plan['files'] if f['path'] == str(installer.LIDAR_CONFIG))['data'])
        self.assertEqual(config['segment_seconds'], 120)
        self.assertEqual(config['root'], '/data/custom-lidar')
        self.assertNotIn(str(existing), [f['path'] for f in plan['files']])

    def test_no_start_enables_network_only_without_activating_it(self):
        installer.apply_plan(self.plan(), start=False)
        self.assertIn(['systemctl', 'enable', installer.UNIT], self.commands)
        self.assertNotIn(['systemctl', 'start', installer.UNIT], self.commands)

    def test_external_transaction_rollback_restores_config_and_only_our_ip(self):
        installer.LIDAR_CONFIG.write_text('{"segment_seconds":120}\n')
        original = installer.LIDAR_CONFIG.read_bytes()
        receipt = installer.apply_plan(self.plan())
        installer.rollback(receipt)
        self.assertEqual(installer.LIDAR_CONFIG.read_bytes(), original)
        self.assertFalse(installer.NETWORK_CONFIG.exists())
        self.assertIn(['ip', 'address', 'del', '172.168.1.100/24', 'dev', 'eth1'], self.commands)
        self.assertFalse(any('flush' in c for c in self.commands))

    def test_rollback_never_deletes_preexisting_radar_address(self):
        self.infos[0] = eth(addresses=['172.168.1.100/24'])
        receipt = installer.apply_plan(self.plan())
        installer.rollback(receipt)
        self.assertFalse(any(c[:3] == ['ip', 'address', 'del'] for c in self.commands))

    def test_failed_first_write_does_not_remove_untouched_existing_config(self):
        installer.LIDAR_CONFIG.write_text('{"segment_seconds":120}\n')
        with patch.object(installer, 'atomic_write', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                installer.apply_plan(self.plan())
        self.assertEqual(installer.LIDAR_CONFIG.read_text(), '{"segment_seconds":120}\n')

    def test_start_failure_rolls_back_applied_files(self):
        self.fail_command = ['systemctl', 'start', installer.UNIT]
        with self.assertRaises(subprocess.CalledProcessError):
            installer.apply_plan(self.plan())
        self.assertFalse(installer.LIDAR_CONFIG.exists())
        self.assertFalse(installer.UNIT_PATH.exists())

    def test_active_recording_refuses_any_file_update(self):
        plan = self.plan()
        self.active_state = 'active'
        with self.assertRaisesRegex(ValueError, '正在采集'):
            installer.apply_plan(plan)
        self.assertFalse(installer.LIDAR_CONFIG.exists())

    def test_legacy_recording_lock_is_respected_before_sdk_process_spawns(self):
        data = self.root / 'recordings'
        data.mkdir()
        installer.LIDAR_CONFIG.write_text(json.dumps({'root': str(data)}))
        with (data / '.uav-lidar-record.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, '旧版点云采集'):
                installer.apply_plan(self.plan())
        self.assertFalse(installer.NETWORK_CONFIG.exists())

    def test_reused_sdk_changed_after_preflight_is_rejected(self):
        existing = self.root / 'old-sdk'
        shutil.copy2(installer.BUNDLE / 'get_pcd_record', existing)
        existing.chmod(0o755)
        installer.LIDAR_CONFIG.write_text(json.dumps({'sdk': str(existing)}))
        plan = self.plan()
        existing.write_bytes(b'new version')
        with self.assertRaisesRegex(ValueError, '发生变化'):
            installer.apply_plan(plan)
        self.assertFalse(installer.NETWORK_CONFIG.exists())

    def test_new_nm_profile_is_deleted_before_previous_keyfile_is_restored(self):
        self.nm_active = True
        installer.NM_PROFILE.write_text('old-keyfile')
        receipt = installer.apply_plan(self.plan())
        calls = []
        original_run = self.fake_run
        original_write = installer.atomic_write
        def observed_run(args, *a, **kw):
            if args[:3] == ['nmcli', 'connection', 'delete']:
                calls.append('delete')
            return original_run(args, *a, **kw)
        def observed_write(path, data, mode):
            if path == installer.NM_PROFILE and data == b'old-keyfile':
                calls.append('restore')
            return original_write(path, data, mode)
        with patch.object(installer, 'run', side_effect=observed_run), patch.object(installer, 'atomic_write', side_effect=observed_write):
            installer.rollback(receipt)
        self.assertEqual(calls, ['delete', 'restore'])
        self.assertEqual(installer.NM_PROFILE.read_text(), 'old-keyfile')

    def test_symlink_destination_is_rejected_without_following_it(self):
        protected = self.root / 'protected'
        protected.write_text('original')
        installer.NETWORK_CONFIG.symlink_to(protected)
        with self.assertRaisesRegex(ValueError, '不是普通文件'):
            installer.apply_plan(self.plan())
        self.assertEqual(protected.read_text(), 'original')

    def test_network_apply_is_additive_and_does_not_flush_or_change_gateway(self):
        self.infos[0] = eth(addresses=['172.168.1.99/24'])
        with patch.object(Path, 'read_text', side_effect=OSError('not plugged')):
            result = network.apply(dict(interface='eth1', host_cidr='172.168.1.100/24', lidar_ip='172.168.1.10'))
        self.assertTrue(result['address_added'])
        self.assertFalse(result['capture_started'])
        self.assertIn(['ip', 'address', 'add', '172.168.1.100/24', 'dev', 'eth1'], self.commands)
        self.assertFalse(any('flush' in c or 'gateway' in c or 'default' in c for c in self.commands))

    def test_network_apply_does_not_readd_existing_address(self):
        self.infos[0] = eth(addresses=['172.168.1.100/24'])
        with patch.object(Path, 'read_text', side_effect=OSError('not plugged')):
            result = network.apply(dict(interface='eth1', host_cidr='172.168.1.100/24', lidar_ip='172.168.1.10'))
        self.assertFalse(result['address_added'])
        self.assertFalse(any(c[:3] == ['ip', 'address', 'add'] for c in self.commands))

    def test_nm_dispatcher_does_not_reactivate_already_active_profile(self):
        identifier = '11111111-1111-1111-1111-111111111111'
        self.current_nm_uuid = identifier
        self.infos[0] = eth(addresses=['172.168.1.100/24'])
        with patch.object(Path, 'read_text', return_value='1'):
            network.apply(dict(interface='eth1', host_cidr='172.168.1.100/24', lidar_ip='172.168.1.10', nm_uuid=identifier))
        self.assertFalse(any(c[:4] == ['nmcli', '--wait', '8', 'connection'] for c in self.commands))


if __name__ == '__main__':
    unittest.main()
