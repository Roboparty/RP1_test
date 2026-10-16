from __future__ import annotations

import csv
import unittest
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
import tempfile

from imports.execution_csv import (
    EXPECTED_HEADERS,
    EXPECTED_ROW_COUNT,
    conservative_active_seconds,
    load_csv,
    normalize_asset,
    normalize_status,
    validate_rows,
)


def source_rows() -> list[dict[str, str]]:
    """Independent synthetic contract fixture; never read workstation history."""
    statuses = ["passed"] * 89 + ["failed"] * 17 + ["blocked"] * 18 + ["scheduled"] * 9 + ["running"] * 2
    benches = ["RD-01"] * 30 + ["RD-02"] * 72 + ["RD-03"] * 33
    cases = (["REL-SARM-001"] * 6 + ["REL-SARM-002"] * 18 + ["REL-SLEG-001"] * 5
             + ["REL-SLEG-002"] * 9 + ["REL-SLEG-010"] * 6 + ["REL-UPPER-001"] * 43
             + ["REL-LOWER-001"] * 32 + ["REL-LOWER-002"] * 11 + ["PERF-SARM-001"] * 5)
    result = []
    for index, (status, bench, case) in enumerate(zip(statuses, benches, cases, strict=True)):
        started = datetime.fromisoformat("2026-09-01T10:00:00+08:00") + timedelta(minutes=index)
        final = status not in {"scheduled", "running"}
        ended = started + timedelta(hours=1) if final else started
        sample = f"RP1.3-{case.split('-')[1]}-001"
        row = dict.fromkeys(EXPECTED_HEADERS, "")
        row.update(test_id=f"{case}_{started:%y%m%d%H%M}_{sample}_{bench}", test_case_id=case,
                   test_case_name="Synthetic contract test", sample_id=sample, robot_id=sample,
                   bench_id=bench, status=status, started_at=started.isoformat(),
                   ended_at=ended.isoformat() if final else "", last_data_at=ended.isoformat(),
                   duration_hours="1" if final else "0", exception_count="0",
                   created_at=started.isoformat(), updated_at=ended.isoformat())
        result.append(row)
    return result


