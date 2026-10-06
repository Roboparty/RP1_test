from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from factory_hmi import (
    FakeMotorBackend,
    FactoryController,
    MotorSample,
    PlaybackOptions,
    SafetyLimits,
    StationConfig,
    StationState,
    StationStateMachine,
    build_station_config,
)
from factory_hmi.core import (
    DiagnosisThresholds,
    FaultDiagnosisEngine,
    InvalidStateTransition,
    MotorSafetyError,
    MotorZeroingSession,
    TrajectoryValidationError,
    diagnose_faults,
    import_trajectory,
    load_station_config,
    preview_station_config,
    preflight_trajectory,
)
from factory_hmi.core.service import FactoryService


def fake_station(
    *,
    control_rate_hz: float = 200.0,
    limits: SafetyLimits | None = None,
) -> tuple[StationConfig, FakeMotorBackend]:
    backend = FakeMotorBackend(2)
    config = StationConfig(
        limb="left_arm",
        entries=backend.entries,
        raw={},
        control_rate_hz=control_rate_hz,
        safety_limits=limits or SafetyLimits(),
    )
    return config, backend


def write_trajectory(
    directory: Path,
    values: np.ndarray,
    timestamps: np.ndarray,
    *,
    limb: str = "left_arm",
) -> Path:
    path = directory / "trajectory.npz"
    fps = 1.0 / float(np.median(np.diff(timestamps))) if len(timestamps) > 1 else 200.0
    np.savez(
        path,
        motor_pos=np.asarray(values, dtype=np.float64),
        fps=np.asarray([fps], dtype=np.float64),
        time=np.asarray(timestamps, dtype=np.float64),
        limb=np.asarray([limb]),
    )
    return path


