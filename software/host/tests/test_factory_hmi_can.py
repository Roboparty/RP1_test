from __future__ import annotations

import io
import json
import subprocess
from typing import Any, Mapping, Sequence

import pytest

from factory_hmi.can_helper import (
    CanPermissionError,
    CanPolicy,
    CanPolicyError,
    execute_request,
    load_policy,
    main as helper_main,
)
from factory_hmi.core.can_manager import (
    ALLOWED_ARBITRATION_BITRATES,
    ALLOWED_DATA_BITRATES,
    ARBITRATION_BITRATE,
    DATA_BITRATE,
    CanInterfaceStatus,
    CanManager,
    CanOperationError,
    CanValidationError,
    NetlinkAdapter,
    PkexecHelperClient,
    Pyroute2NetlinkAdapter,
    validate_timing,
)


class FakeAdapter(NetlinkAdapter):
    def __init__(self, interfaces: Sequence[str] = ("can0", "can1")) -> None:
        self.states: dict[str, dict[str, Any]] = {
            interface: {
                "up": False,
                "operstate": "down",
                "bitrate": None,
                "dbitrate": None,
                "fd": None,
                "bus_state": "stopped",
                "tx_errors": 0,
                "rx_errors": 0,
            }
            for interface in interfaces
        }
        self.calls: list[tuple[Any, ...]] = []
        self.fail_connect: set[str] = set()

    def list_interfaces(self) -> tuple[str, ...]:
        self.calls.append(("list_interfaces",))
        return tuple(sorted(self.states))

    def get_status(self, interface: str) -> CanInterfaceStatus:
        self.calls.append(("status", interface))
        state = self.states.get(interface)
        if state is None:
            return CanInterfaceStatus(
                interface=interface,
                exists=False,
                up=False,
                operstate=None,
                bitrate=None,
                dbitrate=None,
                fd=None,
                bus_state=None,
                tx_errors=None,
                rx_errors=None,
                evidence=("fake: interface not found",),
            )
        return CanInterfaceStatus(
            interface=interface,
            exists=True,
            up=state["up"],
            operstate=state["operstate"],
            bitrate=state["bitrate"],
            dbitrate=state["dbitrate"],
            fd=state["fd"],
            bus_state=state["bus_state"],
            tx_errors=state["tx_errors"],
            rx_errors=state["rx_errors"],
            evidence=("fake: deterministic state",),
        )

    def connect(
        self, interface: str, *, bitrate: int, dbitrate: int | None, fd: bool
    ) -> None:
        self.calls.append(("connect", interface, bitrate, dbitrate, fd))
        if interface in self.fail_connect:
            raise RuntimeError(f"injected failure for {interface}")
        state = self.states[interface]
        state.update(
            {
                "up": True,
                "operstate": "up",
                "bitrate": bitrate,
                "dbitrate": dbitrate,
                "fd": fd,
                "bus_state": "error-active",
            }
        )

    def disconnect(self, interface: str) -> None:
        self.calls.append(("disconnect", interface))
        self.states[interface].update(
            {"up": False, "operstate": "down", "bus_state": "stopped"}
        )

    def recover(
        self, interface: str, *, bitrate: int, dbitrate: int | None, fd: bool
    ) -> None:
        self.calls.append(("recover", interface, bitrate, dbitrate, fd))
        self.states[interface].update(
            {
                "up": True,
                "operstate": "up",
                "bitrate": bitrate,
                "dbitrate": dbitrate,
                "fd": fd,
                "bus_state": "error-active",
                "tx_errors": 0,
                "rx_errors": 0,
            }
        )


