import { describe, expect, it } from "vitest";
import {
  linePath,
  performancePointSegments,
  performanceSeriesStatistics,
  sharedValueDomain,
  valueTicks
} from "./performanceChart";
import type { PerformanceTrendSeries } from "./types";

const makeSeries = (values: number[]): PerformanceTrendSeries => ({
  stage_code: "STEADY",
  stage_name: "稳态",
  sequence_no: 1,
  status: "COMPLETED",
  min_value: Math.min(...values),
  max_value: Math.max(...values),
  avg_value: values.reduce((sum, value) => sum + value, 0) / values.length,
  points: values.map((value, index) => ({
    observed_at: "2026-09-18T00:00:00Z",
    elapsed_seconds: index,
    progress_percent: index * 50,
    value,
    quality_status: "VALID"
  }))
});

describe("performance chart geometry", () => {
  it("pads constant values and never emits NaN", () => {
    const series = makeSeries([4, 4, 4]);
    const domain = sharedValueDomain([series]);
    expect(domain.min).toBeLessThan(4);
    expect(domain.max).toBeGreaterThan(4);
    expect(linePath(series.points, 800, 280, domain)).not.toContain("NaN");
  });

  it("uses one shared domain for multiple stages", () => {
    expect(sharedValueDomain([makeSeries([10, 12]), makeSeries([2, 3])])).toEqual({
      min: 1.2,
      max: 12.8
    });
  });

  it("handles empty and non-finite points", () => {
    const domain = sharedValueDomain([]);
    expect(domain).toEqual({ min: 0, max: 1 });
    expect(linePath([], 800, 280, domain)).toBe("");
    expect(valueTicks(domain)).toHaveLength(5);
  });

  it("excludes invalid points from domains, statistics and connected paths", () => {
    const series = makeSeries([2, 999, 4]);
    series.points[1].quality_status = "INVALID";

    expect(sharedValueDomain([series])).toEqual({ min: 1.84, max: 4.16 });
    expect(performanceSeriesStatistics(series)).toEqual({ min: 2, max: 4, average: 3, count: 2 });
    expect(performancePointSegments(series.points)).toHaveLength(2);
  });
});
