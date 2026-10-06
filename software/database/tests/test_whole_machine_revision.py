from __future__ import annotations

import unittest
from pathlib import Path

from imports.whole_machine_revision import (
    DISABLED_CASE_CODE,
    REVISED_CASE_CODE,
    REVISION,
    load_revision,
)

IMPORT_ROOT = Path(__file__).resolve().parents[1] / "imports"
SOURCE_PATH = IMPORT_ROOT / "whole_machine_test_cases_v1_0.json"
REVISION_PATH = IMPORT_ROOT / "whole_machine_test_plan_r1.json"


class WholeMachineRevisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = load_revision(SOURCE_PATH, REVISION_PATH)
        self.case = self.payload["revised_case"]
        self.changes = {
            item["case_code"]: item for item in self.payload["revision"]["changes"]
        }

    def test_walking_revision_is_published_with_approved_targets(self) -> None:
        self.assertEqual(self.case["case_code"], REVISED_CASE_CODE)
        self.assertEqual(self.case["version"], REVISION)
        self.assertEqual(self.case["status"], "PUBLISHED")
        self.assertEqual(self.case["deviation_notes"], [])
        self.assertEqual(
            self.case["procedure_spec"]["matrix"]["primary_stress_or_duration"],
            "单电池连续段≤1.5 h；累计50 km及300次姿态循环",
        )
        self.assertIn(
            "2. 每5 km执行30次原地转向；累计姿态循环不少于300次。",
            self.case["procedure_spec"]["steps"],
        )
        self.assertNotIn(
            "2. 每5 km执行30次原地转向；累计姿态循环不少于400次。",
            self.case["procedure_spec"]["steps"],
        )

    def test_rain_case_is_disabled_without_erasing_source_history(self) -> None:
        self.assertEqual(
            self.changes[DISABLED_CASE_CODE]["action"],
            "DISABLE",
        )
        source_codes = {
            item["case_code"] for item in self.payload["source_catalog"]["cases"]
        }
        self.assertIn(DISABLED_CASE_CODE, source_codes)


if __name__ == "__main__":
    unittest.main()
