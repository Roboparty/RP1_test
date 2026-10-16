from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import math
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.errors import ApiError
from app.schemas.archives import ArchiveCreate, ArchiveVerification
from app.services.archive_storage import ArchiveStorage
from app.services.curve_query import CurveProcessor, CurveQueryService


def archive_payload(context=None,origin="HMI",raw=b"time_s,joint_pos_rad\n0,0\n1,1\n2,0\n",facts=None):
    end = datetime.now(timezone.utc)-timedelta(minutes=10)
    return {"execution_uuid": str(uuid4()),"test_name": "Synthetic test","actual_object": "Synthetic sample","station_label": "Synthetic station",
            "origin": origin,"filename": "synthetic.csv","size_bytes": len(raw),"sha256": hashlib.sha256(raw).hexdigest(),
            "facts": facts or {"started_at": (end-timedelta(seconds=2)).isoformat(),"ended_at": end.isoformat(),"active_seconds": 2,"completed_cycles": 1,
                      "machine_status": "completed","automatic_verdict": "unconfigured","data_quality": "VALID","clock_quality": "VALID","exception_count": 0,"summary": "Synthetic evidence"},
            "acceptance": {"criteria_version": "UNCONFIGURED"},"classification": context}


def test_archive_contract_rejects_evidence_editing_paths_nonfinite_and_fabricated_history_time():
    base = archive_payload()
    assert ArchiveCreate.model_validate(base).facts.active_seconds==2
    for field,value in [("filename","../secret.csv"),("filename","C:\\data\\secret.csv"),("filename","x.csv\x00"),("size_bytes",0),("sha256","a"*63)]:
        item = deepcopy(base); item[field] = value
        with pytest.raises(ValidationError):
            ArchiveCreate.model_validate(item)
    for field,value in [("active_seconds",float("nan")),("completed_cycles",-1),("machine_status","passed"),("raw_csv","forbidden")]:
        item = deepcopy(base); item["facts"][field] = value
        with pytest.raises(ValidationError):
            ArchiveCreate.model_validate(item)
    history = deepcopy(base);history["origin"]="HISTORY";history["facts"].update(started_at=None,ended_at=None,clock_quality="MISSING")
    assert ArchiveCreate.model_validate(history).facts.started_at is None
    history["facts"]["clock_quality"]="VALID"
    with pytest.raises(ValidationError):
        ArchiveCreate.model_validate(history)
    verification = {"expected_revision": 0,"tester": "QA","station_id": str(uuid4()),"verdict": "blocked","notes": "无","reason": "QA"}
    for field in ("analysis","active_seconds","completed_cycles","automatic_verdict","sha256"):
        with pytest.raises(ValidationError):
            ArchiveVerification.model_validate({**verification,field:"forbidden"})


def test_upload_retry_crash_tail_sequential_offsets_and_immutable_original(tmp_path):
    storage = ArchiveStorage(tmp_path)
    execution = uuid4()
    content = b"time_s,j_pos_rad\n0,0\n1,1\n"
    first = content[:10]
    assert storage.append(execution,0,len(content),0,first)==10
    # Simulate fsync succeeding just before transaction rollback.
    with storage.path(execution,"upload.part").open("ab") as handle:
        handle.write(b"uncommitted tail")
    assert storage.append(execution,10,len(content),0,first)==10
    assert storage.path(execution,"upload.part").read_bytes()==first
    with pytest.raises(ApiError) as conflict:
        storage.append(execution,10,len(content),0,b"different!")
    assert conflict.value.status_code==409
    with pytest.raises(ApiError):
        storage.append(execution,10,len(content),11,b"x")
    storage.append(execution,10,len(content),10,content[10:])
    assert storage.finalize(execution,len(content),hashlib.sha256(content).hexdigest())
    assert storage.path(execution,"raw.csv").read_bytes()==content
    assert storage.append(execution,len(content),len(content),0,first,complete=True)==len(content)
    with pytest.raises(ApiError):
        storage.append(execution,len(content),len(content),0,b"different!",complete=True)
    # A retry after rename and lost transaction remains successful.
    assert storage.finalize(execution,len(content),hashlib.sha256(content).hexdigest())


def test_checksum_failure_never_promotes_raw_and_can_restart(tmp_path):
    storage = ArchiveStorage(tmp_path); execution=uuid4(); content=b"bad CSV bytes"
    storage.append(execution,0,len(content),0,content)
    assert storage.finalize(execution,len(content),"a"*64) is False
    assert not storage.path(execution,"raw.csv").exists()
    assert storage.path(execution,"upload.part").read_bytes()==content
    # Simulate a crash before the CORRUPT/received=0 receipt commits.
    assert storage.finalize(execution,len(content),"a"*64) is False
    # After DB reset, append removes the old tail and resumes from zero.
    storage.append(execution,0,len(content),0,content)
    assert storage.finalize(execution,len(content),hashlib.sha256(content).hexdigest())


def test_storage_never_follows_uuid_directory_or_file_symlinks(tmp_path):
    storage=ArchiveStorage(tmp_path); execution=uuid4(); outside=tmp_path/"outside";outside.mkdir()
    (tmp_path/str(execution)).symlink_to(outside,target_is_directory=True)
    with pytest.raises(ApiError):
        storage.directory(execution)
    execution=uuid4();path=storage.path(execution,"upload.part");path.symlink_to(outside/"file")
    with pytest.raises(ApiError):
        storage.append(execution,0,10,0,b"bytes")


