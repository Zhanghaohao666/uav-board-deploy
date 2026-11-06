"""Failure paths must never fall back to RGA userptr or resubmit after failure."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('gcc'), 'requires gcc')
class SafeRgaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='uav-rga-unit-')
        cls.binary = Path(cls.temp.name) / 'probe'
        subprocess.run([
            'gcc', '-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
            '-I' + str(ROOT / 'tests/fakes'), '-I' + str(ROOT / 'board/src'),
            str(ROOT / 'tests/rga_safe_unit.c'), str(ROOT / 'board/src/rga_safe.c'),
            '-Wl,--wrap=open,--wrap=close,--wrap=ioctl,--wrap=mmap,--wrap=munmap',
            '-lpthread', '-o', str(cls.binary),
        ], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def probe(self, scenario):
        result = subprocess.run([str(self.binary), scenario], capture_output=True,
                                text=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_nv12_visible_planes_padding_and_canary(self): self.probe('padding')
    def test_imported_buffers_are_reused(self): self.probe('reuse')
    def test_invalid_layout_never_reaches_driver(self): self.probe('invalid')
    def test_dma32_failure_has_no_unsafe_fallback(self): self.probe('alloc')
    def test_rga3_configuration_failure_has_no_rga2_fallback(self): self.probe('core')
    def test_import_failure_closes_fd_and_mapping(self): self.probe('import')
    def test_submission_failure_blocks_resubmission(self): self.probe('submit')
    def test_sync_failure_does_not_submit(self): self.probe('sync')
    def test_thread_exit_releases_private_buffers(self): self.probe('thread')
    def test_repeated_idle_cycles_do_not_leak(self): self.probe('cycles')
    def test_changed_shapes_keep_a_bounded_cache(self): self.probe('lru')


if __name__ == '__main__':
    unittest.main()
