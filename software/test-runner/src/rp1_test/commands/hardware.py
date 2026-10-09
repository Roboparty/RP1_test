"""Hardware CLI commands."""

from __future__ import annotations

import argparse
import json
import select
import sys
import time

import numpy as np

from ..artifacts import RunArtifacts
from ..hardware import HardwareRunner, cleanup_motors, load_adapter, preflight
from ..mapping import capability_gaps, load_joint_mappings
from ..trajectory import verify_trajectory_sha
from .common import _model_from_args, load_config


def hardware_preflight(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    model = _model_from_args(args, config)
    trajectory = preflight(
        args.trajectory,
        config=config,
        model_path=model,
        require_sha=True,
    )
    print(
        json.dumps(
            {
                "ready": True,
                "limb": trajectory.limb,
                "control_rate_hz": float(config.get("hardware", {}).get("control_rate_hz", 200)),
                "sha_verified": True,
                "telemetry_gaps": capability_gaps(config),
                "hardware_touched": False,
            },
            sort_keys=True,
        )
    )
    return 0


class TerminalGate:
    def __init__(self) -> None:
        self._attributes = None

    def __enter__(self) -> TerminalGate:
        if sys.stdin.isatty():
            import termios
            import tty

            self._attributes = termios.tcgetattr(sys.stdin.fileno())
            tty.setcbreak(sys.stdin.fileno())
        return self

    def __exit__(self, *_args: object) -> None:
        if self._attributes is not None:
            import termios

            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._attributes)

    def read(self, timeout: float | None = None) -> str:
        if timeout is None:
            print(
                "[R] default  [P] play  [Space] pause/hold  [Q] quit: ",
                end="",
                flush=True,
            )
        readable, _, _ = select.select([sys.stdin], [], [], timeout)
        if not readable:
            return ""
        value = sys.stdin.read(1)
        if timeout is None:
            print()
        return value


def hardware_replay(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    mappings = load_joint_mappings(config)
    model = _model_from_args(args, config)
    trajectory = preflight(
        args.trajectory,
        config=config,
        model_path=model,
        require_sha=True,
    )
    hardware = config.get("hardware", {})
    print("SHA-256 verified; local gate is R/P/Space/Q; MIT default rate is 200 Hz")
    if args.dry_run:
        print("dry-run: no hardware adapter imported and no motor command sent")
        return 0
    if not args.real_hardware or args.acknowledge != "I_UNDERSTAND_RP1_MOTION":
        raise RuntimeError(
            "real replay requires --real-hardware --acknowledge I_UNDERSTAND_RP1_MOTION"
        )
    adapter = str(hardware.get("adapter_module", "")).strip()
    if not adapter:
        raise RuntimeError("hardware.adapter_module is required for real replay")
    run_id = args.run_id or time.strftime("rp1-%Y%m%d-%H%M%S")
    trajectory_info = {
        "trajectory_sha256": verify_trajectory_sha(args.trajectory)["npz_sha256"],
        "trajectory_file_name": args.trajectory.name,
        "trajectory_size_bytes": args.trajectory.stat().st_size,
        "trajectory_key": args.trajectory.stem,
        "trajectory_version": 1,
        "trajectory_point_count": int(trajectory.motor_pos.shape[0]),
    }
    # Filesystem failures must happen before any device is initialized.
    artifacts = RunArtifacts(args.output_dir, run_id, args.config)
    motors = []
    runner = None
    runner_entered = False
    primary_error = None
    status = "completed"
    termination_kind = "NORMAL"
    issues = ""
    exception_count = 0
    try:
        artifacts.start_telemetry(
            [
                "time_s",
                "joint_name",
                "motor_index",
                "cmd_pos_rad",
                "pos_rad",
                "spd_rad_s",
                "torque_nm",
                "temp_c",
                "error_id",
            ]
        )
        motors = load_adapter(adapter, config)
        runner = HardwareRunner(
            motors,
            mappings,
            trajectory,
            default_pose=np.asarray(config["default_pose_rad"], dtype=np.float64),
            safety_config=config,
            control_rate_hz=float(hardware.get("control_rate_hz", 200.0)),
            kp=float(hardware.get("kp", 20.0)),
            kd=float(hardware.get("kd", 0.5)),
            transition_seconds=float(hardware.get("transition_seconds", 2.0)),
            event_sink=artifacts.write_event,
            telemetry_sink=artifacts.write_telemetry,
        )
        with TerminalGate() as gate:
            runner_entered = True  # run() owns cleanup from its first feedback read.
            runner.run(gate.read, loop=args.loop)
    except BaseException as exc:
        primary_error = exc
        if not runner_entered:
            cleanup_motors(motors, primary_error=exc, event_sink=artifacts.write_event)
        status = "failed"
        termination_kind = (
            "SAFETY_WATCHDOG" if runner is not None and runner.watchdog.fault else "SYSTEM_CRASH"
        )
        issues = str(exc)
        exception_count = 1
        try:
            artifacts.write_event({"type": "replay_failed", "detail": str(exc)})
        except BaseException as recording_error:
            if hasattr(exc, "add_note"):
                exc.add_note(f"Failure recording event: {recording_error}")
        raise
    finally:
        intervals = runner.active_intervals if runner is not None else []
        try:
            manifest = artifacts.finalize(
                status=status,
                extra={
                    **trajectory_info,
                    "coordinate_frame": "motor",
                    "source_kind": "real",
                    "active_seconds": sum(item["end_s"] - item["start_s"] for item in intervals),
                    "confirmed_active_intervals": intervals,
                    "active_time_scope": "trajectory playback; excludes entry/default transitions and operator pauses",
                    "termination_kind": termination_kind,
                    "issues": issues,
                    "exception_count": exception_count,
                    "telemetry_capabilities": [
                        getattr(motor, "capabilities", {}) for motor in motors
                    ],
                    "telemetry_gaps": capability_gaps(config),
                },
            )
        except BaseException as finalization_error:
            if primary_error is None:
                raise
            if hasattr(primary_error, "add_note"):
                primary_error.add_note(f"Failure finalizing artifacts: {finalization_error}")
        else:
            print(f"manifest: {manifest}")
    return 0
