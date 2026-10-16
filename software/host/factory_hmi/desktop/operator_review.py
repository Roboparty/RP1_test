"""Build the operator verification text from a local summary.

Measured numbers are copied into the text. This module does not edit them
and does not read a raw CSV.
"""
from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID
import math

_OUTCOMES = {
    "成功": "passed",
    "失败": "failed",
    "中止": "blocked",
}
_VERDICT_LABELS = {"passed": "通过", "failed": "失败", "blocked": "中止／阻断", "unconfigured": "判据未配置完整"}


def automatic_status(summary: Mapping[str, object]) -> str:
    central = summary.get("central_analysis")
    if isinstance(central, Mapping) and central.get("verdict") in _VERDICT_LABELS:
        return str(central["verdict"])
    verdict = summary.get("verdict") or {}
    if verdict.get("criteria_version") == "UNCONFIGURED":
        return "unconfigured"
    return str(verdict.get("status") or "unconfigured")


def _analysis_verdict_lines(summary: Mapping[str, object]) -> str:
    local = summary.get("verdict") or {}
    local_status = "unconfigured" if local.get("criteria_version") == "UNCONFIGURED" else str(local.get("status") or "unconfigured")
    text = f"本机自动判定：{_VERDICT_LABELS.get(local_status, local_status)}。"
    central = summary.get("central_analysis")
    if isinstance(central, Mapping):
        verdict = str(central.get("verdict") or "unconfigured")
        text = f"中央自动判定：{_VERDICT_LABELS.get(verdict, verdict)}（{central.get('version') or '中央分析'}）。" + text
        if verdict != local_status:
            text += "中央与本机结论不同，人工核实以已显示的中央结论检查差异原因。"
        missing = [str(rule.get("name") or "") for rule in central.get("rules") or [] if isinstance(rule, Mapping) and rule.get("result") in {"unconfigured", "not_evaluated"}]
        if missing:
            text += "中央未配置规则：" + "、".join(missing[:5])[:150] + "。"
    return text


def _number(value: object, digits: int = 2) -> str:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "—"
    if number != number or number in {float("inf"), float("-inf")}:
        return "—"
    return f"{number:.{digits}f}"


def measured_lines(summary: Mapping[str, object] | None) -> str:
    """Read-only lines for the verification dialog."""
    if not isinstance(summary, Mapping):
        return "本机没有可读取的关节汇总，不能提交。请先重建本机报告；采集事实不能手工补写。"
    if not summary.get("joint_metrics"):
        return _analysis_verdict_lines(summary) + "本机未识别关节参数，采集事实仍只读；核实归档后由中央分析可识别参数。"
    timing = summary.get("timing") if isinstance(summary.get("timing"), Mapping) else {}
    quality = summary.get("quality") if isinstance(summary.get("quality"), Mapping) else {}
    lines = [
        _analysis_verdict_lines(summary),
        f"已运行 {_number(timing.get('active_seconds'), 1)} 秒",
        f"完成循环 {int(timing.get('completed_cycles') or 0)}",
        f"样本完整度 {_number(float(quality.get('sample_completeness') or 0) * 100, 1)}%",
        "关节汇总（只读）：",
    ]
    joints = summary.get("joint_metrics")
    if isinstance(joints, list):
        for item in joints:
            if not isinstance(item, Mapping):
                continue
            lines.append(
                f"- {item.get('joint_name')}：平均力矩 {_number(item.get('torque_mean_abs_nm'))} N·m，"
                f"峰值力矩 {_number(item.get('torque_peak_abs_nm'))} N·m，"
                f"峰值温升 {_number(item.get('temperature_peak_rise_c'), 1)} °C，"
                f"结束温升 {_number(item.get('temperature_rise_c'), 1)} °C，"
                f"峰值温度 {_number(item.get('temperature_peak_c'), 1)} °C，"
                f"跟踪 RMS {_number(float(item.get('tracking_rms_rad') or 0) * 180 / math.pi)}°，"
                f"最大误差 {_number(float(item.get('tracking_max_rad') or 0) * 180 / math.pi)}°，"
                f"故障区间 {int(item.get('fault_count') or 0)}"
            )
    return "\n".join(lines)


