import sys
from types import SimpleNamespace

from rp1_test.adapters.roboparty_motors import create_motors


class Driver:
    def __init__(self):
        self.initialized = False
        self.mode = None
        self.command = None
        self.disabled = False

    def init_motor(self):
        self.initialized = True

    def set_motor_control_mode(self, mode):
        self.mode = mode

    def motor_mit_cmd(self, *values):
        self.command = values

    def get_motor_pos(self):
        return 0.1

    def get_motor_spd(self):
        return 0.2

    def get_motor_current(self):
        return 0.3

    def get_motor_temperature(self):
        return 40.0

    def get_error_id(self):
        return 0

    def deinit_motor(self):
        self.disabled = True


def test_builtin_adapter_initializes_mit_and_wraps_feedback(monkeypatch):
    driver = Driver()

    class Factory:
        @staticmethod
        def create_motor(**kwargs):
            assert kwargs["interface"] == "can0"
            return driver

    fake_sdk = SimpleNamespace(
        MotorDriver=Factory,
        MotorControlMode=SimpleNamespace(MIT="MIT"),
    )
    monkeypatch.setitem(sys.modules, "motors_py", fake_sdk)
    config = {
        "joint_mappings": [
            {
                "joint_name": "j0",
                "motor_index": 0,
                "telemetry_subject_code": "SARM-SHOULDER-PITCH",
                "target_part_code": "SARM",
            }
        ],
        "hardware": {
            "motors": [
                {
                    "motor_index": 0,
                    "interface": "can0",
                    "interface_type": "canfd",
                    "backend": "socketcan",
                    "motor_type": "LRO",
                    "motor_model": 0,
                    "motor_id": 1,
                    "master_id_offset": 0,
                    "zero_offset": 0.0,
                    "sign": 1.0,
                    "kp": 12.0,
                    "kd": 0.4,
                }
            ]
        },
    }
    motor = create_motors(config)[0]
    assert driver.initialized and driver.mode == "MIT"
    motor.command_mit(0.5, kp=99.0, kd=99.0)
    assert driver.command == (0.5, 0.0, 12.0, 0.4, 0.0)
    assert motor.get_position() == 0.1
    assert motor.get_speed() == 0.2
    assert motor.get_torque() == 0.3
    assert motor.get_temperature() == 40.0
    assert motor.get_error_id() == 0
    motor.disable()
    assert driver.disabled
