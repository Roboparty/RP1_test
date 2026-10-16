"""PLC power-cabinet page for the PySide6 desktop."""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from PySide6.QtCore import QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from factory_hmi.client import GatewayClient, GatewayError
from factory_hmi.desktop.theme import set_tone
from factory_hmi.plc.faults import cabinet_fault_messages


def _text(value: Any) -> str:
    if value is None:
        return "INVALID"
    if isinstance(value, bool):
        return "ON" if value else "OFF"
    if isinstance(value, float):
        return f"{value:.2f}"
    if isinstance(value, (list, dict, tuple)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _at(values: Any, index: int, default: Any = None) -> Any:
    if isinstance(values, (list, tuple)) and len(values) > index:
        return values[index]
    return default


def _ps1_value(value: Any) -> str:
    return "未确认" if value is None else f"{float(value):.3f}"


class _TrendChart(QWidget):
    COLORS = (
        QColor("#2375C9"),
        QColor("#1F9D69"),
        QColor("#D9822B"),
        QColor("#8B5CF6"),
    )

    def __init__(self, title: str, key: str, unit: str, maximum: float) -> None:
        super().__init__()
        self.title = title
        self.key = key
        self.unit = unit
        self.maximum = maximum
        self.minimum_span = 0.2 if unit == "A" else 1.0
        self.axis_range = (0.0, maximum)
        self._has_valid_samples = False
        self.samples: list[Mapping[str, Any]] = []
        self.channel_checks: list[QCheckBox] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(64, 0, 16, 2)
        layout.addStretch(1)
        legend = QHBoxLayout()
        for channel, color in enumerate(self.COLORS):
            check = QCheckBox(f"CH{channel + 1}")
            check.setChecked(True)
            check.setStyleSheet(f"color: {color.name()};")
            check.setToolTip("勾选显示此通道；纵轴按显示通道的有效数据自动缩放")
            check.toggled.connect(self._channels_changed)
            self.channel_checks.append(check)
            legend.addWidget(check)
        legend.addStretch(1)
        layout.addLayout(legend)
        self.setMinimumHeight(190)

    def set_samples(self, samples: list[Mapping[str, Any]]) -> None:
        self.samples = samples[-300:]
        self._update_range()
        self.update()

    def _channels_changed(self, _checked: bool) -> None:
        self._update_range(force=True)
        self.update()

    def _sample_value(self, sample: Mapping[str, Any], channel: int) -> float | None:
        try:
            if not int(sample.get("AnalogValidMask") or 0) & (1 << channel):
                return None
            values = sample.get(self.key)
            if not isinstance(values, (list, tuple)):
                return None
            value = float(values[channel])
        except (IndexError, TypeError, ValueError, OverflowError):
            return None
        return value if math.isfinite(value) and 0.0 <= value <= self.maximum else None

    def _update_range(self, *, force: bool = False) -> None:
        values = [
            value
            for sample in self.samples
            for channel, check in enumerate(self.channel_checks)
            if check.isChecked()
            if (value := self._sample_value(sample, channel)) is not None
        ]
        self._has_valid_samples = bool(values)
        if not values:
            self.axis_range = (0.0, self.maximum)
            return
        low, high = min(values), max(values)
        span = max(self.minimum_span, (high - low) * 1.2)
        middle = (low + high) / 2
        lower = max(0.0, min(middle - span / 2, self.maximum - span))
        upper = min(self.maximum, lower + span)
        # Rounded ticks with headroom, without forcing the axis to start at zero.
        raw_step = (upper - lower) / 4
        magnitude = 10 ** math.floor(math.log10(raw_step))
        step = next(n * magnitude for n in (1, 2, 2.5, 5, 10) if n * magnitude >= raw_step)
        lower = max(0.0, math.floor(lower / step + 1e-9) * step)
        upper = min(self.maximum, math.ceil(upper / step - 1e-9) * step)
        old_lower, old_upper = self.axis_range
        # Expand immediately when needed; shrink only after a substantial change.
        # This avoids axis jitter while a low-current signal fluctuates slightly.
        if force or low < old_lower or high > old_upper or upper - lower < (old_upper - old_lower) * 0.65:
            self.axis_range = (lower, upper)

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#FFFFFF"))
        left, top, right, bottom = 64, 30, 16, 36
        plot_width = max(1, self.width() - left - right)
        plot_height = max(1, self.height() - top - bottom)
        painter.setPen(QColor("#24435E"))
        lower, upper = self.axis_range
        painter.drawText(left, 20, f"{self.title}  (自动 {lower:g}–{upper:g} {self.unit})")
        painter.setPen(QPen(QColor("#DFE6EE"), 1))
        for row in range(5):
            y = top + plot_height * row / 4
            painter.drawLine(left, int(y), left + plot_width, int(y))
            value = lower + (upper - lower) * (4 - row) / 4
            painter.setPen(QColor("#718096"))
            painter.drawText(2, int(y) + 4, f"{value:.3f}".rstrip("0").rstrip("."))
            painter.setPen(QPen(QColor("#DFE6EE"), 1))
        selected = any(check.isChecked() for check in self.channel_checks)
        if len(self.samples) < 2 or not selected or not self._has_valid_samples:
            painter.setPen(QColor("#718096"))
            message = "请选择趋势通道" if not selected else (
                "等待实时样本" if len(self.samples) < 2 else "暂无有效样本"
            )
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, message)
            return
        count = len(self.samples)
        for channel in range(4):
            if not self.channel_checks[channel].isChecked():
                continue
            path = QPainterPath()
            started = False
            for index, sample in enumerate(self.samples):
                value = self._sample_value(sample, channel)
                if value is None:
                    started = False
                    continue
                x = left + plot_width * index / max(1, count - 1)
                y = top + plot_height * (1.0 - (value - lower) / (upper - lower))
                point = QPointF(x, y)
                if not started:
                    path.moveTo(point)
                    started = True
                else:
                    path.lineTo(point)
            painter.setPen(QPen(self.COLORS[channel], 2))
            painter.drawPath(path)


