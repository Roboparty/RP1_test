"""Bridge boundaries, checked against the actual published service schemas."""

from __future__ import annotations

import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

import numpy as np

from factory_hmi.core.report import ReportManager
from factory_hmi.core.service import FactoryService
from factory_hmi.sync.outbox import Outbox
from factory_hmi.sync.reliability_platform import (
    CONTEXT_IDS, ReliabilityPlatformClient, ReliabilityUploaderWorker,
    bridge_manifest, build_telemetry_batches, enqueue_bundle, inspect_bundle,
)
from factory_hmi.sync.uploader import PlatformError


SERVICE_SCHEMA = Path(__file__).resolve().parents[2] / "service/app/schemas/ingestion.py"
spec = importlib.util.spec_from_file_location("bridge_service_ingestion", SERVICE_SCHEMA)
assert spec is not None and spec.loader is not None
schemas = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = schemas
spec.loader.exec_module(schemas)

TOOL_PATH = Path(__file__).resolve().parents[1] / "tools/upload_reliability_bundle.py"
tool_spec = importlib.util.spec_from_file_location("factory_bridge_tool", TOOL_PATH)
assert tool_spec is not None and tool_spec.loader is not None
tool = importlib.util.module_from_spec(tool_spec)
tool_spec.loader.exec_module(tool)


class ContractClient:
    """Idempotent service stub that uses real Pydantic request models."""

    def __init__(self, *, fail_once: bool = False) -> None:
        self.calls: list[tuple[str, dict, str]] = []
        self.seen: dict[str, dict] = {}
        self.fail_once = fail_once

    def _record(self, action, payload, key, schema):
        schema.model_validate(payload)
        if key in self.seen:
            assert self.seen[key] == payload, "idempotency payload changed"
        self.seen[key] = payload
        self.calls.append((action, payload, key))
        if action == "telemetry" and self.fail_once:
            self.fail_once = False
            raise PlatformError("temporary transport failure")
        return {}

    def register_execution(self, payload, key):
        self._record("register", payload, key, schemas.ExecutionRegisterRequest)
        return {"execution_id": "00000000-0000-7000-8000-000000000111", "stage_id": "00000000-0000-7000-8000-000000000112"}

    def telemetry(self, _producer, payload, key):
        return self._record("telemetry", payload, key, schemas.TelemetryBatchRequest)

    def events(self, _producer, payload, key):
        return self._record("events", payload, key, schemas.EventBatchRequest)

    def artifact(self, _producer, payload, key):
        return self._record("artifact", payload, key, schemas.ArtifactMetadataRequest)

    def finish(self, _producer, payload, key):
        return self._record("finish", payload, key, schemas.FinishExecutionRequest)


class ReliabilityBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.csv_path = self.root / "telemetry.csv"
        self.csv_path.write_text(
            "time_s,hip_cmd_pos_rad,hip_pos_rad,hip_spd_rad_s,hip_torque_nm,hip_temp_c,hip_error_id\n"
            "0,0,0.01,0.1,2,30,0\n1,0.1,0.11,0.1,2,30.1,0\n2,0.2,0.21,0.1,2,30.2,0\n",
            encoding="utf-8",
        )
        self.context = {name: f"00000000-0000-7000-8000-{index + 1:012d}" for index, name in enumerate(CONTEXT_IDS)}
        self.context.update(api_key="SECRET-MUST-NOT-BE-PERSISTED", source_kind="synthetic")
        self.record_context = {
            "execution_uuid": "11111111-1111-4111-8111-111111111111", "test_id": "test-only-bridge-run",
            "test_case_id": "test-only-aging", "sample_id": "TEST-ONLY-NO-HARDWARE", "bench_id": "TEST-ONLY",
            "started_at": "2026-10-06T00:00:00Z", "ended_at": "2026-10-06T00:00:02Z",
            "active_seconds": 2, "completed_cycles": 1, "record_rate_hz": 1,
            "outcome": "completed", "entries": [{"joint_name": "hip", "motor_id": 1, "module_name": "test-only"}],
            "config": {"factory_hmi_acceptance": {
                "criteria_version": "test-only-criteria", "required_active_seconds": 2,
                "required_cycles": 1, "min_sample_completeness": 1, "max_gap_seconds": 1,
                "max_temperature_rise_c": 10, "max_torque_p999_utilization": 1,
                "max_tracking_rms_rad": 1, "rated_torque_nm": {"hip": 10},
            }},
        }
        self.report = ReportManager(self.root / "reports").finalize(self.csv_path, self.record_context)
        self.bundle = inspect_bundle(self.report.bundle_path)
        self.manifest = bridge_manifest(self.bundle, self.context, {"hip": "SYS-HIP-PITCH-R"})
        self.outbox = Outbox(self.root / "bridge.sqlite3")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _rewrite_tar(self, transform) -> Path:
        target = self.root / "modified.tar.gz"
        with tarfile.open(self.bundle.path, "r:gz") as source, tarfile.open(target, "w:gz") as destination:
            for member in source:
                stream = source.extractfile(member)
                assert stream is not None
                data = stream.read()
                member, data = transform(member, data)
                destination.addfile(member, io.BytesIO(data))
        return target

    def test_original_passed_report_is_ingested_as_synthetic_inconclusive(self) -> None:
        self.assertEqual(self.bundle.manifest["status"], "passed")
        self.assertEqual(self.manifest["status"], "blocked")
        self.assertNotIn("SECRET", json.dumps(self.manifest))
        enqueue_bundle(self.outbox, self.bundle, self.manifest)
        client = ContractClient()
        self.assertEqual(ReliabilityUploaderWorker(self.outbox, client).process_once(), 1)
        actions = [action for action, _payload, _key in client.calls]
        self.assertEqual(actions, ["register", "telemetry", "events", "artifact", "finish"])
        registration = client.calls[0][1]
        self.assertEqual(registration["source_metadata"]["source_kind"], "synthetic")
        finish = client.calls[-1][1]
        self.assertEqual((finish["source_status"], finish["outcome"]), ("blocked", "INCONCLUSIVE"))
        self.assertEqual(finish["active_seconds"], 2)
        artifact = client.calls[-2][1]
        self.assertEqual(artifact["availability_status"], "MISSING")
        self.assertEqual(artifact["source_location"], "retained on HMI workstation")
        self.assertNotIn("SECRET", json.dumps(client.calls))
        self.assertEqual(self.outbox.get(self.manifest["execution_uuid"]).remote_state, "INGESTED")

    def test_nan_missing_fields_and_invalid_times_are_not_filled_with_zero(self) -> None:
        rows = [
            {"time_s": "0", "hip_cmd_pos_rad": "0", "hip_pos_rad": "0.1", "hip_temp_c": "NaN", "hip_spd_rad_s": "0.2"},
            {"time_s": "1", "hip_cmd_pos_rad": "0.2", "hip_pos_rad": "", "hip_temp_c": "31", "hip_spd_rad_s": "inf"},
            {"time_s": "NaN", "hip_pos_rad": "9", "hip_temp_c": "0"},
            {"time_s": "0.5", "hip_pos_rad": "0.15", "hip_temp_c": "30"},
        ]
        batches = build_telemetry_batches(rows, manifest=self.manifest, stage_id=self.context["station_id"], producer_key=self.manifest["execution_uuid"])
        schemas.TelemetryBatchRequest.model_validate(batches[0])
        series = {item["metric_code"]: item for item in batches[0]["series"]}
        self.assertNotIn("JOINT-TORQUE", series)
        self.assertEqual([point["value"] for point in series["JOINT-TEMPERATURE"]["points"]], [31])
        self.assertAlmostEqual(series["JOINT-TRACKING-ERROR"]["points"][0]["value"], 5.729577951)
        self.assertAlmostEqual(series["JOINT-VELOCITY"]["points"][0]["value"], 11.459155902)
        self.assertEqual(series["JOINT-TEMPERATURE"]["quality_status"], "PARTIAL")

    def test_long_series_is_bounded_by_real_service_contract(self) -> None:
        manifest = {**self.manifest, "ended_at": "2026-10-06T00:02:00Z"}
        rows = [{"time_s": str(index / 100), "hip_pos_rad": str(index / 10000)} for index in range(10001)]
        batch = build_telemetry_batches(rows, manifest=manifest, stage_id=self.context["station_id"], producer_key=self.manifest["execution_uuid"])[0]
        validated = schemas.TelemetryBatchRequest.model_validate(batch)
        self.assertLessEqual(len(validated.series[0].points), 5000)
        self.assertEqual(validated.series[0].points[0].sample_index, 0)

    def test_interrupted_upload_replays_identical_keys_in_order(self) -> None:
        enqueue_bundle(self.outbox, self.bundle, self.manifest)
        client = ContractClient(fail_once=True)
        self.assertEqual(ReliabilityUploaderWorker(self.outbox, client).process_once(), 0)
        self.assertEqual(self.outbox.get(self.manifest["execution_uuid"]).state, "sync_failed")
        recovered = Outbox(self.outbox.path)
        recovered.retry(self.manifest["execution_uuid"])
        self.assertEqual(ReliabilityUploaderWorker(recovered, client).process_once(), 1)
        self.assertEqual(client.calls[0], client.calls[2])
        self.assertEqual(client.calls[1], client.calls[3])
        self.assertEqual(client.calls[-1][0], "finish")

    def test_completed_duplicate_is_not_resubmitted_and_context_is_immutable(self) -> None:
        enqueue_bundle(self.outbox, self.bundle, self.manifest)
        client = ContractClient()
        worker = ReliabilityUploaderWorker(self.outbox, client)
        worker.process_once()
        before = len(client.calls)
        enqueue_bundle(self.outbox, self.bundle, self.manifest)
        self.assertEqual(worker.process_once(), 0)
        self.assertEqual(len(client.calls), before)
        with self.assertRaisesRegex(ValueError, "different bundle or context"):
            enqueue_bundle(self.outbox, self.bundle, {**self.manifest, "subject_map": {"hip": "ANOTHER-SUBJECT"}})

    def test_mutated_bundle_stops_before_network_requests(self) -> None:
        enqueue_bundle(self.outbox, self.bundle, self.manifest)
        with self.bundle.path.open("ab") as handle:
            handle.write(b"changed-after-enqueue")
        client = ContractClient()
        self.assertEqual(ReliabilityUploaderWorker(self.outbox, client).process_once(), 0)
        self.assertEqual(client.calls, [])
        self.assertEqual(self.outbox.get(self.manifest["execution_uuid"]).state, "sync_failed")

    def test_archive_path_traversal_and_symbolic_links_are_rejected(self) -> None:
        def traversal(member, data):
            if member.name == "analysis/report.md":
                member.name = "../report.md"
            return member, data
        with self.assertRaisesRegex(ValueError, "unsafe archive path"):
            inspect_bundle(self._rewrite_tar(traversal))
        def symlink(member, data):
            if member.name == "analysis/report.md":
                member.type, member.linkname, member.size = tarfile.SYMTYPE, "/etc/passwd", 0
                data = b""
            return member, data
        with self.assertRaisesRegex(ValueError, "non-regular files"):
            inspect_bundle(self._rewrite_tar(symlink))

    def test_file_inventory_hash_and_size_are_checked(self) -> None:
        def change(member, data):
            if member.name == "analysis/report.md":
                data = b"x" * member.size
            return member, data
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            inspect_bundle(self._rewrite_tar(change))
        def invalid_size(member, data):
            if member.name == "manifest.json":
                manifest = json.loads(data)
                manifest["files"][0]["size_bytes"] += 1
                data = json.dumps(manifest).encode()
                member.size = len(data)
            return member, data
        with self.assertRaisesRegex(ValueError, "size, type or hash"):
            inspect_bundle(self._rewrite_tar(invalid_size))

    def test_real_requires_explicit_physical_evidence_and_rejects_fake_markers(self) -> None:
        with self.assertRaises(ValueError):
            bridge_manifest(self.bundle, {**self.context, "source_kind": "real"}, {"hip": "SYS-HIP-PITCH-R"}, source_kind="real")
        evidence = {"backend": "motors_py", "measurement_origin": "physical_robot", "recording_reference": "test-only-attestation", "confirmed_by": "test-only-operator"}
        with self.assertRaisesRegex(ValueError, "synthetic database context"):
            bridge_manifest(self.bundle, self.context, {"hip": "SYS-HIP-PITCH-R"}, source_kind="real", evidence=evidence)
        record_context = {**self.record_context, "config": {**self.record_context["config"], "backend": "fake"}}
        fake_bundle = inspect_bundle(ReportManager(self.root / "fake").finalize(self.csv_path, record_context).bundle_path)
        with self.assertRaisesRegex(ValueError, "synthetic/fake bundle"):
            bridge_manifest(fake_bundle, {**self.context, "source_kind": "real"}, {"hip": "SYS-HIP-PITCH-R"}, source_kind="real", evidence=evidence)

    def test_missing_acceptance_rules_cannot_be_ingested_as_passed(self) -> None:
        context = {**self.record_context, "config": {}}
        bundle = inspect_bundle(ReportManager(self.root / "unconfigured").finalize(self.csv_path, context).bundle_path)
        self.assertEqual(bundle.manifest["status"], "passed")
        evidence = {"backend": "motors_py", "measurement_origin": "physical_robot", "recording_reference": "test-only-attestation", "confirmed_by": "test-only-operator"}
        manifest = bridge_manifest(bundle, {**self.context, "source_kind": "real"}, {"hip": "SYS-HIP-PITCH-R"}, source_kind="real", evidence=evidence)
        self.assertFalse(manifest["criteria_complete"])
        self.assertEqual(manifest["status"], "blocked")
        enqueue_bundle(self.outbox, bundle, manifest)
        client = ContractClient()
        self.assertEqual(ReliabilityUploaderWorker(self.outbox, client).process_once(), 1)
        self.assertEqual(client.calls[-1][1]["outcome"], "INCONCLUSIVE")

    def test_fake_service_records_origin_and_cannot_be_relabeled_real(self) -> None:
        host_root = Path(__file__).resolve().parents[1]
        trajectory = self.root / "fake-trajectory.npz"
        np.savez(
            trajectory, motor_pos=np.zeros((3, 7)), time=np.array([0, 0.1, 0.2]),
            fps=np.array([10.0]), limb=np.array(["right_arm"]),
        )
        with patch.dict(os.environ, {"INFLUXDB_TOKEN": "", "TEST_PLATFORM_API_URL": ""}):
            service = FactoryService(data_root=self.root / "actual-fake-service")
            service.configure(config_path=str(host_root / "scripts/config/right_arm_motors.yaml"), limb="right_arm", backend="fake")
            service.can_connect()
            service.discover_motors()
            service.enable_motors(physical_estop_confirmed=True)
            try:
                service.import_trajectory(str(trajectory))
                service.arm()
                service.start(loop=False, cycles=1, record_rate_hz=20, robot_id="RP1.3-UPPER-01", bench_id="MP-01")
                self.assertTrue(service.controller.wait(3))
                deadline = time.monotonic() + 3
                while service._report_threads and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertFalse(service._report_errors)
                history = service.report_manager.history()
                self.assertEqual(len(history), 1)
                bundle = inspect_bundle(history[0]["bundle_path"])
            finally:
                service.disable_motors()
                service.disconnect()
                service.can_disconnect()
        self.assertTrue(bundle.recording_is_synthetic)
        evidence = {"backend": "motors_py", "measurement_origin": "physical_robot", "recording_reference": "test-only-attestation", "confirmed_by": "test-only-operator"}
        with self.assertRaisesRegex(ValueError, "synthetic/fake bundle"):
            bridge_manifest(bundle, {**self.context, "source_kind": "real"}, tool.RIGHT_ARM_SUBJECTS, source_kind="real", evidence=evidence)

    def test_private_context_and_key_env_file_are_required(self) -> None:
        path = self.root / "context.json"
        path.write_text(json.dumps(self.context), encoding="utf-8")
        path.chmod(0o644)
        with self.assertRaises(PermissionError):
            tool.read_mapping(path, private=True)
        path.chmod(0o600)
        self.assertEqual(tool.read_credentials(tool.read_mapping(path, private=True), None), "SECRET-MUST-NOT-BE-PERSISTED")
        env = self.root / "key.env"
        env.write_text("RP1_RELIABILITY_SERVICE_KEY='ENV-KEY'\n", encoding="utf-8")
        env.chmod(0o600)
        self.assertEqual(tool.read_credentials({}, env), "ENV-KEY")

    def test_demo_uses_real_report_manager_and_is_marked_synthetic(self) -> None:
        with patch("sys.stdout", new_callable=io.StringIO):
            path = tool.generate_demo(self.root / "demo", duration=2, rate=10)
        bundle = inspect_bundle(path)
        self.assertTrue(bundle.recording_is_synthetic)
        self.assertEqual(len(bundle.summary["joint_metrics"]), 7)
        self.assertEqual(bundle.summary["timing"]["active_seconds"], 2)
        self.assertEqual(bundle.manifest["status"], "blocked")

    def test_client_sanitizes_remote_errors_and_rejects_url_credentials(self) -> None:
        with self.assertRaises(ValueError):
            ReliabilityPlatformClient("http://user:SECRET@example.test", "key")
        client = ReliabilityPlatformClient("http://example.test", "key")
        from urllib.error import HTTPError
        failure = HTTPError("http://example.test", 403, "SECRET-REMOTE", {}, io.BytesIO(b"SECRET-BODY"))
        with patch("factory_hmi.sync.reliability_platform.request.urlopen", side_effect=failure):
            with self.assertRaisesRegex(PlatformError, "HTTP 403") as captured:
                client.register_execution({}, "key")
        self.assertNotIn("SECRET", str(captured.exception))


if __name__ == "__main__":
    unittest.main()