class FakeHelperClient:
    def __init__(self, adapter: FakeAdapter) -> None:
        self.adapter = adapter
        self.calls: list[tuple[str, tuple[str, ...], dict[str, Any]]] = []

    def execute(
        self,
        action: str,
        interfaces: Sequence[str],
        *,
        bitrate: int | None = None,
        dbitrate: int | None = None,
        fd: bool | None = None,
    ) -> Mapping[str, Any]:
        names = tuple(interfaces)
        kwargs = {"bitrate": bitrate, "dbitrate": dbitrate, "fd": fd}
        self.calls.append((action, names, kwargs))
        for name in names:
            if action == "connect":
                self.adapter.connect(
                    name,
                    bitrate=int(bitrate),
                    dbitrate=None if dbitrate is None else int(dbitrate),
                    fd=bool(fd),
                )
            elif action == "disconnect":
                self.adapter.disconnect(name)
            elif action == "recover":
                self.adapter.recover(
                    name,
                    bitrate=int(bitrate),
                    dbitrate=None if dbitrate is None else int(dbitrate),
                    fd=bool(fd),
                )
            else:
                raise AssertionError(f"unexpected helper action {action}")
        return {"ok": True, "action": action, "interfaces": list(names)}


def approved_policy(*interfaces: str) -> CanPolicy:
    return CanPolicy(
        allowed_interfaces=frozenset(interfaces),
        bitrate=ARBITRATION_BITRATE,
        dbitrate=DATA_BITRATE,
        fd=True,
    )


def test_fake_status_contains_all_required_evidence_fields() -> None:
    adapter = FakeAdapter()
    manager = CanManager(adapter=adapter, helper_client=FakeHelperClient(adapter))

    status = manager.status(["can0", "can9"])

    assert status[0].to_dict() == {
        "interface": "can0",
        "exists": True,
        "up": False,
        "operstate": "down",
        "bitrate": None,
        "dbitrate": None,
        "fd": None,
        "bus_state": "stopped",
        "tx_errors": 0,
        "rx_errors": 0,
        "evidence": ["fake: deterministic state"],
    }
    assert status[1].interface == "can9"
    assert status[1].exists is False
    assert status[1].evidence


def test_manager_lists_interfaces_through_injected_adapter() -> None:
    adapter = FakeAdapter(("can3", "can0"))
    manager = CanManager(adapter=adapter, helper_client=FakeHelperClient(adapter))

    assert manager.list_interfaces() == ("can0", "can3")
    assert adapter.calls == [("list_interfaces",)]


class FakeIPRoute:
    def __init__(self) -> None:
        self.links = [
            {"index": 10, "attrs": [("IFLA_IFNAME", "can10")]},
            {"index": 2, "attrs": [("IFLA_IFNAME", "can2")]},
            {"index": 3, "attrs": [("IFLA_IFNAME", "vcan0")]},
            {"index": 4, "attrs": [("IFLA_IFNAME", "eth0")]},
            {"index": 5, "attrs": [("IFLA_IFNAME", "canx")]},
        ]
        self.link_calls: list[tuple[str, dict[str, Any]]] = []

    def get_links(self, *, ifname: str | None = None) -> list[dict[str, Any]]:
        if ifname is None:
            return list(self.links)
        return [
            link
            for link in self.links
            if dict(link["attrs"]).get("IFLA_IFNAME") == ifname
        ]

    def link(self, action: str, **kwargs: Any) -> None:
        self.link_calls.append((action, kwargs))


def test_pyroute2_adapter_enumerates_existing_can_number_interfaces() -> None:
    iproute = FakeIPRoute()
    adapter = Pyroute2NetlinkAdapter(iproute=iproute)

    assert adapter.list_interfaces() == ("can2", "can10")


def test_pyroute2_status_reads_named_fd_switch_from_current_pyroute2() -> None:
    iproute = FakeIPRoute()
    iproute.links = [
        {
            "index": 2,
            "flags": 1,
            "attrs": [("IFLA_IFNAME", "can2")],
            "IFLA_IFNAME": "can2",
            "IFLA_OPERSTATE": "UP",
            "IFLA_LINKINFO": {
                "IFLA_INFO_DATA": {
                    "IFLA_CAN_BITTIMING": {"bitrate": 1_000_000},
                    "IFLA_CAN_DATA_BITTIMING": {"bitrate": 5_000_000},
                    "IFLA_CAN_CTRLMODE": {"fd": "on"},
                    "IFLA_CAN_STATE": "ERROR-ACTIVE",
                    "IFLA_CAN_BERR_COUNTER": {"txerr": 0, "rxerr": 0},
                }
            },
        }
    ]
    adapter = Pyroute2NetlinkAdapter(iproute=iproute)

    status = adapter.get_status("can2")

    assert status.up is True
    assert status.fd is True
    assert status.bitrate == 1_000_000
    assert status.dbitrate == 5_000_000
    assert status.bus_state == "error-active"


