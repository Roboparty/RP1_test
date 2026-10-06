"""PySide6 operator desktop for the factory ageing gateway."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

try:
    from PySide6.QtCore import (
        QObject,
        QRunnable,
        QThreadPool,
        QTimer,
        Qt,
        QUrl,
        Signal,
        Slot,
    )
    from PySide6.QtGui import QColor, QIcon, QPixmap
    from PySide6.QtWebSockets import QWebSocket
    from PySide6.QtWidgets import (
        QAbstractItemView,
        QAbstractSpinBox,
        QApplication,
        QButtonGroup,
        QCheckBox,
        QComboBox,
        QDialog,
        QDialogButtonBox,
        QDoubleSpinBox,
        QFileDialog,
        QFormLayout,
        QFrame,
        QGridLayout,
        QGroupBox,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QLayout,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QRadioButton,
        QScrollArea,
        QSizePolicy,
        QSlider,
        QSpinBox,
        QStackedWidget,
        QStatusBar,
        QTableView,
        QTableWidget,
        QTableWidgetItem,
        QTextEdit,
        QVBoxLayout,
        QWidget,
    )
except ModuleNotFoundError as exc:
    if exc.name and exc.name.startswith("PySide6"):
        raise ImportError(
            "PySide6 is required for the factory desktop client; "
            "install it with: python -m pip install PySide6",
            name=exc.name,
        ) from exc
    raise

from factory_hmi.client import GatewayClient, GatewayError
from factory_hmi.desktop.table_models import (
    GridTableModel,
    ZeroButtonDelegate,
    ZeroTableModel,
)
from factory_hmi.desktop.theme import apply_theme, set_tone


class WorkerSignals(QObject):
    succeeded = Signal(object)
    failed = Signal(object)
    finished = Signal()


class RestWorker(QRunnable):
    """Execute one synchronous gateway operation away from the UI thread."""

    def __init__(self, operation: Callable[[], Any]) -> None:
        super().__init__()
        self.operation = operation
        self.signals = WorkerSignals()

    @Slot()
    def run(self) -> None:
        try:
            result = self.operation()
        except Exception as exc:  # The UI must display all transport failures.
            try:
                self.signals.failed.emit(exc)
            except RuntimeError:
                # The window may close while a network request completes.
                pass
        else:
            try:
                self.signals.succeeded.emit(result)
            except RuntimeError:
                pass
        finally:
            try:
                self.signals.finished.emit()
            except RuntimeError:
                pass


class PageStack(QStackedWidget):
    """Compatibility wrapper retaining page labels without a duplicate tab bar."""

    def addTab(self, widget: QWidget, _label: str) -> int:
        return self.addWidget(widget)


class FactoryMainWindow(QMainWindow):
    MOTOR_COLUMNS = (
        "关节",
        "ID",
        "CAN",
        "目标(°)",
        "实际(°)",
        "误差(°)",
        "速度(°/s)",
        "力矩(N·m)",
        "温度(°C)",
        "母线电压(V)",
        "母线电流(A)",
        "error_id",
        "软件诊断",
        "反馈延迟(s)",
    )
    ZERO_COLUMNS = (
        "关节",
        "ID",
        "CAN",
        "当前角度(°)",
        "error_id",
        "操作",
    )
    CONFIG_MOTOR_COLUMNS = (
        "序号",
        "关节",
        "电机 ID",
        "电机类型",
        "型号",
        "零偏",
        "Kp",
        "Kd",
        "方向",
    )
    CAN_BINDING_COLUMNS = (
        "总线组",
        "电机数",
        "CAN 接口",
        "模式",
        "仲裁波特率",
        "数据波特率",
        "当前状态",
    )

    def __init__(self, gateway_url: str, *, operator_token: str | None = None) -> None:
        super().__init__()
        self.client = GatewayClient(
            gateway_url,
            operator_token=operator_token,
        )
        self.pool = QThreadPool.globalInstance()
        self._workers: set[RestWorker] = set()
        self._configured = False
        self._trajectory_imported = False
        self._preflight_ok = False
        self._can_connected = False
        self._motors_discovered = False
        self._motors_enabled = False
        self._connected = False
        self._lease_enabled = False
        self._ws_connected = False
        self._snapshot_pending = False
        self._heartbeat_pending = False
        self._preflight_recheck_after_reset = False
        self._configuration_pending = False
        self._active_limb: str | None = None
        self._last_snapshot: dict[str, Any] = {}
        self._last_trajectory: dict[str, Any] = {}
        self._config_preview: dict[str, Any] = {}
        self._available_can: list[dict[str, Any]] = []
        self._child_windows: list[FactoryMainWindow] = []
        self._compact_layout: bool | None = None
        self._manual_mode_active = False
        self._manual_starting = False
        self._manual_command_pending = False
        self._manual_command_queued = False
        self._manual_stop_requested = False
        self._zero_selected_index: int | None = None
        self._zero_selected_label = ""
        self._zero_operation_pending = False
        self._execution_history: list[dict[str, Any]] = []
        self._platform_operator = ""

        app_icon = (
            Path(__file__).resolve().parent
            / "assets"
            / "rp1-factory-hmi.png"
        )
        if app_icon.is_file():
            self.setWindowIcon(QIcon(str(app_icon)))
        self.setWindowTitle("可靠性测试上位机")
        self.resize(1480, 900)
        self.setMinimumSize(760, 620)
        self._build_ui()
        self._apply_style()
        self._apply_responsive_layout(False)
        self._setup_network()

        self._run_rest("检查网关", self.client.health, self._health_received)
        self._refresh_can_interfaces()
        self._refresh_execution_history()

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("appRoot")
        root_layout = QVBoxLayout(root)
        root_layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        header = QWidget()
        header.setObjectName("appHeader")
        header.setFixedHeight(68)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(22, 10, 22, 10)
        header_layout.setSpacing(16)

        logo = QLabel()
        logo.setObjectName("brandLogo")
        logo_path = Path(__file__).resolve().parent / "assets" / "robo_party_logo.png"
        logo_pixmap = QPixmap(str(logo_path))
        if not logo_pixmap.isNull():
            logo.setPixmap(
                logo_pixmap.scaled(
                    122,
                    40,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        logo.setFixedSize(122, 40)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        divider = QFrame()
        divider.setObjectName("headerDivider")
        divider.setFrameShape(QFrame.Shape.VLine)
        divider.setFixedHeight(34)
        title = QLabel("可靠性测试上位机")
        title.setObjectName("pageTitle")
        self.header_module_label = QLabel("当前模块：未配置")
        self.header_module_label.setObjectName("headerContext")
        self.header_robot_label = QLabel("当前样机：未填写")
        self.header_robot_label.setObjectName("headerContext")
        self.gateway_label = QLabel(f"服务：{self.client.base_url}")
        self.gateway_label.setObjectName("headerMuted")
        self.connection_badge = QLabel("● 网关连接中")
        self.connection_badge.setObjectName("statusWarning")
        self.new_window_button = QPushButton("新增模块")
        self.new_window_button.setObjectName("headerActionButton")
        self.new_window_button.setToolTip("新建一个独立控制会话和上位机窗口")
        self.new_window_button.clicked.connect(self._new_module_window)
        header_layout.addWidget(logo)
        header_layout.addWidget(divider)
        header_layout.addWidget(title)
        header_layout.addSpacing(10)
        header_layout.addWidget(self.header_module_label)
        header_layout.addWidget(self.header_robot_label)
        header_layout.addStretch(1)
        header_layout.addWidget(self.new_window_button)
        header_layout.addWidget(self.gateway_label)
        header_layout.addWidget(self.connection_badge)
        root_layout.addWidget(header)

        body = QWidget()
        body.setObjectName("appBody")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(24, 14, 24, 12)
        body_layout.setSpacing(10)

        self.emergency_warning = QLabel(
            "⚠ 软件失能不能替代物理急停！出现人员或设备危险时，立即按下物理急停按钮。"
        )
        self.emergency_warning.setObjectName("emergencyWarning")
        self.emergency_warning.setMinimumHeight(36)
        self.emergency_warning.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body_layout.addWidget(self.emergency_warning)

        self.global_fault = QLabel("●  全局故障：无")
        self.global_fault.setObjectName("faultBarOk")
        self.global_fault.setMinimumHeight(32)
        self.global_fault.setWordWrap(True)
        body_layout.addWidget(self.global_fault)

        self.tabs = PageStack()
        self.tabs.setObjectName("mainTabs")
        self.tabs.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Ignored,
        )
        self.tabs.setMinimumSize(0, 0)
        configuration_page = self._build_operation_page()
        aging_page = self._build_aging_page()
        manual_page = self._build_manual_control_page()
        self._build_config_dialog()
        self._build_motor_dialog()
        self._build_zero_dialog()
        self.tabs.addTab(configuration_page, "样机配置与连接")
        self.tabs.addTab(
            self._wrap_workspace_page(aging_page, "agingPageScroll"),
            "老化测试",
        )
        self.tabs.addTab(
            self._wrap_workspace_page(manual_page, "manualPageScroll"),
            "关节控制",
        )
        body_layout.addWidget(self.tabs, 1)

        shell = QWidget()
        shell.setObjectName("pageWorkspace")
        shell_layout = QHBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)
        shell_layout.addWidget(self._build_side_navigation())
        shell_layout.addWidget(body, 1)
        root_layout.addWidget(shell, 1)
        self.setCentralWidget(root)

        status = QStatusBar()
        status.setObjectName("appStatusBar")
        status.setFixedHeight(24)
        self.setStatusBar(status)
        self.statusBar().showMessage("正在连接网关…")

    @staticmethod
    def _wrap_workspace_page(page: QWidget, object_name: str) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setObjectName(object_name)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setWidget(page)
        return scroll

    def _build_side_navigation(self) -> QFrame:
        navigation = QFrame()
        navigation.setObjectName("sideNavigation")
        navigation.setFixedWidth(216)
        layout = QVBoxLayout(navigation)
        layout.setContentsMargins(12, 18, 12, 14)
        layout.setSpacing(6)

        section = QLabel("功能导航")
        section.setObjectName("navSectionLabel")
        layout.addWidget(section)

        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        self._nav_buttons: list[QPushButton] = []
        entries = (
            ("样机配置与连接", lambda: self._select_main_page(0)),
            ("老化测试", lambda: self._select_main_page(1)),
            ("关节控制", lambda: self._select_main_page(2)),
            ("电机监控", self._show_motor_dialog),
            ("电机标零", self._show_zero_dialog),
            ("模块参数", self._show_module_config_dialog),
        )
        for index, (label, handler) in enumerate(entries):
            if index == 3:
                separator = QFrame()
                separator.setObjectName("navSeparator")
                separator.setFrameShape(QFrame.Shape.HLine)
                layout.addSpacing(8)
                layout.addWidget(separator)
                layout.addSpacing(8)
            button = QPushButton(label)
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.clicked.connect(handler)
            self._nav_group.addButton(button, index)
            self._nav_buttons.append(button)
            layout.addWidget(button)
        self._nav_buttons[0].setChecked(True)
        self.tabs.currentChanged.connect(self._sync_nav_to_tab)
        self.module_config_dialog.finished.connect(self._restore_page_navigation)
        self.motor_dialog.finished.connect(self._restore_page_navigation)
        self.zero_dialog.finished.connect(self._restore_page_navigation)
        layout.addStretch(1)
        return navigation

    def _select_main_page(self, index: int) -> None:
        self.tabs.setCurrentIndex(index)
        self._sync_nav_to_tab(index)

    def _sync_nav_to_tab(self, index: int) -> None:
        if hasattr(self, "_nav_buttons") and 0 <= index < 3:
            self._nav_buttons[index].setChecked(True)

    def _activate_dialog_navigation(self, index: int) -> None:
        if hasattr(self, "_nav_buttons") and 0 <= index < len(self._nav_buttons):
            self._nav_buttons[index].setChecked(True)

    def _restore_page_navigation(self, _result: int = 0) -> None:
        self._sync_nav_to_tab(self.tabs.currentIndex())

    def _build_legacy_operation_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("tabPage")
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(12)

        heading = QLabel("样机配置与连接")
        heading.setObjectName("pageHeading")
        description = QLabel(
            "配置样机与模块参数，按既定顺序完成 CAN 连接、电机扫描和安全使能。"
        )
        description.setObjectName("pageDescription")
        page_layout.addWidget(heading)
        page_layout.addWidget(description)

        scroll = QScrollArea()
        scroll.setObjectName("operationScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        content = QWidget()
        content.setObjectName("operationContent")
        layout = QGridLayout(content)
        layout.setContentsMargins(4, 8, 4, 4)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(12)
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 1)
        layout.setRowStretch(0, 1)
        layout.setRowStretch(1, 1)
        layout.setRowStretch(2, 1)

        expanding = QSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )

        configure_group = QGroupBox("1  选择总成和样机")
        configure_group.setObjectName("workflowCard")
        configure_group.setMinimumHeight(154)
        configure_group.setSizePolicy(expanding)
        configure_form = QFormLayout(configure_group)
        configure_form.setContentsMargins(8, 6, 8, 6)
        configure_form.setHorizontalSpacing(8)
        configure_form.setVerticalSpacing(5)
        configure_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )
        configure_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        configure_form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        config_row = QHBoxLayout()
        self.config_path = QLineEdit()
        self.config_path.setPlaceholderText("选择电机 YAML 配置")
        self.config_path.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        browse_config = QPushButton("导入 YAML…")
        browse_config.setToolTip(
            "选择本机 YAML 后上传至网关，并自动识别电机参数。"
        )
        browse_config.clicked.connect(self._choose_config)
        config_row.addWidget(self.config_path, 1)
        config_row.addWidget(browse_config)
        self.limb = QComboBox()
        self.limb.setEditable(True)
        self.limb.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self.limb.addItems(
            (
                "left_arm",
                "right_arm",
                "left_short_arm",
                "right_short_arm",
                "left_leg",
                "right_leg",
                "left_short_leg",
                "right_short_leg",
                "waist_hip",
                "biped_waist",
                "upper_body",
            )
        )
        self.limb.currentTextChanged.connect(self._suggest_config)
        self.robot_id = QLineEdit()
        self.robot_id.setPlaceholderText("例如 RP1-001（必填）")
        self.robot_id.textChanged.connect(
            lambda value: self.header_robot_label.setText(
                f"当前样机：{value.strip() or '未填写'}"
            )
        )
        self.robot_id.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self.backend = QComboBox()
        self.backend.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self.backend.addItem("真机", "motors_py")
        self.backend.addItem("仿真", "fake")
        self.configure_button = QPushButton("应用配置")
        self.configure_button.setObjectName("primaryButton")
        self.configure_button.setMinimumWidth(118)
        self.configure_button.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Fixed,
        )
        self.configure_button.clicked.connect(self._configure)
        configure_form.addRow("网关配置路径", config_row)
        configure_form.addRow("总成", self.limb)
        configure_form.addRow("样机编号", self.robot_id)
        backend_row = QHBoxLayout()
        backend_row.setSpacing(8)
        backend_row.addWidget(self.backend, 1)
        backend_row.addWidget(self.configure_button)
        configure_form.addRow("后端", backend_row)
        self._suggest_config(self.limb.currentText())

        connect_group = QGroupBox("2  CAN、电机发现与使能")
        connect_group.setObjectName("workflowCard")
        connect_group.setMinimumHeight(116)
        connect_group.setSizePolicy(expanding)
        connect_layout = QVBoxLayout(connect_group)
        connect_layout.setContentsMargins(8, 6, 8, 6)
        connect_layout.setSpacing(6)
        self.can_status_label = QLabel("CAN：未连接 | 电机：未发现 | 使能：否")
        self.can_status_label.setObjectName("infoPanel")
        self.can_status_label.setWordWrap(True)
        self.can_status_label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Minimum,
        )
        button_grid = QGridLayout()
        button_grid.setHorizontalSpacing(8)
        button_grid.setVerticalSpacing(8)
        button_grid.setColumnStretch(0, 1)
        button_grid.setColumnStretch(1, 1)
        button_grid.setColumnStretch(2, 1)
        button_grid.setColumnStretch(3, 1)
        self.can_connect_button = QPushButton("1. 连接 CAN")
        self.can_connect_button.setObjectName("primaryButton")
        self.can_connect_button.setEnabled(False)
        self.can_connect_button.clicked.connect(self._connect_can)
        self.discover_button = QPushButton("2. 连接/扫描电机")
        self.discover_button.setEnabled(False)
        self.discover_button.clicked.connect(self._discover_motors)
        self.enable_motors_button = QPushButton("3. 确认并使能电机")
        self.enable_motors_button.setEnabled(False)
        self.enable_motors_button.clicked.connect(self._enable_motors)
        self.disconnect_button = QPushButton("断开连接")
        self.disconnect_button.setEnabled(False)
        self.disconnect_button.clicked.connect(self._disconnect)
        for button in (
            self.can_connect_button,
            self.discover_button,
            self.enable_motors_button,
            self.disconnect_button,
        ):
            button.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Fixed,
            )
        button_grid.addWidget(self.can_connect_button, 0, 0)
        button_grid.addWidget(self.discover_button, 0, 1)
        button_grid.addWidget(self.enable_motors_button, 0, 2)
        button_grid.addWidget(self.disconnect_button, 0, 3)
        connect_hint = QLabel(
            "使能前必须确认物理急停、安全回路、无人区和机构无干涉。"
        )
        connect_hint.setObjectName("muted")
        connect_hint.setWordWrap(True)
        connect_layout.addWidget(self.can_status_label)
        connect_layout.addLayout(button_grid)
        connect_layout.addWidget(connect_hint)

        import_group = QGroupBox("3  导入轨迹")
        import_group.setObjectName("workflowCard")
        import_group.setMinimumHeight(154)
        import_group.setSizePolicy(expanding)
        import_layout = QVBoxLayout(import_group)
        import_layout.setContentsMargins(8, 6, 8, 6)
        import_layout.setSpacing(6)
        import_row = QHBoxLayout()
        self.trajectory_path = QLineEdit()
        self.trajectory_path.setReadOnly(True)
        self.trajectory_path.setPlaceholderText("先完成配置，再导入 .npz 轨迹")
        self.trajectory_path.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self.import_button = QPushButton("选择轨迹…")
        self.import_button.setEnabled(False)
        self.import_button.clicked.connect(self._choose_trajectory)
        import_row.addWidget(self.trajectory_path, 1)
        import_row.addWidget(self.import_button)
        self.trajectory_info = QLabel("尚未导入轨迹")
        self.trajectory_info.setObjectName("infoPanel")
        self.trajectory_info.setWordWrap(True)
        self.trajectory_info.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self.trajectory_info.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        import_layout.addLayout(import_row)
        import_layout.addWidget(self.trajectory_info, 1)

        preflight_group = QGroupBox("4  预检并进入 Armed")
        preflight_group.setObjectName("workflowCard")
        preflight_group.setMinimumHeight(116)
        preflight_group.setSizePolicy(expanding)
        preflight_layout = QHBoxLayout(preflight_group)
        preflight_layout.setContentsMargins(8, 6, 8, 6)
        preflight_layout.setSpacing(8)
        self.preflight_button = QPushButton("执行预检确认")
        self.preflight_button.setEnabled(False)
        self.preflight_button.setMinimumWidth(142)
        self.preflight_button.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Fixed,
        )
        self.preflight_button.clicked.connect(self._confirm_preflight)
        self.preflight_result = QLabel("等待轨迹导入")
        self.preflight_result.setObjectName("infoPanel")
        self.preflight_result.setWordWrap(True)
        self.preflight_result.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self.preflight_result.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        preflight_layout.addWidget(self.preflight_button)
        preflight_layout.addWidget(self.preflight_result, 1)

        start_group = QGroupBox("5  启动老化")
        start_group.setObjectName("workflowCard")
        start_group.setMinimumHeight(132)
        start_group.setSizePolicy(expanding)
        start_layout = QGridLayout(start_group)
        start_layout.setContentsMargins(8, 6, 8, 6)
        start_layout.setHorizontalSpacing(8)
        start_layout.setVerticalSpacing(4)
        start_layout.setColumnStretch(1, 2)
        start_layout.setColumnStretch(4, 1)
        self.speed = QDoubleSpinBox()
        self.speed.setRange(0.01, 2.0)
        self.speed.setSingleStep(0.05)
        self.speed.setValue(0.2)
        self.speed.setSuffix(" ×")
        self.speed.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self.cycles = QSpinBox()
        self.cycles.setRange(1, 1_000_000)
        self.cycles.setValue(1)
        self.cycles.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self.duration_hours = QDoubleSpinBox()
        self.duration_hours.setRange(0.01, 10_000.0)
        self.duration_hours.setDecimals(2)
        self.duration_hours.setValue(1.0)
        self.duration_hours.setSuffix(" 小时")
        self.duration_hours.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self.duration_mode = QRadioButton("按测试时长")
        self.cycles_mode = QRadioButton("按循环次数")
        self.duration_mode.setChecked(True)
        self.duration_mode.toggled.connect(self._update_run_limit_mode)
        self.record = QCheckBox("记录")
        self.record.setChecked(True)
        self.record_rate_hz = QSpinBox()
        self.record_rate_hz.setRange(1, 20)
        self.record_rate_hz.setValue(20)
        self.record_rate_hz.setSuffix(" Hz")
        self.record_rate_hz.setToolTip(
            "记录频率使用控制线程缓存反馈，最高 20 Hz。"
        )
        self.record.toggled.connect(self.record_rate_hz.setEnabled)
        self.test_id = QLineEdit()
        self.test_id.setPlaceholderText("开始运行后自动生成")
        self.test_id.setReadOnly(True)
        self.test_id.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self.start_button = QPushButton("开始运行")
        self.start_button.setObjectName("startButton")
        self.start_button.setEnabled(False)
        self.start_button.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self.start_button.clicked.connect(self._start_playback)
        start_layout.addWidget(QLabel("速度"), 0, 0)
        start_layout.addWidget(self.speed, 0, 1)
        start_layout.addWidget(self.record, 0, 2, 1, 3)
        start_layout.addWidget(self.duration_mode, 1, 0)
        start_layout.addWidget(self.duration_hours, 1, 1)
        start_layout.addWidget(self.cycles_mode, 1, 2)
        start_layout.addWidget(self.cycles, 1, 3, 1, 2)
        start_layout.addWidget(QLabel("测试单号"), 2, 0)
        start_layout.addWidget(self.test_id, 2, 1, 1, 4)
        start_layout.addWidget(self.start_button, 3, 0, 1, 5)
        self._update_run_limit_mode()

        controls_group = QGroupBox("运行控制")
        controls_group.setObjectName("workflowCard")
        controls_group.setMinimumHeight(132)
        controls_group.setSizePolicy(expanding)
        controls_layout = QGridLayout(controls_group)
        controls_layout.setContentsMargins(8, 6, 8, 6)
        controls_layout.setHorizontalSpacing(8)
        controls_layout.setVerticalSpacing(6)
        controls_layout.setColumnStretch(0, 1)
        controls_layout.setColumnStretch(1, 1)
        controls_layout.setColumnStretch(2, 1)
        controls_layout.setRowStretch(2, 1)
        self.pause_button = QPushButton("暂停")
        self.resume_button = QPushButton("继续")
        self.stop_button = QPushButton("停止")
        self.reset_button = QPushButton("回默认位")
        self.disable_button = QPushButton("软件失能")
        self.disable_button.setObjectName("dangerButton")
        self.pause_button.clicked.connect(
            lambda: self._command("暂停", self.client.playback_pause)
        )
        self.resume_button.clicked.connect(
            lambda: self._command("继续", self.client.playback_resume)
        )
        self.stop_button.clicked.connect(
            lambda: self._command("停止", self.client.playback_stop)
        )
        self.reset_button.clicked.connect(
            lambda: self._command("回默认位", self.client.playback_reset)
        )
        self.disable_button.clicked.connect(self._software_disable)
        for button in (
            self.pause_button,
            self.resume_button,
            self.stop_button,
            self.reset_button,
            self.disable_button,
        ):
            button.setEnabled(False)
            button.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Fixed,
            )
        controls_layout.addWidget(self.pause_button, 0, 0)
        controls_layout.addWidget(self.resume_button, 0, 1)
        controls_layout.addWidget(self.stop_button, 0, 2)
        controls_layout.addWidget(self.reset_button, 1, 0)
        controls_layout.addWidget(self.disable_button, 1, 1, 1, 2)

        self._operation_grid = layout
        self._operation_cards = (
            configure_group,
            import_group,
            preflight_group,
            connect_group,
            start_group,
            controls_group,
        )

        # Match the approved reference: left 1/4/5, right 3/2/controls.
        layout.addWidget(configure_group, 0, 0)
        layout.addWidget(import_group, 0, 1)
        layout.addWidget(preflight_group, 1, 0)
        layout.addWidget(connect_group, 1, 1)
        layout.addWidget(start_group, 2, 0)
        layout.addWidget(controls_group, 2, 1)

        scroll.setWidget(content)
        page_layout.addWidget(scroll, 1)
        return page

    def _build_operation_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("tabPage")
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(12)

        heading = QLabel("样机配置与连接")
        heading.setObjectName("pageHeading")
        description = QLabel(
            "配置样机与模块参数，按既定顺序完成 CAN 连接、电机扫描和安全使能。"
        )
        description.setObjectName("pageDescription")
        page_layout.addWidget(heading)
        page_layout.addWidget(description)

        scroll = QScrollArea()
        scroll.setObjectName("operationScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        content = QWidget()
        content.setObjectName("operationContent")
        layout = QGridLayout(content)
        layout.setContentsMargins(0, 2, 0, 4)
        layout.setHorizontalSpacing(16)
        layout.setVerticalSpacing(16)
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 1)

        steps = QLabel(
            "模块参数与 CAN 绑定  →  应用配置  →  连接 CAN  →  扫描电机  →  确认使能"
        )
        steps.setObjectName("workflowSteps")
        steps.setAlignment(Qt.AlignmentFlag.AlignCenter)
        steps.setWordWrap(True)
        self.workflow_steps = steps
        layout.addWidget(steps, 0, 0, 1, 2)

        module_group = QGroupBox("模块与样机")
        module_group.setObjectName("workflowCard")
        module_layout = QGridLayout(module_group)
        module_layout.setContentsMargins(16, 14, 16, 16)
        module_layout.setHorizontalSpacing(12)
        module_layout.setVerticalSpacing(10)
        module_layout.setColumnStretch(1, 2)
        module_layout.setColumnStretch(3, 2)

        self.limb = QComboBox()
        self.limb.setEditable(True)
        self.limb.addItems(
            (
                "left_arm",
                "right_arm",
                "left_short_arm",
                "right_short_arm",
                "left_leg",
                "right_leg",
                "left_short_leg",
                "right_short_leg",
                "waist_hip",
                "biped_waist",
                "upper_body",
            )
        )
        self.limb.currentTextChanged.connect(self._suggest_config)
        self.robot_id = QLineEdit()
        self.robot_id.setPlaceholderText("例如 RP1-001（必填）")
        self.backend = QComboBox()
        self.robot_id.textChanged.connect(
            lambda value: self.header_robot_label.setText(
                f"当前样机：{value.strip() or '未填写'}"
            )
        )
        self.backend.addItem("真机", "motors_py")
        self.backend.addItem("仿真", "fake")
        self.config_path = QLineEdit()
        self.config_path.setReadOnly(True)
        self.config_path.setPlaceholderText("请打开模块参数并导入 YAML")
        self.module_config_button = QPushButton("模块参数…")
        self.module_config_button.setObjectName("outlineButton")
        self.module_config_button.clicked.connect(self._show_module_config_dialog)
        self.configure_button = QPushButton("应用模块")
        self.configure_button.setObjectName("primaryButton")
        self.configure_button.clicked.connect(self._configure)
        self.config_summary_label = QLabel("尚未载入模块参数")
        self.config_summary_label.setObjectName("summaryPanel")
        self.config_summary_label.setWordWrap(True)
        self.active_config_label = QLabel("当前生效：尚未配置")
        self.active_config_label.setObjectName("infoPanel")
        self.active_config_label.setWordWrap(True)
        self.clear_config_button = QPushButton("清除当前配置")
        self.clear_config_button.setObjectName("criticalOutlineButton")
        self.clear_config_button.setEnabled(False)
        self.clear_config_button.clicked.connect(self._clear_configuration)

        module_layout.addWidget(QLabel("总成"), 0, 0)
        module_layout.addWidget(self.limb, 0, 1)
        module_layout.addWidget(QLabel("样机编号"), 0, 2)
        module_layout.addWidget(self.robot_id, 0, 3)
        module_layout.addWidget(QLabel("后端"), 1, 0)
        module_layout.addWidget(self.backend, 1, 1)
        module_layout.addWidget(QLabel("配置文件"), 1, 2)
        module_layout.addWidget(self.config_path, 1, 3)
        module_layout.addWidget(self.config_summary_label, 2, 0, 1, 2)
        module_layout.addWidget(self.module_config_button, 2, 2)
        module_layout.addWidget(self.configure_button, 2, 3)
        module_layout.addWidget(self.active_config_label, 3, 0, 1, 3)
        module_layout.addWidget(self.clear_config_button, 3, 3)
        self._suggest_config(self.limb.currentText())

        preparation_group = QGroupBox("连接与执行准备")
        preparation_group.setObjectName("workflowCard")
        preparation_layout = QGridLayout(preparation_group)
        preparation_layout.setContentsMargins(16, 14, 16, 16)
        preparation_layout.setHorizontalSpacing(12)
        preparation_layout.setVerticalSpacing(12)
        preparation_layout.setColumnStretch(0, 1)
        preparation_layout.setColumnStretch(1, 1)

        status_panel = QFrame()
        status_panel.setObjectName("phasePanel")
        status_layout = QHBoxLayout(status_panel)
        status_layout.setContentsMargins(10, 8, 10, 8)
        status_layout.setSpacing(8)
        self.can_status_label = QLabel("CAN\n未连接")
        self.motor_discovery_label = QLabel("电机\n未发现")
        self.motor_enable_label = QLabel("使能\n否")
        self.position_hold_label = QLabel("位置保持\n未运行")
        self.feedback_read_label = QLabel("状态读取\n等待连接")
        for label in (
            self.can_status_label,
            self.motor_discovery_label,
            self.motor_enable_label,
            self.position_hold_label,
            self.feedback_read_label,
        ):
            label.setObjectName("infoPanel")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setWordWrap(True)
            status_layout.addWidget(label, 1)
        preparation_layout.addWidget(status_panel, 0, 0, 1, 2)

        connection_panel = QFrame()
        connection_panel.setObjectName("phasePanel")
        connection_layout = QGridLayout(connection_panel)
        connection_layout.setContentsMargins(12, 12, 12, 12)
        connection_layout.setHorizontalSpacing(10)
        connection_layout.setVerticalSpacing(10)
        connection_title = QLabel("连接与使能")
        connection_title.setObjectName("sectionTitle")
        connection_layout.addWidget(connection_title, 0, 0, 1, 4)
        self.can_connect_button = QPushButton("1. 连接 CAN")
        self.can_connect_button.setObjectName("primaryButton")
        self.can_connect_button.setEnabled(False)
        self.can_connect_button.clicked.connect(self._connect_can)
        self.discover_button = QPushButton("2. 扫描电机")
        self.discover_button.setObjectName("outlineButton")
        self.discover_button.setEnabled(False)
        self.discover_button.clicked.connect(self._discover_motors)
        self.enable_motors_button = QPushButton("3. 确认使能")
        self.enable_motors_button.setEnabled(False)
        self.enable_motors_button.clicked.connect(self._enable_motors)
        self.disconnect_button = QPushButton("断开连接")
        self.disconnect_button.setObjectName("outlineButton")
        self.disconnect_button.setEnabled(False)
        self.disconnect_button.clicked.connect(self._disconnect)
        for column, button in enumerate(
            (
                self.can_connect_button,
                self.discover_button,
                self.enable_motors_button,
                self.disconnect_button,
            )
        ):
            connection_layout.setColumnStretch(column, 1)
            connection_layout.addWidget(button, 1, column)
        connection_hint = QLabel(
            "使能前确认急停、安全回路、无人区和机构无干涉。"
        )
        connection_hint.setObjectName("muted")
        connection_hint.setWordWrap(True)
        self.motor_monitor_button = QPushButton("打开电机监控…")
        self.motor_monitor_button.setObjectName("outlineButton")
        self.motor_monitor_button.clicked.connect(self._show_motor_dialog)
        self.zeroing_button = QPushButton("打开电机标零…")
        self.zeroing_button.setObjectName("outlineButton")
        self.zeroing_button.clicked.connect(self._show_zero_dialog)
        connection_layout.addWidget(self.motor_monitor_button, 2, 0, 1, 2)
        connection_layout.addWidget(self.zeroing_button, 2, 2, 1, 2)
        connection_layout.addWidget(connection_hint, 3, 0, 1, 4)

        trajectory_panel = QFrame()
        trajectory_panel.setObjectName("phasePanel")
        trajectory_layout = QVBoxLayout(trajectory_panel)
        trajectory_layout.setContentsMargins(8, 8, 8, 8)
        trajectory_layout.setSpacing(7)
        trajectory_title = QLabel("轨迹、回位与预检")
        trajectory_title.setObjectName("phaseTitle")
        trajectory_layout.addWidget(trajectory_title)
        import_row = QHBoxLayout()
        self.trajectory_path = QLineEdit()
        self.trajectory_path.setReadOnly(True)
        self.trajectory_path.setPlaceholderText("应用模块后选择 .npz 轨迹")
        self.import_button = QPushButton("选择轨迹…")
        self.import_button.setObjectName("outlineButton")
        self.import_button.setEnabled(False)
        self.import_button.clicked.connect(self._choose_trajectory)
        import_row.addWidget(self.trajectory_path, 1)
        import_row.addWidget(self.import_button)
        trajectory_layout.addLayout(import_row)
        self.trajectory_info = QLabel("尚未导入轨迹")
        self.trajectory_info.setObjectName("infoPanel")
        self.trajectory_info.setWordWrap(True)
        trajectory_layout.addWidget(self.trajectory_info)
        preflight_actions = QHBoxLayout()
        self.reset_button = QPushButton("回默认位")
        self.reset_button.setObjectName("outlineButton")
        self.reset_button.setEnabled(False)
        self.reset_button.clicked.connect(self._return_default)
        self.preflight_button = QPushButton("执行预检并进入 Armed")
        self.preflight_button.setObjectName("primaryButton")
        self.preflight_button.setEnabled(False)
        self.preflight_button.clicked.connect(self._confirm_preflight)
        preflight_actions.addWidget(self.reset_button)
        preflight_actions.addWidget(self.preflight_button, 1)
        trajectory_layout.addLayout(preflight_actions)
        self.preflight_result = QLabel("等待轨迹导入")
        self.preflight_result.setObjectName("infoPanel")
        self.preflight_result.setWordWrap(True)
        trajectory_layout.addWidget(self.preflight_result)

        preparation_layout.addWidget(connection_panel, 1, 0, 1, 2)
        self._trajectory_panel = trajectory_panel

        aging_group = QGroupBox("老化运行")
        aging_group.setObjectName("workflowCard")
        aging_layout = QGridLayout(aging_group)
        aging_layout.setContentsMargins(12, 10, 12, 10)
        aging_layout.setHorizontalSpacing(10)
        aging_layout.setVerticalSpacing(8)
        aging_layout.setColumnStretch(1, 2)
        aging_layout.setColumnStretch(3, 2)
        aging_layout.setColumnStretch(4, 2)

        self.speed = QDoubleSpinBox()
        self.speed.setRange(0.01, 2.0)
        self.speed.setSingleStep(0.05)
        self.speed.setValue(0.2)
        self.speed.setSuffix(" ×")
        self.duration_hours = QDoubleSpinBox()
        self.duration_hours.setRange(0.01, 10_000.0)
        self.duration_hours.setDecimals(2)
        self.duration_hours.setValue(1.0)
        self.duration_hours.setSuffix(" 小时")
        self.cycles = QSpinBox()
        self.cycles.setRange(1, 1_000_000)
        self.cycles.setValue(1)
        self.duration_mode = QRadioButton("按测试时长")
        self.cycles_mode = QRadioButton("按循环次数")
        self.duration_mode.setChecked(True)
        self.duration_mode.toggled.connect(self._update_run_limit_mode)
        self.record = QCheckBox("记录")
        self.record.setChecked(True)
        self.record_rate_hz = QSpinBox()
        self.record_rate_hz.setRange(1, 20)
        self.record_rate_hz.setValue(20)
        self.record_rate_hz.setSuffix(" Hz")
        self.record_rate_hz.setToolTip(
            "记录频率使用控制线程缓存反馈，最高 20 Hz。"
        )
        self.record.toggled.connect(self.record_rate_hz.setEnabled)
        self.test_id = QLineEdit()
        self.test_id.setPlaceholderText("开始运行后自动生成")
        self.test_id.setReadOnly(True)
        self.test_id.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self.start_button = QPushButton("开始运行")
        self.start_button.setObjectName("startButton")
        self.start_button.setEnabled(False)
        self.start_button.clicked.connect(self._start_playback)

        aging_layout.addWidget(QLabel("播放速度"), 0, 0)
        aging_layout.addWidget(self.speed, 0, 1)
        record_row = QHBoxLayout()
        record_row.setSpacing(5)
        record_row.addWidget(self.record)
        record_row.addWidget(QLabel("记录频率"))
        record_row.addWidget(self.record_rate_hz)
        aging_layout.addLayout(record_row, 0, 2)
        test_id_row = QHBoxLayout()
        test_id_row.setSpacing(6)
        test_id_label = QLabel("测试单号")
        test_id_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        test_id_row.addWidget(test_id_label)
        test_id_row.addWidget(self.test_id, 1)
        aging_layout.addLayout(test_id_row, 0, 3, 1, 2)
        aging_layout.addWidget(self.duration_mode, 1, 0)
        aging_layout.addWidget(self.duration_hours, 1, 1)
        aging_layout.addWidget(self.cycles_mode, 1, 2)
        aging_layout.addWidget(self.cycles, 1, 3)
        aging_layout.addWidget(self.start_button, 1, 4)

        controls = QHBoxLayout()
        self.pause_button = QPushButton("暂停")
        self.pause_button.setObjectName("outlineButton")
        self.resume_button = QPushButton("继续")
        self.resume_button.setObjectName("outlineButton")
        self.stop_button = QPushButton("停止")
        self.stop_button.setObjectName("criticalOutlineButton")
        self.disable_button = QPushButton("软件失能")
        self.disable_button.setObjectName("dangerButton")
        self.pause_button.clicked.connect(
            lambda: self._command("暂停", self.client.playback_pause)
        )
        self.resume_button.clicked.connect(
            lambda: self._command("继续", self.client.playback_resume)
        )
        self.stop_button.clicked.connect(
            lambda: self._command("停止", self.client.playback_stop)
        )
        self.disable_button.clicked.connect(self._software_disable)
        for button in (
            self.pause_button,
            self.resume_button,
            self.stop_button,
            self.disable_button,
        ):
            button.setEnabled(False)
            controls.addWidget(button, 1)
        aging_layout.addLayout(controls, 2, 0, 1, 5)
        self._update_run_limit_mode()

        self._operation_grid = layout
        self._aging_group = aging_group
        self._operation_cards = (module_group, preparation_group)
        layout.addWidget(module_group, 1, 0, 1, 2)
        layout.addWidget(preparation_group, 2, 0, 1, 2)
        layout.setRowStretch(2, 1)

        scroll.setWidget(content)
        page_layout.addWidget(scroll, 1)
        return page

    def _build_aging_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("tabPage")
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 4)
        page_layout.setSpacing(12)

        heading = QLabel("老化测试")
        heading.setObjectName("pageHeading")
        description = QLabel(
            "完成轨迹导入、回位和安全预检后运行老化测试，并持续查看记录与关键结果。"
        )
        description.setObjectName("pageDescription")
        page_layout.addWidget(heading)
        page_layout.addWidget(description)

        steps = QLabel(
            "导入轨迹  →  低速回默认位  →  安全预检  →  老化运行  →  保存记录 / 下一组"
        )
        steps.setObjectName("workflowSteps")
        steps.setAlignment(Qt.AlignmentFlag.AlignCenter)
        steps.setWordWrap(True)
        page_layout.addWidget(steps)
        setup_row = QHBoxLayout()
        setup_row.setSpacing(16)
        setup_row.addWidget(self._trajectory_panel, 1)
        setup_row.addWidget(self._aging_group, 1)
        page_layout.addLayout(setup_row)

        result_group = QGroupBox("运行记录与关键结果")
        result_group.setObjectName("workflowCard")
        result_layout = QVBoxLayout(result_group)
        result_layout.setContentsMargins(16, 14, 16, 16)
        result_layout.setSpacing(10)
        login_row = QHBoxLayout()
        self.platform_station_id = QLineEdit()
        self.platform_station_id.setPlaceholderText("工位 ID")
        self.platform_username = QLineEdit()
        self.platform_username.setPlaceholderText("操作员账号")
        self.platform_password = QLineEdit()
        self.platform_password.setPlaceholderText("密码（仅保存在内存）")
        self.platform_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.platform_login_button = QPushButton("登录测试平台")
        self.platform_login_button.setObjectName("outlineButton")
        self.platform_login_button.clicked.connect(self._platform_login)
        self.platform_identity_label = QLabel("平台账号：未登录")
        self.platform_identity_label.setObjectName("muted")
        login_row.addWidget(self.platform_station_id, 1)
        login_row.addWidget(self.platform_username, 1)
        login_row.addWidget(self.platform_password, 1)
        login_row.addWidget(self.platform_login_button)
        login_row.addWidget(self.platform_identity_label, 1)
        result_layout.addLayout(login_row)
        status_row = QHBoxLayout()
        self.aging_elapsed_label = QLabel("已运行：00:00:00")
        self.aging_cycle_label = QLabel("循环：0")
        self.aging_record_label = QLabel("记录：未开始")
        for label in (
            self.aging_elapsed_label,
            self.aging_cycle_label,
            self.aging_record_label,
        ):
            label.setObjectName("infoPanel")
            status_row.addWidget(label, 1)
        result_layout.addLayout(status_row)

        aging_headers = (
            "关节",
            "样本",
            "跟踪 RMS(°)",
            "最大误差(°)",
            "峰值力矩(N·m)",
            "当前温度(°C)",
            "温升(°C)",
            "错误样本",
        )
        self.aging_statistics_model = GridTableModel(aging_headers, self)
        self.aging_statistics_table = QTableView()
        self.aging_statistics_table.setObjectName("agingStatisticsTable")
        self.aging_statistics_table.setModel(self.aging_statistics_model)
        self.aging_statistics_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.aging_statistics_table.setAlternatingRowColors(True)
        self.aging_statistics_table.verticalHeader().setVisible(False)
        self.aging_statistics_table.verticalHeader().setDefaultSectionSize(42)
        self.aging_statistics_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        result_layout.addWidget(self.aging_statistics_table, 1)

        self.execution_history_table = QTableWidget(0, 6)
        self.execution_history_table.setHorizontalHeaderLabels(
            ("测试单号", "样机", "判定", "同步状态", "审批意见", "时间")
        )
        self.execution_history_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.execution_history_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.execution_history_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.execution_history_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.execution_history_table.setMaximumHeight(180)
        result_layout.addWidget(self.execution_history_table)

        action_row = QHBoxLayout()
        self.refresh_history_button = QPushButton("刷新执行记录")
        self.refresh_history_button.setObjectName("outlineButton")
        self.refresh_history_button.clicked.connect(self._refresh_execution_history)
        self.save_csv_button = QPushButton("保存 CSV…")
        self.save_csv_button.setObjectName("outlineButton")
        self.save_csv_button.setEnabled(False)
        self.save_csv_button.clicked.connect(self._save_current_csv)
        self.export_bundle_button = QPushButton("导出执行包…")
        self.export_bundle_button.setObjectName("outlineButton")
        self.export_bundle_button.setEnabled(False)
        self.export_bundle_button.clicked.connect(self._export_execution_bundle)
        self.submit_review_button = QPushButton("提交审批")
        self.submit_review_button.setObjectName("primaryButton")
        self.submit_review_button.setEnabled(False)
        self.submit_review_button.clicked.connect(self._submit_execution)
        self.retry_upload_button = QPushButton("重试上传")
        self.retry_upload_button.setObjectName("outlineButton")
        self.retry_upload_button.clicked.connect(self._retry_execution)
        self.review_comment_button = QPushButton("查看审批意见")
        self.review_comment_button.setObjectName("outlineButton")
        self.review_comment_button.clicked.connect(self._show_review_comment)
        self.rebuild_report_button = QPushButton("重建报告")
        self.rebuild_report_button.setObjectName("outlineButton")
        self.rebuild_report_button.clicked.connect(self._rebuild_execution)
        self.next_run_button = QPushButton("准备下一组")
        self.next_run_button.setObjectName("primaryButton")
        self.next_run_button.setEnabled(False)
        self.next_run_button.clicked.connect(self._prepare_next_run)
        for button in (
            self.refresh_history_button,
            self.save_csv_button,
            self.export_bundle_button,
            self.submit_review_button,
            self.retry_upload_button,
            self.review_comment_button,
            self.rebuild_report_button,
            self.next_run_button,
        ):
            action_row.addWidget(button)
        result_layout.addLayout(action_row)
        page_layout.addWidget(result_group, 1)
        return page

    def _build_manual_control_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("tabPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 4)
        layout.setSpacing(12)

        heading = QLabel("关节控制")
        heading.setObjectName("pageHeading")
        description = QLabel(
            "查看当前关节角度，在安全确认后调整目标角度和移动速度。"
        )
        description.setObjectName("pageDescription")
        layout.addWidget(heading)
        layout.addWidget(description)

        warning = QLabel(
            "关节控制仅用于已连接并使能的样机。发送前确认无人进入运动范围、"
            "目标角度无干涉。进入控制后，可直接点击或长按目标角度右侧的上下按钮，"
            "也可输入角度、拖动速度滑块；修改会自动发送，本页面不录制轨迹。"
        )
        warning.setObjectName("zeroWarning")
        warning.setWordWrap(True)
        layout.addWidget(warning)

        card = QFrame()
        card.setObjectName("contentCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 16, 16, 16)
        card_layout.setSpacing(12)
        self.manual_status_label = QLabel("等待样机配置、连接并使能")
        self.manual_status_label.setObjectName("infoPanel")
        card_layout.addWidget(self.manual_status_label)

        self.manual_joint_table = QTableWidget(0, 4)
        self.manual_joint_table.setHorizontalHeaderLabels(
            ("关节", "电机 ID", "当前角度(°)", "目标角度(°)")
        )
        self.manual_joint_table.setAlternatingRowColors(True)
        self.manual_joint_table.verticalHeader().setVisible(False)
        self.manual_joint_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.manual_joint_table.horizontalHeader().setSectionResizeMode(
            0,
            QHeaderView.ResizeMode.Stretch,
        )
        self.manual_joint_table.horizontalHeader().setSectionResizeMode(
            1,
            QHeaderView.ResizeMode.ResizeToContents,
        )
        self.manual_joint_table.verticalHeader().setDefaultSectionSize(42)
        self.manual_joint_table.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        card_layout.addWidget(self.manual_joint_table, 1)

        speed_row = QHBoxLayout()
        speed_row.addWidget(QLabel("移动速度"))
        self.manual_speed_slider = QSlider(Qt.Orientation.Horizontal)
        self.manual_speed_slider.setRange(1, 1800)
        self.manual_speed_slider.setValue(100)
        self.manual_speed_slider.setEnabled(False)
        self.manual_speed_spin = QDoubleSpinBox()
        self.manual_speed_spin.setRange(0.1, 180.0)
        self.manual_speed_spin.setSingleStep(0.5)
        self.manual_speed_spin.setValue(10.0)
        self.manual_speed_spin.setSuffix(" °/s")
        self.manual_speed_spin.setEnabled(False)
        self.manual_speed_slider.valueChanged.connect(
            self._manual_speed_from_slider
        )
        self.manual_speed_spin.valueChanged.connect(self._manual_speed_from_spin)
        speed_row.addWidget(self.manual_speed_slider, 1)
        speed_row.addWidget(self.manual_speed_spin)
        card_layout.addLayout(speed_row)

        actions = QHBoxLayout()
        self.manual_current_button = QPushButton("读取当前位置为目标")
        self.manual_current_button.setObjectName("outlineButton")
        self.manual_current_button.clicked.connect(self._manual_use_current)
        self.manual_move_button = QPushButton("进入关节控制并保持")
        self.manual_move_button.setObjectName("primaryButton")
        self.manual_move_button.setEnabled(False)
        self.manual_move_button.clicked.connect(self._manual_move)
        self.manual_stop_button = QPushButton("立即停止并保持")
        self.manual_stop_button.setObjectName("criticalOutlineButton")
        self.manual_stop_button.setEnabled(False)
        self.manual_stop_button.clicked.connect(self._manual_stop)
        self.manual_disable_button = QPushButton("软件失能")
        self.manual_disable_button.setObjectName("dangerButton")
        self.manual_disable_button.clicked.connect(self._software_disable)
        for button in (
            self.manual_current_button,
            self.manual_move_button,
            self.manual_stop_button,
            self.manual_disable_button,
        ):
            actions.addWidget(button, 1)
        card_layout.addLayout(actions)
        self.manual_command_timer = QTimer(self)
        self.manual_command_timer.setSingleShot(True)
        self.manual_command_timer.setInterval(40)
        self.manual_command_timer.timeout.connect(self._dispatch_manual_command)
        layout.addWidget(card, 1)
        return page

    def _build_config_dialog(self) -> QDialog:
        dialog = QDialog(self)
        dialog.setObjectName("moduleConfigDialog")
        dialog.setWindowTitle("模块参数")
        dialog.resize(1180, 760)
        dialog.setMinimumSize(820, 560)
        self.module_config_dialog = dialog
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(18, 18, 18, 16)
        layout.setSpacing(14)

        toolbar = QFrame()
        toolbar.setObjectName("contentCard")
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(12, 10, 12, 10)
        toolbar_layout.setSpacing(8)
        config_note = QLabel(
            "YAML 的 CAN 口名不直接采用；仅保留总线共享关系，"
            "接口、模式和波特率必须在本页确认。"
        )
        config_note.setObjectName("muted")
        self.upload_config_button = QPushButton("导入 YAML…")
        self.upload_config_button.clicked.connect(self._choose_config)
        self.preview_config_button = QPushButton("读取网关路径")
        self.preview_config_button.setObjectName("outlineButton")
        self.preview_config_button.clicked.connect(self._preview_config)
        self.refresh_can_button = QPushButton("刷新 CAN")
        self.refresh_can_button.setObjectName("outlineButton")
        self.refresh_can_button.clicked.connect(self._refresh_can_interfaces)
        toolbar_layout.addWidget(config_note, 1)
        toolbar_layout.addWidget(self.upload_config_button)
        toolbar_layout.addWidget(self.preview_config_button)
        toolbar_layout.addWidget(self.refresh_can_button)
        layout.addWidget(toolbar)

        motor_group = QGroupBox("YAML 电机参数（可编辑）")
        motor_group.setObjectName("workflowCard")
        motor_layout = QVBoxLayout(motor_group)
        motor_layout.setContentsMargins(12, 12, 12, 12)
        self.config_motor_table = QTableWidget(0, len(self.CONFIG_MOTOR_COLUMNS))
        self.config_motor_table.setHorizontalHeaderLabels(self.CONFIG_MOTOR_COLUMNS)
        self.config_motor_table.setAlternatingRowColors(True)
        self.config_motor_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.config_motor_table.verticalHeader().setVisible(False)
        self.config_motor_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        self.config_motor_table.horizontalHeader().setSectionResizeMode(
            1,
            QHeaderView.ResizeMode.Stretch,
        )
        self.config_motor_table.horizontalHeader().setSectionResizeMode(
            0,
            QHeaderView.ResizeMode.ResizeToContents,
        )
        self.config_motor_table.verticalHeader().setDefaultSectionSize(42)
        for column, width in enumerate((56, 180, 72, 110, 100, 84, 84, 84, 72)):
            if column not in {0, 1}:
                self.config_motor_table.setColumnWidth(column, width)
        motor_layout.addWidget(self.config_motor_table)
        layout.addWidget(motor_group, 2)

        can_group = QGroupBox("CAN 总线绑定（默认从 can0 起，实际接口须确认）")
        can_group.setObjectName("workflowCard")
        can_layout = QVBoxLayout(can_group)
        can_layout.setContentsMargins(12, 12, 12, 12)
        self.can_binding_table = QTableWidget(0, len(self.CAN_BINDING_COLUMNS))
        self.can_binding_table.setHorizontalHeaderLabels(self.CAN_BINDING_COLUMNS)
        self.can_binding_table.setAlternatingRowColors(True)
        self.can_binding_table.verticalHeader().setVisible(False)
        self.can_binding_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        self.can_binding_table.horizontalHeader().setSectionResizeMode(
            0,
            QHeaderView.ResizeMode.Stretch,
        )
        self.can_binding_table.horizontalHeader().setSectionResizeMode(
            len(self.CAN_BINDING_COLUMNS) - 1,
            QHeaderView.ResizeMode.Stretch,
        )
        self.can_binding_table.verticalHeader().setDefaultSectionSize(42)
        for column, width in enumerate((180, 72, 140, 100, 130, 140, 220)):
            if column not in {0, len(self.CAN_BINDING_COLUMNS) - 1}:
                self.can_binding_table.setColumnWidth(column, width)
        can_layout.addWidget(self.can_binding_table)
        layout.addWidget(can_group, 1)

        action_row = QHBoxLayout()
        self.config_validation_label = QLabel("请先导入或读取 YAML 配置")
        self.config_validation_label.setObjectName("infoPanel")
        self.apply_editor_button = QPushButton("应用模块配置")
        self.apply_editor_button.setObjectName("primaryButton")
        self.apply_editor_button.clicked.connect(self._configure)
        action_row.addWidget(self.config_validation_label, 1)
        action_row.addWidget(self.apply_editor_button)
        layout.addLayout(action_row)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        return dialog

    def _build_motor_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("tabPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)
        heading = QLabel("电机监控")
        heading.setObjectName("pageHeading")
        layout.addWidget(heading)
        card = QFrame()
        card.setObjectName("contentCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 16, 16, 16)
        card_layout.setSpacing(12)
        note = QLabel(
            "error_id 仅表示电机驱动器硬件报码；“软件诊断”显示通信超时等"
            "上位机安全诊断，并保留触发时记录。角度单位为度；"
            "“—”表示尚未收到有效反馈。标零请使用独立的“电机标零”功能。"
        )
        note.setObjectName("muted")
        note.setWordWrap(True)
        self.motor_table_model = GridTableModel(self.MOTOR_COLUMNS, self)
        self.motor_table = QTableView()
        self.motor_table.setObjectName("motorTable")
        self.motor_table.setModel(self.motor_table_model)
        self.motor_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.motor_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.motor_table.setAlternatingRowColors(True)
        self.motor_table.setSortingEnabled(False)
        self.motor_table.horizontalHeader().setStretchLastSection(False)
        self.motor_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        self.motor_table.horizontalHeader().setSectionResizeMode(
            0,
            QHeaderView.ResizeMode.Stretch,
        )
        self.motor_table.verticalHeader().setVisible(False)
        self.motor_table.verticalHeader().setDefaultSectionSize(42)
        self.motor_table.horizontalHeader().setMinimumSectionSize(42)
        self.motor_table.horizontalHeader().setDefaultSectionSize(68)
        for column, width in enumerate(
            (130, 48, 58, 70, 70, 70, 78, 82, 72, 88, 88, 72, 200, 90)
        ):
            if column:
                self.motor_table.setColumnWidth(column, width)
        self.motor_table.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        card_layout.addWidget(note)
        card_layout.addWidget(self.motor_table, 1)
        layout.addWidget(card, 1)
        return page

    def _build_zero_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("tabPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)
        heading = QLabel("电机标零")
        heading.setObjectName("pageHeading")
        layout.addWidget(heading)
        card = QFrame()
        card.setObjectName("contentCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 16, 16, 16)
        card_layout.setSpacing(12)
        warning = QLabel(
            "标零会永久改变电机位置基准。执行前请可靠支撑机构，并确认目标关节已经位于"
            "机械零位。单关节标零和一键标零都会先停止位置保持并失能全部电机。"
        )
        warning.setObjectName("zeroWarning")
        warning.setWordWrap(True)
        card_layout.addWidget(warning)
        toolbar = QHBoxLayout()
        batch_label = QLabel("批量危险操作")
        batch_label.setObjectName("sectionTitle")
        toolbar.addWidget(batch_label)
        toolbar.addStretch(1)
        self.zero_all_button = QPushButton("一键标零全部关节")
        self.zero_all_button.setObjectName("criticalOutlineButton")
        self.zero_all_button.setEnabled(False)
        self.zero_all_button.clicked.connect(self._zero_all_motors)
        toolbar.addWidget(self.zero_all_button)
        card_layout.addLayout(toolbar)

        self.zero_table_model = ZeroTableModel(self.ZERO_COLUMNS, self)
        self.zero_table = QTableView()
        self.zero_table.setObjectName("zeroTable")
        self.zero_table.setModel(self.zero_table_model)
        self.zero_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.zero_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.zero_table.setAlternatingRowColors(True)
        self.zero_table.setSortingEnabled(False)
        self.zero_table.verticalHeader().setVisible(False)
        self.zero_table.verticalHeader().setDefaultSectionSize(42)
        self.zero_button_delegate = ZeroButtonDelegate(self.zero_table)
        self.zero_button_delegate.activated.connect(self._zero_row_activated)
        self.zero_table.setItemDelegateForColumn(
            len(self.ZERO_COLUMNS) - 1,
            self.zero_button_delegate,
        )
        self.zero_table.horizontalHeader().setSectionResizeMode(
            0,
            QHeaderView.ResizeMode.Stretch,
        )
        for column, width in enumerate((250, 80, 100, 140, 100, 120)):
            if column:
                self.zero_table.setColumnWidth(column, width)
        card_layout.addWidget(self.zero_table, 1)
        layout.addWidget(card, 1)
        return page

    def _build_motor_dialog(self) -> QDialog:
        dialog = QDialog(self)
        dialog.setWindowTitle("电机监控")
        dialog.resize(1320, 680)
        dialog.setMinimumSize(760, 480)
        dialog.setModal(False)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self._build_motor_page())
        self.motor_dialog = dialog
        return dialog

    def _build_zero_dialog(self) -> QDialog:
        dialog = QDialog(self)
        dialog.setWindowTitle("电机标零")
        dialog.resize(980, 600)
        dialog.setMinimumSize(760, 480)
        dialog.setModal(False)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self._build_zero_page())
        self.zero_dialog = dialog
        return dialog

    def _show_motor_dialog(self) -> None:
        self._activate_dialog_navigation(3)
        self.motor_dialog.show()
        self.motor_dialog.raise_()
        self.motor_dialog.activateWindow()

    def _show_zero_dialog(self) -> None:
        self._activate_dialog_navigation(4)
        self.zero_dialog.show()
        self.zero_dialog.raise_()
        self.zero_dialog.activateWindow()

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_operation_grid"):
            self._apply_responsive_layout(event.size().width() < 1024)

    def _apply_responsive_layout(self, compact: bool) -> None:
        if self._compact_layout is compact:
            return
        self._compact_layout = compact
        self.gateway_label.setVisible(not compact)

        while self._operation_grid.count():
            self._operation_grid.takeAt(0)
        for column in range(2):
            self._operation_grid.setColumnStretch(column, 0)
        for row in range(6):
            self._operation_grid.setRowStretch(row, 0)

        if compact:
            self._operation_grid.setColumnStretch(0, 1)
            self._operation_grid.addWidget(self.workflow_steps, 0, 0)
            for row, card in enumerate(self._operation_cards, start=1):
                self._operation_grid.addWidget(card, row, 0)
        else:
            self._operation_grid.setColumnStretch(0, 1)
            self._operation_grid.setColumnStretch(1, 1)
            self._operation_grid.addWidget(self.workflow_steps, 0, 0, 1, 2)
            for row, card in enumerate(self._operation_cards, start=1):
                self._operation_grid.addWidget(card, row, 0, 1, 2)
            self._operation_grid.setRowStretch(2, 1)

    def _apply_style(self) -> None:
        apply_theme(self)
        return
        self.setStyleSheet(
            """
            * {
                font-family: "Noto Sans CJK SC", "Microsoft YaHei UI",
                             "Microsoft YaHei", sans-serif;
            }
            QMainWindow { background: #f3f5f8; }
            QWidget#appRoot, QWidget#appBody, QWidget#tabPage {
                background: #f3f5f8; color: #263442; font-size: 13px;
            }
            QWidget#appHeader {
                background: #183955; border-bottom: 1px solid #102b42;
            }
            QWidget#appHeader QLabel {
                background: transparent; color: #f4f8fb;
            }
            QLabel#brandLogo { background: transparent; }
            QFrame#headerDivider {
                color: #8fa5b8; background: #8fa5b8; border: none;
                min-width: 1px; max-width: 1px;
            }
            QLabel#pageTitle { font-size: 19px; font-weight: 700; color: #ffffff; }
            QLabel#headerMuted { color: #d0dce7; font-size: 12px; }
            QLabel#muted { color: #5c6874; font-size: 12px; }
            QLabel#fieldLabel { color: #334155; font-weight: 600; }
            QLabel#statusWarning {
                color: #f2f5f7; font-weight: 700; padding: 3px 6px;
                background: transparent;
            }
            QLabel#statusOk {
                color: #8ddd75; font-weight: 700; padding: 3px 6px;
                background: transparent;
            }
            QLabel#statusError {
                color: #ffb4a9; font-weight: 700; padding: 3px 6px;
                background: transparent;
            }
            QPushButton#headerActionButton {
                background: #f7fbff; color: #173a58; border: 1px solid #c7d7e5;
                min-height: 20px; max-height: 22px; padding: 4px 14px;
            }
            QPushButton#headerActionButton:hover { background: #e5f1fb; }
            QLabel#emergencyWarning {
                background: #fff5f4; border: 1px solid #f1c4c0; border-radius: 5px;
                color: #bd2c2c; font-size: 13px; font-weight: 700; padding: 8px 12px;
            }
            QLabel#faultBarOk {
                background: #f5faf5; border: 1px solid #d6e5d7; border-radius: 5px;
                border-left: 5px solid #3a9250; color: #326a3d; padding: 7px 12px;
            }
            QLabel#faultBarError {
                background: #fff5f4; border: 1px solid #f1c4c0; border-radius: 5px;
                border-left: 5px solid #d23b36; color: #9f2824; font-weight: 700;
                padding: 7px 12px;
            }
            QLabel#zeroWarning {
                background: #fff5f4; border: 1px solid #f1c4c0; border-radius: 5px;
                color: #ad332d; font-weight: 600; padding: 10px 14px;
            }
            QLabel#infoPanel {
                background: #fbfcfd; border: 1px solid #d7dfe7; border-radius: 4px;
                padding: 7px 10px; color: #3e4b58;
            }
            QLabel#summaryPanel {
                background: #eef5fb; border: 1px solid #cbdce9; border-radius: 4px;
                padding: 7px 10px; color: #244966; font-weight: 600;
            }
            QLabel#workflowSteps {
                background: #eaf1f7; border: 1px solid #cad8e4; border-radius: 6px;
                color: #234e70; font-weight: 700; padding: 8px 14px;
            }
            QFrame#contentCard {
                background: #ffffff; border: 1px solid #d7dfe7; border-radius: 5px;
            }
            QFrame#phasePanel {
                background: #f8fafc; border: 1px solid #dbe3ea; border-radius: 5px;
            }
            QLabel#phaseTitle { color: #173a58; font-size: 13px; font-weight: 700; }
            QDialog#moduleConfigDialog { background: #f3f5f8; color: #263442; }
            QGroupBox#workflowCard {
                background: #ffffff; border: 1px solid #d7dfe7; border-radius: 6px;
                margin-top: 10px; padding: 6px 8px 6px 8px;
                font-size: 14px; font-weight: 700; color: #173a58;
            }
            QGroupBox#workflowCard::title {
                subcontrol-origin: margin; left: 10px; padding: 0 5px;
                color: #173a58; background: #f3f5f8;
            }
            QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
                background: #ffffff; border: 1px solid #c2cbd4; border-radius: 4px;
                padding: 3px 6px; min-height: 14px; max-height: 18px;
                selection-background-color: #2d72b8;
            }
            QTextEdit {
                background: #ffffff; border: 1px solid #c2cbd4; border-radius: 4px;
                padding: 6px; selection-background-color: #2d72b8;
            }
            QTextEdit#zeroLog {
                background: #fbfcfd; border: 1px solid #d7dfe7; border-radius: 4px;
                padding: 10px; font-family: "Consolas", "Courier New", monospace;
            }
            QPushButton {
                background: #2e73b9; border: 1px solid #2867a6; border-radius: 4px;
                padding: 4px 10px; min-height: 16px; max-height: 18px;
                font-weight: 600; color: #ffffff;
            }
            QPushButton:hover { background: #397fc4; }
            QPushButton:pressed { background: #255f99; }
            QPushButton:disabled {
                color: #95a0ac; background: #f0f3f6; border-color: #d6dde5;
            }
            QPushButton#primaryButton {
                background: #2874b9; color: white; border-color: #2367a5;
            }
            QPushButton#primaryButton:hover { background: #3381c7; }
            QPushButton#primaryButton:pressed { background: #225f99; }
            QPushButton#primaryButton:disabled {
                background: #a8c2d9; color: #eef5fb; border-color: #a8c2d9;
            }
            QPushButton#startButton {
                background: #2b8a45; color: white; border-color: #24773a;
                font-size: 14px; min-height: 18px; max-height: 22px; padding: 4px 8px;
            }
            QPushButton#startButton:hover { background: #339c50; }
            QPushButton#startButton:pressed { background: #24773a; }
            QPushButton#startButton:disabled {
                background: #9fbfab; color: #eef7f1; border-color: #9fbfab;
            }
            QPushButton#dangerButton {
                background: #c62828; color: white; border-color: #9f1f1f;
            }
            QPushButton#dangerButton:hover { background: #d32f2f; }
            QPushButton#dangerButton:pressed { background: #9f1f1f; }
            QPushButton#outlineButton {
                background: #ffffff; color: #334155; border: 1px solid #7e96aa;
            }
            QPushButton#outlineButton:hover { background: #edf4fa; color: #173a58; }
            QPushButton#criticalOutlineButton {
                background: #ffffff; color: #b22d2d; border: 1px solid #d05a55;
            }
            QPushButton#criticalOutlineButton:hover { background: #fff1f0; }
            QTabWidget#mainTabs::pane {
                border: 0; background: transparent; top: -1px;
            }
            QTabWidget#mainTabs QTabBar::tab {
                background: #f6f8fa; color: #3d4c5c; border: 1px solid #d5dde5;
                border-bottom: none; border-top-left-radius: 5px;
                border-top-right-radius: 5px; padding: 8px 24px;
                min-width: 92px; margin-right: 4px; font-weight: 600;
            }
            QTabWidget#mainTabs QTabBar::tab:selected {
                background: #245d91; color: #ffffff; border-color: #245d91;
            }
            QTabWidget#mainTabs QTabBar::tab:hover:!selected {
                background: #d7e2ec;
            }
            QTableWidget, QTableWidget#motorTable {
                background: #ffffff; alternate-background-color: #fafcfd;
                gridline-color: #dde4ea; border: 1px solid #d5dde5;
                border-radius: 4px; selection-background-color: #dbeafe;
                selection-color: #0f172a;
            }
            QHeaderView::section {
                background: #f2f5f8; color: #2f4050; padding: 7px;
                border: none; border-right: 1px solid #d8e0e7;
                border-bottom: 1px solid #d8e0e7; font-weight: 600;
            }
            QStatusBar#appStatusBar {
                background: #f8fafc; color: #475569; border-top: 1px solid #d5dee8;
            }
            QScrollArea#operationScroll, QScrollArea#operationScroll > QWidget > QWidget {
                background: transparent; border: none;
            }
            QWidget#operationContent { background: transparent; }
            """
        )

    def _setup_network(self) -> None:
        self.websocket = QWebSocket()
        self.websocket.connected.connect(self._ws_connected_slot)
        self.websocket.disconnected.connect(self._ws_disconnected_slot)
        self.websocket.textMessageReceived.connect(self._ws_message)
        error_signal = getattr(self.websocket, "errorOccurred", None)
        if error_signal is not None:
            error_signal.connect(self._ws_error)

        self.reconnect_timer = QTimer(self)
        self.reconnect_timer.setSingleShot(True)
        self.reconnect_timer.setInterval(3000)
        self.reconnect_timer.timeout.connect(self._open_websocket)

        self.snapshot_timer = QTimer(self)
        self.snapshot_timer.setInterval(5000)
        self.snapshot_timer.timeout.connect(self._poll_snapshot)
        self.snapshot_timer.start()

        self.lease_timer = QTimer(self)
        self.lease_timer.setInterval(500)
        self.lease_timer.timeout.connect(self._send_lease_heartbeat)
        self.lease_timer.start()

        self._open_websocket()

    def _open_websocket(self) -> None:
        if not self._ws_connected:
            self.websocket.open(QUrl(self.client.status_ws_url))

    @Slot()
    def _ws_connected_slot(self) -> None:
        self._ws_connected = True
        self.connection_badge.setText("● 实时状态已连接")
        self.connection_badge.setObjectName("statusOk")
        self.connection_badge.style().unpolish(self.connection_badge)
        self.connection_badge.style().polish(self.connection_badge)
        self.statusBar().showMessage("WebSocket 实时状态已连接", 3000)

    @Slot()
    def _ws_disconnected_slot(self) -> None:
        self._ws_connected = False
        self.connection_badge.setText("● 实时连接断开（轮询降级）")
        self.connection_badge.setObjectName("statusWarning")
        self.connection_badge.style().unpolish(self.connection_badge)
        self.connection_badge.style().polish(self.connection_badge)
        if not self.reconnect_timer.isActive():
            self.reconnect_timer.start()
        self._poll_snapshot()

    @Slot(object)
    def _ws_error(self, _error: Any) -> None:
        self._ws_connected = False
        if not self.reconnect_timer.isActive():
            self.reconnect_timer.start()

    @Slot(str)
    def _ws_message(self, text: str) -> None:
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            self.statusBar().showMessage("收到无法解析的 WebSocket 消息", 5000)
            return
        if not isinstance(message, Mapping):
            return

        kind = str(message.get("type", "")).lower()
        if kind == "snapshot":
            payload = message.get(
                "snapshot",
                message.get("data", message.get("payload", message)),
            )
            if isinstance(payload, Mapping):
                self._render_snapshot(dict(payload))
        elif kind == "event":
            payload = message.get("snapshot", message.get("payload"))
            if isinstance(payload, Mapping):
                self._render_snapshot(dict(payload))
            event_text = message.get("message") or message.get("event") or "状态事件"
            self.statusBar().showMessage(str(event_text), 5000)
        else:
            self._render_snapshot(dict(message))

    def _poll_snapshot(self) -> None:
        if self._ws_connected or self._snapshot_pending:
            return
        self._snapshot_pending = True
        self._run_rest(
            "刷新状态",
            self.client.snapshot,
            self._snapshot_received,
            quiet=True,
            on_finished=lambda: setattr(self, "_snapshot_pending", False),
        )

    def _send_lease_heartbeat(self) -> None:
        if not self._lease_enabled or self._heartbeat_pending:
            return
        self._heartbeat_pending = True
        self._run_rest(
            "租约心跳",
            self.client.lease_heartbeat,
            quiet=True,
            on_finished=lambda: setattr(self, "_heartbeat_pending", False),
        )

    def _run_rest(
        self,
        label: str,
        operation: Callable[[], Any],
        on_success: Callable[[Any], None] | None = None,
        *,
        quiet: bool = False,
        on_finished: Callable[[], None] | None = None,
    ) -> None:
        worker = RestWorker(operation)
        self._workers.add(worker)

        if on_success is not None:
            worker.signals.succeeded.connect(on_success)
        if not quiet:
            worker.signals.succeeded.connect(
                lambda _result, action=label: self.statusBar().showMessage(
                    f"{action}成功", 4000
                )
            )
        worker.signals.failed.connect(
            lambda exc, action=label, silent=quiet: self._rest_failed(
                action, exc, silent
            )
        )
        worker.signals.finished.connect(
            lambda current=worker: self._workers.discard(current)
        )
        if on_finished is not None:
            worker.signals.finished.connect(on_finished)
        self.pool.start(worker)

    def _rest_failed(self, action: str, exc: Exception, quiet: bool) -> None:
        detail = str(exc.detail or "") if isinstance(exc, GatewayError) else ""
        if action == "进入 Armed" and "first-frame delta too large" in detail:
            self._handle_first_frame_preflight_failure(detail)
            self._run_rest(
                "同步状态",
                self.client.snapshot,
                self._snapshot_received,
                quiet=True,
            )
            return
        if isinstance(exc, GatewayError) and exc.status_code is None:
            message = (
                f"{action}失败：本机控制服务不可用。"
                "请联系维护员检查 rp1-factory-gateway.service。"
            )
        elif action == "导入轨迹" and isinstance(exc, GatewayError):
            detail = str(exc.detail or "")
            prefix = "trajectory limb is "
            separator = ", expected "
            if detail.startswith(prefix) and separator in detail:
                actual, expected = detail[len(prefix) :].split(separator, 1)
                labels = {
                    "left_arm": "左臂",
                    "right_arm": "右臂",
                    "left_short_arm": "左短臂",
                    "right_short_arm": "右短臂",
                    "left_leg": "左腿",
                    "right_leg": "右腿",
                    "left_short_leg": "左短腿",
                    "right_short_leg": "右短腿",
                    "waist_hip": "腰髋模块",
                    "biped_waist": "双腿腰部",
                    "upper_body": "上半身",
                }
                message = (
                    f"轨迹部位不匹配：文件是{labels.get(actual, actual)}，"
                    f"当前模块是{labels.get(expected, expected)}。"
                    f"请在“模块配置”中选择{labels.get(actual, actual)}、重新应用配置，"
                    "然后再导入轨迹。"
                )
            else:
                message = f"{action}失败：{exc}"
        elif "failed to enable with error_id=7" in detail:
            motor_id = "未知"
            if " id=" in detail:
                motor_id = detail.split(" id=", 1)[1].split()[0]
            message = (
                f"{action}失败：电机 ID {motor_id} 报告驱动器故障"
                "（DRV_ERROR，error_id=7）。"
                "系统已撤销全部电机使能。请检查该电机驱动器、电源和线束，"
                "必要时断电复位；排除故障后重新扫描，禁止强制忽略。"
            )
        elif "disconnect the current station before configuring" in detail:
            message = (
                f"{action}失败：当前电机连接尚未断开。"
                "请先点击“断开连接”，确认 CAN 和电机均断开后再应用配置。"
            )
        elif "operation requires state [disconnected], current state is connected" in detail:
            message = (
                f"{action}失败：电机已经连接，不能重复扫描。"
                "如需重新扫描，请先点击“断开连接”。"
            )
        else:
            message = f"{action}失败：{exc}"
        self.statusBar().showMessage(message, 8000)
        if not quiet:
            QMessageBox.critical(self, f"{action}失败", message)
        if action not in {"同步状态", "刷新状态"}:
            self._run_rest(
                "同步状态",
                self.client.snapshot,
                self._snapshot_received,
                quiet=True,
            )

    def _handle_first_frame_preflight_failure(self, detail: str) -> None:
        self._preflight_ok = False
        match = re.search(
            r"motor\[(\d+)\]\s+first-frame delta too large:\s*"
            r"([0-9.eE+-]+)\s*>\s*([0-9.eE+-]+)\s*rad",
            detail,
        )
        index = int(match.group(1)) if match else -1
        delta = float(match.group(2)) if match else None
        limit = float(match.group(3)) if match else None
        motor: Mapping[str, Any] = {}
        motors = self._config_preview.get("motors")
        if isinstance(motors, list) and 0 <= index < len(motors):
            raw_motor = motors[index]
            if isinstance(raw_motor, Mapping):
                motor = raw_motor
        motor_id = motor.get("motor_id", "未知")
        joint = motor.get("joint_name", f"motor[{index}]")
        measurement = (
            f"当前位置与轨迹起点相差 {math.degrees(delta):.1f}°，"
            f"允许值为 {math.degrees(limit):.1f}°。"
            if delta is not None and limit is not None
            else "当前位置与轨迹起点差异超过安全阈值。"
        )
        message = (
            f"关节 {joint}（电机 ID {motor_id}）尚未处于轨迹起点。\n"
            f"{measurement}\n\n"
            "系统仍保持使能和当前位置保持，不会自动失能。"
        )
        self.preflight_result.setText(
            f"预检未通过：{joint} 未复位，禁止启动老化。"
        )
        self.preflight_result.setStyleSheet(
            "color: #b42318; font-weight: 700;"
        )
        if self._preflight_recheck_after_reset:
            self._preflight_recheck_after_reset = False
            QMessageBox.critical(
                self,
                "默认位与轨迹起点不一致",
                message
                + "\n\n回默认位后仍未满足预检条件，请检查默认位配置或重新生成轨迹。",
            )
            return

        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setWindowTitle("执行前尚未复位")
        dialog.setText(message)
        dialog.setInformativeText(
            "可立即低速回默认位；回位完成后系统会自动重新执行安全预检。"
        )
        reset_and_retry = dialog.addButton(
            "回默认位并重新预检",
            QMessageBox.ButtonRole.AcceptRole,
        )
        dialog.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(reset_and_retry)
        dialog.exec()
        if dialog.clickedButton() is reset_and_retry:
            self._start_default_return(recheck=True)

    def _health_received(self, payload: Any) -> None:
        if isinstance(payload, Mapping):
            status = payload.get("status", payload.get("state", "ok"))
            snapshot = payload.get("snapshot")
            if isinstance(snapshot, Mapping):
                self._render_snapshot(dict(snapshot))
        else:
            status = "ok"
        self.statusBar().showMessage(f"网关健康状态：{status}", 4000)

    def _snapshot_received(self, payload: Any) -> None:
        if isinstance(payload, Mapping):
            self._render_snapshot(dict(payload))

    @staticmethod
    def _pick(data: Mapping[str, Any], *keys: str, default: Any = "—") -> Any:
        for key in keys:
            value = data.get(key)
            if value is not None:
                return value
        return default

    @staticmethod
    def _format_value(value: Any, digits: int = 3) -> str:
        if value is None or value == "—":
            return "—"
        if isinstance(value, bool):
            return "是" if value else "否"
        if isinstance(value, float):
            return f"{value:.{digits}f}"
        return str(value)

    @staticmethod
    def _degrees(value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return value
        return math.degrees(float(value))

    @staticmethod
    def _software_diagnostics_by_motor(
        snapshot: Mapping[str, Any],
    ) -> dict[int, str]:
        fault = snapshot.get("global_fault", snapshot.get("fault"))
        diagnoses: Any = None
        if isinstance(fault, Mapping):
            diagnoses = fault.get("diagnoses")
        if not isinstance(diagnoses, list):
            diagnoses = snapshot.get("diagnostics")
        if not isinstance(diagnoses, list):
            return {}

        labels = {
            "multi_motor_bus_stale": "通信反馈超时（触发时）",
            "single_motor_stale": "通信反馈超时（触发时）",
            "motor_error": "驱动器硬件故障",
            "tracking_error": "轨迹跟踪误差",
            "undervoltage": "母线欠压",
            "motor_overtemperature": "电机过温",
            "persistent_torque_temperature_rise": "持续负载与温升异常",
            "persistent_high_torque": "持续高力矩",
            "temperature_rise": "温升异常",
        }
        result: dict[int, list[str]] = {}
        for raw_diagnosis in diagnoses:
            if not isinstance(raw_diagnosis, Mapping):
                continue
            rule = str(raw_diagnosis.get("rule", "software_fault"))
            label = labels.get(rule, "软件安全诊断")
            evidence = raw_diagnosis.get("evidence", [])
            evidence_items = (
                [str(item) for item in evidence]
                if isinstance(evidence, list)
                else [str(evidence)]
            )
            age_evidence = next(
                (
                    item
                    for item in evidence_items
                    if "feedback age=" in item or "minimum feedback age=" in item
                ),
                "",
            )
            if age_evidence:
                label = f"{label}：{age_evidence}"

            raw_ids = raw_diagnosis.get("affected_motor_ids", [])
            motor_ids: list[int] = []
            if isinstance(raw_ids, list):
                for raw_id in raw_ids:
                    try:
                        motor_ids.append(int(raw_id))
                    except (TypeError, ValueError):
                        pass
            if not motor_ids:
                legacy = " ".join(evidence_items)
                match = re.search(r"motor_ids=([0-9, ]+)", legacy)
                if match:
                    motor_ids.extend(
                        int(item)
                        for item in match.group(1).split(",")
                        if item.strip().isdigit()
                    )
            if not motor_ids:
                suspected = str(raw_diagnosis.get("suspected_component", ""))
                match = re.search(r"motor:(\d+)", suspected)
                if match:
                    motor_ids.append(int(match.group(1)))
            for motor_id in motor_ids:
                bucket = result.setdefault(motor_id, [])
                if label not in bucket:
                    bucket.append(label)
        return {
            motor_id: "；".join(items)
            for motor_id, items in result.items()
        }

    def _render_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._last_snapshot = snapshot
        module_name = self._active_limb or self.limb.currentText().strip()
        robot_name = self.robot_id.text().strip()
        self.header_module_label.setText(
            f"当前模块：{module_name or '未配置'}"
        )
        self.header_robot_label.setText(
            f"当前样机：{robot_name or '未填写'}"
        )
        motors: Any = snapshot.get("motors")
        if motors is None:
            motors = snapshot.get("motor_status")
        telemetry = snapshot.get("telemetry")
        if motors is None and isinstance(telemetry, Mapping):
            motors = telemetry.get("motors")
        if isinstance(motors, Mapping):
            normalized = []
            for joint, value in motors.items():
                item = dict(value) if isinstance(value, Mapping) else {"actual": value}
                item.setdefault("joint", joint)
                normalized.append(item)
            motors = normalized
        if not isinstance(motors, list):
            motors = []
        if not motors:
            configured_motors = snapshot.get("configured_motors")
            if isinstance(configured_motors, list):
                motors = [
                    {
                        **dict(item),
                        "software_diagnosis": "等待扫描电机并接收反馈",
                    }
                    for item in configured_motors
                    if isinstance(item, Mapping)
                ]
        software_diagnostics = self._software_diagnostics_by_motor(snapshot)
        station = snapshot.get("station")
        station_state = (
            str(station.get("state", "")).lower()
            if isinstance(station, Mapping)
            else str(snapshot.get("state", "")).lower()
        )
        manual_active = bool(
            station.get("manual_control_active")
            if isinstance(station, Mapping)
            else False
        )
        zero_allowed = (
            station_state == "connected"
            and self._motors_discovered
            and not manual_active
            and not self._zero_operation_pending
        )
        self.zero_all_button.setEnabled(zero_allowed and bool(motors))

        motor_rows: list[tuple[Any, ...]] = []
        motor_tones: dict[tuple[int, int], str] = {}
        zero_rows: list[tuple[Any, ...]] = []
        zero_tones: dict[tuple[int, int], str] = {}
        zero_metadata: list[dict[str, Any]] = []
        for row, raw_motor in enumerate(motors):
            motor = raw_motor if isinstance(raw_motor, Mapping) else {}
            target = self._pick(
                motor,
                "target",
                "target_position",
                "command_position",
                "command_position_rad",
                "cmd_pos_rad",
            )
            actual = self._pick(
                motor,
                "actual",
                "actual_position",
                "position",
                "position_rad",
                "pos_rad",
            )
            feedback_age = self._pick(
                motor,
                "feedback_age",
                "feedback_age_ms",
                "feedback_age_s",
                "age_ms",
                default="—",
            )
            try:
                age_limit = 0.2 if "feedback_age_s" in motor else 200.0
                stale = float(feedback_age) > age_limit
            except (TypeError, ValueError):
                stale = False
            motor_id = self._pick(motor, "motor_id", "id")
            try:
                diagnostic = software_diagnostics.get(
                    int(motor_id),
                    str(
                        self._pick(
                            motor,
                            "software_diagnosis",
                            default="正常",
                        )
                    ),
                )
            except (TypeError, ValueError):
                diagnostic = str(
                    self._pick(
                        motor,
                        "software_diagnosis",
                        default="正常",
                    )
                )
            if (
                diagnostic.startswith("通信反馈超时")
                and not stale
            ):
                diagnostic += "；当前反馈已恢复"
            explicit_error = self._pick(motor, "error", "position_error", default=None)
            if explicit_error is None and isinstance(target, (int, float)) and isinstance(
                actual, (int, float)
            ):
                explicit_error = target - actual
            target = self._degrees(target)
            actual = self._degrees(actual)
            explicit_error = self._degrees(explicit_error)
            speed = self._degrees(
                self._pick(motor, "speed", "speed_rad_s", "spd_rad_s")
            )
            if stale:
                actual = "—"
                explicit_error = "—"
                speed = "—"
            values = (
                self._pick(motor, "joint", "joint_name", "name"),
                self._pick(motor, "motor_id", "id"),
                self._pick(motor, "can", "can_interface", "interface", "bus"),
                target,
                actual,
                explicit_error,
                speed,
                (
                    "—"
                    if stale
                    else self._pick(motor, "torque", "torque_nm")
                ),
                (
                    "—"
                    if stale
                    else self._pick(motor, "temperature", "temperature_c", "temp_c")
                ),
                (
                    "—"
                    if stale
                    else self._pick(
                        motor,
                        "bus_voltage",
                        "bus_voltage_v",
                        "voltage",
                        "voltage_v",
                    )
                ),
                (
                    "—"
                    if stale
                    else self._pick(
                        motor,
                        "bus_current",
                        "bus_current_a",
                        "current",
                        "current_a",
                    )
                ),
                self._pick(motor, "error_id", default=0),
                diagnostic,
                feedback_age,
            )
            formatted_values = tuple(self._format_value(value) for value in values)
            motor_rows.append(formatted_values)
            try:
                has_error = int(values[11]) != 0
            except (TypeError, ValueError):
                has_error = values[11] not in (None, "", "0", "—")
            if has_error:
                motor_tones[(row, 11)] = "danger"
            if diagnostic not in {None, "", "正常", "—"}:
                motor_tones[(row, 12)] = "danger"
            else:
                motor_tones[(row, 12)] = "success"
            if stale:
                motor_tones[(row, 13)] = "warning"
            temperature = self._pick(
                motor,
                "temperature",
                "temperature_c",
                "temp_c",
                default=None,
            )
            try:
                if float(temperature) >= 70.0:
                    motor_tones[(row, 8)] = "warning"
            except (TypeError, ValueError):
                pass

            motor_index = self._pick(motor, "motor_index", "index", default=row)
            label = f"{values[0]}  |  ID {values[1]}  |  index {motor_index}"
            try:
                actual_index = int(motor_index)
            except (TypeError, ValueError):
                actual_index = row
            zero_values = (
                values[0],
                values[1],
                values[2],
                values[4],
                values[11],
                "标零中…" if self._zero_operation_pending else "标零",
            )
            zero_rows.append(
                tuple(self._format_value(value) for value in zero_values)
            )
            if has_error:
                zero_tones[(row, 4)] = "danger"
            zero_metadata.append(
                {
                    "motor_index": actual_index,
                    "label": label,
                    "action_enabled": zero_allowed,
                }
            )

        self.motor_table_model.set_rows(motor_rows, tones=motor_tones)
        self.zero_table_model.set_rows(
            zero_rows,
            tones=zero_tones,
            metadata=zero_metadata,
        )
        self._render_aging_status(snapshot)
        self._render_manual_status(snapshot, motors)
        self._render_fault(snapshot)
        self._sync_state(snapshot)
        zeroing = snapshot.get("zeroing")
        if isinstance(zeroing, Mapping):
            self._sync_zero_controls(zeroing)

    def _render_aging_status(self, snapshot: Mapping[str, Any]) -> None:
        playback = snapshot.get("playback")
        if not isinstance(playback, Mapping):
            self.aging_elapsed_label.setText("已运行：00:00:00")
            self.aging_cycle_label.setText("循环：0")
            self.aging_record_label.setText("记录：未开始")
            self.aging_statistics_model.set_rows(())
            self.save_csv_button.setEnabled(False)
            self.export_bundle_button.setEnabled(False)
            self.submit_review_button.setEnabled(False)
            self.next_run_button.setEnabled(False)
            return

        progress = playback.get("progress")
        if not isinstance(progress, Mapping):
            progress = snapshot.get("progress")
        if not isinstance(progress, Mapping):
            progress = {}
        try:
            active_seconds = max(0, int(float(progress.get("active_seconds", 0))))
        except (TypeError, ValueError):
            active_seconds = 0
        hours, remainder = divmod(active_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        self.aging_elapsed_label.setText(
            f"已运行：{hours:02d}:{minutes:02d}:{seconds:02d}"
        )
        self.aging_cycle_label.setText(
            "循环："
            f"{self._format_value(progress.get('cycle', 0))}"
            f"（已完成 {self._format_value(progress.get('completed_cycles', 0))}）"
        )

        recording = playback.get("recording")
        if isinstance(recording, Mapping):
            if recording.get("active"):
                record_state = "记录中"
            elif recording.get("finished"):
                record_state = "已完成"
            elif recording.get("enabled"):
                record_state = "等待开始"
            else:
                record_state = "未启用"
            filename = recording.get("filename")
            row_count = recording.get("row_count", 0)
            self.aging_record_label.setText(
                f"记录：{record_state} · {row_count} 行"
                + (f" · {filename}" if filename else "")
            )
            self.save_csv_button.setEnabled(
                bool(recording.get("finished") and filename)
            )
        else:
            self.aging_record_label.setText("记录：未启用")
            self.save_csv_button.setEnabled(False)

        statistics = playback.get("statistics")
        joints = (
            statistics.get("joints", [])
            if isinstance(statistics, Mapping)
            else []
        )
        if not isinstance(joints, list):
            joints = []
        rows: list[tuple[Any, ...]] = []
        tones: dict[tuple[int, int], str] = {}
        for row, raw in enumerate(joints):
            item = raw if isinstance(raw, Mapping) else {}
            values = (
                item.get("joint_name", f"joint_{row}"),
                item.get("sample_count", 0),
                self._degrees(item.get("tracking_rms_rad")),
                self._degrees(item.get("tracking_max_rad")),
                item.get("torque_peak_nm"),
                item.get("temperature_current_c"),
                item.get("temperature_rise_c"),
                item.get("error_samples", 0),
            )
            rows.append(tuple(self._format_value(value) for value in values))
            try:
                if int(values[7]) > 0:
                    tones[(row, 7)] = "danger"
            except (TypeError, ValueError):
                pass
            try:
                if float(values[6]) >= 15.0:
                    tones[(row, 6)] = "warning"
            except (TypeError, ValueError):
                pass
        self.aging_statistics_model.set_rows(rows, tones=tones)
        station = snapshot.get("station")
        state = (
            str(station.get("state", ""))
            if isinstance(station, Mapping)
            else str(snapshot.get("state", ""))
        ).lower()
        next_run_ready = state == "completed"
        self.next_run_button.setText(
            "已准备下一组" if state == "connected" else "准备下一组"
        )
        self.next_run_button.setEnabled(next_run_ready)
        report_ready = bool(
            state == "completed"
            and isinstance(recording, Mapping)
            and recording.get("finished")
        )
        self.export_bundle_button.setEnabled(report_ready)
        self.submit_review_button.setEnabled(report_ready)

    def _render_manual_status(
        self,
        snapshot: Mapping[str, Any],
        motors: list[Any],
    ) -> None:
        configured = snapshot.get("configured_motors")
        entries = configured if isinstance(configured, list) else []
        joint_positions = snapshot.get("joint_positions_rad")
        if not isinstance(joint_positions, list):
            joint_positions = []
        manual = snapshot.get("manual")
        manual_data = manual if isinstance(manual, Mapping) else {}
        manual_targets = manual_data.get("targets_rad")
        if not isinstance(manual_targets, list):
            manual_targets = []
        server_manual_active = manual_data.get("active") is True
        was_manual_active = self._manual_mode_active

        rebuild = self.manual_joint_table.rowCount() != len(entries)
        if not rebuild:
            for row, raw in enumerate(entries):
                entry = raw if isinstance(raw, Mapping) else {}
                existing = self.manual_joint_table.item(row, 0)
                if existing is None or existing.text() != str(
                    entry.get("joint_name", f"joint_{row}")
                ):
                    rebuild = True
                    break
        if rebuild:
            self.manual_joint_table.setRowCount(len(entries))
            for row, raw in enumerate(entries):
                entry = raw if isinstance(raw, Mapping) else {}
                self.manual_joint_table.setItem(
                    row,
                    0,
                    QTableWidgetItem(
                        str(entry.get("joint_name", f"joint_{row}"))
                    ),
                )
                motor_id = QTableWidgetItem(str(entry.get("motor_id", "—")))
                motor_id.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.manual_joint_table.setItem(row, 1, motor_id)
                actual = QTableWidgetItem("—")
                actual.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.manual_joint_table.setItem(row, 2, actual)
                target = QDoubleSpinBox()
                target.setObjectName("angleEditor")
                target.setRange(-360.0, 360.0)
                target.setDecimals(3)
                target.setSingleStep(0.1)
                target.setSuffix(" °")
                target.setAccelerated(False)
                target.setKeyboardTracking(False)
                target.setButtonSymbols(
                    QAbstractSpinBox.ButtonSymbols.UpDownArrows
                )
                target.setReadOnly(False)
                target.setEnabled(False)
                if row < len(joint_positions) and isinstance(
                    joint_positions[row], (int, float)
                ):
                    target.setValue(math.degrees(float(joint_positions[row])))
                target.valueChanged.connect(
                    lambda _value, motor_row=row: self._manual_target_changed(
                        motor_row
                    )
                )
                self.manual_joint_table.setCellWidget(row, 3, target)

        for row in range(min(len(entries), self.manual_joint_table.rowCount())):
            actual_value = (
                joint_positions[row] if row < len(joint_positions) else None
            )
            actual = self.manual_joint_table.item(row, 2)
            if actual is not None:
                actual.setText(self._format_value(self._degrees(actual_value)))
            target = self.manual_joint_table.cellWidget(row, 3)
            desired_target: Any = None
            if (
                server_manual_active
                and not was_manual_active
                and row < len(manual_targets)
            ):
                desired_target = manual_targets[row]
            elif not server_manual_active:
                desired_target = actual_value
            if isinstance(target, QDoubleSpinBox) and isinstance(
                desired_target, (int, float)
            ):
                target.blockSignals(True)
                target.setValue(math.degrees(float(desired_target)))
                target.blockSignals(False)

        station = snapshot.get("station")
        station_data = station if isinstance(station, Mapping) else {}
        state = str(station_data.get("state", snapshot.get("state", ""))).lower()
        enabled = station_data.get("motors_enabled") is True
        if self._manual_stop_requested:
            self._manual_mode_active = False
        elif not self._manual_starting or server_manual_active:
            self._manual_mode_active = server_manual_active
        manual_active = self._manual_mode_active
        moving = manual_data.get("moving") is True
        if manual_data.get("fault"):
            status = f"关节控制故障：{manual_data.get('fault')}"
        elif manual_active and moving:
            status = "正在平滑移动；控制线程持续发送目标位置"
        elif manual_active:
            status = "目标已到达，正在保持位置"
        elif enabled and state == "connected":
            status = "已就绪：可编辑整组目标角度和移动速度"
        else:
            status = "等待样机配置、连接并使能"
        self.manual_status_label.setText(status)
        ready = bool(enabled and state == "connected" and entries)
        self.manual_current_button.setEnabled(bool(joint_positions))
        self.manual_move_button.setText(
            "关节控制已开启"
            if manual_active
            else (
                "正在进入关节控制…"
                if self._manual_starting
                else "进入关节控制并保持"
            )
        )
        self.manual_move_button.setEnabled(
            ready and not manual_active and not self._manual_starting
        )
        self.manual_stop_button.setEnabled(manual_active)
        self.manual_disable_button.setEnabled(enabled)
        self._set_manual_input_enabled(manual_active)

    def _motor_background(
        self,
        motor: Mapping[str, Any],
        error_id: Any,
        feedback_age: Any,
        software_diagnosis: Any = "正常",
    ) -> QColor:
        severity = str(
            self._pick(motor, "severity", "status", "level", default="")
        ).lower()
        try:
            has_error = int(error_id) != 0
        except (TypeError, ValueError):
            has_error = error_id not in (None, "", "0", "—")
        has_software_fault = software_diagnosis not in {
            None,
            "",
            "正常",
            "—",
        }
        if (
            has_error
            or has_software_fault
            or severity in {"error", "fault", "critical", "failed"}
        ):
            return QColor("#ffd6d2")
        try:
            age_limit = 0.2 if "feedback_age_s" in motor else 200.0
            stale = float(feedback_age) > age_limit
        except (TypeError, ValueError):
            stale = False
        temperature = self._pick(
            motor, "temperature", "temperature_c", "temp_c", default=0
        )
        try:
            hot = float(temperature) >= 70.0
        except (TypeError, ValueError):
            hot = False
        if stale or hot or severity in {"warning", "warn", "degraded"}:
            return QColor("#fff0bd")
        return QColor("#e7f6e9")

    def _render_fault(self, snapshot: Mapping[str, Any]) -> None:
        fault = snapshot.get("global_fault", snapshot.get("fault"))
        if not fault:
            self.global_fault.setText("●  全局故障：无")
            self.global_fault.setObjectName("faultBarOk")
        else:
            if isinstance(fault, Mapping):
                diagnoses = fault.get("diagnoses")
                diagnosis: Mapping[str, Any] = fault
                if (
                    isinstance(diagnoses, list)
                    and diagnoses
                    and isinstance(diagnoses[0], Mapping)
                ):
                    diagnosis = diagnoses[0]
                suspected = self._pick(
                    diagnosis, "suspected_component", default="未知部件"
                )
                evidence = self._pick(diagnosis, "evidence", default="无")
                if isinstance(evidence, list):
                    evidence = "；".join(str(item) for item in evidence)
                action = self._pick(diagnosis, "action", default="停机检查")
                confidence = self._pick(diagnosis, "confidence", default="未知")
                text = (
                    f"⚠  全局故障  |  疑似部件：{suspected}  |  证据：{evidence}  |  "
                    f"处置：{action}  |  置信度：{confidence}"
                )
            else:
                text = f"⚠  全局故障：{fault}"
            self.global_fault.setText(text)
            self.global_fault.setObjectName("faultBarError")
        self.global_fault.style().unpolish(self.global_fault)
        self.global_fault.style().polish(self.global_fault)

    def _sync_state(self, snapshot: Mapping[str, Any]) -> None:
        configured_value = snapshot.get("configured")
        if isinstance(configured_value, bool):
            self._configured = configured_value
        lease = snapshot.get("lease")
        owns_lease: bool | None = None
        if isinstance(lease, Mapping):
            owns_lease = (
                lease.get("owner") == self.client.client_id
                and lease.get("expired") is not True
            )
        station = snapshot.get("station")
        if isinstance(station, Mapping):
            station_state = station.get("state")
            station_connected = station.get("backend_connected")
            station_enabled = station.get("motors_enabled")
            station_hold = station.get("position_hold_active")
            active_limb = str(station.get("limb", "")).strip()
        else:
            station_state = None
            station_connected = None
            station_enabled = None
            station_hold = None
            active_limb = ""
        self._active_limb = active_limb or None
        configured_motors = snapshot.get("configured_motors")
        active_motor_count = (
            len(configured_motors) if isinstance(configured_motors, list) else 0
        )
        active_config_path = str(snapshot.get("config_path") or "").strip()
        if self._configured and self._active_limb:
            self.active_config_label.setText(
                f"当前生效：{self._active_limb} · {active_motor_count} 个电机"
                + (f"\n{active_config_path}" if active_config_path else "")
            )
        else:
            self.active_config_label.setText("当前生效：尚未配置")
        selected_limb = self.limb.currentText().strip()
        self.configure_button.setText(
            "重新应用模块"
            if self._active_limb and selected_limb == self._active_limb
            else "切换并应用模块"
        )
        state = str(
            self._pick(
                snapshot,
                "state",
                "playback_state",
                "status",
                default=station_state or "",
            )
        ).lower()
        connected_value = self._pick(
            snapshot,
            "connected",
            "hardware_connected",
            default=station_connected,
        )
        if isinstance(connected_value, bool):
            self._connected = connected_value
            self._motors_discovered = connected_value
        elif state in {"connected", "armed", "running", "paused", "stopped"}:
            self._connected = True
        elif state in {"disconnected", "idle"}:
            self._connected = False
            self._motors_discovered = False
        if owns_lease is not None:
            self._lease_enabled = owns_lease

        if isinstance(station_enabled, bool):
            self._motors_enabled = station_enabled
        elif state in {"armed", "running", "paused"}:
            self._motors_enabled = True
        elif not self._motors_discovered:
            self._motors_enabled = False
        if state == "armed" and self._trajectory_imported:
            self._preflight_ok = True
            self.preflight_button.setEnabled(False)
        elif state in {"connected", "disconnected", "idle", "fault"}:
            self._preflight_ok = False

        can = snapshot.get("can")
        if isinstance(can, Mapping):
            all_up = can.get("all_up", can.get("connected"))
            if isinstance(all_up, bool):
                self._can_connected = all_up
            interfaces = can.get("interfaces", [])
            details = []
            if isinstance(interfaces, list):
                for item in interfaces:
                    if isinstance(item, Mapping):
                        name = item.get("interface", item.get("name", "can?"))
                        bus_state = item.get(
                            "bus_state",
                            "up" if item.get("up") else "down",
                        )
                        details.append(f"{name}:{bus_state}")
            detail_text = "，".join(details) if details else "无接口证据"
        else:
            detail_text = "等待服务状态"

        self.can_status_label.setText(
            f"CAN\n{'已连接' if self._can_connected else '未连接'}"
            f" · {detail_text}"
        )
        self.motor_discovery_label.setText(
            "电机\n"
            + (
                f"已发现 {active_motor_count} 台"
                if self._motors_discovered
                else "未发现"
            )
        )
        self.motor_enable_label.setText(
            f"使能\n{'是' if self._motors_enabled else '否'}"
        )
        self.position_hold_label.setText(
            f"位置保持\n{'运行中' if station_hold is True else '未运行'}"
        )
        self.feedback_read_label.setText(
            "状态读取\n"
            + (
                "只读、未施加力矩"
                if self._motors_discovered and not self._motors_enabled
                else ("实时控制反馈" if self._motors_enabled else "等待连接")
            )
        )
        set_tone(
            self.can_status_label,
            "success" if self._can_connected else "warning",
        )
        set_tone(
            self.motor_discovery_label,
            "success" if self._motors_discovered else "warning",
        )
        set_tone(
            self.motor_enable_label,
            "success" if self._motors_enabled else "warning",
        )
        set_tone(
            self.position_hold_label,
            "success" if station_hold is True else "warning",
        )
        set_tone(
            self.feedback_read_label,
            "success" if self._motors_discovered else "warning",
        )
        self.can_connect_button.setEnabled(
            self._configured and not self._can_connected
        )
        self.discover_button.setEnabled(
            self._configured
            and self._can_connected
            and not self._motors_discovered
        )
        self.enable_motors_button.setEnabled(
            self._motors_discovered and not self._motors_enabled
        )
        self.disconnect_button.setEnabled(
            self._can_connected or self._motors_discovered
        )
        can_apply_configuration = (
            bool(self._config_preview) and not self._configuration_pending
        )
        self.configure_button.setEnabled(can_apply_configuration)
        self.apply_editor_button.setEnabled(can_apply_configuration)
        self.clear_config_button.setEnabled(
            self._configured and not self._configuration_pending
        )
        self.preflight_button.setEnabled(
            self._trajectory_imported
            and self._motors_enabled
            and state == "connected"
        )
        self.start_button.setEnabled(
            self._motors_enabled and self._preflight_ok and state == "armed"
        )
        self.pause_button.setEnabled(state == "running")
        self.resume_button.setEnabled(state == "paused")
        self.stop_button.setEnabled(state in {"running", "paused"})
        self.reset_button.setEnabled(
            self._motors_discovered
            and state in {"connected", "armed", "completed", "fault"}
        )
        self.disable_button.setEnabled(self._motors_enabled)

    def _choose_config(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择电机配置", "", "YAML 配置 (*.yaml *.yml);;所有文件 (*)"
        )
        if path:
            limb = self.limb.currentText().strip()
            if not limb:
                QMessageBox.warning(self, "未选择部位", "请先在部位下拉框选择总成。")
                return
            self.upload_config_button.setEnabled(False)
            self._run_rest(
                "导入 YAML",
                lambda: self.client.config_upload(path, limb),
                self._config_preview_received,
                on_finished=lambda: self.upload_config_button.setEnabled(True),
            )

    def _preview_config(self) -> None:
        path = self.config_path.text().strip()
        limb = self.limb.currentText().strip()
        if not path or not limb:
            QMessageBox.warning(self, "信息不完整", "请填写网关配置路径并选择部位。")
            return
        self.preview_config_button.setEnabled(False)
        self._run_rest(
            "读取 YAML",
            lambda: self.client.config_preview(path, limb),
            self._config_preview_received,
            on_finished=lambda: self.preview_config_button.setEnabled(True),
        )

    def _config_preview_received(self, payload: Any) -> None:
        data = dict(payload) if isinstance(payload, Mapping) else {}
        result = data.get("result")
        if isinstance(result, Mapping):
            data = dict(result)
        motors = data.get("motors")
        groups = data.get("bus_groups")
        if not isinstance(motors, list) or not isinstance(groups, list):
            QMessageBox.critical(self, "YAML 无效", "网关未返回有效的电机参数。")
            return
        self._config_preview = data
        preview_limb = str(data.get("limb", "")).strip()
        if preview_limb and preview_limb != self.limb.currentText().strip():
            self.limb.setCurrentText(preview_limb)
        config_path = str(data.get("config_path", "")).strip()
        if config_path:
            self.config_path.setText(config_path)
            self.config_path.setCursorPosition(0)
        self._populate_motor_config(motors)
        self._populate_can_bindings(groups)
        self.config_validation_label.setText(
            f"已识别 {len(motors)} 个电机、{len(groups)} 个总线组；"
            "请检查参数并选择 CAN。"
        )
        self.config_summary_label.setText(
            f"待应用：{preview_limb or self.limb.currentText().strip()} · "
            f"{len(motors)} 个电机 · {len(groups)} 个 CAN 总线组"
        )
        can_apply = not self._configuration_pending
        self.configure_button.setEnabled(can_apply)
        self.apply_editor_button.setEnabled(can_apply)
        self._show_module_config_dialog()
        self._refresh_can_interfaces()

    def _populate_motor_config(self, motors: list[Any]) -> None:
        self.config_motor_table.setRowCount(len(motors))
        keys = (
            "index",
            "joint_name",
            "motor_id",
            "motor_type",
            "motor_model",
            "zero_offset",
            "kp",
            "kd",
            "sign",
        )
        for row, raw in enumerate(motors):
            motor = raw if isinstance(raw, Mapping) else {}
            for column, key in enumerate(keys):
                item = QTableWidgetItem(str(motor.get(key, "")))
                if column in {0, 1}:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if column != 1:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.config_motor_table.setItem(row, column, item)

    def _populate_can_bindings(self, groups: list[Any]) -> None:
        self.can_binding_table.setRowCount(len(groups))
        interface_names = [
            str(item.get("interface"))
            for item in self._available_can
            if isinstance(item, Mapping) and item.get("interface")
        ]
        if not interface_names:
            interface_names = [f"can{index}" for index in range(5)]
        for row, raw in enumerate(groups):
            group = raw if isinstance(raw, Mapping) else {}
            group_item = QTableWidgetItem(str(group.get("index", row)))
            count_item = QTableWidgetItem(str(group.get("motor_count", "")))
            group_item.setFlags(group_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            count_item.setFlags(count_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            group_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            count_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.can_binding_table.setItem(row, 0, group_item)
            self.can_binding_table.setItem(row, 1, count_item)

            interface = QComboBox()
            interface.addItems(interface_names)
            preferred_interface = str(
                group.get("default_interface", f"can{row}")
            )
            preferred_index = interface.findText(preferred_interface)
            if preferred_index >= 0:
                interface.setCurrentIndex(preferred_index)
            elif row < interface.count():
                interface.setCurrentIndex(row)
            mode = QComboBox()
            mode.addItem("CAN-FD", "canfd")
            mode.addItem("经典 CAN", "can")
            bitrate = QComboBox()
            for value in (125_000, 250_000, 500_000, 1_000_000):
                bitrate.addItem(f"{value // 1000} kbit/s", value)
            bitrate.setCurrentIndex(bitrate.count() - 1)
            dbitrate = QComboBox()
            for value in (1_000_000, 2_000_000, 4_000_000, 5_000_000):
                dbitrate.addItem(f"{value // 1_000_000} Mbit/s", value)
            dbitrate.setCurrentIndex(dbitrate.count() - 1)
            status_item = QTableWidgetItem("待刷新")
            status_item.setFlags(status_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            mode.currentIndexChanged.connect(
                lambda _index, mode=mode, data=dbitrate: data.setEnabled(
                    mode.currentData() == "canfd"
                )
            )
            interface.currentTextChanged.connect(
                lambda _name, row=row: self._update_can_binding_status(row)
            )
            self.can_binding_table.setCellWidget(row, 2, interface)
            self.can_binding_table.setCellWidget(row, 3, mode)
            self.can_binding_table.setCellWidget(row, 4, bitrate)
            self.can_binding_table.setCellWidget(row, 5, dbitrate)
            self.can_binding_table.setItem(row, 6, status_item)
            self._update_can_binding_status(row)

    def _refresh_can_interfaces(self) -> None:
        self.refresh_can_button.setEnabled(False)
        self._run_rest(
            "刷新 CAN",
            self.client.can_interfaces,
            self._can_interfaces_received,
            quiet=True,
            on_finished=lambda: self.refresh_can_button.setEnabled(True),
        )

    def _can_interfaces_received(self, payload: Any) -> None:
        try:
            selected = self._bus_bindings_from_table()
        except (TypeError, ValueError):
            selected = []
        data = dict(payload) if isinstance(payload, Mapping) else {}
        result = data.get("result")
        if isinstance(result, Mapping):
            data = dict(result)
        items = data.get("items", data.get("interfaces", []))
        self._available_can = [
            dict(item) for item in items if isinstance(item, Mapping)
        ]
        groups = self._config_preview.get("bus_groups", [])
        if isinstance(groups, list) and groups:
            self._populate_can_bindings(groups)
            for row, binding in enumerate(selected):
                if row >= self.can_binding_table.rowCount():
                    break
                for column, value in (
                    (2, binding["interface"]),
                    (3, binding["mode"]),
                    (4, binding["bitrate"]),
                    (5, binding["dbitrate"]),
                ):
                    combo = self.can_binding_table.cellWidget(row, column)
                    if not isinstance(combo, QComboBox) or value is None:
                        continue
                    index = combo.findData(value)
                    if column == 2:
                        index = combo.findText(str(value))
                    if index >= 0:
                        combo.setCurrentIndex(index)
        for row in range(self.can_binding_table.rowCount()):
            self._update_can_binding_status(row)

    def _update_can_binding_status(self, row: int) -> None:
        widget = self.can_binding_table.cellWidget(row, 2)
        item = self.can_binding_table.item(row, 6)
        if not isinstance(widget, QComboBox) or item is None:
            return
        name = widget.currentText()
        status = next(
            (
                entry
                for entry in self._available_can
                if str(entry.get("interface", "")) == name
            ),
            None,
        )
        if status is None:
            item.setText("未检测到")
            return
        owner = status.get("owner")
        state = "已开启" if status.get("up") else "未开启"
        if not status.get("exists", True):
            state = "不存在"
        if owner and owner != self.client.client_id:
            state += f" / 占用:{owner}"
        item.setText(state)

    def _motor_overrides_from_table(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for row in range(self.config_motor_table.rowCount()):
            values = [
                self.config_motor_table.item(row, column).text().strip()
                for column in range(self.config_motor_table.columnCount())
            ]
            result.append(
                {
                    "index": int(values[0]),
                    "motor_id": int(values[2]),
                    "motor_type": values[3],
                    "motor_model": int(values[4]),
                    "zero_offset": float(values[5]),
                    "kp": float(values[6]),
                    "kd": float(values[7]),
                    "sign": float(values[8]),
                }
            )
        return result

    def _bus_bindings_from_table(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for row in range(self.can_binding_table.rowCount()):
            interface = self.can_binding_table.cellWidget(row, 2)
            mode = self.can_binding_table.cellWidget(row, 3)
            bitrate = self.can_binding_table.cellWidget(row, 4)
            dbitrate = self.can_binding_table.cellWidget(row, 5)
            if not all(
                isinstance(widget, QComboBox)
                for widget in (interface, mode, bitrate, dbitrate)
            ):
                raise ValueError(f"CAN 总线组 {row} 尚未完成配置")
            name = interface.currentText().strip()
            is_fd = mode.currentData() == "canfd"
            result.append(
                {
                    "index": row,
                    "interface": name,
                    "mode": "canfd" if is_fd else "can",
                    "bitrate": int(bitrate.currentData()),
                    "dbitrate": int(dbitrate.currentData()) if is_fd else None,
                }
            )
        return result

    def _show_module_config_dialog(self) -> None:
        self._activate_dialog_navigation(5)
        self.module_config_dialog.show()
        self.module_config_dialog.raise_()
        self.module_config_dialog.activateWindow()

    def _new_module_window(self) -> None:
        window = FactoryMainWindow(
            self.client.base_url,
            operator_token=self.client.operator_token or None,
        )
        self._child_windows.append(window)
        window.destroyed.connect(
            lambda _object=None, current=window: self._child_windows.remove(current)
            if current in self._child_windows
            else None
        )
        window.show()

    def _suggest_config(self, limb: str) -> None:
        root = "/etc/rp1-factory-hmi/stations"
        suggestions = {
            "left_arm": f"{root}/left_arm_motors.yaml",
            "right_arm": f"{root}/right_arm_motors.yaml",
            "left_short_arm": f"{root}/left_short_arm_motors.yaml",
            "right_short_arm": f"{root}/right_short_arm_motors.yaml",
            "left_leg": f"{root}/left_leg_motors.yaml",
            "right_leg": f"{root}/right_leg_motors.yaml",
            "left_short_leg": f"{root}/left_short_leg_motors.yaml",
            "right_short_leg": f"{root}/right_short_leg_motors.yaml",
            "waist_hip": f"{root}/waist_hip_motors.yaml",
            "biped_waist": f"{root}/biped_waist_motors.yaml",
            "upper_body": f"{root}/upper_body_motors.yaml",
        }
        current = self.config_path.text().strip()
        if not current or current.startswith(f"{root}/"):
            self.config_path.setText(suggestions.get(limb, current))
            self.config_path.setCursorPosition(0)
        if self._config_preview and self._config_preview.get("limb") != limb:
            self._config_preview = {}
            if hasattr(self, "config_motor_table"):
                self.config_motor_table.setRowCount(0)
                self.can_binding_table.setRowCount(0)
                self.config_validation_label.setText(
                    "部位已改变，请重新导入或读取 YAML。"
                )
                self.config_summary_label.setText(
                    f"待应用：{limb} · 参数尚未载入"
                )
        self.configure_button.setText(
            "重新应用模块"
            if self._active_limb and limb == self._active_limb
            else "切换并应用模块"
        )
        can_apply = bool(self._config_preview) and not self._configuration_pending
        self.configure_button.setEnabled(can_apply)
        if hasattr(self, "apply_editor_button"):
            self.apply_editor_button.setEnabled(can_apply)

    def _configure(self) -> None:
        config_path = self.config_path.text().strip()
        robot_id = self.robot_id.text().strip()
        limb = self.limb.currentText().strip()
        if not config_path or not robot_id or not limb:
            QMessageBox.warning(self, "信息不完整", "请选择配置，并填写总成和样机编号。")
            return
        if not self._config_preview:
            QMessageBox.warning(
                self,
                "尚未识别 YAML",
                "请先打开“模块参数”弹窗，导入 YAML 或读取网关路径。",
            )
            self._show_module_config_dialog()
            return
        switching_live_module = self._can_connected or self._motors_discovered
        if switching_live_module:
            answer = QMessageBox.question(
                self,
                "确认切换模块",
                f"当前生效模块为 {self._active_limb or '未知'}，待应用模块为 "
                f"{limb}。\n\n切换会立即停止运动、软件失能、断开电机和 CAN，"
                "并清除旧轨迹。确认继续吗？",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            bus_bindings = self._bus_bindings_from_table()
            motor_overrides = self._motor_overrides_from_table()
        except (TypeError, ValueError) as exc:
            QMessageBox.warning(self, "配置参数无效", str(exc))
            self._show_module_config_dialog()
            return
        self._configuration_pending = True
        self.configure_button.setEnabled(False)
        self.apply_editor_button.setEnabled(False)
        self.clear_config_button.setEnabled(False)

        def apply_configuration() -> Any:
            if switching_live_module:
                self.client.clear_configuration()
            return self.client.configure(
                config_path,
                limb,
                str(self.backend.currentData()),
                bus_bindings=bus_bindings,
                motor_overrides=motor_overrides,
            )

        self._run_rest(
            "应用配置",
            apply_configuration,
            self._configured_ok,
            on_finished=self._configuration_operation_finished,
        )

    def _configured_ok(self, payload: Any) -> None:
        self._configured = True
        self._lease_enabled = True
        self._trajectory_imported = False
        self._preflight_ok = False
        self._can_connected = False
        self._motors_discovered = False
        self._motors_enabled = False
        self.configure_button.setEnabled(True)
        self.import_button.setEnabled(True)
        self.preflight_button.setEnabled(False)
        self.can_connect_button.setEnabled(True)
        self.discover_button.setEnabled(False)
        self.enable_motors_button.setEnabled(False)
        self.start_button.setEnabled(False)
        self._clear_trajectory_selection()
        bindings = self._bus_bindings_from_table()
        interfaces = ", ".join(str(item["interface"]) for item in bindings)
        self.config_summary_label.setText(
            f"{self.limb.currentText().strip()} · "
            f"{self.config_motor_table.rowCount()} 个电机 · {interfaces} · 已应用"
        )
        self.module_config_dialog.accept()
        self.setWindowTitle(
            f"可靠性测试上位机 - {self.limb.currentText().strip()} - {interfaces}"
        )
        data = dict(payload) if isinstance(payload, Mapping) else {}
        result = data.get("result")
        if isinstance(result, Mapping):
            data = dict(result)
        if data:
            self._render_snapshot(data)

    def _configuration_operation_finished(self) -> None:
        self._configuration_pending = False
        can_apply = bool(self._config_preview)
        self.configure_button.setEnabled(can_apply)
        self.apply_editor_button.setEnabled(can_apply)
        self.clear_config_button.setEnabled(self._configured)

    def _clear_trajectory_selection(self) -> None:
        self._trajectory_imported = False
        self._preflight_ok = False
        self._last_trajectory = {}
        self.trajectory_path.clear()
        self.trajectory_info.setText("尚未导入轨迹")
        self.preflight_result.setText("等待轨迹导入")
        self.preflight_button.setEnabled(False)
        self.start_button.setEnabled(False)

    def _clear_configuration(self) -> None:
        if not self._configured:
            return
        answer = QMessageBox.question(
            self,
            "确认清除当前配置",
            "这会立即停止运动、软件失能、断开电机和 CAN，并清除当前轨迹。"
            "确认继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._configuration_pending = True
        self.configure_button.setEnabled(False)
        self.apply_editor_button.setEnabled(False)
        self.clear_config_button.setEnabled(False)
        self._run_rest(
            "清除当前配置",
            self.client.clear_configuration,
            self._configuration_cleared_ok,
            on_finished=self._configuration_operation_finished,
        )

    def _configuration_cleared_ok(self, payload: Any) -> None:
        self._configured = False
        self._lease_enabled = False
        self._can_connected = False
        self._motors_discovered = False
        self._motors_enabled = False
        self._connected = False
        self._active_limb = None
        self._clear_trajectory_selection()
        self.active_config_label.setText("当前生效：尚未配置")
        data = dict(payload) if isinstance(payload, Mapping) else {}
        result = data.get("result")
        if isinstance(result, Mapping):
            data = dict(result)
        if data:
            self._render_snapshot(data)

    def _choose_trajectory(self) -> None:
        selected_limb = self.limb.currentText().strip()
        if not self._configured or self._active_limb != selected_limb:
            QMessageBox.warning(
                self,
                "模块配置尚未生效",
                f"当前生效模块：{self._active_limb or '无'}\n"
                f"待应用模块：{selected_limb or '无'}\n\n"
                "请先点击“切换并应用模块”，确认监控表关节数量更新后再导入轨迹。",
            )
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "导入轨迹", "", "NumPy 轨迹 (*.npz);;所有文件 (*)"
        )
        if not path:
            return
        self.trajectory_path.setText(path)
        self.import_button.setEnabled(False)
        self._run_rest(
            "导入轨迹",
            lambda: self.client.trajectory_upload(path),
            self._trajectory_imported_ok,
            on_finished=lambda: self.import_button.setEnabled(True),
        )

    def _trajectory_imported_ok(self, payload: Any) -> None:
        self.import_button.setEnabled(True)
        data = dict(payload) if isinstance(payload, Mapping) else {}
        result = data.get("result")
        if isinstance(result, Mapping):
            data = dict(result)
        self._last_trajectory = data
        self._trajectory_imported = True
        self._preflight_ok = False
        preflight = data.get("preflight")
        violations = data.get("preflight_violations", data.get("violations", []))
        if not isinstance(violations, list):
            violations = [violations] if violations else []
        warnings: list[Any] = []
        if isinstance(preflight, Mapping):
            raw_warnings = preflight.get("warnings", [])
            warnings = raw_warnings if isinstance(raw_warnings, list) else [raw_warnings]
        sha = self._pick(data, "sha256", "hash")
        frames = self._pick(data, "frames", "frame_count")
        duration = self._pick(data, "duration", "duration_s")
        max_speed = self._pick(data, "max_speed", "max_speed_rad_s", default=None)
        if max_speed is None and isinstance(preflight, Mapping):
            max_speed = self._pick(preflight, "max_velocity_rad_s")
        violation_text = "无" if not violations else "\n  • " + "\n  • ".join(
            self._format_violation(item) for item in violations
        )
        warning_text = "无" if not warnings else "\n  • " + "\n  • ".join(
            self._format_violation(item) for item in warnings
        )
        self.trajectory_info.setText(
            f"SHA-256：{sha}\n帧数：{frames}\n时长：{duration} s\n"
            f"最大速度：{max_speed}\n预检违规：{violation_text}\n"
            f"预检提示：{warning_text}"
        )
        self.preflight_result.setText("轨迹已解析，请执行预检确认。")
        self.preflight_button.setEnabled(self._motors_enabled)

    @staticmethod
    def _format_violation(item: Any) -> str:
        if isinstance(item, Mapping):
            return str(
                item.get("message")
                or item.get("detail")
                or json.dumps(dict(item), ensure_ascii=False)
            )
        return str(item)

    def _confirm_preflight(self) -> None:
        violations = self._last_trajectory.get(
            "preflight_violations",
            self._last_trajectory.get("violations", []),
        )
        preflight = self._last_trajectory.get("preflight")
        explicitly_unsafe = (
            isinstance(preflight, Mapping) and preflight.get("safe") is False
        )
        if violations or explicitly_unsafe:
            self._preflight_ok = False
            self.preflight_result.setText("预检不通过：必须解决全部违规后重新导入。")
            self.preflight_result.setStyleSheet("color: #b42318; font-weight: 700;")
            QMessageBox.critical(
                self, "预检不通过", "轨迹存在预检违规，禁止连接和启动。"
            )
            return
        station = self._last_snapshot.get("station")
        current_state = str(
            station.get("state", "")
            if isinstance(station, Mapping)
            else self._last_snapshot.get("state", "")
        ).lower()
        if current_state == "armed":
            self._preflight_ok = True
            self.preflight_result.setText("✓ 预检通过，当前已经处于 Armed 状态。")
            self.preflight_result.setStyleSheet(
                "color: #116329; font-weight: 700;"
            )
            self.preflight_button.setEnabled(False)
            self.start_button.setEnabled(True)
            return
        self._preflight_ok = False
        self.preflight_result.setText("✓ 预检通过，正在进入 Armed 状态。")
        self.preflight_result.setStyleSheet("color: #116329; font-weight: 700;")
        self.preflight_button.setEnabled(False)
        self._run_rest(
            "进入 Armed",
            self.client.arm,
            self._armed_ok,
            on_finished=lambda: self.preflight_button.setEnabled(
                self._trajectory_imported
                and self._motors_enabled
                and not self._preflight_ok
            ),
        )

    def _armed_ok(self, payload: Any) -> None:
        self._preflight_ok = True
        self._preflight_recheck_after_reset = False
        self.preflight_result.setText("✓ 预检通过，已进入 Armed 状态。")
        self.preflight_result.setStyleSheet("color: #116329; font-weight: 700;")
        self.preflight_button.setEnabled(False)
        if isinstance(payload, Mapping):
            snapshot = payload.get("snapshot", payload)
            if isinstance(snapshot, Mapping):
                self._render_snapshot(dict(snapshot))
        else:
            self._poll_snapshot()

    def _connect_can(self) -> None:
        try:
            bindings = self._bus_bindings_from_table()
        except (TypeError, ValueError) as exc:
            QMessageBox.warning(self, "CAN 配置无效", str(exc))
            return
        self.can_connect_button.setEnabled(False)
        self._run_rest(
            "连接 CAN",
            lambda: self.client.can_connect(bindings),
            self._can_connected_ok,
            on_finished=lambda: self.can_connect_button.setEnabled(
                self._configured and not self._can_connected
            ),
        )

    def _can_connected_ok(self, payload: Any) -> None:
        self._can_connected = True
        self._lease_enabled = True
        if isinstance(payload, Mapping):
            self._render_snapshot(dict(payload))
        self.discover_button.setEnabled(True)
        self.disconnect_button.setEnabled(True)

    def _discover_motors(self) -> None:
        if self._motors_discovered:
            QMessageBox.information(
                self,
                "电机已连接",
                "电机已经连接，无需重复扫描。如需重新扫描，请先断开连接。",
            )
            return
        self.discover_button.setEnabled(False)
        self._run_rest(
            "连接/扫描电机",
            self.client.discover_motors,
            self._motors_discovered_ok,
            on_finished=lambda: self.discover_button.setEnabled(
                self._can_connected and not self._motors_discovered
            ),
        )

    def _motors_discovered_ok(self, payload: Any) -> None:
        self._motors_discovered = True
        self._connected = True
        self._lease_enabled = True
        if isinstance(payload, Mapping):
            self._render_snapshot(dict(payload))
        self.enable_motors_button.setEnabled(True)
        self.disconnect_button.setEnabled(True)

    def _enable_motors(self) -> None:
        answer = QMessageBox.question(
            self,
            "电机使能安全确认",
            "确认物理急停有效、测试区域无人、机构无干涉？\n"
            "继续将使能全部已发现电机；任一电机失败会回滚全部使能。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self.enable_motors_button.setEnabled(False)
        self._run_rest(
            "确认并使能电机",
            lambda: self.client.enable_motors(
                physical_estop_confirmed=True
            ),
            self._motors_enabled_ok,
            on_finished=lambda: self.enable_motors_button.setEnabled(
                self._motors_discovered and not self._motors_enabled
            ),
        )

    def _motors_enabled_ok(self, payload: Any) -> None:
        self._motors_enabled = True
        if isinstance(payload, Mapping):
            self._render_snapshot(dict(payload))
        self.enable_motors_button.setEnabled(False)
        self.disconnect_button.setEnabled(True)
        self.preflight_button.setEnabled(self._trajectory_imported)
        self.pause_button.setEnabled(False)
        self.resume_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.reset_button.setEnabled(True)
        self.disable_button.setEnabled(True)
        self._poll_snapshot()

    def _disconnect(self) -> None:
        def disconnect_all() -> dict[str, Any]:
            results: dict[str, Any] = {}
            # Always ask the gateway to close the motor transport first. This
            # remains safe and idempotent when the desktop missed a fault-state
            # transition and its local flags are stale.
            results["motors"] = self.client.disconnect()
            results["can"] = self.client.can_disconnect()
            return results

        self._run_rest("断开连接", disconnect_all, self._disconnected_ok)

    def _disconnected_ok(self, _payload: Any) -> None:
        self._can_connected = False
        self._motors_discovered = False
        self._motors_enabled = False
        self._connected = False
        self._lease_enabled = False
        self.can_connect_button.setEnabled(self._configured)
        self.discover_button.setEnabled(False)
        self.enable_motors_button.setEnabled(False)
        self.disconnect_button.setEnabled(False)
        self.start_button.setEnabled(False)
        for button in (
            self.pause_button,
            self.resume_button,
            self.stop_button,
            self.reset_button,
            self.disable_button,
        ):
            button.setEnabled(False)

    def _manual_speed_from_slider(self, value: int) -> None:
        speed = float(value) / 10.0
        if abs(self.manual_speed_spin.value() - speed) >= 0.001:
            self.manual_speed_spin.blockSignals(True)
            self.manual_speed_spin.setValue(speed)
            self.manual_speed_spin.blockSignals(False)
        self._schedule_manual_command()

    def _manual_speed_from_spin(self, value: float) -> None:
        slider_value = int(round(float(value) * 10.0))
        if self.manual_speed_slider.value() != slider_value:
            self.manual_speed_slider.blockSignals(True)
            self.manual_speed_slider.setValue(slider_value)
            self.manual_speed_slider.blockSignals(False)
        self._schedule_manual_command()

    def _set_manual_input_enabled(self, enabled: bool) -> None:
        self.manual_speed_slider.setEnabled(enabled)
        self.manual_speed_spin.setEnabled(enabled)
        for row in range(self.manual_joint_table.rowCount()):
            target = self.manual_joint_table.cellWidget(row, 3)
            if isinstance(target, QDoubleSpinBox):
                target.setEnabled(enabled)

    def _manual_target_changed(self, _row: int) -> None:
        self._schedule_manual_command()

    def _schedule_manual_command(self) -> None:
        if (
            not self._manual_mode_active
            or self._manual_starting
            or self._manual_stop_requested
        ):
            return
        self.manual_command_timer.start()

    def _manual_targets(self) -> list[float]:
        targets: list[float] = []
        for row in range(self.manual_joint_table.rowCount()):
            target = self.manual_joint_table.cellWidget(row, 3)
            if not isinstance(target, QDoubleSpinBox):
                raise ValueError(f"第 {row + 1} 行缺少目标角度")
            targets.append(target.value())
        if not targets:
            raise ValueError("当前模块没有可控制的关节")
        return targets

    def _dispatch_manual_command(self) -> None:
        if not self._manual_mode_active or self._manual_stop_requested:
            return
        if self._manual_command_pending:
            self._manual_command_queued = True
            return
        try:
            targets = self._manual_targets()
        except ValueError as exc:
            self.statusBar().showMessage(str(exc), 8000)
            return
        speed = self.manual_speed_spin.value()
        self._manual_command_pending = True
        self._run_rest(
            "更新关节目标",
            lambda: self.client.manual_move(targets, speed),
            quiet=True,
            on_finished=self._manual_command_finished,
        )

    def _manual_command_finished(self) -> None:
        self._manual_command_pending = False
        if self._manual_stop_requested:
            self._send_manual_stop()
            return
        if self._manual_command_queued and self._manual_mode_active:
            self._manual_command_queued = False
            self.manual_command_timer.setInterval(40)
            self.manual_command_timer.start()

    def _manual_use_current(self) -> None:
        positions = self._last_snapshot.get("joint_positions_rad")
        if not isinstance(positions, list) or not positions:
            QMessageBox.warning(self, "无有效反馈", "尚未读取到当前关节角度。")
            return
        for row, value in enumerate(positions):
            target = self.manual_joint_table.cellWidget(row, 3)
            if isinstance(target, QDoubleSpinBox) and isinstance(
                value, (int, float)
            ):
                target.blockSignals(True)
                target.setValue(math.degrees(float(value)))
                target.blockSignals(False)
        self._schedule_manual_command()

    def _manual_move(self) -> None:
        try:
            targets = self._manual_targets()
        except ValueError as exc:
            QMessageBox.warning(self, "目标无效", str(exc))
            return
        answer = QMessageBox.question(
            self,
            "确认进入关节控制",
            "将持续保持当前位置。进入后可直接输入目标角度、点击或长按上下按钮，"
            f"并可随时调整移动速度（当前 {self.manual_speed_spin.value():.1f} °/s）。\n"
            "确认测试区域无人、机构运动路径无干涉？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._manual_starting = True
        self.manual_move_button.setEnabled(False)
        self._run_rest(
            "进入关节控制",
            lambda: self.client.manual_move(
                targets,
                self.manual_speed_spin.value(),
            ),
            self._manual_started,
            on_finished=self._manual_start_finished,
        )

    def _manual_started(self, payload: Any) -> None:
        self._manual_mode_active = True
        self._manual_stop_requested = False
        if isinstance(payload, Mapping):
            snapshot = payload.get("snapshot", payload.get("result"))
            if isinstance(snapshot, Mapping):
                self._render_snapshot(dict(snapshot))
        self._set_manual_input_enabled(True)
        self.manual_move_button.setText("关节控制已开启")
        self.manual_move_button.setEnabled(False)
        self.manual_stop_button.setEnabled(True)

    def _manual_start_finished(self) -> None:
        self._manual_starting = False
        if not self._manual_mode_active:
            self.manual_move_button.setText("进入关节控制并保持")
            self.manual_move_button.setEnabled(self._motors_enabled)

    def _manual_stop(self) -> None:
        self.manual_command_timer.stop()
        self._manual_command_queued = False
        self._manual_stop_requested = True
        self._manual_mode_active = False
        self._set_manual_input_enabled(False)
        self.manual_stop_button.setEnabled(False)
        if not self._manual_command_pending:
            self._send_manual_stop()

    def _send_manual_stop(self) -> None:
        self._run_rest(
            "停止关节移动",
            self.client.manual_stop,
            self._manual_stopped,
            on_finished=lambda: setattr(self, "_manual_stop_requested", False),
        )

    def _manual_stopped(self, payload: Any) -> None:
        self._manual_mode_active = False
        if isinstance(payload, Mapping):
            snapshot = payload.get("snapshot", payload.get("result"))
            if isinstance(snapshot, Mapping):
                self._render_snapshot(dict(snapshot))
        self.manual_move_button.setText("进入关节控制并保持")
        self.manual_move_button.setEnabled(self._motors_enabled)

    def _save_current_csv(self) -> None:
        playback = self._last_snapshot.get("playback")
        recording = (
            playback.get("recording")
            if isinstance(playback, Mapping)
            else None
        )
        filename = (
            str(recording.get("filename", "factory_record.csv"))
            if isinstance(recording, Mapping)
            else "factory_record.csv"
        )
        destination, _ = QFileDialog.getSaveFileName(
            self,
            "保存老化测试 CSV",
            filename,
            "CSV 文件 (*.csv);;所有文件 (*)",
        )
        if not destination:
            return
        self.save_csv_button.setEnabled(False)
        self._run_rest(
            "保存 CSV",
            lambda: self.client.download_current_record(destination),
            lambda path: self.statusBar().showMessage(
                f"CSV 已保存：{path}",
                8000,
            ),
            on_finished=lambda: self.save_csv_button.setEnabled(True),
        )

    def _platform_login(self) -> None:
        username = self.platform_username.text().strip()
        password = self.platform_password.text()
        station_id = self.platform_station_id.text().strip() or None
        if not username or not password:
            QMessageBox.warning(self, "信息不完整", "请输入工位操作员账号和密码。")
            return
        self.platform_login_button.setEnabled(False)
        self._run_rest(
            "登录测试平台",
            lambda: self.client.platform_login(
                username,
                password,
                station_id=station_id,
            ),
            self._platform_login_ok,
            on_finished=lambda: self.platform_login_button.setEnabled(True),
        )

    def _platform_login_ok(self, payload: Any) -> None:
        result = payload.get("result", payload) if isinstance(payload, Mapping) else {}
        operator = (
            str(result.get("operator") or self.platform_username.text().strip())
            if isinstance(result, Mapping)
            else self.platform_username.text().strip()
        )
        self._platform_operator = operator
        self.platform_password.clear()
        self.platform_identity_label.setText(f"平台账号：{operator}（认证中/已连接）")
        self.statusBar().showMessage("平台登录信息仅保存在网关内存中。", 8000)
        self._refresh_execution_history()

    def _refresh_execution_history(self) -> None:
        self.refresh_history_button.setEnabled(False)
        self._run_rest(
            "刷新执行记录",
            self.client.execution_history,
            self._execution_history_received,
            quiet=True,
            on_finished=lambda: self.refresh_history_button.setEnabled(True),
        )

    def _execution_history_received(self, payload: Any) -> None:
        items = payload.get("items", []) if isinstance(payload, Mapping) else []
        self._execution_history = [
            dict(item) for item in items if isinstance(item, Mapping)
        ]
        table = self.execution_history_table
        table.setRowCount(len(self._execution_history))
        for row, item in enumerate(self._execution_history):
            sync_state = str(item.get("sync_state") or "local_completed")
            sync_label = {
                "local_completed": "本地完成",
                "uploading": "上传中",
                "validating": "平台处理中",
                "pending_review": "待审批",
                "approved": "已批准",
                "rejected": "已拒绝",
                "sync_failed": "同步失败",
            }.get(sync_state, sync_state)
            values = (
                item.get("test_id", ""),
                item.get("sample_id", ""),
                item.get("verdict", ""),
                sync_label,
                item.get("review_reason", ""),
                item.get("started_at", ""),
            )
            for column, value in enumerate(values):
                table.setItem(row, column, QTableWidgetItem(str(value)))
        self.statusBar().showMessage(
            f"执行记录已刷新，共 {len(self._execution_history)} 条。", 4000
        )

    def _selected_execution(self) -> dict[str, Any] | None:
        row = self.execution_history_table.currentRow()
        if 0 <= row < len(self._execution_history):
            return self._execution_history[row]
        test_id = self.test_id.text().strip()
        return {"test_id": test_id} if test_id else None

    def _selected_test_id(self) -> str:
        item = self._selected_execution()
        return str(item.get("test_id") or "") if item else ""

    def _export_execution_bundle(self) -> None:
        test_id = self._selected_test_id()
        if not test_id:
            QMessageBox.warning(self, "未选择记录", "请选择一条执行记录。")
            return
        destination, _ = QFileDialog.getSaveFileName(
            self,
            "导出标准执行包",
            f"{test_id}.tar.gz",
            "执行包 (*.tar.gz);;所有文件 (*)",
        )
        if not destination:
            return
        self.export_bundle_button.setEnabled(False)
        self._run_rest(
            "导出执行包",
            lambda: self.client.download_execution_bundle(test_id, destination),
            lambda path: self.statusBar().showMessage(
                f"执行包已导出：{path}",
                8000,
            ),
            on_finished=lambda: self.export_bundle_button.setEnabled(True),
        )

    def _submit_execution(self) -> None:
        test_id = self._selected_test_id()
        if not test_id:
            QMessageBox.warning(self, "未选择记录", "请选择一条执行记录。")
            return
        self.submit_review_button.setEnabled(False)
        self._run_rest(
            "提交审批",
            lambda: self.client.submit_execution(test_id),
            lambda _payload: self._submission_action_ok("已加入待上传队列"),
            on_finished=lambda: self.submit_review_button.setEnabled(True),
        )

    def _retry_execution(self) -> None:
        test_id = self._selected_test_id()
        if not test_id:
            QMessageBox.warning(self, "未选择记录", "请选择一条执行记录。")
            return
        self._run_rest(
            "重试上传",
            lambda: self.client.retry_execution(test_id),
            lambda _payload: self._submission_action_ok("已重新加入上传队列"),
        )

    def _rebuild_execution(self) -> None:
        test_id = self._selected_test_id()
        if not test_id:
            QMessageBox.warning(self, "未选择记录", "请选择一条执行记录。")
            return
        self._run_rest(
            "重建报告",
            lambda: self.client.rebuild_execution(test_id),
            lambda _payload: self._submission_action_ok("报告与执行包已重建"),
        )

    def _submission_action_ok(self, message: str) -> None:
        self.statusBar().showMessage(message, 8000)
        self._refresh_execution_history()

    def _show_review_comment(self) -> None:
        item = self._selected_execution()
        if item is None:
            QMessageBox.information(self, "审批意见", "请选择一条执行记录。")
            return
        state = str(item.get("sync_state") or "local_completed")
        reason = str(item.get("review_reason") or "暂无审批意见")
        error = str(item.get("sync_error") or "")
        message = f"状态：{state}\n审批意见：{reason}"
        if error:
            message += f"\n同步错误：{error}"
        QMessageBox.information(self, "审批意见", message)

    def _prepare_next_run(self) -> None:
        answer = QMessageBox.question(
            self,
            "准备下一组老化",
            "将清除当前轨迹和预检状态，保留样机配置、CAN 连接及位置保持。"
            "确认继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.next_run_button.setEnabled(False)
        self._run_rest(
            "准备下一组",
            self.client.playback_next,
            self._next_run_ready,
        )

    def _next_run_ready(self, payload: Any) -> None:
        self._clear_trajectory_selection()
        self.next_run_button.setText("已准备下一组")
        if isinstance(payload, Mapping):
            snapshot = payload.get("snapshot", payload.get("result", payload))
            if isinstance(snapshot, Mapping):
                self._render_snapshot(dict(snapshot))
        self.statusBar().showMessage("已准备下一组，请导入新轨迹并执行预检。", 8000)

    def _start_playback(self) -> None:
        robot_id = self.robot_id.text().strip()
        if not robot_id:
            QMessageBox.warning(self, "信息不完整", "样机编号不能为空。")
            return
        use_duration = self.duration_mode.isChecked()
        self._run_rest(
            "开始运行",
            lambda: self.client.playback_start(
                speed=self.speed.value(),
                loop=True,
                cycles=0 if use_duration else self.cycles.value(),
                duration_hours=(
                    self.duration_hours.value() if use_duration else None
                ),
                record=self.record.isChecked(),
                record_rate_hz=self.record_rate_hz.value(),
                test_id=None,
                robot_id=robot_id,
                test_case_id=self._active_limb or self.limb.currentText().strip(),
                station_id=self.platform_station_id.text().strip() or None,
                operator_id=self._platform_operator or None,
            ),
            self._playback_started,
        )

    def _playback_started(self, payload: Any) -> None:
        result = payload.get("result", payload) if isinstance(payload, Mapping) else {}
        snapshot = result.get("snapshot", result) if isinstance(result, Mapping) else {}
        playback = snapshot.get("playback", {}) if isinstance(snapshot, Mapping) else {}
        test_id = str(playback.get("test_id") or "") if isinstance(playback, Mapping) else ""
        if test_id:
            self.test_id.setText(test_id)
        if isinstance(snapshot, Mapping):
            self._render_snapshot(dict(snapshot))

    def _update_run_limit_mode(self) -> None:
        use_duration = self.duration_mode.isChecked()
        self.duration_hours.setEnabled(use_duration)
        self.cycles.setEnabled(not use_duration)

    def _command(self, label: str, operation: Callable[[], Any]) -> None:
        self._run_rest(label, operation)

    def _return_default(self) -> None:
        answer = QMessageBox.question(
            self,
            "确认回默认位",
            "确认测试区域无人、机构运动路径无干涉？\n"
            "机械臂将低速回到 YAML 中配置的默认位置。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._start_default_return(recheck=False)

    def _start_default_return(self, *, recheck: bool) -> None:
        self.reset_button.setEnabled(False)
        self.preflight_button.setEnabled(False)
        self.preflight_result.setText("正在低速回默认位，请勿进入设备运动范围。")
        self.preflight_result.setStyleSheet(
            "color: #8a5a00; font-weight: 700;"
        )
        self._run_rest(
            "回默认位",
            self.client.playback_reset,
            lambda payload: self._default_returned(payload, recheck=recheck),
            on_finished=lambda: self.reset_button.setEnabled(
                self._motors_discovered
            ),
        )

    def _default_returned(self, payload: Any, *, recheck: bool) -> None:
        self._preflight_ok = False
        if isinstance(payload, Mapping):
            snapshot = payload.get("snapshot", payload)
            if isinstance(snapshot, Mapping):
                self._render_snapshot(dict(snapshot))
        self.preflight_result.setText(
            "已回默认位并恢复位置保持。"
            + (" 正在重新执行安全预检…" if recheck else " 请执行安全预检。")
        )
        self.preflight_result.setStyleSheet(
            "color: #116329; font-weight: 700;"
        )
        if recheck:
            self._preflight_recheck_after_reset = True
            QTimer.singleShot(0, self._confirm_preflight)

    def _software_disable(self) -> None:
        answer = QMessageBox.warning(
            self,
            "确认软件失能",
            "软件失能不能替代物理急停。\n确认向网关发送软件失能命令？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._run_rest(
                "软件失能",
                self.client.disable_motors,
                self._software_disabled_ok,
            )

    def _software_disabled_ok(self, payload: Any) -> None:
        self._motors_enabled = False
        self._preflight_ok = False
        if isinstance(payload, Mapping):
            self._render_snapshot(dict(payload))
        self.enable_motors_button.setEnabled(self._motors_discovered)
        self.start_button.setEnabled(False)

    def _extract_zeroing(self, payload: Any) -> dict[str, Any] | None:
        if not isinstance(payload, Mapping):
            return None
        data = dict(payload)
        result = data.get("result")
        if isinstance(result, Mapping):
            nested = result.get("zeroing")
            if isinstance(nested, Mapping):
                return dict(nested)
            if "state" in result and (
                "zeroed_indices" in result or "current_index" in result
            ):
                return dict(result)
        snapshot = data.get("snapshot")
        if isinstance(snapshot, Mapping):
            zeroing = snapshot.get("zeroing")
            if isinstance(zeroing, Mapping):
                return dict(zeroing)
        zeroing = data.get("zeroing")
        if isinstance(zeroing, Mapping):
            return dict(zeroing)
        if "state" in data and (
            "zeroed_indices" in data or "current_index" in data
        ):
            return data
        return None

    def _sync_zero_controls(self, zeroing: Mapping[str, Any] | None) -> None:
        if not hasattr(self, "zero_status_label"):
            return
        if not zeroing:
            self.zero_status_label.setText("等待从电机监控表选择电机。")
            self.zero_read_button.setEnabled(False)
            self.zero_confirm_button.setEnabled(False)
            self.zero_abort_button.setEnabled(False)
            return

        state = str(zeroing.get("state", "")).lower()
        current = zeroing.get("current_index")
        if state == "reading":
            if self._zero_selected_index != current:
                self._zero_selected_index = int(current) if current is not None else None
                self._zero_selected_label = (
                    f"index {current}" if current is not None else "未知电机"
                )
            self.zero_motor_label.setText(
                f"当前标零电机：{self._zero_selected_label}"
            )
            self.zero_status_label.setText(
                "所选电机已解除控制。请将该电机移动到机械零位，"
                "读取并核对当前位置后再确认标零。"
            )
        elif state == "prepared":
            self.zero_status_label.setText(
                "当前电机标零已完成。可关闭窗口，然后在电机监控表选择任意电机。"
            )
        elif state == "completed":
            self.zero_status_label.setText(
                "当前电机标零已完成。可在电机监控表继续选择任意电机。"
            )
        elif state == "aborted":
            reason = zeroing.get("abort_reason") or "operator abort"
            self.zero_status_label.setText(
                f"本次标零已取消（{reason}）。"
            )
        else:
            self.zero_status_label.setText("等待开始所选电机的标零。")

        reading = state == "reading"
        self.zero_read_button.setEnabled(reading)
        self.zero_confirm_button.setEnabled(reading)
        self.zero_abort_button.setEnabled(reading)

    def _zero_row_activated(self, row: int) -> None:
        metadata = self.zero_table_model.metadata(row)
        index = metadata.get("motor_index")
        label = metadata.get("label")
        if isinstance(index, int) and isinstance(label, str):
            self._start_zero_for_motor(index, label)

    def _start_zero_for_motor(self, motor_index: int, label: str) -> None:
        if not self._motors_discovered:
            QMessageBox.warning(
                self,
                "尚未扫描电机",
                "请先连接 CAN 并扫描电机。",
            )
            return
        station = self._last_snapshot.get("station")
        state = (
            str(station.get("state", "")).lower()
            if isinstance(station, Mapping)
            else str(self._last_snapshot.get("state", "")).lower()
        )
        if state != "connected":
            QMessageBox.warning(
                self,
                "当前不能标零",
                "电机标零只允许在 Connected 状态执行。"
                "请先停止老化或关节控制，再进行标零。",
            )
            return

        if self._zero_operation_pending:
            return
        answer = QMessageBox.warning(
            self,
            "确认单关节标零",
            (
                f"确认将当前姿态写为该关节的零位？\n\n{label}\n\n"
                "标零前会停止位置保持并失能全部电机，完成后保持失能。"
                "请先可靠支撑机构，并确认所选关节已位于机械零位。"
            ),
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._zero_operation_pending = True
        self._render_snapshot(dict(self._last_snapshot))
        self._run_rest(
            f"标零 {label}",
            lambda: self.client.zero_motor(int(motor_index)),
            lambda payload: self._direct_zero_succeeded(payload, label),
            on_finished=self._direct_zero_finished,
        )

    def _zero_all_motors(self) -> None:
        if self._zero_operation_pending or not self._motors_discovered:
            return
        answer = QMessageBox.warning(
            self,
            "确认一键标零全部关节",
            (
                "确认将所有关节的当前位置同时写为零位？\n\n"
                "该操作不可撤销。系统会停止位置保持、失能全部电机并逐个写入零位，"
                "完成后继续保持失能。请先可靠支撑整个机构，并确认每个关节均已在机械零位。"
            ),
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._zero_operation_pending = True
        self._render_snapshot(dict(self._last_snapshot))
        self._run_rest(
            "一键标零全部关节",
            self.client.zero_all_motors,
            lambda payload: self._direct_zero_succeeded(payload, "全部关节"),
            on_finished=self._direct_zero_finished,
        )

    def _direct_zero_succeeded(self, payload: Any, label: str) -> None:
        warnings: list[Any] = []
        if isinstance(payload, Mapping):
            snapshot = payload.get("snapshot")
            if isinstance(snapshot, Mapping):
                self._render_snapshot(dict(snapshot))
            result = payload.get("result")
            if isinstance(result, Mapping):
                raw_warnings = result.get("warnings")
                if isinstance(raw_warnings, list):
                    warnings = raw_warnings
        if warnings:
            QMessageBox.warning(
                self,
                "标零命令已发送，请复查",
                (
                    f"{label}的置零命令已发送，但驱动反馈角度未立即回到零位附近。\n"
                    "这与终端标零脚本显示的 warning 含义一致，不代表 CAN 发送失败。"
                    "请重新使能并读取角度，确认零位是否生效；未生效时不要继续运动。\n\n"
                    "当前全部电机保持失能。"
                ),
            )
        else:
            QMessageBox.information(
                self,
                "标零完成",
                (
                    f"{label}标零完成。\n"
                    "当前全部电机保持失能；需要运动前，请重新执行“使能电机”。"
                ),
            )

    def _direct_zero_finished(self) -> None:
        self._zero_operation_pending = False
        if self._last_snapshot:
            self._render_snapshot(dict(self._last_snapshot))

    def _prepare_selected_zero(self) -> None:
        motor_index = self._zero_selected_index
        if motor_index is None:
            return
        self.zero_motor_label.setText(
            f"准备标零电机：{self._zero_selected_label}"
        )
        self.zero_status_label.setText("正在解除所选电机控制并准备标零…")
        self._zero_command(
            f"prepare motor {motor_index}",
            lambda: self.client.zero_prepare(int(motor_index)),
            on_success=self._zero_prepared,
        )

    def _zero_prepared(self, _payload: Any) -> None:
        self._show_zero_dialog()

    def _zero_confirm(self) -> None:
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setWindowTitle("二次确认标零")
        dialog.setText(
            f"确认当前位置就是该电机的机械零位？\n{self._zero_selected_label}"
        )
        dialog.setInformativeText(
            "此操作会写入零位。故障态下默认且始终不允许确认（allow_on_fault=false）。"
        )
        dialog.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        dialog.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if dialog.exec() == QMessageBox.StandardButton.Yes:
            self._zero_command(
                "confirm (allow_on_fault=false)",
                lambda: self.client.zero_confirm(allow_on_fault=False),
            )

    def _zero_command(
        self,
        label: str,
        operation: Callable[[], Any],
        *,
        on_success: Callable[[Any], None] | None = None,
    ) -> None:
        def succeeded(payload: Any) -> None:
            self._zero_result(label, payload)
            if on_success is not None:
                on_success(payload)

        self._run_rest(
            f"标零 {label}",
            operation,
            succeeded,
        )

    def _zero_result(self, action: str, payload: Any) -> None:
        zeroing = self._extract_zeroing(payload)
        display: Any = zeroing
        if display is None and isinstance(payload, Mapping):
            result = payload.get("result")
            display = result if result is not None else payload
        if isinstance(display, (dict, list)):
            rendered = json.dumps(display, ensure_ascii=False, indent=2)
        else:
            rendered = str(display)
        self.zero_log.append(f"[{action}]\n{rendered}\n")
        self._sync_zero_controls(zeroing)
        if isinstance(payload, Mapping):
            snapshot = payload.get("snapshot")
            if isinstance(snapshot, Mapping):
                self._render_snapshot(dict(snapshot))
        if action.startswith("confirm"):
            QMessageBox.information(
                self,
                "标零完成",
                f"已完成所选电机标零：\n{self._zero_selected_label}",
            )
            self.zero_dialog.accept()
        elif action == "abort":
            self.zero_dialog.reject()

    def closeEvent(self, event: Any) -> None:
        self._lease_enabled = False
        previous_timeout = self.client.timeout
        self.client.timeout = min(previous_timeout, 2.0)
        try:
            if self._motors_enabled:
                self.client.playback_disable()
            if self._motors_discovered:
                self.client.disconnect()
            if self._can_connected:
                self.client.can_disconnect()
        except Exception:
            # The gateway lease monitor performs the same fail-safe cleanup
            # when the desktop disappears unexpectedly.
            pass
        finally:
            self.client.timeout = previous_timeout
        self.reconnect_timer.stop()
        self.snapshot_timer.stop()
        self.lease_timer.stop()
        self.websocket.close()
        super().closeEvent(event)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="可靠性测试 PySide6 桌面客户端")
    parser.add_argument(
        "--gateway-url",
        default="http://127.0.0.1:8765",
        help="网关基地址（默认：http://127.0.0.1:8765）",
    )
    parser.add_argument(
        "--operator-token",
        default=None,
        help="可选网关操作令牌",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    app = QApplication(list(sys.argv if argv is None else [sys.argv[0], *argv]))
    app.setApplicationName("Factory Ageing HMI")
    window = FactoryMainWindow(
        args.gateway_url,
        operator_token=args.operator_token,
    )
    window.show()
    return app.exec()


__all__ = ["FactoryMainWindow", "build_parser", "main"]
