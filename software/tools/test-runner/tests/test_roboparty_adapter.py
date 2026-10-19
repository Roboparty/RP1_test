import sys
from types import SimpleNamespace

import pytest

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


def adapter_config(count=2):
    return {
        "joint_mappings": [
            {
                "joint_name": f"j{index}",
                "motor_index": index,
                "telemetry_subject_code": f"SARM-J{index}",
                "target_part_code": "SARM",
            }
            for index in range(count)
        ],
        "hardware": {
            "motors": [
                {
                    "motor_index": index,
                    "interface": "can0",
                    "interface_type": "canfd",
                    "motor_type": "LRO",
                    "motor_model": 0,
                    "motor_id": index + 1,
                    "zero_offset": 0.0,
                    "sign": 1.0,
                    "kp": 12.0,
                    "kd": 0.4,
                }
                for index in range(count)
            ]
        },
    }


@pytest.mark.parametrize("phase", ["init", "mode", "interrupt"])
def test_partial_initialization_rolls_back_current_and_previous_driver(monkeypatch, phase):
    drivers = [Driver(), Driver()]
    primary = KeyboardInterrupt() if phase == "interrupt" else OSError("initialization failed")
    cleanup_attempts = []

    def fail(*_):
        raise primary

    if phase == "init":
        drivers[1].init_motor = fail
    else:
        drivers[1].set_motor_control_mode = fail

    def failed_cleanup():
        cleanup_attempts.append(1)
        raise RuntimeError("rollback failed")

    drivers[1].deinit_motor = failed_cleanup
    selected = iter(drivers)
    sdk = SimpleNamespace(
        MotorDriver=SimpleNamespace(create_motor=lambda **_: next(selected)),
        MotorControlMode=SimpleNamespace(MIT="MIT"),
    )
    monkeypatch.setitem(sys.modules, "motors_py", sdk)
    with pytest.raises(type(primary)) as failure:
        create_motors(adapter_config())
    assert failure.value is primary
    assert cleanup_attempts == [1]
    assert drivers[0].disabled


@pytest.mark.parametrize(
    "field,value",
    [
        ("sign", float("nan")),
        ("zero_offset", float("inf")),
        ("kp", float("nan")),
        ("kd", -float("inf")),
    ],
)
def test_invalid_motor_parameters_do_not_import_sdk(monkeypatch, field, value):
    from rp1_test.adapters import roboparty_motors

    imported = []
    monkeypatch.setattr(roboparty_motors, "import_motors_py", lambda: imported.append(True))
    config = adapter_config(1)
    config["hardware"]["motors"][0][field] = value
    with pytest.raises(ValueError, match="finite"):
        create_motors(config)
    assert not imported