def test_pyroute2_classic_configuration_omits_data_phase_and_fd_true() -> None:
    iproute = FakeIPRoute()
    adapter = Pyroute2NetlinkAdapter(iproute=iproute)

    adapter.connect("can2", bitrate=500_000, dbitrate=None, fd=False)

    configure = iproute.link_calls[1]
    assert configure == (
        "set",
        {
            "index": 2,
            "kind": "can",
            "can_bittiming": {"bitrate": 500_000},
            "can_ctrlmode": {"fd": "off"},
        },
    )
    assert "can_data_bittiming" not in configure[1]
    assert configure[1]["can_ctrlmode"]["fd"] == "off"


def test_pyroute2_can_fd_configuration_includes_data_phase() -> None:
    iproute = FakeIPRoute()
    adapter = Pyroute2NetlinkAdapter(iproute=iproute)

    adapter.connect("can2", bitrate=250_000, dbitrate=2_000_000, fd=True)

    assert iproute.link_calls[1][1] == {
        "index": 2,
        "kind": "can",
        "can_bittiming": {"bitrate": 250_000},
        "can_ctrlmode": {"fd": "on"},
        "can_data_bittiming": {"bitrate": 2_000_000},
    }


@pytest.mark.parametrize(
    "interface",
    [
        "vcan0",
        "can",
        "can-1",
        "can0;shutdown -h now",
        "can0 $(id)",
        "../can0",
        0,
    ],
)
def test_invalid_interface_is_rejected_before_adapter_use(interface: object) -> None:
    adapter = FakeAdapter()
    manager = CanManager(adapter=adapter, helper_client=FakeHelperClient(adapter))

    with pytest.raises(CanValidationError):
        manager.status([interface])  # type: ignore[list-item]

    assert adapter.calls == []


@pytest.mark.parametrize("bitrate", sorted(ALLOWED_ARBITRATION_BITRATES))
def test_validate_timing_accepts_safe_arbitration_bitrates(bitrate: int) -> None:
    assert validate_timing(bitrate, None, False) == (bitrate, None, False)


@pytest.mark.parametrize("dbitrate", sorted(ALLOWED_DATA_BITRATES))
def test_validate_timing_accepts_safe_can_fd_data_bitrates(dbitrate: int) -> None:
    assert validate_timing(500_000, dbitrate, True) == (
        500_000,
        dbitrate,
        True,
    )


def test_validate_timing_normalizes_legacy_classic_dbitrate_to_none() -> None:
    assert validate_timing(250_000, DATA_BITRATE, False) == (
        250_000,
        None,
        False,
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"bitrate": 750_000}, "bitrate is not in the safe allowlist"),
        ({"dbitrate": 3_000_000}, "dbitrate is not in the safe allowlist"),
        ({"fd": "true"}, "fd must be a boolean"),
        ({"bitrate": "1000000"}, "bitrate must be an integer"),
    ],
)
def test_invalid_timing_is_rejected_before_helper(
    kwargs: dict[str, Any], message: str
) -> None:
    adapter = FakeAdapter()
    helper = FakeHelperClient(adapter)
    manager = CanManager(adapter=adapter, helper_client=helper)

    with pytest.raises(CanValidationError, match=message):
        manager.connect(["can0"], **kwargs)

    assert helper.calls == []
    assert adapter.calls == []


def test_manager_classic_connect_and_recover_pass_normalized_none_dbitrate() -> None:
    adapter = FakeAdapter(("can0",))
    helper = FakeHelperClient(adapter)
    manager = CanManager(adapter=adapter, helper_client=helper)

    manager.connect(["can0"], bitrate=500_000, dbitrate=None, fd=False)
    manager.recover(["can0"], bitrate=250_000, dbitrate=None, fd=False)

    assert helper.calls[0][2] == {
        "bitrate": 500_000,
        "dbitrate": None,
        "fd": False,
    }
    assert helper.calls[1][2] == {
        "bitrate": 250_000,
        "dbitrate": None,
        "fd": False,
    }


