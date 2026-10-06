"""Thread-safe factory-station state machine."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable

from factory_hmi.models import StationState


class InvalidStateTransition(RuntimeError):
    pass


ALLOWED_TRANSITIONS: dict[StationState, frozenset[StationState]] = {
    StationState.DISCONNECTED: frozenset(
        {StationState.CONNECTED, StationState.FAULT}
    ),
    StationState.CONNECTED: frozenset(
        {
            StationState.ARMED,
            StationState.DISCONNECTED,
            StationState.FAULT,
        }
    ),
    StationState.ARMED: frozenset(
        {
            StationState.CONNECTED,
            StationState.RUNNING,
            StationState.DISCONNECTED,
            StationState.FAULT,
        }
    ),
    StationState.RUNNING: frozenset(
        {
            StationState.PAUSED,
            StationState.STOPPING,
            StationState.COMPLETED,
            StationState.FAULT,
        }
    ),
    StationState.PAUSED: frozenset(
        {
            StationState.RUNNING,
            StationState.STOPPING,
            StationState.FAULT,
        }
    ),
    StationState.STOPPING: frozenset(
        {
            StationState.COMPLETED,
            StationState.DISCONNECTED,
            StationState.FAULT,
        }
    ),
    StationState.COMPLETED: frozenset(
        {
            StationState.ARMED,
            StationState.CONNECTED,
            StationState.DISCONNECTED,
            StationState.FAULT,
        }
    ),
    StationState.FAULT: frozenset(
        {
            StationState.CONNECTED,
            StationState.DISCONNECTED,
        }
    ),
}


class StationStateMachine:
    def __init__(self, initial: StationState | str = StationState.DISCONNECTED) -> None:
        self._state = StationState(initial)
        self._version = 0
        self._changed = threading.Condition(threading.RLock())

    @property
    def state(self) -> StationState:
        with self._changed:
            return self._state

    @property
    def version(self) -> int:
        with self._changed:
            return self._version

    def require(self, allowed: StationState | str | Iterable[StationState | str]) -> StationState:
        if isinstance(allowed, (StationState, str)):
            states = {StationState(allowed)}
        else:
            states = {StationState(item) for item in allowed}
        with self._changed:
            if self._state not in states:
                expected = ", ".join(sorted(item.value for item in states))
                raise InvalidStateTransition(
                    f"operation requires state [{expected}], current state is {self._state.value}"
                )
            return self._state

    def transition(
        self,
        target: StationState | str,
        *,
        expected: StationState | str | Iterable[StationState | str] | None = None,
    ) -> StationState:
        next_state = StationState(target)
        with self._changed:
            current = self._state
            if expected is not None:
                self.require(expected)
            if next_state == current:
                raise InvalidStateTransition(
                    f"state is already {current.value}; duplicate transitions are rejected"
                )
            if next_state not in ALLOWED_TRANSITIONS[current]:
                raise InvalidStateTransition(
                    f"illegal station transition: {current.value} -> {next_state.value}"
                )
            self._state = next_state
            self._version += 1
            self._changed.notify_all()
            return next_state

    def wait_for(
        self,
        target: StationState | str | Iterable[StationState | str],
        timeout: float | None = None,
    ) -> StationState:
        if isinstance(target, (StationState, str)):
            targets = {StationState(target)}
        else:
            targets = {StationState(item) for item in target}
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        with self._changed:
            while self._state not in targets:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0.0:
                    wanted = ", ".join(sorted(item.value for item in targets))
                    raise TimeoutError(
                        f"timed out waiting for station state [{wanted}], current={self._state.value}"
                    )
                self._changed.wait(remaining)
            return self._state


# Concise alias for applications that already use the station terminology.
StationStateController = StationStateMachine


__all__ = [
    "ALLOWED_TRANSITIONS",
    "InvalidStateTransition",
    "StationStateController",
    "StationStateMachine",
]
