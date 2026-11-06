import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]


class GimbalInstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='uav-gimbal-test-', dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'device'
        self.pkg = self.base / 'package'
        shutil.copytree(REPO / 'gimbal', self.pkg,
                        ignore=shutil.ignore_patterns('runtime', '__pycache__', 'PAYLOAD.sha256'))
        for path in ('home/iqfiles', 'etc/init.d', 'etc/cfg', 'usr/local/bin', 'data'):
            (self.root / path).mkdir(parents=True, exist_ok=True)
        for path, data in {'home/hust_tracker_3576': b'original factory binary',
                           'home/device.cert': b'device-bound-certificate',
                           'home/iqfiles/camera.json': b'factory IQ',
                           'etc/init.d/S99AppStart': b'factory boot'}.items():
            (self.root / path).write_bytes(data)
        for profile in ('old', 'new'):
            path = self.pkg / 'runtime/bin' / profile / 'hust_tracker_3576'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(('ELF test ' + profile).encode())
        (self.pkg / 'runtime/lib').mkdir()
        (self.pkg / 'runtime/lib/libtest.so').write_bytes(b'library')
        (self.pkg / 'runtime/model').mkdir()
        (self.pkg / 'runtime/model/detection.rknn').write_bytes(b'model')
        (self.pkg / 'VERSION').write_text('v1.7.0\n')
        self.manifest()
        self.env = dict(os.environ, UAV_GIMBAL_TESTING='1', UAV_GIMBAL_TEST_ROOT=str(self.root))

    def manifest(self):
        files = sorted(p for p in self.pkg.rglob('*') if p.is_file() and p.name != 'PAYLOAD.sha256')
        (self.pkg / 'PAYLOAD.sha256').write_text(''.join(
            hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + str(p.relative_to(self.pkg)) + '\n'
            for p in files))

    def install(self, *args):
        return subprocess.run(['sh', str(self.pkg / 'install.sh'), '--profile', 'new',
                               '--no-start', *args], env=self.env,
                              capture_output=True, text=True, timeout=20)

    def test_check_does_not_modify_factory_files(self):
        before = {str(p.relative_to(self.root)): p.read_bytes()
                  for p in self.root.rglob('*') if p.is_file()}
        result = self.install('--check')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        after = {str(p.relative_to(self.root)): p.read_bytes()
                 for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertFalse((self.root / 'data/uav-gimbal').exists())

    def test_install_preserves_identity_iq_calibration_and_is_idempotent(self):
        (self.root / 'home/recap_servo.env').write_bytes(b'my measured calibration')
        first = self.install()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual((self.root / 'home/hust_tracker_3576').read_bytes(), b'ELF test new')
        self.assertEqual((self.root / 'home/device.cert').read_bytes(), b'device-bound-certificate')
        self.assertEqual((self.root / 'home/iqfiles/camera.json').read_bytes(), b'factory IQ')
        self.assertEqual((self.root / 'home/recap_servo.env').read_bytes(), b'my measured calibration')
        self.assertIn('HUST_RGB_SOURCE_FPS=60', (self.root / 'etc/cfg/uav-gimbal.env').read_text())
        self.assertTrue((self.root / 'etc/init.d/_99AppStart').exists())
        self.assertFalse((self.root / 'etc/init.d/S99AppStart').exists())
        second = self.install()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn('already installed', second.stdout)

    def test_old_profile_selects_original_camera_and_binary(self):
        result = self.install('--profile', 'old')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / 'home/hust_tracker_3576').read_bytes(), b'ELF test old')
        config = (self.root / 'etc/cfg/uav-gimbal.env').read_text()
        self.assertIn('UAV_GIMBAL_CAMERA_ID=2', config)
        self.assertNotIn('export HUST_RGB_SOURCE_FPS=60', config)

    def test_restore_recovers_original_boot_and_removes_only_created_paths(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        restore = next((self.root / 'data/uav-gimbal/backups').glob('*/restore.sh'))
        result = subprocess.run(['sh', str(restore), '--no-start'], env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / 'home/hust_tracker_3576').read_bytes(), b'original factory binary')
        self.assertEqual((self.root / 'etc/init.d/S99AppStart').read_bytes(), b'factory boot')
        self.assertFalse((self.root / 'model').exists())
        self.assertFalse((self.root / 'data/uav-gimbal/current').exists())
        self.assertFalse((self.root / 'etc/cfg/uav-gimbal-installation').exists())
        self.assertEqual((self.root / 'home/device.cert').read_bytes(), b'device-bound-certificate')

    def test_bad_payload_is_rejected_before_touching_the_device(self):
        (self.pkg / 'runtime/bin/new/hust_tracker_3576').write_bytes(b'corrupted')
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / 'home/hust_tracker_3576').read_bytes(), b'original factory binary')
        self.assertFalse((self.root / 'data/uav-gimbal').exists())

    def test_different_existing_model_is_preserved(self):
        (self.root / 'model').mkdir()
        (self.root / 'model/detection.rknn').write_bytes(b'other application model')
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Existing /model differs', result.stderr)
        self.assertEqual((self.root / 'model/detection.rknn').read_bytes(), b'other application model')

    def test_failed_copy_restores_previous_application(self):
        tools = self.base / 'mock-bin'
        tools.mkdir()
        cp = tools / 'cp'
        cp.write_text('#!/bin/sh\ncase "$2" in */runtime/.) exit 70 ;; esac\nexec /bin/cp "$@"\n')
        cp.chmod(0o755)
        self.env['PATH'] = str(tools) + ':' + self.env['PATH']
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / 'home/hust_tracker_3576').read_bytes(), b'original factory binary')
        self.assertTrue((self.root / 'etc/init.d/S99AppStart').exists())
        self.assertFalse((self.root / 'etc/cfg/uav-gimbal-installation').exists())


if __name__ == '__main__':
    unittest.main()