def processed(tmp_path,raw,acceptance=None):
    storage=ArchiveStorage(tmp_path); payload=archive_payload(raw=raw)
    if acceptance is not None:
        payload["acceptance"]=acceptance
    storage.path(payload["execution_uuid"],"raw.csv").write_bytes(raw)
    archive={**payload,"id": str(uuid4()),"archive_code": "ARC-SYNTHETIC","processing_state": "READY"}
    archive["manifest"]=CurveProcessor(storage).build(archive)
    return storage,archive


def test_minmax_view_preserves_single_sample_spike_and_zoom_recovers_raw(tmp_path):
    raw=("time_s,joint_cmd_pos_rad,joint_pos_rad,joint_temp_c\n"+"".join(f"{i/100},0,{99 if i==4321 else -7 if i==4330 else 0},{20+i/10000}\n" for i in range(10000))).encode()
    storage,archive=processed(tmp_path,raw)
    query=CurveQueryService(storage)
    overview=query.query(archive,["pos_rad"],[],None,None,100)
    points=overview["series"][0]["points"]
    assert len(points)<=100
    assert max(p[1] for p in points if p[1] is not None)==99
    assert min(p[1] for p in points if p[1] is not None)==-7
    zoom=query.query(archive,["pos_rad"],[],43.20,43.22,100)
    assert zoom["resolution"]=="raw"
    assert zoom["series"][0]["points"]==[[43.2,0.0],[43.21,99.0],[43.22,0.0]]
    assert archive["manifest"]["analysis"]["verdict"]=="unconfigured"
    assert "joint_tracking_error_rad" in [i["key"] for i in archive["manifest"]["catalog"]]


def test_series_budget_null_values_stage_cycle_and_time_gaps(tmp_path):
    rows=[]
    for i in range(2000):
        time=i/10 if i<1000 else i/10+100
        rows.append(f"{time},{'first' if i<1000 else 'second'},{i//1000},{'NaN' if i==200 else i},{-i}\n")
    storage,archive=processed(tmp_path,("time_s,stage_code,cycle_no,a_pos_rad,b_pos_rad\n"+"".join(rows)).encode())
    result=CurveQueryService(storage).query(archive,["pos_rad"],[],None,None,100)
    assert sum(len(s["points"]) for s in result["series"])<=100
    assert result["gaps"] and all(any(p[1] is None for p in s["points"]) for s in result["series"])
    zoom=CurveQueryService(storage).query(archive,["a_pos_rad"],[],19.9,20.1,100,stage="first",cycle="0")
    assert zoom["series"][0]["points"][1]==[20.0,None]
    with pytest.raises(ApiError):
        CurveQueryService(storage).query(archive,["pos_rad"],[],None,None,100,stage="missing")
    with pytest.raises(ApiError):
        CurveQueryService(storage).query(archive,["../raw.csv"],[],None,None,100)


def test_invalid_time_and_drive_fault_never_auto_pass(tmp_path):
    raw=b"time_s,joint_pos_rad,joint_error_id\n0,0,0\nNaN,1,0\n1,0,1\n1,0,1\n2,0,0\n"
    _,archive=processed(tmp_path,raw)
    assert archive["manifest"]["quality_status"]=="INVALID"
    assert archive["manifest"]["analysis"]["verdict"]=="failed"
    fault=next(m for m in archive["manifest"]["analysis"]["metrics"] if m["key"]=="joint_error_id")
    assert fault["fault_count"]==1
    assert archive["manifest"]["analysis"]["quality"]["invalid_time_rows"]==1


def test_peak_temperature_rise_cannot_be_hidden_by_final_cooling(tmp_path):
    raw=b"time_s,joint_pos_rad,joint_temp_c\n0,0,20\n1,0,120\n2,0,20\n"
    _,archive=processed(tmp_path,raw,{"criteria_version":"SYNTHETIC-PEAK/1","record_rate_hz":1,"required_active_seconds":2,
        "required_cycles":1,"min_sample_completeness":1,"max_gap_seconds":1.1,"max_temperature_rise_c":10})
    analysis=archive["manifest"]["analysis"]
    assert analysis["verdict"]=="failed"
    thermal=next(rule for rule in analysis["rules"] if rule["name"]=="joint_temp_c.rise")
    assert thermal["actual"]==100 and thermal["result"]=="failed"
    metric=next(metric for metric in analysis["metrics"] if metric["key"]=="joint_temp_c")
    assert metric["rise"]==100 and metric["end_rise"]==0


@pytest.mark.parametrize("raw",[b"time_s,x,x\n0,1,2\n",b"a,b\n0,1\n",b"time_s,x\n0,1,2\n",b"time_s,x\nNaN,1\n"])
def test_malformed_csv_keeps_original_and_rejects_query_publication(tmp_path,raw):
    storage=ArchiveStorage(tmp_path);payload=archive_payload(raw=raw);raw_path=storage.path(payload["execution_uuid"],"raw.csv");raw_path.write_bytes(raw)
    with pytest.raises(ValueError):
        CurveProcessor(storage).build(payload)
    assert raw_path.read_bytes()==raw
