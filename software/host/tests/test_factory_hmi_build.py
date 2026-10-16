from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "build_factory_hmi.py"
SPEC = importlib.util.spec_from_file_location("build_factory_hmi", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
builder = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = builder
SPEC.loader.exec_module(builder)


class FactoryHmiBuildTests(unittest.TestCase):
    def test_privileged_helper_build_excludes_motor_runtime(self) -> None:
        with (
            mock.patch.object(builder.importlib.util, "find_spec", return_value=object()),
            mock.patch.object(builder.subprocess, "run") as run,
        ):
            builder.build("can-helper", one_dir=False, motors_py=None)

        command = run.call_args.args[0]
        self.assertIn("factory-hmi-can-helper", command)
        self.assertIn("pyroute2", command)
        self.assertNotIn("--add-binary", command)
        self.assertNotIn("motors_py", command)
        self.assertEqual(command[-1], str(ROOT / "factory_hmi" / "can_helper.py"))

    def test_gateway_build_embeds_motor_extension_and_station_data(self) -> None:
        motors_py = Path("/tmp/motors_py.test.so")
        with (
            mock.patch.object(builder.importlib.util, "find_spec", return_value=object()),
            mock.patch.object(builder.subprocess, "run") as run,
        ):
            builder.build("gateway", one_dir=False, motors_py=motors_py)

        command = run.call_args.args[0]
        self.assertIn(f"{motors_py}{builder.os.pathsep}.", command)
        self.assertIn("scripts.rp1_limb_common", command)
        self.assertIn("motors_py", command)
        self.assertIn("uvicorn.protocols.http.auto", command)
        self.assertIn("--collect-all", command)
        self.assertIn("pymodbus", command)

    def test_desktop_build_requires_pyside6(self) -> None:
        def find_spec(name: str):
            return object() if name == "PyInstaller" else None

        with (
            mock.patch.object(builder.importlib.util, "find_spec", side_effect=find_spec),
            self.assertRaisesRegex(SystemExit, r"\.\[build,desktop\]"),
        ):
            builder.build(
                "desktop",
                one_dir=False,
                motors_py=Path("/tmp/motors_py.test.so"),
            )

    def test_desktop_build_embeds_visual_assets(self) -> None:
        motors_py = Path("/tmp/motors_py.test.so")
        with (
            mock.patch.object(builder.importlib.util, "find_spec", return_value=object()),
            mock.patch.object(builder.subprocess, "run") as run,
        ):
            builder.build("desktop", one_dir=False, motors_py=motors_py)

        command = run.call_args.args[0]
        self.assertIn(
            (
                f"{builder.ROOT / 'factory_hmi' / 'desktop' / 'assets'}"
                f"{builder.os.pathsep}factory_hmi/desktop/assets"
            ),
            command,
        )

    def test_desktop_exposes_three_page_layout_and_module_dialog(self) -> None:
        source = (
            ROOT / "factory_hmi" / "desktop" / "app.py"
        ).read_text(encoding="utf-8")

        self.assertIn('self.limb = QComboBox()', source)
        self.assertIn('for key, label in LIMB_LABELS:', source)
        self.assertIn('self.configure_button = QPushButton("应用模块")', source)
        self.assertIn("def _build_config_dialog(self) -> QDialog:", source)
        self.assertIn(
            'self.tabs.addTab(configuration_page, "样机配置与连接")',
            source,
        )
        self.assertIn(
            'self._wrap_workspace_page(aging_page, "agingPageScroll")',
            source,
        )
        self.assertIn(
            'self._wrap_workspace_page(manual_page, "manualPageScroll")',
            source,
        )
        self.assertIn("def _build_motor_dialog(self) -> QDialog:", source)
        self.assertIn("self._build_zero_dialog()", source)
        self.assertIn('QPushButton("打开电机监控…")', source)
        self.assertIn('QPushButton("打开电机标零…")', source)
        self.assertIn(
            "self.zeroing_button.clicked.connect(self._show_zero_dialog)",
            source,
        )
        self.assertIn('"操作",', source)
        self.assertIn("ZeroTableModel(self.ZERO_COLUMNS", source)
        self.assertIn("ZeroButtonDelegate(self.zero_table)", source)
        self.assertIn('QPushButton("一键标零全部关节")', source)
        self.assertIn("def _start_zero_for_motor(", source)
        self.assertIn("def _zero_all_motors(", source)
        self.assertIn("self.client.zero_motor(int(motor_index))", source)
        self.assertNotIn("self.zero_motor = QComboBox()", source)
        self.assertIn("本页面不录制轨迹", source)
        self.assertIn('QPushButton("保存 CSV…")', source)
        self.assertIn('QPushButton("准备下一组")', source)
        self.assertIn('QPushButton("进入关节控制并保持")', source)
        self.assertIn("target.valueChanged.connect(", source)
        self.assertIn("self.manual_command_timer.setInterval(40)", source)
        self.assertIn("self._schedule_manual_command()", source)
        self.assertIn('"rp1-factory-hmi.png"', source)
        self.assertIn("self.setWindowIcon(QIcon(str(app_icon)))", source)
        self.assertIn("回默认位并重新预检", source)
        self.assertIn('"软件诊断"', source)
        self.assertIn('QPushButton("清除当前配置")', source)

    def test_desktop_uses_packaged_shared_qss_theme(self) -> None:
        source = (
            ROOT / "factory_hmi" / "desktop" / "app.py"
        ).read_text(encoding="utf-8")
        theme = (
            ROOT / "factory_hmi" / "desktop" / "assets" / "factory_hmi.qss"
        ).read_text(encoding="utf-8")
        setup_source = (ROOT / "setup.py").read_text(encoding="utf-8")
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn("apply_theme(self)", source)
        self.assertIn("PageStack()", source)
        self.assertIn("#12334D", theme)
        self.assertIn("#2375C9", theme)
        self.assertIn("#D83B3B", theme)
        self.assertIn('QPushButton#dangerButton', theme)
        self.assertIn('"desktop/assets/*.qss"', setup_source)
        self.assertIn('"desktop/assets/*.qss"', pyproject)
        self.assertIn("当前生效模块", source)
        self.assertIn("等待扫描电机并接收反馈", source)

    def test_motors_build_uses_portable_compiler_launcher(self) -> None:
        fake_extension = Path("/tmp/motors_py.test.so")
        with (
            mock.patch.object(builder.subprocess, "run") as run,
            mock.patch.object(builder, "find_motors_py", return_value=fake_extension),
        ):
            result = builder.build_motors_py()

        self.assertEqual(result, fake_extension)
        configure = run.call_args_list[0].args[0]
        joined = " ".join(str(item) for item in configure)
        self.assertIn("cxx_portable_launcher.py", joined)
        self.assertIn("CCACHE_PROGRAM-NOTFOUND", joined)


if __name__ == "__main__":
    unittest.main()
