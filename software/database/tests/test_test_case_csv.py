from __future__ import annotations

import csv
import io
import unittest
from pathlib import Path

from imports.test_case_csv import EXPECTED_COUNTS, EXPECTED_HEADERS, load_csv, validate_rows


CSV_PATH = Path(__file__).resolve().parents[1] / "imports" / "test_cases_202609181013.csv"


class TestCaseCsvTests(unittest.TestCase):
    def test_catalog_has_exact_expected_rows_and_groups(self) -> None:
        rows = load_csv(CSV_PATH)
        counts = {
            prefix: sum(row["case_code"].startswith(f"{prefix}-") for row in rows)
            for prefix in EXPECTED_COUNTS
        }
        self.assertEqual(len(rows), 60)
        self.assertEqual(counts, EXPECTED_COUNTS)
        self.assertEqual(
            {row["target_part_code"] for row in rows},
            {"SYS", "SARM", "SLEG", "UPPER", "LOWER", "CHEST"},
        )

    def test_unknown_prefix_fails_closed(self) -> None:
        row = next(csv.DictReader(io.StringIO(CSV_PATH.read_text(encoding="utf-8"))))
        row["id"] = "REL-HAND-001"
        with self.assertRaisesRegex(ValueError, "unknown or ambiguous"):
            validate_rows(EXPECTED_HEADERS, [row] * 60)

    def test_duplicate_id_is_rejected(self) -> None:
        with CSV_PATH.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        rows[1]["id"] = rows[0]["id"]
        with self.assertRaisesRegex(ValueError, "duplicate id"):
            validate_rows(EXPECTED_HEADERS, rows)


if __name__ == "__main__":
    unittest.main()
