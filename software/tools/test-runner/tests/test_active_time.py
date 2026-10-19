from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from test_fake_motor_watchdog import Clock, FakeMotor, safety_config

from rp1_test.hardware import HardwareRunner
from rp1_test.mapping import JointMapping
from rp1_test.run_ingestion import _finish_payload
from rp1_test.trajectory import load_trajectory, write_trajectory


def test_active_time_excludes_entry_home_and_operator_wait(tmp_path):
    path = write_trajectory(
        tmp_path / "motion.npz",
        limb="test",
        joint_names=["j0"],
        motor_pos=np.asarray([[0.0], [0.1]]),
        time=np.asarray([0.0, 0.1]),
        fps=10.0,
        model_fingerprint="m",
        config_fingerprint="c",
        generated_by="pytest",
    )
    clock = Clock()
    runner = HardwareRunner(
        [FakeMotor()],
        [JointMapping("j0", 0, "SARM-SHOULDER-PITCH", "SARM")],
        load_trajectory(path),
        default_pose=np.asarray([0.0]),
        safety_config=safety_config(),
        control_rate_hz=100,
        transition_seconds=0.1,
        clock=clock,
        sleep=clock.sleep,
    )
    sequence = iter(["p", "q"])

    def gate(timeout):
        if timeout == 0:
            return ""
        clock.sleep(1.0)  # Operator waiting is wall time only.
        return next(sequence)

    runner.run(gate)
    active = sum(item["end_s"] - item["start_s"] for item in runner.active_intervals)
    assert 0.1 <= active < 0.12
    assert active < clock.now / 10


def test_completed_tool_is_inconclusive_and_requires_confirmed_counter():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    metadata = {
        "run_id": "run",
        "status": "completed",
        "ended_at": (start + timedelta(seconds=60)).isoformat(),
    }
    result = _finish_payload(metadata, [{"time_s": 60}], start)
    assert result["active_seconds"] == 0
    assert result["outcome"] == "INCONCLUSIVE"
    metadata.update(status="passed", source_kind="synthetic", active_seconds=30)
    result = _finish_payload(metadata, [], start)
    assert result["outcome"] == "INCONCLUSIVE"
    assert result["active_seconds"] == 30
    metadata["active_seconds"] = float("nan")
    with pytest.raises(ValueError, match="finite"):
        _finish_payload(metadata, [], start)
