from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.integrations.influxdb import InfluxWriteError, write_lines
from app.repositories.influx_exports import retry_delay
from app.schemas.ingestion import TelemetryBatchRequest
from app.services.influx import telemetry_lines


def batch(metric='JOINT-ACTUAL-POSITION', unit='deg', value=1.0, quality='VALID'):
    timestamp = datetime(2026, 10, 6, tzinfo=timezone.utc)
    return TelemetryBatchRequest(producer_key='synthetic-batch', stage_id=uuid4(), series=[{
        'producer_series_key': 'synthetic-series', 'metric_code': metric,
        'subject_code': 'SYS-HIP-PITCH-L', 'canonical_unit': unit,
        'series_kind': 'STEADY_STATE_WINDOW', 'started_at': timestamp,
        'ended_at': timestamp, 'sampling_interval_ms': 100,
        'downsample_method': 'UNIFORM',
        'points': [{'sample_index': 0, 'observed_at': timestamp, 'value': value, 'quality_status': quality}],
    }])


def encode(payload):
    return telemetry_lines(payload, run_id='run-001', case_id='SYS-REL-001', source_kind='synthetic')


def test_retry_produces_same_tags_fields_and_timestamp():
    payload = batch()
    assert encode(payload) == encode(payload)
    lines, count = encode(payload)
    assert count == 1
    assert lines == 'joint_telemetry,run_id=run-001,case_id=SYS-REL-001,subject=SYS-HIP-PITCH-L,source_kind=synthetic pos_deg=1.0 1791244800000000000'


@pytest.mark.parametrize(('metric', 'unit', 'field'), [
    ('JOINT-TARGET-POSITION', 'deg', 'cmd_pos_deg'),
    ('JOINT-TRACKING-ERROR', 'deg', 'tracking_error_deg'),
    ('JOINT-TEMPERATURE', '°C', 'temp_c'),
    ('JOINT-VELOCITY', 'deg/s', 'spd_deg_s'),
    ('JOINT-TORQUE', 'N·m', 'torque_nm'),
])
def test_catalog_units_map_to_dashboard_fields(metric, unit, field):
    assert f' {field}=1.0 ' in encode(batch(metric, unit))[0]


def test_rad_is_rejected_without_implicit_conversion():
    with pytest.raises(ValueError, match='requires deg'):
        encode(batch(unit='rad'))


def test_invalid_missing_and_unsupported_metrics_are_not_written_as_zero():
    assert encode(batch(quality='INVALID')) == ('', 0)
    assert encode(batch(quality='MISSING')) == ('', 0)
    assert encode(batch('JOINT-CURRENT', 'A')) == ('', 0)


def test_nan_is_rejected_before_database_ingestion():
    with pytest.raises(ValidationError):
        batch(value=float('nan'))


def test_offset_timestamp_uses_same_utc_instant():
    payload = batch()
    instant = payload.series[0].points[0].observed_at
    payload.series[0].points[0].observed_at = instant.astimezone(timezone(timedelta(hours=8)))
    assert encode(payload) == encode(batch())


def test_naive_timestamp_is_rejected_for_projection():
    payload = batch()
    payload.series[0].points[0].observed_at = datetime(2026, 10, 6)
    with pytest.raises(ValueError, match='UTC offset'):
        encode(payload)


def test_tag_escaping_and_newline_rejection():
    payload = batch()
    payload.series[0].subject_code = 'hip left,1=2'
    assert 'subject=hip\\ left\\,1\\=2' in encode(payload)[0]
    payload.series[0].subject_code = 'hip\nforged'
    with pytest.raises(ValueError, match='line breaks'):
        encode(payload)


def test_write_uses_canonical_org_bucket_precision_and_secret_header():
    settings = Settings(influx_token=SecretStr('synthetic-token'), influx_url='http://influx.test')
    def respond(request):
        assert dict(request.url.params) == {'org': 'rp1_test', 'bucket': 'telemetry', 'precision': 'ns'}
        assert request.headers['Authorization'] == 'Token synthetic-token'
        assert request.content == b'example-line'
        return httpx.Response(204)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        write_lines(settings, 'example-line', client=client)


def test_remote_error_never_exposes_token_or_body():
    settings = Settings(influx_token=SecretStr('synthetic-token'))
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(401, text='synthetic-token'))) as client:
        with pytest.raises(InfluxWriteError, match='HTTP 401') as failure:
            write_lines(settings, 'example-line', client=client)
        assert 'synthetic-token' not in str(failure.value)


def test_transport_timeout_is_retryable_and_safe():
    settings = Settings(influx_token=SecretStr('synthetic-token'))
    def fail(request):
        raise httpx.ReadTimeout('remote message containing synthetic-token', request=request)
    with httpx.Client(transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(InfluxWriteError, match='ReadTimeout') as failure:
            write_lines(settings, 'example-line', client=client)
        assert 'synthetic-token' not in str(failure.value)


def test_retry_delay_is_bounded_even_after_extended_outage():
    assert [retry_delay(value) for value in [1, 2, 8, 9, 100000]] == [2, 4, 256, 300, 300]


@pytest.mark.parametrize(('target', 'subject_target', 'kind', 'metric', 'expected_error'), [
    ('SYS', 'SYS', 'JOINT', 'JOINT-ACTUAL-POSITION', None),
    ('SYS', 'SARM', 'JOINT', 'JOINT-ACTUAL-POSITION', 'subject_mismatch'),
    ('SARM', 'SYS', 'JOINT', 'JOINT-ACTUAL-POSITION', 'subject_mismatch'),
    ('SLEG', 'SARM', 'JOINT', 'JOINT-TEMPERATURE', 'subject_mismatch'),
    ('SARM', 'SARM', 'JOINT', 'JOINT-ACTUAL-POSITION', None),
    ('BAT', 'BAT', 'BATTERY_CHANNEL', 'JOINT-TEMPERATURE', None),
    ('BAT', 'BAT', 'BATTERY_CHANNEL', 'JOINT-ACTUAL-POSITION', 'subject_metric_mismatch'),
])
def test_subject_validation_preserves_whole_machine_and_module_boundaries(target, subject_target, kind, metric, expected_error):
    from app.core.errors import ApiError
    from app.repositories.ingestion import IngestionRepository
    class Result:
        def mappings(self):
            return self
        def first(self):
            return {'target_part_code': subject_target, 'subject_kind': kind}
    class Session:
        def execute(self, *_args, **_kwargs):
            return Result()
    repository = IngestionRepository(Session())
    if expected_error:
        with pytest.raises(ApiError) as failure:
            repository._validate_subject({'target_part_code': target}, 'example-subject', metric)
        assert failure.value.status_code == 422
        assert failure.value.code == expected_error
    else:
        repository._validate_subject({'target_part_code': target}, 'example-subject', metric)
