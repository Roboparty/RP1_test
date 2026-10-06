#!/usr/bin/env python3
"""Run a clean-install Fake backend acceptance flow against the local gateway."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib import error, parse, request

import numpy as np


CLIENT_ID = "standalone-acceptance"


def call(
    base_url: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    actual_headers = {
        "Accept": "application/json",
        "X-RP1-Client-ID": CLIENT_ID,
        **(headers or {}),
    }
    data = body
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        actual_headers["Content-Type"] = "application/json"
    response = request.urlopen(
        request.Request(
            f"{base_url.rstrip('/')}{path}",
            data=data,
            headers=actual_headers,
            method=method,
        ),
        timeout=10.0,
    )
    with response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict):
        raise RuntimeError(f"{path} returned a non-object JSON response")
    return result


def wait_for_gateway(base_url: str, timeout_s: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            call(base_url, "GET", "/api/v1/health")
            return
        except (OSError, error.URLError):
            if time.monotonic() >= deadline:
                raise RuntimeError("gateway did not become healthy before timeout")
            time.sleep(0.2)


def expect_conflict(
    base_url: str,
    path: str,
    payload: dict[str, Any],
) -> None:
    try:
        call(base_url, "POST", path, payload)
    except error.HTTPError as exc:
        if exc.code == 409:
            return
        raise
    raise RuntimeError(f"{path} unexpectedly accepted an unsafe operation")


def run(base_url: str, config_path: Path) -> None:
    wait_for_gateway(base_url)
    configured = call(
        base_url,
        "POST",
        "/api/v1/configure",
        {
            "config_path": str(config_path),
            "limb": "left_arm",
            "backend": "fake",
        },
    )
    if configured["result"]["configured"] is not True:
        raise RuntimeError("station did not become configured")

    initial_can = call(base_url, "GET", "/api/v1/can/status")
    if initial_can["result"]["all_up"]:
        raise RuntimeError("Fake CAN unexpectedly started connected")
    connected_can = call(base_url, "POST", "/api/v1/can/connect")
    if connected_can["result"]["can"]["all_up"] is not True:
        raise RuntimeError("Fake CAN did not connect")

    discovered = call(base_url, "POST", "/api/v1/motors/discover")
    motor_count = len(discovered["result"]["motors"])
    if motor_count != 7:
        raise RuntimeError(f"expected 7 arm motors, discovered {motor_count}")
    expect_conflict(
        base_url,
        "/api/v1/motors/enable",
        {"physical_estop_confirmed": False},
    )
    enabled = call(
        base_url,
        "POST",
        "/api/v1/motors/enable",
        {"physical_estop_confirmed": True},
    )
    if enabled["result"]["station"]["motors_enabled"] is not True:
        raise RuntimeError("motors did not enable")
    expect_conflict(
        base_url,
        "/api/v1/motors/enable",
        {"physical_estop_confirmed": True},
    )

    with tempfile.TemporaryDirectory() as temporary:
        trajectory = Path(temporary) / "acceptance.npz"
        np.savez(
            trajectory,
            motor_pos=np.zeros((2, motor_count), dtype=np.float64),
            fps=np.asarray([200.0]),
            time=np.asarray([0.0, 0.005]),
            limb=np.asarray(["left_arm"]),
        )
        imported = call(
            base_url,
            "POST",
            "/api/v1/trajectory/upload",
            body=trajectory.read_bytes(),
            headers={
                "Content-Type": "application/octet-stream",
                "X-Filename": trajectory.name,
            },
        )
    if imported["result"]["preflight"]["safe"] is not True:
        raise RuntimeError("acceptance trajectory failed preflight")

    call(base_url, "POST", "/api/v1/arm")
    call(
        base_url,
        "POST",
        "/api/v1/playback/start",
        {
            "speed": 1.0,
            "loop": False,
            "cycles": 1,
            "record": False,
            "test_id": "ci-acceptance",
            "robot_id": "FAKE-001",
        },
    )
    deadline = time.monotonic() + 10.0
    while True:
        snapshot = call(base_url, "GET", "/api/v1/snapshot")
        if snapshot["state"] == "completed":
            break
        if snapshot["state"] == "fault":
            raise RuntimeError(f"Fake playback faulted: {snapshot.get('fault')}")
        if time.monotonic() >= deadline:
            raise RuntimeError("Fake playback did not complete before timeout")
        time.sleep(0.05)

    call(base_url, "POST", "/api/v1/motors/disable")
    call(base_url, "POST", "/api/v1/disconnect")
    disconnected = call(base_url, "POST", "/api/v1/can/disconnect")
    if disconnected["result"]["can"]["all_up"]:
        raise RuntimeError("Fake CAN did not disconnect")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--gateway-executable",
        type=Path,
        help="start and stop this frozen gateway for the acceptance run",
    )
    args = parser.parse_args()
    config = args.config.resolve(strict=True)
    gateway = (
        args.gateway_executable.resolve(strict=True)
        if args.gateway_executable is not None
        else None
    )
    if gateway is None:
        run(args.url, config)
    else:
        parsed_url = parse.urlsplit(args.url)
        if parsed_url.hostname is None or parsed_url.port is None:
            raise SystemExit("--url must include a host and port")
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        environment["RP1_FACTORY_ALLOWED_CONFIG_ROOT"] = str(config.parent)
        with tempfile.TemporaryDirectory() as data_root:
            process = subprocess.Popen(
                [
                    str(gateway),
                    "--host",
                    parsed_url.hostname,
                    "--port",
                    str(parsed_url.port),
                    "--data-root",
                    data_root,
                ],
                cwd="/tmp",
                env=environment,
            )
            try:
                run(args.url, config)
            finally:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=10.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5.0)
            if process.returncode != 0:
                raise RuntimeError(
                    f"gateway exited with status {process.returncode}"
                )
    print("standalone Fake acceptance passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
