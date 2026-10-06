import numpy as np

from rp1_test.close_chain import ankle_joint_to_motor, ankle_motor_to_joint


def test_close_chain_round_trip_both_sides():
    expected = np.asarray([-0.2, 0.08])
    for left in (False, True):
        motor = ankle_joint_to_motor(*expected, left=left)
        actual = ankle_motor_to_joint(motor, left=left, initial=expected)
        assert np.allclose(actual, expected, atol=1e-6)
