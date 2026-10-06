from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (
    ROOT / "factory_hmi" / "desktop" / "app.py"
).read_text(encoding="utf-8")
CLIENT_SOURCE = (ROOT / "factory_hmi" / "client.py").read_text(encoding="utf-8")


class FactoryHmiUiContractTests(unittest.TestCase):
    def test_operator_controls_keep_their_existing_labels(self) -> None:
        labels = (
            "新增模块",
            "模块参数…",
            "重新应用模块",
            "清除当前配置",
            "1. 连接 CAN",
            "2. 扫描电机",
            "3. 确认使能",
            "断开连接",
            "打开电机监控…",
            "打开电机标零…",
            "选择轨迹…",
            "回默认位",
            "执行预检并进入 Armed",
            "开始运行",
            "暂停",
            "继续",
            "停止",
            "软件失能",
            "保存 CSV…",
            "导出执行包…",
            "提交审批",
            "重试上传",
            "查看审批意见",
            "重建报告",
            "登录测试平台",
            "准备下一组",
            "读取当前位置为目标",
            "进入关节控制并保持",
            "立即停止并保持",
            "导入 YAML…",
            "读取网关路径",
            "刷新 CAN",
            "应用模块配置",
            "Close",
            "一键标零全部关节",
            "标零",
        )
        for label in labels:
            with self.subTest(label=label):
                self.assertIn(label, APP_SOURCE)

    def test_existing_signal_slot_bindings_remain_present(self) -> None:
        bindings = (
            "self.new_window_button.clicked.connect(self._new_module_window)",
            "self.limb.currentTextChanged.connect(self._suggest_config)",
            "self.module_config_button.clicked.connect(self._show_module_config_dialog)",
            "self.configure_button.clicked.connect(self._configure)",
            "self.clear_config_button.clicked.connect(self._clear_configuration)",
            "self.can_connect_button.clicked.connect(self._connect_can)",
            "self.discover_button.clicked.connect(self._discover_motors)",
            "self.enable_motors_button.clicked.connect(self._enable_motors)",
            "self.disconnect_button.clicked.connect(self._disconnect)",
            "self.motor_monitor_button.clicked.connect(self._show_motor_dialog)",
            "self.zeroing_button.clicked.connect(self._show_zero_dialog)",
            "self.import_button.clicked.connect(self._choose_trajectory)",
            "self.reset_button.clicked.connect(self._return_default)",
            "self.preflight_button.clicked.connect(self._confirm_preflight)",
            "self.duration_mode.toggled.connect(self._update_run_limit_mode)",
            "self.record.toggled.connect(self.record_rate_hz.setEnabled)",
            "self.start_button.clicked.connect(self._start_playback)",
            "self.disable_button.clicked.connect(self._software_disable)",
            "self.save_csv_button.clicked.connect(self._save_current_csv)",
            "self.next_run_button.clicked.connect(self._prepare_next_run)",
            "self.manual_current_button.clicked.connect(self._manual_use_current)",
            "self.manual_move_button.clicked.connect(self._manual_move)",
            "self.manual_stop_button.clicked.connect(self._manual_stop)",
            "self.manual_disable_button.clicked.connect(self._software_disable)",
            "self.upload_config_button.clicked.connect(self._choose_config)",
            "self.preview_config_button.clicked.connect(self._preview_config)",
            "self.refresh_can_button.clicked.connect(self._refresh_can_interfaces)",
            "self.apply_editor_button.clicked.connect(self._configure)",
            "self.zero_all_button.clicked.connect(self._zero_all_motors)",
        )
        for binding in bindings:
            with self.subTest(binding=binding):
                self.assertIn(binding, APP_SOURCE)

    def test_control_routes_are_not_renamed(self) -> None:
        routes = (
            "/api/v1/configure",
            "/api/v1/can/connect",
            "/api/v1/motors/discover",
            "/api/v1/motors/enable",
            "/api/v1/trajectory/upload",
            "/api/v1/playback/start",
            "/api/v1/playback/pause",
            "/api/v1/playback/resume",
            "/api/v1/playback/stop",
            "/api/v1/playback/next",
            "/api/v1/playback/reset",
            "/api/v1/playback/disable",
            "/api/v1/manual/move",
            "/api/v1/manual/stop",
            "/api/v1/zero/motor",
            "/api/v1/zero/all",
            "/api/v1/platform/login",
            "/api/v1/executions/submit",
            "/api/v1/executions/retry",
            "/api/v1/executions/rebuild",
        )
        for route in routes:
            with self.subTest(route=route):
                self.assertIn(route, CLIENT_SOURCE)


if __name__ == "__main__":
    unittest.main()
