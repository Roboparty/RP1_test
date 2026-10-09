# SPDX-License-Identifier: Apache-2.0
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "analyze_run", ROOT / "scripts/analyze_run.py"
)
analyzer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = analyzer
SPEC.loader.exec_module(analyzer)


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "run"
        shutil.copytree(ROOT / "examples/synthetic-run", self.directory)

    def metadata(self, **updates):
        path = self.directory / "metadata.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(updates)
        path.write_text(json.dumps(data), encoding="utf-8")

    def assert_subset(self, expected, actual):
        if isinstance(expected, dict):
            for key, value in expected.items():
                self.assertIn(key, actual)
                self.assert_subset(value, actual[key])
        elif isinstance(expected, list):
            self.assertEqual(len(expected), len(actual))
            for value, observed in zip(expected, actual):
                self.assert_subset(value, observed)
        elif isinstance(expected, float):
            self.assertAlmostEqual(expected, actual, places=12)
        else:
            self.assertEqual(expected, actual)

    def test_cli_matches_independent_expected_values(self):
        output = Path(self.temp.name) / "output"
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(ROOT / "scripts/analyze_run.py"),
                str(self.directory),
                "--output",
                str(output),
                "--max-gap-s",
                "1.5",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        self.assertIn("summary:", result.stdout)
        expected = json.loads(
            (self.directory / "expected.json").read_text(encoding="utf-8")
        )
        actual = json.loads((output / "summary.json").read_text(encoding="utf-8"))
        self.assert_subset(expected, actual)
        self.assertTrue((output / "report.md").is_file())
        self.assertEqual(3, len(actual["inputs"]))
        self.assertEqual([], actual["warnings"])

    def test_missing_active_intervals_are_unknown_not_wall_time(self):
        self.metadata(confirmed_active_intervals=None, active_seconds=6)
        result = analyzer.analyze(self.directory)
        self.assertIsNone(result["active_seconds"])
        self.assertIsNone(result["joints"][0]["active_samples"])
        self.assertIsNone(result["joints"][0]["tracking_error"]["rms"])
        self.assertEqual(80, result["joints"][0]["temperature"]["peak"])

    def test_duplicates_and_out_of_order_values_do_not_change_statistics(self):
        with (self.directory / "telemetry.csv").open("a", encoding="utf-8") as handle:
            handle.write("8,demo_joint,0,0,999,0,0,999,0\n")
            handle.write("1,demo_joint,0,0,999,0,0,999,0\n")
        joint = analyzer.analyze(self.directory)["joints"][0]
        self.assertEqual(1, joint["duplicate_rows"])
        self.assertEqual(1, joint["out_of_order_rows"])
        self.assertEqual(9, joint["accepted_rows"])
        self.assertEqual(80, joint["temperature"]["peak"])

    def test_invalid_active_intervals_are_rejected(self):
        for intervals in [
            [{"start_s": 0, "end_s": 4}, {"start_s": 3, "end_s": 5}],
            [{"start_s": -1, "end_s": 2}],
            [{"start_s": 0, "end_s": 9}],
            [{"start_s": 0, "end_s": "nan"}],
        ]:
            with self.subTest(intervals=intervals):
                self.metadata(confirmed_active_intervals=intervals)
                with self.assertRaises(ValueError):
                    analyzer.analyze(self.directory)

    def test_finite_rms_does_not_overflow_or_underflow_intermediate_sum(self):
        path = self.directory / "telemetry.csv"
        self.metadata(confirmed_active_intervals=[{"start_s": 0, "end_s": 4}])
        for magnitude in [1e308, 1e-308, 0.0]:
            with self.subTest(magnitude=magnitude):
                path.write_text(
                    "time_s,motor_index,cmd_pos_rad,pos_rad,temp_c\n"
                    + "".join(f"{i},0,0,{magnitude},25\n" for i in range(4)),
                    encoding="utf-8",
                )
                result = analyzer.analyze(self.directory)
                error = result["joints"][0]["tracking_error"]
                self.assertEqual(4, error["sample_count"])
                self.assertEqual(magnitude, error["rms"])
                json.dumps(result, allow_nan=False)

    def test_missing_ambient_and_events_are_reported(self):
        self.metadata(ambient_temperature_c=None)
        (self.directory / "events.jsonl").unlink()
        result = analyzer.analyze(self.directory)
        self.assertIsNone(result["joints"][0]["temperature"]["rise_above_ambient"])
        self.assertTrue(any("events.jsonl" in item for item in result["warnings"]))

    def test_gap_check_is_explicit_and_does_not_change_active_time(self):
        path = self.directory / "telemetry.csv"
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text(
            "\n".join(line for line in lines if not line.startswith(("3,", "4,")))
            + "\n",
            encoding="utf-8",
        )
        result = analyzer.analyze(self.directory, max_gap_s=1.5)
        self.assertEqual(6, result["active_seconds"])
        self.assertEqual(1, result["joints"][0]["sampling_gaps"]["count"])
        self.assertEqual(3, result["joints"][0]["sampling_gaps"]["largest_interval_s"])

    def test_invalid_csv_identity_and_shape_are_rejected(self):
        path = self.directory / "telemetry.csv"
        for row in ["nan,0,0,0", "0,0.5,0,0", "0,0,0", "0,0,0,0,extra"]:
            with self.subTest(row=row):
                path.write_text(
                    "time_s,motor_index,cmd_pos_rad,pos_rad\n" + row + "\n",
                    encoding="utf-8",
                )
                with self.assertRaises(ValueError):
                    analyzer.analyze(self.directory)


if __name__ == "__main__":
    unittest.main()
