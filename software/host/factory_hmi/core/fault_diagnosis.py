"""Evidence-based fault diagnosis rules for factory aging stations.

Rules report *suspected* components only.  They intentionally avoid turning a
symptom (for example stale feedback) into an unsupported root-cause claim.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Iterable

from factory_hmi.models import FaultDiagnosis, MotorSample


@dataclass(frozen=True)
class DiagnosisThresholds:
    stale_seconds: float = 2.0
    tracking_error_rad: float = 0.5
    undervoltage_v: float | None = 36.0
    overtemperature_c: float = 85.0
    high_torque_nm: float = 35.0
    high_torque_seconds: float = 2.0
    temperature_rise_c: float = 10.0
    temperature_window_s: float = 30.0
    heartbeat_stale_seconds: float = 0.5

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "DiagnosisThresholds":
        hmi = config.get("factory_hmi", {})
        if hmi is None:
            hmi = {}
        if not isinstance(hmi, dict):
            raise ValueError("factory_hmi must be a mapping")
        raw = config.get("factory_hmi_faults", hmi.get("faults", {}))
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ValueError("factory_hmi_faults/factory_hmi.faults must be a mapping")
        defaults = cls()
        values: dict[str, Any] = {
            field: raw.get(field, getattr(defaults, field))
            for field in (
                "stale_seconds",
                "tracking_error_rad",
                "overtemperature_c",
                "high_torque_nm",
                "high_torque_seconds",
                "temperature_rise_c",
                "temperature_window_s",
                "heartbeat_stale_seconds",
            )
        }
        # Bus nominal voltage differs between assemblies. Do not infer it.
        values["undervoltage_v"] = raw.get("undervoltage_v")
        return cls(**values)


class FaultDiagnosisEngine:
    def __init__(self, thresholds: DiagnosisThresholds | None = None) -> None:
        self.thresholds = thresholds or DiagnosisThresholds()
        self._high_torque_since: dict[int, float] = {}
        self._temperatures: dict[int, deque[tuple[float, float]]] = defaultdict(
            deque
        )

    def reset(self) -> None:
        self._high_torque_since.clear()
        self._temperatures.clear()

    def diagnose(
        self,
        samples: Iterable[MotorSample],
        *,
        heartbeat_age_s: float | None = None,
        now_s: float | None = None,
    ) -> list[FaultDiagnosis]:
        now = time.monotonic() if now_s is None else float(now_s)
        current = list(samples)
        result: list[FaultDiagnosis] = []

        stale_by_bus: dict[str, list[MotorSample]] = defaultdict(list)
        for sample in current:
            if sample.error_id != 0:
                result.append(
                    FaultDiagnosis(
                        suspected_component=self._motor_label(sample),
                        evidence=(
                            f"motor-reported error_id={sample.error_id}",
                            f"joint={sample.joint_name or sample.index}",
                        ),
                        action="Stop motion; decode the vendor error and inspect this motor, wiring, and load.",
                        severity="critical",
                        confidence=0.95,
                        rule="motor_error",
                        affected_motor_ids=(int(sample.motor_id),),
                    )
                )

            if sample.feedback_age_s >= self.thresholds.stale_seconds:
                stale_by_bus[sample.bus or "unknown"].append(sample)

            tracking = self._tracking_error(sample)
            if tracking is not None and tracking >= self.thresholds.tracking_error_rad:
                result.append(
                    FaultDiagnosis(
                        suspected_component=self._motor_label(sample),
                        evidence=(
                            f"|command-position|={tracking:.3f} rad",
                            f"threshold={self.thresholds.tracking_error_rad:.3f} rad",
                        ),
                        action="Pause and inspect mechanical obstruction, gain limits, encoder feedback, and motor sizing.",
                        severity="warning",
                        confidence=0.65,
                        rule="tracking_error",
                        affected_motor_ids=(int(sample.motor_id),),
                    )
                )

            voltage = float(sample.bus_voltage_v)
            undervoltage = self.thresholds.undervoltage_v
            if (
                undervoltage is not None
                and math.isfinite(voltage)
                and voltage < float(undervoltage)
            ):
                result.append(
                    FaultDiagnosis(
                        suspected_component=f"power_or_bus:{sample.bus or 'unknown'}",
                        evidence=(
                            f"measured bus voltage={voltage:.2f} V",
                            f"threshold={float(undervoltage):.2f} V",
                        ),
                        action="Stop high-load motion and measure the supply, connector drop, and shared-bus load.",
                        severity="critical",
                        confidence=0.8,
                        rule="undervoltage",
                        affected_motor_ids=(int(sample.motor_id),),
                    )
                )

            temperature = float(sample.temp_c)
            if (
                math.isfinite(temperature)
                and temperature >= self.thresholds.overtemperature_c
            ):
                result.append(
                    FaultDiagnosis(
                        suspected_component=self._motor_label(sample),
                        evidence=(
                            f"motor temperature={temperature:.1f} C",
                            f"threshold={self.thresholds.overtemperature_c:.1f} C",
                        ),
                        action="Stop and cool the motor; inspect loading, cooling, and current limits before retrying.",
                        severity="critical",
                        confidence=0.9,
                        rule="motor_overtemperature",
                        affected_motor_ids=(int(sample.motor_id),),
                    )
                )

            persistent = self._persistent_load_diagnosis(sample, now)
            if persistent is not None:
                result.append(persistent)

        multi_bus_motor_indices: set[int] = set()
        for bus, stale_samples in stale_by_bus.items():
            if len(stale_samples) < 2:
                continue
            multi_bus_motor_indices.update(item.index for item in stale_samples)
            ids = ", ".join(str(item.motor_id) for item in stale_samples)
            age = min(item.feedback_age_s for item in stale_samples)
            result.append(
                FaultDiagnosis(
                    suspected_component=f"communication_bus:{bus}",
                    evidence=(
                        f"{len(stale_samples)} motors on the same bus have stale feedback",
                        f"motor_ids={ids}",
                        f"minimum feedback age={age:.3f} s",
                    ),
                    action="Stop commands; inspect the shared interface, termination, power, and bus traffic.",
                    severity="critical",
                    confidence=0.85,
                    rule="multi_motor_bus_stale",
                    affected_motor_ids=tuple(
                        int(item.motor_id) for item in stale_samples
                    ),
                )
            )

        for stale_samples in stale_by_bus.values():
            for sample in stale_samples:
                if sample.index in multi_bus_motor_indices:
                    continue
                result.append(
                    FaultDiagnosis(
                        suspected_component=self._motor_label(sample),
                        evidence=(
                            f"feedback age={sample.feedback_age_s:.3f} s",
                            f"threshold={self.thresholds.stale_seconds:.3f} s",
                        ),
                        action="Stop motion and inspect this motor's link, power, and feedback path.",
                        severity="critical",
                        confidence=0.7,
                        rule="single_motor_stale",
                        affected_motor_ids=(int(sample.motor_id),),
                    )
                )

        if (
            heartbeat_age_s is not None
            and math.isfinite(float(heartbeat_age_s))
            and float(heartbeat_age_s) >= self.thresholds.heartbeat_stale_seconds
        ):
            result.append(
                FaultDiagnosis(
                    suspected_component="control_dispatch_path",
                    evidence=(
                        f"controller heartbeat age={float(heartbeat_age_s):.3f} s",
                        f"threshold={self.thresholds.heartbeat_stale_seconds:.3f} s",
                    ),
                    action="Disable motors and inspect the control thread, scheduler latency, and backend dispatch errors.",
                    severity="critical",
                    confidence=0.9,
                    rule="controller_heartbeat_stale",
                )
            )

        return result

    def _persistent_load_diagnosis(
        self, sample: MotorSample, now: float
    ) -> FaultDiagnosis | None:
        index = int(sample.index)
        torque = abs(float(sample.torque_nm))
        if math.isfinite(torque) and torque >= self.thresholds.high_torque_nm:
            started = self._high_torque_since.setdefault(index, now)
        else:
            self._high_torque_since.pop(index, None)
            started = now

        temperatures = self._temperatures[index]
        temperature = float(sample.temp_c)
        if math.isfinite(temperature):
            temperatures.append((now, temperature))
        cutoff = now - self.thresholds.temperature_window_s
        while temperatures and temperatures[0][0] < cutoff:
            temperatures.popleft()
        rise = (
            temperature - min(item[1] for item in temperatures)
            if temperatures and math.isfinite(temperature)
            else 0.0
        )
        torque_duration = now - started
        if (
            torque >= self.thresholds.high_torque_nm
            and torque_duration >= self.thresholds.high_torque_seconds
            and rise >= self.thresholds.temperature_rise_c
        ):
            return FaultDiagnosis(
                suspected_component=f"load_or_mechanics:{self._motor_label(sample)}",
                evidence=(
                    f"|torque|={torque:.2f} Nm for {torque_duration:.2f} s",
                    f"temperature rise={rise:.1f} C within {self.thresholds.temperature_window_s:.1f} s",
                ),
                action="Pause and inspect binding, excess external load, lubrication, and commanded gains.",
                severity="critical",
                confidence=0.8,
                rule="persistent_torque_temperature_rise",
                affected_motor_ids=(int(sample.motor_id),),
            )
        if (
            torque >= self.thresholds.high_torque_nm
            and torque_duration >= self.thresholds.high_torque_seconds
        ):
            return FaultDiagnosis(
                suspected_component=f"load_or_mechanics:{self._motor_label(sample)}",
                evidence=(
                    f"|torque|={torque:.2f} Nm for {torque_duration:.2f} s",
                    f"threshold={self.thresholds.high_torque_nm:.2f} Nm for "
                    f"{self.thresholds.high_torque_seconds:.2f} s",
                ),
                action="Pause and inspect external load, binding, gains, and motor current limits.",
                severity="warning",
                confidence=0.65,
                rule="persistent_high_torque",
                affected_motor_ids=(int(sample.motor_id),),
            )
        if rise >= self.thresholds.temperature_rise_c:
            return FaultDiagnosis(
                suspected_component=f"thermal_or_load:{self._motor_label(sample)}",
                evidence=(
                    f"temperature rise={rise:.1f} C within "
                    f"{self.thresholds.temperature_window_s:.1f} s",
                    f"rise threshold={self.thresholds.temperature_rise_c:.1f} C",
                ),
                action="Pause and inspect cooling, load, friction, and current demand.",
                severity="warning",
                confidence=0.6,
                rule="temperature_rise",
                affected_motor_ids=(int(sample.motor_id),),
            )
        return None

    @staticmethod
    def _tracking_error(sample: MotorSample) -> float | None:
        command = float(sample.cmd_pos_rad)
        position = float(sample.pos_rad)
        if not math.isfinite(command) or not math.isfinite(position):
            return None
        return abs(command - position)

    @staticmethod
    def _motor_label(sample: MotorSample) -> str:
        return f"motor:{sample.motor_id}({sample.joint_name or sample.index})"


def diagnose_faults(
    samples: Iterable[MotorSample],
    *,
    heartbeat_age_s: float | None = None,
    thresholds: DiagnosisThresholds | None = None,
    now_s: float | None = None,
) -> list[FaultDiagnosis]:
    """Stateless convenience API for all non-persistence rules."""

    return FaultDiagnosisEngine(thresholds).diagnose(
        samples,
        heartbeat_age_s=heartbeat_age_s,
        now_s=now_s,
    )


__all__ = [
    "DiagnosisThresholds",
    "FaultDiagnosisEngine",
    "diagnose_faults",
]
