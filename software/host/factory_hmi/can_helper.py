"""Policy-enforcing privileged helper for SocketCAN administration.

The installed entry point is intended to be root-owned and launched by a
polkit rule.  It accepts either one JSON request on stdin or a strict argparse
command.  It never executes a shell or accepts a command fragment.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, IO, Iterable, Mapping, Sequence

import yaml

from factory_hmi.core.can_manager import (
    ALLOWED_ARBITRATION_BITRATES,
    ALLOWED_DATA_BITRATES,
    ARBITRATION_BITRATE,
    CAN_FD_ENABLED,
    DATA_BITRATE,
    CanInterfaceStatus,
    CanOperationError,
    CanValidationError,
    NetlinkAdapter,
    Pyroute2NetlinkAdapter,
    validate_interface,
    validate_interfaces,
    validate_timing,
)


POLICY_PATH = Path(
    os.environ.get("RP1_FACTORY_CAN_POLICY", "/etc/rp1-test-hmi/can-policy.yaml")
)
MAX_JSON_REQUEST_BYTES = 64 * 1024
SUPPORTED_ACTIONS = frozenset({"status", "connect", "disconnect", "recover"})


class CanHelperError(RuntimeError):
    """Base error for helper policy and request failures."""


class CanPolicyError(CanHelperError):
    """The installed policy is missing or unsafe."""


class CanPermissionError(CanHelperError, PermissionError):
    """The request is not authorized by the installed policy."""


def _validated_rate_set(
    value: object,
    *,
    field: str,
    safe_values: frozenset[int],
    allow_empty: bool = False,
) -> frozenset[int]:
    if isinstance(value, (str, bytes, Mapping)):
        raise CanValidationError(f"{field} must be a list of integer bitrates")
    try:
        rates = tuple(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise CanValidationError(f"{field} must be a list of integer bitrates") from exc
    if not rates and not allow_empty:
        raise CanValidationError(f"{field} must not be empty")
    if any(isinstance(rate, bool) or not isinstance(rate, int) for rate in rates):
        raise CanValidationError(f"{field} must contain only integers")
    unsupported = sorted(set(rates) - safe_values)
    if unsupported:
        raise CanValidationError(
            f"{field} contains unsafe bitrate(s): "
            + ", ".join(str(rate) for rate in unsupported)
        )
    return frozenset(rates)


@dataclass(frozen=True, init=False)
class CanPolicy:
    """Validated, fail-closed factory CAN policy."""

    allowed_interfaces: frozenset[str]
    allowed_bitrates: frozenset[int]
    allowed_dbitrates: frozenset[int]
    allow_classic_can: bool
    allow_can_fd: bool

    def __init__(
        self,
        allowed_interfaces: Iterable[str],
        *,
        allowed_bitrates: Iterable[int] | None = None,
        allowed_dbitrates: Iterable[int] | None = None,
        allow_classic_can: bool | None = None,
        allow_can_fd: bool | None = None,
        # Legacy constructor keywords mirror the old single-timing policy.
        bitrate: int | None = None,
        dbitrate: int | None = None,
        fd: bool | None = None,
    ) -> None:
        try:
            names = validate_interfaces(tuple(allowed_interfaces))
        except (TypeError, CanValidationError) as exc:
            raise CanValidationError(
                f"invalid policy interface whitelist: {exc}"
            ) from exc

        using_legacy_timing = all(
            value is None
            for value in (
                allowed_bitrates,
                allowed_dbitrates,
                allow_classic_can,
                allow_can_fd,
            )
        )
        if using_legacy_timing:
            actual_bitrate, actual_dbitrate, actual_fd = validate_timing(
                ARBITRATION_BITRATE if bitrate is None else bitrate,
                DATA_BITRATE if dbitrate is None else dbitrate,
                CAN_FD_ENABLED if fd is None else fd,
            )
            bitrates = frozenset({actual_bitrate})
            dbitrates = (
                frozenset({actual_dbitrate})
                if actual_dbitrate is not None
                else frozenset()
            )
            classic_allowed = not actual_fd
            fd_allowed = actual_fd
        else:
            if allowed_bitrates is None:
                raise CanValidationError(
                    "allowed_bitrates is required for an allowlist policy"
                )
            if bitrate is not None or dbitrate is not None or fd is not None:
                raise CanValidationError(
                    "do not mix single timing fields with timing allowlists"
                )
            bitrates = _validated_rate_set(
                allowed_bitrates,
                field="allowed_bitrates",
                safe_values=ALLOWED_ARBITRATION_BITRATES,
            )
            if not isinstance(allow_classic_can, bool):
                raise CanValidationError("allow_classic_can must be a boolean")
            if not isinstance(allow_can_fd, bool):
                raise CanValidationError("allow_can_fd must be a boolean")
            classic_allowed = allow_classic_can
            fd_allowed = allow_can_fd
            if not classic_allowed and not fd_allowed:
                raise CanValidationError(
                    "policy must allow Classical CAN, CAN-FD, or both"
                )
            dbitrates = _validated_rate_set(
                [] if allowed_dbitrates is None else allowed_dbitrates,
                field="allowed_dbitrates",
                safe_values=ALLOWED_DATA_BITRATES,
                allow_empty=not fd_allowed,
            )

        object.__setattr__(self, "allowed_interfaces", frozenset(names))
        object.__setattr__(self, "allowed_bitrates", bitrates)
        object.__setattr__(self, "allowed_dbitrates", dbitrates)
        object.__setattr__(self, "allow_classic_can", classic_allowed)
        object.__setattr__(self, "allow_can_fd", fd_allowed)

    @property
    def bitrate(self) -> int:
        """Preferred legacy bitrate for callers reading old policies."""

        if ARBITRATION_BITRATE in self.allowed_bitrates:
            return ARBITRATION_BITRATE
        return min(self.allowed_bitrates)

    @property
    def dbitrate(self) -> int | None:
        """Preferred legacy data bitrate, or ``None`` for classic-only policy."""

        if DATA_BITRATE in self.allowed_dbitrates:
            return DATA_BITRATE
        return min(self.allowed_dbitrates) if self.allowed_dbitrates else None

    @property
    def fd(self) -> bool:
        """Legacy mode preference; new code should use the allow flags."""

        return self.allow_can_fd

    def authorize(self, interfaces: Sequence[str]) -> tuple[str, ...]:
        names = validate_interfaces(interfaces)
        denied = [name for name in names if name not in self.allowed_interfaces]
        if denied:
            raise CanPermissionError(
                "CAN interface not allowed by policy: " + ", ".join(denied)
            )
        return names


PolicyTiming = tuple[frozenset[int], frozenset[int], bool, bool]


def _policy_timing(mapping: Mapping[str, Any]) -> PolicyTiming:
    timing = mapping.get("timing", mapping)
    if not isinstance(timing, Mapping):
        raise CanPolicyError("policy timing must be a mapping")

    allowlist_fields = {
        "allowed_bitrates",
        "allowed_dbitrates",
        "allow_classic_can",
        "allow_can_fd",
    }
    if allowlist_fields.intersection(timing):
        missing = [
            key
            for key in ("allowed_bitrates", "allow_classic_can", "allow_can_fd")
            if key not in timing
        ]
        if missing:
            raise CanPolicyError(
                "allowlist policy must explicitly define " + ", ".join(missing)
            )
        try:
            policy = CanPolicy(
                ("can0",),
                allowed_bitrates=timing["allowed_bitrates"],
                allowed_dbitrates=timing.get("allowed_dbitrates"),
                allow_classic_can=timing["allow_classic_can"],
                allow_can_fd=timing["allow_can_fd"],
            )
        except CanValidationError as exc:
            raise CanPolicyError(f"policy contains unsafe CAN timing: {exc}") from exc
        return (
            policy.allowed_bitrates,
            policy.allowed_dbitrates,
            policy.allow_classic_can,
            policy.allow_can_fd,
        )

    # Backward compatibility for the original single timing schema.
    missing = [key for key in ("bitrate", "dbitrate", "fd") if key not in timing]
    if missing:
        raise CanPolicyError("policy must explicitly define " + ", ".join(missing))
    try:
        bitrate, dbitrate, fd = validate_timing(
            timing["bitrate"], timing["dbitrate"], timing["fd"]
        )
    except CanValidationError as exc:
        raise CanPolicyError(f"policy contains unsafe CAN timing: {exc}") from exc
    return (
        frozenset({bitrate}),
        frozenset({dbitrate}) if dbitrate is not None else frozenset(),
        not fd,
        fd,
    )


def _parse_policy(data: object) -> CanPolicy:
    if not isinstance(data, Mapping):
        raise CanPolicyError("CAN policy must be a YAML mapping")

    interface_config = data.get("interfaces")
    if interface_config is None:
        interface_config = data.get("allowed_interfaces")
    if interface_config is None:
        raise CanPolicyError(
            "policy must define an interfaces or allowed_interfaces whitelist"
        )

    if isinstance(interface_config, Mapping):
        if not interface_config:
            raise CanPolicyError("CAN interface whitelist must not be empty")
        names: list[str] = []
        policy_timing: PolicyTiming | None = None
        for raw_name, raw_settings in interface_config.items():
            try:
                name = validate_interface(raw_name)
            except CanValidationError as exc:
                raise CanPolicyError(f"invalid interface in policy: {exc}") from exc
            if not isinstance(raw_settings, Mapping):
                raise CanPolicyError(f"policy settings for {name} must be a mapping")
            settings: dict[str, Any] = dict(data)
            settings.update(raw_settings)
            timing = _policy_timing(settings)
            if policy_timing is None:
                policy_timing = timing
            elif timing != policy_timing:
                raise CanPolicyError(
                    "all allowed interfaces must use the same approved timing"
                )
            names.append(name)
        assert policy_timing is not None
        allowed_bitrates, allowed_dbitrates, allow_classic_can, allow_can_fd = (
            policy_timing
        )
    elif isinstance(interface_config, Sequence) and not isinstance(
        interface_config, (str, bytes)
    ):
        try:
            names = list(validate_interfaces(interface_config))
        except CanValidationError as exc:
            raise CanPolicyError(f"invalid interface whitelist: {exc}") from exc
        allowed_bitrates, allowed_dbitrates, allow_classic_can, allow_can_fd = (
            _policy_timing(data)
        )
    else:
        raise CanPolicyError("policy interface whitelist must be a list or mapping")

    return CanPolicy(
        allowed_interfaces=frozenset(names),
        allowed_bitrates=tuple(allowed_bitrates),
        allowed_dbitrates=tuple(allowed_dbitrates),
        allow_classic_can=allow_classic_can,
        allow_can_fd=allow_can_fd,
    )


def load_policy(
    path: str | os.PathLike[str] = POLICY_PATH,
    *,
    require_root_owned: bool = True,
) -> CanPolicy:
    """Load a strict policy, optionally enforcing production file ownership."""

    policy_path = Path(path)
    try:
        file_stat = policy_path.stat()
    except OSError as exc:
        raise CanPolicyError(f"cannot read CAN policy {policy_path}: {exc}") from exc
    if require_root_owned:
        if file_stat.st_uid != 0:
            raise CanPolicyError(f"CAN policy must be owned by root: {policy_path}")
        if stat.S_IMODE(file_stat.st_mode) & 0o022:
            raise CanPolicyError(
                f"CAN policy must not be group- or world-writable: {policy_path}"
            )
    try:
        data = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise CanPolicyError(
            f"failed to parse CAN policy {policy_path}: {exc}"
        ) from exc
    return _parse_policy(data)


def _request_fields(request: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
    action = request.get("action")
    if not isinstance(action, str) or action not in SUPPORTED_ACTIONS:
        raise CanValidationError(
            "action must be one of status, connect, disconnect, recover"
        )
    allowed_fields = {"action", "interfaces"}
    if action in {"connect", "recover"}:
        allowed_fields.update({"bitrate", "dbitrate", "fd"})
    unknown = set(request) - allowed_fields
    if unknown:
        raise CanValidationError(
            "unknown request field(s): " + ", ".join(sorted(map(str, unknown)))
        )
    if "interfaces" not in request:
        raise CanValidationError("request must include interfaces")
    names = validate_interfaces(request["interfaces"])
    if action in {"connect", "recover"}:
        missing = [field for field in ("bitrate", "fd") if field not in request]
        if missing:
            raise CanValidationError(
                f"{action} request must include " + ", ".join(missing)
            )
        validate_timing(request["bitrate"], request.get("dbitrate"), request["fd"])
    return action, names


def authorize_request(
    policy: CanPolicy, request: Mapping[str, Any]
) -> tuple[str, tuple[str, ...]]:
    """Validate request shape, whitelist, and approved bit timing."""

    action, names = _request_fields(request)
    policy.authorize(names)
    if action in {"connect", "recover"}:
        request_timing = validate_timing(
            request["bitrate"], request.get("dbitrate"), request["fd"]
        )
        bitrate, dbitrate, fd = request_timing
        if bitrate not in policy.allowed_bitrates:
            raise CanPermissionError(
                f"requested bitrate {bitrate} is not allowed by CAN policy"
            )
        if fd:
            if not policy.allow_can_fd:
                raise CanPermissionError("CAN-FD is not allowed by CAN policy")
            if dbitrate not in policy.allowed_dbitrates:
                raise CanPermissionError(
                    f"requested dbitrate {dbitrate} is not allowed by CAN policy"
                )
        elif not policy.allow_classic_can:
            raise CanPermissionError("Classical CAN is not allowed by CAN policy")
    return action, names


def _status_dict(status: CanInterfaceStatus | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(status, CanInterfaceStatus):
        return status.to_dict()
    if isinstance(status, Mapping):
        return dict(status)
    raise CanOperationError("netlink adapter returned an invalid status object")


def execute_request(
    request: Mapping[str, Any],
    *,
    policy: CanPolicy,
    adapter: NetlinkAdapter,
) -> dict[str, Any]:
    """Authorize and execute one helper request."""

    action, names = authorize_request(policy, request)
    timing = (
        validate_timing(request["bitrate"], request.get("dbitrate"), request["fd"])
        if action in {"connect", "recover"}
        else None
    )
    if action == "status":
        statuses = [_status_dict(adapter.get_status(name)) for name in names]
        return {
            "ok": True,
            "action": action,
            "interfaces": list(names),
            "status": statuses,
        }

    if action == "connect":
        assert timing is not None
        bitrate, dbitrate, fd = timing
        connected: list[str] = []
        try:
            for name in names:
                adapter.connect(
                    name,
                    bitrate=bitrate,
                    dbitrate=dbitrate,
                    fd=fd,
                )
                connected.append(name)
        except Exception as exc:
            rollback_errors: list[str] = []
            for completed_name in reversed(connected):
                try:
                    adapter.disconnect(completed_name)
                except Exception as rollback_exc:
                    rollback_errors.append(f"{completed_name}: {rollback_exc}")
            suffix = (
                "; rollback failures: " + ", ".join(rollback_errors)
                if rollback_errors
                else "; completed interfaces were rolled back"
            )
            raise CanOperationError(
                f"failed to connect interface set at {name}: {exc}{suffix}"
            ) from exc
    elif action == "disconnect":
        errors: list[str] = []
        for name in names:
            try:
                adapter.disconnect(name)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        if errors:
            raise CanOperationError(
                "failed to disconnect one or more interfaces: " + "; ".join(errors)
            )
    elif action == "recover":
        assert timing is not None
        bitrate, dbitrate, fd = timing
        errors = []
        for name in names:
            try:
                adapter.recover(
                    name,
                    bitrate=bitrate,
                    dbitrate=dbitrate,
                    fd=fd,
                )
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        if errors:
            raise CanOperationError(
                "failed to recover one or more interfaces: " + "; ".join(errors)
            )

    statuses = [_status_dict(adapter.get_status(name)) for name in names]
    return {
        "ok": True,
        "action": action,
        "interfaces": list(names),
        "status": statuses,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="factory-hmi-can-helper",
        description="Policy-restricted SocketCAN administration helper",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    for action in ("status", "disconnect"):
        command = subparsers.add_parser(action)
        command.add_argument("interfaces", nargs="+")
    for action in ("connect", "recover"):
        command = subparsers.add_parser(action)
        command.add_argument("interfaces", nargs="+")
        command.add_argument("--bitrate", type=int, default=ARBITRATION_BITRATE)
        command.add_argument("--dbitrate", type=int, default=DATA_BITRATE)
        command.add_argument(
            "--fd",
            action=argparse.BooleanOptionalAction,
            default=CAN_FD_ENABLED,
        )
    return parser


def _request_from_argv(argv: Sequence[str]) -> dict[str, Any]:
    parsed = vars(_build_parser().parse_args(list(argv)))
    return parsed


def _request_from_stdin(stream: IO[str]) -> dict[str, Any]:
    payload = stream.read(MAX_JSON_REQUEST_BYTES + 1)
    if len(payload.encode("utf-8")) > MAX_JSON_REQUEST_BYTES:
        raise CanValidationError("JSON request is too large")
    if not payload.strip():
        raise CanValidationError("expected one JSON request on stdin")
    try:
        request = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise CanValidationError(f"invalid JSON request: {exc.msg}") from exc
    if not isinstance(request, Mapping):
        raise CanValidationError("JSON request must be an object")
    return dict(request)


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
    adapter: NetlinkAdapter | None = None,
    policy_path: str | os.PathLike[str] = POLICY_PATH,
    require_root_owned_policy: bool = True,
    geteuid: Any = os.geteuid,
) -> int:
    """Run the helper CLI; dependencies are injectable for Fake/Mock tests."""

    actual_argv = list(sys.argv[1:] if argv is None else argv)
    actual_stdin = sys.stdin if stdin is None else stdin
    actual_stdout = sys.stdout if stdout is None else stdout
    actual_stderr = sys.stderr if stderr is None else stderr
    try:
        if int(geteuid()) != 0:
            raise CanPermissionError(
                "CAN helper must run as root via the configured polkit action"
            )
        request = (
            _request_from_argv(actual_argv)
            if actual_argv
            else _request_from_stdin(actual_stdin)
        )
        policy = load_policy(policy_path, require_root_owned=require_root_owned_policy)
        actual_adapter = adapter or Pyroute2NetlinkAdapter()
        response = execute_request(request, policy=policy, adapter=actual_adapter)
        json.dump(response, actual_stdout, separators=(",", ":"))
        actual_stdout.write("\n")
        return 0
    except CanPermissionError as exc:
        json.dump({"ok": False, "error": str(exc)}, actual_stderr)
        actual_stderr.write("\n")
        return 77
    except CanValidationError as exc:
        json.dump({"ok": False, "error": str(exc)}, actual_stderr)
        actual_stderr.write("\n")
        return 64
    except CanPolicyError as exc:
        json.dump({"ok": False, "error": str(exc)}, actual_stderr)
        actual_stderr.write("\n")
        return 78
    except Exception as exc:
        json.dump({"ok": False, "error": str(exc)}, actual_stderr)
        actual_stderr.write("\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "POLICY_PATH",
    "CanHelperError",
    "CanPermissionError",
    "CanPolicy",
    "CanPolicyError",
    "authorize_request",
    "execute_request",
    "load_policy",
    "main",
]
