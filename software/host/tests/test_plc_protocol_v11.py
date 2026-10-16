"""V1.1 receipts with in-memory PLCs only; no network or CAN operations."""
from dataclasses import replace
import uuid
import time

import pytest

from factory_hmi.plc.client import MockPlcClient
from factory_hmi.plc.config import PlcCabinetConfig
from factory_hmi.plc.faults import cabinet_fault_messages
from factory_hmi.plc.models import CabinetPhase, FaultSource, PROTOCOL_V10, ResetResult, sequence_reached
from factory_hmi.plc.service import PlcCabinetService


class DeferredResetPlc(MockPlcClient):
    """Receipt arrives first; PLC reset judgement is supplied by each test."""
    reset_sequence = 0

    def write_command(self, command):
        previous = self._status
        super().write_command(command)
        if command.reset_fault_pulse:
            self.reset_sequence = command.command_sequence & 0xFFFF
            self.set_status(
                fault_latched=previous.fault_latched, fault_code=previous.fault_code,
                fault_source_flags=previous.fault_source_flags,
                reset_ack_sequence=previous.reset_ack_sequence,
                reset_result=ResetResult.PENDING,
            )

    def complete(self, result, *, ack=None):
        changes = dict(reset_ack_sequence=self.reset_sequence if ack is None else ack,
                       reset_result=result)
        if result == ResetResult.SUCCEEDED:
            changes.update(fault_latched=False, fault_code=0,
                           fault_source_flags=self._status.fault_source_flags & ~int(FaultSource.PLC_LATCHED))
        self.set_status(**changes)


@pytest.fixture
def service(tmp_path, monkeypatch):
    client = DeferredResetPlc()
    monkeypatch.setattr("factory_hmi.plc.service.build_plc_client", lambda *args: client)
    service = PlcCabinetService(PlcCabinetConfig(driver="mock"), data_root=tmp_path)
    service.controller.connect(user="offline")
    yield service
    service.controller.disconnect()


def fault(service, **extra):
    service.client.set_status(fault_latched=True, fault_code=7, **extra)
    service.controller.tick()
    service.controller.tick()
    assert service.controller.phase == CabinetPhase.FAULT


def submit(service, operation="reset-fault"):
    seen = service.snapshot()["coordination"]
    return service.coordination.execute(
        operation, {}, user="offline", client_id="offline-window",
        request_id=str(uuid.uuid4()), expected_epoch=seen["epoch"],
        expected_revision=seen["revision"],
    )


def test_old_fault_first_frame_remains_pending_then_reset_success(service):
    fault(service)
    receipt = submit(service)
    assert receipt["request"]["state"] == "pending"
    for _ in range(3):
        service.controller.tick()
        snapshot = service.snapshot()
        assert snapshot["status"]["FaultLatched"] is True
        assert snapshot["coordination"]["active_command"]["state"] == "pending"
        assert snapshot["phase"] == "WAIT_RESET_RESULT"
    service.client.complete(ResetResult.SUCCEEDED)
    service.controller.tick()
    snapshot = service.snapshot()
    assert snapshot["coordination"]["last_command"]["state"] == "confirmed"
    assert snapshot["phase"] == "OFF"
    assert snapshot["status"]["FaultLatched"] is False


@pytest.mark.parametrize("result,state,detail", [
    (ResetResult.REJECTED_NOT_SAFE, "rejected", "KM0反馈仍在"),
    (ResetResult.FAILED_STILL_LATCHED, "failed", "故障仍锁存"),
])
def test_final_negative_results_are_reported_in_chinese(service, result, state, detail):
    fault(service)
    submit(service)
    service.client.complete(result)
    service.controller.tick()
    snapshot = service.snapshot()
    receipt = snapshot["coordination"]["last_command"]
    assert receipt["state"] == state
    assert detail in receipt["detail"]
    assert snapshot["phase"] == "FAULT"
    assert not any(snapshot["command"]["ChannelEnable"])
    assert snapshot["command"]["MainEnable"] is False


def test_success_resets_plc_only_and_keeps_ps1_faults_and_start_blocked(service):
    fault(service, ps1_fault=True, ps1_comm_fault=True, ps1_device_fault=True,
          ps1_mb_status=0xF001, ps1_device_status=0x12, fault_source_flags=7)
    submit(service)
    service.client.complete(ResetResult.SUCCEEDED)
    service.controller.tick()
    snapshot = service.snapshot()
    assert snapshot["coordination"]["last_command"]["state"] == "confirmed"
    assert snapshot["status"]["FaultLatched"] is False
    assert snapshot["status"]["FaultCode"] == 0
    assert snapshot["status"]["PS1Fault"] is True
    assert snapshot["status"]["FaultSourceFlags"] == 6
    assert snapshot["phase"] == "FAULT"
    assert not service.controller.request_start(user="offline")
    text = "；".join(cabinet_fault_messages(snapshot["status"]))
    assert "PLC↔PS1通信故障" in text and "PS1设备故障" in text
    assert "PLC控制故障已锁存" not in text


