from pathlib import Path

import numpy as np
import pytest
import yaml

from rp1_test.mapping import capability_gaps, error_event, load_joint_mappings
from rp1_test.units import deg_to_rad, rad_to_deg


def config():
    return {
        "joint_mappings": [
            {
                "joint_name": "j0",
                "motor_index": 0,
                "telemetry_subject_code": "SARM-SHOULDER-PITCH",
                "target_part_code": "SARM",
            },
            {
                "joint_name": "j1",
                "motor_index": 1,
                "telemetry_subject_code": "SARM-SHOULDER-ROLL",
                "target_part_code": "SARM",
            },
        ],
        "telemetry_gaps": {
            "bus_voltage": "adapter-dependent",
            "vibration": "sensor absent",
        },
    }


def test_explicit_mapping_and_error_event():
    mappings = load_joint_mappings(config())
    assert mappings[1].target_part_code == "SARM"
    assert capability_gaps(config())["vibration"] == "sensor absent"
    event = error_event(mappings[0], 7, elapsed_s=1.25)
    assert event["error_id"] == 7
    assert event["telemetry_subject_code"] == "SARM-SHOULDER-PITCH"


def test_non_contiguous_mapping_rejected():
    value = config()
    value["joint_mappings"][1]["motor_index"] = 3
    with pytest.raises(ValueError, match="contiguous"):
        load_joint_mappings(value)


def test_platform_code_constraints_are_enforced():
    value = config()
    value["joint_mappings"][0]["telemetry_subject_code"] = "BAD.CODE"
    with pytest.raises(ValueError, match="telemetry_subject_code"):
        load_joint_mappings(value)
    value = config()
    value["joint_mappings"][0]["target_part_code"] = "RARM"
    with pytest.raises(ValueError, match="target_part_code"):
        load_joint_mappings(value)


def test_units_round_trip():
    degrees = np.asarray([-180.0, 0.0, 90.0])
    assert np.allclose(rad_to_deg(deg_to_rad(degrees)), degrees)


def test_example_right_arm_uses_stable_platform_codes():
    path = Path(__file__).parents[1] / "config" / "rp1_test.example.yaml"
    mappings = load_joint_mappings(yaml.safe_load(path.read_text(encoding="utf-8")))
    assert len(mappings) == 7
    assert all(item.target_part_code == "SARM" for item in mappings)
    assert [item.telemetry_subject_code for item in mappings] == [
        "SARM-SHOULDER-PITCH",
        "SARM-SHOULDER-ROLL",
        "SARM-SHOULDER-YAW",
        "SARM-ELBOW-PITCH",
        "SARM-WRIST-YAW",
        "SARM-WRIST-PITCH",
        "SARM-WRIST-ROLL",
    ]
