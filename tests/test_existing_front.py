import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'algorithm'))
import existing_front as f
import migrate_existing as m

class ExistingFrontTests(unittest.TestCase):
    def test_one_camera_and_algorithm_only_on_algorithm_slot(self):
        a, env, cwd = f.build('algorithm', {})
        b, rawenv, rawcwd = f.build('preview', {'TRACKER_MULTI_OBJECT': '1'})
        self.assertEqual(a.count('--cam'), 1)
        self.assertEqual(b.count('--cam'), 1)
        self.assertNotEqual(a[a.index('--cam')+1], b[b.index('--cam')+1])
        self.assertIn('5602', a); self.assertIn('5603', b)
        self.assertEqual(a[a.index('--format')+1], 'rgb')
        self.assertEqual(b[b.index('--format')+1], 'yuv')
        self.assertEqual(env['TRACKER_MULTI_OBJECT'], '1')
        self.assertNotIn('TRACKER_MULTI_OBJECT', rawenv)
        self.assertTrue(str(cwd).endswith('TTTracker_V1.1_publish/build'))

    def test_missing_own_camera_never_opens_or_checks_other_camera(self):
        for slot in f.DEFAULTS:
            with patch.object(sys, 'argv', ['worker', slot]), patch.object(f.runtime, 'inspect_camera', return_value=('missing','absent')) as check, patch.object(f.runtime, 'supervise') as start:
                with self.assertRaisesRegex(ValueError,'仅本路'): f.main()
                self.assertEqual(check.call_count,1)
                self.assertEqual(check.call_args.args[1]['device'],f.DEFAULTS[slot]['device'])
                start.assert_not_called()

    def test_units_have_separate_env_and_no_cross_stop_dependency(self):
        for slot in m.SLOTS:
            text=m.text(slot)
            self.assertIn('/%s.env'%slot,text)
            self.assertIn('existing_front.py '+slot,text)
            self.assertNotIn('PartOf=',text)
            self.assertNotIn('BindsTo=',text)
            self.assertNotIn('uav-front-cameras',text)

    def test_failed_migration_restores_original_files_and_state(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); stream=root/'stream'; units=root/'units'; stream.mkdir();units.mkdir()
            (stream/'run_algorithm_board.sh').write_text('original script')
            (units/m.OLD).write_text('original unit')
            import subprocess
            from contextlib import ExitStack
            calls=[]
            def ctl(*args,check=True):
                calls.append(args)
                if args==('enable','--now',m.unit('preview')):raise RuntimeError('injected')
                return subprocess.CompletedProcess(args,0,{'is-enabled':'enabled','is-active':'active'}.get(args[0],''),'')
            with ExitStack() as stack:
                for name,value in dict(ROOT=stream,UNITS=units,BASE=root/'base',CONFIG=root/'config',BACKUPS=root/'backups').items():stack.enter_context(patch.object(m,name,value))
                stack.enter_context(patch.object(m,'preflight'))
                stack.enter_context(patch.object(m,'ctl',side_effect=ctl))
                with self.assertRaisesRegex(RuntimeError,'injected'):m.migrate()
                self.assertEqual((stream/'run_algorithm_board.sh').read_text(),'original script')
                self.assertEqual((units/m.OLD).read_text(),'original unit')
                self.assertFalse(m.BASE.exists()); self.assertFalse(m.CONFIG.exists())
                self.assertIn(('start',m.OLD),calls)
                for slot in m.SLOTS:self.assertFalse((units/m.unit(slot)).exists())
