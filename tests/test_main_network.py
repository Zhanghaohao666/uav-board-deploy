import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('main_network_test', ROOT / 'common/main_network.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
C = dict(payload_iface='eth0', mk22_iface='eth2', extension_iface='eth3', default_mode='algorithm')


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.patch = patch.multiple(m, CONF=root/'nm.conf', IFUPDOWN=root/'interfaces', IFUPDOWN_DIR=root/'interfaces.d')
        self.patch.start()
        self.infos = [dict(ifname='eth0', ifindex=2, addr_info=[dict(local='192.168.10.1')]),
                      dict(ifname='eth2', ifindex=3, addr_info=[dict(local='192.168.2.36')]),
                      dict(ifname='wlan0', ifindex=4, addr_info=[dict(local='192.168.1.104')])]
        self.sockets = ''
        self.routes = [dict(dev='wlan0')]
        self.ss_rc = 0
        self.commands = []

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def runner(self, cmd, check=True):
        self.commands.append(cmd)
        rc = 0; out = ''
        if cmd == ['ip', '-j', 'address', 'show']: out = json.dumps(self.infos)
        elif cmd[:5] == ['ip', '-j', 'route', 'show', 'default']: out = json.dumps(self.routes)
        elif cmd[0] == 'ss': rc = self.ss_rc; out = self.sockets
        elif cmd[:2] == ['systemctl', 'is-active']:
            rc = 1 if cmd[2] == 'systemd-networkd.service' else 0
        elif cmd[:3] == ['nmcli', '-g', 'GENERAL.NM-MANAGED']: out = 'yes\n'
        return subprocess.CompletedProcess(cmd, rc, out, '')

    def preflight(self):
        with patch.object(m, 'run', side_effect=self.runner):
            return m.preflight(C, ssh_connection='')

    def test_sudo_missing_env_protects_ssh_on_selected_interface(self):
        self.sockets = '0 0 192.168.10.1:22 192.168.10.9:51432 users:(("sshd",pid=100,fd=4))\n'
        with self.assertRaisesRegex(ValueError, 'SSH正在使用'):
            self.preflight()
        self.assertFalse(m.CONF.exists())

    def test_nonstandard_sshd_port_is_also_protected(self):
        self.sockets = '0 0 192.168.2.36:2222 192.168.2.50:51432 users:(("sshd-session",pid=100,fd=4))\n'
        with self.assertRaisesRegex(ValueError, 'SSH正在使用'):
            self.preflight()

    def test_port22_without_process_permissions_is_protected(self):
        self.sockets = '0 0 192.168.10.1:22 192.168.10.9:51432\n'
        with self.assertRaisesRegex(ValueError, 'SSH正在使用'):
            self.preflight()

    def test_wifi_ssh_allows_dedicated_interfaces(self):
        self.sockets = '0 0 192.168.1.104:22 192.168.1.9:51432 users:(("sshd",pid=100,fd=4))\n'
        plan = self.preflight()
        self.assertEqual(plan['names'], ['eth0', 'eth2'])
        self.assertFalse(m.CONF.exists())

    def test_ipv4_mapped_ipv6_socket_matches_interface(self):
        self.sockets = 'ESTAB 0 0 [::ffff:192.168.10.1]:22 [::ffff:192.168.10.9]:51432 users:(("sshd",pid=100,fd=4))\n'
        with self.assertRaisesRegex(ValueError, 'SSH正在使用'):
            self.preflight()

    def test_ipv6_zone_is_normalized(self):
        self.infos[0]['addr_info'].append(dict(local='fe80::123'))
        self.sockets = '0 0 [fe80::123%eth0]:22 [fe80::456%eth0]:51432 users:(("sshd",pid=100,fd=4))\n'
        with self.assertRaisesRegex(ValueError, 'SSH正在使用'):
            self.preflight()

    def test_ss_failure_is_fail_closed(self):
        self.ss_rc = 1
        with self.assertRaisesRegex(ValueError, '无法检查 SSH'):
            self.preflight()

    def test_bridge_member_ssh_is_protected(self):
        self.infos[0]['master'] = 'br0'
        self.infos.append(dict(ifname='br0', ifindex=5, addr_info=[dict(local='192.168.4.1')]))
        self.sockets = '0 0 192.168.4.1:22 192.168.4.9:51432 users:(("sshd",pid=100,fd=4))\n'
        with self.assertRaisesRegex(ValueError, 'SSH正在使用'):
            self.preflight()

    def test_bridge_default_route_is_protected(self):
        self.infos[0]['master'] = 5
        self.infos.append(dict(ifname='br0', ifindex=5, addr_info=[dict(local='192.168.4.1')]))
        self.routes = [dict(dev='br0')]
        with self.assertRaisesRegex(ValueError, '默认上网路由'):
            self.preflight()

    def test_new_ssh_between_plan_and_apply_is_rejected(self):
        plan = self.preflight()
        self.sockets = '0 0 192.168.10.1:22 192.168.10.9:51432 users:(("sshd",pid=100,fd=4))\n'
        with patch.object(m, 'run', side_effect=self.runner), patch.dict(m.os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, 'SSH已开始使用'):
                m.apply(plan)
        self.assertFalse(m.CONF.exists())
        self.assertFalse(any(cmd[:3] == ['nmcli', 'device', 'set'] for cmd in self.commands))
        self.assertFalse(any(cmd[:3] == ['nmcli', 'general', 'reload'] for cmd in self.commands))

    def test_env_remains_an_additional_protection(self):
        with patch.object(m, 'run', side_effect=self.runner):
            with self.assertRaisesRegex(ValueError, 'SSH正在使用'):
                m.preflight(C, ssh_connection='192.168.10.9 50000 192.168.10.1 22')

    def test_non_ssh_connection_does_not_block(self):
        self.sockets = '0 0 192.168.2.36:9072 192.168.2.50:51432 users:(("python3",pid=100,fd=4))\n'
        self.assertEqual(self.preflight()['names'], ['eth0', 'eth2'])

    def test_default_route_changed_after_plan_is_rejected_without_mutation(self):
        plan = self.preflight()
        self.routes = [dict(dev='eth2')]
        with patch.object(m, 'run', side_effect=self.runner):
            with self.assertRaisesRegex(ValueError, '默认路由'):
                m.apply(plan)
        self.assertFalse(m.CONF.exists())
        self.assertFalse(any(cmd[:3] == ['nmcli', 'general', 'reload'] for cmd in self.commands))

    def test_apply_and_rollback_preserve_initial_managed_interfaces(self):
        plan = self.preflight()
        with patch.object(m, 'run', side_effect=self.runner), patch.dict(m.os.environ, {}, clear=True):
            receipt = m.apply(plan)
            self.assertTrue(m.CONF.is_file())
            m.rollback(receipt)
        self.assertFalse(m.CONF.exists())
        for name in ('eth0', 'eth2'):
            self.assertIn(['nmcli', 'device', 'set', name, 'managed', 'no'], self.commands)
            self.assertIn(['nmcli', 'device', 'set', name, 'managed', 'yes'], self.commands)

    def test_failure_during_reload_restores_managed_state(self):
        plan = self.preflight()
        reloads = 0
        def fail_first_reload(cmd, check=True):
            nonlocal reloads
            if cmd[:3] == ['nmcli', 'general', 'reload']:
                reloads += 1
                if reloads == 1: raise subprocess.CalledProcessError(1, cmd)
            return self.runner(cmd, check)
        with patch.object(m, 'run', side_effect=fail_first_reload), patch.dict(m.os.environ, {}, clear=True):
            with self.assertRaises(subprocess.CalledProcessError): m.apply(plan)
        self.assertFalse(m.CONF.exists())
        for name in ('eth0', 'eth2'):
            self.assertIn(['nmcli', 'device', 'set', name, 'managed', 'yes'], self.commands)

    def test_no_start_only_writes_persistent_config_and_rollback_does_not_reload(self):
        plan = self.preflight()
        with patch.object(m, 'run', side_effect=self.runner), patch.dict(m.os.environ, {}, clear=True):
            receipt = m.apply(plan, start=False)
            self.assertTrue(m.CONF.is_file())
            self.assertFalse(receipt['reloaded'])
            self.assertEqual(receipt['changed'], [])
            m.rollback(receipt)
        self.assertFalse(m.CONF.exists())
        self.assertFalse(any(cmd[0] == 'nmcli' and cmd[1] in ('device', 'general') for cmd in self.commands))


if __name__ == '__main__':
    unittest.main()
