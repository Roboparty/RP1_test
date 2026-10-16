from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from urllib import error
from unittest import mock

from factory_hmi.client import GatewayClient, GatewayHTTPError


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class GatewayClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = GatewayClient("http://127.0.0.1:8765/", timeout=3.5)

    @mock.patch("factory_hmi.client.request.urlopen")
    def test_get_uses_expected_url_and_timeout(
        self, urlopen: mock.Mock
    ) -> None:
        urlopen.return_value = FakeResponse(b'{"status":"ok"}')

        result = self.client.health()

        self.assertEqual(result, {"status": "ok"})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8765/api/v1/health")
        self.assertEqual(request.method, "GET")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 3.5)

    @mock.patch("factory_hmi.client.request.urlopen")
    def test_post_serializes_json_and_headers(
        self, urlopen: mock.Mock
    ) -> None:
        urlopen.return_value = FakeResponse(b'{"accepted":true}')

        result = self.client.configure("/tmp/left.yaml", "left_leg", "hardware")

        self.assertEqual(result, {"accepted": True})
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url, "http://127.0.0.1:8765/api/v1/configure"
        )
        self.assertEqual(request.method, "POST")
        self.assertEqual(
            json.loads(request.data),
            {
                "config_path": "/tmp/left.yaml",
                "limb": "left_leg",
                "backend": "hardware",
            },
        )
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(request.get_header("Accept"), "application/json")

    def test_websocket_url_matches_http_security(self) -> None:
        self.assertEqual(
            self.client.status_ws_url,
            f"ws://127.0.0.1:8765/ws/status?client_id={self.client.client_id}",
        )
        secure = GatewayClient("https://gateway.example.test/prefix")
        self.assertEqual(
            secure.build_ws_url(),
            f"wss://gateway.example.test/ws/status?client_id={secure.client_id}",
        )

    @mock.patch("factory_hmi.client.request.urlopen")
    def test_standalone_can_and_enable_endpoints(self, urlopen: mock.Mock) -> None:
        urlopen.return_value = FakeResponse(b'{"ok":true}')

        self.client.can_connect()
        can_request = urlopen.call_args.args[0]
        self.assertEqual(can_request.full_url, self.client.base_url + "/api/v1/can/connect")

        self.client.discover_motors()
        discover_request = urlopen.call_args.args[0]
        self.assertEqual(
            discover_request.full_url,
            self.client.base_url + "/api/v1/motors/discover",
        )

        self.client.enable_motors(physical_estop_confirmed=True)
        enable_request = urlopen.call_args.args[0]
        self.assertEqual(
            enable_request.full_url,
            self.client.base_url + "/api/v1/motors/enable",
        )
        self.assertEqual(
            json.loads(enable_request.data),
            {"physical_estop_confirmed": True},
        )
        self.client.reset_motor_fault()
        self.assertEqual(
            urlopen.call_args.args[0].full_url,
            self.client.base_url + "/api/v1/motors/reset-fault",
        )

    @mock.patch("factory_hmi.client.request.urlopen")
    def test_configuration_upload_and_editable_overrides(self, urlopen: mock.Mock) -> None:
        urlopen.return_value = FakeResponse(b'{"ok":true}')
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "arm.yaml"
            path.write_text("kp: [70.0]\n", encoding="utf-8")
            self.client.config_upload(path, "left_arm")
        upload = urlopen.call_args.args[0]
        self.assertEqual(upload.data, b"kp: [70.0]\n")
        self.assertEqual(upload.get_header("X-rp1-limb"), "left_arm")
        self.assertEqual(upload.get_header("X-filename"), "arm.yaml")

        bindings = [
            {
                "index": 0,
                "interface": "can2",
                "mode": "canfd",
                "bitrate": 1_000_000,
                "dbitrate": 5_000_000,
            }
        ]
        motors = [
            {
                "index": 0,
                "motor_id": 1,
                "motor_type": "LRO",
                "motor_model": 2,
                "zero_offset": 0.0,
                "kp": 80.0,
                "kd": 2.0,
                "sign": 1.0,
            }
        ]
        self.client.configure(
            "/var/lib/rp1-factory-hmi/configs/arm.yaml",
            "left_arm",
            "motors_py",
            bus_bindings=bindings,
            motor_overrides=motors,
        )
        configured = urlopen.call_args.args[0]
        payload = json.loads(configured.data)
        self.assertEqual(payload["bus_bindings"], bindings)
        self.assertEqual(payload["motor_overrides"], motors)

    @mock.patch("factory_hmi.client.request.urlopen")
    def test_http_error_exposes_status_and_service_detail(
        self, urlopen: mock.Mock
    ) -> None:
        urlopen.side_effect = error.HTTPError(
            "http://127.0.0.1:8765/api/v1/arm",
            409,
            "Conflict",
            {},
            io.BytesIO(b'{"detail":{"code":"unsafe","reason":"preflight"}}'),
        )

        with self.assertRaises(GatewayHTTPError) as caught:
            self.client.arm()

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(
            caught.exception.service_detail,
            {"code": "unsafe", "reason": "preflight"},
        )
        self.assertIn("HTTP 409", str(caught.exception))
        self.assertIn("preflight", str(caught.exception))

    @mock.patch("factory_hmi.client.request.urlopen")
    def test_empty_success_response_returns_empty_object(
        self, urlopen: mock.Mock
    ) -> None:
        urlopen.return_value = FakeResponse(b" \n")

        result = self.client.playback_stop()

        self.assertEqual(result, {})
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "http://127.0.0.1:8765/api/v1/playback/stop",
        )
        self.assertEqual(json.loads(request.data), {})

    @mock.patch("factory_hmi.client.request.urlopen")
    def test_playback_start_uses_complete_gateway_contract(
        self, urlopen: mock.Mock
    ) -> None:
        urlopen.return_value = FakeResponse(b"{}")

        self.client.playback_start(
            speed=0.25,
            loop=True,
            cycles=12,
            duration_hours=8.0,
            record=True,
            record_rate_hz=10.0,
            test_id="AGE-42",
            robot_id="RP1-007",
        )

        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "http://127.0.0.1:8765/api/v1/playback/start",
        )
        self.assertEqual(
            json.loads(request.data),
            {
                "speed": 0.25,
                "loop": True,
                "cycles": 12,
                "duration_hours": 8.0,
                "record": True,
                "record_rate_hz": 10.0,
                "test_id": "AGE-42",
                "robot_id": "RP1-007",
            },
        )

    @mock.patch("factory_hmi.client.request.urlopen")
    def test_zero_confirm_never_implies_fault_override(
        self, urlopen: mock.Mock
    ) -> None:
        urlopen.return_value = FakeResponse(b"{}")

        self.client.zero_confirm()

        request = urlopen.call_args.args[0]
        self.assertEqual(json.loads(request.data), {"allow_on_fault": False})

    @mock.patch("factory_hmi.client.request.urlopen")
    def test_direct_zero_requests_use_explicit_routes(
        self, urlopen: mock.Mock
    ) -> None:
        urlopen.return_value = FakeResponse(b"{}")

        self.client.zero_motor(3)
        request = urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/api/v1/zero/motor"))
        self.assertEqual(json.loads(request.data), {"motor_index": 3})

        self.client.zero_all_motors()
        request = urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/api/v1/zero/all"))

    @mock.patch("factory_hmi.client.request.urlopen")
    def test_client_identity_token_and_binary_upload(
        self, urlopen: mock.Mock
    ) -> None:
        urlopen.return_value = FakeResponse(b'{"ok":true}')
        client = GatewayClient(
            "http://127.0.0.1:8765",
            client_id="station-operator",
            operator_token="secret",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "aging.npz"
            path.write_bytes(b"npz")
            result = client.trajectory_upload(path)

        self.assertEqual(result, {"ok": True})
        sent = urlopen.call_args.args[0]
        self.assertEqual(sent.data, b"npz")
        self.assertEqual(sent.get_header("X-rp1-client-id"), "station-operator")
        self.assertEqual(sent.get_header("X-rp1-operator-token"), "secret")
        self.assertEqual(sent.get_header("X-filename"), "aging.npz")
        self.assertEqual(
            client.status_ws_url,
            "ws://127.0.0.1:8765/ws/status?"
            "client_id=station-operator&token=secret",
        )


if __name__ == "__main__":
    unittest.main()