class PlcCabinetPage(QWidget):
    snapshot_changed = Signal(dict)

    def __init__(
        self,
        client: GatewayClient,
        runner: Callable[..., None],
        operator_provider: Callable[[], str],
    ) -> None:
        super().__init__()
        self.client = client
        self.runner = runner
        self.operator_provider = operator_provider
        self.setObjectName("tabPage")
        self._pending = False
        self._pending_count = 0
        self._retired_epochs: set[str] = set()
        self._request_error = ""
        self._submitted_request_id: str | None = None
        self._snapshot: dict[str, Any] = {}
        self._setpoint_dirty = False
        self._setpoint_edit_revision = 0
        self._submitted_setpoint_revision: int | None = None
        self._last_setpoint_feedback = ""
        self._build()
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        QTimer.singleShot(0, self.refresh)

    def stop(self) -> None:
        self.timer.stop()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 4)
        layout.setSpacing(12)
        title = QLabel("PLC 配电柜")
        title.setObjectName("pageHeading")
        description = QLabel(
            "系统总使能严格执行“主回路 → PS1 实际输出 → 通道许可”；"
            "所有操作发送逻辑命令并等待 PLC 序号确认。"
        )
        description.setObjectName("pageDescription")
        self.description = description
        layout.addWidget(title)
        layout.addWidget(description)
        self.coordination_state = QLabel("PLC 为所有窗口共享；正在同步控制状态")
        self.coordination_state.setObjectName("infoPanel")
        self.coordination_state.setWordWrap(True)
        layout.addWidget(self.coordination_state)

        flow = QFrame()
        flow.setObjectName("phasePanel")
        flow_layout = QHBoxLayout(flow)
        flow_layout.setContentsMargins(14, 10, 14, 10)
        flow_layout.setSpacing(8)
        self.flow_labels: list[QLabel] = []
        for index, text in enumerate(
            ("1  PLC通信", "2  主回路", "3  PS1输出", "4  通道投入")
        ):
            label = QLabel(text)
            label.setObjectName("flowStep")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.flow_labels.append(label)
            flow_layout.addWidget(label, 1)
            if index < 3:
                arrow = QLabel("→")
                arrow.setObjectName("flowArrow")
                flow_layout.addWidget(arrow)
        layout.addWidget(flow)

        control_row = QHBoxLayout()
        control_row.setSpacing(12)

        system_group = QGroupBox("系统总控（安全顺序）")
        system_group.setObjectName("workflowCard")
        system_layout = QGridLayout(system_group)
        system_layout.setContentsMargins(14, 18, 14, 14)
        system_layout.setHorizontalSpacing(10)
        system_layout.setVerticalSpacing(10)
        self.communication = QLabel("通信：连接中")
        self.communication.setObjectName("infoPanel")
        self.phase = QLabel("阶段：—")
        self.phase.setObjectName("infoPanel")
        self.main_state = QLabel("主回路：—")
        self.main_state.setObjectName("infoPanel")
        self.main_state.setWordWrap(True)
        self.ps1_state = QLabel("PS1：—")
        self.ps1_state.setObjectName("infoPanel")
        system_layout.addWidget(self.communication, 0, 0)
        system_layout.addWidget(self.phase, 0, 1)
        system_layout.addWidget(self.main_state, 1, 0, 1, 2)
        system_layout.addWidget(self.ps1_state, 2, 0, 1, 2)

        self.connect_button = QPushButton("连接 PLC")
        self.connect_button.setObjectName("outlineButton")
        self.connect_button.clicked.connect(
            lambda: self._command(
                "连接 PLC", lambda context: self.client.plc_connect(self._user(), context=context)
            )
        )
        self.start_button = QPushButton("系统总使能（顺序启动）")
        self.start_button.setObjectName("startButton")
        self.start_button.clicked.connect(self._start)
        self.stop_button = QPushButton("正常总停止")
        self.stop_button.setObjectName("criticalOutlineButton")
        self.stop_button.clicked.connect(
            lambda: self._command(
                "正常停止", lambda context: self.client.plc_stop(self._user(), context=context)
            )
        )
        self.all_stop_button = QPushButton("全部停止")
        self.all_stop_button.setObjectName("dangerButton")
        self.all_stop_button.clicked.connect(
            lambda: self._command(
                "全部停止", lambda context: self.client.plc_all_stop(self._user(), context=context), priority=True
            )
        )
        self.reset_button = QPushButton("故障复位脉冲")
        self.reset_button.setObjectName("outlineButton")
        self.reset_button.clicked.connect(
            lambda: self._command(
                "故障复位", lambda context: self.client.plc_reset_fault(self._user(), context=context)
            )
        )
        primary_controls = QHBoxLayout()
        primary_controls.setSpacing(8)
        primary_controls.addWidget(self.connect_button)
        primary_controls.addWidget(self.start_button, 2)
        primary_controls.addWidget(self.stop_button)
        system_layout.addLayout(primary_controls, 3, 0, 1, 2)
        safety_controls = QHBoxLayout()
        safety_controls.setSpacing(8)
        safety_controls.addWidget(self.reset_button)
        safety_controls.addWidget(self.all_stop_button)
        system_layout.addLayout(safety_controls, 4, 0, 1, 2)
        self.sequence_feedback = QLabel("总控反馈：尚未操作")
        self.sequence_feedback.setObjectName("controlHint")
        self.sequence_feedback.setWordWrap(True)
        system_layout.addWidget(self.sequence_feedback, 5, 0, 1, 2)
        self.reset_feedback = QLabel("PLC控制故障复位：等待协议状态")
        self.reset_feedback.setObjectName("controlHint")
        self.reset_feedback.setWordWrap(True)
        system_layout.addWidget(self.reset_feedback, 6, 0, 1, 2)
        control_row.addWidget(system_group, 3)

        setpoint_group = QGroupBox("PS1 电源设定")
        setpoint_group.setObjectName("workflowCard")
        setpoint_layout = QGridLayout(setpoint_group)
        setpoint_layout.setContentsMargins(14, 18, 14, 14)
        setpoint_layout.setHorizontalSpacing(10)
        setpoint_layout.setVerticalSpacing(10)
        voltage_label = QLabel("目标电压")
        voltage_label.setObjectName("fieldLabel")
        self.voltage_setpoint = QDoubleSpinBox()
        self.voltage_setpoint.setDecimals(3)
        self.voltage_setpoint.setSuffix(" V")
        self.voltage_setpoint.setValue(3.0)
        self.voltage_setpoint.setKeyboardTracking(False)
        self.voltage_setpoint.valueChanged.connect(self._mark_setpoint_dirty)
        self.voltage_setpoint.lineEdit().textEdited.connect(self._mark_setpoint_dirty)
        current_label = QLabel("限流值")
        current_label.setObjectName("fieldLabel")
        self.current_setpoint = QDoubleSpinBox()
        self.current_setpoint.setDecimals(3)
        self.current_setpoint.setSuffix(" A")
        self.current_setpoint.setValue(0.5)
        self.current_setpoint.setKeyboardTracking(False)
        self.current_setpoint.valueChanged.connect(self._mark_setpoint_dirty)
        self.current_setpoint.lineEdit().textEdited.connect(self._mark_setpoint_dirty)
        setpoint_layout.addWidget(voltage_label, 0, 0)
        setpoint_layout.addWidget(self.voltage_setpoint, 0, 1)
        setpoint_layout.addWidget(current_label, 1, 0)
        setpoint_layout.addWidget(self.current_setpoint, 1, 1)
        self.apply_setpoints_button = QPushButton("应用设定并顺序启动")
        self.apply_setpoints_button.setObjectName("primaryButton")
        self.apply_setpoints_button.clicked.connect(self._apply_setpoints)
        setpoint_layout.addWidget(self.apply_setpoints_button, 2, 0, 1, 2)
        self.setpoint_summary = QLabel("设定回读：—\nPS1实际输出：—")
        self.setpoint_summary.setObjectName("infoPanel")
        self.setpoint_summary.setWordWrap(True)
        setpoint_layout.addWidget(self.setpoint_summary, 3, 0, 1, 2)
        self.setpoint_feedback = QLabel("设定反馈：尚未操作")
        self.setpoint_feedback.setObjectName("controlHint")
        self.setpoint_feedback.setWordWrap(True)
        setpoint_layout.addWidget(self.setpoint_feedback, 4, 0, 1, 2)
        self.setpoint_hint = QLabel("设定值将在主回路与PS1通信就绪后自动下发。")
        self.setpoint_hint.setObjectName("muted")
        self.setpoint_hint.setWordWrap(True)
        setpoint_layout.addWidget(self.setpoint_hint, 5, 0, 1, 2)
        control_row.addWidget(setpoint_group, 2)
        layout.addLayout(control_row)

        self.fault = QLabel("故障：无")
        self.fault.setObjectName("faultBarOk")
        self.fault.setWordWrap(True)
        layout.addWidget(self.fault)

        channel_group = QGroupBox("CH1～CH4 单路使能")
        channel_group.setObjectName("workflowCard")
        channel_layout = QHBoxLayout(channel_group)
        channel_layout.setContentsMargins(12, 18, 12, 12)
        channel_layout.setSpacing(10)
        self.channel_checks: list[QCheckBox] = []
        self.channel_titles: list[QLabel] = []
        self.channel_physical: list[QLabel] = []
        self.channel_permits: list[QLabel] = []
        self.channel_measurements: list[QLabel] = []
        self.channel_hints: list[QLabel] = []
        self.channel_buttons: list[QPushButton] = []
        for row in range(4):
            channel = row + 1
            card = QFrame()
            card.setObjectName("channelCard")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(12, 10, 12, 12)
            card_layout.setSpacing(7)
            card_title = QLabel(f"CH{channel}  ·  DUT{channel} / can{row}")
            card_title.setObjectName("channelTitle")
            physical_label = QLabel("物理许可：—")
            physical_label.setObjectName("channelState")
            permit_label = QLabel("PLC通道许可：—")
            permit_label.setObjectName("channelState")
            measurement = QLabel("0.00 V   0.00 A\n0.00 W   模拟量 —")
            measurement.setObjectName("channelMetric")
            checkbox = QCheckBox("系统总使能时投入")
            checkbox.setChecked(True)
            button = QPushButton(f"CH{channel} 单路投入")
            button.setObjectName("startButton")
            button.clicked.connect(
                lambda _checked=False, selected=channel: self._toggle_channel(selected)
            )
            hint = QLabel("等待状态")
            hint.setObjectName("controlHint")
            hint.setWordWrap(True)
            card_layout.addWidget(card_title)
            card_layout.addWidget(physical_label)
            card_layout.addWidget(permit_label)
            card_layout.addWidget(measurement)
            card_layout.addWidget(checkbox)
            card_layout.addWidget(button)
            card_layout.addWidget(hint)
            self.channel_checks.append(checkbox)
            self.channel_titles.append(card_title)
            self.channel_physical.append(physical_label)
            self.channel_permits.append(permit_label)
            self.channel_measurements.append(measurement)
            self.channel_hints.append(hint)
            self.channel_buttons.append(button)
            channel_layout.addWidget(card, 1)
        layout.addWidget(channel_group)

        trend_group = QGroupBox("实时趋势")
        trend_group.setObjectName("workflowCard")
        charts = QHBoxLayout(trend_group)
        charts.setContentsMargins(10, 16, 10, 8)
        self.voltage_chart = _TrendChart("电压趋势", "Voltage", "V", 50.0)
        self.current_chart = _TrendChart("电流趋势", "Current", "A", 50.0)
        charts.addWidget(self.voltage_chart, 1)
        charts.addWidget(self.current_chart, 1)
        layout.addWidget(trend_group)

        log_group = QGroupBox(
            "操作日志（UTC时间 / 用户 / 旧值 / 新值 / 序号 / PLC确认）"
        )
        log_group.setObjectName("workflowCard")
        log_layout = QVBoxLayout(log_group)
        log_layout.setContentsMargins(10, 16, 10, 10)
        self.logs = QTableWidget(0, 7)
        self.logs.setHorizontalHeaderLabels(
            ("时间", "用户", "字段", "旧值", "新值", "命令序号", "确认结果")
        )
        self.logs.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.logs.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.logs.verticalHeader().setVisible(False)
        self.logs.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.logs.setMinimumHeight(170)
        log_layout.addWidget(self.logs)
        layout.addWidget(log_group, 1)

    def _user(self) -> str:
        return self.operator_provider().strip() or "local-operator"

    def _finished(self) -> None:
        self._pending_count = max(0, self._pending_count - 1)
        self._pending = self._pending_count > 0

    def refresh(self) -> None:
        if self._pending:
            return
        self._pending_count += 1
        self._pending = True
        self.runner(
            "刷新 PLC 状态", self.client.plc_snapshot, self._receive,
            quiet=True, on_finished=self._finished,
        )

    def _failed(self, exc: Exception) -> None:
        if isinstance(exc, GatewayError) and isinstance(exc.detail, Mapping):
            self._request_error = str(exc.detail.get("message") or exc)
            self._receive(exc.detail.get("snapshot"))
        else:
            self._request_error = str(exc)
        self._render()

    def _command(self, label: str, operation: Callable[[Mapping[str, Any]], Any],
                 *, priority: bool = False, request_id: str | None = None) -> None:
        if self._pending and not priority:
            return
        context = dict(self._snapshot.get("coordination", {}))
        context["request_id"] = request_id or str(uuid.uuid4())
        self._request_error = ""
        self._pending_count += 1
        self._pending = True
        self.runner(
            label, lambda: operation(context), self._receive, quiet=True,
            on_finished=self._finished, on_failure=self._failed,
        )

    def _start(self) -> None:
        if self._pending:
            return
        # Commit drafts on the GUI thread before the runner queues its worker.
        self.voltage_setpoint.interpretText()
        self.current_setpoint.interpretText()
        voltage = self.voltage_setpoint.value()
        current = self.current_setpoint.value()
        user = self._user()
        channels = [
            index
            for index, checkbox in enumerate(self.channel_checks, start=1)
            if checkbox.isChecked()
        ]
        self._submitted_setpoint_revision = self._setpoint_edit_revision
        self._submitted_request_id = str(uuid.uuid4())
        self._command(
            "启动 PLC 供电顺序",
            lambda context: self.client.plc_start(channels, user, voltage, current, context=context),
            request_id=self._submitted_request_id,
        )

    def _toggle_channel(self, channel: int) -> None:
        status = self._snapshot.get("status")
        permits = status.get("ChannelPermit", []) if isinstance(status, Mapping) else []
        enabled = not bool(permits[channel - 1]) if len(permits) >= channel else True
        self._command(
            f"CH{channel} {'投入' if enabled else '切除'}",
            lambda context: self.client.plc_set_channel(channel, enabled, self._user(), context=context),
        )

    def _mark_setpoint_dirty(self, _value: float | str) -> None:
        self._setpoint_dirty = True
        self._setpoint_edit_revision += 1

    def _apply_setpoints(self) -> None:
        self._start()

    def _receive(self, payload: Any) -> None:
        value = payload
        if isinstance(value, Mapping) and isinstance(value.get("result"), Mapping):
            value = value["result"]
        if not isinstance(value, Mapping):
            return
        new = value.get("coordination", {})
        old = self._snapshot.get("coordination", {})
        epoch = str(new.get("epoch", ""))
        if epoch in self._retired_epochs:
            return
        if old.get("epoch") == epoch:
            if new.get("snapshot_sequence", 0) < old.get("snapshot_sequence", 0):
                return
        elif old.get("epoch"):
            self._retired_epochs.add(old["epoch"])
        self._snapshot = dict(value)
        self._render()
        self.snapshot_changed.emit(self._snapshot)

    def _render(self) -> None:
        snapshot = self._snapshot
        communication = str(snapshot.get("communication_state") or "DISCONNECTED")
        stale = bool(snapshot.get("stale"))
        fresh = communication == "LIVE" and not stale
        read_only = bool(snapshot.get("read_only"))
        coordination = snapshot.get("coordination", {})
        busy = bool(coordination.get("busy"))
        last = coordination.get("active_command") or coordination.get("last_command") or {}
        states = {"pending": "等待 PLC 确认", "confirmed": "已确认", "rejected": "已拒绝", "superseded": "已被全部停止中止", "timeout": "确认超时", "failed": "执行未确认"}
        operations = {"start": "总启动", "stop": "正常总停止", "all-stop": "全部停止", "channel": "通道切换", "setpoints": "电源设定", "reset-fault": "故障复位", "connect": "连接", "disconnect": "断开"}
        text = "所有窗口共同控制同一 PLC；冲突操作不自动补发"
        if last:
            source = "本窗口" if last.get("client_id") == self.client.client_id else f"其他窗口 {str(last.get('client_id', ''))[-8:]}"
            text += f"\n{source} · {last.get('user', '')} · {operations.get(last.get('operation'), '')} · {states.get(last.get('state'), last.get('state', ''))}"
        if self._request_error:
            text += f"\n本次操作未确认：{self._request_error}"
        self.coordination_state.setText(text)
        set_tone(self.coordination_state, "warning" if self._request_error or busy else "neutral")
        live = fresh and not read_only
        driver = str(snapshot.get("driver") or "未知")
        connection_type = "PLC 模拟" if driver == "mock" else f"PLC 实机 · {driver}"
        access = "只读监控" if read_only else "控制模式"
        self.description.setText(
            "读取实物 PLC 状态；当前未启用心跳、启停、使能和设定值写入。"
            if read_only else "系统总使能严格执行“主回路 → PS1 实际输出 → 通道许可”；操作等待 PLC 序号确认。"
        )
        self.communication.setText(
            f"{connection_type} · {access} · {snapshot.get('endpoint', '')}\n通信：{communication}{' / STALE' if stale else ''}"
        )
        set_tone(
            self.communication,
            "warning" if driver == "mock" else "success" if fresh else "danger",
        )
        phase = str(snapshot.get("phase") or "—")
        self.phase.setText(f"阶段：{phase}")
        set_tone(
            self.phase,
            "success"
            if phase == "RUNNING"
            else "warning"
            if phase.startswith(("START_", "WAIT_", "STOP_"))
            else "danger"
            if phase in {"FAULT", "COMM_LOST", "STALE"}
            else "neutral",
        )
        status = snapshot.get("status")
        status = status if isinstance(status, Mapping) else {}
        self.communication.setText(self.communication.text() + f"\n协议：{status.get('ProtocolMode') or '尚未确认'}")
        command = snapshot.get("command")
        command = command if isinstance(command, Mapping) else {}
        self.main_state.setText(
            "主回路  ·  "
            f"请求 {_text(command.get('MainEnable'))}  /  "
            f"KM0反馈 {_text(status.get('MainContactorFB'))}  /  "
            f"主回路就绪 {_text(status.get('MainReady'))}"
        )
        self.ps1_state.setText(
            "PS1输出/真实反馈  ·  "
            f"通信 {_text(status.get('PS1CommOK'))}  /  "
            f"请求 {_text(command.get('PS1OutputEnable'))}  /  "
            f"实际 {_text(status.get('PS1ActualOutput')) if status.get('PS1ActualOutput') is not None else '未确认'}  /  "
            f"PS1反馈 {'有效' if status.get('PS1FeedbackValid') else '未确认或过期'}"
        )
        messages = cabinet_fault_messages(status)
        faulted = bool(messages)
        last_error = str(snapshot.get("last_error") or "")
        self.fault.setText(
            ("故障：" + "；".join(messages) if messages else "故障：无")
            + (f"  |  {last_error}" if last_error and last_error != "；".join(messages) else "")
        )
        self.fault.setObjectName(
            "faultBarError" if faulted or last_error else "faultBarOk"
        )
        self.fault.style().unpolish(self.fault)
        self.fault.style().polish(self.fault)

        main_ready = bool(status.get("MainReady"))
        ps1_actual = bool(status.get("PS1ActualOutput"))
        permits = status.get("ChannelPermit") or [False] * 4
        any_channel = any(bool(_at(permits, index, False)) for index in range(4))
        flow_states = (
            (f"1  PLC通信  {'LIVE' if live else communication}", live),
            (f"2  主回路  {'已就绪' if main_ready else '未就绪'}", main_ready),
            (f"3  PS1输出  {'未确认' if status.get('PS1ActualOutput') is None else '已开启' if ps1_actual else '已关闭'}", ps1_actual),
            (
                f"4  通道投入  {'运行中' if any_channel else '未投入'}",
                any_channel,
            ),
        )
        for index, (text, confirmed) in enumerate(flow_states):
            label = self.flow_labels[index]
            label.setText(text)
            waiting = (
                index == 1
                and phase in {"WAIT_MAIN_ACK", "WAIT_MAIN_READY"}
                or index == 2
                and phase
                in {
                    "WAIT_PS1_COMM",
                    "WAIT_PS1_STOP_ACK",
                    "WAIT_PS1_STOP_READY",
                    "WAIT_PS1_ACK",
                    "WAIT_PS1_OUTPUT",
                }
                or index == 3
                and phase in {"WAIT_CHANNEL_ACK", "WAIT_CHANNEL_PERMIT"}
            )
            set_tone(
                label,
                "success"
                if confirmed
                else "warning"
                if waiting
                else "danger"
                if index == 0 and not live
                else "neutral",
            )

        physical = status.get("PhysicalPermit") or [False] * 4
        voltage = status.get("Voltage") or [None] * 4
        current = status.get("Current") or [None] * 4
        power = status.get("Power") or [None] * 4
        valid_mask = int(status.get("AnalogValidMask") or 0)
        can_map = snapshot.get("dut_can_map") or {}
        feedback = snapshot.get("feedback") or {}
        feedback = feedback if isinstance(feedback, Mapping) else {}
        group_feedback = feedback.get("ChannelEnable") or {}
        for row in range(4):
            item_feedback = feedback.get(f"ChannelEnable{row + 1}") or group_feedback
            item_feedback = item_feedback if isinstance(item_feedback, Mapping) else {}
            physical_value = _at(physical, row, None)
            permit_value = bool(_at(permits, row, False))
            self.channel_titles[row].setText(
                f"CH{row + 1}  ·  DUT{row + 1} / "
                f"{can_map.get(f'DUT{row + 1}', f'can{row}')}"
            )
            self.channel_physical[row].setText(f"物理许可：{_text(physical_value)}")
            set_tone(
                self.channel_physical[row],
                "success"
                if physical_value is True
                else "danger"
                if physical_value is False
                else "warning",
            )
            self.channel_permits[row].setText(f"PLC通道许可：{_text(permit_value)}")
            set_tone(
                self.channel_permits[row],
                "success" if permit_value else "neutral",
            )
            analog_state = "有效" if valid_mask & (1 << row) else "无效"
            self.channel_measurements[row].setText(
                f"{_text(_at(voltage, row))} V   {_text(_at(current, row))} A\n"
                f"{_text(_at(power, row))} W   模拟量{analog_state}"
            )
            feedback_state = str(item_feedback.get("state") or "confirmed")
            feedback_detail = str(item_feedback.get("detail") or "")
            if feedback_state == "pending":
                reason = "等待PLC确认命令序号"
            elif permit_value:
                reason = "已投入；可单独切除"
            elif not live:
                reason = "PLC通信不可用"
            elif phase != "RUNNING":
                reason = "请先点击“系统总使能（顺序启动）”"
            elif not ps1_actual:
                reason = "等待PS1实际输出"
            elif physical_value is False:
                reason = "物理许可缺失，禁止投入"
            elif feedback_state in {"rejected", "timeout"}:
                reason = feedback_detail or f"上次命令{feedback_state}"
            elif physical_value is None:
                reason = "原始许可未提供，以PLC最终许可为准"
            else:
                reason = "条件满足，可投入"
            button = self.channel_buttons[row]
            button.setText(
                f"CH{row + 1} 单路切除" if permit_value else f"CH{row + 1} 单路投入"
            )
            can_operate = (
                live
                and not busy
                and phase == "RUNNING"
                and feedback_state != "pending"
                and (
                    permit_value
                    or (
                        phase == "RUNNING"
                        and ps1_actual
                        and physical_value is not False
                        and bool((snapshot.get('output_admission') or {}).get('allowed'))
                    )
                )
            )
            button.setEnabled(can_operate)
            button.setObjectName(
                "criticalOutlineButton" if permit_value else "startButton"
            )
            button.style().unpolish(button)
            button.style().polish(button)
            button.setToolTip(reason)
            self.channel_hints[row].setText(reason)
            self.channel_checks[row].setEnabled(live and phase == "OFF")

        start_feedback = feedback.get("Start") or {}
        stop_feedback = feedback.get("Stop") or {}
        start_feedback = start_feedback if isinstance(start_feedback, Mapping) else {}
        stop_feedback = stop_feedback if isinstance(stop_feedback, Mapping) else {}
        start_state = str(start_feedback.get("state") or "confirmed")
        stop_state = str(stop_feedback.get("state") or "confirmed")
        start_detail = str(start_feedback.get("detail") or "")
        stop_detail = str(stop_feedback.get("detail") or "")
        if start_state == "pending":
            sequence_text = f"总启动：等待PLC确认 · {start_detail}"
        elif stop_state == "pending":
            sequence_text = f"正常总停止：等待PLC确认 · {stop_detail}"
        elif start_detail and start_detail != "not requested":
            sequence_text = f"总启动：{start_state} · {start_detail}"
        elif stop_detail and stop_detail != "not requested":
            sequence_text = f"正常总停止：{stop_state} · {stop_detail}"
        else:
            sequence_text = "总控反馈：尚未操作"
        self.sequence_feedback.setText(sequence_text)
        any_pending = busy or any(
            isinstance(value, Mapping) and value.get("state") == "pending"
            for value in feedback.values()
        )
        self.start_button.setEnabled(
            live and phase == "OFF" and not faulted and not any_pending
            and status.get('ProtocolVersion') == 0x0102
            and not status.get('PS1ManualRestartLock') and not status.get('PS1RecoveryExhausted')
        )
        self.start_button.setToolTip("控制需配套PLC V1.5 / Host Protocol V1.2（0x0102）" if status.get('ProtocolVersion') != 0x0102 else "人工顺序启动；PS1真实反馈确认后才允许输出")
        self.stop_button.setEnabled(
            live
            and phase not in {"OFF", "DISCONNECTED", "COMM_LOST", "STALE"}
            and not any_pending
        )
        reset_supported = bool(status.get("ResetResultSupported"))
        reset_feedback = feedback.get("ResetFaultPulse") or {}
        reset_detail = str(reset_feedback.get("detail") or "")
        reset_state = str(reset_feedback.get("state") or "confirmed")
        if status.get("ProtocolVersion") != 0x0102:
            reset_text = ("旧协议V1.1仅可监控；复位回执已解码，控制需配套PLC Host Protocol V1.2（0x0102）"
                          if reset_supported else "旧协议/无复位结果回执：控制需配套PLC Host Protocol V1.2（0x0102）")
        elif reset_detail and reset_detail != "not requested":
            reset_text = f"PLC控制故障复位：{states.get(reset_state, reset_state)} · {reset_detail}"
        else:
            reset_text = "V1.2：复位仅清除PLC控制故障，不清除PS1通信或设备故障"
        self.reset_feedback.setText(reset_text)
        set_tone(self.reset_feedback, "warning" if not reset_supported or reset_state in {"pending", "rejected", "timeout", "failed"} else "neutral")
        self.reset_button.setText("复位PLC控制故障")
        self.reset_button.setToolTip(reset_text)
        requests_off = not command.get("MainEnable") and not command.get("PS1OutputEnable") and not any(command.get("ChannelEnable") or [])
        self.reset_button.setEnabled(live and reset_supported and status.get('ProtocolVersion') == 0x0102 and requests_off and not any_pending)
        self.all_stop_button.setEnabled(live)
        self.connect_button.setEnabled(communication != "LIVE")

        limits = snapshot.get("setpoint_limits")
        limits = limits if isinstance(limits, Mapping) else {}
        setpoint_enabled = bool(limits.get("enabled"))
        voltage_min = float(limits.get("voltage_min_v") or 0.0)
        voltage_max = float(limits.get("voltage_max_v") or 0.0)
        current_min = float(limits.get("current_min_a") or 0.0)
        current_max = float(limits.get("current_max_a") or 0.0)
        step = float(limits.get("step") or 0.001)
        setpoint_feedback = feedback.get("Setpoints") or {}
        setpoint_feedback = (
            setpoint_feedback if isinstance(setpoint_feedback, Mapping) else {}
        )
        setpoint_state = str(setpoint_feedback.get("state") or "confirmed")
        setpoint_detail = str(setpoint_feedback.get("detail") or "")
        own = next((item for item in coordination.get("commands", [])
                    if item.get("request_id") == self._submitted_request_id
                    and item.get("client_id") == self.client.client_id), {})
        own_confirmed = own.get("state") == "confirmed"
        legacy_confirmed = not coordination and self._last_setpoint_feedback == "pending" and setpoint_state == "confirmed"
        if own_confirmed or legacy_confirmed:
            if self._submitted_setpoint_revision == self._setpoint_edit_revision:
                self._setpoint_dirty = False
            self._submitted_setpoint_revision = None
            self._submitted_request_id = None
        self._last_setpoint_feedback = setpoint_state
        ranges_valid = (
            setpoint_enabled
            and voltage_max > voltage_min
            and current_max > current_min
            and step > 0.0
        )
        if ranges_valid:
            for editor, minimum, maximum in (
                (self.voltage_setpoint, voltage_min, voltage_max),
                (self.current_setpoint, current_min, current_max),
            ):
                editor.blockSignals(True)
                # Even an unchanged setRange normalizes uncommitted text.
                if editor.minimum() != minimum or editor.maximum() != maximum:
                    editor.setRange(minimum, maximum)
                if editor.singleStep() != step:
                    editor.setSingleStep(step)
                editor.blockSignals(False)
            readback_available = bool(status.get("PS1SetpointValid"))
            should_sync = (
                readback_available
                and not self._setpoint_dirty
                and not self.voltage_setpoint.hasFocus()
                and not self.current_setpoint.hasFocus()
            )
            if should_sync:
                self.voltage_setpoint.blockSignals(True)
                self.current_setpoint.blockSignals(True)
                self.voltage_setpoint.setValue(
                    float(status.get("PS1SetVoltage") or 0.0)
                )
                self.current_setpoint.setValue(
                    float(status.get("PS1SetCurrent") or 0.0)
                )
                self.voltage_setpoint.blockSignals(False)
                self.current_setpoint.blockSignals(False)
        setpoint_can_edit = (
            ranges_valid
            and live
            and phase == "OFF"
            and not ps1_actual
            and setpoint_state != "pending"
            and not any_pending
            and status.get('ProtocolVersion') == 0x0102
        )
        self.voltage_setpoint.setEnabled(setpoint_can_edit)
        self.current_setpoint.setEnabled(setpoint_can_edit)
        self.apply_setpoints_button.setEnabled(setpoint_can_edit)
        self.setpoint_summary.setText(
            "设定回读："
            f"{_ps1_value(status.get('PS1SetVoltage'))} V / "
            f"{_ps1_value(status.get('PS1SetCurrent'))} A\n"
            "实际输出："
            f"{_ps1_value(status.get('PS1OutputVoltage'))} V / "
            f"{_ps1_value(status.get('PS1OutputCurrent'))} A"
            + ("\nPS1状态/测量/设定尚未确认；TCP在线不代表设备反馈有效" if not status.get('PS1FeedbackValid') else "")
        )
        if setpoint_detail == "not requested" or not setpoint_detail:
            setpoint_feedback_text = "设定反馈：尚未操作"
        else:
            setpoint_feedback_text = f"设定反馈：{setpoint_state} · {setpoint_detail}"
        self.setpoint_feedback.setText(setpoint_feedback_text)
        if not ranges_valid:
            setpoint_reason = "配置未提供有效的PS1设定范围，控制已禁用。"
        elif not live:
            setpoint_reason = "PLC通信不可用，禁止修改设定。"
        elif phase != "OFF" or ps1_actual:
            setpoint_reason = "运行中禁止修改；请先执行正常总停止。"
        elif setpoint_state == "pending" or any_pending:
            setpoint_reason = "等待PLC确认当前命令。"
        else:
            setpoint_reason = (
                f"允许范围：{voltage_min:g}–{voltage_max:g} V，"
                f"{current_min:g}–{current_max:g} A；将在总使能过程中下发并回读。"
            )
        self.setpoint_hint.setText(setpoint_reason)
        self.apply_setpoints_button.setToolTip(setpoint_reason)

        trend = snapshot.get("trend")
        trend_items = (
            [item for item in trend if isinstance(item, Mapping)]
            if isinstance(trend, list)
            else []
        )
        self.voltage_chart.set_samples(trend_items)
        self.current_chart.set_samples(trend_items)

        logs = snapshot.get("operation_log")
        log_items = (
            [item for item in logs if isinstance(item, Mapping)]
            if isinstance(logs, list)
            else []
        )
        self.logs.setRowCount(len(log_items))
        for row, entry in enumerate(reversed(log_items)):
            values = (
                str(entry.get("timestamp") or ""),
                str(entry.get("user") or ""),
                str(entry.get("field") or ""),
                _text(entry.get("old_value")),
                _text(entry.get("new_value")),
                _text(entry.get("command_sequence")),
                str(entry.get("confirmation") or ""),
            )
            for column, value in enumerate(values):
                self.logs.setItem(row, column, QTableWidgetItem(value))


__all__ = ["PlcCabinetPage"]
