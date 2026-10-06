"""Single-operator control lease used by the factory gateway."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


class LeaseConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class LeaseSnapshot:
    owner: str | None
    age_s: float | None
    timeout_s: float
    expired: bool


class ControlLease:
    def __init__(self, timeout_s: float = 2.0) -> None:
        if timeout_s <= 0.0:
            raise ValueError("timeout_s must be positive")
        self.timeout_s = float(timeout_s)
        self._owner: str | None = None
        self._last_refresh: float | None = None
        self._lock = threading.Lock()

    def claim(self, owner: str, *, now: float | None = None) -> None:
        owner = owner.strip()
        if not owner:
            raise ValueError("lease owner cannot be empty")
        timestamp = time.monotonic() if now is None else float(now)
        with self._lock:
            if (
                self._owner is not None
                and self._owner != owner
                and not self._expired_locked(timestamp)
            ):
                raise LeaseConflict(f"station is controlled by {self._owner}")
            self._owner = owner
            self._last_refresh = timestamp

    def refresh(self, owner: str, *, now: float | None = None) -> None:
        timestamp = time.monotonic() if now is None else float(now)
        with self._lock:
            if self._owner is None or self._expired_locked(timestamp):
                self._owner = owner
            elif self._owner != owner:
                raise LeaseConflict(f"station is controlled by {self._owner}")
            self._last_refresh = timestamp

    def release(self, owner: str | None = None) -> None:
        with self._lock:
            if owner is not None and self._owner not in {None, owner}:
                raise LeaseConflict(f"station is controlled by {self._owner}")
            self._owner = None
            self._last_refresh = None

    def expired(self, *, now: float | None = None) -> bool:
        timestamp = time.monotonic() if now is None else float(now)
        with self._lock:
            return self._expired_locked(timestamp)

    def snapshot(self, *, now: float | None = None) -> LeaseSnapshot:
        timestamp = time.monotonic() if now is None else float(now)
        with self._lock:
            age = None if self._last_refresh is None else max(0.0, timestamp - self._last_refresh)
            return LeaseSnapshot(
                owner=self._owner,
                age_s=age,
                timeout_s=self.timeout_s,
                expired=self._expired_locked(timestamp),
            )

    def _expired_locked(self, now: float) -> bool:
        return (
            self._owner is not None
            and self._last_refresh is not None
            and now - self._last_refresh > self.timeout_s
        )