def test_manager_rolls_back_completed_interfaces_on_partial_connect_failure() -> None:
    adapter = FakeAdapter()
    adapter.fail_connect.add("can1")
    helper = FakeHelperClient(adapter)
    manager = CanManager(adapter=adapter, helper_client=helper)

    with pytest.raises(CanOperationError, match="rolled back"):
        manager.connect(["can0", "can1"])

    assert [call[:2] for call in helper.calls] == [
        ("connect", ("can0",)),
        ("connect", ("can1",)),
        ("disconnect", ("can0",)),
    ]
    assert adapter.states["can0"]["up"] is False


def test_helper_rolls_back_partial_connect_failure() -> None:
    adapter = FakeAdapter()
    adapter.fail_connect.add("can1")
    request = {
        "action": "connect",
        "interfaces": ["can0", "can1"],
        "bitrate": ARBITRATION_BITRATE,
        "dbitrate": DATA_BITRATE,
        "fd": True,
    }

    with pytest.raises(CanOperationError, match="rolled back"):
        execute_request(
            request, policy=approved_policy("can0", "can1"), adapter=adapter
        )

    assert adapter.states["can0"]["up"] is False
    assert ("disconnect", "can0") in adapter.calls


def test_bus_off_recover_uses_helper_and_restores_approved_timing() -> None:
    adapter = FakeAdapter(("can0",))
    adapter.states["can0"].update(
        {
            "up": True,
            "operstate": "up",
            "bus_state": "bus-off",
            "tx_errors": 255,
            "rx_errors": 12,
        }
    )
    helper = FakeHelperClient(adapter)
    manager = CanManager(adapter=adapter, helper_client=helper)

    result = manager.recover(["can0"])

    assert helper.calls[0] == (
        "recover",
        ("can0",),
        {
            "bitrate": ARBITRATION_BITRATE,
            "dbitrate": DATA_BITRATE,
            "fd": True,
        },
    )
    assert result[0].bus_state == "error-active"
    assert result[0].bitrate == ARBITRATION_BITRATE
    assert result[0].dbitrate == DATA_BITRATE
    assert result[0].fd is True
    assert result[0].tx_errors == 0


def test_helper_refuses_interface_not_in_policy_without_adapter_call() -> None:
    adapter = FakeAdapter()
    request = {"action": "disconnect", "interfaces": ["can1"]}

    with pytest.raises(CanPermissionError, match="not allowed"):
        execute_request(request, policy=approved_policy("can0"), adapter=adapter)

    assert adapter.calls == []


def test_policy_requires_explicit_approved_timing(tmp_path: Any) -> None:
    policy_path = tmp_path / "can-policy.yaml"
    policy_path.write_text("interfaces: [can0]\nbitrate: 500000\n", encoding="utf-8")

    with pytest.raises(CanPolicyError, match="explicitly define"):
        load_policy(policy_path, require_root_owned=False)


def test_policy_allowlists_authorize_and_execute_requested_timing(
    tmp_path: Any,
) -> None:
    policy_path = tmp_path / "can-policy.yaml"
    policy_path.write_text(
        """
allowed_interfaces: [can0]
timing:
  allowed_bitrates: [250000, 500000]
  allowed_dbitrates: [1000000, 2000000]
  allow_classic_can: true
  allow_can_fd: true
""".lstrip(),
        encoding="utf-8",
    )
    policy = load_policy(policy_path, require_root_owned=False)
    adapter = FakeAdapter(("can0",))
    fd_request = {
        "action": "connect",
        "interfaces": ["can0"],
        "bitrate": 250_000,
        "dbitrate": 2_000_000,
        "fd": True,
    }

    execute_request(fd_request, policy=policy, adapter=adapter)
    execute_request(
        {
            "action": "recover",
            "interfaces": ["can0"],
            "bitrate": 500_000,
            "fd": False,
        },
        policy=policy,
        adapter=adapter,
    )

    assert adapter.calls[0] == (
        "connect",
        "can0",
        250_000,
        2_000_000,
        True,
    )
    assert ("recover", "can0", 500_000, None, False) in adapter.calls


