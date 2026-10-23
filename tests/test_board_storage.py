import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('board_storage_test', ROOT / 'common/board_storage.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [patch.object(m, 'SYSTEM_ROOT', self.root)]
        self.patches[0].start()
        for name in ('/data', '/var/log', '/etc/logrotate.d', '/usr/lib/rsyslog'):
            m.system(name).mkdir(parents=True, exist_ok=True)
        m.system('/usr/lib/rsyslog/rsyslog-rotate').write_text('#!/bin/sh\n')
        m.system('/var/log/syslog').write_text('original system log\n')
        m.system('/etc/logrotate.d/rsyslog').write_text('factory rotation\n')
        self.mounted = {str(m.system('/data'))}
        self.enabled = {'logrotate.timer'}
        self.commands = []

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def mock(self, target, *args, **kwargs):
        p = patch.object(m, target, *args, **kwargs)
        self.patches.append(p)
        return p.start()

    def runner(self, cmd, check=True, **kwargs):
        self.commands.append(cmd)
        rc = 0
        out = ''
        if cmd[:2] == ['systemctl', 'show']:
            out = 'LoadState=not-found\nActiveState=inactive\nUnitFileState=\n'
        if cmd[:2] == ['systemctl', 'is-enabled']:
            rc = 0 if cmd[2] in self.enabled else 1
        if cmd[:2] == ['systemctl', 'enable']:
            self.enabled.update(cmd[2:])
        if cmd[:2] == ['systemctl', 'disable']:
            self.enabled.difference_update(cmd[2:])
        if cmd[:3] == ['systemctl', 'start', 'var-log.mount']:
            self.mounted.add(str(m.system('/var/log')))
        if cmd[:3] == ['systemctl', 'stop', 'var-log.mount']:
            self.mounted.discard(str(m.system('/var/log')))
        if cmd[:3] == ['systemctl', 'start', 'data.mount']:
            self.mounted.add(str(m.system('/data')))
        if cmd[:3] == ['systemctl', 'stop', 'data.mount']:
            self.mounted.discard(str(m.system('/data')))
        if cmd[0] == 'rsync':
            shutil.copytree(Path(cmd[-2]), Path(cmd[-1]), dirs_exist_ok=True)
        return subprocess.CompletedProcess(cmd, rc, out, '')

    def prepare(self):
        self.mock('run', side_effect=self.runner)
        self.mock('_data_ready', return_value={'free_bytes': m.MIN_FREE * 2})
        self.mock('_existing_mount_persistence', return_value=None)
        self.mock('_existing_logs', return_value=None)
        p = patch.object(m.os.path, 'ismount', side_effect=lambda p: str(p) in self.mounted)
        p.start(); self.patches.append(p)
        p = patch.object(m.shutil, 'which', side_effect=lambda n: '/usr/bin/' + n)
        p.start(); self.patches.append(p)
        p = patch.object(m.os, 'geteuid', return_value=0)
        p.start(); self.patches.append(p)
        p = patch.object(m.os, 'chown')
        p.start(); self.patches.append(p)

    def test_no_data_mount_is_an_error_not_root_fallback(self):
        self.mounted.clear()
        with patch.object(m.os.path, 'ismount', return_value=False):
            with self.assertRaisesRegex(ValueError, '不会格式化'):
                m.preflight('main')
        self.assertEqual(list(m.system('/data').iterdir()), [])

    def test_main_cannot_use_other_directory(self):
        with self.assertRaisesRegex(ValueError, '主控使用 /data'):
            m.preflight('main', '/home/dev/data')

    def test_same_filesystem_rejected(self):
        with patch.object(m.os.path, 'ismount', return_value=True):
            with self.assertRaisesRegex(ValueError, '系统盘'):
                m._data_ready(m.system('/data'))

    def test_candidates_exclude_root_readonly_unformatted(self):
        rows = {'blockdevices': [dict(name='/dev/mmcblk0', type='disk', children=[
            dict(name='/dev/mmcblk0p5', type='part', fstype='ext4', uuid='root', mountpoints=['/'], ro=False),
            dict(name='/dev/mmcblk0p8', type='part', fstype=None, uuid=None, mountpoints=[None], ro=False),
            dict(name='/dev/mmcblk0p9', type='part', fstype='ext4', uuid='readonly', mountpoints=[None], ro=True),
            dict(name='/dev/mmcblk0p10', type='part', fstype='ext4', uuid='data', mountpoints=[None], ro=False),
        ])]}
        self.mock('run', return_value=subprocess.CompletedProcess([], 0, json.dumps(rows), ''))
        self.assertEqual([r['uuid'] for r in m.available_partitions()], ['data'])

    def test_invalid_uuid_rejected_before_discovery(self):
        with patch.object(m, 'available_partitions') as lookup:
            with self.assertRaisesRegex(ValueError, 'UUID 格式'):
                m._selected_partition('../../dev/mmcblk0')
            lookup.assert_not_called()

    def test_uuid_duplicated_with_root_partition_is_not_selectable(self):
        rows = {'blockdevices': [
            dict(name='/dev/a1', type='part', fstype='ext4', uuid='ABCD1234', mountpoints=['/'], ro=False),
            dict(name='/dev/b1', type='part', fstype='ext4', uuid='abcd1234', mountpoints=[None], ro=False),
        ]}
        self.mock('run', return_value=subprocess.CompletedProcess([], 0, json.dumps(rows), ''))
        self.assertEqual(m.available_partitions(), [])

    def test_data_mountpoint_with_files_not_covered(self):
        (m.system('/data') / 'user-file').write_text('keep')
        with patch.object(m.os.path, 'ismount', return_value=False), patch.object(m, '_selected_partition', return_value={'uuid': '12345678', 'device': '/dev/example'}):
            with self.assertRaisesRegex(ValueError, '不为空'):
                m.preflight('main', data_uuid='12345678')
        self.assertEqual((m.system('/data') / 'user-file').read_text(), 'keep')

    def test_fstab_owned_mountpoint_refused(self):
        m.system('/etc/fstab').write_text('UUID=other /data ext4 defaults 0 2\n')
        with patch.object(m.os.path, 'ismount', return_value=False), patch.object(m, '_selected_partition', return_value={'uuid': '12345678', 'device': '/dev/example'}):
            with self.assertRaisesRegex(ValueError, 'fstab'):
                m.preflight('main', data_uuid='12345678')

    def test_plan_does_not_mutate(self):
        self.prepare()
        plan = m.preflight('main')
        self.assertIsNone(plan['mount'])
        self.assertEqual(plan['log_root'], '/data/uav-system-logs/main')
        self.assertFalse(m.system(plan['log_root']).exists())
        self.assertFalse(any(c[:2] in (['systemctl', 'start'], ['systemctl', 'restart']) for c in self.commands))

    def test_symlink_log_cannot_escape_data_disk(self):
        self.prepare()
        m.system('/var/log/journal').symlink_to('/outside')
        with self.assertRaisesRegex(ValueError, '日志文件是符号链接'):
            m.preflight('main')

    def test_apply_and_rollback_preserve_old_logs_and_timer_state(self):
        self.prepare()
        self.mock('_verify_logging')
        plan = m.preflight('main')
        receipt = m.apply(plan)
        self.assertTrue(receipt['renamed_logs'])
        self.assertEqual(m.system(receipt['shadow'] + '/syslog').read_text(), 'original system log\n')
        self.assertEqual(m.system(receipt['log_root'] + '/syslog').read_text(), 'original system log\n')
        self.assertEqual(m.system(receipt['log_root'] + '/journal').stat().st_mode & 0o7777, 0o2755)
        self.assertIn('SystemMaxUse=512M', m.system('/etc/systemd/journald.conf.d/90-uav-data-log.conf').read_text())
        result = m.rollback(receipt)
        self.assertTrue(result['restored'], result)
        self.assertEqual(m.system('/var/log/syslog').read_text(), 'original system log\n')
        self.assertEqual(m.system('/etc/logrotate.d/rsyslog').read_text(), 'factory rotation\n')
        self.assertIn(['journalctl', '--flush'], self.commands)
        self.assertIn('logrotate.timer', self.enabled)
        self.assertNotIn('var-log.mount', self.enabled)
        self.assertTrue(m.system(receipt['log_root'] + '/syslog').exists())
        self.assertFalse(any('uav-switch' in ' '.join(cmd) or 'uav-front' in ' '.join(cmd) for cmd in self.commands))

    def test_copy_failure_rolls_back_config_without_renaming_logs(self):
        self.prepare()
        def fail(cmd, **kwargs):
            if cmd[0] == 'rsync':
                raise subprocess.CalledProcessError(1, cmd, stderr='copy failed')
            return self.runner(cmd, **kwargs)
        self.mock('run', side_effect=fail)
        with self.assertRaisesRegex(RuntimeError, 'copy failed|部署失败'):
            m.apply(m.preflight('main'))
        self.assertEqual(m.system('/var/log/syslog').read_text(), 'original system log\n')
        self.assertFalse(m.system('/etc/systemd/system/var-log.mount').exists())
        self.assertEqual(m.system('/etc/logrotate.d/rsyslog').read_text(), 'factory rotation\n')
        self.assertIn(['journalctl', '--flush'], self.commands)

    def test_failure_after_mount_recovers_original_log_tree(self):
        self.prepare()
        self.mock('_verify_logging', side_effect=RuntimeError('verification failed'))
        with self.assertRaisesRegex(RuntimeError, 'verification failed'):
            m.apply(m.preflight('main'))
        self.assertEqual(m.system('/var/log/syslog').read_text(), 'original system log\n')
        self.assertNotIn(str(m.system('/var/log')), self.mounted)

    def test_existing_logging_is_reused_without_commands(self):
        self.prepare()
        plan = m.preflight('main')
        plan['existing_logs'] = {'reused': True, 'root': '/data/uav-system-logs/main'}
        self.mock('preflight', return_value=plan)
        self.commands.clear()
        result = m.apply(plan)
        self.assertTrue(result['reused'])
        self.assertFalse(self.commands)

    def test_rollback_does_not_overwrite_a_later_configuration_change(self):
        self.prepare()
        self.mock('_verify_logging')
        receipt = m.apply(m.preflight('main'))
        p = m.system('/etc/logrotate.d/rsyslog')
        p.write_text('operator changed this')
        result = m.rollback(receipt)
        self.assertFalse(result['restored'])
        self.assertEqual(p.read_text(), 'operator changed this')

    def test_explicit_uuid_mount_applied_and_removed_by_rollback(self):
        self.prepare()
        self.mounted.clear()
        self.mock('_selected_partition', return_value={'uuid': '12345678', 'device': '/dev/example', 'fstype': 'ext4'})
        self.mock('_verify_logging')
        plan = m.preflight('main', data_uuid='12345678')
        self.assertIn('What=/dev/disk/by-uuid/12345678', plan['mount']['content'])
        receipt = m.apply(plan)
        self.assertTrue(receipt['mounted_data'])
        self.assertIn('data.mount', self.enabled)
        self.assertFalse(m.system('/etc/fstab').exists())
        self.assertFalse(any(cmd[0].startswith(('mkfs', 'fdisk', 'wipefs')) for cmd in self.commands))
        result = m.rollback(receipt)
        self.assertTrue(result['restored'], result)
        self.assertNotIn(str(m.system('/data')), self.mounted)
        self.assertFalse(m.system('/etc/systemd/system/data.mount').exists())
        self.assertTrue(m.system(receipt['log_root'] + '/syslog').exists())

    def test_uuid_disk_too_full_after_mount_unmounts_without_log_changes(self):
        self.prepare()
        self.mounted.clear()
        self.mock('_selected_partition', return_value={'uuid': '12345678', 'device': '/dev/example', 'fstype': 'ext4'})
        plan = m.preflight('main', data_uuid='12345678')
        self.mock('_data_ready', side_effect=ValueError('数据盘可用空间不足 10 GiB'))
        with self.assertRaisesRegex(RuntimeError, '10 GiB'):
            m.apply(plan)
        self.assertNotIn(str(m.system('/data')), self.mounted)
        self.assertFalse(m.system('/etc/systemd/system/data.mount').exists())
        self.assertEqual(m.system('/var/log/syslog').read_text(), 'original system log\n')

    def persistence_runner(self, cmd, check=True, **kwargs):
        if cmd[0] == 'findmnt':
            return subprocess.CompletedProcess(cmd, 0, '12345678-abcd\n', '')
        if cmd[0] == 'lsblk':
            return subprocess.CompletedProcess(cmd, 0, json.dumps({'blockdevices': [dict(name='/dev/testdata', uuid='12345678-abcd')]}), '')
        return self.runner(cmd, check=check, **kwargs)

    def test_existing_data_mount_without_permanent_config_gets_uuid_unit(self):
        self.mock('run', side_effect=self.persistence_runner)
        ready = {'mount': {'source': '/dev/testdata', 'fstype': 'ext4'}}
        plan = m._existing_mount_persistence('/data', ready)
        self.assertEqual(plan['unit'], 'data.mount')
        self.assertTrue(plan['create'])
        self.assertIn('What=/dev/disk/by-uuid/12345678-abcd', plan['content'])
        self.assertFalse(m.system('/etc/systemd/system/data.mount').exists())

    def test_existing_fstab_matching_actual_disk_is_reused(self):
        self.mock('run', side_effect=self.persistence_runner)
        m.system('/etc/fstab').write_text('UUID=12345678-abcd /data ext4 defaults,nofail 0 2\n')
        self.assertIsNone(m._existing_mount_persistence('/data', {'mount': {'source': '/dev/testdata', 'fstype': 'ext4'}}))

    def test_existing_fstab_wrong_disk_or_noauto_rejected(self):
        self.mock('run', side_effect=self.persistence_runner)
        for line in ('UUID=87654321 /data ext4 defaults 0 2\n', 'UUID=12345678-abcd /data ext4 noauto 0 2\n'):
            m.system('/etc/fstab').write_text(line)
            with self.assertRaisesRegex(ValueError, 'fstab'):
                m._existing_mount_persistence('/data', {'mount': {'source': '/dev/testdata', 'fstype': 'ext4'}})

    def test_add_persistence_to_existing_mount_never_remounts_or_unmounts_it(self):
        self.prepare()
        _, content = m._mount_unit('/data', '12345678-abcd')
        change = dict(unit='data.mount', content=content, create=True, enable=True)
        self.mock('_existing_mount_persistence', return_value=change)
        self.mock('_existing_logs', return_value={'reused': True, 'root': '/data/uav-system-logs/main'})
        plan = m.preflight('main')
        receipt = m.apply(plan)
        self.assertIn('data.mount', self.enabled)
        self.assertFalse(receipt['mounted_data'])
        result = m.rollback(receipt)
        self.assertTrue(result['restored'], result)
        self.assertIn(str(m.system('/data')), self.mounted)
        self.assertFalse(m.system('/etc/systemd/system/data.mount').exists())
        self.assertFalse(any(cmd[:3] in (['systemctl', 'start', 'data.mount'], ['systemctl', 'stop', 'data.mount']) for cmd in self.commands))

    def test_open_old_text_logs_get_copytruncate_archive_and_binary_is_reported(self):
        self.prepare()
        shadow = m.system('/var/log.uav-before-data_test')
        (shadow / 'desktop').mkdir(parents=True)
        (shadow / 'app').mkdir()
        files = [shadow/'desktop/app.log', shadow/'app/app.log', shadow/'wtmp']
        for p in files[:2]: p.write_text('old text log\n')
        files[2].write_bytes(b'\0binary')
        fds = m.system('/proc/123/fd'); fds.mkdir(parents=True)
        for index, p in enumerate(files): (fds/str(index)).symlink_to(p)
        backup = self.root/'backup'; backup.mkdir()
        receipt = dict(shadow='/var/log.uav-before-data_test', log_root='/data/uav-system-logs/main', backup=str(backup), changed_files=[])
        m._root_log_residue(receipt)
        report = receipt['root_log_residue']
        self.assertEqual(sum(r['managed'] for r in report), 2)
        archives = [r['archive'] for r in report if r['managed']]
        self.assertEqual(len(set(archives)), 2)
        config = m.system('/etc/logrotate.d/uav-root-log-residue').read_text()
        self.assertEqual(config.count('copytruncate'), 2)
        self.assertEqual(config.count('size 1M'), 2)
        self.assertEqual(config.count('rotate 2'), 2)
        self.assertEqual(files[0].read_text(), 'old text log\n')
        self.assertEqual(files[2].read_bytes(), b'\0binary')
        self.assertTrue(any(r['path'].endswith('/wtmp') and not r['managed'] for r in report))

    def test_busy_varlog_rollback_preserves_configuration_and_restarts_logging(self):
        self.prepare()
        self.mock('_verify_logging')
        receipt = m.apply(m.preflight('main'))
        def busy(cmd, **kwargs):
            if cmd[:3] == ['systemctl', 'stop', 'var-log.mount']:
                self.commands.append(cmd)
                return subprocess.CompletedProcess(cmd, 1, '', 'busy')
            return self.runner(cmd, **kwargs)
        self.mock('run', side_effect=busy)
        result = m.rollback(receipt)
        self.assertFalse(result['restored'])
        self.assertTrue(m.system('/etc/systemd/system/var-log.mount').exists())
        self.assertTrue(m.system(receipt['shadow'] + '/syslog').exists())
        self.assertIn(['systemctl', 'restart', 'rsyslog.service'], self.commands)


if __name__ == '__main__':
    unittest.main()
