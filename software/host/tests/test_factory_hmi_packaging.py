from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree

from factory_hmi.can_helper import load_policy


ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "tools" / "build_factory_hmi_deb.py"
SPEC = importlib.util.spec_from_file_location("build_factory_hmi_deb", BUILD_SCRIPT)
assert SPEC is not None and SPEC.loader is not None
packaging = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = packaging
SPEC.loader.exec_module(packaging)


class PackageFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        shutil.copytree(ROOT / "packaging", self.root / "packaging")

        dist = self.root / "dist"
        dist.mkdir()
        for name in (
            "rp1-factory-gateway",
            "rp1-factory-desktop",
            "rp1-factory-uploader",
            "factory-hmi-can-helper",
        ):
            artifact = dist / name
            artifact.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            artifact.chmod(0o755)

        motors_py = self.root / "build" / "motors_py.cpython-310-x86_64-linux-gnu.so"
        motors_py.parent.mkdir()
        motors_py.write_bytes(b"test motors extension")

        defaults = self.root / "factory_hmi" / "config" / "station_defaults.yaml"
        defaults.parent.mkdir(parents=True)
        defaults.write_text("factory_hmi: {}\n", encoding="utf-8")
        (defaults.parent / "plc_cabinet.yaml").write_text(
            "driver: mock\n"
            "timings:\n  poll_ms: 200\n"
            "dut_can_map: {DUT1: can0, DUT2: can1, DUT3: can2, DUT4: can3}\n",
            encoding="utf-8",
        )
        (defaults.parent / "reliability_platform.yaml").write_text(
            "profiles: []\n",
            encoding="utf-8",
        )
        shutil.copytree(
            ROOT / "factory_hmi" / "contracts",
            self.root / "factory_hmi" / "contracts",
        )

        station_dir = self.root / "scripts" / "config"
        station_dir.mkdir(parents=True)
        for name in packaging.STATION_NAMES:
            (station_dir / name).write_text("motor_id: [1]\n", encoding="utf-8")

    def cleanup(self) -> None:
        self.temporary.cleanup()


class FactoryHmiPackagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = PackageFixture()

    def tearDown(self) -> None:
        self.fixture.cleanup()

    def _stage(self) -> Path:
        inputs = packaging.validate_inputs(self.fixture.root)
        package_root = self.fixture.root / "package-root"
        package_root.mkdir()
        packaging.stage_package(
            inputs,
            package_root,
            version="1.2.3-1",
            architecture="amd64",
        )
        return package_root

    def test_staged_layout_and_generated_metadata(self) -> None:
        package_root = self._stage()
        expected_files = (
            "opt/rp1-test-hmi/gateway/rp1-factory-gateway",
            "opt/rp1-test-hmi/desktop/rp1-factory-desktop",
            "opt/rp1-test-hmi/uploader/rp1-factory-uploader",
            "opt/rp1-test-hmi/motors/lib/python3/site-packages/"
            "motors_py.cpython-310-x86_64-linux-gnu.so",
            "etc/rp1-test-hmi/gateway.env",
            "etc/rp1-test-hmi/uploader.env",
            "etc/rp1-test-hmi/can-policy.yaml",
            "etc/rp1-test-hmi/station_defaults.yaml",
            "etc/rp1-test-hmi/plc_cabinet.yaml",
            "etc/rp1-test-hmi/reliability_platform.yaml",
            "lib/systemd/system/rp1-test-gateway.service",
            "lib/systemd/system/rp1-test-uploader.service",
            "usr/share/applications/rp1-test-hmi.desktop",
            "usr/share/icons/hicolor/512x512/apps/rp1-test-hmi.png",
            "usr/share/rp1-test-hmi/contracts/execution-bundle-v1.schema.json",
            "usr/share/rp1-test-hmi/contracts/aging-summary-v1.schema.json",
            "usr/lib/rp1-test-hmi/factory-hmi-can-helper",
            "usr/share/polkit-1/actions/com.rp1.test-hmi.policy",
            "usr/share/polkit-1/rules.d/50-rp1-test-hmi.rules",
            "var/lib/polkit-1/localauthority/10-vendor.d/10-rp1-test-hmi.pkla",
            "DEBIAN/control",
            "DEBIAN/conffiles",
            "DEBIAN/md5sums",
            "DEBIAN/postinst",
            "DEBIAN/prerm",
            "DEBIAN/postrm",
        )
        for relative in expected_files:
            self.assertTrue((package_root / relative).is_file(), relative)
        for relative in (
            "var/lib/rp1-test-hmi/trajectories",
            "var/lib/rp1-test-hmi/records",
            "var/lib/rp1-test-hmi/executions",
            "var/lib/rp1-test-hmi/exports",
            "var/lib/rp1-test-hmi/sync",
            "var/lib/rp1-test-hmi/plc",
        ):
            self.assertTrue((package_root / relative).is_dir(), relative)

        control = (package_root / "DEBIAN" / "control").read_text(encoding="utf-8")
        self.assertIn("Version: 1.2.3-1", control)
        self.assertIn("Architecture: amd64", control)
        self.assertRegex(control, r"(?m)^Installed-Size: [1-9][0-9]*$")
        self.assertNotRegex(control, r"@[A-Z_]+@")

        md5_lines = (
            (package_root / "DEBIAN" / "md5sums")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        md5_by_path = {
            line.split("  ", 1)[1]: line.split("  ", 1)[0] for line in md5_lines
        }
        gateway_path = "opt/rp1-test-hmi/gateway/rp1-factory-gateway"
        self.assertEqual(
            md5_by_path[gateway_path],
            hashlib.md5((package_root / gateway_path).read_bytes()).hexdigest(),
        )
        self.assertFalse(any(path.startswith("DEBIAN/") for path in md5_by_path))

    def test_control_and_conffiles_cover_only_runtime_configuration(self) -> None:
        package_root = self._stage()
        control = (package_root / "DEBIAN" / "control").read_text(encoding="utf-8")
        depends = next(
            line.removeprefix("Depends: ").split(", ")
            for line in control.splitlines()
            if line.startswith("Depends: ")
        )
        self.assertTrue({"iproute2", "libxcb-cursor0", "passwd", "policykit-1",
                         "ca-certificates", "libegl1", "fonts-noto-cjk"} <= set(depends))
        self.assertNotRegex(control.lower(), r"venv|source tree|pyinstaller|cmake")

        conffiles = set(
            (package_root / "DEBIAN" / "conffiles")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        expected = {
            "/etc/rp1-test-hmi/gateway.env",
            "/etc/rp1-test-hmi/uploader.env",
            "/etc/rp1-test-hmi/can-policy.yaml",
            "/etc/rp1-test-hmi/station_defaults.yaml",
            "/etc/rp1-test-hmi/plc_cabinet.yaml",
            "/etc/rp1-test-hmi/reliability_platform.yaml",
            *{f"/etc/rp1-test-hmi/stations/{name}" for name in packaging.STATION_NAMES},
        }
        self.assertEqual(conffiles, expected)
        for conffile in conffiles:
            self.assertTrue((package_root / conffile.lstrip("/")).is_file(), conffile)

    def test_package_is_installable_beside_the_original_hmi(self) -> None:
        package_root = self._stage()
        control = (package_root / "DEBIAN" / "control").read_text(encoding="utf-8")
        self.assertIn("Package: rp1-test-hmi", control)
        self.assertNotIn("Conflicts:", control)
        self.assertNotIn("Replaces:", control)

        installed_paths = {
            path.relative_to(package_root).as_posix()
            for path in package_root.rglob("*")
        }
        forbidden_prefixes = (
            "etc/rp1-factory-hmi",
            "opt/rp1-factory-hmi",
            "var/lib/rp1-factory-hmi",
            "usr/lib/rp1-factory-hmi",
            "usr/share/rp1-factory-hmi",
        )
        self.assertFalse(
            any(
                path == prefix or path.startswith(f"{prefix}/")
                for path in installed_paths
                for prefix in forbidden_prefixes
            )
        )
        self.assertNotIn(
            "lib/systemd/system/rp1-factory-gateway.service", installed_paths
        )
        self.assertNotIn(
            "lib/systemd/system/rp1-factory-uploader.service", installed_paths
        )

    def test_scripts_are_executable_and_shell_syntax_is_valid(self) -> None:
        package_root = self._stage()
        scripts = (
            package_root / "DEBIAN" / "postinst",
            package_root / "DEBIAN" / "prerm",
            package_root / "DEBIAN" / "postrm",
            package_root / "usr" / "lib" / "rp1-test-hmi" / "factory-hmi-can-helper",
        )
        for script in scripts:
            self.assertTrue(os.access(script, os.X_OK), script)
            subprocess.run(["sh", "-n", str(script)], check=True)

        postinst = scripts[0].read_text(encoding="utf-8")
        postrm = scripts[2].read_text(encoding="utf-8")
        self.assertIn("groupadd --system rp1-factory", postinst)
        self.assertIn("useradd --system --gid rp1-factory", postinst)
        self.assertIn("[ -d /run/systemd/system ]", postinst)
        self.assertIn("try-reload-or-restart polkit.service", postinst)
        self.assertIn("systemctl enable", postinst)
        self.assertIn("systemctl start", postinst)
        self.assertIn(
            "install -d -o rp1-test-gateway -g rp1-factory -m 0750 "
            "/var/lib/rp1-test-hmi/sync",
            postinst,
        )
        self.assertIn(
            'chown rp1-test-gateway:rp1-factory "$database"',
            postinst,
        )
        self.assertIn('chmod 0660 "$database"', postinst)
        self.assertNotRegex(postrm, r"\brm\b.*?/var/lib/rp1-test-hmi")

    def test_desktop_polkit_and_systemd_integration_is_consistent(self) -> None:
        package_root = self._stage()
        desktop = (
            package_root / "usr" / "share" / "applications" / "rp1-test-hmi.desktop"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "Exec=/opt/rp1-test-hmi/desktop/rp1-factory-desktop "
            "--gateway-url http://127.0.0.1:8766",
            desktop,
        )
        self.assertIn("Icon=rp1-test-hmi", desktop)
        icon = (
            package_root
            / "usr"
            / "share"
            / "icons"
            / "hicolor"
            / "512x512"
            / "apps"
            / "rp1-test-hmi.png"
        ).read_bytes()
        self.assertEqual(icon[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(int.from_bytes(icon[16:20], "big"), 512)
        self.assertEqual(int.from_bytes(icon[20:24], "big"), 512)

        unit = (
            package_root / "lib" / "systemd" / "system" / "rp1-test-gateway.service"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "ExecStart=/opt/rp1-test-hmi/gateway/rp1-factory-gateway",
            unit,
        )
        self.assertIn("--host 127.0.0.1 --port 8766", unit)
        self.assertIn("EnvironmentFile=-/etc/rp1-test-hmi/gateway.env", unit)
        self.assertIn("AmbientCapabilities=CAP_NET_RAW", unit)
        self.assertNotIn("RestrictAddressFamilies=", unit)
        self.assertNotIn("CapabilityBoundingSet=", unit)
        self.assertNotIn("NoNewPrivileges=true", unit)
        self.assertNotIn(".venv", unit)

        policy_path = (
            package_root
            / "usr"
            / "share"
            / "polkit-1"
            / "actions"
            / "com.rp1.test-hmi.policy"
        )
        policy = ElementTree.parse(policy_path)
        annotation = policy.find(
            ".//annotate[@key='org.freedesktop.policykit.exec.path']"
        )
        self.assertIsNotNone(annotation)
        self.assertEqual(
            annotation.text,
            "/usr/lib/rp1-test-hmi/factory-hmi-can-helper",
        )
        rule = (
            package_root
            / "usr"
            / "share"
            / "polkit-1"
            / "rules.d"
            / "50-rp1-test-hmi.rules"
        ).read_text(encoding="utf-8")
        self.assertIn('subject.user == "rp1-test-gateway"', rule)
        self.assertIn('subject.isInGroup("rp1-factory")', rule)
        self.assertIn('action.lookup("program") == helper', rule)
        self.assertIn("polkit.Result.YES", rule)
        legacy_rule = (
            package_root
            / "var"
            / "lib"
            / "polkit-1"
            / "localauthority"
            / "10-vendor.d"
            / "10-rp1-test-hmi.pkla"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "Identity=unix-group:rp1-factory;unix-user:rp1-test-gateway",
            legacy_rule,
        )
        self.assertIn("Action=com.rp1.test-hmi.configure-can", legacy_rule)
        self.assertIn("ResultAny=yes", legacy_rule)
        self.assertEqual(
            (package_root / "var" / "lib" / "polkit-1").stat().st_mode & 0o777,
            0o700,
        )
        self.assertEqual(
            (package_root / "usr" / "share" / "polkit-1" / "actions").stat().st_mode
            & 0o777,
            0o755,
        )
        can_policy = load_policy(
            package_root / "etc" / "rp1-test-hmi" / "can-policy.yaml",
            require_root_owned=False,
        )
        self.assertIn("can0", can_policy.allowed_interfaces)
        self.assertEqual(
            can_policy.allowed_bitrates,
            frozenset({125_000, 250_000, 500_000, 1_000_000}),
        )
        self.assertEqual(
            can_policy.allowed_dbitrates,
            frozenset({1_000_000, 2_000_000, 4_000_000, 5_000_000}),
        )
        self.assertTrue(can_policy.allow_classic_can)
        self.assertTrue(can_policy.allow_can_fd)

    def test_missing_prebuilt_input_fails_before_staging_or_dpkg(self) -> None:
        (self.fixture.root / "dist" / "rp1-factory-gateway").unlink()
        output_dir = self.fixture.root / "output"
        stderr = io.StringIO()
        with (
            mock.patch.object(packaging, "ROOT", self.fixture.root),
            mock.patch.object(packaging.subprocess, "run") as run,
            contextlib.redirect_stderr(stderr),
        ):
            result = packaging.main(
                [
                    "--version",
                    "1.0.0",
                    "--architecture",
                    "amd64",
                    "--output-dir",
                    str(output_dir),
                    "--staging-only",
                ]
            )
        self.assertEqual(result, 2)
        self.assertIn("missing frozen application", stderr.getvalue())
        self.assertFalse(output_dir.exists())
        run.assert_not_called()

    def test_builder_invokes_only_dpkg_deb_after_staging(self) -> None:
        inputs = packaging.validate_inputs(self.fixture.root)
        output = self.fixture.root / "output" / "rp1-test-hmi_1.0.0_amd64.deb"
        with mock.patch.object(packaging.subprocess, "run") as run:
            packaging._build_deb(
                inputs,
                output,
                version="1.0.0",
                architecture="amd64",
            )
        command = run.call_args.args[0]
        self.assertEqual(command[0:3], ["dpkg-deb", "--build", "--root-owner-group"])
        self.assertNotRegex(" ".join(command).lower(), r"pyinstaller|cmake")
        self.assertTrue(run.call_args.kwargs["check"])

    @unittest.skipUnless(shutil.which("dpkg-deb"), "dpkg-deb is unavailable")
    def test_dpkg_deb_builds_an_inspectable_package(self) -> None:
        inputs = packaging.validate_inputs(self.fixture.root)
        output = self.fixture.root / "output" / "rp1-test-hmi_1.0.0_amd64.deb"
        packaging._build_deb(
            inputs,
            output,
            version="1.0.0",
            architecture="amd64",
        )
        self.assertTrue(output.is_file())
        inspected = subprocess.run(
            ["dpkg-deb", "--field", str(output), "Package", "Version", "Architecture"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("Package: rp1-test-hmi", inspected.stdout)
        self.assertIn("Version: 1.0.0", inspected.stdout)
        self.assertIn("Architecture: amd64", inspected.stdout)


if __name__ == "__main__":
    unittest.main()