def test_final_result_requires_receipt_acceptance_and_matching_reset_sequence(service):
    fault(service)
    submit(service)
    sequence = service.controller.command.command_sequence
    service.client.complete(ResetResult.SUCCEEDED, ack=(sequence - 1) & 0xFFFF)
    service.controller.tick()
    assert service.snapshot()["coordination"]["busy"]
    service.client.complete(ResetResult.SUCCEEDED)
    service.client.set_status(acknowledged_sequence=sequence - 1)
    service.controller.tick()
    assert service.snapshot()["coordination"]["busy"]
    service.client.set_status(acknowledged_sequence=sequence + 1)
    service.controller.tick()
    assert service.snapshot()["coordination"]["last_command"]["state"] == "confirmed"


def test_pending_result_times_out_only_at_configured_deadline(service, monkeypatch):
    now = [time.monotonic()]
    read_status = service.client.read_status
    monkeypatch.setattr(service.controller, "clock", lambda: now[0])
    monkeypatch.setattr(service.client, "read_status", lambda: replace(read_status(), received_monotonic=now[0]))
    fault(service)
    submit(service)
    deadline = service.controller._pending.deadline
    now[0] = deadline - 0.001
    service.controller.tick()
    assert service.snapshot()["coordination"]["active_command"]["state"] == "pending"
    now[0] = deadline + 0.001
    service.controller.tick()
    assert service.snapshot()["coordination"]["last_command"]["state"] == "timeout"
    assert "复位结果超时" in service.controller.feedback["ResetFaultPulse"].detail


def test_sequence_wrap_and_final_ack_raw_word(service):
    service.controller.command = replace(service.controller.command, command_sequence=0xFFFF)
    service.client.set_status(acknowledged_sequence=0xFFFF)
    submit(service)
    service.client.complete(ResetResult.SUCCEEDED)
    service.controller.tick()
    snapshot = service.snapshot()
    assert service.client.reset_sequence == 0
    assert snapshot["coordination"]["last_command"]["state"] == "confirmed"
    assert sequence_reached(0, 0xFFFF)
    assert not sequence_reached(0xFFFF, 0)
    assert not sequence_reached(0x8000, 0)


def test_legacy_protocol_rejects_reset_without_any_write(service):
    service.client.set_status(protocol_version=PROTOCOL_V10)
    service.controller.tick()
    before = len(service.client.command_history)
    assert not service.controller.request_reset_fault(user="offline")
    assert len(service.client.command_history) == before
    snapshot = service.snapshot()
    assert snapshot["status"]["ResetResultSupported"] is False
    assert snapshot["status"]["ResetResult"] is None
    assert "旧协议/无复位结果回执" in snapshot["status"]["ProtocolMode"]
    assert "V1.2" in snapshot["feedback"]["ResetFaultPulse"]["detail"]


def test_all_stop_can_supersede_reset_without_replaying_pulse(service):
    fault(service)
    submit(service)
    pulses = sum(command.reset_fault_pulse for command in service.client.command_history)
    submit(service, "all-stop")
    service.controller.tick()
    assert service.snapshot()["coordination"]["commands"][-2]["state"] == "superseded"
    service.controller.heartbeat()
    assert sum(command.reset_fault_pulse for command in service.client.command_history) == pulses == 1


def test_three_fault_sources_with_zero_fault_code_are_separate(service):
    fault(service, ps1_comm_fault=True, ps1_device_fault=True, fault_source_flags=7)
    service.client.set_status(fault_code=0)
    service.controller.tick()
    messages = cabinet_fault_messages(service.snapshot()["status"])
    assert len(messages) == 3
    assert "PLC控制故障已锁存（FaultCode=0）" == messages[0]
    assert "PLC↔PS1通信故障" in messages[1]
    assert "PS1设备故障" in messages[2]


# Confirmed Windows PLC budget: a fresh image after >=100 ms; still latched
# outcome after about 2 s; NOT_SAFE completes in the command-processing scan.
PLC_RESET_FRESH_IMAGE_SECONDS = 0.100
PLC_RESET_OUTCOME_TIMEOUT_SECONDS = 2.000


