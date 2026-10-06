from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from fastapi.testclient import TestClient

from factory_hmi.core.service import FactoryService
from factory_hmi.gateway.app import create_app
from factory_hmi.gateway.lease import ControlLease, LeaseConflict
from factory_hmi.sync.outbox import Outbox


class StubController:
    def __init__(self) -> None:
        self.state = "disconnected"
        self.config = None
        self.trajectory = None
        self.stop_reason = None

    def snapshot(self):
        return {
            "state": self.state,
            "motors": [],
            "trajectory": self.trajectory,
            "diagnostics": [],
        }

    def configure(self, **kwargs):
        self.config = kwargs
        return kwargs

    def connect(self):
        self.state = "connected"

    def disconnect(self):
        self.state = "disconnected"

    def arm(self):
        self.state = "armed"

    def import_trajectory(self, path):
        self.trajectory = {"path": str(path)}
        return self.trajectory

    def start(self, **_kwargs):
        self.state = "running"

    def pause(self):
        self.state = "paused"

    def resume(self):
        self.state = "running"

    def stop(self, reason=None):
        self.stop_reason = reason
        self.state = "completed"

    def reset(self):
        return None

    def disable(self):
        self.state = "completed"

    def zero_prepare(self, motor_index):
        return {"motor_index": motor_index}

    def zero_motor(self, motor_index):
        return {"zeroed": [{"index": motor_index}]}

    def zero_all_motors(self):
        return {"zeroed": "all"}

    def zero_read(self):
        return {"position": 0.0, "error_id": 0}

    def zero_confirm(self, allow_on_fault=False):
        return {"zeroed": True, "allow_on_fault": allow_on_fault}

    def zero_skip(self):
        return None

    def zero_abort(self):
        return None


class EventLoopSensitiveController(StubController):
    def __init__(self) -> None:
        super().__init__()
        self.reject_event_loop = False

    def snapshot(self):
        if self.reject_event_loop:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                raise RuntimeError("snapshot must run outside the event-loop thread")
        return super().snapshot()


class SlowConfigureController(StubController):
    def configure(self, **kwargs):
        time.sleep(0.15)
        return super().configure(**kwargs)


class ControlLeaseTests(unittest.TestCase):
    def test_exclusive_owner_and_expiry(self) -> None:
        lease = ControlLease(timeout_s=1.0)
        lease.claim("one", now=10.0)
        with self.assertRaises(LeaseConflict):
            lease.claim("two", now=10.5)
        self.assertFalse(lease.expired(now=10.9))
        self.assertTrue(lease.expired(now=11.1))
        lease.claim("two", now=11.1)
        self.assertEqual(lease.snapshot(now=11.2).owner, "two")


