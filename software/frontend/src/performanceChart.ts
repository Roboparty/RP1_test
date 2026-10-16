import type { PerformanceTrendPoint, PerformanceTrendSeries } from "./types";
import { isMetricQualityUsable } from "./metricQuality";

export type NumericDomain = { min: number; max: number };

export function isUsablePerformancePoint(point: PerformanceTrendPoint) {
  return Number.isFinite(point.progress_percent) &&
    Number.isFinite(point.value) &&
    isMetricQualityUsable(point.quality_status);
}

export function performancePointSegments(points: PerformanceTrendPoint[]) {
  const segments: PerformanceTrendPoint[][] = [];
  let current: PerformanceTrendPoint[] = [];
  for (const point of points) {
    if (!isUsablePerformancePoint(point)) {
      if (current.length > 0) segments.push(current);
      current = [];
      continue;
    }
    current.push(point);
  }
  if (current.length > 0) segments.push(current);
  return segments;
}

export function performanceSeriesStatistics(series: PerformanceTrendSeries) {
  const values = series.points
    .filter(isUsablePerformancePoint)
    .map((point) => point.value);
  if (values.length === 0) return null;
  return {
    min: Math.min(...values),
    max: Math.max(...values),
    average: values.reduce((sum, value) => sum + value, 0) / values.length,
    count: values.length
  };
}

export function sharedValueDomain(series: PerformanceTrendSeries[]): NumericDomain {
  const values = series.flatMap((item) =>
    item.points.filter(isUsablePerformancePoint).map((point) => point.value)
  );
  if (values.length === 0) return { min: 0, max: 1 };
  const min = Math.min(...values);
  const max = Math.max(...values);
  if (min === max) {
    const padding = Math.max(Math.abs(min) * 0.08, 1);
    return { min: min - padding, max: max + padding };
  }
  const padding = (max - min) * 0.08;
  return { min: min - padding, max: max + padding };
}

export function valueTicks(domain: NumericDomain, count = 5): number[] {
  const safeCount = Math.max(2, Math.floor(count));
  const span = domain.max - domain.min;
  if (!Number.isFinite(span) || span <= 0) return [0, 1];
  return Array.from({ length: safeCount }, (_, index) => domain.min + span * index / (safeCount - 1));
}

export function linePath(
  points: PerformanceTrendPoint[],
  width: number,
  height: number,
  domain: NumericDomain,
  inset = 24
): string {
  const finite = points.filter(isUsablePerformancePoint);
  const xSpan = Math.max(1, width - inset * 2);
  const ySpan = Math.max(1, height - inset * 2);
  const valueSpan = Math.max(Number.EPSILON, domain.max - domain.min);
  return finite.map((point, index) => {
    const progress = Math.max(0, Math.min(100, point.progress_percent));
    const x = inset + progress / 100 * xSpan;
    const y = inset + (domain.max - point.value) / valueSpan * ySpan;
    return `${index === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(" ");
}