def timed_reset(service, monkeypatch, result, delay):
    """Drive the host and a scheduled mock receipt without sleeping or I/O."""
    now = [time.monotonic()]
    submitted_at = [None]
    completed = [False]
    read_status = service.client.read_status
    write_command = service.client.write_command

    def complete_when_due():
        if submitted_at[0] is not None and not completed[0] and now[0] >= submitted_at[0] + delay:
            service.client.complete(result)
            completed[0] = True

    def write(command):
        write_command(command)
        if command.reset_fault_pulse:
            submitted_at[0] = now[0]
            complete_when_due()  # NOT_SAFE may finish in this command scan.

    def read():
        complete_when_due()
        return replace(read_status(), received_monotonic=now[0])

    monkeypatch.setattr(service.controller, "clock", lambda: now[0])
    monkeypatch.setattr(service.client, "write_command", write)
    monkeypatch.setattr(service.client, "read_status", read)
    fault(service)
    receipt = submit(service)
    assert receipt["request"]["state"] == "pending"
    assert service.client._status.acknowledged_sequence == service.controller.command.command_sequence
    assert service.client._status.reset_result == (ResetResult.PENDING if delay else result)
    assert service.config.timings.command_timeout_ms == 5000
    assert service.config.timings.poll_ms == 200
    assert service.controller._pending.deadline == pytest.approx(now[0] + 5.0)

    def poll_after(seconds):
        now[0] = submitted_at[0] + seconds
        service.controller.tick()
        return service.snapshot()

    return poll_after


def test_confirmed_plc_budget_requires_100ms_fresh_image_before_success(service, monkeypatch):
    poll = timed_reset(service, monkeypatch, ResetResult.SUCCEEDED, PLC_RESET_FRESH_IMAGE_SECONDS)
    for seconds in (0.0, 0.008, 0.050, 0.099):
        snapshot = poll(seconds)
        assert snapshot["status"]["FaultLatched"] is True
        assert snapshot["status"]["ResetResult"] == ResetResult.PENDING
        assert snapshot["coordination"]["active_command"]["state"] == "pending"
    snapshot = poll(0.100)
    assert snapshot["status"]["ResetResult"] == ResetResult.SUCCEEDED
    assert snapshot["status"]["FaultLatched"] is False
    assert snapshot["coordination"]["last_command"]["state"] == "confirmed"


def test_100ms_plc_result_is_observed_at_next_200ms_host_poll(service, monkeypatch):
    poll = timed_reset(service, monkeypatch, ResetResult.SUCCEEDED, PLC_RESET_FRESH_IMAGE_SECONDS)
    assert poll(0.0)["coordination"]["active_command"]["state"] == "pending"
    snapshot = poll(service.config.timings.poll_ms / 1000.0)
    assert snapshot["coordination"]["last_command"]["state"] == "confirmed"
    assert service.controller._pending is None


def test_confirmed_plc_2s_still_latched_budget_finishes_before_host_5s(service, monkeypatch):
    poll = timed_reset(service, monkeypatch, ResetResult.FAILED_STILL_LATCHED,
                       PLC_RESET_OUTCOME_TIMEOUT_SECONDS)
    for seconds in (0.0, 0.2, 1.0, 1.8, 1.999):
        snapshot = poll(seconds)
        assert snapshot["coordination"]["active_command"]["state"] == "pending"
        assert snapshot["status"]["ResetResult"] == ResetResult.PENDING
    snapshot = poll(2.0)
    assert snapshot["status"]["ResetResult"] == ResetResult.FAILED_STILL_LATCHED
    assert snapshot["status"]["FaultLatched"] is True
    assert snapshot["coordination"]["last_command"]["state"] == "failed"
    assert "故障仍锁存" in snapshot["coordination"]["last_command"]["detail"]
    assert service.controller.feedback["ResetFaultPulse"].state.value != "timeout"


def test_confirmed_not_safe_completes_in_command_scan_without_100ms_wait(service, monkeypatch):
    poll = timed_reset(service, monkeypatch, ResetResult.REJECTED_NOT_SAFE, 0.0)
    snapshot = poll(0.0)
    assert snapshot["status"]["ResetResult"] == ResetResult.REJECTED_NOT_SAFE
    assert snapshot["coordination"]["last_command"]["state"] == "rejected"
    assert "KM0反馈仍在" in snapshot["coordination"]["last_command"]["detail"]
    assert service.controller._pending is None
