import copy
from contextlib import ExitStack
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'algorithm'))
import full_install as m
import full_runtime as r
import runtime_bundle as b


def done(command, **kwargs):
    return subprocess.CompletedProcess(command, 0, '', '')


def fixture_bundle(root, change=None):
    elf = b'\x7fELF\x02\x01' + b'\x00' * 12 + (183).to_bytes(2, 'little') + b'fixture'
    files = {name: elf for name in b.REQUIRED}
    files.update({'stream/TTTracker_V1.1_publish/model/' + name: b'model-data' for name in b.MODEL_NAMES})
    files.update({name: b'Person\nCar\n' for name in b.LABELS})
    files['stream/TTTracker_V1.1_publish/libs/rknn/librknnrt.so'] = b'runtime-library'
    manifest = {'schema': 1, 'architecture': 'aarch64', 'files': {},
                'models': sorted(name for name in files if name.endswith('.rknn')),
                'library_dirs': ['stream/TTTracker_V1.1_publish/libs/rknn']}
    for name, data in files.items():
        manifest['files'][name] = {'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data),
                                   'mode': 0o755 if name in b.REQUIRED else 0o644}
    if change:
        change(manifest, files)
    archive = root / 'runtime.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        all_files = dict(files, **{'runtime-manifest.json': json.dumps(manifest).encode()})
        for name, data in all_files.items():
            info = tarfile.TarInfo(name); info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return archive, b.sha256(archive), manifest


def config(root):
    c = {'iface': 'eth0', 'address': '192.168.10.2/24', 'host': '192.168.10.1', 'user': 'dev',
         'network_mode': 'direct', 'network_uuid': '', 'data_root': str(root),
         'release': str(root / 'uav-algorithm/releases/testdigest'),
         'library_dirs': ['stream/TTTracker_V1.1_publish/libs/rknn']}
    for slot, n in (('front1', 1), ('front2', 2), ('infrared', 3)):
        c[slot] = {'device': '/dev/video%d' % n, 'rotate': 0, 'enabled': True,
                   'bitrate': {'front1': 5000, 'front2': 3000, 'infrared': 1500}[slot]}
    return c


class BundleTests(unittest.TestCase):
    def test_actual_tar_hash_extract_and_tamper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive, digest, manifest = fixture_bundle(root)
            dest = root / 'extracted'
            b.extract(archive, digest, dest)
            self.assertTrue(b.verify_tree(dest, manifest))
            (dest / 'stream/tracker_worker').write_bytes(b'changed')
            self.assertFalse(b.verify_tree(dest, manifest))

    def test_outer_hash_failure_creates_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); archive, _, _ = fixture_bundle(root)
            with self.assertRaisesRegex(ValueError, 'SHA256'):
                b.extract(archive, '0' * 64, root / 'dest')
            self.assertFalse((root / 'dest').exists())

    def test_inner_hash_failure_cleans_extracted_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive, digest, _ = fixture_bundle(root, lambda manifest, files: files.update({'stream/tracker_worker': b'changed' + b'X' * 20}))
            with self.assertRaisesRegex(ValueError, '校验失败'):
                b.extract(archive, digest, root / 'dest')
            self.assertFalse((root / 'dest').exists())

    def test_unsafe_path_symlink_and_duplicate_members_rejected(self):
        for case in ('traversal', 'symlink', 'duplicate'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); archive, _, _ = fixture_bundle(root)
                with tarfile.open(archive, 'r:gz') as source:
                    contents = [(x, source.extractfile(x).read()) for x in source.getmembers()]
                with tarfile.open(archive, 'w:gz') as target:
                    for entry, data in contents:
                        target.addfile(entry, io.BytesIO(data))
                    entry = tarfile.TarInfo('../outside' if case == 'traversal' else 'stream/link' if case == 'symlink' else contents[0][0].name)
                    if case == 'symlink':
                        entry.type = tarfile.SYMTYPE; entry.linkname = '/etc/passwd'
                    target.addfile(entry, io.BytesIO(b''))
                with self.assertRaises(ValueError):
                    b.extract(archive, b.sha256(archive), root / 'dest')
                self.assertFalse((root / 'outside').exists())

    def test_rejects_static_library_as_runtime_and_missing_models(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, manifest = fixture_bundle(Path(directory))
            a = copy.deepcopy(manifest)
            a['files']['stream/libTrackerLib.a'] = dict(next(iter(a['files'].values())))
            with self.assertRaisesRegex(ValueError, '静态'):
                b.validate_manifest(a)
            a = copy.deepcopy(manifest); a['models'] = []
            with self.assertRaisesRegex(ValueError, '模型'):
                b.validate_manifest(a)

    def test_refuses_existing_destination_or_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); archive, digest, _ = fixture_bundle(root)
            dest = root / 'dest'; dest.mkdir(); (dest / 'keep').write_text('original')
            with self.assertRaises(ValueError):b.extract(archive, digest, dest)
            self.assertEqual((dest / 'keep').read_text(), 'original')
            link = root / 'link'; link.symlink_to(dest)
            with self.assertRaises(ValueError):b.extract(archive, digest, link)

    def test_private_staging_umask_does_not_make_release_unreadable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); archive, digest, _ = fixture_bundle(root)
            previous = os.umask(0o077)
            try:
                dest = root / 'private-stage'; dest.mkdir(mode=0o700)
                b.extract(archive, digest, dest)
            finally:
                os.umask(previous)
            self.assertEqual(dest.stat().st_mode & 0o777, 0o755)
            self.assertTrue(all((p.stat().st_mode & 0o005) == 0o005 for p in dest.rglob('*') if p.is_dir()))


