import numpy as np

from rp1_test.interpolation import interpolate_keyframes, minimum_jerk, target_at, transition


def test_minimum_jerk_boundaries_and_monotonicity():
    alpha = np.linspace(0.0, 1.0, 101)
    blend = minimum_jerk(alpha)
    assert blend[0] == 0.0
    assert blend[-1] == 1.0
    assert np.all(np.diff(blend) >= 0.0)


def test_keyframes_and_time_interpolation():
    frames, timestamps = interpolate_keyframes(
        np.asarray([[0.0, 1.0], [1.0, 3.0]]),
        fps=10.0,
        segment_seconds=1.0,
    )
    assert np.allclose(frames[0], [0.0, 1.0])
    assert np.allclose(frames[-1], [1.0, 3.0])
    assert np.allclose(target_at(frames, timestamps, timestamps[5]), frames[5])


def test_transition_has_exact_endpoints():
    rows = transition(np.zeros(2), np.ones(2), fps=20.0, seconds=0.5)
    assert np.allclose(rows[0], 0.0)
    assert np.allclose(rows[-1], 1.0)
