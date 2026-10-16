#!/usr/bin/env python3
"""Stage and build the standalone RP1 Factory HMI Debian package."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "rp1-test-hmi"
DEFAULT_VERSION = "0.1.0"
DEFAULT_ARCHITECTURE = "amd64"
STATION_NAMES = (
    "arm_motors.yaml",
    "biped_waist_motors.yaml",
    "left_arm_motors.yaml",
    "left_leg_motors.yaml",
    "left_short_arm_motors.yaml",
    "left_short_leg_motors.yaml",
    "lower_body_motors.yaml",
    "right_arm_motors.yaml",
    "right_leg_motors.yaml",
    "right_short_arm_motors.yaml",
    "right_short_leg_motors.yaml",
    "short_arm_motors.yaml",
    "upper_body_motors.yaml",
    "waist_hip_motors.yaml",
)
DEBIAN_ASSETS = (
    "control",
    "conffiles",
    "postinst",
    "prerm",
    "postrm",
    "gateway.env",
    "uploader.env",
    "can-policy.yaml",
    "rp1-factory-hmi.desktop",
    "rp1-factory-hmi.png",
    "com.rp1.factory-hmi.policy",
    "50-rp1-factory-hmi.rules",
    "10-rp1-factory-hmi.pkla",
)


class PackagingError(RuntimeError):
    """Raised when package inputs or metadata are invalid."""


@dataclass(frozen=True)
class BuildInputs:
    root: Path
    gateway: Path
    desktop: Path
    uploader: Path
    helper: Path
    motors_py: Path
    station_defaults: Path
    plc_config: Path
    reliability_profiles: Path
    stations: tuple[Path, ...]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default=DEFAULT_VERSION, help="Debian package version")
    parser.add_argument(
        "--architecture",
        default=DEFAULT_ARCHITECTURE,
        help="Debian architecture (default: amd64)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "dist",
        help="directory for the versioned package or staging tree",
    )
    parser.add_argument(
        "--staging-only",
        action="store_true",
        help="create the package root without invoking dpkg-deb",
    )
    return parser.parse_args(argv)


def _artifact_problem(path: Path) -> str | None:
    if not path.exists():
        return f"missing frozen application: {path}"
    executable = path / path.name if path.is_dir() else path
    if not executable.is_file():
        return f"missing application executable: {executable}"
    return None


def _motors_candidates(root: Path) -> list[Path]:
    patterns = (
        "dist/motors_py*.so",
        "dist/rp1-factory-gateway/**/motors_py*.so",
        "motors/install/**/motors_py*.so",
        "motors/build-factory-hmi/**/motors_py*.so",
        "build/motors_py*.so",
    )
    candidates: set[Path] = set()
    for pattern in patterns:
        candidates.update(path for path in root.glob(pattern) if path.is_file())
    return sorted(candidates)


def validate_inputs(root: Path = ROOT) -> BuildInputs:
    """Validate all prebuilt inputs before creating any package output."""

    root = root.resolve()
    debian_dir = root / "packaging" / "debian"
    gateway = root / "dist" / "rp1-factory-gateway"
    desktop = root / "dist" / "rp1-factory-desktop"
    uploader = root / "dist" / "rp1-factory-uploader"
    helper = root / "dist" / "factory-hmi-can-helper"
    station_defaults = root / "factory_hmi" / "config" / "station_defaults.yaml"
    plc_config = root / "factory_hmi" / "config" / "plc_cabinet.yaml"
    reliability_profiles = (
        root / "factory_hmi" / "config" / "reliability_platform.yaml"
    )
    stations = tuple(root / "scripts" / "config" / name for name in STATION_NAMES)
    problems: list[str] = []

    for artifact in (gateway, desktop, uploader):
        problem = _artifact_problem(artifact)
        if problem:
            problems.append(problem)
    helper_problem = _artifact_problem(helper)
    if helper_problem:
        problems.append(helper_problem)
    if not station_defaults.is_file():
        problems.append(f"missing station defaults: {station_defaults}")
    if not plc_config.is_file():
        problems.append(f"missing PLC configuration: {plc_config}")
    if not reliability_profiles.is_file():
        problems.append(
            f"missing reliability platform profiles: {reliability_profiles}"
        )

    motors_candidates = _motors_candidates(root)
    if not motors_candidates:
        problems.append(
            "missing bundled motors_py extension; expected motors_py*.so under "
            f"{root / 'dist'}, {root / 'motors' / 'install'}, or {root / 'build'}"
        )

    for name in DEBIAN_ASSETS:
        asset = debian_dir / name
        if not asset.is_file():
            problems.append(f"missing Debian packaging asset: {asset}")
    service = root / "packaging" / "systemd" / "rp1-factory-gateway.service"
    if not service.is_file():
        problems.append(f"missing systemd unit: {service}")
    uploader_service = root / "packaging" / "systemd" / "rp1-factory-uploader.service"
    if not uploader_service.is_file():
        problems.append(f"missing systemd unit: {uploader_service}")
    for station in stations:
        if not station.is_file():
            problems.append(f"missing station configuration: {station}")

    if problems:
        raise PackagingError("\n".join(problems))

    return BuildInputs(
        root=root,
        gateway=gateway,
        desktop=desktop,
        uploader=uploader,
        helper=helper,
        motors_py=motors_candidates[0],
        station_defaults=station_defaults,
        plc_config=plc_config,
        reliability_profiles=reliability_profiles,
        stations=stations,
    )


def _validate_metadata(version: str, architecture: str) -> None:
    if not re.fullmatch(r"[0-9][0-9A-Za-z.+:~\-]*", version):
        raise PackagingError(f"invalid Debian version: {version!r}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", architecture):
        raise PackagingError(f"invalid Debian architecture: {architecture!r}")


def _copy_file(source: Path, destination: Path, mode: int | None = None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    if mode is not None:
        destination.chmod(mode)


def _copy_application(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        for child in source.iterdir():
            target = destination / child.name
            if child.is_dir():
                shutil.copytree(child, target, symlinks=True)
            else:
                shutil.copy2(child, target, follow_symlinks=False)
        executable = destination / source.name
    else:
        executable = destination / source.name
        shutil.copy2(source, executable)
    executable.chmod(executable.stat().st_mode | 0o755)


def _payload_files(package_root: Path) -> list[Path]:
    debian_dir = package_root / "DEBIAN"
    return sorted(
        path
        for path in package_root.rglob("*")
        if path.is_file() and not path.is_symlink() and debian_dir not in path.parents
    )


def _installed_size_kib(package_root: Path) -> int:
    size = sum(path.stat().st_size for path in _payload_files(package_root))
    return max(1, (size + 1023) // 1024)


def _write_md5sums(package_root: Path) -> None:
    lines = []
    for path in _payload_files(package_root):
        digest = hashlib.md5(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.relative_to(package_root).as_posix()}")
    output = package_root / "DEBIAN" / "md5sums"
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    output.chmod(0o644)


def stage_package(
    inputs: BuildInputs,
    package_root: Path,
    *,
    version: str,
    architecture: str,
) -> None:
    """Populate a package root with deterministic system directory modes."""

    previous_umask = os.umask(0o022)
    try:
        _stage_package(
            inputs,
            package_root,
            version=version,
            architecture=architecture,
        )
    finally:
        os.umask(previous_umask)


def _stage_package(
    inputs: BuildInputs,
    package_root: Path,
    *,
    version: str,
    architecture: str,
) -> None:
    """Populate a Debian package root from already-built applications."""

    _validate_metadata(version, architecture)
    debian_source = inputs.root / "packaging" / "debian"
    debian_target = package_root / "DEBIAN"
    debian_target.mkdir(parents=True, exist_ok=True)
    debian_target.chmod(0o755)

    _copy_application(
        inputs.gateway,
        package_root / "opt" / "rp1-test-hmi" / "gateway",
    )
    _copy_application(
        inputs.desktop,
        package_root / "opt" / "rp1-test-hmi" / "desktop",
    )
    _copy_application(
        inputs.uploader,
        package_root / "opt" / "rp1-test-hmi" / "uploader",
    )
    _copy_file(
        inputs.motors_py,
        package_root
        / "opt"
        / "rp1-test-hmi"
        / "motors"
        / "lib"
        / "python3"
        / "site-packages"
        / inputs.motors_py.name,
        0o644,
    )

    _copy_file(
        debian_source / "gateway.env",
        package_root / "etc" / "rp1-test-hmi" / "gateway.env",
        0o640,
    )
    _copy_file(
        debian_source / "uploader.env",
        package_root / "etc" / "rp1-test-hmi" / "uploader.env",
        0o640,
    )
    _copy_file(
        debian_source / "can-policy.yaml",
        package_root / "etc" / "rp1-test-hmi" / "can-policy.yaml",
        0o640,
    )
    _copy_file(
        inputs.station_defaults,
        package_root / "etc" / "rp1-test-hmi" / "station_defaults.yaml",
        0o640,
    )
    _copy_file(
        inputs.plc_config,
        package_root / "etc" / "rp1-test-hmi" / "plc_cabinet.yaml",
        0o640,
    )
    _copy_file(
        inputs.reliability_profiles,
        package_root / "etc" / "rp1-test-hmi" / "reliability_platform.yaml",
        0o640,
    )
    for station in inputs.stations:
        _copy_file(
            station,
            package_root / "etc" / "rp1-test-hmi" / "stations" / station.name,
            0o640,
        )
    for schema_name in (
        "execution-bundle-v1.schema.json",
        "aging-summary-v1.schema.json",
    ):
        _copy_file(
            inputs.root / "factory_hmi" / "contracts" / schema_name,
            package_root / "usr" / "share" / "rp1-test-hmi" / "contracts" / schema_name,
            0o644,
        )

    for directory in (
        package_root / "var" / "lib" / "rp1-test-hmi" / "trajectories",
        package_root / "var" / "lib" / "rp1-test-hmi" / "records",
        package_root / "var" / "lib" / "rp1-test-hmi" / "executions",
        package_root / "var" / "lib" / "rp1-test-hmi" / "exports",
        package_root / "var" / "lib" / "rp1-test-hmi" / "sync",
        package_root / "var" / "lib" / "rp1-test-hmi" / "plc",
    ):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o2770)

    _copy_file(
        inputs.root / "packaging" / "systemd" / "rp1-factory-gateway.service",
        package_root / "lib" / "systemd" / "system" / "rp1-test-gateway.service",
        0o644,
    )
    _copy_file(
        inputs.root / "packaging" / "systemd" / "rp1-factory-uploader.service",
        package_root / "lib" / "systemd" / "system" / "rp1-test-uploader.service",
        0o644,
    )
    _copy_file(
        debian_source / "rp1-factory-hmi.desktop",
        package_root / "usr" / "share" / "applications" / "rp1-test-hmi.desktop",
        0o644,
    )
    _copy_file(
        debian_source / "rp1-factory-hmi.png",
        package_root
        / "usr"
        / "share"
        / "icons"
        / "hicolor"
        / "512x512"
        / "apps"
        / "rp1-test-hmi.png",
        0o644,
    )
    _copy_file(
        inputs.helper,
        package_root
        / "usr"
        / "lib"
        / "rp1-test-hmi"
        / "factory-hmi-can-helper",
        0o755,
    )
    _copy_file(
        debian_source / "com.rp1.factory-hmi.policy",
        package_root
        / "usr"
        / "share"
        / "polkit-1"
        / "actions"
        / "com.rp1.test-hmi.policy",
        0o644,
    )
    _copy_file(
        debian_source / "50-rp1-factory-hmi.rules",
        package_root
        / "usr"
        / "share"
        / "polkit-1"
        / "rules.d"
        / "50-rp1-test-hmi.rules",
        0o644,
    )
    _copy_file(
        debian_source / "10-rp1-factory-hmi.pkla",
        package_root
        / "var"
        / "lib"
        / "polkit-1"
        / "localauthority"
        / "10-vendor.d"
        / "10-rp1-test-hmi.pkla",
        0o644,
    )
    # Match polkitd's protected ownership hierarchy instead of relaxing it
    # through directory modes recorded in the package archive.
    (package_root / "var" / "lib" / "polkit-1").chmod(0o700)
    (package_root / "var" / "lib" / "polkit-1" / "localauthority").chmod(0o700)

    for script in ("postinst", "prerm", "postrm"):
        _copy_file(debian_source / script, debian_target / script, 0o755)
    _copy_file(debian_source / "conffiles", debian_target / "conffiles", 0o644)

    notice_source = inputs.root / "third-party-licenses"
    if notice_source.is_dir():
        shutil.copytree(
            notice_source,
            package_root / "usr/share/doc/rp1-test-hmi/third-party-licenses",
            dirs_exist_ok=True,
        )

    installed_size = _installed_size_kib(package_root)
    control_template = (debian_source / "control").read_text(encoding="utf-8")
    control = (
        control_template.replace("@VERSION@", version)
        .replace("@ARCHITECTURE@", architecture)
        .replace("@INSTALLED_SIZE@", str(installed_size))
    )
    if re.search(r"@[A-Z_]+@", control):
        raise PackagingError("unresolved placeholder in Debian control template")
    (debian_target / "control").write_text(control, encoding="utf-8")
    (debian_target / "control").chmod(0o644)
    _write_md5sums(package_root)


def _artifact_basename(version: str, architecture: str) -> str:
    filename_version = version.replace(":", "%3a")
    return f"{PACKAGE_NAME}_{filename_version}_{architecture}"


def _stage_persistent(
    inputs: BuildInputs,
    destination: Path,
    *,
    version: str,
    architecture: str,
) -> Path:
    if destination.exists():
        raise PackagingError(f"staging destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{destination.name}.",
        dir=destination.parent,
    ) as temporary:
        temporary_root = Path(temporary) / "root"
        temporary_root.mkdir()
        stage_package(
            inputs,
            temporary_root,
            version=version,
            architecture=architecture,
        )
        shutil.move(str(temporary_root), destination)
    return destination


def _build_deb(
    inputs: BuildInputs,
    destination: Path,
    *,
    version: str,
    architecture: str,
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"{PACKAGE_NAME}-") as temporary:
        package_root = Path(temporary) / "root"
        package_root.mkdir()
        stage_package(
            inputs,
            package_root,
            version=version,
            architecture=architecture,
        )
        subprocess.run(
            [
                "dpkg-deb",
                "--build",
                "--root-owner-group",
                str(package_root),
                str(destination),
            ],
            check=True,
        )
    return destination


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        _validate_metadata(args.version, args.architecture)
        inputs = validate_inputs(ROOT)
        output_dir = args.output_dir.resolve()
        basename = _artifact_basename(args.version, args.architecture)
        if args.staging_only:
            result = _stage_persistent(
                inputs,
                output_dir / basename,
                version=args.version,
                architecture=args.architecture,
            )
        else:
            result = _build_deb(
                inputs,
                output_dir / f"{basename}.deb",
                version=args.version,
                architecture=args.architecture,
            )
    except (OSError, PackagingError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