class FullRuntimeTests(unittest.TestCase):
    def test_algorithm_has_original_quality_and_parallel_cpu_low_config(self):
        c = config(Path('/data'))
        args, env, cwd = r.build(c, 'algorithm')
        self.assertIn('input=nv12,format=rgb,bitrate=5000,fps=30,gop=15,rot=0', args)
        self.assertEqual(env['TRACKER_MULTI_OBJECT'], '1')
        self.assertEqual(env['UAV_TRACKER_CPUS'], '6,7')
        self.assertEqual(env['OPENCV_FOR_THREADS_NUM'], '8')
        self.assertEqual(env['UAV_LOW_CPUS'], '0-3')
        self.assertEqual(str(cwd), c['release'] + '/stream/TTTracker_V1.1_publish/build')
        self.assertNotIn('/mnt/data/csj', env['LD_LIBRARY_PATH'])

    def test_preview_and_ir_do_not_run_algorithm(self):
        c = config(Path('/data'))
        with patch.dict(os.environ, {'TRACKER_MULTI_OBJECT': '1', 'UAV_TRACKER_CPUS': '6,7'}):
            args, env, _ = r.build(c, 'preview')
            self.assertIn('input=nv12,format=yuv,bitrate=3000,fps=30,gop=15,rot=0', args)
            self.assertNotIn('TRACKER_MULTI_OBJECT', env)
            args, env, _ = r.build(c, 'infrared')
            self.assertTrue(args[0].endswith('/mino17_streamer'))
            self.assertEqual(args[args.index('--port') + 1], '5604')
            self.assertNotIn('TRACKER_MULTI_OBJECT', env)

    def test_absent_ir_waits_without_touching_other_camera(self):
        c = config(Path('/data'))
        with patch.object(os.path, 'ismount', return_value=True), patch.object(r.raw, 'has_address', return_value=True), patch.object(r.raw, 'addresses', return_value=[]), patch.object(r.raw, 'run', return_value=subprocess.CompletedProcess([], 0, '[{"dev":"eth0"}]', '')) as run, patch.object(Path, 'exists', return_value=False), patch.object(r.time, 'sleep', side_effect=InterruptedError):
            with self.assertRaises(InterruptedError):r.prepare(c, 'infrared')
        self.assertTrue(all(x.args[0][0] == 'ip' for x in run.call_args_list))

    def test_units_independent_names_and_cpu_binding(self):
        c = config(Path('/data'))
        with patch.object(m, 'ctl', return_value=subprocess.CompletedProcess([], 1, '', '')):
            contents = m.unit_files(c)
        alg = contents[m.UNITS / r.UNITS['algorithm']]
        self.assertIn('CPUAffinity=4 5', alg)
        self.assertIn('LogRateLimitBurst=200', alg)
        for slot in r.UNITS:
            text = contents[m.UNITS / r.UNITS[slot]]
            self.assertNotIn('Requires=uav-front-', text)
            self.assertIn('RestartSec=30', text)
            self.assertIn('RequiresMountsFor=/data', text)