class StateMachineTests(unittest.TestCase):
    def test_legal_and_illegal_transitions(self) -> None:
        machine = StationStateMachine()
        machine.transition(StationState.CONNECTED)
        machine.transition(StationState.ARMED)
        machine.transition(StationState.RUNNING)
        machine.transition(StationState.PAUSED)
        machine.transition(StationState.STOPPING)
        machine.transition(StationState.COMPLETED)
        self.assertEqual(machine.state, StationState.COMPLETED)

        with self.assertRaises(InvalidStateTransition):
            machine.transition(StationState.RUNNING)

    def test_all_required_station_configs_load(self) -> None:
        expected_counts = {
            "left_arm": 7,
            "right_arm": 7,
            "left_short_arm": 5,
            "right_short_arm": 5,
            "left_leg": 6,
            "right_leg": 6,
            "left_short_leg": 5,
            "right_short_leg": 5,
            "waist_hip": 4,
            "biped_waist": 14,
            "upper_body": 12,
        }
        for limb, count in expected_counts.items():
            with self.subTest(limb=limb):
                self.assertEqual(len(load_station_config(limb).entries), count)

    def test_per_bus_transport_backend_is_configurable(self) -> None:
        loaded = load_station_config("left_arm")
        raw = dict(loaded.raw)
        raw["motor_backend"] = "zlg"
        configured = build_station_config(raw, "left_arm")
        self.assertEqual(
            {entry.backend for entry in configured.entries},
            {"zlg"},
        )

    def test_yaml_preview_ignores_can_and_exposes_editable_motor_values(self) -> None:
        path = Path(__file__).resolve().parents[1] / "scripts" / "config" / "arm_motors.yaml"
        preview = preview_station_config("left_arm", path)
        self.assertEqual(
            preview["bus_groups"],
            [{"index": 0, "motor_count": 7, "default_interface": "can0"}],
        )
        self.assertEqual(len(preview["motors"]), 7)
        self.assertEqual(preview["motors"][0]["kp"], 60.0)
        self.assertEqual(preview["motors"][0]["motor_type"], "LRO")
        self.assertNotIn("interface", preview["motors"][0])

    def test_yaml_preview_recognizes_five_motor_arm_as_short_arm(self) -> None:
        path = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "config"
            / "short_arm_motors.yaml"
        )

        preview = preview_station_config("left_arm", path)

        self.assertEqual(preview["limb"], "left_short_arm")
        self.assertEqual(
            preview["bus_groups"],
            [{"index": 0, "motor_count": 5, "default_interface": "can0"}],
        )
        self.assertEqual(len(preview["motors"]), 5)
        self.assertEqual(
            preview["motors"][-1]["joint_name"],
            "left_wrist_roll_joint",
        )

    def test_shared_bus_topology_is_renumbered_from_can0(self) -> None:
        path = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "config"
            / "waist_hip_motors.yaml"
        )

        preview = preview_station_config("waist_hip", path)

        self.assertEqual(
            [group["default_interface"] for group in preview["bus_groups"]],
            ["can0", "can1", "can0", "can1"],
        )
        self.assertEqual(len(preview["motors"]), 4)

    def test_short_leg_close_chain_configuration_loads(self) -> None:
        configured = load_station_config("right_short_leg")

        self.assertEqual(len(configured.entries), 5)
        self.assertEqual(
            [entry.joint_name for entry in configured.entries[-2:]],
            ["right_ankle_pitch_joint", "right_ankle_roll_joint"],
        )

    def test_service_accepts_reused_physical_bus_with_identical_timing(self) -> None:
        path = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "config"
            / "waist_hip_motors.yaml"
        )
        interfaces = ["can0", "can1", "can0", "can1"]
        bindings = [
            {
                "index": index,
                "interface": interface,
                "mode": "canfd",
                "bitrate": 1_000_000,
                "dbitrate": 5_000_000,
            }
            for index, interface in enumerate(interfaces)
        ]
        with tempfile.TemporaryDirectory() as directory:
            service = FactoryService(data_root=directory)
            snapshot = service.configure(
                config_path=str(path),
                limb="waist_hip",
                backend="fake",
                bus_bindings=bindings,
            )

        self.assertEqual(snapshot["station"]["limb"], "waist_hip")
        self.assertEqual(
            [entry.interface for entry in service.controller.config.entries],
            interfaces,
        )

    def test_hmi_bindings_and_motor_overrides_replace_yaml_values(self) -> None:
        path = Path(__file__).resolve().parents[1] / "scripts" / "config" / "arm_motors.yaml"
        configured = load_station_config(
            "left_arm",
            path,
            bus_bindings=[
                {
                    "index": 0,
                    "interface": "can4",
                    "mode": "can",
                    "bitrate": 500_000,
                    "dbitrate": None,
                }
            ],
            motor_overrides=[
                {
                    "index": 0,
                    "motor_id": 21,
                    "motor_type": "evo",
                    "motor_model": 3,
                    "zero_offset": 0.25,
                    "kp": 88.0,
                    "kd": 2.5,
                    "sign": -1.0,
                }
            ],
        )
        first = configured.entries[0]
        self.assertEqual(first.interface, "can4")
        self.assertEqual(first.interface_type, "can")
        self.assertEqual(first.motor_id, 21)
        self.assertEqual(first.motor_type, "EVO")
        self.assertEqual(first.kp, 88.0)
        self.assertEqual(first.kd, 2.5)
        self.assertEqual(first.sign, -1.0)

    def test_bus_binding_count_and_duplicate_motor_override_are_rejected(self) -> None:
        path = Path(__file__).resolve().parents[1] / "scripts" / "config" / "arm_motors.yaml"
        with self.assertRaisesRegex(ValueError, "bus_bindings must contain 1"):
            load_station_config("left_arm", path, bus_bindings=[])
        with self.assertRaisesRegex(ValueError, "duplicate motor override"):
            load_station_config(
                "left_arm",
                path,
                motor_overrides=[{"index": 0}, {"index": 0}],
            )


