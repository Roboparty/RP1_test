export type CarouselSlot = "previous" | "current" | "next" | "hidden";

export function circularIndex(index: number, step: number, itemCount: number) {
  if (itemCount <= 0) return 0;
  return (index + step + itemCount) % itemCount;
}

export function caseSlot(itemIndex: number, currentIndex: number, itemCount: number): CarouselSlot {
  if (itemCount <= 0) return "hidden";
  if (itemIndex === currentIndex) return "current";
  if (itemIndex === circularIndex(currentIndex, -1, itemCount)) return "previous";
  if (itemIndex === circularIndex(currentIndex, 1, itemCount)) return "next";
  return "hidden";
}

export function recordedCases<T extends { cumulative_duration_seconds: number }>(items: T[]) {
  return items.filter((item) => item.cumulative_duration_seconds > 0);
}

export function initialCaseIndex<T extends { code: string }>(
  items: T[],
  currentCode: string | null | undefined
) {
  if (!currentCode) return 0;
  const matchedIndex = items.findIndex((item) => item.code === currentCode);
  return matchedIndex >= 0 ? matchedIndex : 0;
}

export function shouldAutoAdvance({
  itemCount,
  paused,
  focusPaused,
  hoverPaused,
  reducedMotion
}: {
  itemCount: number;
  paused: boolean;
  focusPaused: boolean;
  hoverPaused: boolean;
  reducedMotion: boolean;
}) {
  return itemCount > 1 && !paused && !focusPaused && !hoverPaused && !reducedMotion;
}

export const MODULE_DIRECTORY = [
  { code: "ARM", members: ["ARM", "SARM"] },
  { code: "LEG", members: ["LEG", "SLEG"] },
  { code: "UPPER", members: ["UPPER"] },
  { code: "LOWER", members: ["LOWER"] },
  { code: "CHEST", members: ["CHEST"] },
  { code: "HEAD", members: ["HEAD"] },
  { code: "BAT", members: ["BAT"] }
] as const;

export function partsCompatible(casePart: string | null | undefined, assetPart: string | null | undefined) {
  if (!casePart || !assetPart) return false;
  if (casePart === assetPart) return true;
  return (casePart === "ARM" && assetPart === "SARM") || (casePart === "LEG" && assetPart === "SLEG");
}

const DEGREE_FAMILY: Record<string, { code: string; name: string }> = {
  ARM: { code: "ARM", name: "单臂" },
  SARM: { code: "ARM", name: "单臂" },
  LEG: { code: "LEG", name: "单腿" },
  SLEG: { code: "LEG", name: "单腿" }
};

const STATUS_RANK = ["RUNNING", "PAUSED", "BLOCKED", "FAILED", "COMPLETED", "NOT_STARTED"];

export function displayPartName(code: string, fallback = code) {
  return DEGREE_FAMILY[code]?.name ?? fallback;
}

type DegreeScope = {
  asset_kind: string;
  target_part_code: string;
  target_part_name: string;
  code: string;
  name: string;
  id: string;
  status: string;
  part_asset_count: number;
  active_execution_count: number;
  abnormal_asset_count: number;
  part_durations: {
    total_duration_seconds: number;
    effective_exposure_seconds: number;
    excluded_seconds: number;
    pending_seconds: number;
    active_elapsed_seconds: number;
  };
  test_cases: Array<{ id: string; target_part_code: string; target_part_name: string; cumulative_duration_seconds: number }>;
};

export function foldDegreeScopes<T extends DegreeScope>(scopes: T[]): T[] {
  const folded: T[] = [];
  const indexByFamily = new Map<string, number>();
  for (const scope of scopes) {
    const family = DEGREE_FAMILY[scope.target_part_code];
    if (!family) {
      folded.push(scope);
      continue;
    }
    const key = `${scope.asset_kind}:${family.code}`;
    const existingIndex = indexByFamily.get(key);
    if (existingIndex === undefined) {
      indexByFamily.set(key, folded.length);
      folded.push({
        ...scope,
        id: key,
        code: family.code,
        name: family.name,
        target_part_code: family.code,
        target_part_name: family.name,
        test_cases: scope.test_cases.map((item) => ({
          ...item,
          target_part_name: displayPartName(item.target_part_code, item.target_part_name)
        }))
      });
      continue;
    }
    const existing = folded[existingIndex];
    const durations = existing.part_durations;
    const incoming = scope.part_durations;
    const status = STATUS_RANK.find((item) => item === existing.status || item === scope.status) ?? existing.status;
    folded[existingIndex] = {
      ...existing,
      status,
      part_asset_count: existing.part_asset_count + scope.part_asset_count,
      active_execution_count: existing.active_execution_count + scope.active_execution_count,
      abnormal_asset_count: existing.abnormal_asset_count + scope.abnormal_asset_count,
      part_durations: {
        total_duration_seconds: durations.total_duration_seconds + incoming.total_duration_seconds,
        effective_exposure_seconds: durations.effective_exposure_seconds + incoming.effective_exposure_seconds,
        excluded_seconds: durations.excluded_seconds + incoming.excluded_seconds,
        pending_seconds: durations.pending_seconds + incoming.pending_seconds,
        active_elapsed_seconds: durations.active_elapsed_seconds + incoming.active_elapsed_seconds
      },
      test_cases: [
        ...existing.test_cases,
        ...scope.test_cases.map((item) => ({
          ...item,
          target_part_name: displayPartName(item.target_part_code, item.target_part_name)
        }))
      ]
    };
  }
  return folded;
}

export function uniquePartScopes<T extends { asset_kind: string; target_part_code: string }>(
  scopes: T[]
): T[] {
  const seen = new Set<string>();
  return scopes.filter((scope) => {
    const key = `${scope.asset_kind}:${scope.target_part_code}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}
