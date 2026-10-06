"""High-level factory station controller for desktop/web HMI adapters."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from factory_hmi.models import (
    FaultStatus,
    MotorSample,
    PlaybackOptions,
    StationConfig,
    StationState,
    TrajectoryData,
    json_number,
)

from ._legacy import motor_default_positions
from .backend import MotorBackend, MotorsPyBackend
from .config import build_station_config, load_station_config
from .fault_diagnosis import DiagnosisThresholds, FaultDiagnosisEngine
from .manual_control import ManualControlSession, motor_positions_to_joint
from .playback import PlaybackController
from .state_machine import InvalidStateTransition, StationStateMachine
from .trajectory import (
    import_trajectory,
    preflight_trajectory,
)
from .zeroing import MotorZeroingSession


class FactoryControllerError(RuntimeError):
    pass


class FactoryController:
    """Coordinates state, backend, trajectory safety, playback, and snapshots."""

    def __init__(
        self,
        config: StationConfig | dict[str, Any] | None = None,
        backend: MotorBackend | None = None,
        *,
        limb: str | None = None,
        config_path: Path | str | None = None,
        control_rate_hz: float | None = None,
        enable_watchdog: bool = True,
        enable_recording: bool = False,
        record_output: Path | str | None = None,
    ) -> None:
        if isinstance(config, StationConfig):
            station_config = config
        elif isinstance(config, dict):
            if limb is None:
                raise ValueError("limb is required when config is a mapping")
            station_config = build_station_config(
                config,
                limb,
                control_rate_hz=control_rate_hz,
            )
        elif config is None:
            if limb is None:
                raise ValueError("limb is required")
            station_config = load_station_config(
                limb,
                config_path,
                control_rate_hz=control_rate_hz,
            )
        else:
            raise TypeError("config must be StationConfig, mapping, or None")

        self.config = station_config
        self.backend = backend or MotorsPyBackend(station_config.entries)
        if len(self.backend.entries) != len(station_config.entries):
            raise ValueError("backend motor count does not match station config")
        self.state_machine = StationStateMachine()
        self.enable_watchdog = bool(enable_watchdog)
        self.enable_recording = bool(enable_recording)
        self.record_output = Path(record_output) if record_output is not None else None
        if self.enable_recording and self.record_output is None:
            raise ValueError("record_output is required when recording is enabled")

        self._lock = threading.RLock()
        self._trajectory: TrajectoryData | None = None
        self._playback: PlaybackController | None = None
        self._manual: ManualControlSession | None = None
        self._zeroing: MotorZeroingSession | None = None
        self._last_samples: list[MotorSample] = []
        self._fault: FaultStatus | None = None
        self._diagnosis = FaultDiagnosisEngine(
            DiagnosisThresholds.from_config(station_config.raw)
        )
        self._position_hold_stop = threading.Event()
        self._position_hold_thread: threading.Thread | None = None
        self._position_hold_target: np.ndarray | None = None

    @property
    def state(self) -> StationState:
        return self.state_machine.state

    @property
    def trajectory(self) -> TrajectoryData | None:
        with self._lock:
            return self._trajectory

    @property
    def playback(self) -> PlaybackController | None:
        with self._lock:
            return self._playback

    @property
    def position_hold_active(self) -> bool:
        with self._lock:
            return (
                self._position_hold_thread is not None
                and self._position_hold_thread.is_alive()
            )

    @property
    def manual_control_active(self) -> bool:
        with self._lock:
            return self._manual is not None and self._manual.active

    def connect(self) -> None:
        """Backward-compatible discover plus enable sequence."""

        self.discover_motors()
        self.enable_motors()

    def discover_motors(self) -> None:
        with self._lock:
            self.state_machine.require(StationState.DISCONNECTED)
            try:
                self.backend.open_transport()
                self.backend.refresh_feedback()
                self._last_samples = self.backend.read_samples()
            except Exception as exc:
                self._set_fault(exc)
                raise
            self._fault = None
            self.state_machine.transition(
                StationState.CONNECTED, expected=StationState.DISCONNECTED
            )

    def enable_motors(self) -> None:
        with self._lock:
            self.state_machine.require(StationState.CONNECTED)
            if self.backend.motors_enabled:
                raise FactoryControllerError("motors are already enabled")
            try:
                self.backend.enable_motors()
                self._last_samples = self.backend.read_samples()
            except Exception as exc:
                self._set_fault(exc)
                raise
            faulted = [sample for sample in self._last_samples if sample.error_id != 0]
            if faulted:
                error = FactoryControllerError(
                    "motor enable feedback contains errors: "
                    + ", ".join(
                        f"id={sample.motor_id}:error_id={sample.error_id}"
                        for sample in faulted
                    )
                )
                self._set_fault(error, samples=self._last_samples)
                raise error
            positions = np.asarray(
                [sample.pos_rad for sample in self._last_samples],
                dtype=np.float64,
            )
            if not np.all(np.isfinite(positions)):
                error = FactoryControllerError(
                    "motor enable feedback contains non-finite positions"
                )
                self._set_fault(error, samples=self._last_samples)
                raise error
            for name, raw_limit, comparison in (
                (
                    "motor_min_rad",
                    self.config.safety_limits.motor_min_rad,
                    np.less,
                ),
                (
                    "motor_max_rad",
                    self.config.safety_limits.motor_max_rad,
                    np.greater,
                ),
            ):
                if raw_limit is None:
                    continue
                limits = np.asarray(raw_limit, dtype=np.float64)
                if limits.ndim == 0:
                    limits = np.full(positions.shape, float(limits))
                else:
                    limits = limits.reshape(-1)
                if limits.shape != positions.shape:
                    error = FactoryControllerError(
                        f"{name} must be scalar or contain {positions.size} values"
                    )
                    self._set_fault(error, samples=self._last_samples)
                    raise error
                violating = np.flatnonzero(comparison(positions, limits))
                if violating.size:
                    index = int(violating[0])
                    error = FactoryControllerError(
                        f"motor[{index}] initial position {positions[index]:.6f} "
                        f"violates {name}={limits[index]:.6f}"
                    )
                    self._set_fault(error, samples=self._last_samples)
                    raise error
            self._fault = None
            try:
                self._start_position_hold(positions)
            except Exception as exc:
                self._set_fault(exc, samples=self._last_samples)
                raise

    def load_trajectory(self, path: Path | str) -> TrajectoryData:
        with self._lock:
            if self._manual is not None and self._manual.active:
                raise FactoryControllerError(
                    "stop manual control before loading a trajectory"
                )
            self.state_machine.require(
                {
                    StationState.DISCONNECTED,
                    StationState.CONNECTED,
                    StationState.COMPLETED,
                }
            )
            trajectory = import_trajectory(
                path,
                limb=self.config.limb,
                motor_count=len(self.config.entries),
            )
            # Import only performs trajectory-intrinsic checks.  The first-frame
            # delta depends on live feedback and belongs to arm(), where the HMI
            # can offer a controlled return-to-default recovery without losing
            # the newly uploaded trajectory.
            preflight_trajectory(
                trajectory,
                entries=self.config.entries,
                config=self.config.raw,
                limits=self.config.safety_limits,
                current_motor_pos=None,
            )
            self._trajectory = trajectory
            return trajectory

    # Name used by service/RPC layers.
    import_trajectory = load_trajectory

    def arm(self) -> None:
        with self._lock:
            if self._manual is not None and self._manual.active:
                raise FactoryControllerError(
                    "stop manual control before arming playback"
                )
            self.state_machine.require(
                StationState.CONNECTED
            )
            if not self.backend.motors_enabled:
                raise FactoryControllerError("enable motors before arming")
            if self._trajectory is None:
                raise FactoryControllerError("load a trajectory before arming")
            samples = self.backend.read_samples()
            self._last_samples = samples
            faulted = [sample for sample in samples if sample.error_id != 0]
            if faulted:
                error = FactoryControllerError(
                    "cannot arm while motors report errors: "
                    + ", ".join(
                        f"id={sample.motor_id}:error_id={sample.error_id}"
                        for sample in faulted
                    )
                )
                self._set_fault(error, samples=samples)
                raise error
            current = np.asarray(
                [sample.pos_rad for sample in samples], dtype=np.float64
            )
            preflight_trajectory(
                self._trajectory,
                entries=self.config.entries,
                config=self.config.raw,
                limits=self.config.safety_limits,
                current_motor_pos=current,
            )
            self.state_machine.transition(
                StationState.ARMED, expected=self.state_machine.state
            )

    def start(
        self,
        options: PlaybackOptions | None = None,
        *,
        loop: bool = False,
        cycles: int = 0,
        duration_s: float | None = None,
        speed: float = 1.0,
        record_rate_hz: float = 20.0,
        test_id: str | None = None,
        robot_id: str | None = None,
    ) -> None:
        with self._lock:
            self.state_machine.require(StationState.ARMED)
            if self._trajectory is None:
                raise FactoryControllerError("no trajectory is loaded")
        self._stop_position_hold()
        try:
            with self._lock:
                self.state_machine.require(StationState.ARMED)
                assert self._trajectory is not None
                playback_options = options or PlaybackOptions(
                    loop=loop,
                    cycles=cycles,
                    duration_s=duration_s,
                    speed=speed,
                )
                self._playback = PlaybackController(
                    self.backend,
                    self.config,
                    self._trajectory,
                    state_machine=self.state_machine,
                    enable_watchdog=self.enable_watchdog,
                    enable_recording=self.enable_recording,
                    record_output=self.record_output,
                    record_rate_hz=record_rate_hz,
                    test_id=test_id,
                    robot_id=robot_id,
                    on_fault=self._on_playback_fault,
                    on_complete=self._on_playback_complete,
                )
                self._playback.start(playback_options)
        except Exception:
            try:
                self._resume_position_hold_from_feedback()
            except Exception:
                pass
            raise

    def pause(self) -> None:
        playback = self._require_playback()
        playback.pause()

    def resume(self) -> None:
        playback = self._require_playback()
        playback.resume()

    def stop(self, *, wait: bool = True, timeout: float = 2.0) -> None:
        playback = self._require_playback()
        playback.stop(wait=wait, timeout=timeout)

    def manual_move(
        self,
        targets_rad: np.ndarray | list[float],
        *,
        speed_rad_s: float,
    ) -> None:
        with self._lock:
            self.state_machine.require(StationState.CONNECTED)
            if not self.backend.connected or not self.backend.motors_enabled:
                raise FactoryControllerError(
                    "manual control requires discovered and enabled motors"
                )
            if self._zeroing is not None and self._zeroing.state.value in {
                "idle",
                "prepared",
                "reading",
            }:
                raise FactoryControllerError(
                    "manual control is unavailable during motor zeroing"
                )
            manual = self._manual
        if manual is None or not manual.active:
            samples = self.backend.read_samples()
            initial = np.asarray(
                [sample.pos_rad for sample in samples],
                dtype=np.float64,
            )
            self._stop_position_hold()
            manual = ManualControlSession(
                self.backend,
                self.config,
                initial,
                on_fault=self._on_manual_fault,
            )
            manual.move(targets_rad, speed_rad_s=speed_rad_s)
            with self._lock:
                self._manual = manual
                self._last_samples = samples
            try:
                manual.start()
            except Exception:
                with self._lock:
                    if self._manual is manual:
                        self._manual = None
                self._start_position_hold(initial)
                raise
        else:
            manual.move(targets_rad, speed_rad_s=speed_rad_s)

    def manual_stop(self) -> None:
        manual = self._stop_manual_session()
        if manual is None:
            raise FactoryControllerError("manual control is not active")
        if (
            self.backend.connected
            and self.backend.motors_enabled
            and self.state_machine.state is StationState.CONNECTED
        ):
            self._resume_position_hold_from_feedback()

    def prepare_next_run(self) -> None:
        with self._lock:
            self.state_machine.require(StationState.COMPLETED)
            if self._manual is not None and self._manual.active:
                raise FactoryControllerError(
                    "stop manual control before preparing the next run"
                )
            self.state_machine.transition(
                StationState.CONNECTED,
                expected=StationState.COMPLETED,
            )
            self._trajectory = None
            self._playback = None
            self._fault = None

    def reset(self, *, to_default: bool = False) -> None:
        self._stop_manual_session()
        with self._lock:
            state = self.state_machine.state
            playback = self._playback
            zeroing = self._zeroing
        if state in {StationState.RUNNING, StationState.PAUSED}:
            assert playback is not None
            playback.stop(wait=True)
        if zeroing is not None and zeroing.state.value in {
            "idle",
            "prepared",
            "reading",
        }:
            zeroing.abort("station reset")

        self._stop_position_hold()
        hold_target: np.ndarray | None = None
        with self._lock:
            state = self.state_machine.state
            if state not in {
                StationState.CONNECTED,
                StationState.ARMED,
                StationState.COMPLETED,
                StationState.FAULT,
            }:
                raise InvalidStateTransition(
                    f"reset is not allowed from {state.value}"
                )
            try:
                recovery_required = (
                    state is StationState.FAULT
                    or not self.backend.connected
                    or not self.backend.motors_enabled
                )
                if recovery_required:
                    # Fault recovery still requires a known disabled transport
                    # and a fresh enable. Healthy "return to default" does not.
                    if self.backend.connected:
                        self.backend.disconnect()
                    self.backend.connect()
                    self.backend.clear_errors()
                samples = self.backend.read_samples()
                faulted = [sample for sample in samples if sample.error_id != 0]
                if faulted:
                    raise FactoryControllerError(
                        "cannot return to default while motors report errors: "
                        + ", ".join(
                            f"id={sample.motor_id}:error_id={sample.error_id}"
                            for sample in faulted
                        )
                    )
                current = np.asarray(
                    [sample.pos_rad for sample in samples],
                    dtype=np.float64,
                )
                if not np.all(np.isfinite(current)):
                    raise FactoryControllerError(
                        "cannot return to default with non-finite motor feedback"
                    )
                hold_target = current.copy()
                if to_default:
                    target = motor_default_positions(
                        self.config.raw, self.config.limb
                    )
                    duration_s = 1.0
                    steps = max(2, int(self.config.control_rate_hz * duration_s))
                    next_tick = time.monotonic()
                    for step in range(steps):
                        x = float(step + 1) / float(steps)
                        blend = x * x * (3.0 - 2.0 * x)
                        self.backend.command_mit(
                            current + (target - current) * blend
                        )
                        next_tick += 1.0 / float(self.config.control_rate_hz)
                        delay = next_tick - time.monotonic()
                        if delay > 0.0:
                            time.sleep(delay)
                    hold_target = target.copy()
                self._last_samples = self.backend.read_samples()
            except Exception as exc:
                self._set_fault(exc)
                raise
            if state is not StationState.CONNECTED:
                self.state_machine.transition(
                    StationState.CONNECTED, expected=state
                )
            self._fault = None
            self._playback = None
            self._zeroing = None
        assert hold_target is not None
        try:
            self._start_position_hold(hold_target)
        except Exception as exc:
            with self._lock:
                self._set_fault(exc, samples=self._last_samples)
            raise

    def disconnect(self) -> None:
        self._stop_manual_session()
        with self._lock:
            state = self.state_machine.state
            playback = self._playback
            zeroing = self._zeroing
        if state in {StationState.RUNNING, StationState.PAUSED} and playback is not None:
            playback.stop(wait=True)
        if zeroing is not None and zeroing.state.value in {
            "idle",
            "prepared",
            "reading",
        }:
            zeroing.abort("station disconnect")

        self._stop_position_hold()
        disconnect_error: Exception | None = None
        try:
            if self.backend.connected:
                try:
                    self.backend.disable()
                except Exception:
                    pass
                self.backend.disconnect()
        except Exception as exc:
            disconnect_error = exc

        with self._lock:
            state = self.state_machine.state
            if state is not StationState.DISCONNECTED:
                self.state_machine.transition(
                    StationState.DISCONNECTED, expected=state
                )
            self._last_samples = []
            self._playback = None
            self._zeroing = None
            if disconnect_error is not None:
                self._fault = FaultStatus(
                    message=str(disconnect_error),
                    kind=disconnect_error.__class__.__name__,
                    timestamp_s=time.monotonic(),
                )
        if disconnect_error is not None:
            raise disconnect_error

    def disable_motors(self, *, reason: str = "software disable") -> None:
        """Immediately remove motor torque without closing the transport."""

        self._stop_manual_session()
        self._stop_position_hold()
        with self._lock:
            if self.backend.connected:
                self.backend.disable_motors()
            state = self.state_machine.state
            if state in {StationState.ARMED, StationState.COMPLETED}:
                self.state_machine.transition(
                    StationState.CONNECTED,
                    expected=state,
                )
            elif state in {
                StationState.RUNNING,
                StationState.PAUSED,
                StationState.STOPPING,
            }:
                self._set_fault(
                    RuntimeError(reason),
                    samples=self._last_samples,
                )

    def zero_motors(self, indices: list[int] | tuple[int, ...]) -> dict[str, Any]:
        """Disable the station and set selected motor positions as zero."""

        normalized: list[int] = []
        for raw_index in indices:
            index = int(raw_index)
            if index < 0 or index >= len(self.config.entries):
                raise IndexError(f"motor index {index} is out of range")
            if index not in normalized:
                normalized.append(index)
        if not normalized:
            raise ValueError("at least one motor is required for zeroing")

        with self._lock:
            self.state_machine.require(StationState.CONNECTED)
            if not self.backend.connected:
                raise FactoryControllerError(
                    "motor transport must be connected before zeroing"
                )
            if self._manual is not None and self._manual.active:
                raise FactoryControllerError(
                    "stop manual control before motor zeroing"
                )
            zeroing = self._zeroing

        self._stop_position_hold()
        if zeroing is not None and zeroing.state.value in {
            "idle",
            "prepared",
            "reading",
        }:
            zeroing.abort("direct zeroing requested")
        with self._lock:
            self._zeroing = None

        # The hardware protocol requires set-zero while drives are unlocked.
        # Disable every motor because the position-hold path sends batched
        # commands and cannot safely exclude only one motor from that batch.
        self.backend.disable_motors()
        samples = self.backend.read_samples()
        sample_by_index = {int(sample.index): sample for sample in samples}
        faulted = [
            sample_by_index[index]
            for index in normalized
            if index in sample_by_index
            and int(sample_by_index[index].error_id) != 0
        ]
        if faulted:
            details = ", ".join(
                f"id={sample.motor_id}:error_id={sample.error_id}"
                for sample in faulted
            )
            raise FactoryControllerError(
                f"refusing to zero motors with active errors: {details}"
            )

        zeroed: list[dict[str, Any]] = []
        warnings: list[str] = []
        for index in normalized:
            entry = self.config.entries[index]
            acknowledged = self.backend.zero_motor(
                index,
                force_on_error=False,
            )
            zeroed.append(
                {
                    "index": index,
                    "motor_id": int(entry.motor_id),
                    "joint_name": entry.joint_name,
                    "acknowledged": bool(acknowledged),
                }
            )
            if not acknowledged:
                warnings.append(
                    f"{entry.joint_name} (ID {entry.motor_id}) received the "
                    "set-zero command but did not report a near-zero position"
                )

        try:
            self.backend.refresh_feedback()
            refreshed = self.backend.read_samples()
        except Exception:
            refreshed = samples
        with self._lock:
            self._last_samples = refreshed
        return {
            "zeroed": zeroed,
            "warnings": warnings,
            "motors_enabled": False,
        }

    def create_zeroing_session(self) -> MotorZeroingSession:
        with self._lock:
            self.state_machine.require(StationState.CONNECTED)
            if self._manual is not None and self._manual.active:
                raise FactoryControllerError(
                    "stop manual control before motor zeroing"
                )
        self._stop_position_hold()
        self.backend.disable_motors()
        with self._lock:
            self._zeroing = MotorZeroingSession(
                self.backend, station_state=self.state_machine
            )
            return self._zeroing

    # More compact spelling for direct Python users.
    zeroing_session = create_zeroing_session

    def wait(self, timeout: float | None = None) -> bool:
        playback = self.playback
        return True if playback is None else playback.wait(timeout)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            playback = self._playback
            if playback is not None:
                playback_samples = playback.last_samples()
                if playback_samples:
                    self._last_samples = playback_samples
                playback_snapshot = playback.snapshot()
                progress = playback_snapshot["progress"]
            else:
                playback_snapshot = None
                progress = None
            manual_snapshot = self._manual.snapshot() if self._manual else None
            if self.backend.connected and self.state_machine.state not in {
                StationState.RUNNING,
                StationState.PAUSED,
            }:
                try:
                    if not self.backend.motors_enabled:
                        self.backend.refresh_feedback()
                    self._last_samples = self.backend.read_samples()
                except Exception:
                    pass
            joint_positions: list[float | None] = []
            if len(self._last_samples) == len(self.config.entries):
                try:
                    converted = motor_positions_to_joint(
                        self.config,
                        [sample.pos_rad for sample in self._last_samples],
                    )
                    joint_positions = [
                        json_number(float(value)) for value in converted
                    ]
                except Exception:
                    joint_positions = []
            zeroing = self._zeroing.snapshot() if self._zeroing else None
            return {
                "station": {
                    "state": self.state_machine.state.value,
                    "limb": self.config.limb,
                    "control_rate_hz": float(self.config.control_rate_hz),
                    "backend_connected": bool(self.backend.connected),
                    "motors_enabled": bool(self.backend.motors_enabled),
                    "position_hold_active": self.position_hold_active,
                    "manual_control_active": bool(
                        manual_snapshot and manual_snapshot.get("active")
                    ),
                },
                "motors": [sample.to_dict() for sample in self._last_samples],
                "joint_positions_rad": joint_positions,
                "trajectory": (
                    self._trajectory.to_dict() if self._trajectory else None
                ),
                "progress": progress,
                "playback": playback_snapshot,
                "manual": manual_snapshot,
                "zeroing": zeroing,
                "fault": self._fault.to_dict() if self._fault else None,
                "timestamp_s": json_number(time.monotonic()),
            }

    def _require_playback(self) -> PlaybackController:
        with self._lock:
            if self._playback is None:
                raise FactoryControllerError("playback has not been started")
            return self._playback

    def _start_position_hold(self, target: np.ndarray | list[float]) -> None:
        target_array = np.asarray(target, dtype=np.float64).reshape(-1)
        if target_array.shape != (len(self.config.entries),):
            raise ValueError(
                "position hold target count does not match configured motors"
            )
        if not np.all(np.isfinite(target_array)):
            raise ValueError("position hold target contains NaN or inf")
        if not self.backend.connected or not self.backend.motors_enabled:
            raise FactoryControllerError(
                "position hold requires connected and enabled motors"
            )

        self._stop_position_hold()
        stop_event = threading.Event()
        thread = threading.Thread(
            target=self._position_hold_loop,
            args=(target_array.copy(), stop_event),
            name=f"factory-position-hold-{self.config.limb}",
            daemon=True,
        )
        with self._lock:
            self._position_hold_stop = stop_event
            self._position_hold_target = target_array.copy()
            self._position_hold_thread = thread
        try:
            thread.start()
        except Exception:
            with self._lock:
                if self._position_hold_thread is thread:
                    self._position_hold_thread = None
                    self._position_hold_target = None
            raise

    def _stop_position_hold(self, timeout: float = 2.0) -> None:
        with self._lock:
            stop_event = self._position_hold_stop
            thread = self._position_hold_thread
        stop_event.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(float(timeout))
            if thread.is_alive():
                raise TimeoutError("position hold thread did not stop in time")
        with self._lock:
            if self._position_hold_thread is thread:
                self._position_hold_thread = None
                self._position_hold_target = None

    def _position_hold_loop(
        self,
        target: np.ndarray,
        stop_event: threading.Event,
    ) -> None:
        period = 1.0 / float(self.config.control_rate_hz)
        next_tick = time.monotonic()
        last_sample_at = float("-inf")
        samples: list[MotorSample] = []
        try:
            while not stop_event.is_set():
                self.backend.command_mit(target)
                now = time.monotonic()
                if now - last_sample_at >= 0.05:
                    samples = self.backend.read_samples()
                    faulted = [
                        sample for sample in samples if sample.error_id != 0
                    ]
                    if faulted:
                        raise FactoryControllerError(
                            "position hold feedback contains errors: "
                            + ", ".join(
                                f"id={sample.motor_id}:error_id={sample.error_id}"
                                for sample in faulted
                            )
                        )
                    with self._lock:
                        if self._position_hold_stop is stop_event:
                            self._last_samples = samples
                    last_sample_at = now
                next_tick += period
                delay = next_tick - time.monotonic()
                if delay > 0.0:
                    stop_event.wait(delay)
                elif -delay > period * 4.0:
                    next_tick = time.monotonic()
        except Exception as exc:
            if not stop_event.is_set():
                with self._lock:
                    if self._position_hold_stop is stop_event:
                        self._set_fault(
                            FactoryControllerError(
                                f"continuous position hold failed: {exc}"
                            ),
                            samples=samples,
                        )
        finally:
            with self._lock:
                if (
                    self._position_hold_stop is stop_event
                    and self._position_hold_thread is threading.current_thread()
                ):
                    self._position_hold_thread = None
                    self._position_hold_target = None

    def _resume_position_hold_from_feedback(self) -> None:
        if (
            not self.backend.connected
            or not self.backend.motors_enabled
            or self.state_machine.state
            not in {
                StationState.CONNECTED,
                StationState.ARMED,
                StationState.COMPLETED,
            }
        ):
            return
        samples = self.backend.read_samples()
        target = np.asarray(
            [sample.pos_rad for sample in samples],
            dtype=np.float64,
        )
        with self._lock:
            self._last_samples = samples
        self._start_position_hold(target)

    def _stop_manual_session(self) -> ManualControlSession | None:
        with self._lock:
            manual = self._manual
            self._manual = None
        if manual is not None:
            manual.stop()
        return manual

    def _on_manual_fault(
        self,
        exc: Exception,
        samples: list[MotorSample],
    ) -> None:
        with self._lock:
            self._manual = None
            self._set_fault(
                FactoryControllerError(f"manual control failed: {exc}"),
                samples=samples,
            )

    def _on_playback_complete(self) -> None:
        self._resume_position_hold_from_feedback()

    def _on_playback_fault(self, fault: FaultStatus) -> None:
        with self._lock:
            self._fault = fault

    def _set_fault(
        self,
        exc: Exception,
        *,
        samples: list[MotorSample] | None = None,
    ) -> None:
        self._position_hold_stop.set()
        manual = self._manual
        self._manual = None
        if manual is not None:
            try:
                manual.stop()
            except Exception:
                pass
        actual_samples = samples or []
        diagnoses = self._diagnosis.diagnose(actual_samples)
        self._fault = FaultStatus(
            message=str(exc),
            kind=exc.__class__.__name__,
            timestamp_s=time.monotonic(),
            diagnoses=diagnoses,
        )
        if self.backend.connected:
            try:
                self.backend.disable()
            except Exception:
                pass
        state = self.state_machine.state
        if state is not StationState.FAULT:
            self.state_machine.transition(StationState.FAULT, expected=state)


__all__ = ["FactoryController", "FactoryControllerError"]
