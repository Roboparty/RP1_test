from __future__ import annotations

import unittest
from pathlib import Path

from imports.whole_machine_catalog import (
    EXPECTED_DRAFT_CODES,
    EXPECTED_SOURCE_SHA256,
    VERSION,
    load_catalog,
)

CATALOG_PATH = (
    Path(__file__).resolve().parents[1]
    / "imports"
    / "whole_machine_test_cases_v1_0.json"
)


class WholeMachineCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = load_catalog(CATALOG_PATH)
        self.by_code = {item["case_code"]: item for item in self.payload["cases"]}

    def test_catalog_preserves_the_source_matrix(self) -> None:
        self.assertEqual(len(self.by_code), 19)
        self.assertEqual(self.payload["source"]["sha256"], EXPECTED_SOURCE_SHA256)
        self.assertEqual({item["version"] for item in self.by_code.values()}, {VERSION})

    def test_only_source_blockers_remain_draft(self) -> None:
        draft_codes = {
            code for code, item in self.by_code.items() if item["status"] == "DRAFT"
        }
        self.assertEqual(draft_codes, EXPECTED_DRAFT_CODES)
        self.assertEqual(
            self.payload["summary"],
            {
                "matrix_case_count": 19,
                "published_count": 17,
                "draft_count": 2,
            },
        )

    def test_published_cases_have_executable_content(self) -> None:
        for code, item in self.by_code.items():
            if item["status"] != "PUBLISHED":
                continue
            with self.subTest(code=code):
                procedure = item["procedure_spec"]
                self.assertTrue(procedure["preconditions"])
                self.assertTrue(procedure["steps"])
                self.assertTrue(procedure["monitoring_and_records"])
                self.assertTrue(item["decision_rules"]["pass_criteria"])
                self.assertTrue(item["termination_rules"]["items"])
                self.assertTrue(item["equipment_requirements"]["items_text"])

    def test_conflicting_and_incomplete_cases_are_not_silently_published(
        self,
    ) -> None:
        walking = self.by_code["SYS-REL-002"]
        self.assertIn(
            "100 km及1000次",
            walking["procedure_spec"]["matrix"]["primary_stress_or_duration"],
        )
        self.assertTrue(
            any("累计目标50 km" in step for step in walking["procedure_spec"]["steps"])
        )
        self.assertIn("唯一口径", walking["deviation_notes"][0])

        rain = self.by_code["SYS-REL-015"]
        self.assertEqual(rain["procedure_spec"]["steps"], [])
        self.assertIn("缺少前置条件", rain["deviation_notes"][0])


if __name__ == "__main__":
    unittest.main()
