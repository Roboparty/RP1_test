from pathlib import Path

import yaml

from rp1_test.cli import main


def write_config(path: Path, model: Path) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "limb": "test_limb",
                "joint_mappings": [
                    {
                        "joint_name": "j0",
                        "motor_index": 0,
                        "telemetry_subject_code": "SARM-SHOULDER-PITCH",
                        "target_part_code": "SARM",
                        "position_min_rad": -1.0,
                        "position_max_rad": 1.0,
                        "max_velocity_rad_s": 2.0,
                        "max_acceleration_rad_s2": 10.0,
                    }
                ],
                "default_pose_rad": [0.0],
                "motor_sign": [1.0],
                "hardware": {"control_rate_hz": 200},
                "simulation": {"model": model.name},
                "safety": {"require_model_fingerprint": True},
                "telemetry_gaps": {
                    "bus_voltage": "unavailable",
                    "vibration": "unavailable",
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_cli_generate_validate_and_dry_runs(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    model = tmp_path / "model.xml"
    trajectory = tmp_path / "motion.npz"
    model.write_text("<mujoco model='test'/>", encoding="utf-8")
    write_config(config, model)
    assert (
        main(
            [
                "trajectory",
                "generate",
                "--config",
                str(config),
                "--profile",
                "joint-inspect",
                "--output",
                str(trajectory),
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "trajectory",
                "validate",
                str(trajectory),
                "--config",
                str(config),
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "sim",
                "replay",
                str(trajectory),
                "--config",
                str(config),
                "--headless",
                "--dry-run",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "hardware",
                "replay",
                str(trajectory),
                "--config",
                str(config),
                "--dry-run",
            ]
        )
        == 0
    )
    output = capsys.readouterr()
    assert "hardware adapter imported" in output.out
    assert output.err == ""