class GatewayApiTests(unittest.TestCase):
    def test_new_client_session_sees_migrated_station_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            test_id = "left_arm_2609110413_RP1.3-LARM-001_RD-01"
            legacy_execution = root / "sessions/old-client/executions" / test_id
            (legacy_execution / "analysis").mkdir(parents=True)
            (legacy_execution / "manifest.json").write_text(
                json.dumps(
                    {
                        "execution_uuid": "11111111-1111-4111-8111-111111111111",
                        "test_id": test_id,
                        "sample_id": "RP1.3-LARM-001",
                        "station_id": "SHN",
                        "operator_id": "test1",
                        "started_at": "2026-09-11T04:13:49Z",
                        "ended_at": "2026-09-11T04:15:55Z",
                    }
                ),
                encoding="utf-8",
            )
            (legacy_execution / "analysis/summary.json").write_text(
                json.dumps({"verdict": {"status": "passed"}}),
                encoding="utf-8",
            )
            legacy_export = root / "sessions/old-client/exports" / f"{test_id}.tar.gz"
            legacy_export.parent.mkdir(parents=True)
            legacy_export.write_bytes(b"legacy bundle")

            with TestClient(create_app(data_root=root)) as client:
                response = client.get(
                    "/api/v1/executions",
                    headers={"X-RP1-Client-ID": "new-client"},
                )

            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["items"][0]["test_id"], test_id)
            self.assertTrue((root / "executions" / test_id / "manifest.json").is_file())
            self.assertEqual(
                (root / "exports" / f"{test_id}.tar.gz").read_bytes(),
                b"legacy bundle",
            )

    def test_platform_presence_uses_configured_bench_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app(
                controller=StubController(),
                data_root=Path(temp_dir),
            )
            runtime = app.state.runtime.session("operator-a")
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "RP1_FACTORY_STATION_ID": "Fab_01",
                        "RP1_FACTORY_BENCH_ID": "RD-01",
                    },
                ),
                mock.patch.object(
                    runtime._platform,
                    "snapshot",
                    return_value={
                        "operator": "left_arm_01",
                        "uploader_running": True,
                    },
                ),
                mock.patch.object(runtime._platform, "set_presence") as set_presence,
            ):
                runtime.snapshot()

            presence = set_presence.call_args.args[0]
            self.assertEqual(presence["bench_id"], "RD-01")
            self.assertNotEqual(presence["bench_id"], "Fab_01")

    def test_direct_single_and_all_motor_zero_endpoints(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        config_path = repository / "scripts/config/arm_motors.yaml"
        headers = {"X-RP1-Client-ID": "zero-operator"}
        with tempfile.TemporaryDirectory() as temp_dir, TestClient(
            create_app(data_root=Path(temp_dir))
        ) as client:
            configured = client.post(
                "/api/v1/configure",
                headers=headers,
                json={
                    "config_path": str(config_path),
                    "limb": "left_arm",
                    "backend": "fake",
                },
            )
            self.assertEqual(configured.status_code, 200, configured.text)
            client.post("/api/v1/can/connect", headers=headers)
            client.post("/api/v1/motors/discover", headers=headers)
            client.post(
                "/api/v1/motors/enable",
                headers=headers,
                json={"physical_estop_confirmed": True},
            )

            single = client.post(
                "/api/v1/zero/motor",
                headers=headers,
                json={"motor_index": 2},
            )
            self.assertEqual(single.status_code, 200, single.text)
            self.assertEqual(
                single.json()["result"]["zeroed"][0]["index"],
                2,
            )
            self.assertFalse(
                single.json()["snapshot"]["station"]["motors_enabled"]
            )

            all_motors = client.post(
                "/api/v1/zero/all",
                headers=headers,
            )
            self.assertEqual(all_motors.status_code, 200, all_motors.text)
            self.assertEqual(
                len(all_motors.json()["result"]["zeroed"]),
                7,
            )

    def test_manual_joint_control_endpoint_and_stop(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        config_path = repository / "scripts/config/arm_motors.yaml"
        headers = {"X-RP1-Client-ID": "operator-a"}
        with tempfile.TemporaryDirectory() as temp_dir, TestClient(
            create_app(data_root=Path(temp_dir))
        ) as client:
            configured = client.post(
                "/api/v1/configure",
                headers=headers,
                json={
                    "config_path": str(config_path),
                    "limb": "left_arm",
                    "backend": "fake",
                },
            )
            self.assertEqual(configured.status_code, 200, configured.text)
            self.assertEqual(
                client.post("/api/v1/can/connect", headers=headers).status_code,
                200,
            )
            self.assertEqual(
                client.post("/api/v1/motors/discover", headers=headers).status_code,
                200,
            )
            self.assertEqual(
                client.post(
                    "/api/v1/motors/enable",
                    headers=headers,
                    json={"physical_estop_confirmed": True},
                ).status_code,
                200,
            )
            moving = client.post(
                "/api/v1/manual/move",
                headers=headers,
                json={
                    "targets_deg": [1.0, -1.0, 0.5, 0.0, 0.0, 0.0, 0.0],
                    "speed_deg_s": 20.0,
                },
            )
            self.assertEqual(moving.status_code, 200, moving.text)
            self.assertTrue(
                moving.json()["result"]["station"]["manual_control_active"]
            )
            stopped = client.post("/api/v1/manual/stop", headers=headers)
            self.assertEqual(stopped.status_code, 200, stopped.text)
            self.assertTrue(
                stopped.json()["result"]["station"]["position_hold_active"]
            )

    def test_current_record_endpoint_downloads_only_service_record(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        config_path = repository / "scripts/config/arm_motors.yaml"
        with tempfile.TemporaryDirectory() as temp_dir:
            service = FactoryService(data_root=temp_dir)
            service.configure(
                config_path=str(config_path),
                limb="left_arm",
                backend="fake",
            )
            record = service.record_root / "REL-001.csv"
            record.write_text("sample,value\n0,1\n", encoding="utf-8")
            service.controller.record_output = record
            with TestClient(
                create_app(service, data_root=Path(temp_dir))
            ) as client:
                response = client.get(
                    "/api/v1/records/current.csv",
                    headers={"X-RP1-Client-ID": "operator-a"},
                )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.text, "sample,value\n0,1\n")
            self.assertIn(
                'filename="REL-001.csv"',
                response.headers["content-disposition"],
            )

    def test_record_rate_is_limited_to_cached_feedback_rate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, TestClient(
            create_app(StubController(), data_root=Path(temp_dir))
        ) as client:
            response = client.post(
                "/api/v1/playback/start",
                headers={"X-RP1-Client-ID": "operator-a"},
                json={"record_rate_hz": 21.0},
            )

        self.assertEqual(response.status_code, 422, response.text)

    def test_slow_mutation_is_not_released_while_command_is_running(self) -> None:
        controller = SlowConfigureController()
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "arm.yaml"
            config_path.write_text("{}")
            environment = {"RP1_FACTORY_ALLOWED_CONFIG_ROOT": temp_dir}
            with mock.patch.dict(os.environ, environment, clear=False), TestClient(
                create_app(
                    controller,
                    data_root=Path(temp_dir),
                    lease_timeout_s=0.05,
                )
            ) as client:
                configured = client.post(
                    "/api/v1/configure",
                    headers={"X-RP1-Client-ID": "operator-a"},
                    json={
                        "config_path": str(config_path),
                        "limb": "left_arm",
                        "backend": "fake",
                    },
                )
                heartbeat = client.post(
                    "/api/v1/lease/heartbeat",
                    headers={"X-RP1-Client-ID": "operator-a"},
                )

                self.assertEqual(configured.status_code, 200, configured.text)
                self.assertEqual(heartbeat.status_code, 200, heartbeat.text)
                self.assertIsNotNone(controller.config)

    def test_commands_are_leased_to_one_client(self) -> None:
        controller = StubController()
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "arm.yaml"
            config_path.write_text("{}")
            environment = {"RP1_FACTORY_ALLOWED_CONFIG_ROOT": temp_dir}
            with mock.patch.dict(os.environ, environment, clear=False), TestClient(
                create_app(controller, data_root=Path(temp_dir))
            ) as client:
                first = client.post(
                    "/api/v1/configure",
                    headers={"X-RP1-Client-ID": "operator-a"},
                    json={"config_path": str(config_path), "limb": "left_arm", "backend": "fake"},
                )
                self.assertEqual(first.status_code, 200)
                second = client.post(
                    "/api/v1/connect",
                    headers={"X-RP1-Client-ID": "operator-b"},
                )
                self.assertEqual(second.status_code, 409)
                connected = client.post(
                    "/api/v1/connect",
                    headers={"X-RP1-Client-ID": "operator-a"},
                )
                self.assertEqual(connected.status_code, 200)
                self.assertEqual(connected.json()["snapshot"]["state"], "connected")

    def test_operator_token_and_websocket_snapshot(self) -> None:
        controller = EventLoopSensitiveController()
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "arm.yaml"
            config_path.write_text("{}")
            environment = {
                "RP1_FACTORY_OPERATOR_TOKEN": "secret",
                "RP1_FACTORY_ALLOWED_CONFIG_ROOT": temp_dir,
            }
            with mock.patch.dict(
                os.environ,
                environment,
                clear=False,
            ), TestClient(create_app(controller, data_root=Path(temp_dir))) as client:
                denied = client.post(
                    "/api/v1/configure",
                    json={"config_path": str(config_path), "limb": "left_arm", "backend": "fake"},
                )
                self.assertEqual(denied.status_code, 401)
                accepted = client.post(
                    "/api/v1/configure",
                    headers={"X-RP1-Operator-Token": "secret"},
                    json={"config_path": str(config_path), "limb": "left_arm", "backend": "fake"},
                )
                self.assertEqual(accepted.status_code, 200)
                controller.reject_event_loop = True
                with client.websocket_connect("/ws/status?token=secret") as socket:
                    self.assertEqual(socket.receive_json()["state"], "disconnected")

    def test_trajectory_upload_runs_snapshot_outside_event_loop(self) -> None:
        controller = EventLoopSensitiveController()
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "arm.yaml"
            config_path.write_text("{}")
            environment = {"RP1_FACTORY_ALLOWED_CONFIG_ROOT": temp_dir}
            with mock.patch.dict(os.environ, environment, clear=False), TestClient(
                create_app(controller, data_root=Path(temp_dir))
            ) as client:
                configured = client.post(
                    "/api/v1/configure",
                    headers={"X-RP1-Client-ID": "operator-a"},
                    json={
                        "config_path": str(config_path),
                        "limb": "left_arm",
                        "backend": "fake",
                    },
                )
                self.assertEqual(configured.status_code, 200, configured.text)
                controller.reject_event_loop = True

                uploaded = client.post(
                    "/api/v1/trajectory/upload",
                    headers={
                        "X-RP1-Client-ID": "operator-a",
                        "X-Filename": "aging.npz",
                        "Content-Type": "application/octet-stream",
                    },
                    content=b"npz-placeholder",
                )

                self.assertEqual(uploaded.status_code, 200, uploaded.text)
                self.assertEqual(
                    uploaded.json()["result"]["path"],
                    str(Path(temp_dir) / "trajectories" / "aging.npz"),
                )

    def test_binary_trajectory_upload_is_saved_then_validated(self) -> None:
        controller = StubController()
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "arm.yaml"
            config_path.write_text("{}")
            environment = {"RP1_FACTORY_ALLOWED_CONFIG_ROOT": temp_dir}
            with mock.patch.dict(os.environ, environment, clear=False), TestClient(
                create_app(controller, data_root=Path(temp_dir))
            ) as client:
                client.post(
                    "/api/v1/configure",
                    headers={"X-RP1-Client-ID": "operator-a"},
                    json={"config_path": str(config_path), "limb": "left_arm", "backend": "fake"},
                )
                response = client.post(
                    "/api/v1/trajectory/upload",
                    headers={
                        "X-RP1-Client-ID": "operator-a",
                        "X-Filename": "../aging.npz",
                        "Content-Type": "application/octet-stream",
                    },
                    content=b"npz-placeholder",
                )
                self.assertEqual(response.status_code, 200)
                path = Path(controller.trajectory["path"])
                self.assertEqual(path.name, "aging.npz")
                self.assertEqual(path.read_bytes(), b"npz-placeholder")

    def test_expired_operator_lease_stops_running_test(self) -> None:
        controller = StubController()
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "arm.yaml"
            config_path.write_text("{}")
            environment = {"RP1_FACTORY_ALLOWED_CONFIG_ROOT": temp_dir}
            with mock.patch.dict(os.environ, environment, clear=False), TestClient(
                create_app(
                    controller,
                    data_root=Path(temp_dir),
                    lease_timeout_s=0.05,
                )
            ) as client:
                client.post(
                    "/api/v1/configure",
                    headers={"X-RP1-Client-ID": "operator-a"},
                    json={"config_path": str(config_path), "limb": "left_arm", "backend": "fake"},
                )
                controller.state = "running"
                time.sleep(0.2)
                self.assertEqual(controller.state, "disconnected")
                self.assertEqual(controller.stop_reason, "operator lease expired")

    def test_distinct_clients_control_distinct_can_sessions(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        config_path = repository / "scripts/config/arm_motors.yaml"
        with tempfile.TemporaryDirectory() as temp_dir, TestClient(
            create_app(data_root=Path(temp_dir))
        ) as client:
            def configure(owner: str, interface: str):
                return client.post(
                    "/api/v1/configure",
                    headers={"X-RP1-Client-ID": owner},
                    json={
                        "config_path": str(config_path),
                        "limb": "left_arm",
                        "backend": "fake",
                        "bus_bindings": [
                            {
                                "index": 0,
                                "interface": interface,
                                "mode": "canfd",
                                "bitrate": 1_000_000,
                                "dbitrate": 5_000_000,
                            }
                        ],
                    },
                )

            first = configure("arm-window", "can0")
            second = configure("leg-window", "can1")
            conflict = configure("other-window", "can0")
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(second.status_code, 200, second.text)
            self.assertEqual(conflict.status_code, 409, conflict.text)
            discovered = client.get(
                "/api/v1/can/interfaces",
                headers={"X-RP1-Client-ID": "arm-window"},
            )
            self.assertEqual(discovered.status_code, 200, discovered.text)
            by_name = {
                item["interface"]: item
                for item in discovered.json()["result"]["items"]
            }
            self.assertEqual(by_name["can0"]["owner"], "arm-window")
            self.assertEqual(by_name["can1"]["owner"], "leg-window")
            self.assertEqual(
                client.post(
                    "/api/v1/can/connect",
                    headers={"X-RP1-Client-ID": "arm-window"},
                ).status_code,
                200,
            )
            self.assertEqual(
                client.post(
                    "/api/v1/can/connect",
                    headers={"X-RP1-Client-ID": "leg-window"},
                ).status_code,
                200,
            )
            arm_snapshot = client.get(
                "/api/v1/snapshot",
                headers={"X-RP1-Client-ID": "arm-window"},
            ).json()
            leg_snapshot = client.get(
                "/api/v1/snapshot",
                headers={"X-RP1-Client-ID": "leg-window"},
            ).json()
            self.assertEqual(
                arm_snapshot["can"]["interfaces"][0]["interface"],
                "can0",
            )
            self.assertEqual(
                leg_snapshot["can"]["interfaces"][0]["interface"],
                "can1",
            )

    def test_sessions_share_global_outbox_without_resetting_active_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            legacy_root = root / "sessions/legacy-session"
            bundle = legacy_root / "execution.tar.gz"
            bundle.parent.mkdir(parents=True)
            bundle.write_bytes(b"bundle")
            legacy_outbox = Outbox(
                legacy_root / "sync" / "outbox.sqlite3"
            )
            item = legacy_outbox.enqueue(
                {
                    "execution_uuid": "execution-a",
                    "test_id": "test-a",
                },
                bundle,
            )
            legacy_outbox.update(
                item.execution_uuid,
                state="uploading",
            )
            app = create_app(data_root=root)

            with TestClient(app) as client:
                client.get(
                    "/api/v1/snapshot",
                    headers={"X-RP1-Client-ID": "operator-a"},
                )
                client.get(
                    "/api/v1/snapshot",
                    headers={"X-RP1-Client-ID": "operator-b"},
                )
                first = app.state.runtime.session("operator-a")
                second = app.state.runtime.session("operator-b")

                self.assertIs(first.outbox, second.outbox)
                self.assertIs(first.controller.outbox, second.controller.outbox)
                self.assertIs(first._platform, second._platform)
                self.assertEqual(
                    first.outbox.path,
                    root / "sync" / "outbox.sqlite3",
                )
                recovered = second.outbox.get(item.execution_uuid)
                assert recovered is not None
                self.assertEqual(recovered.state, "uploading")
                self.assertFalse(
                    (root / "sessions/operator-a/sync/outbox.sqlite3").exists()
                )

    def test_disconnect_releases_interface_for_another_session(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        config_path = repository / "scripts/config/arm_motors.yaml"

        def configuration(interface: str) -> dict[str, object]:
            return {
                "config_path": str(config_path),
                "limb": "left_arm",
                "backend": "fake",
                "bus_bindings": [
                    {
                        "index": 0,
                        "interface": interface,
                        "mode": "canfd",
                        "bitrate": 1_000_000,
                        "dbitrate": 5_000_000,
                    }
                ],
            }

        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app(data_root=Path(temp_dir))
            with TestClient(app) as client:
                first_headers = {"X-RP1-Client-ID": "operator-a"}
                second_headers = {"X-RP1-Client-ID": "operator-b"}
                first = client.post(
                    "/api/v1/configure",
                    headers=first_headers,
                    json=configuration("can0"),
                )
                self.assertEqual(first.status_code, 200, first.text)
                self.assertEqual(
                    app.state.runtime.interface_owners()["can0"],
                    "operator-a",
                )

                disconnected = client.post(
                    "/api/v1/disconnect",
                    headers=first_headers,
                )
                self.assertEqual(disconnected.status_code, 200, disconnected.text)
                self.assertNotIn(
                    "can0",
                    app.state.runtime.interface_owners(),
                )

                replacement = client.post(
                    "/api/v1/configure",
                    headers=second_headers,
                    json=configuration("can0"),
                )
                self.assertEqual(replacement.status_code, 200, replacement.text)

    def test_expired_lease_releases_interface_owner(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        config_path = repository / "scripts/config/arm_motors.yaml"
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app(
                data_root=Path(temp_dir),
                lease_timeout_s=0.05,
            )
            with TestClient(app) as client:
                configured = client.post(
                    "/api/v1/configure",
                    headers={"X-RP1-Client-ID": "operator-a"},
                    json={
                        "config_path": str(config_path),
                        "limb": "left_arm",
                        "backend": "fake",
                        "bus_bindings": [
                            {
                                "index": 0,
                                "interface": "can0",
                                "mode": "canfd",
                                "bitrate": 1_000_000,
                                "dbitrate": 5_000_000,
                            }
                        ],
                    },
                )
                self.assertEqual(configured.status_code, 200, configured.text)
                deadline = time.monotonic() + 1.0
                while (
                    "can0" in app.state.runtime.interface_owners()
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
                self.assertNotIn(
                    "can0",
                    app.state.runtime.interface_owners(),
                )

    def test_yaml_upload_previews_motor_parameters_without_yaml_can(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        source = repository / "scripts/config/arm_motors.yaml"
        with tempfile.TemporaryDirectory() as temp_dir, TestClient(
            create_app(data_root=Path(temp_dir))
        ) as client:
            response = client.post(
                "/api/v1/config/upload",
                headers={
                    "X-RP1-Client-ID": "operator-a",
                    "X-RP1-Limb": "left_arm",
                    "X-Filename": source.name,
                    "Content-Type": "application/x-yaml",
                },
                content=source.read_bytes(),
            )
            self.assertEqual(response.status_code, 200, response.text)
            preview = response.json()["result"]
            self.assertEqual(
                preview["bus_groups"],
                [
                    {
                        "index": 0,
                        "motor_count": 7,
                        "default_interface": "can0",
                    }
                ],
            )
            self.assertEqual(preview["motors"][0]["kp"], 60.0)
            self.assertNotIn("interface", preview["motors"][0])
            self.assertTrue(Path(preview["config_path"]).is_file())

    def test_configuration_clear_allows_switching_motor_count(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        arm_config = repository / "scripts/config/arm_motors.yaml"
        short_arm_config = repository / "scripts/config/short_arm_motors.yaml"
        headers = {"X-RP1-Client-ID": "operator-a"}
        with tempfile.TemporaryDirectory() as temp_dir, TestClient(
            create_app(data_root=Path(temp_dir))
        ) as client:
            configured = client.post(
                "/api/v1/configure",
                headers=headers,
                json={
                    "config_path": str(arm_config),
                    "limb": "left_arm",
                    "backend": "fake",
                },
            )
            self.assertEqual(configured.status_code, 200, configured.text)
            self.assertEqual(
                len(configured.json()["result"]["configured_motors"]),
                7,
            )
            self.assertEqual(
                client.post("/api/v1/can/connect", headers=headers).status_code,
                200,
            )
            self.assertEqual(
                client.post("/api/v1/motors/discover", headers=headers).status_code,
                200,
            )

            cleared = client.post(
                "/api/v1/configuration/clear",
                headers=headers,
            )
            self.assertEqual(cleared.status_code, 200, cleared.text)
            self.assertFalse(cleared.json()["result"]["configured"])

            replacement = client.post(
                "/api/v1/configure",
                headers=headers,
                json={
                    "config_path": str(short_arm_config),
                    "limb": "right_short_arm",
                    "backend": "fake",
                },
            )
            self.assertEqual(replacement.status_code, 200, replacement.text)
            result = replacement.json()["result"]
            self.assertEqual(result["station"]["limb"], "right_short_arm")
            self.assertEqual(len(result["configured_motors"]), 5)

    def test_standalone_can_discovery_and_enable_flow(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        config_path = repository / "scripts/config/arm_motors.yaml"
        with tempfile.TemporaryDirectory() as temp_dir, TestClient(
            create_app(data_root=Path(temp_dir))
        ) as client:
            headers = {"X-RP1-Client-ID": "operator-a"}
            configured = client.post(
                "/api/v1/configure",
                headers=headers,
                json={
                    "config_path": str(config_path),
                    "limb": "left_arm",
                    "backend": "fake",
                },
            )
            self.assertEqual(configured.status_code, 200, configured.text)
            self.assertFalse(
                client.get("/api/v1/can/status").json()["result"]["all_up"]
            )

            can_connected = client.post("/api/v1/can/connect", headers=headers)
            self.assertEqual(can_connected.status_code, 200, can_connected.text)
            self.assertTrue(can_connected.json()["result"]["can"]["all_up"])
            discovered = client.post("/api/v1/motors/discover", headers=headers)
            self.assertEqual(discovered.status_code, 200, discovered.text)
            self.assertTrue(
                discovered.json()["result"]["station"]["backend_connected"]
            )

            denied = client.post(
                "/api/v1/motors/enable",
                headers=headers,
                json={"physical_estop_confirmed": False},
            )
            self.assertEqual(denied.status_code, 409)
            enabled = client.post(
                "/api/v1/motors/enable",
                headers=headers,
                json={"physical_estop_confirmed": True},
            )
            self.assertEqual(enabled.status_code, 200, enabled.text)
            self.assertTrue(
                enabled.json()["result"]["station"]["motors_enabled"]
            )

            disabled = client.post("/api/v1/motors/disable", headers=headers)
            self.assertEqual(disabled.status_code, 200, disabled.text)
            self.assertFalse(
                disabled.json()["result"]["station"]["motors_enabled"]
            )
            self.assertEqual(
                client.post("/api/v1/disconnect", headers=headers).status_code,
                200,
            )
            can_disconnected = client.post(
                "/api/v1/can/disconnect",
                headers=headers,
            )
            self.assertEqual(can_disconnected.status_code, 200)
            self.assertFalse(
                can_disconnected.json()["result"]["can"]["all_up"]
            )

    def test_default_service_runs_complete_fake_station_flow(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        config_path = repository / "scripts/config/arm_motors.yaml"
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "arm-aging.npz"
            np.savez(
                source,
                motor_pos=np.zeros((3, 7), dtype=np.float64),
                fps=np.asarray([100.0]),
                time=np.asarray([0.0, 0.01, 0.02]),
                limb=np.asarray(["left_arm"]),
            )
            with TestClient(
                create_app(data_root=Path(temp_dir) / "data")
            ) as client:
                headers = {"X-RP1-Client-ID": "operator-a"}
                configured = client.post(
                    "/api/v1/configure",
                    headers=headers,
                    json={
                        "config_path": str(config_path),
                        "limb": "left_arm",
                        "backend": "fake",
                    },
                )
                self.assertEqual(configured.status_code, 200, configured.text)
                uploaded = client.post(
                    "/api/v1/trajectory/upload",
                    headers={
                        **headers,
                        "X-Filename": source.name,
                        "Content-Type": "application/octet-stream",
                    },
                    content=source.read_bytes(),
                )
                self.assertEqual(uploaded.status_code, 200, uploaded.text)
                self.assertTrue(
                    uploaded.json()["result"]["preflight"]["safe"]
                )
                self.assertEqual(
                    client.post("/api/v1/connect", headers=headers).status_code,
                    200,
                )
                self.assertEqual(
                    client.post("/api/v1/arm", headers=headers).status_code,
                    200,
                )
                started = client.post(
                    "/api/v1/playback/start",
                    headers=headers,
                    json={
                        "speed": 1.0,
                        "loop": False,
                        "cycles": 0,
                        "record": False,
                    },
                )
                self.assertEqual(started.status_code, 200, started.text)
                deadline = time.monotonic() + 1.0
                state = ""
                while time.monotonic() < deadline:
                    state = client.get("/api/v1/snapshot").json()["state"]
                    if state == "completed":
                        break
                    time.sleep(0.01)
                self.assertEqual(state, "completed")
                reset = client.post(
                    "/api/v1/playback/reset",
                    headers=headers,
                )
                self.assertEqual(reset.status_code, 200, reset.text)
                self.assertEqual(reset.json()["snapshot"]["state"], "connected")
                disabled = client.post(
                    "/api/v1/playback/disable",
                    headers=headers,
                )
                self.assertEqual(disabled.status_code, 200, disabled.text)
                self.assertEqual(
                    disabled.json()["snapshot"]["state"],
                    "disconnected",
                )


if __name__ == "__main__":
    unittest.main()
