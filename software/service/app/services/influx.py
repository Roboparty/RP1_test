"""Canonical telemetry -> Influx OSS 2 line protocol (projection only)."""
from __future__ import annotations

import math
from datetime import datetime, timezone

from app.schemas.ingestion import TelemetryBatchRequest

# Units match the published PostgreSQL metric catalog; never silently convert.
FIELD_MAPPING = {
    'JOINT-TARGET-POSITION': ('cmd_pos_deg', 'deg'),
    'JOINT-ACTUAL-POSITION': ('pos_deg', 'deg'),
    'JOINT-TRACKING-ERROR': ('tracking_error_deg', 'deg'),
    'JOINT-TEMPERATURE': ('temp_c', '°C'),
    'JOINT-VELOCITY': ('spd_deg_s', 'deg/s'),
    'JOINT-TORQUE': ('torque_nm', 'N·m'),
}


def _tag(value: str) -> str:
    if '\n' in value or '\r' in value:
        raise ValueError('Influx tags must not contain line breaks')
    return value.replace('\\', '\\\\').replace(' ', '\\ ').replace(',', '\\,').replace('=', '\\=')


def _timestamp_ns(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('telemetry timestamps must include UTC offset')
    delta = value.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000


def telemetry_lines(
    payload: TelemetryBatchRequest, *, run_id: str, case_id: str,
    source_kind: str,
) -> tuple[str, int]:
    if source_kind not in {'real', 'synthetic'}:
        raise ValueError('source_kind must be real or synthetic')
    lines: list[str] = []
    for series in payload.series:
        mapping = FIELD_MAPPING.get(series.metric_code)
        if mapping is None or series.quality_status not in {'VALID', 'PARTIAL'}:
            continue
        field, unit = mapping
        if series.canonical_unit != unit:
            raise ValueError(f'{series.metric_code} requires {unit}')
        tags = ','.join(f'{key}={_tag(value)}' for key, value in [
            ('run_id', run_id), ('case_id', case_id),
            ('subject', series.subject_code), ('source_kind', source_kind),
        ])
        for point in series.points:
            if point.quality_status not in {'VALID', 'PARTIAL'}:
                continue
            if not math.isfinite(point.value):
                raise ValueError('non-finite telemetry is not exportable')
            lines.append(f'joint_telemetry,{tags} {field}={point.value!r} {_timestamp_ns(point.observed_at)}')
    return '\n'.join(lines), len(lines)
