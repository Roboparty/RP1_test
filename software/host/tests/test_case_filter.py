import importlib.util
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "case_filter",
    Path(__file__).resolve().parents[1] / "factory_hmi/desktop/case_filter.py",
)
assert _spec is not None and _spec.loader is not None
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
profile_matches_limb = _module.profile_matches_limb
start_decision = _module.start_decision
OTHER_CASE = _module.OTHER_CASE


def profile(case: str, sample: str) -> dict[str, str]:
    return {"case_code": case, "sample_id": sample}


class CaseFilterTests(unittest.TestCase):
    def test_arm_hides_other_parts_and_short_arm_samples(self) -> None:
        rows = [
            profile("REL-ARM-002", "RP1.3-ARM-001"),
            profile("REL-ARM-002", "RP1.3-SARM-004"),
            profile("REL-LEG-001", "RP1.3-LEG-001"),
            profile("REL-UPPER-001", "RP1.3-UPPER-001"),
        ]
        visible = [row for row in rows if profile_matches_limb(row, "left_arm")]
        self.assertEqual(visible, [rows[0]])

    def test_short_arm_keeps_arm_cases_and_only_reduced_samples(self) -> None:
        rows = [
            profile("REL-ARM-002", "RP1.3-ARM-001"),
            profile("REL-ARM-002", "RP1.3-SARM-004"),
            profile("REL-SARM-002", "RP1.3-SARM-004"),
        ]
        visible = [row for row in rows if profile_matches_limb(row, "left_short_arm")]
        self.assertEqual(visible, rows[1:])

    def test_unknown_limb_keeps_every_case(self) -> None:
        row = profile("REL-CHEST-001", "RP1.3-CHEST-001")
        self.assertTrue(profile_matches_limb(row, "custom_module"))

    def test_start_requires_a_case_and_the_same_bench(self) -> None:
        self.assertEqual(start_decision(saved_bench="", context_bench="", case_code=None, has_context=False), "need_case")
        self.assertEqual(start_decision(saved_bench="a10", context_bench="", case_code=OTHER_CASE, has_context=False), "local_other")
        self.assertEqual(start_decision(saved_bench="a10", context_bench="", case_code="REL-ARM-002", has_context=False), "need_sample")
        self.assertEqual(start_decision(saved_bench="", context_bench="A10", case_code="REL-ARM-002", has_context=True), "need_bench")
        self.assertEqual(start_decision(saved_bench="a10", context_bench="RD-01", case_code="REL-ARM-002", has_context=True), "bench_mismatch")
        self.assertEqual(start_decision(saved_bench="a10", context_bench="A10", case_code="REL-ARM-002", has_context=True), "ready")


if __name__ == "__main__":
    unittest.main()
