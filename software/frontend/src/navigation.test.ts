import { describe, expect, it } from "vitest";
import { formatDate, formatDuration, isShowcaseValue, locationHref, readLocation } from "./navigation";

describe("read-model navigation", () => {
  it("round-trips a drill-down route and asset kind", () => {
    const href = locationHref({ view: "execution", kind: "MODULE", id: "EX-01", part: null });
    expect(readLocation(href)).toEqual({
      view: "execution",
      kind: "MODULE",
      id: "EX-01",
      part: null
    });
  });

  it("falls back to the home route for unknown views", () => {
    expect(readLocation("?view=fixture&kind=whole")).toEqual({
      view: "home",
      kind: "WHOLE_MACHINE",
      id: null,
      part: null
    });
  });

  it("builds a part-filtered asset center route", () => {
    const href = locationHref({ view: "assets", kind: "MODULE", id: null, part: "SLEG" });
    expect(href).toContain("view=assets");
    expect(href).toContain("kind=module");
    expect(href).toContain("part=SLEG");
  });

  it.each(["account", "access-request", "admin"] as const)(
    "round-trips the %s security route",
    (view) => {
      const href = locationHref({ view, kind: "WHOLE_MACHINE", id: null, part: null });
      expect(readLocation(href).view).toBe(view);
    }
  );
});

describe("duration presentation", () => {
  it("uses seconds as the API source unit", () => {
    expect(formatDuration(5400)).toBe("1.50 h");
    expect(formatDuration(90)).toBe("1.5 min");
  });

  it("does not invent a value for missing duration", () => {
    expect(formatDuration(undefined)).toBe("—");
  });

  it("does not throw when an upstream timestamp is malformed", () => {
    expect(formatDate("not-a-date")).toBe("无效时间");
  });
});

describe("showcase identity", () => {
  it("marks only explicitly prefixed demo records", () => {
    expect(isShowcaseValue("SHOWCASE-RP1-023")).toBe(true);
    expect(isShowcaseValue("DEMO-ASSET-01")).toBe(true);
    expect(isShowcaseValue("RP1-023")).toBe(false);
  });
});
