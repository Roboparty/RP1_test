"""Offline regressions for device lifecycle and generated-motion safety."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from test_fake_motor_watchdog import MAPPING, Clock, FakeMotor, safety_config

from rp1_test.commands import hardware as hardware_commands
from rp1_test.hardware import HardwareRunner, preflight
from rp1_test.mapping import JointMapping
from rp1_test.trajectory import Trajectory
from rp1_test.validation import validate_hardware_config, validate_motion_safety
from rp1_test.watchdog import FeedbackWatchdog


def trajectory(count=1):
    return Trajectory(
        Path("unused.npz"),
        "rp1-trajectory/v1",
        "test",
        tuple(f"j{index}" for index in range(count)),
        np.zeros((2, count)),
        np.asarray([0.0, 0.1]),
        10.0,
        "m",
        "c",
    )


def make_runner(motors, *, config=None, **kwargs):
    mappings = [
        JointMapping(f"j{index}", index, f"SARM-J{index}", "SARM") for index in range(len(motors))
    ]
    config = config or safety_config(mappings)
    clock = Clock()
    settings = dict(control_rate_hz=100, transition_seconds=0.2, clock=clock, sleep=clock.sleep)
    settings.update(kwargs)
    return HardwareRunner(
        motors,
        mappings,
        trajectory(len(motors)),
        default_pose=np.asarray(config["default_pose_rad"]),
        safety_config=config,
        **settings,
    )


@pytest.mark.parametrize("position", [float("nan"), float("inf"), -float("inf")])
def test_watchdog_rejects_nonfinite_positions_and_latches_fault(position):
    motor = FakeMotor()
    motor.position = position
    events = []
    watchdog = FeedbackWatchdog([motor], [MAPPING], event_sink=events.append)
    assert not watchdog.check(np.asarray([0.0]), elapsed_s=0.0)
    assert "non-finite" in watchdog.fault
    assert events[0]["type"] == "feedback_invalid"
    motor.position = 0.0
    assert not watchdog.check(np.asarray([0.0]), elapsed_s=1.0)


def test_watchdog_turns_position_read_exception_into_fault():
    motor = FakeMotor()
    motor.get_position = lambda: (_ for _ in ()).throw(OSError("feedback unavailable"))
    watchdog = FeedbackWatchdog([motor], [MAPPING])
    assert not watchdog.check(np.asarray([0.0]), elapsed_s=0.0)
    assert "read failed" in watchdog.fault


@pytest.mark.parametrize("position", [float("nan"), float("inf"), -float("inf"), 3.0])
def test_invalid_initial_feedback_sends_no_command_and_disables_all(position):
    motors = [FakeMotor(), FakeMotor()]
    motors[0].position = position
    runner = make_runner(motors)
    with pytest.raises((RuntimeError, ValueError)):
        runner.run(lambda *_: "q")
    assert all(not motor.commands and motor.disabled for motor in motors)


def test_normal_quit_starts_home_at_actual_feedback_and_finishes_at_default():
    motor = FakeMotor()
    motor.position = 1.0
    runner = make_runner([motor])
    runner.run(lambda *_: "q")
    assert motor.commands[0][0] == 1.0
    assert motor.commands[-1][0] == 0.0
    assert len(motor.commands) > 2
    assert motor.disabled


def test_feedback_exception_during_playback_never_sends_cleanup_motion():
    motor = FakeMotor()
    original_get = motor.get_position

    def feedback():
        if motor.commands:
            raise OSError("feedback went offline")
        return original_get()

    motor.get_position = feedback
    runner = make_runner([motor])
    with pytest.raises(RuntimeError, match="position feedback read failed") as failure:
        runner.run(lambda *_: "p")
    assert isinstance(failure.value.__cause__, OSError)
    assert len(motor.commands) == 1
    assert motor.disabled


def test_keyboard_interrupt_does_not_home_and_disables_all():
    motors = [FakeMotor(), FakeMotor()]
    runner = make_runner(motors)

    def interrupt(*_):
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        runner.run(interrupt)
    assert all(not motor.commands and motor.disabled for motor in motors)


def test_disable_failure_does_not_skip_other_motor_or_replace_motion_error():
    motors = [FakeMotor(), FakeMotor()]
    disable_attempts = []
    primary = OSError("original motion failure")

    def failing_disable():
        disable_attempts.append(0)
        raise RuntimeError("disable failed")

    def failing_command(*_args, **_kwargs):
        raise primary

    motors[0].disable = failing_disable
    motors[0].command_mit = failing_command
    runner = make_runner(motors)
    with pytest.raises(OSError) as failure:
        runner.run(lambda *_: "p")
    assert failure.value is primary
    assert disable_attempts == [0]
    assert motors[1].disabled
    assert not motors[1].commands


def test_normal_exit_reports_disable_failure_after_attempting_all_motors():
    motors = [FakeMotor(), FakeMotor()]

    def failing_disable():
        raise OSError("disable failed")

    motors[0].disable = failing_disable
    runner = make_runner(motors)
    with pytest.raises(RuntimeError, match="Motor cleanup failures"):
        runner.run(lambda *_: "q")
    assert motors[1].disabled


@pytest.mark.parametrize(
    "limit_name,limit",
    [
        ("max_velocity_rad_s", 1.0),
        ("max_acceleration_rad_s2", 1.0),
    ],
)
def test_generated_home_transition_rejected_before_any_motor_command(limit_name, limit):
    mapping = JointMapping("j0", 0, "SARM-J0", "SARM")
    config = safety_config([mapping])
    config["joint_mappings"][0][limit_name] = limit
    motor = FakeMotor()
    motor.position = 1.0
    runner = make_runner([motor], config=config)
    with pytest.raises(ValueError, match="transition.*(velocity|acceleration)"):
        runner.run(lambda *_: "q")
    assert not motor.commands
    assert motor.disabled


def test_generated_entry_transition_rejected_before_any_motor_command():
    mapping = JointMapping("j0", 0, "SARM-J0", "SARM")
    config = safety_config([mapping])
    config["joint_mappings"][0]["max_velocity_rad_s"] = 1.0
    motor = FakeMotor()
    runner = make_runner([motor], config=config)
    runner.trajectory = replace(runner.trajectory, motor_pos=np.ones((2, 1)))
    with pytest.raises(ValueError, match="transition.*velocity"):
        runner.run(lambda *_: "p")
    assert not motor.commands
    assert motor.disabled


def loop_runner(*, transition_seconds=2.0):
    config = safety_config()
    config["joint_mappings"][0]["max_velocity_rad_s"] = 2.0
    config["joint_mappings"][0]["max_acceleration_rad_s2"] = 5.0
    clock = Clock()
    samples = []
    events = []

    class RecordingMotor(FakeMotor):
        def command_mit(self, position_rad, **kwargs):
            samples.append((clock(), position_rad))
            super().command_mit(position_rad, **kwargs)

    motor = RecordingMotor()
    runner = HardwareRunner(
        [motor],
        [MAPPING],
        replace(
            trajectory(),
            motor_pos=np.asarray([[0.0], [1.0]]),
            time=np.asarray([0.0, 1.0]),
        ),
        default_pose=np.asarray([0.0]),
        safety_config=config,
        control_rate_hz=10,
        transition_seconds=transition_seconds,
        clock=clock,
        sleep=clock.sleep,
        event_sink=events.append,
    )
    return runner, motor, samples, events, config


def test_loop_wrap_uses_validated_feedback_transition_outside_active_time():
    runner, motor, samples, events, config = loop_runner()

    def gate(timeout):
        if timeout is None:
            return "p"
        starts = sum(event["type"] == "ACTIVE_START" for event in events)
        return "q" if starts == 2 and samples[-1][1] == 1.0 else ""

    runner.run(gate, loop=True)
    assert motor.disabled
    assert len(runner.active_intervals) == 2
    first, second = runner.active_intervals
    assert 1.9 <= second["start_s"] - first["end_s"] <= 2.01
    assert sum(item["end_s"] - item["start_s"] for item in runner.active_intervals) < 2.3
    wrap = [sample for sample in samples if first["end_s"] <= sample[0] <= second["start_s"]]
    assert len(wrap) >= 20
    assert wrap[0][1] == 1.0 and wrap[-1][1] == 0.0
    unique = [wrap[0]]
    for sample in wrap[1:]:
        if sample[0] == unique[-1][0]:
            assert sample[1] == unique[-1][1]
        else:
            unique.append(sample)
    validate_motion_safety(
        np.asarray([[position] for _, position in unique]),
        np.asarray([timestamp for timestamp, _ in unique]),
        config,
        label="recorded loop transition",
    )


def test_unsafe_loop_wrap_rejected_without_jump_or_fault_cleanup_motion():
    runner, motor, samples, _events, _config = loop_runner(transition_seconds=0.2)
    with pytest.raises(ValueError, match="transition.*velocity"):
        runner.run(lambda timeout: "p" if timeout is None else "", loop=True)
    assert motor.disabled
    assert len(runner.active_intervals) == 1
    assert samples[-1][1] == 1.0


def test_loop_transition_pause_returns_to_gate_without_active_time():
    runner, motor, samples, _events, _config = loop_runner()
    first_gate = True
    paused = False

    def gate(timeout):
        nonlocal first_gate, paused
        if timeout is None:
            if first_gate:
                first_gate = False
                return "p"
            assert paused
            return "q"
        if len(runner.active_intervals) == 1 and samples[-1][1] < 0.8:
            paused = True
            return " "
        return ""

    runner.run(gate, loop=True)
    assert paused and motor.disabled
    assert len(runner.active_intervals) == 1


@pytest.mark.parametrize(
    "name,value",
    [
        ("control_rate_hz", 0),
        ("transition_seconds", float("nan")),
        ("kp", float("inf")),
        ("kd", -1),
    ],
)
def test_runner_rejects_invalid_actual_execution_arguments(name, value):
    with pytest.raises(ValueError, match="finite"):
        make_runner([FakeMotor()], **{name: value})


@pytest.mark.parametrize("default", [[100.0], [float("nan")], [float("inf")], []])
def test_hardware_preflight_rejects_invalid_default_before_loading_files(tmp_path, default):
    config = safety_config()
    config["default_pose_rad"] = default
    with pytest.raises(ValueError, match="default"):
        preflight(tmp_path / "missing.npz", config=config, model_path=None)


@pytest.mark.parametrize(
    "name,value",
    [
        ("control_rate_hz", 0),
        ("control_rate_hz", float("nan")),
        ("transition_seconds", 0),
        ("transition_seconds", float("inf")),
        ("kp", -1),
        ("kp", float("nan")),
        ("kd", float("inf")),
    ],
)
def test_execution_settings_rejected_before_adapter_import(name, value):
    config = safety_config()
    config["hardware"] = {name: value}
    with pytest.raises(ValueError, match=name):
        validate_hardware_config(config)


def command_args(tmp_path):
    path = tmp_path / "unused.npz"
    path.write_bytes(b"only used for metadata stat; preflight is stubbed")
    return SimpleNamespace(
        config=tmp_path / "config.yaml",
        trajectory=path,
        dry_run=False,
        real_hardware=True,
        acknowledge="I_UNDERSTAND_RP1_MOTION",
        run_id="test-run",
        output_dir=tmp_path / "runs",
        loop=False,
    )


def stub_command(monkeypatch, tmp_path):
    args = command_args(tmp_path)
    config = safety_config()
    config["hardware"] = {"adapter_module": "fake_adapter"}
    config["telemetry_gaps"] = {"bus_voltage": "not available", "vibration": "not available"}
    monkeypatch.setattr(hardware_commands, "load_config", lambda _: config)
    monkeypatch.setattr(hardware_commands, "_model_from_args", lambda *_: None)
    monkeypatch.setattr(hardware_commands, "preflight", lambda *_a, **_k: trajectory())
    monkeypatch.setattr(
        hardware_commands, "verify_trajectory_sha", lambda _: {"npz_sha256": "0" * 64}
    )
    return args


class StubArtifacts:
    def __init__(self, *_args):
        pass

    def start_telemetry(self, _fields):
        pass

    def write_event(self, _event):
        pass

    def write_telemetry(self, _row):
        pass

    def finalize(self, **_kwargs):
        return "fake-manifest"


def test_artifact_creation_failure_happens_before_device_initialization(monkeypatch, tmp_path):
    args = stub_command(monkeypatch, tmp_path)
    adapter_calls = []
    monkeypatch.setattr(hardware_commands, "load_adapter", lambda *_: adapter_calls.append(True))

    def fail(*_):
        raise OSError("run directory unavailable")

    monkeypatch.setattr(hardware_commands, "RunArtifacts", fail)
    with pytest.raises(OSError, match="run directory"):
        hardware_commands.hardware_replay(args)
    assert not adapter_calls


def test_runner_construction_failure_cleans_initialized_devices(monkeypatch, tmp_path):
    args = stub_command(monkeypatch, tmp_path)
    motors = [FakeMotor(), FakeMotor()]
    primary = ValueError("runner setup failure")
    monkeypatch.setattr(hardware_commands, "load_adapter", lambda *_: motors)
    monkeypatch.setattr(hardware_commands, "RunArtifacts", StubArtifacts)

    def fail(*_args, **_kwargs):
        raise primary

    monkeypatch.setattr(hardware_commands, "HardwareRunner", fail)
    with pytest.raises(ValueError) as failure:
        hardware_commands.hardware_replay(args)
    assert failure.value is primary
    assert all(motor.disabled and not motor.commands for motor in motors)


def test_recording_failures_do_not_hide_original_setup_failure(monkeypatch, tmp_path):
    args = stub_command(monkeypatch, tmp_path)
    motor = FakeMotor()
    primary = ValueError("runner setup failure")

    class BrokenRecording(StubArtifacts):
        def write_event(self, _event):
            raise OSError("event disk failure")

        def finalize(self, **_kwargs):
            raise OSError("manifest disk failure")

    monkeypatch.setattr(hardware_commands, "load_adapter", lambda *_: [motor])
    monkeypatch.setattr(hardware_commands, "RunArtifacts", BrokenRecording)

    def fail(*_args, **_kwargs):
        raise primary

    monkeypatch.setattr(hardware_commands, "HardwareRunner", fail)
    with pytest.raises(ValueError) as failure:
        hardware_commands.hardware_replay(args)
    assert failure.value is primary
    assert motor.disabled


def test_cli_normal_quit_uses_runner_cleanup_and_finalizes_success(monkeypatch, tmp_path):
    args = stub_command(monkeypatch, tmp_path)
    motor = FakeMotor()
    finalized = []
    clock = Clock()

    class Artifacts(StubArtifacts):
        def finalize(self, **kwargs):
            finalized.append(kwargs)
            return "fake-manifest"

    class Gate:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self, *_args):
            return "q"

    def runner_factory(*runner_args, **kwargs):
        return HardwareRunner(*runner_args, **kwargs, clock=clock, sleep=clock.sleep)

    monkeypatch.setattr(hardware_commands, "load_adapter", lambda *_: [motor])
    monkeypatch.setattr(hardware_commands, "RunArtifacts", Artifacts)
    monkeypatch.setattr(hardware_commands, "HardwareRunner", runner_factory)
    monkeypatch.setattr(hardware_commands, "TerminalGate", Gate)
    assert hardware_commands.hardware_replay(args) == 0
    assert motor.disabled and motor.commands
    assert finalized[0]["status"] == "completed"
    assert finalized[0]["extra"]["active_seconds"] == 0


def test_terminal_setup_failure_cleans_initialized_devices(monkeypatch, tmp_path):
    args = stub_command(monkeypatch, tmp_path)
    motor = FakeMotor()

    class Gate:
        def __enter__(self):
            raise OSError("terminal setup failed")

        def __exit__(self, *_args):
            pass

    monkeypatch.setattr(hardware_commands, "load_adapter", lambda *_: [motor])
    monkeypatch.setattr(hardware_commands, "RunArtifacts", StubArtifacts)
    monkeypatch.setattr(hardware_commands, "TerminalGate", Gate)
    with pytest.raises(OSError, match="terminal setup"):
        hardware_commands.hardware_replay(args)
    assert motor.disabled and not motor.commands
