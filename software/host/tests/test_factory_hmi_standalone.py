from __future__ import annotations

import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np

from factory_hmi.core.backend import (
    FakeMotorBackend,
    MotorSafetyError,
    MotorsPyBackend,
)
from factory_hmi.core.config import load_station_config
from factory_hmi.core.service import FactoryService


class FailingEnableBackend(FakeMotorBackend):
    def _enable_motors_impl(self) -> None:
        self.enabled[0] = True
        raise MotorSafetyError("injected partial enable failure")


class ReadOnlyStatusMotor:
    def __init__(self, position: float) -> None:
        self.position = float(position)
        self.response_count = 0
        self.refresh_count = 0

    def refresh_motor_status(self) -> None:
        self.refresh_count += 1
        self.response_count += 1
        # A real motors_py callback resets this counter after parsing feedback.
        self.response_count = 0

    def get_response_count(self) -> int:
        return self.response_count

    def get_motor_pos(self) -> float:
        return self.position

    def get_motor_spd(self) -> float:
        return 0.0

    def get_motor_current(self) -> float:
        return 0.0

    def get_motor_temperature(self) -> float:
        return 31.0

    def get_error_id(self) -> int:
        return 0

    def get_motor_dc_bus_voltage(self) -> float:
        return 48.0

    def get_motor_dc_bus_current(self) -> float:
        return 0.0


class SequencedFeedbackMotor(ReadOnlyStatusMotor):
    def __init__(self, position: float) -> None:
        super().__init__(position)
        self.feedback_count = 0

    def get_feedback_count(self) -> int:
        return self.feedback_count


