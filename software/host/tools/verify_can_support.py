#!/usr/bin/env python3
"""Verify CAN policy, helper request and launcher isolation with fake devices only."""
from __future__ import annotations
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--runtime-dir', type=Path, default=SOURCE / '.local-runtime')
args = parser.parse_args()
runtime = args.runtime_dir.resolve(strict=True)
if os.environ.get('RP1_CAN_VERIFY_BOOTSTRAPPED') != '1':
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(SOURCE), str(runtime)]),
               LD_LIBRARY_PATH=str(runtime), RP1_CAN_VERIFY_BOOTSTRAPPED='1')
    os.execve(sys.executable, [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]], env)

import sysconfig
sys.path.insert(1, sysconfig.get_path('stdlib'))
import xml.etree.ElementTree as ET

from factory_hmi.can_helper import CanPermissionError, execute_request, load_policy
from factory_hmi.core.can_manager import CanInterfaceStatus, HelperExecutionError, PkexecHelperClient

spec = importlib.util.spec_from_file_location('offline_launcher', SOURCE / 'launch.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class FakeAdapter:
    def __init__(self):
        self.calls = []
        self.fail = None
        self.up = set()

    def connect(self, interface, **timing):
        self.calls.append(('connect', interface, timing))
        if interface == self.fail:
            raise RuntimeError('injected failure')
        self.up.add(interface)

    def disconnect(self, interface):
        self.calls.append(('disconnect', interface))
        self.up.discard(interface)

    def get_status(self, interface):
        return CanInterfaceStatus(interface, True, interface in self.up, 'up', 1000000,
                                  5000000, True, 'error-active', 0, 0)


class CanTests(unittest.TestCase):
    def setUp(self):
        self.policy = load_policy(SOURCE / 'packaging/physical-can-policy.yaml', require_root_owned=False)
        self.request = dict(action='connect', interfaces=['can0'], bitrate=1000000, dbitrate=5000000, fd=True)

    def test_real_launcher_enables_fixed_root_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            env = launcher.gateway_environment(Path(directory), runtime, offline=False, driver='modbus_tcp', mode='control')
        self.assertEqual(env['RP1_FACTORY_CAN_DISABLED'], '0')
        self.assertEqual(env['RP1_FACTORY_CAN_HELPER_PATH'], '/usr/lib/rp1-test-hmi/factory-hmi-can-helper')
        self.assertEqual(env['RP1_FACTORY_CAN_POLICY'], '/etc/rp1-test-hmi/can-policy.yaml')

    def test_offline_launcher_remains_disabled_even_with_host_override(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'RP1_FACTORY_CAN_DISABLED': '0'}):
            env = launcher.gateway_environment(Path(directory), runtime, offline=True, driver='mock', mode='control')
        self.assertEqual(env['RP1_FACTORY_CAN_DISABLED'], '1')
        with patch.dict(os.environ, env), patch('subprocess.run') as run:
            with self.assertRaises(HelperExecutionError):
                PkexecHelperClient().execute('connect', ['can0'], bitrate=1000000, dbitrate=5000000, fd=True)
            run.assert_not_called()

    def test_helper_policy_only_allows_adapter_can_channels(self):
        self.assertEqual(self.policy.allowed_interfaces, frozenset(['can0', 'can1', 'can2', 'can3']))
        for interface in ['can4', 'can99', 'lin0']:
            adapter = FakeAdapter()
            with self.assertRaises((CanPermissionError, ValueError)):
                execute_request(dict(self.request, interfaces=[interface]), policy=self.policy, adapter=adapter)
            self.assertEqual(adapter.calls, [])

    def test_can_fd_connection_and_partial_failure_rollback(self):
        adapter = FakeAdapter()
        result = execute_request(self.request, policy=self.policy, adapter=adapter)
        self.assertTrue(result['ok'])
        self.assertTrue(result['status'][0]['up'])
        self.assertEqual(adapter.calls, [('connect', 'can0', dict(bitrate=1000000, dbitrate=5000000, fd=True))])
        adapter = FakeAdapter()
        adapter.fail = 'can1'
        with self.assertRaises(RuntimeError):
            execute_request(dict(self.request, interfaces=['can0', 'can1']), policy=self.policy, adapter=adapter)
        self.assertFalse(adapter.up)
        self.assertEqual(adapter.calls[-1], ('disconnect', 'can0'))

    def test_invalid_rates_and_unknown_fields_are_rejected_before_adapter(self):
        for changes in [dict(bitrate=123), dict(dbitrate=999), dict(shell='anything')]:
            adapter = FakeAdapter()
            with self.assertRaises(ValueError):
                execute_request(dict(self.request, **changes), policy=self.policy, adapter=adapter)
            self.assertEqual(adapter.calls, [])

    def test_enabled_client_uses_fixed_argv_and_bounded_json_request(self):
        completed = subprocess.CompletedProcess([], 0, '{"ok":true}', '')
        with patch.dict(os.environ, {'RP1_FACTORY_CAN_DISABLED': '0'}), patch('subprocess.run', return_value=completed) as run:
            client = PkexecHelperClient(helper_path='/usr/lib/rp1-test-hmi/factory-hmi-can-helper')
            client.execute('connect', ['can0'], bitrate=1000000, dbitrate=5000000, fd=True)
        self.assertEqual(run.call_args.args[0], ['/usr/bin/pkexec', '/usr/lib/rp1-test-hmi/factory-hmi-can-helper'])
        self.assertEqual(json.loads(run.call_args.kwargs['input']), self.request)
        self.assertNotIn('shell', run.call_args.kwargs)

    def test_polkit_requires_admin_and_wrapper_uses_isolated_fixed_policy(self):
        action = ET.parse(SOURCE / 'packaging/com.rp1.test-hmi.can.policy').getroot().find('action')
        self.assertEqual(action.find('defaults/allow_active').text, 'auth_admin_keep')
        self.assertEqual(action.find('defaults/allow_any').text, 'no')
        self.assertEqual(action.find('defaults/allow_inactive').text, 'no')
        wrapper = (SOURCE / 'packaging/can-helper').read_text()
        self.assertTrue(wrapper.startswith('#!/usr/bin/python3 -I\n'))
        self.assertIn("main(policy_path='/etc/rp1-test-hmi/can-policy.yaml')", wrapper)

    def test_desktop_name_has_no_suffix(self):
        entry = (SOURCE / 'packaging/rp1-test-hmi.desktop').read_text().splitlines()
        self.assertIn('Name=rp1-test-hmi', entry)
        self.assertIn('Name[zh_CN]=rp1-test-hmi', entry)
        self.assertIn('Exec=rp1-test-hmi', entry)


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0]], verbosity=2)
