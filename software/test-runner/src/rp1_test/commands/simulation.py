"""Simulation CLI commands."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace

from ..close_chain import motor_frames_to_joint
from ..simulation import replay as sim_replay
from ..simulation import replay_summary
from ..trajectory import load_trajectory
from .common import _model_from_args, load_config


def simulation_replay(args: argparse.Namespace) -> int:
    trajectory = load_trajectory(args.trajectory, verify_metadata=True)
    config = load_config(args.config)
    trajectory = replace(trajectory, motor_pos=motor_frames_to_joint(trajectory.motor_pos, config))
    model = _model_from_args(args, config)
    summary = replay_summary(trajectory, model)
    print(json.dumps(summary, sort_keys=True))
    if args.dry_run:
        if not summary["model_available"]:
            print(
                "dry-run: trajectory is valid; rp1_description model is unavailable",
                file=sys.stderr,
            )
        return 0
    sim_replay(
        trajectory,
        model_path=model,
        loop=args.loop,
        cycles=args.cycles,
        headless=args.headless,
    )
    return 0