class StandaloneRuntimeTests(unittest.TestCase):
    def test_embedded_station_config_loads_without_working_tree_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            previous = Path.cwd()
            try:
                os.chdir(temp_dir)
                config = load_station_config("left_arm")
            finally:
                os.chdir(previous)
        self.assertEqual(config.limb, "left_arm")
        self.assertGreater(len(config.entries), 0)

    def test_transport_and_motor_enable_are_separate(self) -> None:
        backend = FakeMotorBackend(2)
        backend.open_transport()
        self.assertTrue(backend.connected)
        self.assertFalse(backend.motors_enabled)
        with self.assertRaises(MotorSafetyError):
            backend.command_mit(np.zeros(2))

        backend.enable_motors()
        self.assertTrue(backend.motors_enabled)
        backend.command_mit(np.array([0.1, -0.1]))
        backend.disable_motors()
        self.assertTrue(backend.connected)
        self.assertFalse(backend.motors_enabled)
        backend.close_transport()
        self.assertFalse(backend.connected)

    def test_disabled_lro_feedback_is_polled_without_enabling_motors(self) -> None:
        template = FakeMotorBackend(2).entries
        entries = (
            replace(template[0], motor_type="LRO"),
            replace(template[1], motor_type="EVO"),
        )
        lro = ReadOnlyStatusMotor(0.25)
        evo = ReadOnlyStatusMotor(-0.5)
        backend = MotorsPyBackend(entries)
        backend._motors = [lro, evo]
        backend._connected = True
        try:
            backend.refresh_feedback()
            samples = backend.read_samples()
        finally:
            backend._connected = False

        self.assertFalse(backend.motors_enabled)
        self.assertEqual(lro.refresh_count, 1)
        self.assertEqual(evo.refresh_count, 0)
        self.assertAlmostEqual(samples[0].pos_rad, 0.25)
        self.assertLess(samples[0].feedback_age_s, 0.1)
        self.assertGreater(samples[1].feedback_age_s, 1.0)

    def test_enabled_batched_feedback_tracks_each_motor_response(self) -> None:
        template = FakeMotorBackend(2).entries
        backend = MotorsPyBackend(template)
        first = ReadOnlyStatusMotor(0.25)
        second = ReadOnlyStatusMotor(-0.5)
        backend._motors = [first, second]
        backend._connected = True
        backend._enabled = True
        backend._last_mit_command_at = time.monotonic()
        try:
            fresh = backend.read_samples()
            second.response_count = 1
            backend._last_feedback_at[1] = time.monotonic() - 1.0
            stale = backend.read_samples()
        finally:
            backend._enabled = False
            backend._connected = False

        self.assertLess(fresh[0].feedback_age_s, 0.1)
        self.assertLess(fresh[1].feedback_age_s, 0.1)
        self.assertGreater(stale[1].feedback_age_s, 0.9)

    def test_lro_feedback_sequence_cannot_be_hidden_by_pending_commands(self) -> None:
        template = FakeMotorBackend(2).entries
        entries = tuple(replace(entry, motor_type="LRO") for entry in template)
        backend = MotorsPyBackend(entries)
        first = SequencedFeedbackMotor(0.25)
        second = SequencedFeedbackMotor(-0.5)
        first.response_count = 20
        second.response_count = 20
        backend._motors = [first, second]
        backend._connected = True
        backend._enabled = True
        backend._last_feedback_counts = [0, 0]
        backend._last_feedback_at[:] = time.monotonic() - 3.0
        first.feedback_count += 1
        try:
            samples = backend.read_samples()
        finally:
            backend._enabled = False
            backend._connected = False

        self.assertLess(samples[0].feedback_age_s, 0.1)
        self.assertGreater(samples[1].feedback_age_s, 2.9)

    def test_partial_enable_failure_rolls_back_all_motors(self) -> None:
        backend = FailingEnableBackend(3)
        backend.open_transport()
        with self.assertRaisesRegex(MotorSafetyError, "partial enable"):
            backend.enable_motors()
        self.assertFalse(backend.motors_enabled)
        self.assertFalse(bool(np.any(backend.enabled)))
        backend.close_transport()

    def test_service_enable_failure_closes_motor_transport_but_keeps_can(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        config_path = repository / "scripts/config/arm_motors.yaml"
        with tempfile.TemporaryDirectory() as temp_dir:
            service = FactoryService(data_root=temp_dir)
            service.configure(
                config_path=str(config_path),
                limb="left_arm",
                backend="fake",
            )
            service.can_connect()
            backend = FailingEnableBackend(service.controller.config.entries)
            service.controller.backend = backend
            service.discover_motors()

            with self.assertRaisesRegex(MotorSafetyError, "partial enable"):
                service.enable_motors(physical_estop_confirmed=True)

            snapshot = service.snapshot()
            self.assertFalse(backend.connected)
            self.assertEqual(snapshot["station"]["state"], "disconnected")
            self.assertFalse(snapshot["station"]["motors_discovered"])
            self.assertTrue(snapshot["can"]["all_up"])

    def test_service_requires_estop_confirmation_before_enable(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        config_path = repository / "scripts/config/arm_motors.yaml"
        with tempfile.TemporaryDirectory() as temp_dir:
            service = FactoryService(data_root=temp_dir)
            service.configure(
                config_path=str(config_path),
                limb="left_arm",
                backend="fake",
            )
            self.assertFalse(service.can_status()["all_up"])
            can_connected = service.can_connect()
            self.assertTrue(can_connected["can"]["all_up"])
            self.assertEqual(
                can_connected["station"]["connection_phase"],
                "can_connected",
            )
            discovered = service.discover_motors()
            self.assertTrue(discovered["station"]["backend_connected"])
            self.assertFalse(discovered["station"]["motors_enabled"])
            self.assertEqual(
                discovered["station"]["connection_phase"],
                "motors_discovered",
            )
            with self.assertRaisesRegex(RuntimeError, "emergency-stop"):
                service.enable_motors(physical_estop_confirmed=False)
            enabled = service.enable_motors(physical_estop_confirmed=True)
            self.assertTrue(enabled["station"]["motors_enabled"])
            self.assertEqual(
                enabled["station"]["connection_phase"],
                "motors_enabled",
            )
            with self.assertRaisesRegex(RuntimeError, "already enabled"):
                service.enable_motors(physical_estop_confirmed=True)
            after_duplicate = service.snapshot()
            self.assertTrue(after_duplicate["station"]["backend_connected"])
            self.assertTrue(after_duplicate["station"]["motors_enabled"])
            self.assertTrue(after_duplicate["station"]["position_hold_active"])

            source = Path(temp_dir) / "source.npz"
            np.savez(
                source,
                motor_pos=np.zeros((2, 7), dtype=np.float64),
                fps=np.asarray([200.0]),
                time=np.asarray([0.0, 0.005]),
                limb=np.asarray(["left_arm"]),
            )
            imported = service.import_trajectory(str(source))
            imported_path = Path(imported["path"])
            self.assertEqual(
                imported_path.parent,
                Path(temp_dir) / "trajectories",
            )
            self.assertTrue(imported_path.is_file())
            disabled = service.disable_motors()
            self.assertFalse(disabled["station"]["motors_enabled"])
            service.disconnect()
            self.assertFalse(service.can_disconnect()["can"]["all_up"])

    def test_active_configuration_can_be_cleared_and_replaced(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        arm_config = repository / "scripts/config/arm_motors.yaml"
        short_arm_config = repository / "scripts/config/short_arm_motors.yaml"
        with tempfile.TemporaryDirectory() as temp_dir:
            service = FactoryService(data_root=temp_dir)
            first = service.configure(
                config_path=str(arm_config),
                limb="left_arm",
                backend="fake",
            )
            self.assertEqual(first["station"]["limb"], "left_arm")
            self.assertEqual(len(first["configured_motors"]), 7)
            service.can_connect()
            service.discover_motors()
            service.enable_motors(physical_estop_confirmed=True)

            cleared = service.clear_configuration()
            self.assertFalse(cleared["configured"])
            self.assertEqual(cleared["configured_motors"], [])
            self.assertFalse(cleared["can"]["all_up"])

            replacement = service.configure(
                config_path=str(short_arm_config),
                limb="right_short_arm",
                backend="fake",
            )
            self.assertEqual(replacement["station"]["limb"], "right_short_arm")
            self.assertEqual(len(replacement["configured_motors"]), 5)
            self.assertEqual(
                replacement["configured_motors"][0]["joint_name"],
                "right_shoulder_pitch_joint",
            )

    def test_manual_joint_move_runs_smooth_control_and_returns_to_hold(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        arm_config = repository / "scripts/config/arm_motors.yaml"
        with tempfile.TemporaryDirectory() as temp_dir:
            service = FactoryService(data_root=temp_dir)
            service.configure(
                config_path=str(arm_config),
                limb="left_arm",
                backend="fake",
            )
            service.can_connect()
            service.discover_motors()
            service.enable_motors(physical_estop_confirmed=True)

            moving = service.manual_move(
                targets_deg=[1.0, -1.0, 0.5, 0.0, 0.0, 0.0, 0.0],
                speed_deg_s=20.0,
            )
            self.assertTrue(moving["station"]["manual_control_active"])
            time.sleep(0.15)
            active = service.snapshot()
            self.assertTrue(active["manual"]["active"])
            self.assertEqual(len(active["joint_positions_rad"]), 7)
            updated = service.manual_move(
                targets_deg=[1.2, -1.0, 0.5, 0.0, 0.0, 0.0, 0.0],
                speed_deg_s=5.0,
            )
            self.assertAlmostEqual(
                updated["manual"]["targets_rad"][0],
                np.deg2rad(1.2),
            )
            self.assertAlmostEqual(
                updated["manual"]["speed_rad_s"],
                np.deg2rad(5.0),
            )

            stopped = service.manual_stop()
            self.assertFalse(stopped["station"]["manual_control_active"])
            self.assertTrue(stopped["station"]["position_hold_active"])

    def test_manual_joint_move_rejects_motor_count_mismatch(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        arm_config = repository / "scripts/config/arm_motors.yaml"
        with tempfile.TemporaryDirectory() as temp_dir:
            service = FactoryService(data_root=temp_dir)
            service.configure(
                config_path=str(arm_config),
                limb="left_arm",
                backend="fake",
            )
            service.can_connect()
            service.discover_motors()
            service.enable_motors(physical_estop_confirmed=True)
            with self.assertRaisesRegex(ValueError, "requires 7 joints"):
                service.manual_move(
                    targets_deg=[0.0, 0.0],
                    speed_deg_s=10.0,
                )


if __name__ == "__main__":
    unittest.main()
