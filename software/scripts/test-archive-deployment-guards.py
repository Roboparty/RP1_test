#!/usr/bin/env python3
"""Verify disposable-project guards without contacting Docker or site services."""
import json
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
from unittest.mock import patch

API = runpy.run_path(str(Path(__file__).with_name("test-archive-deployment.py")))


class OwnedProjectGuards(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix=API["PREFIX"])
        self.addCleanup(self.scratch.cleanup)
        self.directory = Path(self.scratch.name)
        self.stage = self.directory / "candidate"
        self.stage.mkdir()
        self.project = API["PREFIX"] + "123456789abc"
        (self.stage / ".env").write_text("COMPOSE_PROJECT_NAME=" + self.project + "\n")
        self.state = {"run_dir": str(self.directory), "project": self.project, "stage": str(self.stage)}

    def save(self):
        (self.directory / "run.json").write_text(json.dumps(self.state))

    def rejected_without_docker(self):
        self.save()
        with patch("subprocess.run") as mutation, patch("subprocess.check_output") as inspection:
            with self.assertRaises(ValueError):
                API["load_state"](self.directory)
            with self.assertRaises(ValueError):
                API["cleanup"](self.directory, self.state)
            mutation.assert_not_called()
            inspection.assert_not_called()

    def test_valid_private_state_loads(self):
        self.save()
        directory, state = API["load_state"](self.directory)
        self.assertEqual(directory, self.directory)
        self.assertEqual(state["project"], self.project)

    def test_external_stage_is_rejected_before_any_compose_action(self):
        with tempfile.TemporaryDirectory(prefix="rp1-unrelated-site-") as external:
            self.state["stage"] = external
            self.rejected_without_docker()

    def test_different_environment_project_cannot_be_cleaned(self):
        (self.stage / ".env").write_text("COMPOSE_PROJECT_NAME=unrelated-site\n")
        self.rejected_without_docker()

    def test_symlink_escape_cannot_target_another_site(self):
        with tempfile.TemporaryDirectory(prefix="rp1-unrelated-site-") as external:
            (self.stage / ".env").unlink()
            self.stage.rmdir()
            self.stage.symlink_to(external, target_is_directory=True)
            self.rejected_without_docker()

    def test_arbitrary_browser_container_cannot_be_removed(self):
        self.state["browser_container"] = "unrelated-browser"
        self.rejected_without_docker()

    def test_startup_failure_attempts_cleanup_of_the_new_owned_project(self):
        with tempfile.TemporaryDirectory(prefix=API["PREFIX"]) as fresh:
            def copied(stage):
                stage.mkdir()

            with patch("tempfile.mkdtemp", return_value=fresh), patch.dict(API["start"].__globals__, {
                "copy_candidate": copied, "fingerprint": lambda *_: "a" * 64,
                "create_tls": lambda *_: None,
            }), patch.dict(API["start"].__globals__, {
                "run": unittest.mock.Mock(side_effect=subprocess.CalledProcessError(1, ["isolated-startup"])),
                "cleanup": unittest.mock.Mock(),
            }):
                cleanup = API["start"].__globals__["cleanup"]
                with self.assertRaises(subprocess.CalledProcessError):
                    API["start"]()
                directory, state = cleanup.call_args.args
                self.assertEqual(directory, Path(fresh))
                self.assertEqual(state["stage"], str(Path(fresh) / "candidate"))
                self.assertTrue(state["project"].startswith(API["PREFIX"]))
                cleanup.assert_called_once()


if __name__ == "__main__":
    unittest.main()
