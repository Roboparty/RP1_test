import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiRequest } from "./client";
import { getAllAssets } from "./api";
import type { AssetList, AssetSummary } from "./types";

vi.mock("./client", () => ({ apiRequest: vi.fn() }));

function page(ids: string[], nextCursor: string | null): AssetList {
  return {
    items: ids.map((id) => ({ id }) as AssetSummary),
    page: { limit: 100, next_cursor: nextCursor },
    as_of_at: "2026-09-22T00:00:00Z",
    data_cutoff_at: "2026-09-22T00:00:00Z"
  };
}

beforeEach(() => {
  vi.mocked(apiRequest).mockReset();
});

describe("complete asset directory", () => {
  it("loads every cursor page and removes duplicated rows", async () => {
    vi.mocked(apiRequest)
      .mockResolvedValueOnce(page(["asset-1", "asset-2"], "cursor-2"))
      .mockResolvedValueOnce(page(["asset-2", "asset-3"], null));

    const result = await getAllAssets({ assetKind: "MODULE" });

    expect(result.items.map((item) => item.id)).toEqual(["asset-1", "asset-2", "asset-3"]);
    expect(result.page).toEqual({ limit: 3, next_cursor: null });
    expect(vi.mocked(apiRequest).mock.calls[0][0]).toContain("limit=100");
    expect(vi.mocked(apiRequest).mock.calls[1][0]).toContain("cursor=cursor-2");
  });
});