class TrajectoryTests(unittest.TestCase):
    def test_import_hash_preview_and_safety_preflight(self) -> None:
        config, backend = fake_station()
        with tempfile.TemporaryDirectory() as tmp:
            path = write_trajectory(
                Path(tmp),
                np.asarray([[0.0, 0.0], [0.1, -0.1], [0.2, 0.0]]),
                np.asarray([0.0, 0.1, 0.2]),
            )
            trajectory = import_trajectory(path, limb="left_arm", motor_count=2)
            report = preflight_trajectory(
                trajectory,
                entries=config.entries,
                config=config.raw,
                limits=SafetyLimits(
                    motor_min_rad=-0.5,
                    motor_max_rad=0.5,
                    max_frame_delta_rad=0.2,
                    max_velocity_rad_s=2.0,
                    max_first_frame_delta_rad=0.1,
                ),
                current_motor_pos=backend.positions,
            )

        self.assertEqual(len(trajectory.sha256), 64)
        self.assertTrue(report.safe)
        self.assertEqual(
            [point["frame"] for point in trajectory.preview["points"]],
            [0, 1, 2],
        )

    def test_invalid_npz_and_limit_violation_are_rejected(self) -> None:
        config, _ = fake_station()
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            bad = write_trajectory(
                directory,
                np.asarray([[0.0, np.nan]]),
                np.asarray([0.0]),
            )
            with self.assertRaises(TrajectoryValidationError):
                import_trajectory(bad, limb="left_arm", motor_count=2)

            fast = write_trajectory(
                directory,
                np.asarray([[0.0, 0.0], [1.0, 0.0]]),
                np.asarray([0.0, 0.01]),
            )
            trajectory = import_trajectory(fast, limb="left_arm", motor_count=2)
            with self.assertRaises(TrajectoryValidationError) as caught:
                preflight_trajectory(
                    trajectory,
                    entries=config.entries,
                    config=config.raw,
                    limits=SafetyLimits(
                        motor_min_rad=-0.5,
                        motor_max_rad=0.5,
                        max_frame_delta_rad=0.2,
                        max_velocity_rad_s=5.0,
                    ),
                )
            self.assertIn("motor[0]", str(caught.exception))


