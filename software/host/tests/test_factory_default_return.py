from __future__ import annotations

import concurrent.futures
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from fastapi.testclient import TestClient

from factory_hmi import FakeMotorBackend, FactoryController, SafetyLimits, StationConfig, StationState
from factory_hmi.gateway.app import create_app


def controller_for(backend: FakeMotorBackend, rate: float = 100.0) -> FactoryController:
    config = StationConfig(
        limb="left_arm", entries=backend.entries, raw={},
        control_rate_hz=rate, safety_limits=SafetyLimits(),
    )
    return FactoryController(config, backend, enable_watchdog=False)


class VirtualClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def sleep(self, duration: float) -> None:
        self.now += duration


class DefaultReturnTests(unittest.TestCase):
    def test_gain_timing_and_smooth_approach_preserve_healthy_connection(self) -> None:
        backend = FakeMotorBackend(2, initial_positions=[0.4, -0.3])
        controller = controller_for(backend, rate=20.0)
        clock = VirtualClock()
        with (
            mock.patch.object(controller, "_start_position_hold") as hold,
            mock.patch("factory_hmi.core.controller.time.monotonic", side_effect=lambda: clock.now),
            mock.patch("factory_hmi.core.controller.time.sleep", side_effect=clock.sleep),
            mock.patch("factory_hmi.core.controller.motor_default_positions", return_value=np.array([0.1, -0.1])),
        ):
            controller.connect()
            with mock.patch.object(backend, "connect", wraps=backend.connect) as connect:
                controller.reset(to_default=True)
            connect.assert_not_called()
            commands = backend.command_history
            self.assertEqual(len(commands), 60)
            self.assertEqual([x["kp_scale"] for x in commands[:20]], [0.5] * 20)
            self.assertEqual([x["kp_scale"] for x in commands[20:]], [1.0] * 40)
            self.assertTrue(all(x["kd_scale"] == 1.0 for x in commands))
            self.assertAlmostEqual(commands[20]["timestamp_s"] - commands[0]["timestamp_s"], 1.0)
            self.assertAlmostEqual(clock.now - commands[0]["timestamp_s"], 3.0)
            self.assertGreater(commands[0]["target"][0], 0.1)
            np.testing.assert_allclose(commands[19]["target"], [0.1, -0.1])
            np.testing.assert_allclose(commands[-1]["target"], [0.1, -0.1])
            np.testing.assert_allclose(hold.call_args.args[0], [0.1, -0.1])
            self.assertTrue(backend.motors_enabled)
            controller.disconnect()

    def test_mid_return_fault_stops_commands_and_does_not_restore_hold(self) -> None:
        class FaultDuringMove(FakeMotorBackend):
            def _command_mit_impl(self, *args, **kwargs):
                super()._command_mit_impl(*args, **kwargs)
                if len(self.command_history) == 10:
                    self.inject_error(1, 7)

        backend = FaultDuringMove(2)
        controller = controller_for(backend)
        clock = VirtualClock()
        with (
            mock.patch.object(controller, "_start_position_hold") as hold,
            mock.patch("factory_hmi.core.controller.time.monotonic", side_effect=lambda: clock.now),
            mock.patch("factory_hmi.core.controller.time.sleep", side_effect=clock.sleep),
            mock.patch("factory_hmi.core.controller.motor_default_positions", return_value=np.array([0.2, -0.2])),
        ):
            controller.connect()
            hold.reset_mock()
            with self.assertRaisesRegex(RuntimeError, "feedback error.*id=2:error_id=7"):
                controller.reset(to_default=True)
            self.assertLessEqual(len(backend.command_history), 16)
            self.assertFalse(backend.motors_enabled)
            self.assertEqual(controller.state, StationState.FAULT)
            hold.assert_not_called()
            self.assertEqual(controller.snapshot()["fault"]["diagnoses"][0]["rule"], "motor_error")
            controller.disconnect()

    def test_stale_feedback_during_return_is_rejected(self) -> None:
        backend = FakeMotorBackend(2)
        controller = controller_for(backend)
        clock = VirtualClock()
        with (
            mock.patch.object(controller, "_start_position_hold"),
            mock.patch("factory_hmi.core.controller.time.monotonic", side_effect=lambda: clock.now),
            mock.patch("factory_hmi.core.controller.time.sleep", side_effect=clock.sleep),
            mock.patch("factory_hmi.core.controller.motor_default_positions", return_value=np.array([0.2, -0.2])),
        ):
            controller.connect()
            backend.set_stale(0)
            with self.assertRaisesRegex(RuntimeError, "feedback stale: id=1"):
                controller.reset(to_default=True)
            self.assertLess(len(backend.command_history), 300)
            self.assertFalse(backend.motors_enabled)
            controller.disconnect()

    def test_direct_disable_interrupts_an_active_return(self) -> None:
        backend = FakeMotorBackend(2)
        controller = controller_for(backend)
        with mock.patch.object(controller, "_start_position_hold"), mock.patch(
            "factory_hmi.core.controller.motor_default_positions", return_value=np.array([0.2, -0.2])
        ), concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            controller.connect()
            moving = pool.submit(controller.reset, to_default=True)
            self.assertTrue(backend.wait_for_commands(3, timeout=1.0))
            before = time.monotonic()
            controller.disable_motors()
            self.assertLess(time.monotonic() - before, 0.5)
            with self.assertRaisesRegex(RuntimeError, "interrupted by stop request"):
                moving.result(timeout=1.0)
            self.assertFalse(backend.motors_enabled)
            count = len(backend.command_history)
            time.sleep(0.02)
            self.assertEqual(len(backend.command_history), count)
            controller.disconnect()


