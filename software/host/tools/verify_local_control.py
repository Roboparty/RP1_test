#!/usr/bin/env python3
"""Offline control, protocol and relative-YAML verification; never connects to real PLC."""
from __future__ import annotations

import argparse
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

SOURCE = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--runtime-dir", type=Path, default=SOURCE / ".local-runtime")
args = parser.parse_args()
runtime = args.runtime_dir.resolve(strict=True)
if os.environ.get("RP1_LOCAL_VERIFY_BOOTSTRAPPED") != "1":
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(SOURCE), str(runtime)]),
               LD_LIBRARY_PATH=str(runtime), QT_QPA_PLATFORM="offscreen",
               QT_QPA_PLATFORM_PLUGIN_PATH=str(runtime / "PySide6/Qt/plugins"),
               RP1_LOCAL_VERIFY_BOOTSTRAPPED="1")
    os.execve(sys.executable, [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]], env)

from PySide6.QtWidgets import QApplication
from factory_hmi.client import GatewayClient, GatewayHTTPError
from factory_hmi.core.can_manager import HelperExecutionError, PkexecHelperClient
from factory_hmi.desktop.plc_page import PlcCabinetPage
from factory_hmi.plc.client import MockPlcClient, ModbusTcpPlcClient, PlcCommunicationError
from factory_hmi.plc.config import load_plc_config
from factory_hmi.plc.models import CabinetPhase, PlcCommand
from factory_hmi.plc.service import PlcCabinetService, PlcControlDisabledError


class Response:
    def __init__(self, registers=None):
        self.registers = registers or []

    def isError(self):
        return False