class PlaybackTests(unittest.TestCase):
    def test_first_frame_preflight_failure_keeps_position_hold_available(self) -> None:
        config, backend = fake_station(
            control_rate_hz=20.0,
            limits=SafetyLimits(max_first_frame_delta_rad=0.1),
        )
        controller = FactoryController(config, backend, enable_watchdog=False)
        with tempfile.TemporaryDirectory() as tmp:
            path = write_trajectory(
                Path(tmp),
                np.asarray([[0.5, -0.5], [0.6, -0.6]]),
                np.asarray([0.0, 0.1]),
            )
            controller.connect()
            trajectory = controller.load_trajectory(path)
            self.assertIn(
                "first-frame delta check is deferred",
                trajectory.preflight.warnings[0],
            )

            with self.assertRaisesRegex(
                TrajectoryValidationError,
                "first-frame delta too large",
            ):
                controller.arm()

            self.assertEqual(controller.state, StationState.CONNECTED)
            self.assertTrue(backend.motors_enabled)
            self.assertTrue(controller.position_hold_active)

            with (
                mock.patch(
                    "factory_hmi.core.controller.motor_default_positions",
                    return_value=np.asarray([0.5, -0.5]),
                ),
                mock.patch(
                    "factory_hmi.core.controller.time.sleep",
                    return_value=None,
                ),
            ):
                controller.reset(to_default=True)
            controller.arm()
            self.assertEqual(controller.state, StationState.ARMED)
            controller.disconnect()

    def test_enable_continuously_holds_the_measured_position(self) -> None:
        config, backend = fake_station(control_rate_hz=100.0)
        backend.positions[:] = np.asarray([0.3, -0.2])
        controller = FactoryController(config, backend, enable_watchdog=False)

        controller.discover_motors()
        self.assertEqual(len(backend.command_history), 0)
        controller.enable_motors()
        self.assertTrue(backend.wait_for_commands(3, timeout=0.2))

        snapshot = controller.snapshot()
        self.assertTrue(snapshot["station"]["position_hold_active"])
        self.assertTrue(snapshot["station"]["motors_enabled"])
        for command in backend.command_history[:3]:
            np.testing.assert_allclose(command["target"], [0.3, -0.2])
        controller.disconnect()

    def test_return_to_default_keeps_transport_and_enable_state(self) -> None:
        config, backend = fake_station(control_rate_hz=20.0)
        backend.positions[:] = np.asarray([0.4, -0.3])
        controller = FactoryController(config, backend, enable_watchdog=False)
        controller.connect()

        with (
            mock.patch.object(backend, "disconnect", wraps=backend.disconnect) as disconnect,
            mock.patch.object(backend, "connect", wraps=backend.connect) as connect,
            mock.patch(
                "factory_hmi.core.controller.motor_default_positions",
                return_value=np.asarray([0.1, -0.1]),
            ),
            mock.patch("factory_hmi.core.controller.time.sleep", return_value=None),
        ):
            controller.reset(to_default=True)

        disconnect.assert_not_called()
        connect.assert_not_called()
        self.assertTrue(backend.motors_enabled)
        self.assertTrue(controller.position_hold_active)
        np.testing.assert_allclose(
            backend.command_history[-1]["target"],
            [0.1, -0.1],
        )
        controller.disconnect()

    def test_fake_playback_completes_and_snapshot_is_strict_json(self) -> None:
        config, backend = fake_station(control_rate_hz=200.0)
        controller = FactoryController(
            config,
            backend,
            enable_watchdog=True,
            enable_recording=False,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = write_trajectory(
                Path(tmp),
                np.asarray([[0.0, 0.0], [0.2, -0.2], [0.0, 0.0]]),
                np.asarray([0.0, 0.03, 0.06]),
            )
            controller.connect()
            controller.load_trajectory(path)
            controller.arm()
            controller.start()
            self.assertTrue(controller.wait(1.0))
            self.assertEqual(controller.state, StationState.COMPLETED)
            self.assertGreaterEqual(len(backend.command_history), 3)
            self.assertTrue(backend.motors_enabled)
            self.assertTrue(controller.position_hold_active)
            json.dumps(controller.snapshot(), allow_nan=False)
            controller.disconnect()

    def test_pause_holds_in_worker_and_stop_completes(self) -> None:
        config, backend = fake_station(control_rate_hz=200.0)
        controller = FactoryController(
            config,
            backend,
            enable_watchdog=False,
            enable_recording=False,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = write_trajectory(
                Path(tmp),
                np.asarray([[0.0, 0.0], [0.5, -0.5]]),
                np.asarray([0.0, 1.0]),
            )
            controller.connect()
            controller.load_trajectory(path)
            controller.arm()
            controller.start(PlaybackOptions(loop=True, cycles=0))
            self.assertTrue(backend.wait_for_commands(3))
            controller.pause()
            before = controller.snapshot()["progress"]["active_seconds"]
            command_count = len(backend.command_history)
            time.sleep(0.04)
            after = controller.snapshot()["progress"]["active_seconds"]
            self.assertEqual(controller.state, StationState.PAUSED)
            self.assertGreater(len(backend.command_history), command_count)
            self.assertLess(after - before, 0.02)
            controller.stop()
            self.assertEqual(controller.state, StationState.COMPLETED)
            controller.disconnect()

    def test_watchdog_fault_disables_and_reset_reconnects(self) -> None:
        config, backend = fake_station(control_rate_hz=200.0)
        controller = FactoryController(config, backend, enable_watchdog=True)
        with tempfile.TemporaryDirectory() as tmp:
            path = write_trajectory(
                Path(tmp),
                np.asarray([[0.0, 0.0], [0.5, -0.5]]),
                np.asarray([0.0, 1.0]),
            )
            controller.connect()
            controller.load_trajectory(path)
            controller.arm()
            backend.inject_error(0, 7)
            controller.start()
            self.assertTrue(controller.wait(1.0))
            self.assertEqual(controller.state, StationState.FAULT)
            self.assertFalse(bool(np.any(backend.enabled)))

            controller.reset()
            self.assertEqual(controller.state, StationState.CONNECTED)
            self.assertTrue(bool(np.all(backend.enabled)))
            controller.disconnect()

    def test_platform_outage_does_not_discard_local_recording(self) -> None:
        config, backend = fake_station(control_rate_hz=100.0)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {
                "TEST_PLATFORM_API_URL": "http://127.0.0.1:1",
                "INFLUXDB_TOKEN": "",
            },
            clear=False,
        ):
            output = Path(tmp) / "local.csv"
            controller = FactoryController(
                config,
                backend,
                enable_watchdog=False,
                enable_recording=True,
                record_output=output,
            )
            path = write_trajectory(
                Path(tmp),
                np.asarray([[0.0, 0.0], [0.1, -0.1]]),
                np.asarray([0.0, 0.03]),
            )
            controller.connect()
            controller.load_trajectory(path)
            controller.arm()
            controller.start(
                test_id="LOCAL-001",
                robot_id="RP1-TEST",
                record_rate_hz=10.0,
            )
            self.assertTrue(controller.wait(2.0))
            self.assertEqual(controller.state, StationState.COMPLETED)
            playback = controller.snapshot()["playback"]
            self.assertEqual(
                playback["record_rate_hz"],
                10.0,
            )
            self.assertTrue(playback["recording"]["finished"])
            self.assertEqual(playback["recording"]["filename"], "local.csv")
            self.assertGreaterEqual(playback["recording"]["row_count"], 0)
            self.assertGreater(playback["statistics"]["sample_count"], 0)
            self.assertEqual(len(playback["statistics"]["joints"]), 2)
            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 0)
            controller.prepare_next_run()
            next_snapshot = controller.snapshot()
            self.assertEqual(controller.state, StationState.CONNECTED)
            self.assertIsNone(next_snapshot["trajectory"])
            self.assertTrue(next_snapshot["station"]["position_hold_active"])
            controller.disconnect()


