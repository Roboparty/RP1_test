"""Safe SocketCAN interface management for the Linux factory HMI.

Read-only status is obtained directly from netlink.  Operations that require
``CAP_NET_ADMIN`` are delegated to the small, policy-enforcing helper process.
No operation in this module invokes a shell.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import threading
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Protocol, Sequence


ARBITRATION_BITRATE = 1_000_000
DATA_BITRATE = 5_000_000
CAN_FD_ENABLED = True
# Match the known-good USB-CAN FD timing used by the production udev rule.
# Netlink represents sample points in one-tenth of one percent (800 = 80.0%).
_FACTORY_ARBITRATION_SAMPLE_POINT = 800
_FACTORY_ARBITRATION_SJW = 4
_FACTORY_DATA_SAMPLE_POINT = 750
_FACTORY_DATA_SJW = 2
_FACTORY_CAN_FD_MTU = 72
_FACTORY_TX_QUEUE_LENGTH = 10_000
ALLOWED_ARBITRATION_BITRATES = frozenset({125_000, 250_000, 500_000, 1_000_000})
ALLOWED_DATA_BITRATES = frozenset({1_000_000, 2_000_000, 4_000_000, 5_000_000})
INTERFACE_RE = re.compile(r"^can[0-9]+$")

PKEXEC_PATH = "/usr/bin/pkexec"
HELPER_PATH = os.environ.get(
    "RP1_FACTORY_CAN_HELPER_PATH",
    "/usr/lib/rp1-test-hmi/factory-hmi-can-helper",
)


class CanManagerError(RuntimeError):
    """Base error for SocketCAN management."""


class CanValidationError(CanManagerError, ValueError):
    """An interface name or CAN timing is outside the supported policy."""


class NetlinkUnavailableError(CanManagerError):
    """The production netlink implementation cannot be loaded."""


class HelperExecutionError(CanManagerError):
    """The privileged helper rejected or failed an operation."""


class CanOperationError(CanManagerError):
    """One or more requested interface operations failed."""


@dataclass(frozen=True)
class CanInterfaceStatus:
    """Normalized status for one SocketCAN interface."""

    interface: str
    exists: bool
    up: bool
    operstate: str | None
    bitrate: int | None
    dbitrate: int | None
    fd: bool | None
    bus_state: str | None
    tx_errors: int | None
    rx_errors: int | None
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["evidence"] = list(self.evidence)
        return result


def validate_interface(interface: object) -> str:
    """Return a validated SocketCAN interface name."""

    if not isinstance(interface, str) or INTERFACE_RE.fullmatch(interface) is None:
        raise CanValidationError(
            f"invalid CAN interface {interface!r}; expected ^can[0-9]+$"
        )
    return interface


def validate_interfaces(interfaces: object) -> tuple[str, ...]:
    """Validate a non-empty, duplicate-free interface list."""

    if isinstance(interfaces, (str, bytes)) or not isinstance(interfaces, Sequence):
        raise CanValidationError("interfaces must be a list of CAN interface names")
    names = tuple(validate_interface(item) for item in interfaces)
    if not names:
        raise CanValidationError("at least one CAN interface is required")
    if len(set(names)) != len(names):
        raise CanValidationError("duplicate CAN interfaces are not allowed")
    return names


def validate_timing(
    bitrate: object = ARBITRATION_BITRATE,
    dbitrate: object = DATA_BITRATE,
    fd: object = CAN_FD_ENABLED,
) -> tuple[int, int | None, bool]:
    """Validate and normalize a timing from the factory-safe allowlists."""

    if isinstance(bitrate, bool) or not isinstance(bitrate, int):
        raise CanValidationError("bitrate must be an integer")
    if bitrate not in ALLOWED_ARBITRATION_BITRATES:
        raise CanValidationError(
            "bitrate is not in the safe allowlist "
            f"{sorted(ALLOWED_ARBITRATION_BITRATES)}, got {bitrate}"
        )
    if not isinstance(fd, bool):
        raise CanValidationError("fd must be a boolean")

    if dbitrate is not None and (
        isinstance(dbitrate, bool) or not isinstance(dbitrate, int)
    ):
        raise CanValidationError("dbitrate must be an integer or null")
    if dbitrate is not None and dbitrate not in ALLOWED_DATA_BITRATES:
        raise CanValidationError(
            "dbitrate is not in the safe allowlist "
            f"{sorted(ALLOWED_DATA_BITRATES)}, got {dbitrate}"
        )
    if fd and dbitrate is None:
        raise CanValidationError("dbitrate is required when fd=true")

    # Data-phase timing has no meaning for Classical CAN.  Accepting a safe
    # legacy value here keeps older callers compatible, but the normalized
    # value is always None so it can never leak into netlink configuration.
    return bitrate, dbitrate if fd else None, fd


class NetlinkAdapter(ABC):
    """Injectable boundary around Linux netlink operations."""

    @abstractmethod
    def list_interfaces(self) -> tuple[str, ...]:
        """List existing SocketCAN-style interface names."""

    @abstractmethod
    def get_status(self, interface: str) -> CanInterfaceStatus:
        """Read one interface without changing it."""

    @abstractmethod
    def connect(
        self, interface: str, *, bitrate: int, dbitrate: int | None, fd: bool
    ) -> None:
        """Configure and bring up one interface."""

    @abstractmethod
    def disconnect(self, interface: str) -> None:
        """Bring down one interface."""

    @abstractmethod
    def recover(
        self, interface: str, *, bitrate: int, dbitrate: int | None, fd: bool
    ) -> None:
        """Cycle and reconfigure one interface, including from bus-off."""


def _attr(message: object, name: str, default: Any = None) -> Any:
    if hasattr(message, "get_attr"):
        value = message.get_attr(name)  # type: ignore[union-attr]
        return default if value is None else value
    if isinstance(message, Mapping):
        if name in message:
            return message[name]
        attrs = message.get("attrs")
        if isinstance(attrs, Sequence):
            for item in attrs:
                if (
                    isinstance(item, Sequence)
                    and not isinstance(item, (str, bytes))
                    and len(item) == 2
                    and item[0] == name
                ):
                    return item[1]
    return default


def _nested_can_data(link: object) -> object:
    link_info = _attr(link, "IFLA_LINKINFO", {})
    return _attr(link_info, "IFLA_INFO_DATA", {})


def _integer_field(value: object, *names: str) -> int | None:
    for name in names:
        candidate = _attr(value, name)
        if candidate is None and isinstance(value, Mapping):
            candidate = value.get(name)
        if isinstance(candidate, bool):
            continue
        try:
            return int(candidate)
        except (TypeError, ValueError):
            continue
    return None


class Pyroute2NetlinkAdapter(NetlinkAdapter):
    """Production netlink adapter backed by :mod:`pyroute2`."""

    _CAN_STATES = {
        0: "error-active",
        1: "error-warning",
        2: "error-passive",
        3: "bus-off",
        4: "stopped",
        5: "sleeping",
    }
    _CAN_CTRLMODE_FD = 0x20

    def __init__(self, iproute: Any | None = None) -> None:
        self._provided_iproute = iproute
        self._thread_local = threading.local()
        if iproute is not None:
            self._iproute_factory = None
            return
        try:
            from pyroute2 import IPRoute
        except ImportError as exc:
            raise NetlinkUnavailableError(
                "SocketCAN management requires pyroute2; install the 'pyroute2' "
                "package on the Linux HMI"
            ) from exc
        self._iproute_factory = IPRoute

    def _iproute(self) -> Any:
        if self._provided_iproute is not None:
            return self._provided_iproute
        current = getattr(self._thread_local, "iproute", None)
        if current is not None:
            return current
        assert self._iproute_factory is not None
        # uvicorn may install uvloop as the process-wide event-loop policy.
        # uvloop rejects AF_NETLINK sockets (family 16), while pyroute2 0.9+
        # creates its netlink transport through asyncio.  Give pyroute2 an
        # explicit standard selector loop so it can coexist with uvloop.
        event_loop = asyncio.SelectorEventLoop()
        try:
            current = self._iproute_factory(use_event_loop=event_loop)
        except Exception as exc:
            event_loop.close()
            raise NetlinkUnavailableError(
                f"failed to open the Linux netlink socket with pyroute2: {exc}"
            ) from exc
        self._thread_local.iproute = current
        self._thread_local.iproute_event_loop = event_loop
        return current

    def _links(self, interface: str) -> list[Any]:
        validate_interface(interface)
        try:
            return list(self._iproute().get_links(ifname=interface))
        except Exception as exc:
            raise CanOperationError(
                f"failed to query netlink status for {interface}: {exc}"
            ) from exc

    def list_interfaces(self) -> tuple[str, ...]:
        try:
            links = self._iproute().get_links()
        except Exception as exc:
            raise CanOperationError(
                f"failed to enumerate CAN interfaces through netlink: {exc}"
            ) from exc
        names = {
            name
            for link in links
            if isinstance((name := _attr(link, "IFLA_IFNAME")), str)
            and INTERFACE_RE.fullmatch(name) is not None
        }
        return tuple(sorted(names, key=lambda name: int(name[3:])))

    def _required_index(self, interface: str) -> int:
        links = self._links(interface)
        if not links:
            raise CanOperationError(f"CAN interface {interface} does not exist")
        index = links[0].get("index") if isinstance(links[0], Mapping) else None
        if index is None:
            index = getattr(links[0], "index", None)
        if isinstance(index, bool) or not isinstance(index, int):
            raise CanOperationError(
                f"netlink did not return a valid index for {interface}"
            )
        return index

    def get_status(self, interface: str) -> CanInterfaceStatus:
        interface = validate_interface(interface)
        links = self._links(interface)
        if not links:
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
                evidence=("netlink: interface not found",),
            )

        link = links[0]
        flags = link.get("flags", 0) if isinstance(link, Mapping) else 0
        operstate_raw = _attr(link, "IFLA_OPERSTATE")
        operstate = str(operstate_raw).lower() if operstate_raw is not None else None
        can_data = _nested_can_data(link)
        bitrate = _integer_field(
            _attr(can_data, "IFLA_CAN_BITTIMING", {}),
            "bitrate",
            "CAN_BITTIMING_BITRATE",
        )
        dbitrate = _integer_field(
            _attr(can_data, "IFLA_CAN_DATA_BITTIMING", {}),
            "bitrate",
            "CAN_BITTIMING_BITRATE",
        )

        ctrlmode = _attr(can_data, "IFLA_CAN_CTRLMODE")
        ctrlmode_fd = (
            ctrlmode.get("fd")
            if isinstance(ctrlmode, Mapping)
            else _attr(ctrlmode, "fd")
        )
        if isinstance(ctrlmode_fd, str) and ctrlmode_fd.lower() in {
            "on",
            "off",
            "yes",
            "no",
        }:
            fd = ctrlmode_fd.lower() in {"on", "yes"}
        elif isinstance(ctrlmode_fd, bool):
            fd = ctrlmode_fd
        else:
            ctrlmode_flags = _integer_field(
                ctrlmode,
                "flags",
                "CAN_CTRLMODE_FLAGS",
            )
            fd = (
                bool(ctrlmode_flags & self._CAN_CTRLMODE_FD)
                if ctrlmode_flags is not None
                else None
            )

        state_raw = _attr(can_data, "IFLA_CAN_STATE")
        if isinstance(state_raw, str):
            bus_state = state_raw.lower().replace("_", "-")
        else:
            try:
                state_number = int(state_raw)
            except (TypeError, ValueError):
                state_number = None
            bus_state = self._CAN_STATES.get(state_number)

        counters = _attr(can_data, "IFLA_CAN_BERR_COUNTER", {})
        tx_errors = _integer_field(counters, "txerr", "tx_errors")
        rx_errors = _integer_field(counters, "rxerr", "rx_errors")

        evidence = [f"netlink: operstate={operstate or 'unknown'}"]
        evidence.append(f"netlink: flags=0x{int(flags):x}")
        if bitrate is None or fd is None or (fd and dbitrate is None):
            evidence.append("netlink: one or more CAN timing attributes unavailable")
        else:
            evidence.append("netlink: CAN timing attributes present")
        if bus_state is not None:
            evidence.append(f"netlink: bus_state={bus_state}")

        return CanInterfaceStatus(
            interface=interface,
            exists=True,
            up=bool(int(flags) & 0x1),
            operstate=operstate,
            bitrate=bitrate,
            dbitrate=dbitrate,
            fd=fd,
            bus_state=bus_state,
            tx_errors=tx_errors,
            rx_errors=rx_errors,
            evidence=tuple(evidence),
        )

    def connect(
        self, interface: str, *, bitrate: int, dbitrate: int | None, fd: bool
    ) -> None:
        interface = validate_interface(interface)
        bitrate, dbitrate, fd = validate_timing(bitrate, dbitrate, fd)
        index = self._required_index(interface)
        try:
            iproute = self._iproute()
            iproute.link("set", index=index, state="down")
            timing_options: dict[str, Any] = {
                "kind": "can",
                "can_bittiming": {"bitrate": bitrate},
                "can_ctrlmode": {"fd": "on" if fd else "off"},
            }
            if fd:
                timing_options["can_data_bittiming"] = {"bitrate": dbitrate}
            if fd and bitrate == ARBITRATION_BITRATE and dbitrate == DATA_BITRATE:
                timing_options["can_bittiming"].update(
                    {
                        "sample_point": _FACTORY_ARBITRATION_SAMPLE_POINT,
                        "sjw": _FACTORY_ARBITRATION_SJW,
                    }
                )
                timing_options["can_data_bittiming"].update(
                    {
                        "sample_point": _FACTORY_DATA_SAMPLE_POINT,
                        "sjw": _FACTORY_DATA_SJW,
                    }
                )
            iproute.link("set", index=index, **timing_options)
            if fd and bitrate == ARBITRATION_BITRATE and dbitrate == DATA_BITRATE:
                iproute.link(
                    "set",
                    index=index,
                    mtu=_FACTORY_CAN_FD_MTU,
                    txqlen=_FACTORY_TX_QUEUE_LENGTH,
                )
            iproute.link("set", index=index, state="up")
        except Exception as exc:
            raise CanOperationError(
                f"netlink failed to connect {interface}: {exc}"
            ) from exc

    def disconnect(self, interface: str) -> None:
        interface = validate_interface(interface)
        index = self._required_index(interface)
        try:
            self._iproute().link("set", index=index, state="down")
        except Exception as exc:
            raise CanOperationError(
                f"netlink failed to disconnect {interface}: {exc}"
            ) from exc

    def recover(
        self, interface: str, *, bitrate: int, dbitrate: int | None, fd: bool
    ) -> None:
        # A down/configure/up cycle is supported by SocketCAN after bus-off and
        # also restores the approved timing if a device was re-enumerated.
        self.connect(interface, bitrate=bitrate, dbitrate=dbitrate, fd=fd)


class HelperClient(Protocol):
    """Protocol implemented by the privileged helper transport."""

    def execute(
        self,
        action: str,
        interfaces: Sequence[str],
        *,
        bitrate: int | None = None,
        dbitrate: int | None = None,
        fd: bool | None = None,
    ) -> Mapping[str, Any]:
        """Execute one validated helper request."""


class PkexecHelperClient:
    """Invoke the fixed, root-owned helper through polkit without a shell."""

    def __init__(
        self,
        *,
        pkexec_path: str = PKEXEC_PATH,
        helper_path: str = HELPER_PATH,
        timeout_s: float = 15.0,
    ) -> None:
        if not os.path.isabs(pkexec_path) or not os.path.isabs(helper_path):
            raise ValueError("pkexec and helper paths must be absolute")
        self.pkexec_path = pkexec_path
        self.helper_path = helper_path
        self.timeout_s = float(timeout_s)

    def execute(
        self,
        action: str,
        interfaces: Sequence[str],
        *,
        bitrate: int | None = None,
        dbitrate: int | None = None,
        fd: bool | None = None,
    ) -> Mapping[str, Any]:
        if os.environ.get("RP1_FACTORY_CAN_DISABLED") == "1":
            raise HelperExecutionError("CAN 配置未启用：当前包仅提供 PLC 通信，不配置物理 CAN")
        names = validate_interfaces(interfaces)
        request: dict[str, Any] = {"action": action, "interfaces": list(names)}
        if action in {"connect", "recover"}:
            actual_bitrate, actual_dbitrate, actual_fd = validate_timing(
                bitrate, dbitrate, fd
            )
            request.update(
                {
                    "bitrate": actual_bitrate,
                    "dbitrate": actual_dbitrate,
                    "fd": actual_fd,
                }
            )
        elif action not in {"status", "disconnect"}:
            raise CanValidationError(f"unsupported CAN helper action {action!r}")
        try:
            completed = subprocess.run(
                [self.pkexec_path, self.helper_path],
                input=json.dumps(request, separators=(",", ":")),
                text=True,
                capture_output=True,
                timeout=self.timeout_s,
                check=False,
            )
        except FileNotFoundError as exc:
            raise HelperExecutionError(
                f"CAN helper launcher not found: {exc.filename}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise HelperExecutionError(
                f"CAN helper timed out after {self.timeout_s:g} seconds"
            ) from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise HelperExecutionError(
                f"CAN helper failed with exit code {completed.returncode}"
                + (f": {detail}" if detail else "")
            )
        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise HelperExecutionError("CAN helper returned invalid JSON") from exc
        if not isinstance(response, Mapping) or response.get("ok") is not True:
            raise HelperExecutionError("CAN helper returned an unsuccessful response")
        return response


class CanManager:
    """Coordinate status and policy-mediated operations for interface lists."""

    def __init__(
        self,
        *,
        adapter: NetlinkAdapter | None = None,
        helper_client: HelperClient | None = None,
    ) -> None:
        self._adapter = adapter
        self._helper = helper_client or PkexecHelperClient()
        self._lock = threading.RLock()

    @property
    def adapter(self) -> NetlinkAdapter:
        # Delay the optional dependency check until status is actually needed.
        if self._adapter is None:
            self._adapter = Pyroute2NetlinkAdapter()
        return self._adapter

    def status(self, interfaces: Sequence[str]) -> list[CanInterfaceStatus]:
        names = validate_interfaces(interfaces)
        with self._lock:
            return [self.adapter.get_status(name) for name in names]

    def list_interfaces(self) -> tuple[str, ...]:
        """List existing ``canN`` interfaces through the netlink adapter."""

        with self._lock:
            return self.adapter.list_interfaces()

    def connect(
        self,
        interfaces: Sequence[str],
        *,
        bitrate: int = ARBITRATION_BITRATE,
        dbitrate: int | None = DATA_BITRATE,
        fd: bool = CAN_FD_ENABLED,
    ) -> list[CanInterfaceStatus]:
        names = validate_interfaces(interfaces)
        bitrate, dbitrate, fd = validate_timing(bitrate, dbitrate, fd)
        connected: list[str] = []
        with self._lock:
            try:
                for name in names:
                    self._helper.execute(
                        "connect",
                        [name],
                        bitrate=bitrate,
                        dbitrate=dbitrate,
                        fd=fd,
                    )
                    connected.append(name)
            except Exception as exc:
                rollback_errors: list[str] = []
                for name in reversed(connected):
                    try:
                        self._helper.execute("disconnect", [name])
                    except Exception as rollback_exc:
                        rollback_errors.append(f"{name}: {rollback_exc}")
                detail = (
                    "; rollback failures: " + ", ".join(rollback_errors)
                    if rollback_errors
                    else "; completed interfaces were rolled back"
                )
                raise CanOperationError(
                    f"failed to connect CAN interface set at {name}: {exc}{detail}"
                ) from exc
            return [self.adapter.get_status(name) for name in names]

    def disconnect(self, interfaces: Sequence[str]) -> list[CanInterfaceStatus]:
        names = validate_interfaces(interfaces)
        errors: list[str] = []
        with self._lock:
            for name in names:
                try:
                    self._helper.execute("disconnect", [name])
                except Exception as exc:
                    errors.append(f"{name}: {exc}")
            if errors:
                raise CanOperationError(
                    "failed to disconnect one or more CAN interfaces: "
                    + "; ".join(errors)
                )
            return [self.adapter.get_status(name) for name in names]

    def recover(
        self,
        interfaces: Sequence[str],
        *,
        bitrate: int = ARBITRATION_BITRATE,
        dbitrate: int | None = DATA_BITRATE,
        fd: bool = CAN_FD_ENABLED,
    ) -> list[CanInterfaceStatus]:
        names = validate_interfaces(interfaces)
        bitrate, dbitrate, fd = validate_timing(bitrate, dbitrate, fd)
        errors: list[str] = []
        with self._lock:
            for name in names:
                try:
                    self._helper.execute(
                        "recover",
                        [name],
                        bitrate=bitrate,
                        dbitrate=dbitrate,
                        fd=fd,
                    )
                except Exception as exc:
                    errors.append(f"{name}: {exc}")
            if errors:
                raise CanOperationError(
                    "failed to recover one or more CAN interfaces: " + "; ".join(errors)
                )
            return [self.adapter.get_status(name) for name in names]


__all__ = [
    "ALLOWED_ARBITRATION_BITRATES",
    "ALLOWED_DATA_BITRATES",
    "ARBITRATION_BITRATE",
    "CAN_FD_ENABLED",
    "DATA_BITRATE",
    "HELPER_PATH",
    "CanInterfaceStatus",
    "CanManager",
    "CanManagerError",
    "CanOperationError",
    "CanValidationError",
    "HelperClient",
    "HelperExecutionError",
    "NetlinkAdapter",
    "NetlinkUnavailableError",
    "PkexecHelperClient",
    "Pyroute2NetlinkAdapter",
    "validate_interface",
    "validate_interfaces",
    "validate_timing",
]
