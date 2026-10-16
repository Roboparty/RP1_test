#!/usr/bin/env python3
"""Build the RP1 gateway or desktop HMI with PyInstaller."""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MOTORS_ROOT = ROOT / "motors"
MOTORS_BUILD_ROOT = MOTORS_ROOT / "build-factory-hmi"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "target",
        choices=("gateway", "desktop", "uploader", "can-helper", "all"),
    )
    parser.add_argument("--one-dir", action="store_true", help="build a directory instead of one executable")
    parser.add_argument(
        "--skip-motors-build",
        action="store_true",
        help="reuse an existing motors_py shared library",
    )
    return parser.parse_args()


def build_motors_py() -> Path:
    subprocess.run(
        [
            "cmake",
            "-S",
            str(MOTORS_ROOT),
            "-B",
            str(MOTORS_BUILD_ROOT),
            "-DCMAKE_BUILD_TYPE=Release",
            "-DCMAKE_DISABLE_FIND_PACKAGE_ament_cmake=ON",
            f"-DPython3_EXECUTABLE={sys.executable}",
            "-DCCACHE_PROGRAM=CCACHE_PROGRAM-NOTFOUND",
            (
                "-DCMAKE_CXX_COMPILER_LAUNCHER="
                f"{sys.executable};{ROOT / 'tools' / 'cxx_portable_launcher.py'}"
            ),
        ],
        cwd=ROOT,
        check=True,
    )
    subprocess.run(
        [
            "cmake",
            "--build",
            str(MOTORS_BUILD_ROOT),
            "--target",
            "motors_py",
            "--parallel",
            os.environ.get("RP1_BUILD_JOBS", "2"),
        ],
        cwd=ROOT,
        check=True,
    )
    return find_motors_py()


def find_motors_py() -> Path:
    candidates = sorted(MOTORS_BUILD_ROOT.rglob("motors_py*.so"))
    if len(candidates) != 1:
        raise SystemExit(
            f"expected one motors_py shared library under {MOTORS_BUILD_ROOT}; "
            f"found {len(candidates)}"
        )
    return candidates[0]


def build(target: str, *, one_dir: bool, motors_py: Path | None) -> None:
    if importlib.util.find_spec("PyInstaller") is None:
        raise SystemExit('PyInstaller is missing; run: python -m pip install -e ".[build]"')
    if target == "desktop" and importlib.util.find_spec("PySide6") is None:
        raise SystemExit(
            'PySide6 is missing; run: python -m pip install -e ".[build,desktop]"'
        )
    if target == "gateway" and importlib.util.find_spec("pymodbus") is None:
        raise SystemExit(
            'pymodbus is missing; run: python -m pip install "pymodbus==3.8.6"'
        )

    entry = (
        ROOT / "factory_hmi" / "can_helper.py"
        if target == "can-helper"
        else ROOT / "factory_hmi" / target / "__main__.py"
    )
    if not entry.is_file():
        raise SystemExit(f"missing entry point: {entry}")

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--paths",
        str(ROOT),
        "--hidden-import",
        "pyroute2",
        "--exclude-module",
        "matplotlib",
        "--name",
        (
            "factory-hmi-can-helper"
            if target == "can-helper"
            else f"rp1-factory-{target}"
        ),
        "--onedir" if one_dir else "--onefile",
    ]
    if target in {"gateway", "desktop"}:
        if motors_py is None:
            raise SystemExit("motors_py is required for gateway and desktop builds")
        command.extend(
            [
                "--add-data",
                f"{ROOT / 'factory_hmi' / 'config'}{os.pathsep}factory_hmi/config",
                "--add-data",
                f"{ROOT / 'scripts' / 'config'}{os.pathsep}scripts/config",
                "--add-binary",
                f"{motors_py}{os.pathsep}.",
                "--hidden-import",
                "scripts.log_motor_telemetry",
                "--hidden-import",
                "scripts.motor_feedback_watchdog",
                "--hidden-import",
                "scripts.rp1_close_chain",
                "--hidden-import",
                "scripts.rp1_limb_common",
                "--hidden-import",
                "motors_py",
            ]
        )
    if target in {"gateway", "uploader"}:
        command.extend(
            [
                "--add-data",
                f"{ROOT / 'factory_hmi' / 'contracts'}{os.pathsep}factory_hmi/contracts",
            ]
        )
    if target == "desktop":
        command.extend(
            [
                "--add-data",
                (
                    f"{ROOT / 'factory_hmi' / 'desktop' / 'assets'}"
                    f"{os.pathsep}factory_hmi/desktop/assets"
                ),
                "--windowed",
                "--hidden-import",
                "PySide6.QtWebSockets",
            ]
        )
    elif target == "gateway":
        command.extend(
            [
                "--collect-all",
                "pymodbus",
                "--hidden-import",
                "factory_hmi.sync.uploader",
                "--hidden-import",
                "factory_hmi.sync.reliability_platform",
                "--hidden-import",
                "uvicorn.logging",
                "--hidden-import",
                "uvicorn.loops.auto",
                "--hidden-import",
                "uvicorn.protocols.http.auto",
                "--hidden-import",
                "uvicorn.protocols.websockets.auto",
            ]
        )
    command.append(str(entry))
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> int:
    args = parse_args()
    motors_py = (
        None
        if args.target in {"can-helper", "uploader"}
        else (
            find_motors_py()
            if args.skip_motors_build
            else build_motors_py()
        )
    )
    targets = (
        ("gateway", "desktop", "uploader", "can-helper")
        if args.target == "all"
        else (args.target,)
    )
    for target in targets:
        build(target, one_dir=bool(args.one_dir) and target != "can-helper", motors_py=motors_py)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