class FullNetworkTests(unittest.TestCase):
    def test_ssh_interface_is_never_reconfigured_even_without_env(self):
        def run(cmd, **kw):
            out = '[]'
            if cmd[0] == 'ss':out = '0 0 192.168.10.2:22 192.168.10.1:54123\n'
            if cmd[:5] == ['ip', '-j', 'route', 'get', '192.168.10.1']:out = '[{"dev":"eth0"}]'
            return subprocess.CompletedProcess(cmd, 0, out, '')
        c = config(Path('/data'))
        with patch.object(Path, 'exists', lambda p: p.name != 'wireless'), patch.object(m.rt, 'addresses', return_value=[]), patch.object(m.rt, 'run', side_effect=run), patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, 'SSH'):m.assert_network_safe(c)

    def test_default_route_interface_is_never_reconfigured(self):
        c = config(Path('/data'))
        with patch.object(Path, 'exists', lambda p: p.name != 'wireless'), patch.object(m.rt, 'addresses', return_value=[]), patch.object(m.rt, 'run', return_value=subprocess.CompletedProcess([], 0, '[{"dev":"eth0"}]', '')):
            with self.assertRaisesRegex(ValueError, '默认路由'):m.assert_network_safe(c)

    def test_nm_managed_safe_new_profile_not_rejected(self):
        def run(cmd, **kw):
            if 'GENERAL.STATE' in cmd:return subprocess.CompletedProcess(cmd, 0, '100 (connected)', '')
            if 'GENERAL.CON-UUID' in cmd:return subprocess.CompletedProcess(cmd, 0, '--', '')
            return subprocess.CompletedProcess(cmd, 10, '', '')
        with patch.object(m, 'assert_network_safe'), patch.object(m.shutil, 'which', return_value='/usr/bin/nmcli'), patch.object(m.rt, 'run', side_effect=run):
            c = m.network_plan(config(Path('/data')))
        self.assertEqual(c['network_mode'], 'nm-new')
        self.assertIn('never-default=true', m.nm_text(c))
        self.assertNotIn('gateway=', m.nm_text(c))

    def test_nm_compatible_static_reused_without_mutation(self):
        identity = '12345678-1234-1234-1234-123456789abc'
        values = {'GENERAL.STATE': '100 (connected)', 'GENERAL.CON-UUID': identity,
                  'ipv4.method': 'manual', 'ipv4.addresses': '192.168.10.2/24',
                  'ipv4.never-default': 'yes', 'ipv4.gateway': ''}
        def run(cmd, **kw):return subprocess.CompletedProcess(cmd, 0, values[cmd[2]], '')
        with patch.object(m, 'assert_network_safe'), patch.object(m.shutil, 'which', return_value='/usr/bin/nmcli'), patch.object(m.rt, 'run', side_effect=run) as call:
            c = m.network_plan(config(Path('/data')))
        self.assertEqual(c['network_mode'], 'nm-reuse')
        self.assertEqual(c['network_uuid'], identity)
        self.assertFalse(any(x.args[0][1:3] in (['connection', 'modify'], ['connection', 'up']) for x in call.call_args_list))

    def test_root_filesystem_never_used_for_runtime(self):
        with patch.object(os.path, 'ismount', return_value=False):
            with self.assertRaisesRegex(ValueError, '数据盘'):m.mounted_data_root('/data')

    def test_argument_preflight_rejects_invalid_rotation_before_storage(self):
        args = m.arguments(['install', '--front1-rotate', '13'])
        with patch.object(m, 'fresh_check') as fresh:
            with self.assertRaisesRegex(ValueError, '旋转'):m.preflight_arguments(args)
            fresh.assert_not_called()


