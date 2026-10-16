"""Threaded PLC cabinet runtime used by the local gateway."""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from .client import PlcCommunicationError, ReadOnlyPlcClient, build_plc_client
from .config import PlcCabinetConfig, load_plc_config
from .models import CabinetPhase
from .coordination import PlcCommandCoordinator, PlcRequestConflict
from .state_machine import PlcCabinetController


class PlcControlDisabledError(RuntimeError):
    pass


class PlcCabinetService:
    def __init__(
        self,
        config: PlcCabinetConfig | None = None,
        *,
        config_path: Path | str | None = None,
        data_root: Path | str = "/var/lib/rp1-factory-hmi",
    ) -> None:
        self.config = config or load_plc_config(config_path)
        self.client = build_plc_client(self.config.driver, self.config.protocol)
        self.read_only = self.config.access_mode == "monitor"
        if self.read_only:
            self.client = ReadOnlyPlcClient(self.client)
        self.controller = PlcCabinetController(
            self.client,
            self.config.timings,
            self.config.setpoints,
        )
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_heartbeat = 0.0
        self._last_reconnect = 0.0
        self._last_status_counter: int | None = None
        max_samples = max(
            10,
            int(
                self.config.timings.trend_retention_seconds
                * 1000
                / self.config.timings.poll_ms
            ),
        )
        self._trend: deque[dict[str, Any]] = deque(maxlen=max_samples)
        self._persisted_logs = 0
        self._log_path = (
            Path(data_root).expanduser().resolve() / "plc" / "operations.jsonl"
        )

        self.coordination = PlcCommandCoordinator(self)

    def _checked(self, control: str, operation: Any) -> None:
        previous = dict(self.controller.feedback)
        if not operation():
            detail = self.controller.feedback[control].detail
            # A competing rejection belongs to its request, never to the
            # currently executing shared command. Keep its audit log entry.
            self.controller.feedback = previous
            raise PlcRequestConflict("rejected", detail)

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._try_connect("system")
            self._thread = threading.Thread(
                target=self._run,
                name="rp1-plc-cabinet",
                daemon=True,
            )
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        with self._lock:
            if self.client.connected:
                if self.read_only:
                    self.controller.disconnect()
                    return
                try:
                    self.controller.request_stop(user="system-shutdown")
                    deadline = time.monotonic() + 1.0
                    while time.monotonic() < deadline:
                        self.controller.tick()
                        if self.controller.phase.value == "OFF":
                            break
                except Exception:
                    pass
                self.controller.disconnect()
            self._persist_logs()

    def _try_connect(self, user: str) -> None:
        self.coordination.revision += 1
        try:
            if self.read_only:
                self.client.connect()
                self.controller.status = self.client.read_status()
                self.controller._ps1_feedback.reset()
                self.controller._ps1_feedback.observe(self.controller.status, time.monotonic())
                self.controller.communication_state = "LIVE"
                self.controller.phase = CabinetPhase.MONITORING
                self.controller.last_error = ""
            else:
                self.controller.connect(user=user)
        except Exception as exc:
            self.client.disconnect()
            self.controller.last_error = str(exc)
            self.controller.communication_state = "COMM_LOST"
            self.controller.phase = CabinetPhase.COMM_LOST
            self._last_reconnect = time.monotonic()

    def connect(self, *, user: str) -> dict[str, Any]:
        with self._lock:
            if not self.client.connected:
                self._try_connect(user)
            return self.snapshot()

    def disconnect(self) -> dict[str, Any]:
        with self._lock:
            if self.client.connected:
                self.controller.disconnect()
            return self.snapshot()

    def _require_control(self) -> None:
        if self.read_only:
            raise PlcControlDisabledError("PLC 处于实机只读监控模式，未启用控制操作")

    def start_sequence(
        self,
        channels: list[int],
        *,
        user: str,
        voltage: float | None = None,
        current: float | None = None,
    ) -> dict[str, Any]:
        self._require_control()
        selected = tuple(index in set(channels) for index in range(1, 5))
        with self._lock:
            self._checked("Start", lambda: self.controller.request_start(
                selected, user=user, voltage=voltage, current=current,
            ))
            return self.snapshot()

    def stop_sequence(self, *, user: str) -> dict[str, Any]:
        self._require_control()
        with self._lock:
            self._checked('Stop', lambda: self.controller.request_stop(user=user))
            return self.snapshot()

    def set_channel(self, channel: int, enabled: bool, *, user: str) -> dict[str, Any]:
        self._require_control()
        with self._lock:
            self._checked(f"ChannelEnable{channel}", lambda: self.controller.request_channel(channel, enabled, user=user))
            return self.snapshot()

    def set_ps1_setpoints(
        self,
        voltage: float,
        current: float,
        *,
        user: str,
    ) -> dict[str, Any]:
        self._require_control()
        with self._lock:
            self._checked('Setpoints', lambda: self.controller.request_setpoints(voltage, current, user=user))
            return self.snapshot()

    def reset_fault(self, *, user: str) -> dict[str, Any]:
        self._require_control()
        with self._lock:
            self._checked('ResetFaultPulse', lambda: self.controller.request_reset_fault(user=user))
            return self.snapshot()

    def all_stop(self, *, user: str) -> dict[str, Any]:
        self._require_control()
        with self._lock:
            self._checked('AllStop', lambda: self.controller.request_all_stop(user=user))
            return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            coordination = self.coordination.snapshot()
            value = self.controller.snapshot()
            value["driver"] = self.config.driver
            value["access_mode"] = self.config.access_mode
            value["read_only"] = self.read_only
            value["endpoint"] = (
                f"{self.config.protocol.get('host')}:{self.config.protocol.get('port')} / Unit {self.config.protocol.get('unit_id')}"
                if self.config.driver == "modbus_tcp" else self.config.driver
            )
            value["timings"] = {
                "heartbeat_ms": self.config.timings.heartbeat_ms,
                "poll_ms": self.config.timings.poll_ms,
                "stale_timeout_ms": self.config.timings.stale_timeout_ms,
                "command_timeout_ms": self.config.timings.command_timeout_ms,
                "km0_timeout_ms": self.config.timings.km0_timeout_ms,
                "ps1_timeout_ms": self.config.timings.ps1_timeout_ms,
                "startup_budget_ms": self.controller.startup_budget_ms,
            }
            value["dut_can_map"] = dict(self.config.dut_can_map)
            value["setpoint_limits"] = {
                "enabled": self.config.setpoints.enabled,
                "voltage_min_v": self.config.setpoints.voltage_min_v,
                "voltage_max_v": self.config.setpoints.voltage_max_v,
                "current_min_a": self.config.setpoints.current_min_a,
                "current_max_a": self.config.setpoints.current_max_a,
                "step": self.config.setpoints.step,
            }
            value["coordination"] = coordination
            value["trend"] = list(self._trend)
            return value

    def _run(self) -> None:
        poll_seconds = self.config.timings.poll_ms / 1000.0
        while not self._stop.wait(poll_seconds):
            with self._lock:
                now = time.monotonic()
                if not self.client.connected:
                    if (
                        now - self._last_reconnect
                        >= self.config.timings.reconnect_ms / 1000.0
                    ):
                        self._last_reconnect = now
                        self._try_connect("system-reconnect")
                    continue
                try:
                    if self.read_only:
                        self.controller.status = self.client.read_status()
                        self.controller._ps1_feedback.observe(self.controller.status, now)
                        self.controller.communication_state = "LIVE"
                        self.controller.phase = CabinetPhase.MONITORING
                        self.controller.last_error = ""
                    else:
                        self.controller.tick(now)
                    if not self.read_only and (
                        now - self._last_heartbeat
                        >= self.config.timings.heartbeat_ms / 1000.0
                    ):
                        self.controller.heartbeat()
                        self._last_heartbeat = now
                    self._collect_trend()
                    self._persist_logs()
                except PlcCommunicationError as exc:
                    self.controller._communication_lost(str(exc))
                    self.client.disconnect()
                except Exception as exc:
                    self.controller.last_error = str(exc)

    def _collect_trend(self) -> None:
        status = self.controller.status
        if status is None or self.controller.stale:
            return
        if status.status_counter == self._last_status_counter:
            return
        self._last_status_counter = status.status_counter
        values = self.controller.snapshot()["status"]
        self._trend.append(
            {
                "timestamp": time.time(),
                "Voltage": values["Voltage"],
                "Current": values["Current"],
                "Power": values["Power"],
                "AnalogValidMask": values["AnalogValidMask"],
            }
        )

    def _persist_logs(self) -> None:
        entries = self.controller.operation_log
        if self._persisted_logs >= len(entries):
            return
        confirmed_end = self._persisted_logs
        while (
            confirmed_end < len(entries)
            and entries[confirmed_end].confirmation.value != "pending"
        ):
            confirmed_end += 1
        if confirmed_end == self._persisted_logs:
            return
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._log_path.open("a", encoding="utf-8") as handle:
            for entry in entries[self._persisted_logs : confirmed_end]:
                handle.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
        self._persisted_logs = confirmed_end


__all__ = ["PlcCabinetService"]