@pytest.mark.parametrize(
    "timing_request",
    [
        {
            "action": "connect",
            "interfaces": ["can0"],
            "bitrate": 125_000,
            "dbitrate": 2_000_000,
            "fd": True,
        },
        {
            "action": "connect",
            "interfaces": ["can0"],
            "bitrate": 250_000,
            "dbitrate": 4_000_000,
            "fd": True,
        },
        {
            "action": "connect",
            "interfaces": ["can0"],
            "bitrate": 250_000,
            "fd": False,
        },
    ],
)
def test_policy_rejects_disallowed_requested_timing_without_adapter_call(
    timing_request: dict[str, Any],
) -> None:
    policy = CanPolicy(
        allowed_interfaces=("can0",),
        allowed_bitrates=(250_000,),
        allowed_dbitrates=(2_000_000,),
        allow_classic_can=False,
        allow_can_fd=True,
    )
    adapter = FakeAdapter(("can0",))

    with pytest.raises(CanPermissionError):
        execute_request(timing_request, policy=policy, adapter=adapter)

    assert adapter.calls == []


def test_legacy_single_classic_timing_policy_is_compatible(tmp_path: Any) -> None:
    policy_path = tmp_path / "can-policy.yaml"
    policy_path.write_text(
        """
interfaces: [can0]
timing:
  bitrate: 500000
  dbitrate: null
  fd: false
""".lstrip(),
        encoding="utf-8",
    )

    policy = load_policy(policy_path, require_root_owned=False)

    assert policy.allowed_bitrates == frozenset({500_000})
    assert policy.allowed_dbitrates == frozenset()
    assert policy.allow_classic_can is True
    assert policy.allow_can_fd is False


def test_json_helper_cli_uses_fake_adapter_only(tmp_path: Any) -> None:
    policy_path = tmp_path / "can-policy.yaml"
    policy_path.write_text(
        """
interfaces:
  can0:
    bitrate: 1000000
    dbitrate: 5000000
    fd: true
""".lstrip(),
        encoding="utf-8",
    )
    request = {
        "action": "connect",
        "interfaces": ["can0"],
        "bitrate": ARBITRATION_BITRATE,
        "dbitrate": DATA_BITRATE,
        "fd": True,
    }
    stdin = io.StringIO(json.dumps(request))
    stdout = io.StringIO()
    stderr = io.StringIO()
    adapter = FakeAdapter(("can0",))

    result = helper_main(
        [],
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        adapter=adapter,
        policy_path=policy_path,
        require_root_owned_policy=False,
        geteuid=lambda: 0,
    )

    assert result == 0
    assert stderr.getvalue() == ""
    response = json.loads(stdout.getvalue())
    assert response["ok"] is True
    assert response["status"][0]["up"] is True


def test_helper_cli_rejects_non_root_before_loading_policy() -> None:
    stderr = io.StringIO()

    result = helper_main(
        ["status", "can0"],
        stdin=io.StringIO(),
        stdout=io.StringIO(),
        stderr=stderr,
        adapter=FakeAdapter(),
        policy_path="/definitely/not/read",
        geteuid=lambda: 1000,
    )

    assert result == 77
    assert "must run as root" in stderr.getvalue()


def test_pkexec_client_uses_fixed_argv_and_json_without_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        observed["argv"] = argv
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout='{"ok":true,"action":"disconnect","interfaces":["can0"]}',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = PkexecHelperClient()

    client.execute("disconnect", ["can0"])

    assert observed["argv"] == [
        "/usr/bin/pkexec",
        "/usr/lib/rp1-test-hmi/factory-hmi-can-helper",
    ]
    assert "shell" not in observed["kwargs"]
    assert json.loads(observed["kwargs"]["input"]) == {
        "action": "disconnect",
        "interfaces": ["can0"],
    }


def test_pkexec_client_serializes_classic_timing_with_null_dbitrate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        observed["request"] = json.loads(kwargs["input"])
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout='{"ok":true,"action":"connect","interfaces":["can0"]}',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    PkexecHelperClient().execute(
        "connect",
        ["can0"],
        bitrate=500_000,
        dbitrate=None,
        fd=False,
    )

    assert observed["request"] == {
        "action": "connect",
        "interfaces": ["can0"],
        "bitrate": 500_000,
        "dbitrate": None,
        "fd": False,
    }
