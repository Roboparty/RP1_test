"""Hardware replay control core.

No vendor SDK is imported here. Real use requires an explicit adapter module and
an explicit CLI acknowledgement; tests use fake motors only.
"""

from __future__ import annotations

import importlib
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from .interpolation import target_at, transition
from .mapping import JointMapping
from .trajectory import Trajectory, load_trajectory, verify_trajectory_sha
from .validation import validate_trajectory_safety
from .watchdog import FeedbackWatchdog


class Motor(Protocol):
    def command_mit(self, position_rad: float, *, kp: float, kd: float) -> None: ...
    def get_position(self) -> float: ...
    def get_speed(self) -> float: ...
    def get_torque(self) -> float: ...
    def get_temperature(self) -> float: ...
    def get_error_id(self) -> int: ...
    def disable(self) -> None: ...


def preflight(
    trajectory_path: Path,
    *,
    config: dict[str, Any],
    model_path: Path | None,
    require_sha: bool = True,
) -> Trajectory:
    trajectory = load_trajectory(trajectory_path, verify_metadata=require_sha)
    validate_trajectory_safety(
        trajectory,
        config,
        model_path=model_path,
        require_model_fingerprint=True,
    )
    if require_sha:
        verify_trajectory_sha(trajectory_path)
    return trajectory


def load_adapter(module_name: str, config: dict[str, Any]) -> list[Motor]:
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        raise RuntimeError(f"failed to import hardware adapter {module_name!r}: {exc}") from exc
    factory = getattr(module, "create_motors", None)
    if factory is None:
        raise RuntimeError("hardware adapter must export create_motors(config)")
    motors = list(factory(config))
    return motors


class HardwareRunner:
    def __init__(
        self,
        motors: list[Motor],
        mappings: list[JointMapping],
        trajectory: Trajectory,
        *,
        default_pose: np.ndarray,
        control_rate_hz: float = 200.0,
        kp: float = 20.0,
        kd: float = 0.5,
        transition_seconds: float = 2.0,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
        telemetry_sink: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if len(motors) != len(mappings):
            raise ValueError("motors/mappings length mismatch")
        self.motors = motors
        self.mappings = mappings
        self.trajectory = trajectory
        self.default_pose = np.asarray(default_pose, dtype=np.float64).reshape(-1)
        if self.default_pose.shape != (len(motors),):
            raise ValueError("default_pose length mismatch")
        self.rate = float(control_rate_hz)
        self.kp = float(kp)
        self.kd = float(kd)
        self.transition_seconds = float(transition_seconds)
        self.clock = clock
        self.sleep = sleep
        self.event_sink = event_sink or (lambda event: None)
        self.telemetry_sink = telemetry_sink or (lambda row: None)
        self._telemetry_started = self.clock()
        self.active_intervals: list[dict[str, float]] = []
        self.watchdog = FeedbackWatchdog(motors, mappings, clock=clock, event_sink=self.event_sink)

    @staticmethod
    def _optional_float(motor: Motor, getter_name: str) -> float:
        getter = getattr(motor, getter_name, None)
        if getter is None:
            return float("nan")
        try:
            return float(getter())
        except Exception:
            return float("nan")

    @staticmethod
    def _optional_error_id(motor: Motor) -> int | float:
        try:
            return int(motor.get_error_id())
        except Exception:
            return float("nan")

    def _command(self, target: np.ndarray, *, elapsed_s: float) -> None:
        for motor, mapping, value in zip(self.motors, self.mappings, target):
            motor.command_mit(float(value), kp=self.kp, kd=self.kd)
            self.telemetry_sink(
                {
                    "time_s": float(self.clock() - self._telemetry_started),
                    "joint_name": mapping.joint_name,
                    "motor_index": mapping.motor_index,
                    "cmd_pos_rad": float(value),
                    "pos_rad": float(motor.get_position()),
                    "spd_rad_s": self._optional_float(motor, "get_speed"),
                    "torque_nm": self._optional_float(motor, "get_torque"),
                    "temp_c": self._optional_float(motor, "get_temperature"),
                    "error_id": self._optional_error_id(motor),
                }
            )
        if not self.watchdog.check(target, elapsed_s=elapsed_s):
            raise RuntimeError(self.watchdog.fault or "watchdog fault")

    @staticmethod
    def _read_key(key_source: Callable[..., str], timeout: float | None) -> str:
        try:
            return str(key_source(timeout)).lower()
        except TypeError:
            return str(key_source()).lower()

    def _play_rows(
        self,
        rows: np.ndarray,
        timestamps: np.ndarray,
        key_source: Callable[..., str] | None = None,
        *,
        task_active: bool = False,
    ) -> tuple[str, np.ndarray]:
        if not task_active:
            return self._play_rows_inner(rows, timestamps, key_source)
        started = self.clock() - self._telemetry_started
        self.event_sink({"type": "ACTIVE_START", "elapsed_s": started})
        try:
            return self._play_rows_inner(rows, timestamps, key_source)
        finally:
            ended = self.clock() - self._telemetry_started
            self.active_intervals.append({"start_s": started, "end_s": ended})
            self.event_sink({"type": "ACTIVE_END", "elapsed_s": ended})

    def _play_rows_inner(
        self,
        rows: np.ndarray,
        timestamps: np.ndarray,
        key_source: Callable[..., str] | None = None,
    ) -> tuple[str, np.ndarray]:
        started = self.clock()
        period = 1.0 / self.rate
        target = np.asarray(rows[0], dtype=np.float64)
        while True:
            elapsed = self.clock() - started
            target = target_at(rows, timestamps, elapsed)
            self._command(target, elapsed_s=elapsed)
            if key_source is not None:
                key = self._read_key(key_source, 0.0)
                if key == "q":
                    return "quit", target
                if key == "r":
                    return "reset", target
                if key == " ":
                    return "pause", target
            if elapsed >= float(timestamps[-1]):
                return "done", target
            self.sleep(period)

    def return_default(self, current: np.ndarray) -> None:
        rows = transition(
            current,
            self.default_pose,
            fps=self.rate,
            seconds=self.transition_seconds,
        )
        self._play_rows(rows, np.arange(rows.shape[0]) / self.rate)

    def run(self, key_source: Callable[..., str], *, loop: bool = False) -> None:
        """Local gate: R homes, P starts/resumes, Space pauses, Q exits."""
        current = self.default_pose.copy()
        try:
            while True:
                key = self._read_key(key_source, None)
                if key == "q":
                    return
                if key == "r":
                    self.return_default(current)
                    current = self.default_pose.copy()
                elif key == "p":
                    entry = transition(
                        current,
                        self.trajectory.motor_pos[0],
                        fps=self.rate,
                        seconds=self.transition_seconds,
                    )
                    action, current = self._play_rows(
                        entry, np.arange(entry.shape[0]) / self.rate, key_source
                    )
                    if action == "quit":
                        return
                    if action == "reset":
                        self.return_default(current)
                        current = self.default_pose.copy()
                        continue
                    if action == "pause":
                        continue
                    while True:
                        action, current = self._play_rows(
                            self.trajectory.motor_pos,
                            self.trajectory.time,
                            key_source,
                            task_active=True,
                        )
                        if action == "quit":
                            return
                        if action == "reset":
                            self.return_default(current)
                            current = self.default_pose.copy()
                            break
                        if action == "pause":
                            break
                        if not loop:
                            self.return_default(current)
                            current = self.default_pose.copy()
                            break
                elif key == " ":
                    self._command(current, elapsed_s=0.0)
        finally:
            try:
                self.return_default(current)
            finally:
                for motor in self.motors:
                    motor.disable()
