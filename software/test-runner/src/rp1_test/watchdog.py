"""Motor feedback watchdog with injectable clock and event sink."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

from .mapping import JointMapping, error_event


class FeedbackMotor(Protocol):
    def get_position(self) -> float: ...
    def get_error_id(self) -> int: ...


@dataclass
class _State:
    position: float | None = None
    command_at_change: float | None = None
    changed_at: float = 0.0
    error_capability_reported: bool = False


class FeedbackWatchdog:
    def __init__(
        self,
        motors: list[FeedbackMotor],
        mappings: list[JointMapping],
        *,
        stale_seconds: float = 2.0,
        tracking_error_rad: float = 0.5,
        command_motion_rad: float = 0.05,
        clock: Callable[[], float] = time.monotonic,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if len(motors) != len(mappings):
            raise ValueError("motors/mappings length mismatch")
        self.motors = motors
        self.mappings = mappings
        self.stale_seconds = float(stale_seconds)
        self.tracking_error_rad = float(tracking_error_rad)
        self.command_motion_rad = float(command_motion_rad)
        self.clock = clock
        self.event_sink = event_sink or (lambda event: None)
        now = clock()
        self.states = [_State(changed_at=now) for _ in motors]
        self.fault: str | None = None

    def check(self, command: np.ndarray, *, elapsed_s: float) -> bool:
        now = self.clock()
        values = np.asarray(command, dtype=np.float64).reshape(-1)
        for index, (motor, mapping, state) in enumerate(
            zip(self.motors, self.mappings, self.states)
        ):
            position = float(motor.get_position())
            try:
                raw_error_id = float(motor.get_error_id())
            except Exception:
                raw_error_id = float("nan")
            if not np.isfinite(raw_error_id):
                if not state.error_capability_reported:
                    self.event_sink(
                        {
                            "type": "telemetry_capability_gap",
                            "elapsed_s": float(elapsed_s),
                            "joint_name": mapping.joint_name,
                            "field": "error_id",
                        }
                    )
                    state.error_capability_reported = True
            elif int(raw_error_id):
                error_id = int(raw_error_id)
                event = error_event(mapping, error_id, elapsed_s=elapsed_s)
                self.event_sink(event)
                self.fault = f"{mapping.joint_name}: motor error_id={error_id}"
                return False
            if state.position is None or abs(position - state.position) > 1e-6:
                state.position = position
                state.command_at_change = float(values[index])
                state.changed_at = now
                continue
            moved_command = (
                state.command_at_change is not None
                and abs(float(values[index]) - state.command_at_change) >= self.command_motion_rad
            )
            tracking_error = abs(float(values[index]) - position)
            if (
                moved_command
                and tracking_error >= self.tracking_error_rad
                and now - state.changed_at >= self.stale_seconds
            ):
                event = {
                    "type": "feedback_stale",
                    "elapsed_s": float(elapsed_s),
                    "joint_name": mapping.joint_name,
                    "motor_index": mapping.motor_index,
                    "tracking_error_rad": tracking_error,
                }
                self.event_sink(event)
                self.fault = f"{mapping.joint_name}: stale feedback"
                return False
        return True
