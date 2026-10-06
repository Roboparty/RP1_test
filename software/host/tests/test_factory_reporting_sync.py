from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from factory_hmi.core.identity import build_execution_code
from factory_hmi.core.report import ReportManager
from factory_hmi.sync.outbox import Outbox
from factory_hmi.sync.uploader import OperatorCredentials, UploaderWorker


class FakePlatformClient:
    def __init__(self) -> None:
        self.access_token = ""
        self.chunks: list[tuple[int, bytes]] = []
        self.remote_state = "pending_review"

    def station_login(self, **_kwargs):
        self.access_token = "station-session"
        return {"access_token": self.access_token}

    def heartbeat(self, payload):
        return dict(payload)

    def create_submission(self, payload):
        return {"id": "submission-1", "state": "reserved", **dict(payload)}

    def create_upload(self, _submission_id, payload):
        return {"id": "upload-1", **dict(payload)}

    def put_chunk(self, _upload_id, index, body):
        self.chunks.append((index, body))
        return {"chunk_index": index}

    def complete_upload(self, _upload_id):
        return {"id": "submission-1", "state": "pending_review"}

    def submission(self, _submission_id):
        return {
            "id": "submission-1",
            "state": self.remote_state,
            "review_reason": "approved by factory" if self.remote_state == "verified" else "",
        }


class ReportingAndSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.csv_path = self.root / "source.csv"
        self.csv_path.write_text(
            "\n".join(
                [
                    "time_s,joint_a_cmd_pos_rad,joint_a_pos_rad,joint_a_torque_nm,"
                    "joint_a_temp_c,joint_a_error_id",
                    "0,0.0,0.0,1.0,30.0,0",
                    "1,0.1,0.0,-2.0,31.0,0",
                    "2,0.2,0.1,3.0,32.0,0",
                    "3,0.3,0.2,-4.0,33.0,0",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        self.test_id = "upper-body-aging_2608300000_RP1.3-UPPER-01_MP-01"
        self.context = {
            "execution_uuid": "11111111-1111-4111-8111-111111111111",
            "test_id": self.test_id,
            "test_case_id": "upper-body-aging",
            "test_case_version": "1",
            "sample_id": "RP1.3-UPPER-01",
            "bench_id": "MP-01",
            "station_id": "station-a",
            "operator_id": "operator-a",
            "started_at": "2026-08-30T00:00:00Z",
            "ended_at": "2026-08-30T00:00:03Z",
            "active_seconds": 3.0,
            "completed_cycles": 2,
            "record_rate_hz": 1.0,
            "outcome": "completed",
            "entries": [
                {
                    "joint_name": "joint_a",
                    "motor_id": 1,
                    "module_name": "upper_body",
                }
            ],
            "trajectory": {"sha256": "a" * 64, "path": "/tmp/motion.npz"},
            "config": {
                "factory_hmi_acceptance": {
                    "criteria_version": "factory-v1",
                    "required_active_seconds": 3,
                    "required_cycles": 2,
                    "min_sample_completeness": 1.0,
                    "max_gap_seconds": 1.0,
                    "max_temperature_rise_c": 10.0,
                    "max_torque_p999_utilization": 1.0,
                    "max_tracking_rms_rad": 0.2,
                    "rated_torque_nm": {"joint_a": 5.0},
                }
            },
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_report_contract_metrics_and_deterministic_rebuild(self) -> None:
        manager = ReportManager(self.root / "data")
        result = manager.finalize(self.csv_path, self.context)
        schemas = Path(__file__).parents[1] / "factory_hmi" / "contracts"
        manifest_schema = json.loads(
            (schemas / "execution-bundle-v1.schema.json").read_text(encoding="utf-8")
        )
        summary_schema = json.loads(
            (schemas / "aging-summary-v1.schema.json").read_text(encoding="utf-8")
        )
        Draft202012Validator(
            manifest_schema, format_checker=FormatChecker()
        ).validate(result.manifest)
        Draft202012Validator(
            summary_schema, format_checker=FormatChecker()
        ).validate(result.summary)

        joint = result.summary["joint_metrics"][0]
        self.assertEqual(joint["temperature_rise_c"], 3.0)
        self.assertEqual(joint["torque_peak_abs_nm"], 4.0)
        self.assertGreater(joint["torque_p999_abs_nm"], 3.9)
        self.assertEqual(joint["fault_count"], 0)
        self.assertEqual(result.summary["quality"]["sample_completeness"], 1.0)
        self.assertEqual(result.summary["verdict"]["status"], "passed")

        first_digest = hashlib.sha256(result.bundle_path.read_bytes()).hexdigest()
        rebuilt = manager.rebuild(self.test_id)
        second_digest = hashlib.sha256(rebuilt.bundle_path.read_bytes()).hexdigest()
        self.assertEqual(first_digest, second_digest)

        self.assertEqual(
            build_execution_code(
                self.context["test_case_id"],
                datetime.fromisoformat(
                    self.context["started_at"].replace("Z", "+00:00")
                ),
                self.context["sample_id"],
                self.context["bench_id"],
            ),
            self.test_id,
        )

    def test_outbox_restart_resume_and_uploader_review_poll(self) -> None:
        manager = ReportManager(self.root / "data")
        result = manager.finalize(self.csv_path, self.context)
        database = self.root / "data" / "sync" / "outbox.sqlite3"
        outbox = Outbox(database)
        item = outbox.enqueue(result.manifest, result.bundle_path)
        outbox.update(item.execution_uuid, state="uploading", next_chunk_index=1)

        recovered = Outbox(database).get(item.execution_uuid)
        assert recovered is not None
        self.assertEqual(recovered.state, "sync_failed")
        Outbox(database).retry(item.execution_uuid)

        client = FakePlatformClient()
        worker = UploaderWorker(
            Outbox(database),
            client,  # type: ignore[arg-type]
            station_id="station-a",
            machine_token="m" * 64,
            chunk_size=128,
        )
        grant = worker.authenticate(
            OperatorCredentials("operator-a", "not-stored").username,
            "not-stored",
        )
        self.assertEqual(grant["access_token"], "station-session")
        self.assertEqual(worker.process_once(), 1)
        pending = Outbox(database).get(item.execution_uuid)
        assert pending is not None
        self.assertEqual(pending.state, "pending_review")
        self.assertGreater(len(client.chunks), 1)

        outbox.fail(pending.execution_uuid, "platform processing failed", base_delay_s=0)
        outbox.update(
            pending.execution_uuid,
            next_attempt_at=0,
            remote_state="failed",
        )
        uploaded_chunk_count = len(client.chunks)
        client.remote_state = "verified"
        self.assertEqual(worker.process_once(), 1)
        approved = Outbox(database).get(item.execution_uuid)
        assert approved is not None
        self.assertEqual(approved.state, "approved")
        self.assertEqual(approved.review_reason, "approved by factory")
        self.assertEqual(len(client.chunks), uploaded_chunk_count)


if __name__ == "__main__":
    unittest.main()
