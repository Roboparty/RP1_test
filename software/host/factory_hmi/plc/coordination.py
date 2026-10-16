"""Coordinate every window through the cabinet service's one command lock.

Normal commands are never queued or replayed after a conflicting operation.
The caller must have observed the current control revision. AllStop deliberately
bypasses that revision and may supersede the active transaction.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import suppress
from copy import deepcopy
import json
import time
from typing import Any, TYPE_CHECKING
import uuid

from .client import PlcCommunicationError
from .models import CabinetPhase

if TYPE_CHECKING:
    from .service import PlcCabinetService


class PlcRequestConflict(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PlcCommandCoordinator:
    def __init__(self, service: PlcCabinetService) -> None:
        self.service = service
        self.epoch = str(uuid.uuid4())
        self.revision = 0
        self._last_sequence = 0
        self._snapshot_sequence = 0
        self._requests: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
        self._active: dict[str, Any] | None = None
        self._last: dict[str, Any] | None = None

    def _busy(self) -> bool:
        controller = self.service.controller
        return (
            controller._pending is not None
            or controller.phase.value.startswith(("START_", "WAIT_", "STOP_"))
            or self._active is not None and self._active["state"] == "pending"
        )

    def _audit(self, record: dict[str, Any]) -> None:
        # Operational metadata stays on the workstation. No acquisition data.
        path = self.service._log_path.with_name("requests.jsonl")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(self._public(record), ensure_ascii=False) + "\n")
        except OSError:
            # A log failure must never block a stop request or repeat a write.
            pass

    @staticmethod
    def _public(record: dict[str, Any]) -> dict[str, Any]:
        return {key: deepcopy(value) for key, value in record.items() if not key.startswith("_")}

    def _finish(self, record: dict[str, Any], state: str, detail: str) -> None:
        record.update(state=state, detail=detail, completed_at=time.time())
        self._audit(record)
        if record is self._active:
            self._active = None

    def _resolve(self) -> None:
        sequence = self.service.controller.command.command_sequence
        if sequence != self._last_sequence:
            self._last_sequence = sequence
            self.revision += 1
        record = self._active
        if record is None:
            return
        controller = self.service.controller
        operation = record["operation"]
        control = {
            "start": "Start", "stop": "Stop", "all-stop": "AllStop",
            "setpoints": "Setpoints", "reset-fault": "ResetFaultPulse",
            "channel": "ChannelEnable",
        }.get(operation, "")
        feedback = controller.feedback.get(control)
        if feedback is not None and feedback.state.value in {"rejected", "timeout", "failed"}:
            self._finish(record, feedback.state.value, feedback.detail)
            return
        if controller.communication_state != "LIVE" or controller.stale:
            self._finish(record, "failed", controller.last_error or "PLC 通信中断或状态过期")
            return
        if controller.phase == CabinetPhase.FAULT and operation not in {"all-stop", "reset-fault"}:
            self._finish(record, "failed", controller.last_error or "PLC 进入故障状态")
            return
        confirmed = feedback is not None and feedback.state.value == "confirmed"
        status = controller.status
        if operation == "start":
            confirmed = confirmed and controller.phase == CabinetPhase.RUNNING
        elif operation == "stop":
            confirmed = confirmed and controller.phase == CabinetPhase.OFF
        elif operation == "channel":
            confirmed = (
                confirmed and status is not None
                and controller.phase == CabinetPhase.RUNNING
                and status.channel_permit[record['_channel'] - 1] == record['_enabled']
            )
        elif operation == "all-stop":
            confirmed = (
                confirmed and status is not None
                and not any(status.channel_permit)
                and not status.ps1_actual_output and not status.main_ready
                and status.main_contactor_fb is not True
            )
        if confirmed:
            self._finish(record, "confirmed", feedback.detail)
        elif time.monotonic() >= record["_deadline"]:
            self._finish(record, "timeout", "PLC 实际状态未在规定时间内确认")
            if operation == "channel":
                controller._enter_fault("channel output confirmation timeout", "system")

    def snapshot(self) -> dict[str, Any]:
        # The caller holds the service lock, including while serializing a GET.
        self._resolve()
        self._snapshot_sequence += 1
        return {
            "epoch": self.epoch,
            "revision": self.revision,
            "snapshot_sequence": self._snapshot_sequence,
            "busy": self._busy(),
            "active_command": self._public(self._active) if self._active else None,
            "last_command": self._public(self._last) if self._last else None,
            "commands": [self._public(r) for r in self._requests.values() if r.get("accepted")][-20:],
        }

    def execute(
        self, operation: str, payload: dict[str, Any], *, user: str,
        client_id: str | None, request_id: str | None,
        expected_epoch: str | None, expected_revision: int | None,
    ) -> dict[str, Any]:
        service = self.service
        with service._lock:
            if operation not in {"connect", "disconnect"}:
                service._require_control()
            self._resolve()
            if not client_id or not request_id:
                raise PlcRequestConflict("missing_context", "缺少窗口或请求标识，请更新上位机并刷新 PLC 页面")
            key = (client_id, request_id)
            signature = json.dumps([operation, payload, user], sort_keys=True)
            previous = self._requests.get(key)
            if previous is not None:
                if previous["_signature"] != signature:
                    raise PlcRequestConflict("request_id_reused", "同一请求标识不能用于不同 PLC 操作")
                if not previous["accepted"]:
                    raise PlcRequestConflict(previous["_code"], previous["detail"])
                return {"ok": True, "result": service.snapshot(), "request": self._public(previous), "replayed": True}
            record = dict(
                request_id=request_id, client_id=client_id, user=user,
                operation=operation, state="pending", accepted=False,
                submitted_at=time.time(), detail="等待 PLC 确认", _signature=signature,
            )
            self._requests[key] = record
            while len(self._requests) > 256:
                oldest = next(iter(self._requests))
                if self._requests[oldest] is self._active:
                    self._requests.move_to_end(oldest)
                else:
                    self._requests.pop(oldest)

            def deny(code: str, detail: str) -> None:
                record.update(_code=code)
                self._finish(record, "rejected", detail)
                raise PlcRequestConflict(code, detail)

            if operation != "all-stop":
                if expected_epoch != self.epoch or expected_revision != self.revision:
                    deny("stale_revision", "其他窗口已改变 PLC 控制状态；已同步最新状态，请核对后重新操作")
                if self._busy():
                    deny("busy", "PLC 正在执行共享指令，请等待确认；需要中止时可使用全部停止")
            old_active = self._active
            try:
                if operation == "connect":
                    service.connect(user=user)
                    if service.controller.communication_state != "LIVE":
                        deny("not_live", service.controller.last_error or "PLC 连接失败")
                elif operation == "disconnect":
                    if service.controller.phase not in {CabinetPhase.OFF, CabinetPhase.DISCONNECTED, CabinetPhase.COMM_LOST, CabinetPhase.MONITORING}:
                        deny("not_off", "请先停止 PLC 供电，再断开连接")
                    service.disconnect()
                elif operation == "start":
                    service.start_sequence(user=user, **payload)
                elif operation == "stop":
                    service.stop_sequence(user=user)
                elif operation == "channel":
                    service.set_channel(user=user, **payload)
                    record.update(channel=payload["channel"], enabled=payload["enabled"],
                                  _channel=payload["channel"], _enabled=payload["enabled"])
                elif operation == "setpoints":
                    service.set_ps1_setpoints(user=user, **payload)
                elif operation == "reset-fault":
                    service.reset_fault(user=user)
                elif operation == "all-stop":
                    service.all_stop(user=user)
                else:
                    deny("unknown_operation", "未知 PLC 操作")
            except PlcRequestConflict as exc:
                if record["state"] == "pending":
                    deny(exc.code, str(exc))
                raise
            except PlcCommunicationError as exc:
                # A write may have reached the PLC despite a lost reply. Never
                # retry it automatically; invalidate the observed context.
                self.revision += 1
                service.controller._communication_lost(str(exc))
                service.client.disconnect()
                deny("communication_error", "PLC 通信失败，执行结果未确认；请检查回读状态")
            except Exception as exc:
                # Retain an uncertain result so retries of the same ID cannot
                # execute a possibly completed command a second time.
                self.revision += 1
                service.controller._communication_lost(str(exc))
                with suppress(Exception):
                    service.client.disconnect()
                deny("unconfirmed", "PLC 指令执行结果未确认；请刷新并检查连接")
            if operation == "all-stop" and old_active is not None:
                self._finish(old_active, "superseded", f"被窗口 {client_id} 的全部停止中止")
            self.revision += 1
            self._last_sequence = service.controller.command.command_sequence
            record.update(accepted=True, revision=self.revision)
            timings = service.config.timings
            record["_deadline"] = time.monotonic() + (
                service.controller.startup_budget_ms if operation == "start" else
                6 * timings.command_timeout_ms + timings.km0_timeout_ms + 4 * timings.ps1_timeout_ms
                if operation == "stop" else timings.command_timeout_ms
            ) / 1000.0
            self._last = record
            self._active = record
            self._audit(record)
            if operation in {"connect", "disconnect"}:
                self._finish(record, "confirmed", "连接状态已更新")
            result = service.snapshot()
            return {"ok": True, "result": result, "request": self._public(record), "replayed": False}
