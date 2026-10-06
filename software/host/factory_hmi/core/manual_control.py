"""Threaded, safety-bounded joint control for the factory HMI."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np

from factory_hmi.models import MotorSample, StationConfig, json_number

from ._legacy import (
    limb_joint_to_motor,
    limb_motor_entries,
    limb_motor_to_joint,
)
from .backend import MotorBackend


class ManualControlError(RuntimeError):
    pass


def _sign_scales(config: StationConfig) -> tuple[np.ndarray, np.ndarray]:
    raw_entries = tuple(limb_motor_entries(config.raw, config.limb))
    if len(raw_entries) != len(config.entries):
        raise ManualControlError("manual mapping motor count mismatch")
    raw_signs = np.asarray([entry.sign for entry in raw_entries], dtype=np.float64)
    active_signs = np.asarray(
        [entry.sign for entry in config.entries], dtype=np.float64
    )
    if np.any(np.abs(raw_signs) <= 1e-12) or np.any(
        np.abs(active_signs) <= 1e-12
    ):
        raise ManualControlError("motor signs must be non-zero")
    return active_signs / raw_signs, raw_signs / active_signs


def motor_positions_to_joint(
    config: StationConfig,
    motor_position: np.ndarray | list[float],
) -> np.ndarray:
    _, to_raw_sign = _sign_scales(config)
    raw_motor = np.asarray(motor_position, dtype=np.float64) * to_raw_sign
    return np.asarray(
        limb_motor_to_joint(
            raw_motor.reshape(1, -1),
            config.raw,
            config.limb,
        ),
        dtype=np.float64,
    ).reshape(-1)


def joint_positions_to_motor(
    config: StationConfig,
    joint_position: np.ndarray | list[float],
) -> np.ndarray:
    to_active_sign, _ = _sign_scales(config)
    raw_motor = np.asarray(
        limb_joint_to_motor(
            np.asarray(joint_position, dtype=np.float64).reshape(1, -1),
            config.raw,
            config.limb,
        ),
        dtype=np.float64,
    ).reshape(-1)
    return raw_motor * to_active_sign


def _limit_vector(
    raw: float | list[float] | tuple[float, ...] | np.ndarray | None,
    count: int,
    name: str,
) -> np.ndarray | None:
    if raw is None:
        return None
    values = np.asarray(raw, dtype=np.float64)
    if values.ndim == 0:
        values = np.full(count, float(values), dtype=np.float64)
    else:
        values = values.reshape(-1)
    if values.shape != (count,) or not np.all(np.isfinite(values)):
        raise ManualControlError(f"{name} must be finite scalar or contain {count} values")
    return values


class ManualControlSession:
    """Continuously dispatch smooth joint targets while manual mode is active."""

    def __init__(
        self,
        backend: MotorBackend,
        config: StationConfig,
        initial_motor_position: np.ndarray | list[float],
        *,
        on_fault: Callable[[Exception, list[MotorSample]], None] | None = None,
    ) -> None:
        self.backend = backend
        self.config = config
        self.on_fault = on_fault
        self._count = len(config.entries)
        motor_position = np.asarray(
            initial_motor_position, dtype=np.float64
        ).reshape(-1)
        if motor_position.shape != (self._count,) or not np.all(
            np.isfinite(motor_position)
        ):
            raise ManualControlError("initial motor position is invalid")
        if not backend.connected or not backend.motors_enabled:
            raise ManualControlError(
                "manual control requires connected and enabled motors"
            )

        initial_joint = self.motor_to_joint(motor_position)
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._joint_command = initial_joint.copy()
        self._joint_start = initial_joint.copy()
        self._joint_target = initial_joint.copy()
        self._joint_feedback = initial_joint.copy()
        self._move_started_at = time.monotonic()
        self._move_duration_s = 0.0
        self._speed_rad_s = 0.0
        self._moving = False
        self._last_samples: list[MotorSample] = []
        self._fault: str | None = None

    def motor_to_joint(self, motor_position: np.ndarray) -> np.ndarray:
        return motor_positions_to_joint(self.config, motor_position)

    def joint_to_motor(self, joint_position: np.ndarray) -> np.ndarray:
        return joint_positions_to_motor(self.config, joint_position)

    @property
    def active(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._fault = None
            thread = threading.Thread(
                target=self._run,
                name=f"factory-manual-{self.config.limb}",
                daemon=True,
            )
            self._thread = thread
            thread.start()

    def move(self, targets_rad: np.ndarray | list[float], speed_rad_s: float) -> None:
        targets = np.asarray(targets_rad, dtype=np.float64).reshape(-1)
        if targets.shape != (self._count,) or not np.all(np.isfinite(targets)):
            raise ManualControlError(
                f"manual target must contain {self._count} finite values"
            )
        speed = float(speed_rad_s)
        if not math.isfinite(speed) or speed <= 0.0:
            raise ManualControlError("manual speed must be positive and finite")
        self._validate_target(targets, speed)
        with self._lock:
            now = time.monotonic()
            current = self._command_at(now)
            distance = float(np.max(np.abs(targets - current)))
            self._joint_command = current
            self._joint_start = current.copy()
            self._joint_target = targets.copy()
            self._move_started_at = now
            # Smoothstep reaches a peak derivative of 1.5. Scale the duration so
            # the operator-selected speed remains the actual peak joint speed.
            self._move_duration_s = (
                1.5 * distance / speed if distance > 0.0 else 0.0
            )
            self._speed_rad_s = speed
            self._moving = self._move_duration_s > 0.0

    def stop(self, timeout: float = 2.0) -> list[MotorSample]:
        self._stop_event.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(float(timeout))
            if thread.is_alive():
                raise TimeoutError("manual control thread did not stop in time")
        with self._lock:
            self._moving = False
            return list(self._last_samples)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active": self._thread is not None and self._thread.is_alive(),
                "moving": bool(self._moving),
                "targets_rad": [
                    json_number(float(value)) for value in self._joint_target
                ],
                "commands_rad": [
                    json_number(float(value)) for value in self._joint_command
                ],
                "positions_rad": [
                    json_number(float(value)) for value in self._joint_feedback
                ],
                "speed_rad_s": json_number(self._speed_rad_s),
                "fault": self._fault,
            }

    def _command_at(self, now: float) -> np.ndarray:
        if not self._moving or self._move_duration_s <= 0.0:
            return self._joint_target.copy()
        fraction = min(
            1.0,
            max(0.0, (now - self._move_started_at) / self._move_duration_s),
        )
        blend = fraction * fraction * (3.0 - 2.0 * fraction)
        command = self._joint_start + (self._joint_target - self._joint_start) * blend
        if fraction >= 1.0:
            self._moving = False
        return command

    def _validate_target(self, joint_target: np.ndarray, speed: float) -> None:
        limits = self.config.safety_limits
        joint_min = _limit_vector(limits.joint_min_rad, self._count, "joint_min_rad")
        joint_max = _limit_vector(limits.joint_max_rad, self._count, "joint_max_rad")
        if joint_min is not None and np.any(joint_target < joint_min):
            index = int(np.flatnonzero(joint_target < joint_min)[0])
            raise ManualControlError(
                f"joint[{index}] target {joint_target[index]:.6f} "
                f"is below joint_min_rad={joint_min[index]:.6f}"
            )
        if joint_max is not None and np.any(joint_target > joint_max):
            index = int(np.flatnonzero(joint_target > joint_max)[0])
            raise ManualControlError(
                f"joint[{index}] target {joint_target[index]:.6f} "
                f"is above joint_max_rad={joint_max[index]:.6f}"
            )
        motor_target = self.joint_to_motor(joint_target)
        motor_min = _limit_vector(limits.motor_min_rad, self._count, "motor_min_rad")
        motor_max = _limit_vector(limits.motor_max_rad, self._count, "motor_max_rad")
        if motor_min is not None and np.any(motor_target < motor_min):
            index = int(np.flatnonzero(motor_target < motor_min)[0])
            raise ManualControlError(
                f"motor[{index}] mapped target {motor_target[index]:.6f} "
                f"is below motor_min_rad={motor_min[index]:.6f}"
            )
        if motor_max is not None and np.any(motor_target > motor_max):
            index = int(np.flatnonzero(motor_target > motor_max)[0])
            raise ManualControlError(
                f"motor[{index}] mapped target {motor_target[index]:.6f} "
                f"is above motor_max_rad={motor_max[index]:.6f}"
            )
        velocity_limit = _limit_vector(
            limits.max_velocity_rad_s,
            self._count,
            "max_velocity_rad_s",
        )
        if velocity_limit is not None and speed > float(np.min(velocity_limit)):
            raise ManualControlError(
                f"manual speed {speed:.6f} exceeds configured maximum "
                f"{float(np.min(velocity_limit)):.6f}"
            )

    def _run(self) -> None:
        period = 1.0 / float(self.config.control_rate_hz)
        next_tick = time.monotonic()
        last_sample_at = float("-inf")
        samples: list[MotorSample] = []
        try:
            while not self._stop_event.is_set():
                now = time.monotonic()
                with self._lock:
                    joint_command = self._command_at(now)
                    self._joint_command = joint_command.copy()
                self.backend.command_mit(self.joint_to_motor(joint_command))
                if now - last_sample_at >= 0.05:
                    samples = self.backend.read_samples()
                    faulted = [sample for sample in samples if sample.error_id != 0]
                    if faulted:
                        raise ManualControlError(
                            "manual feedback contains errors: "
                            + ", ".join(
                                f"id={sample.motor_id}:error_id={sample.error_id}"
                                for sample in faulted
                            )
                        )
                    stale = [
                        sample for sample in samples if sample.feedback_age_s > 2.0
                    ]
                    if stale:
                        raise ManualControlError(
                            "manual feedback timed out: "
                            + ", ".join(
                                f"id={sample.motor_id}:age={sample.feedback_age_s:.3f}s"
                                for sample in stale
                            )
                        )
                    feedback = self.motor_to_joint(
                        np.asarray(
                            [sample.pos_rad for sample in samples],
                            dtype=np.float64,
                        )
                    )
                    with self._lock:
                        self._last_samples = list(samples)
                        self._joint_feedback = feedback
                    last_sample_at = now
                next_tick += period
                delay = next_tick - time.monotonic()
                if delay > 0.0:
                    self._stop_event.wait(delay)
                elif -delay > period * 4.0:
                    next_tick = time.monotonic()
        except Exception as exc:
            with self._lock:
                self._fault = str(exc)
                self._moving = False
            if not self._stop_event.is_set() and self.on_fault is not None:
                self.on_fault(exc, samples)
        finally:
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None
                self._moving = False


__all__ = [
    "ManualControlError",
    "ManualControlSession",
    "joint_positions_to_motor",
    "motor_positions_to_joint",
]
