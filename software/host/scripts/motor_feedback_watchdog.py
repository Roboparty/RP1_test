"""Detect stale or diverging motor feedback during hardware replay."""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field

import numpy as np

try:
    from scripts.rp1_limb_common import MotorEntry
except ModuleNotFoundError:
    from rp1_limb_common import MotorEntry


@dataclass
class _MotorState:
    last_pos: float | None = None
    last_cmd: float | None = None
    cmd_when_pos_changed: float | None = None
    unchanged_since: float | None = None
    stale_reported: bool = False
    track_reported: bool = False
    error_reported: bool = False


@dataclass
class MotorFeedbackWatchdog:
    """Sample motor feedback and warn when a joint stops updating or diverges."""

    motors: list
    entries: list[MotorEntry]
    sample_hz: float = 10.0
    stale_sec: float = 2.0
    track_err_rad: float = 0.5
    cmd_move_rad: float = 0.05
    grace_sec: float = 10.0
    abort_on_fault: bool = True
    abort_on_tracking: bool = False
    query_feedback: bool = False

    _states: list[_MotorState] = field(default_factory=list, init=False)
    _next_sample: float = field(default=0.0, init=False)
    _period: float = field(default=0.1, init=False)
    _fault: str | None = field(default=None, init=False)
    _fault_kind: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._period = 1.0 / float(self.sample_hz)
        self._states = [_MotorState() for _ in self.entries]
        self._next_sample = 0.0

    @property
    def faulted(self) -> bool:
        return self._fault is not None

    @property
    def fault_message(self) -> str | None:
        return self._fault

    @property
    def fault_kind(self) -> str | None:
        return self._fault_kind

    def maybe_check(self, cmd_pos: np.ndarray, *, elapsed_s: float) -> bool:
        """Return False when playback should stop (--watchdog-abort)."""

        if self._fault is not None:
            return not self.abort_on_fault

        now = time.monotonic()
        if now < self._next_sample:
            return True
        self._next_sample = now + self._period

        cmd = np.asarray(cmd_pos, dtype=np.float64).reshape(-1)
        if self.query_feedback:
            for motor in self.motors:
                motor.refresh_motor_status()

        for index, (motor, entry, state) in enumerate(zip(self.motors, self.entries, self._states)):
            pos = float(motor.get_motor_pos())
            err_id = int(motor.get_error_id())
            cmd_value = float(cmd[index])

            if err_id != 0 and not state.error_reported:
                state.error_reported = True
                self._report(
                    elapsed_s,
                    entry,
                    f"error_id={err_id} pos={pos:+.4f} cmd={cmd_value:+.4f}",
                    kind="error_id",
                    abort=True,
                )

            track_err = abs(cmd_value - pos)
            in_grace = elapsed_s < self.grace_sec
            if (
                not in_grace
                and track_err >= self.track_err_rad
                and not state.track_reported
            ):
                state.track_reported = True
                self._report(
                    elapsed_s,
                    entry,
                    f"|cmd-act|={track_err:.3f} rad pos={pos:+.4f} cmd={cmd_value:+.4f}",
                    kind="tracking",
                    abort=self.abort_on_tracking,
                )

            pos_changed = state.last_pos is None or abs(pos - state.last_pos) > 1e-6
            if pos_changed:
                state.last_pos = pos
                state.cmd_when_pos_changed = cmd_value
                state.unchanged_since = now
            elif state.unchanged_since is None:
                state.unchanged_since = now

            state.last_cmd = cmd_value

            stale_for = now - (state.unchanged_since or now)
            commanded_motion = (
                state.cmd_when_pos_changed is not None
                and abs(cmd_value - state.cmd_when_pos_changed) >= self.cmd_move_rad
            )
            if (
                stale_for >= self.stale_sec
                and track_err >= self.track_err_rad
                and commanded_motion
                and not state.stale_reported
            ):
                state.stale_reported = True
                self._report(
                    elapsed_s,
                    entry,
                    f"feedback stale {stale_for:.1f}s pos={pos:+.4f} cmd={cmd_value:+.4f} "
                    f"|cmd-act|={track_err:.3f} rad",
                    kind="stale",
                    abort=True,
                )

        return self._fault is None or not self.abort_on_fault

    def _report(
        self,
        elapsed_s: float,
        entry: MotorEntry,
        detail: str,
        *,
        kind: str,
        abort: bool,
    ) -> None:
        message = (
            f"[watchdog] t={elapsed_s:.2f}s kind={kind} "
            f"motor_id={entry.motor_id} joint={entry.joint_name} {detail}"
        )
        print(message, file=sys.stderr, flush=True)
        if self.abort_on_fault and abort and self._fault is None:
            self._fault = message
            self._fault_kind = kind