class ExecutionCsvTests(unittest.TestCase):
    def test_synthetic_dataset_and_normalization_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.csv"
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=EXPECTED_HEADERS)
                writer.writeheader()
                writer.writerows(source_rows())
            rows = load_csv(path)
        warning_counts = Counter(
            warning["code"]
            for row in rows
            for warning in row["normalization_warnings"]
        )

        self.assertEqual(len(rows), EXPECTED_ROW_COUNT)
        self.assertEqual(
            {row["asset_code"] for row in rows},
            {
                "RP1.3-SARM-001",
                "RP1.3-SLEG-001",
                "RP1.3-UPPER-001",
                "RP1.3-LOWER-001",
            },
        )
        self.assertEqual(warning_counts["asset_code_normalized"], 0)
        self.assertEqual(warning_counts["case_part_overrode_source_part"], 0)
        self.assertEqual(warning_counts["robot_sample_sequence_conflict"], 0)
        self.assertEqual(warning_counts["reported_duration_mismatch"], 0)
        self.assertEqual(
            sum(row["active_seconds"] is not None for row in rows),
            124,
        )

    def test_case_controls_part_and_sample_controls_sequence(self) -> None:
        asset_code, warnings = normalize_asset(
            "REL-UPPER-001",
            "RP1.3-LOWER-01",
            "RP1.3-LEG-02",
        )
        self.assertEqual(asset_code, "RP1.3-UPPER-001")
        self.assertEqual(
            {warning["code"] for warning in warnings},
            {
                "case_part_overrode_source_part",
                "asset_code_normalized",
                "robot_sample_sequence_conflict",
            },
        )

    def test_direct_status_mapping(self) -> None:
        self.assertEqual(
            normalize_status("scheduled", "", ""),
            {
                "execution_status": "SCHEDULED",
                "outcome": "NOT_EVALUATED",
                "termination_kind": "SCHEDULED",
                "source_status_conflict": False,
            },
        )
        self.assertEqual(
            normalize_status("running", "", "")["termination_kind"],
            "RUNNING",
        )
        self.assertEqual(
            normalize_status("failed", "ordinary failure", "")["outcome"],
            "FAILED",
        )
        self.assertEqual(
            normalize_status("blocked", "interrupted", "")["outcome"],
            "INCONCLUSIVE",
        )
        passed = normalize_status("passed", "通过（长时老化无故障）", "")
        self.assertEqual(
            (
                passed["execution_status"],
                passed["outcome"],
                passed["termination_kind"],
            ),
            ("COMPLETED", "PASSED", "NORMAL"),
        )

    def test_passed_semantic_contradictions_are_not_marked_passed(self) -> None:
        watchdog = normalize_status(
            "passed",
            "测试被安全监控终止：[watchdog] feedback stale",
            "",
        )
        timeout = normalize_status(
            "passed",
            "采集端超时无数据，服务端自动收口",
            "",
        )
        crash = normalize_status("passed", "工控机卡死强制关机", "")
        manual = normalize_status("passed", "测试收到 Ctrl+C 后中断", "")

        self.assertEqual(
            (
                watchdog["execution_status"],
                watchdog["outcome"],
                watchdog["termination_kind"],
            ),
            ("BLOCKED", "INCONCLUSIVE", "SAFETY_WATCHDOG"),
        )
        self.assertEqual(timeout["termination_kind"], "DATA_TIMEOUT")
        self.assertEqual(crash["termination_kind"], "SYSTEM_CRASH")
        self.assertEqual(
            (
                manual["execution_status"],
                manual["outcome"],
                manual["termination_kind"],
            ),
            ("COMPLETED", "INCONCLUSIVE", "MANUAL_STOP"),
        )
        self.assertTrue(
            all(
                normalized["source_status_conflict"]
                for normalized in (watchdog, timeout, crash, manual)
            )
        )

    def test_conservative_runtime_uses_minimum_and_flags_disagreement(self) -> None:
        started_at = datetime.fromisoformat("2026-09-01T10:00:00+08:00")
        ended_at = datetime.fromisoformat("2026-09-01T12:00:00+08:00")
        last_data_at = datetime.fromisoformat("2026-09-01T10:05:00+08:00")
        active_seconds, quality, warnings = conservative_active_seconds(
            Decimal("7200"),
            started_at,
            ended_at,
            last_data_at,
        )
        self.assertEqual(active_seconds, Decimal("300.0"))
        self.assertEqual(quality, "PARTIAL")
        self.assertIn(
            "last_data_window_shorter_than_elapsed",
            {warning["code"] for warning in warnings},
        )

    def test_headers_row_count_duplicates_and_timezone_fail_closed(self) -> None:
        rows = source_rows()
        with self.assertRaisesRegex(ValueError, "unexpected execution CSV headers"):
            validate_rows(EXPECTED_HEADERS[:-1], rows)
        with self.assertRaisesRegex(ValueError, "expected exactly 135"):
            validate_rows(EXPECTED_HEADERS, rows[:-1])

        duplicate_rows = [dict(row) for row in rows]
        duplicate_rows[1]["test_id"] = duplicate_rows[0]["test_id"]
        with self.assertRaisesRegex(ValueError, "duplicate test_id"):
            validate_rows(EXPECTED_HEADERS, duplicate_rows)

        naive_time_rows = [dict(row) for row in rows]
        naive_time_rows[0]["started_at"] = "2026-06-25T10:00:00"
        with self.assertRaisesRegex(ValueError, "must include a timezone"):
            validate_rows(EXPECTED_HEADERS, naive_time_rows)

    def test_status_bench_and_case_formats_fail_closed(self) -> None:
        rows = source_rows()

        bad_status = [dict(row) for row in rows]
        bad_status[0]["status"] = "complete"
        with self.assertRaisesRegex(ValueError, "unexpected status"):
            validate_rows(EXPECTED_HEADERS, bad_status)

        bad_bench = [dict(row) for row in rows]
        bad_bench[0]["bench_id"] = "RD-99"
        with self.assertRaisesRegex(ValueError, "unexpected bench"):
            validate_rows(EXPECTED_HEADERS, bad_bench)

        bad_case = [dict(row) for row in rows]
        bad_case[0]["test_case_id"] = "REL-HAND-001"
        with self.assertRaisesRegex(ValueError, "invalid execution test case format"):
            validate_rows(EXPECTED_HEADERS, bad_case)


if __name__ == "__main__":
    unittest.main()
