import json

import numpy as np
import pytest

from rp1_test.trajectory import (
    REQUIRED_KEYS,
    load_trajectory,
    metadata_path,
    write_trajectory,
)


def make_trajectory(path):
    return write_trajectory(
        path,
        limb="test_limb",
        joint_names=["j0", "j1"],
        motor_pos=np.asarray([[0.0, 0.0], [0.1, -0.1]]),
        time=np.asarray([0.0, 0.01]),
        fps=100.0,
        model_fingerprint="model",
        config_fingerprint="config",
        generated_by="pytest",
    )


def test_contract_and_detached_sha(tmp_path):
    path = make_trajectory(tmp_path / "motion.npz")
    loaded = load_trajectory(path)
    assert loaded.motor_pos.shape == (2, 2)
    with np.load(path, allow_pickle=False) as data:
        assert REQUIRED_KEYS.issubset(data.files)
        assert "npz_sha256" not in data.files
        assert "sha256" not in data.files
    metadata = json.loads(metadata_path(path).read_text())
    assert len(metadata["npz_sha256"]) == 64
    assert metadata["npz_size_bytes"] == path.stat().st_size


def test_sha_tampering_is_rejected(tmp_path):
    path = make_trajectory(tmp_path / "motion.npz")
    with path.open("ab") as handle:
        handle.write(b"tamper")
    with pytest.raises(ValueError, match="SHA-256"):
        load_trajectory(path)