class DiagnosisTests(unittest.TestCase):
    def test_rules_report_evidence_without_certain_root_cause(self) -> None:
        samples = [
            MotorSample(
                pos_rad=0.0,
                cmd_pos_rad=1.0,
                temp_c=90.0,
                error_id=3,
                bus_voltage_v=30.0,
                motor_id=1,
                index=0,
                joint_name="joint_0",
                bus="can0",
                feedback_age_s=3.0,
            ),
            MotorSample(
                pos_rad=0.0,
                cmd_pos_rad=0.0,
                temp_c=30.0,
                motor_id=2,
                index=1,
                joint_name="joint_1",
                bus="can0",
                feedback_age_s=3.0,
            ),
        ]
        diagnoses = diagnose_faults(samples, heartbeat_age_s=1.0)
        rules = {item.rule for item in diagnoses}
        self.assertTrue(
            {
                "motor_error",
                "tracking_error",
                "undervoltage",
                "motor_overtemperature",
                "multi_motor_bus_stale",
                "controller_heartbeat_stale",
            }.issubset(rules)
        )
        for diagnosis in diagnoses:
            self.assertTrue(diagnosis.evidence)
            self.assertLessEqual(diagnosis.confidence, 0.95)
        stale = next(
            item
            for item in diagnoses
            if item.rule == "multi_motor_bus_stale"
        )
        self.assertEqual(stale.affected_motor_ids, (1, 2))
        self.assertEqual(stale.to_dict()["affected_motor_ids"], [1, 2])

    def test_persistent_torque_and_temperature_rise_needs_history(self) -> None:
        engine = FaultDiagnosisEngine(
            DiagnosisThresholds(
                high_torque_nm=10.0,
                high_torque_seconds=2.0,
                temperature_rise_c=5.0,
                temperature_window_s=10.0,
            )
        )
        initial = MotorSample(
            pos_rad=0.0,
            torque_nm=12.0,
            temp_c=30.0,
            motor_id=1,
            index=0,
        )
        hot = MotorSample(
            pos_rad=0.0,
            torque_nm=12.0,
            temp_c=37.0,
            motor_id=1,
            index=0,
        )
        self.assertFalse(engine.diagnose([initial], now_s=10.0))
        diagnoses = engine.diagnose([hot], now_s=12.1)
        self.assertIn(
            "persistent_torque_temperature_rise",
            {item.rule for item in diagnoses},
        )


