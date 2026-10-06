"""Dedicated-thread trajectory playback at the configured control rate."""

from __future__ import annotations

import math
import threading
import time
from pathlib import Path
from typing import Any, Callable

import numpy as np

from factory_hmi.models import (
    FaultStatus,
    MotorSample,
    PlaybackOptions,
    PlaybackProgress,
    StationConfig,
    StationState,
    TrajectoryData,
    json_number,
)

from ._legacy import (
    ControllerHeartbeat,
    MotorFeedbackWatchdog,
    MotorTelemetryRecorder,
    compute_hip_roll_grav_torque,
    hip_roll_grav_comp_enabled,
    trajectory_target_at,
)
from .backend import MotorBackend
from .fault_diagnosis import DiagnosisThresholds, FaultDiagnosisEngine
from .state_machine import InvalidStateTransition, StationStateMachine


class PlaybackError(RuntimeError):
    pass


class _CommandPosition:
    def __init__(self, count: int) -> None:
        self._lock = threading.Lock()
        self._values = np.full(count, np.nan, dtype=np.float64)

    def set(self, values: np.ndarray) -> None:
        with self._lock:
            self._values[:] = values

    def as_list(self) -> list[float]:
        with self._lock:
            return [float(item) for item in self._values]


class _RunStatistics:
    def __init__(self, config: StationConfig) -> None:
        self._lock = threading.Lock()
        self._entries = tuple(config.entries)
        count = len(self._entries)
        self._samples = np.zeros(count, dtype=np.int64)
        self._tracking_sq = np.zeros(count, dtype=np.float64)
        self._tracking_max = np.zeros(count, dtype=np.float64)
        self._torque_abs_sum = np.zeros(count, dtype=np.float64)
        self._torque_peak = np.zeros(count, dtype=np.float64)
        self._temperature_start = np.full(count, np.nan, dtype=np.float64)
        self._temperature_current = np.full(count, np.nan, dtype=np.float64)
        self._temperature_peak = np.full(count, np.nan, dtype=np.float64)
        self._error_samples = np.zeros(count, dtype=np.int64)

    def update(
        self,
        samples: list[MotorSample],
        target: np.ndarray,
    ) -> None:
        with self._lock:
            for index, sample in enumerate(samples):
                if index >= len(self._entries):
                    break
                position = float(sample.pos_rad)
                command = float(target[index])
                torque = float(sample.torque_nm)
                temperature = float(sample.temp_c)
                if math.isfinite(position) and math.isfinite(command):
                    error = abs(command - position)
                    self._tracking_sq[index] += error * error
                    self._tracking_max[index] = max(
                        self._tracking_max[index],
                        error,
                    )
                if math.isfinite(torque):
                    absolute_torque = abs(torque)
                    self._torque_abs_sum[index] += absolute_torque
                    self._torque_peak[index] = max(
                        self._torque_peak[index],
                        absolute_torque,
                    )
                if math.isfinite(temperature):
                    if not math.isfinite(self._temperature_start[index]):
                        self._temperature_start[index] = temperature
                    self._temperature_current[index] = temperature
                    if not math.isfinite(self._temperature_peak[index]):
                        self._temperature_peak[index] = temperature
                    else:
                        self._temperature_peak[index] = max(
                            self._temperature_peak[index],
                            temperature,
                        )
                if int(sample.error_id) != 0:
                    self._error_samples[index] += 1
                self._samples[index] += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            joints = []
            for index, entry in enumerate(self._entries):
                count = int(self._samples[index])
                start_temperature = self._temperature_start[index]
                current_temperature = self._temperature_current[index]
                joints.append(
                    {
                        "index": index,
                        "motor_id": int(entry.motor_id),
                        "joint_name": entry.joint_name,
                        "sample_count": count,
                        "tracking_rms_rad": json_number(
                            math.sqrt(self._tracking_sq[index] / count)
                            if count
                            else None
                        ),
                        "tracking_max_rad": json_number(
                            self._tracking_max[index] if count else None
                        ),
                        "torque_peak_nm": json_number(
                            self._torque_peak[index] if count else None
                        ),
                        "torque_mean_abs_nm": json_number(
                            self._torque_abs_sum[index] / count
                            if count
                            else None
                        ),
                        "temperature_start_c": json_number(start_temperature),
                        "temperature_current_c": json_number(current_temperature),
                        "temperature_rise_c": json_number(
                            current_temperature - start_temperature
                            if math.isfinite(start_temperature)
                            and math.isfinite(current_temperature)
                            else None
                        ),
                        "temperature_peak_c": json_number(
                            self._temperature_peak[index]
                        ),
                        "error_samples": int(self._error_samples[index]),
                    }
                )
            return {
                "sample_count": int(np.max(self._samples))
                if self._samples.size
                else 0,
                "joints": joints,
            }


