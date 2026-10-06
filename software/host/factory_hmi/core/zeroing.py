"""UI-neutral, per-motor zero-calibration session."""

from __future__ import annotations

import threading
import time
from typing import Any

from factory_hmi.models import MotorSample, StationState, ZeroingState

from .backend import MotorBackend, MotorSafetyError
from .state_machine import StationStateMachine


class ZeroingSessionError(RuntimeError):
    pass


class MotorZeroingSession:
    """Explicit prepare/read/confirm workflow; no terminal or ``termios`` use."""

    def __init__(
        self,
        backend: MotorBackend,
        *,
        station_state: StationStateMachine | None = None,
    ) -> None:
        if not backend.connected:
            raise ZeroingSessionError("backend must be connected before zeroing")
        if station_state is not None:
            station_state.require(StationState.CONNECTED)
        self.backend = backend
        self.station_state = station_state
        self._lock = threading.RLock()
        self._state = ZeroingState.IDLE
        self._current_index: int | None = None
        self._next_index = 0
        self._zeroed: list[int] = []
        self._skipped: list[int] = []
        self._last_sample: MotorSample | None = None
        self._abort_reason: str | None = None

    @property
    def state(self) -> ZeroingState:
        with self._lock:
            return self._state

    @property
    def current_index(self) -> int | None:
        with self._lock:
            return self._current_index

    def prepare(self, index: int | None = None) -> dict[str, Any]:
        with self._lock:
            if self._state not in {ZeroingState.IDLE, ZeroingState.PREPARED}:
                raise ZeroingSessionError(
                    f"prepare requires idle/prepared, current={self._state.value}"
                )
            selected = self._next_index if index is None else int(index)
            if not 0 <= selected < len(self.backend.entries):
                raise IndexError(f"motor index {selected} is out of range")
            if selected in self._zeroed or selected in self._skipped:
                raise ZeroingSessionError(f"motor[{selected}] was already handled")
            self.backend.disable(selected)
            self._current_index = selected
            self._next_index = selected
            self._last_sample = None
            self._state = ZeroingState.READING
            entry = self.backend.entries[selected]
            return {
                "index": selected,
                "motor_id": int(entry.motor_id),
                "joint_name": entry.joint_name,
                "bus": entry.interface,
                "instruction": "motor disabled; move it by hand, then read and confirm or skip",
            }

    def read(self) -> MotorSample:
        with self._lock:
            self._require_reading()
            assert self._current_index is not None
            self._last_sample = self.backend.read_samples()[self._current_index]
            return self._last_sample

    def confirm(self, *, force_on_error: bool = False) -> bool:
        with self._lock:
            self._require_reading()
            assert self._current_index is not None
            sample = self.backend.read_samples()[self._current_index]
            self._last_sample = sample
            if sample.error_id != 0 and not force_on_error:
                raise MotorSafetyError(
                    f"refusing to zero motor[{self._current_index}] while "
                    f"error_id={sample.error_id}; clear the fault or explicitly force"
                )
            result = self.backend.zero_motor(
                self._current_index, force_on_error=force_on_error
            )
            if not result:
                raise ZeroingSessionError(
                    f"motor[{self._current_index}] set-zero command was not acknowledged"
                )
            self._zeroed.append(self._current_index)
            self._advance()
            return True

    def skip(self) -> int:
        with self._lock:
            self._require_reading()
            assert self._current_index is not None
            skipped = self._current_index
            self._skipped.append(skipped)
            self._advance()
            return skipped

    def abort(self, reason: str = "operator abort") -> None:
        with self._lock:
            # Terminal sessions are already finished; treat abort as a no-op so
            # the HMI does not surface a 409 after the last motor is confirmed.
            if self._state in {ZeroingState.COMPLETED, ZeroingState.ABORTED}:
                return
            if self._current_index is not None:
                self.backend.disable(self._current_index)
            self._abort_reason = str(reason)
            self._current_index = None
            self._state = ZeroingState.ABORTED

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            handled = set(self._zeroed) | set(self._skipped)
            remaining = [
                index
                for index in range(len(self.backend.entries))
                if index not in handled
            ]
            return {
                "state": self._state.value,
                "current_index": self._current_index,
                "next_index": self._next_index,
                "zeroed_indices": list(self._zeroed),
                "skipped_indices": list(self._skipped),
                "remaining_indices": remaining,
                "last_sample": (
                    self._last_sample.to_dict() if self._last_sample else None
                ),
                "abort_reason": self._abort_reason,
                "timestamp_s": time.monotonic(),
            }

    def _require_reading(self) -> None:
        if self._state is not ZeroingState.READING:
            raise ZeroingSessionError(
                f"operation requires reading state, current={self._state.value}"
            )

    def _advance(self) -> None:
        assert self._current_index is not None
        previous = self._current_index
        handled = set(self._zeroed) | set(self._skipped)
        self._current_index = None
        self._last_sample = None
        total = len(self.backend.entries)
        if len(handled) >= total:
            self._next_index = total
            self._state = ZeroingState.COMPLETED
            return
        # Prefer the next unhandled motor after the one just finished, then wrap.
        # Completing only the last selected motor must not end the whole session
        # while earlier motors still need prepare/confirm/skip.
        for offset in range(1, total + 1):
            candidate = (previous + offset) % total
            if candidate not in handled:
                self._next_index = candidate
                self._state = ZeroingState.PREPARED
                return
        self._next_index = total
        self._state = ZeroingState.COMPLETED


# Short name suitable for HMI service layers.
ZeroingSession = MotorZeroingSession


__all__ = [
    "MotorZeroingSession",
    "ZeroingSession",
    "ZeroingSessionError",
]
