import { describe, expect, it } from "vitest";
import type { JointAnalysisPoint, JointAnalysisSeries } from "../types";
import { analysisGroups, numericDomain, pointSegments } from "./JointAnalysisPanel";

function point(
  sampleIndex: number,
  progress: number | null,
  value: number | null,
  quality: JointAnalysisPoint["quality_status"] = "VALID"
): JointAnalysisPoint {
  return {
    sample_index: sampleIndex,
    elapsed_seconds: sampleIndex,
    progress_percent: progress,
    value,
    observed_at: `2026-09-18T00:00:${String(sampleIndex).padStart(2, "0")}Z`,
    quality_status: quality
  };
}

function series(
  code: string,
  metricName: string,
  unit: string,
  points: JointAnalysisPoint[]
): JointAnalysisSeries {
  return {
    id: code,
    metric_code: code,
    metric_name: metricName,
    unit,
    subject_code: "SLEG-KNEE-PITCH",
    stage_code: "STEADY_RUN",
    series_kind: "MOTION_CYCLE",
    cycle_index: 3,
    point_count: points.length,
    display_point_count: points.length,
    started_at: "2026-09-18T00:00:00Z",
    ended_at: "2026-09-18T00:00:10Z",
    sampling_interval_ms: 100,
    downsample_method: "deterministic-100-point",
    quality_status: "VALID",
    points
  };
}

describe("joint analysis chart helpers", () => {
  it("splits curves at invalid or missing samples", () => {
    const segments = pointSegments([
      point(0, 0, 1),
      point(1, 20, 2),
      point(2, 40, 3, "INVALID"),
      point(3, 60, 4),
      point(4, 80, null),
      point(5, 100, 5)
    ]);
    expect(segments).toEqual([[{ progress_percent: 0, value: 1 }, { progress_percent: 20, value: 2 }]]);
  });

  it("combines target and actual position while separating units", () => {
    const target = series("JOINT-TARGET-POSITION", "目标位置", "deg", [point(0, 0, 10)]);
    const actual = series("JOINT-ACTUAL-POSITION", "实际位置", "deg", [point(0, 0, 9.8)]);
    const torque = series("JOINT-TORQUE", "力矩", "N·m", [point(0, 0, 22)]);
    const groups = analysisGroups({
      execution_id: "execution",
      execution_code: "execution-code",
      subjects: [],
      stages: [],
      cycles: [],
      metrics: [],
      resolved_selection: { subject_code: null, stage_code: null, cycle_index: null },
      summary: [],
      series: [target, actual, torque],
      as_of_at: "2026-09-18T00:00:00Z",
      data_cutoff_at: null
    }, [target.metric_code, actual.metric_code, torque.metric_code]);
    expect(groups.map((group) => [group.key, group.series.length, group.unit])).toEqual([
      ["position", 2, "deg"],
      ["JOINT-TORQUE", 1, "N·m"]
    ]);
  });

  it("pads constant chart domains", () => {
    const domain = numericDomain([
      series("JOINT-TORQUE", "力矩", "N·m", [point(0, 0, 5), point(1, 100, 5)])
    ]);
    expect(domain.min).toBeLessThan(5);
    expect(domain.max).toBeGreaterThan(5);
  });

  it("does not let invalid values stretch the visible chart domain", () => {
    const domain = numericDomain([
      series("JOINT-TORQUE", "力矩", "N·m", [
        point(0, 0, 4),
        point(1, 50, 10_000, "INVALID"),
        point(2, 100, 6)
      ])
    ]);
    expect(domain.max).toBeLessThan(10);
  });
});
