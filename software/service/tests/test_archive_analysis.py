from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.archives import ArchiveAnalysisCreate
from app.services.archive_analysis import published_acceptance,retrospective_analysis
from test_archives import processed


RULES={"record_rate_hz":1,"required_active_seconds":2,"required_cycles":1,"min_sample_completeness":1,
       "max_gap_seconds":1.1,"max_temperature_rise_c":10,"max_tracking_rms_rad":1,"max_torque_abs_nm":100}


def source(decision,sampling=None):
    return {"id":str(uuid4()),"version":"Synthetic new version","decision_rules":decision,"sampling_requirements":sampling or {}}


def test_only_explicit_published_numeric_fields_are_mapped_and_source_identity_wins():
    published=source({"archive_acceptance":{**RULES,"criteria_version":"untrusted display label"}})
    acceptance,status,_=published_acceptance(published)
    assert status=="MAPPED"
    assert acceptance["criteria_version"]=="CATALOG:"+published["id"]
    assert acceptance["max_temperature_rise_c"]==10
    # A prose number must never turn into an invented formal threshold.
    acceptance,status,_=published_acceptance(source({"pass_criteria":"温升不得超过 10 °C，连续运行 2 s"}))
    assert status=="UNCONFIGURED" and acceptance["max_temperature_rise_c"] is None
    for value in ("10",True,float("inf"),-1):
        acceptance,status,_=published_acceptance(source({"archive_acceptance":{"max_temperature_rise_c":value}}))
        assert status=="INVALID" and acceptance["criteria_version"]=="UNCONFIGURED"
    assert published_acceptance(source({"archive_acceptance":{"unsupported_rule":10}}))[1]=="INVALID"
    accepted=published_acceptance(source({"archive_acceptance":{"max_temperature_rise_c":10}}, {"record_rate_hz":2}))[0]
    assert accepted["record_rate_hz"]==2


def test_reanalysis_uses_peak_temperature_and_preserves_original_facts_manifest_and_verdict(tmp_path):
    raw=b"time_s,joint_pos_rad,joint_temp_c\n0,0,20\n1,0,120\n2,0,20\n"
    _,archive=processed(tmp_path,raw)
    before=deepcopy(archive)
    analyzed=retrospective_analysis(archive,source({"archive_acceptance":RULES}))
    assert analyzed["report"]["verdict"]=="failed"
    rule=next(r for r in analyzed["report"]["rules"] if r["name"]=="joint_temp_c.rise")
    assert rule["actual"]==100 and rule["limit"]==10
    assert analyzed["report"]["scope"]=="RETROSPECTIVE_REFERENCE_ONLY"
    assert archive==before
    assert archive["manifest"]["analysis"]["verdict"]=="unconfigured"


def test_original_rule_failure_is_provenance_in_new_rule_analysis(tmp_path):
    raw=b"time_s,joint_cmd_pos_rad,joint_pos_rad,joint_temp_c,joint_torque_nm\n0,0,0,20,0\n1,0,0,30,0\n2,0,0,20,0\n"
    _,archive=processed(tmp_path,raw)
    archive["facts"]["automatic_verdict"]="failed"
    analyzed=retrospective_analysis(archive,source({"archive_acceptance":{**RULES,"max_temperature_rise_c":100}}))
    assert analyzed["report"]["verdict"]=="passed"
    assert analyzed["report"]["source_automatic_verdict"]=="failed"
    assert analyzed["report"]["timing"]["automatic_verdict"]=="failed"


def test_new_rule_with_missing_measurement_cannot_pass(tmp_path):
    _,archive=processed(tmp_path,b"time_s,joint_pos_rad\n0,0\n1,0\n2,0\n")
    analyzed=retrospective_analysis(archive,source({"archive_acceptance":RULES}))
    assert analyzed["report"]["verdict"]=="unconfigured"
    assert any(rule["name"]=="max_temperature_rise_c.missing_measurement" and rule["result"]=="unconfigured" for rule in analyzed["report"]["rules"])


def test_analysis_request_does_not_accept_manual_rules_or_measurements():
    payload={"test_case_version_id":str(uuid4()),"reason":"Synthetic reference analysis"}
    assert ArchiveAnalysisCreate.model_validate(payload)
    for key in ("rules","acceptance","report","active_seconds","verdict"):
        with pytest.raises(ValidationError):
            ArchiveAnalysisCreate.model_validate({**payload,key:{"max_temperature_rise_c":999}})