class FakeTransport:
    def __init__(self):
        self.registers = [0] * 64
        self.registers[:3] = [0x4C46, 0x0100, 1]
        self.writes = []

    def connect(self):
        return True

    def close(self):
        pass

    def read_holding_registers(self, address, *, count, slave):
        assert slave == 1
        return Response(self.registers[address:address + count])

    def write_registers(self, address, values, *, slave):
        self.writes.append((16, address, list(values), slave))
        return Response()

    def write_register(self, address, value, *, slave):
        self.writes.append((6, address, value, slave))
        return Response()


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.config = load_plc_config(SOURCE / "factory_hmi/config/plc_cabinet.yaml")

    def test_real_control_profile_and_fake_modbus_transaction_contract(self):
        self.assertEqual((self.config.driver, self.config.access_mode), ("modbus_tcp", "control"))
        self.assertEqual((self.config.protocol['host'], self.config.protocol['port'], self.config.protocol['unit_id']), ('192.168.137.10', 502, 1))
        transport = FakeTransport()
        client = ModbusTcpPlcClient(self.config.protocol, transport_factory=lambda *a, **k: transport)
        client.connect()
        client.write_command(PlcCommand(heartbeat_counter=7, command_sequence=1, all_stop=True, set_voltage=3.0, set_current=.5))
        self.assertEqual(transport.writes, [(16, 32, [0x835A, 7], 1), (16, 35, [64, 3000, 500], 1), (6, 34, 1, 1)])
        client.disconnect()

    def test_identity_mismatch_is_rejected_before_any_write(self):
        for magic, version in [(0, 0x0100), (0x4C46, 0x0200)]:
            transport = FakeTransport()
            transport.registers[:2] = [magic, version]
            client = ModbusTcpPlcClient(self.config.protocol, transport_factory=lambda *a, **k: transport)
            with self.assertRaises(PlcCommunicationError):
                client.connect()
            self.assertFalse(client.connected)
            self.assertEqual(transport.writes, [])

    def test_control_service_and_gui_enable_controls_with_mock_feedback(self):
        client = MockPlcClient()
        with tempfile.TemporaryDirectory() as directory, patch('factory_hmi.plc.service.build_plc_client', return_value=client):
            service = PlcCabinetService(self.config, data_root=directory)
            try:
                service.start()
                snapshot = service.snapshot()
                self.assertFalse(snapshot['read_only'])
                self.assertEqual(snapshot['communication_state'], 'LIVE')
                page = PlcCabinetPage(GatewayClient('http://127.0.0.1:1'), lambda *a, **k: None, lambda: 'offline')
                page.stop()
                page._snapshot = snapshot
                page._render()
                self.assertIn('控制模式', page.communication.text())
                self.assertTrue(page.start_button.isEnabled())
                self.assertTrue(page.apply_setpoints_button.isEnabled())
                page.close()
                service.start_sequence([1, 2], user='offline', voltage=3.0, current=.5)
                for _ in range(20):
                    with service._lock:
                        service.controller.tick()
                    if service.controller.phase == CabinetPhase.RUNNING:
                        break
                self.assertEqual(service.controller.phase, CabinetPhase.RUNNING)
                self.assertEqual(service.controller.status.channel_permit, (True, True, False, False))
                service.stop_sequence(user='offline')
                for _ in range(20):
                    with service._lock:
                        service.controller.tick()
                    if service.controller.phase == CabinetPhase.OFF:
                        break
                self.assertEqual(service.controller.phase, CabinetPhase.OFF)
            finally:
                service.close()

    def test_monitor_profile_keeps_no_write_guarantee(self):
        transport = FakeTransport()
        client = ModbusTcpPlcClient(self.config.protocol, transport_factory=lambda *a, **k: transport)
        with tempfile.TemporaryDirectory() as directory, patch('factory_hmi.plc.service.build_plc_client', return_value=client):
            service = PlcCabinetService(replace(self.config, access_mode='monitor'), data_root=directory)
            service.start()
            self.assertEqual(service.snapshot()['phase'], 'MONITORING')
            with self.assertRaises(PlcControlDisabledError):
                service.start_sequence([1], user='offline')
            service.close()
        self.assertEqual(transport.writes, [])

    def test_can_guard_rejects_before_helper_subprocess(self):
        with patch.dict(os.environ, {'RP1_FACTORY_CAN_DISABLED': '1'}), patch('subprocess.run') as run:
            with self.assertRaises(HelperExecutionError):
                PkexecHelperClient().execute('connect', ['can0'], bitrate=500000, dbitrate=5000000, fd=True)
            run.assert_not_called()

    def test_existing_mock_interlock_regressions(self):
        path = SOURCE / 'tests/test_plc_cabinet.py'
        spec = importlib.util.spec_from_file_location('offline_plc_regressions', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        tests = [value for name, value in vars(module).items() if name.startswith('test_') and callable(value)]
        self.assertEqual(len(tests), 17)
        for test in tests:
            with self.subTest(test=test.__name__):
                test()

    def test_isolated_gateway_control_and_relative_yaml(self):
        for mode in ['control', 'monitor']:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(prefix='rp1-local-control-') as directory:
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1', 0))
                    port = sock.getsockname()[1]
                url = f'http://127.0.0.1:{port}'
                log_path = Path(directory) / 'launcher.log'
                with log_path.open('w+') as log:
                    process = subprocess.Popen([sys.executable, str(SOURCE / 'launch.py'), '--runtime-dir', str(runtime), '--state-dir', directory, '--offline-check', '--offline-access', mode, '--gateway-only', '--port', str(port)], stdout=log, stderr=subprocess.STDOUT)
                    def request(path, body=None):
                        req = urllib.request.Request(url + path, data=None if body is None else json.dumps(body).encode(), headers={'Content-Type': 'application/json', 'X-RP1-Client-ID': 'offline-verification'})
                        try:
                            with urllib.request.urlopen(req, timeout=3) as response:
                                return response.status, json.load(response)
                        except urllib.error.HTTPError as error:
                            return error.code, json.load(error)
                    try:
                        for _ in range(150):
                            try:
                                _, snapshot = request('/api/v1/plc/snapshot')
                                break
                            except OSError:
                                if process.poll() is not None:
                                    log.seek(0)
                                    self.fail(log.read()[-5000:])
                                time.sleep(.1)
                        else:
                            self.fail('Isolated gateway failed to start')
                        self.assertEqual((snapshot['driver'], snapshot['access_mode']), ('mock', mode))
                        status, preview = request('/api/v1/config/preview', {'config_path': 'left_arm_motors.yaml', 'limb': 'left_arm'})
                        self.assertEqual(status, 200, preview)
                        self.assertEqual(preview['result']['config_path'], 'left_arm_motors.yaml')
                        _, profiles = request('/api/v1/configs')
                        self.assertTrue(profiles['items'])
                        self.assertTrue(all(not Path(p['path']).is_absolute() for p in profiles['items']))
                        status, configured = request('/api/v1/configure', {'config_path': 'left_arm_motors.yaml', 'limb': 'left_arm', 'backend': 'fake'})
                        self.assertEqual(status, 200, configured)
                        _, active = request('/api/v1/snapshot')
                        self.assertEqual(active['config_path'], 'left_arm_motors.yaml')
                        status, _ = request('/api/v1/config/preview', {'config_path': 'missing.yaml', 'limb': 'left_arm'})
                        self.assertEqual(status, 404)
                        status, _ = request('/api/v1/config/preview', {'config_path': '../../../../etc/passwd', 'limb': 'left_arm'})
                        self.assertNotEqual(status, 200)
                        client = GatewayClient(url, client_id='offline-verification')
                        client.plc_snapshot()
                        if mode == 'monitor':
                            with self.assertRaises(GatewayHTTPError) as rejected:
                                client.plc_start([1], 'offline', 3.0, .5)
                            self.assertEqual(rejected.exception.status_code, 403)
                        else:
                            # Old callers must not bypass the shared command context.
                            status, response = request('/api/v1/plc/start', {'channels': [1], 'voltage': 3.0, 'current': .5, 'user': 'offline'})
                            self.assertEqual(status, 409, response)
                            client.plc_snapshot()
                            response = client.plc_start([1], 'offline', 3.0, .5)
                            self.assertTrue(response['ok'], response)
                            for _ in range(50):
                                _, snapshot = request('/api/v1/plc/snapshot')
                                if snapshot['phase'] == 'RUNNING':
                                    break
                                time.sleep(.1)
                            self.assertEqual(snapshot['phase'], 'RUNNING', snapshot)
                            client.plc_snapshot()
                            response = client.plc_stop('offline')
                            self.assertTrue(response['ok'], response)
                    finally:
                        process.terminate()
                        process.wait(timeout=12)


if __name__ == '__main__':
    app = QApplication([])
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ControlTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
