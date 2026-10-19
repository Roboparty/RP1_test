import numpy as np

from rp1_test.hardware import HardwareRunner
from rp1_test.mapping import JointMapping
from rp1_test.trajectory import load_trajectory, write_trajectory
from rp1_test.watchdog import FeedbackWatchdog


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeMotor:
    def __init__(self, *, follow=True):
        self.position = 0.0
        self.error_id = 0
        self.follow = follow
        self.disabled = False
        self.commands = []

    def command_mit(self, position_rad, *, kp, kd):
        self.commands.append((position_rad, kp, kd))
        if self.follow:
            self.position = position_rad

    def get_position(self):
        return self.position

    def get_speed(self):
        return 0.25

    def get_torque(self):
        return 1.5

    def get_temperature(self):
        return 36.0

    def get_error_id(self):
        return self.error_id

    def disable(self):
        self.disabled = True


MAPPING = JointMapping("j0", 0, "SARM-SHOULDER-PITCH", "SARM")


def safety_config(mappings=None, default_pose=None):
    selected = mappings or [MAPPING]
    return {
        "limb": "test",
        "default_pose_rad": default_pose or [0.0] * len(selected),
        "joint_mappings": [
            {
                **mapping.__dict__,
                "position_min_rad": -2.0,
                "position_max_rad": 2.0,
                "max_velocity_rad_s": 20.0,
                "max_acceleration_rad_s2": 1000.0,
            }
            for mapping in selected
        ],
    }


def test_watchdog_emits_error_id_as_event():
    motor = FakeMotor()
    motor.error_id = 9
    events = []
    watchdog = FeedbackWatchdog([motor], [MAPPING], event_sink=events.append)
    assert not watchdog.check(np.asarray([0.0]), elapsed_s=0.2)
    assert events == [
        {
            "type": "motor_error",
            "elapsed_s": 0.2,
            "joint_name": "j0",
            "telemetry_subject_code": "SARM-SHOULDER-PITCH",
            "target_part_code": "SARM",
            "error_id": 9,
        }
    ]


def test_watchdog_detects_stale_feedback():
    clock = Clock()
    motor = FakeMotor(follow=False)
    watchdog = FeedbackWatchdog(
        [motor],
        [MAPPING],
        stale_seconds=1.0,
        tracking_error_rad=0.1,
        command_motion_rad=0.05,
        clock=clock,
    )
    assert watchdog.check(np.asarray([0.0]), elapsed_s=0.0)
    clock.now = 1.1
    assert not watchdog.check(np.asarray([0.2]), elapsed_s=1.1)
    assert "stale" in watchdog.fault


def test_runner_defaults_to_200hz_and_disables(tmp_path):
    path = write_trajectory(
        tmp_path / "motion.npz",
        limb="test",
        joint_names=["j0"],
        motor_pos=np.asarray([[0.0], [0.1]]),
        time=np.asarray([0.0, 0.01]),
        fps=100.0,
        model_fingerprint="m",
        config_fingerprint="c",
        generated_by="pytest",
    )
    motor = FakeMotor()
    clock = Clock()
    telemetry = []
    runner = HardwareRunner(
        [motor],
        [MAPPING],
        load_trajectory(path),
        default_pose=np.asarray([0.0]),
        safety_config=safety_config(),
        clock=clock,
        sleep=clock.sleep,
        telemetry_sink=telemetry.append,
    )
    assert runner.rate == 200.0
    runner.run(lambda: "q")
    assert motor.disabled
    assert motor.commands
    assert {
        "cmd_pos_rad",
        "pos_rad",
        "spd_rad_s",
        "torque_nm",
        "temp_c",
        "error_id",
    }.issubset(telemetry[0])
