import math
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from client import line_protocol, timestamp_ns
from telemetry import generate
from init import initialize


class ContractTest(unittest.TestCase):
    tags = {"run_id": "demo", "case_id": "SYS-REL-001", "subject": "joint 1", "source_kind": "synthetic"}

    def test_nanoseconds_and_timezone_preserved(self):
        self.assertEqual(timestamp_ns("1970-01-01T00:00:00.123456789Z"), 123456789)
        self.assertEqual(timestamp_ns("1970-01-01T08:00:00.123456789+08:00"), 123456789)
        with self.assertRaises(ValueError):
            timestamp_ns("2026-10-06T12:00:00")

    def test_null_omitted_zero_preserved_and_tags_escaped(self):
        result = line_protocol(self.tags, {"temp_c": 0, "torque_nm": None}, 123456789)
        self.assertIn("subject=joint\\ 1", result)
        self.assertIn("temp_c=0.0", result)
        self.assertNotIn("torque_nm", result)
        self.assertTrue(result.endswith(" 123456789"))

    def test_bad_values_and_source_rejected(self):
        for value in (math.nan, math.inf, True):
            with self.assertRaises(ValueError):
                line_protocol(self.tags, {"temp_c": value}, 0)
        with self.assertRaises(ValueError):
            line_protocol({**self.tags, "source_kind": "unknown"}, {"temp_c": 20}, 0)

    def test_synthetic_gap_and_missing_torque(self):
        points = generate("demo", "SYS-REL-001", "joint", 0)
        self.assertEqual(len(points), 575)
        times = [int(point.rsplit(" ", 1)[1]) for point in points]
        self.assertFalse(any(55_000_000_000 <= t < 60_000_000_000 for t in times))
        self.assertTrue(all("source_kind=synthetic" in point and "torque_nm" not in point for point in points))

    def test_reused_instance_does_not_setup_again(self):
        class Existing:
            org = "rp1_test"
            bucket = "telemetry"
            def health(self):
                return {"status": "pass"}
            def request(self, method, path, **kwargs):
                self_test.assertEqual(method, "GET")
                if path == "/api/v2/setup":
                    return b'{"allowed": false}'
                return b'{"buckets": [{"name": "telemetry"}]}'
        self_test = self
        with patch("init.InfluxClient", return_value=Existing()):
            initialize(0)


if __name__ == "__main__":
    unittest.main()
