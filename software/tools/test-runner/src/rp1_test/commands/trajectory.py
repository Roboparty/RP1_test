"""Trajectory CLI commands."""

from __future__ import annotations

import argparse
import json

from ..close_chain import joint_frames_to_motor
from ..generators import generate
from ..mapping import load_joint_mappings
from ..recording import record_ik_keyframes, record_joint_keyframes
from ..trajectory import (
    fingerprint_config,
    fingerprint_file,
    load_trajectory,
    verify_trajectory_sha,
    write_trajectory,
)
from ..validation import validate_trajectory_safety
from .common import _model_from_args, _params, load_config


def trajectory_generate(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    mappings = load_joint_mappings(config)
    names = [item.joint_name for item in mappings]
    values, timestamps = generate(args.profile, names, fps=args.fps, params=_params(args.params))
    values = joint_frames_to_motor(values, config)
    model = _model_from_args(args, config)
    write_trajectory(
        args.output,
        limb=str(config["limb"]),
        joint_names=names,
        motor_pos=values,
        time=timestamps,
        fps=args.fps,
        model_fingerprint=fingerprint_file(model),
        config_fingerprint=fingerprint_config(config),
        generated_by=f"rp1-test trajectory generate {args.profile}",
        generation={"profile": args.profile, "params": _params(args.params)},
    )
    print(args.output.resolve())
    return 0


def trajectory_record(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    mappings = load_joint_mappings(config)
    names = [item.joint_name for item in mappings]
    model = _model_from_args(args, config)
    if model is None or not model.is_file():
        raise RuntimeError(
            "rp1_description MuJoCo model is required; pass --model or set RP1_DESCRIPTION_ROOT"
        )
    if not args.keyframes.is_file():
        raise FileNotFoundError(f"keyframe input not found: {args.keyframes}")
    if args.dry_run:
        print(json.dumps({"mode": args.mode, "model": str(model), "input": str(args.keyframes)}))
        return 0
    if args.mode == "joint":
        values, timestamps = record_joint_keyframes(
            model,
            args.keyframes,
            names,
            fps=args.fps,
            segment_seconds=args.segment_seconds,
        )
    else:
        values, timestamps = record_ik_keyframes(
            model,
            args.keyframes,
            names,
            site_name=args.site,
            fps=args.fps,
            segment_seconds=args.segment_seconds,
        )
    values = joint_frames_to_motor(values, config)
    write_trajectory(
        args.output,
        limb=str(config["limb"]),
        joint_names=names,
        motor_pos=values,
        time=timestamps,
        fps=args.fps,
        model_fingerprint=fingerprint_file(model),
        config_fingerprint=fingerprint_config(config),
        generated_by=f"rp1-test trajectory record {args.mode}",
        generation={"mode": args.mode, "source": str(args.keyframes), "site": args.site},
    )
    print(args.output.resolve())
    return 0


def trajectory_validate(args: argparse.Namespace) -> int:
    trajectory = load_trajectory(args.trajectory, verify_metadata=True)
    config = load_config(args.config)
    model = _model_from_args(args, config)
    validation = validate_trajectory_safety(
        trajectory,
        config,
        model_path=model,
        require_model_fingerprint=bool(
            config.get("safety", {}).get("require_model_fingerprint", True)
        ),
    )
    metadata = verify_trajectory_sha(args.trajectory)
    print(
        json.dumps(
            {
                "valid": True,
                "schema_version": trajectory.schema_version,
                "limb": trajectory.limb,
                "frames": int(trajectory.motor_pos.shape[0]),
                "joints": len(trajectory.joint_names),
                "sha256": metadata["npz_sha256"],
                "safety": validation,
            },
            sort_keys=True,
        )
    )
    return 0
