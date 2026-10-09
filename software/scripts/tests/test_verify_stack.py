# SPDX-License-Identifier: Apache-2.0
"""Offline regressions for transport errors and bounded startup waiting."""

import contextlib
import http.client
import importlib.util
import io
import json
import sys
import traceback
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).resolve().parents[1] / "verify_stack.py"
SPEC = importlib.util.spec_from_file_location("verify_stack_under_test", SCRIPT)
verify_stack = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify_stack)


class RequestTests(unittest.TestCase):
    def test_connection_errors_are_retryable_and_do_not_expose_credentials(self):
        secret = "private-token-not-for-errors"
        errors = [
            http.client.RemoteDisconnected(secret),
            ConnectionResetError(secret),
            TimeoutError(secret),
            urllib.error.URLError(secret),
            http.client.BadStatusLine(secret),
        ]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                with (
                    patch.object(
                        verify_stack.urllib.request, "urlopen", side_effect=error
                    ),
                    self.assertRaisesRegex(
                        RuntimeError, "^A stack endpoint is unavailable$"
                    ) as failure,
                ):
                    verify_stack.request(
                        f"http://user:{secret}@localhost/health?token={secret}",
                        headers={"Authorization": f"Bearer {secret}"},
                    )
                rendered = "".join(traceback.format_exception(failure.exception))
                self.assertNotIn(secret, rendered)
                self.assertNotIn("Authorization", rendered)
                self.assertTrue(failure.exception.__suppress_context__)

    def test_interrupted_response_body_is_retryable(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.side_effect = http.client.IncompleteRead(b"private-body", 10)
        with (
            patch.object(verify_stack.urllib.request, "urlopen", return_value=response),
            self.assertRaisesRegex(
                RuntimeError, "^A stack endpoint is unavailable$"
            ) as failure,
        ):
            verify_stack.request("http://localhost/health")
        self.assertNotIn(
            "private-body", "".join(traceback.format_exception(failure.exception))
        )

    def test_http_status_error_preserves_only_status_and_path(self):
        secret = "private-token-not-for-errors"
        url = f"http://user:{secret}@localhost/health?token={secret}"
        error = urllib.error.HTTPError(
            url, 503, secret, {"Authorization": secret}, None
        )
        with (
            patch.object(verify_stack.urllib.request, "urlopen", side_effect=error),
            self.assertRaisesRegex(RuntimeError, "^HTTP 503 from /health$") as failure,
        ):
            verify_stack.request(url, headers={"Authorization": secret})
        self.assertNotIn(secret, "".join(traceback.format_exception(failure.exception)))

    def test_successful_response_is_unchanged(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"status":"ok"}'
        with patch.object(
            verify_stack.urllib.request, "urlopen", return_value=response
        ) as opened:
            self.assertEqual(
                b'{"status":"ok"}', verify_stack.request("http://localhost/health")
            )
        self.assertEqual(20, opened.call_args.kwargs["timeout"])


class StartupWaitTests(unittest.TestCase):
    def test_connection_interruption_retries_until_success(self):
        report = {"api": {"status": "ok"}}
        calls = []

        def inspect(_env_file, *, demo):
            calls.append(demo)
            if len(calls) == 1:
                return verify_stack.request("http://localhost/health")
            return report

        with (
            patch.object(sys, "argv", [str(SCRIPT), "--demo", "--wait-seconds", "2"]),
            patch.object(verify_stack, "inspect_stack", side_effect=inspect),
            patch.object(
                verify_stack.urllib.request,
                "urlopen",
                side_effect=http.client.RemoteDisconnected("closed"),
            ),
            patch.object(verify_stack.time, "monotonic", side_effect=[10.0, 10.0]),
            patch.object(verify_stack.time, "sleep") as sleep,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            verify_stack.main()
        self.assertEqual([True, True], calls)
        sleep.assert_called_once_with(1)
        self.assertEqual(report, json.loads(output.getvalue()))

    def test_repeated_transport_errors_stop_at_existing_deadline(self):
        def inspect(_env_file, *, demo):
            return verify_stack.request("http://localhost/health")

        with (
            patch.object(sys, "argv", [str(SCRIPT), "--wait-seconds", "2"]),
            patch.object(
                verify_stack, "inspect_stack", side_effect=inspect
            ) as inspected,
            patch.object(
                verify_stack.urllib.request,
                "urlopen",
                side_effect=ConnectionResetError("private-error"),
            ),
            patch.object(
                verify_stack.time, "monotonic", side_effect=[10.0, 10.0, 11.0, 12.0]
            ),
            patch.object(verify_stack.time, "sleep") as sleep,
            self.assertRaisesRegex(RuntimeError, "^A stack endpoint is unavailable$"),
        ):
            verify_stack.main()
        self.assertEqual(3, inspected.call_count)
        self.assertEqual(
            [unittest.mock.call(1), unittest.mock.call(1)], sleep.call_args_list
        )

    def test_zero_wait_does_not_retry(self):
        with (
            patch.object(sys, "argv", [str(SCRIPT), "--wait-seconds", "0"]),
            patch.object(
                verify_stack, "inspect_stack", side_effect=RuntimeError("unavailable")
            ) as inspected,
            patch.object(verify_stack.time, "monotonic", side_effect=[10.0, 10.0]),
            patch.object(verify_stack.time, "sleep") as sleep,
            self.assertRaisesRegex(RuntimeError, "^unavailable$"),
        ):
            verify_stack.main()
        inspected.assert_called_once()
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