class ZeroingTests(unittest.TestCase):
    def test_direct_zero_disables_station_and_supports_single_or_all(self) -> None:
        config, backend = fake_station()
        controller = FactoryController(config, backend, enable_recording=False)
        controller.discover_motors()
        controller.enable_motors()

        single = controller.zero_motors([1])
        self.assertEqual([item["index"] for item in single["zeroed"]], [1])
        self.assertFalse(backend.motors_enabled)
        self.assertEqual(backend.zeroed_indices, [1])
        self.assertFalse(controller.position_hold_active)

        all_motors = controller.zero_motors(
            list(range(len(config.entries)))
        )
        self.assertEqual(
            [item["index"] for item in all_motors["zeroed"]],
            list(range(len(config.entries))),
        )
        self.assertFalse(backend.motors_enabled)
        controller.disconnect()

    def test_direct_zero_reports_unverified_feedback_without_http_failure(
        self,
    ) -> None:
        config, backend = fake_station()
        controller = FactoryController(config, backend, enable_recording=False)
        controller.discover_motors()
        controller.enable_motors()

        with mock.patch.object(backend, "zero_motor", return_value=False):
            result = controller.zero_motors([1])

        self.assertFalse(result["zeroed"][0]["acknowledged"])
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("ID 2", result["warnings"][0])
        self.assertFalse(backend.motors_enabled)
        controller.disconnect()

    def test_faulted_motor_is_rejected_unless_explicitly_forced(self) -> None:
        backend = FakeMotorBackend(2, initial_positions=[0.3, -0.2])
        backend.connect()
        backend.inject_error(0, 7)
        session = MotorZeroingSession(backend)
        session.prepare()
        sample = session.read()
        self.assertEqual(sample.error_id, 7)
        with self.assertRaises(MotorSafetyError):
            session.confirm()
        self.assertEqual(backend.zeroed_indices, [])

        session.confirm(force_on_error=True)
        session.prepare()
        self.assertEqual(session.skip(), 1)
        self.assertEqual(session.snapshot()["state"], "completed")
        backend.disconnect()

    def test_confirming_last_motor_does_not_complete_while_others_remain(self) -> None:
        backend = FakeMotorBackend(3, initial_positions=[0.1, 0.2, 0.3])
        backend.connect()
        session = MotorZeroingSession(backend)
        session.prepare(2)
        session.read()
        session.confirm()
        snapshot = session.snapshot()
        self.assertEqual(snapshot["state"], "prepared")
        self.assertEqual(snapshot["next_index"], 0)
        self.assertEqual(snapshot["zeroed_indices"], [2])
        self.assertEqual(snapshot["remaining_indices"], [0, 1])
        session.abort()
        session.abort()  # idempotent on aborted
        self.assertEqual(session.snapshot()["state"], "aborted")
        backend.disconnect()

    def test_abort_is_idempotent_after_completed(self) -> None:
        backend = FakeMotorBackend(1, initial_positions=[0.0])
        backend.connect()
        session = MotorZeroingSession(backend)
        session.prepare(0)
        session.confirm()
        self.assertEqual(session.snapshot()["state"], "completed")
        session.abort()
        self.assertEqual(session.snapshot()["state"], "completed")
        backend.disconnect()

    def test_service_allows_independent_repeat_zeroing_of_one_motor(self) -> None:
        config, backend = fake_station()
        controller = FactoryController(config, backend, enable_recording=False)
        controller.discover_motors()
        controller.enable_motors()
        with tempfile.TemporaryDirectory() as directory:
            service = FactoryService(data_root=directory)
            service._controller = controller

            service.zero_prepare(1)
            service.zero_read()
            service.zero_confirm()
            prepared = service.zero_prepare(1)

            self.assertEqual(prepared["index"], 1)
            self.assertEqual(service._zeroing.current_index, 1)
            service.zero_abort()
        controller.disconnect()


if __name__ == "__main__":
    unittest.main()