class BlockingReturn:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.cancelled = threading.Event()
        self.disabled = False
        self.reset_calls = 0

    def snapshot(self):
        return {"state": "connected", "motors": [], "diagnostics": []}

    def reset(self):
        self.reset_calls += 1
        self.started.set()
        if not self.cancelled.wait(3.0):
            raise RuntimeError("test return was not cancelled")
        raise RuntimeError("return to default interrupted by stop request")

    def request_motion_stop(self):
        self.cancelled.set()
        return True

    def disable_motors(self):
        self.disabled = True

    def disconnect(self):
        self.disabled = True


class DefaultReturnGatewayTests(unittest.TestCase):
    def test_a_queued_return_is_discarded_after_disable_arrives(self) -> None:
        controller = BlockingReturn()
        queued = threading.Event()

        class ObservedGuard:
            def __init__(self):
                self.lock = threading.RLock()

            def __enter__(self):
                if controller.started.is_set() and threading.current_thread().name == "AnyIO worker thread":
                    queued.set()
                return self.lock.__enter__()

            def __exit__(self, *args):
                return self.lock.__exit__(*args)

        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(controller, lease_timeout_s=30.0, data_root=Path(tmp))
            runtime = app.state.runtime.session("return-operator")
            runtime._lease_guard = ObservedGuard()
            with TestClient(app) as client, concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                headers = {"X-RP1-Client-ID": "return-operator"}
                moving = pool.submit(client.post, "/api/v1/playback/reset", headers=headers)
                self.assertTrue(controller.started.wait(1.0))
                waiting = pool.submit(client.post, "/api/v1/playback/reset", headers=headers)
                try:
                    self.assertTrue(queued.wait(1.0))
                finally:
                    response = client.post("/api/v1/motors/disable", headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
                stale = waiting.result(timeout=1.0)
                self.assertEqual(stale.status_code, 409, stale.text)
                self.assertIn("superseded", stale.json()["detail"])
                self.assertEqual(controller.reset_calls, 1)
                self.assertEqual(moving.result(timeout=1.0).status_code, 409)

    def test_disable_signals_before_waiting_for_the_return_request_guard(self) -> None:
        controller = BlockingReturn()
        with tempfile.TemporaryDirectory() as tmp, TestClient(
            create_app(controller, lease_timeout_s=30.0, data_root=Path(tmp))
        ) as client, concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            headers = {"X-RP1-Client-ID": "return-operator"}
            moving = pool.submit(client.post, "/api/v1/playback/reset", headers=headers)
            self.assertTrue(controller.started.wait(1.0))
            started = time.monotonic()
            response = client.post("/api/v1/motors/disable", headers=headers)
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(controller.disabled)
            self.assertEqual(moving.result(timeout=1.0).status_code, 409)

    def test_invalid_token_cannot_cancel_a_return(self) -> None:
        controller = BlockingReturn()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            "os.environ", {"RP1_FACTORY_OPERATOR_TOKEN": "test-token"}
        ), TestClient(create_app(controller, data_root=Path(tmp))) as client:
            response = client.post("/api/v1/motors/disable", headers={"X-RP1-Client-ID": "operator"})
            self.assertEqual(response.status_code, 401)
            self.assertFalse(controller.cancelled.is_set())
            self.assertFalse(controller.disabled)

    def test_another_owner_cannot_signal_the_active_return(self) -> None:
        controller = BlockingReturn()
        with tempfile.TemporaryDirectory() as tmp, TestClient(
            create_app(controller, lease_timeout_s=30.0, data_root=Path(tmp))
        ) as client, concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            headers = {"X-RP1-Client-ID": "return-operator"}
            moving = pool.submit(client.post, "/api/v1/playback/reset", headers=headers)
            self.assertTrue(controller.started.wait(1.0))
            try:
                response = client.post(
                    "/api/v1/motors/disable", headers={"X-RP1-Client-ID": "other-operator"}
                )
                self.assertEqual(response.status_code, 409)
                self.assertFalse(controller.cancelled.is_set())
            finally:
                client.post("/api/v1/motors/disable", headers=headers)
            self.assertEqual(moving.result(timeout=1.0).status_code, 409)
