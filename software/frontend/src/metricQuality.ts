const UNUSABLE_METRIC_QUALITIES = new Set(["INVALID", "MISSING"]);

export function isMetricQualityUsable(qualityStatus: string) {
  return !UNUSABLE_METRIC_QUALITIES.has(qualityStatus);
}
