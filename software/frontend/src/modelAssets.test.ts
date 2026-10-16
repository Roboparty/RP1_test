import { describe, expect, it } from "vitest";
import { getPartModelAsset } from "./modelAssets";

describe("part model assets", () => {
  it("maps available RP1 posters to canonical part codes", () => {
    expect(getPartModelAsset("SYS")?.poster).toContain("humanoid-robot");
    expect(getPartModelAsset("SARM")?.poster).toContain("single-arm");
    expect(getPartModelAsset("SLEG")?.poster).toContain("single-leg");
    expect(getPartModelAsset("UPPER")?.poster).toContain("upper-product");
    expect(getPartModelAsset("LOWER")?.poster).toContain("lower-product");
    expect(getPartModelAsset("CHEST")?.poster).toContain("torso");
    expect(getPartModelAsset("HEAD")?.poster).toContain("head");
    expect(getPartModelAsset("BAT")?.poster).toContain("bat");
  });
});