def analysis_text(summary: Mapping[str, object] | None) -> str:
    """Short analysis generated from the measured summary."""
    if not isinstance(summary, Mapping):
        return "本机摘要未生成，不能据此给出关节分析。"
    joints = [item for item in summary.get("joint_metrics") or [] if isinstance(item, Mapping)]
    if not joints:
        return _analysis_verdict_lines(summary) + "本机摘要没有关节汇总，不能据此给出关节分析。"
    peak = max(joints, key=lambda item: float(item.get("torque_peak_abs_nm") or 0))
    rise = max(joints, key=lambda item: float(item.get("temperature_peak_rise_c") or 0))
    faults = sum(int(item.get("fault_count") or 0) for item in joints)
    criteria = str((summary.get("verdict") or {}).get("criteria_version") or "UNCONFIGURED")
    timing = summary.get("timing") or {}
    quality = summary.get("quality") or {}
    tracking = max(float(item.get("tracking_rms_rad") or 0) for item in joints)
    return (
        _analysis_verdict_lines(summary)
        + f"运行 {_number(timing.get('active_seconds'), 1)} 秒，完成 {int(timing.get('completed_cycles') or 0)} 循环，"
        f"样本完整度 {_number(float(quality.get('sample_completeness') or 0) * 100, 1)}%。共 {len(joints)} 个关节。"
        f"峰值力矩最高为 {_number(peak.get('torque_peak_abs_nm'))} N·m（{peak.get('joint_name')}）。"
        f"峰值温升最高为 {_number(rise.get('temperature_peak_rise_c'), 1)} °C（{rise.get('joint_name')}，"
        f"该关节结束温升 {_number(rise.get('temperature_rise_c'), 1)} °C）。"
        f"故障计数 {faults}。"
        f"最高跟踪 RMS {_number(tracking * 180 / math.pi)}°。"
        f"判据版本：{'未配置' if criteria == 'UNCONFIGURED' else criteria}。"
    )


def review_package(
    *,
    tester: str,
    bench: str,
    outcome: str,
    notes: str,
    summary: Mapping[str, object] | None,
    station_id: str = "",
    discrepancy_reason: str = "",
    test_name: str = "",
    actual_object: str = "",
) -> dict[str, str]:
    """Editable identity plus a generated analysis. Measured values stay in summary."""
    person = tester.strip()
    station = bench.strip().upper()
    remark = notes.strip()
    if not person or not station:
        raise ValueError("请填写测试人和工位")
    if not test_name.strip() or not actual_object.strip() or actual_object.strip() == "unknown":
        raise ValueError("请填写测试名称和实际测试对象")
    if outcome not in _OUTCOMES:
        raise ValueError("请选择成功、失败或中止")
    if not remark:
        raise ValueError("测试员记录必填，无补充事项请填写“无”")
    if len(remark) > 300:
        raise ValueError("测试员记录最多 300 字")
    if not isinstance(summary, Mapping) or not isinstance(summary.get("timing"), Mapping):
        raise ValueError("本机摘要未读取，不能提交；请先重建报告")
    try:
        valid_station_id = str(UUID(station_id.strip()))
    except ValueError as exc:
        raise ValueError("请选择平台有效工位") from exc
    automatic_verdict = automatic_status(summary)
    discrepancy = discrepancy_reason.strip()
    if automatic_verdict != _OUTCOMES[outcome] and not discrepancy:
        raise ValueError("人工结论与自动判定不一致，必须填写差异原因")
    if len(discrepancy) > 300:
        raise ValueError("差异原因最多 300 字")
    analysis = analysis_text(summary)
    operator_summary = (
        f"核实人：{person}；工位：{station}；结果：{outcome}。"
        f"分析：{analysis} 备注：{remark or '无'}"
    )[:500]
    reason = f"核实：{person}，工位{station}，{outcome}。{remark}"[:300].strip("。")
    if len(reason) < 1:
        reason = f"核实：{person}，工位{station}，{outcome}"
    return {
        "tester": person[:80],
        "bench": bench.strip().upper()[:64],
        "station_id": valid_station_id,
        "outcome": outcome,
        "notes": remark,
        "analysis": analysis[:500],
        "discrepancy_reason": discrepancy,
        "test_name": test_name.strip()[:200],
        "actual_object": actual_object.strip()[:200],
        "verified_status": _OUTCOMES[outcome],
        "operator_summary": operator_summary,
        "submission_reason": reason[:300],
    }
