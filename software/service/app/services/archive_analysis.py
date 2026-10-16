"""Retrospective analysis from versioned published rules and frozen statistics."""
from copy import deepcopy
import math

from pydantic import ValidationError

from app.core.errors import ApiError
from app.schemas.archives import AcceptanceRules
from app.services.curve_query import CurveProcessor, Statistic


MAPPING_VERSION = "published-case-archive-acceptance/1"


def published_acceptance(source: dict) -> tuple[dict,str,str]:
    """Map only explicit machine-readable fields, never infer prose thresholds.

    Supported location is catalog.test_case_version.decision_rules.archive_acceptance.
    sampling_requirements.record_rate_hz may supply the typed sampling rate.
    The source catalog UUID is always the criteria identity, overriding display text.
    """
    decision = source.get("decision_rules")
    sampling = source.get("sampling_requirements")
    candidate = decision.get("archive_acceptance") if isinstance(decision,dict) else None
    if candidate is None:
        acceptance = AcceptanceRules().model_dump(mode="json")
        return acceptance,"UNCONFIGURED","该发布版本只有文字判据或未提供 decision_rules.archive_acceptance；未从文字推断数值阈值。"
    if not isinstance(candidate,dict):
        return AcceptanceRules().model_dump(mode="json"),"INVALID","发布版本的 archive_acceptance 不是固定字段对象，未用于自动判定。"
    candidate = dict(candidate)
    candidate["criteria_version"] = "CATALOG:"+str(source["id"])
    if candidate.get("record_rate_hz") is None and isinstance(sampling,dict) and "record_rate_hz" in sampling:
        candidate["record_rate_hz"] = sampling["record_rate_hz"]
    # Reject strings/bools as numeric rules; a formal schema must specify numbers.
    if any(value is not None and (isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value))
           for key,value in candidate.items() if key!="criteria_version"):
        return AcceptanceRules().model_dump(mode="json"),"INVALID","发布版本数值判据含非数值或非有限值，未用于自动判定。"
    try:
        acceptance = AcceptanceRules.model_validate(candidate).model_dump(mode="json")
    except ValidationError:
        return AcceptanceRules().model_dump(mode="json"),"INVALID","发布版本数值判据含未知字段或超出支持范围，未用于自动判定。"
    return acceptance,"MAPPED","仅映射该发布版本的固定数值字段；未配置项保留未配置，文字判据保留在来源快照中。"


def retrospective_analysis(archive: dict, source: dict) -> dict:
    manifest = archive.get("manifest",{})
    original = manifest.get("analysis",{})
    if archive["processing_state"]!="READY" or original.get("version") is None:
        raise ApiError(409,"analysis_not_ready","请先等待原始归档和基础分析完成。")
    acceptance,status,notes = published_acceptance(source)
    metrics = original.get("metrics")
    quality = deepcopy(original.get("quality",{}))
    required_quality = {"row_count","valid_time_rows","invalid_time_rows","non_monotonic","max_gap_seconds"}
    if not isinstance(metrics,list) or not required_quality.issubset(quality):
        raise ApiError(409,"analysis_statistics_missing","当前归档缺少可复算统计，请先重建基础分析。")
    stats = {}
    for metric in metrics:
        count = metric["count"]
        rms = metric.get("rms")
        stats[metric["key"]] = Statistic(count=count,missing=metric["missing"],mean=metric.get("mean") or 0,
            sum_squares=rms*rms*count if rms is not None and count else 0,
            minimum=metric.get("min"),maximum=metric.get("max"),first=metric.get("first"),last=metric.get("last"),
            faults=metric.get("fault_count") or 0)
    rate = acceptance.get("record_rate_hz")
    active = archive["facts"].get("active_seconds")
    expected = math.floor(active*rate)+1 if rate and active is not None else None
    quality["expected_sample_count"] = expected
    quality["sample_completeness"] = min(1.0,quality["valid_time_rows"]/expected) if expected else None
    report = CurveProcessor.analyze(archive["facts"],acceptance,quality,stats,honor_recorded_verdict=False)
    # The imported automatic verdict belongs to the original rules. Keep it as
    # provenance instead of treating it as a new-rules measurement failure.
    report.update(scope="RETROSPECTIVE_REFERENCE_ONLY",mapping_version=MAPPING_VERSION,
                  mapping_status=status,mapping_notes=notes,source_automatic_verdict=archive["facts"]["automatic_verdict"])
    if status!="MAPPED" and report["verdict"]=="passed":
        report["verdict"]="unconfigured"
    return {"acceptance":acceptance,"mapping_status":status,"mapping_notes":notes,"report":report,
            "original_analysis_version":original["version"]}