class FullInstallTests(unittest.TestCase):
    def fixture(self, root):
        archive, digest, manifest = fixture_bundle(root)
        c = config(root / 'data'); Path(c['data_root']).mkdir()
        units = root / 'units'; units.mkdir()
        paths = dict(BASE=root / 'opt', CONFIG_DIR=root / 'etc', UNITS=units,
                     LAUNCHER=root / 'bin/uav-algorithm', NM_FILE=root / 'nm/uav.nmconnection',
                     DROPIN=units / 'factory.service.d/95-full.conf', BACKUPS=root / 'backups')
        stack = ExitStack()
        stack.enter_context(patch.multiple(m, **paths))
        stack.enter_context(patch.object(m, 'fresh_check'))
        stack.enter_context(patch.object(m, 'ensure_tools'))
        stack.enter_context(patch.object(m, 'check_runtime'))
        stack.enter_context(patch.object(m.rt, 'addresses', return_value=[]))
        stack.enter_context(patch.object(m.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())))
        return stack, c, archive, digest, manifest, paths

    def test_real_filesystem_install_no_start_and_idempotence(self):
        with tempfile.TemporaryDirectory() as directory:
            stack, c, archive, digest, manifest, paths = self.fixture(Path(directory))
            with stack, patch.object(m.rt, 'run', side_effect=done) as call:
                m.install(c, archive, digest, manifest, 'installer', no_start=True)
                self.assertTrue((paths['BASE'] / 'installation-full.json').exists())
                self.assertTrue(b.verify_tree(c['release'], manifest))
                self.assertFalse(any(x.args[0][:2] == ['systemctl', 'start'] for x in call.call_args_list))
                call.reset_mock()
                m.install(c, archive, digest, manifest, 'installer', no_start=True)
                call.assert_not_called()
                (Path(c['release']) / 'stream/rk_streamer').write_text('tampered')
                with self.assertRaisesRegex(ValueError, '校验失败'):
                    m.install(c, archive, digest, manifest, 'installer', no_start=True)

    def test_failure_start_rolls_back_only_owned_files_keeps_original_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stack, c, archive, digest, manifest, paths = self.fixture(root)
            keep = paths['UNITS'] / 'downstream-static-proxy.service'; keep.write_text('factory-original')
            def run(cmd, **kw):
                if cmd[:2] == ['systemctl', 'start']:raise subprocess.CalledProcessError(1, cmd)
                return done(cmd, **kw)
            with stack, patch.object(m.rt, 'run', side_effect=run), patch.object(m.rt, 'has_address', return_value=True):
                with self.assertRaises(subprocess.CalledProcessError):
                    m.install(c, archive, digest, manifest, 'installer')
            self.assertEqual(keep.read_text(), 'factory-original')
            self.assertFalse(paths['BASE'].exists())
            self.assertFalse(paths['CONFIG_DIR'].exists())
            self.assertFalse((Path(c['data_root']) / 'uav-algorithm').exists())
            self.assertEqual(list(paths['UNITS'].glob('*.service')), [keep])

    def test_dependency_failure_never_stops_services(self):
        with tempfile.TemporaryDirectory() as directory:
            stack, c, archive, digest, manifest, paths = self.fixture(Path(directory))
            with stack, patch.object(m.rt, 'run', side_effect=done) as call, patch.object(m, 'check_runtime', side_effect=ValueError('missing library')):
                with self.assertRaisesRegex(ValueError, 'library'):
                    m.install(c, archive, digest, manifest, 'installer')
            self.assertFalse(any(x.args[0][0] == 'systemctl' for x in call.call_args_list))
            self.assertFalse(paths['BASE'].exists())

    def test_fresh_check_refuses_existing_service_without_stopping(self):
        def run(cmd, **kw):
            if cmd[:2] == ['systemctl', 'is-active']:return subprocess.CompletedProcess(cmd, 0, 'active', '')
            return done(cmd, **kw)
        with patch.object(Path, 'exists', return_value=False), patch.object(Path, 'is_symlink', return_value=False), patch.object(m.rt, 'run', side_effect=run) as call:
            with self.assertRaisesRegex(ValueError, '已有服务'):
                m.fresh_check(config(Path('/data')))
        self.assertFalse(any('stop' in x.args[0] for x in call.call_args_list))

    def test_ldd_check_rejects_incompatible_bsp_without_running_program(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); archive, digest, manifest = fixture_bundle(root)
            stage = root / 'stage'; b.extract(archive, digest, stage)
            with patch.object(m.rt, 'run', return_value=subprocess.CompletedProcess([], 0, 'libopencv_core.so.3.4 => not found', '')) as call:
                with self.assertRaisesRegex(ValueError, '运行依赖'):m.check_runtime(stage, manifest)
            self.assertTrue(all(x.args[0][0] == 'ldd' for x in call.call_args_list))

    def test_nm_no_start_loads_disabled_profile_without_activating(self):
        with tempfile.TemporaryDirectory() as directory:
            stack, c, archive, digest, manifest, paths = self.fixture(Path(directory))
            c.update(network_mode='nm-new', network_uuid='12345678-1234-1234-1234-123456789abc')
            with stack, patch.object(m.rt, 'run', side_effect=done) as call:
                m.install(c, archive, digest, manifest, 'installer', no_start=True)
            self.assertIn('autoconnect=false', paths['NM_FILE'].read_text())
            commands = [x.args[0] for x in call.call_args_list]
            self.assertIn(['nmcli', 'connection', 'load', str(paths['NM_FILE'])], commands)
            self.assertFalse(any(c[:3] in (['nmcli', 'connection', 'up'], ['nmcli', 'connection', 'modify']) or c[:2] == ['systemctl', 'start'] for c in commands))

    def test_nm_load_failure_rolls_back_only_new_profile_and_keeps_no_start(self):
        with tempfile.TemporaryDirectory() as directory:
            stack, c, archive, digest, manifest, paths = self.fixture(Path(directory))
            c.update(network_mode='nm-new', network_uuid='12345678-1234-1234-1234-123456789abc')
            def run(cmd, **kw):
                if cmd[:3] == ['nmcli', 'connection', 'load']:
                    raise subprocess.CalledProcessError(1, cmd)
                return done(cmd, **kw)
            with stack, patch.object(m.rt, 'run', side_effect=run) as call:
                with self.assertRaises(subprocess.CalledProcessError):
                    m.install(c, archive, digest, manifest, 'installer', no_start=True)
            commands = [x.args[0] for x in call.call_args_list]
            self.assertIn(['nmcli', 'connection', 'delete', 'uuid', c['network_uuid']], commands)
            self.assertFalse(any(x[:3] == ['nmcli', 'connection', 'up'] for x in commands))
            self.assertFalse(paths['NM_FILE'].exists())
            self.assertFalse(paths['BASE'].exists())


if __name__ == '__main__':
    unittest.main()
