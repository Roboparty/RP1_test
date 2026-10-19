import numpy as np
import pytest

from rp1_test.trajectory import (
    fingerprint_config,
    fingerprint_file,
    load_trajectory,
    write_trajectory,
)
from rp1_test.validation import validate_trajectory_safety


def config():
    return {
        "limb": "right_arm",
        "joint_mappings": [
            {
                "joint_name": "joint0",
                "motor_index": 0,
                "telemetry_subject_code": "SARM-SHOULDER-PITCH",
                "target_part_code": "SARM",
                "position_min_rad": -1.0,
                "position_max_rad": 1.0,
                "max_velocity_rad_s": 2.0,
                "max_acceleration_rad_s2": 20.0,
            }
        ],
    }


def make(tmp_path, cfg, values, times, *, fps=10.0):
    model = tmp_path / "model.xml"
    model.write_text("<mujoco model='test'/>", encoding="utf-8")
    path = write_trajectory(
        tmp_path / "motion.npz",
        limb="right_arm",
        joint_names=["joint0"],
        motor_pos=np.asarray(values, dtype=float).reshape(-1, 1),
        time=np.asarray(times, dtype=float),
        fps=fps,
        model_fingerprint=fingerprint_file(model),
        config_fingerprint=fingerprint_config(cfg),
        generated_by="pytest",
    )
    return load_trajectory(path), model


def test_safety_validation_checks_all_contracts(tmp_path):
    cfg = config()
    trajectory, model = make(tmp_path, cfg, [0.0, 0.1], [0.0, 0.1])
    result = validate_trajectory_safety(
        trajectory, cfg, model_path=model, require_model_fingerprint=True
    )
    assert result["model_fingerprint_verified"]
    assert result["limits_verified"]
    assert result["timing_verified"]


def test_position_and_timing_failures(tmp_path):
    cfg = config()
    trajectory, model = make(tmp_path, cfg, [0.0, 1.1], [0.0, 0.1])
    with pytest.raises(ValueError, match="position limit"):
        validate_trajectory_safety(
            trajectory, cfg, model_path=model, require_model_fingerprint=True
        )

    trajectory, model = make(
        tmp_path, cfg, [0.0, 0.1, 0.2], [0.0, 0.1, 0.25], fps=10.0
    )
    with pytest.raises(ValueError, match="inconsistent with fps"):
        validate_trajectory_safety(
            trajectory, cfg, model_path=model, require_model_fingerprint=True
        )


def test_model_fingerprint_is_required(tmp_path):
    cfg = config()
    trajectory, _ = make(tmp_path, cfg, [0.0, 0.1], [0.0, 0.1])
    with pytest.raises(ValueError, match="model"):
        validate_trajectory_safety(
            trajectory, cfg, model_path=None, require_model_fingerprint=True
        )
