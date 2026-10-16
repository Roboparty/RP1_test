import { describe, expect, it } from "vitest";
import { mergeUniquePages, paginationKey } from "./pagination";

describe("read-model pagination", () => {
  it("appends pages in order and removes repeated records", () => {
    const merged = mergeUniquePages(
      [
        { items: [{ id: "1" }, { id: "2" }] },
        { items: [{ id: "2" }, { id: "3" }] }
      ],
      (item) => item.id
    );
    expect(merged.map((item) => item.id)).toEqual(["1", "2", "3"]);
  });

  it("changes cache identity when a filter changes", () => {
    expect(paginationKey("assets", "MODULE", "ACTIVE", "sample-a")).not.toEqual(
      paginationKey("assets", "WHOLE_MACHINE", "ACTIVE", "sample-a")
    );
    expect(paginationKey("assets", "MODULE", "ACTIVE", "sample-a")).not.toEqual(
      paginationKey("assets", "MODULE", "EXITED", "sample-a")
    );
  });
});
