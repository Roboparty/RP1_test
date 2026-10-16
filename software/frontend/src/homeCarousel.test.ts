import { describe, expect, it } from "vitest";
import {
  caseSlot,
  circularIndex,
  initialCaseIndex,
  foldDegreeScopes,
  partsCompatible,
  recordedCases,
  shouldAutoAdvance,
  uniquePartScopes
} from "./homeCarousel";

const cases = [
  { code: "TC-01" },
  { code: "TC-02" },
  { code: "TC-03" },
  { code: "TC-04" }
];

describe("home Test Case carousel", () => {
  it("wraps previous and next indices", () => {
    expect(circularIndex(0, -1, cases.length)).toBe(3);
    expect(circularIndex(3, 1, cases.length)).toBe(0);
    expect(caseSlot(3, 0, cases.length)).toBe("previous");
    expect(caseSlot(1, 0, cases.length)).toBe("next");
  });

  it("selects the current case again when the object changes", () => {
    expect(initialCaseIndex(cases, "TC-03")).toBe(2);
    expect(initialCaseIndex(cases, "TC-01")).toBe(0);
  });

  it("falls back to the first case without a matching current code", () => {
    expect(initialCaseIndex(cases, null)).toBe(0);
    expect(initialCaseIndex(cases, "TC-MISSING")).toBe(0);
  });

  it("drops cases whose recorded duration is zero", () => {
    expect(recordedCases([
      { code: "REL-ARM-001", cumulative_duration_seconds: 0 },
      { code: "REL-ARM-002", cumulative_duration_seconds: 3600 }
    ]).map((item) => item.code)).toEqual(["REL-ARM-002"]);
  });

  it("never enables autoplay under reduced motion", () => {
    expect(shouldAutoAdvance({
      itemCount: cases.length,
      paused: false,
      focusPaused: false,
      hoverPaused: false,
      reducedMotion: true
    })).toBe(false);
    expect(shouldAutoAdvance({
      itemCount: cases.length,
      paused: false,
      focusPaused: false,
      hoverPaused: false,
      reducedMotion: false
    })).toBe(true);
  });

  it("shows full and reduced arm or leg scopes as one part", () => {
    const durations = {
      total_duration_seconds: 10,
      effective_exposure_seconds: 0,
      excluded_seconds: 0,
      pending_seconds: 0,
      active_elapsed_seconds: 0
    };
    const scope = (code: string, seconds: number) => ({
      id: `MODULE:${code}`,
      code,
      name: code,
      asset_kind: "MODULE",
      target_part_code: code,
      target_part_name: code,
      status: "COMPLETED",
      part_asset_count: 1,
      active_execution_count: 0,
      abnormal_asset_count: 0,
      part_durations: { ...durations, total_duration_seconds: seconds },
      test_cases: [{ id: code, target_part_code: code, target_part_name: code, cumulative_duration_seconds: seconds }]
    });
    const folded = foldDegreeScopes([scope("ARM", 7), scope("SARM", 5), scope("CHEST", 1)]);
    expect(folded.map((item) => item.target_part_name)).toEqual(["单臂", "CHEST"]);
    expect(folded[0].part_asset_count).toBe(2);
    expect(folded[0].part_durations.total_duration_seconds).toBe(12);
    expect(folded[0].code).toBe("ARM");
  });

  it("renders one selector for each part scope", () => {
    const scopes = uniquePartScopes([
      { asset_kind: "MODULE", target_part_code: "SLEG", label: "first leg" },
      { asset_kind: "MODULE", target_part_code: "SLEG", label: "second leg" },
      { asset_kind: "MODULE", target_part_code: "SARM", label: "arm" }
    ]);
    expect(scopes.map((scope) => scope.target_part_code)).toEqual(["SLEG", "SARM"]);
  });

  it("lets a full-degree case use the reduced-degree sample", () => {
    expect(partsCompatible("ARM", "SARM")).toBe(true);
    expect(partsCompatible("LEG", "SLEG")).toBe(true);
    expect(partsCompatible("ARM", "LEG")).toBe(false);
  });
});
