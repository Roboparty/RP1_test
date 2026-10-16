"""Offline checks for the database execution code and durable receipt mapping."""
from datetime import datetime, timezone, timedelta
import sqlite3

import pytest
import yaml

from factory_hmi.core.identity import build_execution_code
from factory_hmi.sync.outbox import Outbox
from factory_hmi.sync.reliability_platform import load_test_profiles, profiles_from_setup


@pytest.mark.parametrize('started', [
    datetime(2026, 10, 10, 17, 30, tzinfo=timezone.utc),
    datetime(2026, 10, 11, 1, 30, tzinfo=timezone(timedelta(hours=8))),
    datetime(2026, 10, 10, 13, 30, tzinfo=timezone(timedelta(hours=-4))),
])
def test_database_shanghai_timestamp_and_uppercase_code(started):
    assert build_execution_code('rel-sarm-001', started, 'rp1.3-sarm-001', 'hmi-01') == 'REL-SARM-001_2610110130_RP1.3-SARM-001_HMI-01'


@pytest.mark.parametrize('case,sample,station', [
    ('left_arm', 'RP1.3-SARM-001', 'MP-01'),
    ('REL SARM 001', 'RP1.3-SARM-001', 'MP-01'),
    ('REL-SARM-001', 'RP1.3-LARM-001', 'MP-01'),
    ('REL-SARM-001', 'RP1.3-SARM-01', 'MP-01'),
    ('REL-SARM-001', 'RP1.3-SARM-0001', 'MP-01'),
    ('REL-SARM-001', 'RP1.3-SARM-001', 'factory_01'),
])
def test_database_rejects_noncanonical_components(case, sample, station):
    with pytest.raises(ValueError):
        build_execution_code(case, datetime.now(timezone.utc), sample, station)


def test_identifier_never_uses_implicit_host_timezone():
    with pytest.raises(ValueError, match='timezone'):
        build_execution_code('REL-SARM-001', datetime(2026, 10, 10), 'RP1.3-SARM-001', 'MP-01')


def profile():
    return {'key': 'arm', 'label': '真实测试关联', **{key: '11111111-1111-4111-8111-111111111111' for key in (
        'campaign_id', 'cycle_id', 'segment_id', 'asset_id', 'configuration_id', 'test_case_version_id', 'station_id'
    )}, 'test_case_code': 'REL-SARM-001', 'asset_code': 'RP1.3-SARM-001', 'station_code': 'MP-01'}


def test_profile_uses_catalog_and_asset_codes(tmp_path):
    p = tmp_path / 'profiles.yaml'
    p.write_text(yaml.safe_dump({'profiles': [profile()]}))
    loaded, = load_test_profiles(p)
    assert loaded['ready']
    assert (loaded['test_case_id'], loaded['sample_id'], loaded['bench_id']) == ('REL-SARM-001', 'RP1.3-SARM-001', 'MP-01')


@pytest.mark.parametrize('field', ['test_case_code', 'asset_code', 'station_code'])
def test_uuid_only_profile_cannot_claim_canonical_identity(tmp_path, field):
    value = profile()
    del value[field]
    p = tmp_path / 'profiles.yaml'
    p.write_text(yaml.safe_dump({'profiles': [value]}))
    loaded, = load_test_profiles(p)
    assert not loaded['ready']


def test_conflicting_profile_aliases_are_not_silently_overridden(tmp_path):
    value = profile()
    value['sample_id'] = 'RP1.3-SARM-002'
    p = tmp_path / 'profiles.yaml'
    p.write_text(yaml.safe_dump({'profiles': [value]}))
    loaded, = load_test_profiles(p)
    assert not loaded['ready']
    assert any('编号不一致' in reason for reason in loaded['missing'])


def test_old_outbox_migration_keeps_rows_and_import_accepts_legacy_schema(tmp_path):
    source = tmp_path / 'old.sqlite3'
    bundle = tmp_path / 'bundle.tar.gz'
    bundle.write_bytes(b'offline placeholder')
    old = Outbox(source)
    old.enqueue({'execution_uuid': 'original-uuid', 'test_id': 'original-record'}, bundle)
    with sqlite3.connect(source) as connection:
        connection.execute('ALTER TABLE execution_outbox DROP COLUMN execution_code')
    target = Outbox(tmp_path / 'new.sqlite3')
    assert target.import_from(source) == 1
    assert target.get('original-uuid').test_id == 'original-record'
    assert target.get('original-uuid').execution_code == ''
    migrated = Outbox(source)
    code = 'REL-SARM-001_2610101800_RP1.3-SARM-001_MP-01_E42'
    migrated.update('original-uuid', execution_code=code, state='uploaded')
    reopened = Outbox(source)
    assert reopened.get('original-uuid').execution_code == code
    assert reopened.retry('original-uuid').state == 'uploaded'
    assert reopened.due() == []


def test_setup_profiles_use_case_code_and_initial_stage():
    campaign_id = "00000000-0000-7000-8000-000000000010"
    profiles = profiles_from_setup(
        [{
            "id": campaign_id,
            "status": "ACTIVE",
            "stages": [{"sequence_no": 1, "stage_name": "测试阶段1"}],
        }],
        {campaign_id: [{
            "id": "coverage-1",
            "status": "NOT_STARTED",
            "case_code": "REL-SARM-001",
            "asset_code": "RP1.3-SARM-001",
            "version": "CSV-20260918",
            "context": {
                "campaign_id": campaign_id,
                "cycle_id": "00000000-0000-7000-8000-000000000011",
                "segment_id": "00000000-0000-7000-8000-000000000012",
                "asset_id": "00000000-0000-7000-8000-000000000013",
                "configuration_id": "00000000-0000-7000-8000-000000000014",
                "test_case_version_id": "00000000-0000-7000-8000-000000000015",
                "station_id": "00000000-0000-7000-8000-000000000016",
            },
        }]},
        [{"id": "00000000-0000-7000-8000-000000000016", "code": "RD-01"}],
    )
    assert len(profiles) == 1
    assert profiles[0]["ready"] is True
    assert profiles[0]["case_code"] == "REL-SARM-001"
    assert profiles[0]["sample_id"] == "RP1.3-SARM-001"
    assert profiles[0]["stages"] == [{"sequence_no": 1, "stage_name": "测试阶段1"}]