class PlaybackController:
    """Own the 200 Hz control thread; callers only set thread-safe events."""

    def __init__(
        self,
        backend: MotorBackend,
        config: StationConfig,
        trajectory: TrajectoryData,
        *,
        state_machine: StationStateMachine | None = None,
        enable_watchdog: bool = True,
        enable_recording: bool = False,
        record_output: Path | str | None = None,
        record_rate_hz: float | None = None,
        test_id: str | None = None,
        robot_id: str | None = None,
        on_fault: Callable[[FaultStatus], None] | None = None,
        on_complete: Callable[[], None] | None = None,
    ) -> None:
        if len(backend.entries) != len(config.entries):
            raise ValueError("backend motor count does not match station config")
        if trajectory.motor_count != len(config.entries):
            raise ValueError("trajectory motor count does not match station config")
        if enable_recording and record_output is None:
            raise ValueError("record_output is required when recording is enabled")
        actual_record_rate_hz = float(
            record_rate_hz or min(config.control_rate_hz, 20.0)
        )
        if not 1.0 <= actual_record_rate_hz <= 20.0:
            raise ValueError("record_rate_hz must be between 1 and 20 Hz")
        self.backend = backend
        self.config = config
        self.trajectory = trajectory
        self.state_machine = state_machine or StationStateMachine(
            StationState.ARMED
        )
        self.enable_watchdog = bool(enable_watchdog)
        self.enable_recording = bool(enable_recording)
        self.record_output = Path(record_output) if record_output is not None else None
        # Hardware feedback is sampled by the control thread.  Recording should
        # consume that cache instead of reading raw motor handles concurrently
        # at the 200 Hz command rate.
        self.record_rate_hz = actual_record_rate_hz
        self.test_id = test_id
        self.robot_id = robot_id
        self.on_fault = on_fault
        self.on_complete = on_complete

        self.heartbeat = ControllerHeartbeat()
        self._command_position = _CommandPosition(len(config.entries))
        self._diagnosis = FaultDiagnosisEngine(
            DiagnosisThresholds.from_config(config.raw)
        )
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._progress = PlaybackProgress()
        self._last_samples: list[MotorSample] = []
        self._fault: FaultStatus | None = None
        self._options = PlaybackOptions()
        self._statistics = _RunStatistics(config)
        self._recorder: MotorTelemetryRecorder | None = None
        self._record_finished = False

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def fault(self) -> FaultStatus | None:
        with self._lock:
            return self._fault

    def start(self, options: PlaybackOptions | None = None) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise PlaybackError("playback is already running")
            if not self.backend.connected:
                raise PlaybackError("backend must be connected before playback")
            self.state_machine.require(StationState.ARMED)
            self._options = options or PlaybackOptions()
            self._progress = PlaybackProgress(cycle=1)
            self._statistics = _RunStatistics(self.config)
            self._recorder = None
            self._record_finished = False
            self._fault = None
            self._last_samples = []
            self._stop_event.clear()
            self._pause_event.clear()
            self.state_machine.transition(
                StationState.RUNNING, expected=StationState.ARMED
            )
            self._thread = threading.Thread(
                target=self._run,
                name=f"factory-playback-{self.config.limb}",
                daemon=True,
            )
            try:
                self._thread.start()
            except Exception:
                self.state_machine.transition(
                    StationState.FAULT, expected=StationState.RUNNING
                )
                self._thread = None
                raise

    def pause(self) -> None:
        with self._lock:
            self.state_machine.require(StationState.RUNNING)
            self._pause_event.set()
            self.state_machine.transition(
                StationState.PAUSED, expected=StationState.RUNNING
            )

    def resume(self) -> None:
        with self._lock:
            self.state_machine.require(StationState.PAUSED)
            self._pause_event.clear()
            self.state_machine.transition(
                StationState.RUNNING, expected=StationState.PAUSED
            )

    def stop(self, *, wait: bool = True, timeout: float = 2.0) -> None:
        with self._lock:
            state = self.state_machine.state
            if state not in {StationState.RUNNING, StationState.PAUSED}:
                raise InvalidStateTransition(
                    f"stop requires running/paused, current={state.value}"
                )
            self.state_machine.transition(StationState.STOPPING, expected=state)
            self._pause_event.clear()
            self._stop_event.set()
            thread = self._thread
        if wait and thread is not None:
            thread.join(float(timeout))
            if thread.is_alive():
                raise TimeoutError("playback thread did not stop in time")

    def wait(self, timeout: float | None = None) -> bool:
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            thread_alive = self._thread is not None and self._thread.is_alive()
            recorder = self._recorder
            record_path = self.record_output if self.enable_recording else None
            return {
                "thread_alive": thread_alive,
                "paused": self._pause_event.is_set(),
                "test_id": self.test_id,
                "robot_id": self.robot_id,
                "control_rate_hz": float(self.config.control_rate_hz),
                "record_rate_hz": (
                    self.record_rate_hz if self.enable_recording else None
                ),
                "options": {
                    "loop": self._options.loop,
                    "cycles": self._options.cycles,
                    "duration_s": self._options.duration_s,
                    "speed": self._options.speed,
                },
                "progress": self._progress.to_dict(),
                "statistics": self._statistics.snapshot(),
                "recording": {
                    "enabled": bool(self.enable_recording),
                    "active": bool(
                        self.enable_recording
                        and thread_alive
                        and not self._record_finished
                    ),
                    "finished": bool(self._record_finished),
                    "filename": record_path.name if record_path is not None else None,
                    "row_count": int(recorder.row_count) if recorder is not None else 0,
                    "rate_hz": (
                        self.record_rate_hz if self.enable_recording else None
                    ),
                },
                "heartbeat_age_s": self.heartbeat.age(),
                "fault": self._fault.to_dict() if self._fault else None,
            }

    def last_samples(self) -> list[MotorSample]:
        with self._lock:
            return list(self._last_samples)

    def _run(self) -> None:
        recorder: MotorTelemetryRecorder | None = None
        watchdog: MotorFeedbackWatchdog | None = None
        terminal_outcome = "completed"
        try:
            if self.enable_watchdog:
                watchdog = MotorFeedbackWatchdog(
                    self.backend.motors,
                    list(self.config.entries),
                    sample_hz=20.0,
                    stale_sec=2.0,
                    track_err_rad=0.5,
                    grace_sec=10.0,
                    abort_on_fault=True,
                    abort_on_tracking=False,
                    query_feedback=False,
                )
            if self.enable_recording:
                assert self.record_output is not None
                recorder = MotorTelemetryRecorder(
                    self.backend.motors,
                    list(self.config.entries),
                    self.record_output,
                    rate_hz=self.record_rate_hz,
                    query=False,
                    get_cmd_pos=self._command_position.as_list,
                    sample_provider=self.last_samples,
                    print_status=False,
                    quiet=True,
                    limb=self.config.limb,
                    test_id=self.test_id,
                    robot_id=self.robot_id,
                    controller_heartbeat=self.heartbeat,
                    platform_required=False,
                )
                recorder.start()
                with self._lock:
                    self._recorder = recorder

            terminal_outcome = self._control_loop(watchdog, recorder)
            with self._lock:
                self._progress.outcome = terminal_outcome
        except Exception as exc:
            self._handle_fault(exc)
        finally:
            if recorder is not None:
                try:
                    recorder.stop_sampling()
                    recorder.finalize(
                        final_status=(
                            "blocked" if self.state_machine.state is StationState.FAULT else None
                        ),
                        final_summary=(
                            self._fault.message if self._fault is not None else None
                        ),
                    )
                    with self._lock:
                        self._record_finished = True
                except Exception as exc:
                    if self.state_machine.state is not StationState.FAULT:
                        self._handle_fault(
                            PlaybackError(f"telemetry recorder failed: {exc}")
                        )
        current = self.state_machine.state
        if current in {StationState.RUNNING, StationState.STOPPING}:
            self.state_machine.transition(StationState.COMPLETED, expected=current)
        if (
            self.state_machine.state is StationState.COMPLETED
            and self.on_complete is not None
        ):
            try:
                self.on_complete()
            except Exception as exc:
                self._handle_fault(
                    PlaybackError(f"failed to resume position hold: {exc}")
                )

    def _control_loop(
        self,
        watchdog: MotorFeedbackWatchdog | None,
        recorder: MotorTelemetryRecorder | None,
    ) -> str:
        period = 1.0 / float(self.config.control_rate_hz)
        options = self._options
        trajectory_duration = self.trajectory.duration_s
        cycle_time = 0.0
        active_time = 0.0
        completed_cycles = 0
        next_tick = time.monotonic()
        last_tick = next_tick
        last_sample_at = float("-inf")
        target = np.asarray(self.trajectory.motor_pos[0], dtype=np.float64).copy()

        while not self._stop_event.is_set():
            now = time.monotonic()
            delta = max(0.0, now - last_tick)
            last_tick = now

            if self._pause_event.is_set():
                self._dispatch(target, active_time, watchdog, recorder)
            else:
                active_time += delta
                cycle_time += delta * float(options.speed)
                if trajectory_duration <= 0.0:
                    target = np.asarray(
                        self.trajectory.motor_pos[-1], dtype=np.float64
                    )
                else:
                    timeline_time = min(cycle_time, trajectory_duration)
                    target = trajectory_target_at(
                        self.trajectory.motor_pos,
                        self.trajectory.timestamps,
                        timeline_time,
                    )
                self._dispatch(target, active_time, watchdog, recorder)

                if now - last_sample_at >= 0.05:
                    samples = self.backend.read_samples()
                    with self._lock:
                        self._last_samples = samples
                    self._statistics.update(samples, target)
                    critical = [
                        diagnosis
                        for diagnosis in self._diagnosis.diagnose(
                            samples,
                            heartbeat_age_s=self.heartbeat.age(),
                            now_s=now,
                        )
                        if diagnosis.severity == "critical"
                    ]
                    if critical:
                        diagnosis = critical[0]
                        evidence = "; ".join(diagnosis.evidence)
                        raise PlaybackError(
                            f"safety rule {diagnosis.rule}: "
                            f"{diagnosis.suspected_component}; {evidence}"
                        )
                    last_sample_at = now

                if (
                    options.duration_s is not None
                    and active_time >= float(options.duration_s)
                ):
                    self._update_progress(
                        cycle_time,
                        active_time,
                        completed_cycles,
                        target,
                    )
                    return "duration_complete"

                if trajectory_duration <= 0.0 or cycle_time >= trajectory_duration:
                    completed_cycles += 1
                    should_repeat = bool(options.loop)
                    if not should_repeat:
                        self._update_progress(
                            trajectory_duration,
                            active_time,
                            completed_cycles,
                            target,
                        )
                        return "completed"
                    if options.cycles > 0 and completed_cycles >= options.cycles:
                        self._update_progress(
                            trajectory_duration,
                            active_time,
                            completed_cycles,
                            target,
                        )
                        return "cycles_complete"
                    cycle_time = (
                        max(0.0, cycle_time - trajectory_duration)
                        if trajectory_duration > 0.0
                        else 0.0
                    )

            self._update_progress(
                cycle_time,
                active_time,
                completed_cycles,
                target,
            )
            next_tick += period
            current = time.monotonic()
            if next_tick > current:
                self._stop_event.wait(next_tick - current)
            elif current - next_tick > period * 4.0:
                next_tick = current

        return "stopped"

    def _dispatch(
        self,
        target: np.ndarray,
        active_time: float,
        watchdog: MotorFeedbackWatchdog | None,
        recorder: MotorTelemetryRecorder | None,
    ) -> None:
        torque = None
        if hip_roll_grav_comp_enabled(self.config.raw):
            torque = compute_hip_roll_grav_torque(
                target,
                self.config.raw,
                self.config.limb,
                list(self.config.entries),
            )
        self.backend.command_mit(target, torque=torque)
        self.heartbeat.touch()
        self._command_position.set(target)
        with self._lock:
            self._progress.command_count += 1
        if recorder is not None and recorder.blocked_reason is not None:
            raise PlaybackError(recorder.blocked_reason)
        if watchdog is not None and not watchdog.maybe_check(
            target, elapsed_s=active_time
        ):
            raise PlaybackError(
                watchdog.fault_message or "motor feedback watchdog fault"
            )

    def _update_progress(
        self,
        cycle_time: float,
        active_time: float,
        completed_cycles: int,
        target: np.ndarray,
    ) -> None:
        del target
        duration = self.trajectory.duration_s
        frame = int(
            np.searchsorted(
                self.trajectory.timestamps,
                min(cycle_time, duration),
                side="right",
            )
            - 1
        )
        frame = max(0, min(frame, self.trajectory.frame_count - 1))
        with self._lock:
            self._progress.cycle = completed_cycles + 1
            self._progress.completed_cycles = completed_cycles
            self._progress.frame = frame
            self._progress.active_seconds = active_time
            self._progress.trajectory_seconds = cycle_time
            self._progress.fraction = (
                min(1.0, max(0.0, cycle_time / duration))
                if duration > 0.0
                else 1.0
            )

    def _handle_fault(self, exc: Exception) -> None:
        samples: list[MotorSample]
        try:
            samples = self.backend.read_samples()
        except Exception:
            samples = []
        diagnoses = self._diagnosis.diagnose(
            samples,
            heartbeat_age_s=self.heartbeat.age(),
        )
        fault = FaultStatus(
            message=str(exc),
            kind=exc.__class__.__name__,
            timestamp_s=time.monotonic(),
            diagnoses=diagnoses,
        )
        with self._lock:
            self._fault = fault
            self._last_samples = samples
            self._progress.outcome = "fault"
        try:
            self.backend.disable()
        except Exception:
            pass
        current = self.state_machine.state
        if current is not StationState.FAULT:
            try:
                self.state_machine.transition(StationState.FAULT, expected=current)
            except InvalidStateTransition:
                pass
        if self.on_fault is not None:
            self.on_fault(fault)


__all__ = ["PlaybackController", "PlaybackError"]
