// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { AssetCreateForm, CatalogPage, PlanningPage } from "./SetupPages";
import { AssetsPage } from "./ReadModelPages";
import { setupApi } from "../setupApi";

const identity = vi.hoisted(() => ({ role: "SYSTEM_ADMIN" }));
vi.mock("../auth", () => ({ useAuth: () => ({ session: { user: identity } }) }));
vi.mock("../api", () => ({ getAllAssets: vi.fn(async () => ({ items: [], as_of_at: "2026-10-08T00:00:00Z", data_cutoff_at: null, page: { limit: 100, next_cursor: null } })) }));

const options = { parts: [{ code: "SLEG" as const, name: "单腿", asset_kind: "MODULE" as const }], programs: [], stations: [], assets: [], configurations: [] };
function view(element: React.ReactNode) {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={cache}>{element}</QueryClientProvider>);
}
afterEach(() => { cleanup(); vi.restoreAllMocks(); identity.role = "SYSTEM_ADMIN"; });

it("shows a published catalog without samples and preserves disabled catalog entries", async () => {
  vi.spyOn(setupApi, "catalog").mockResolvedValue([
    { id: "1", code: "REL-SLEG-001", name: "单腿耐久", asset_kind: "MODULE", target_part_code: "SLEG", target_part_name: "单腿", enabled: true, domain: "RELIABILITY", version_id: "v1", version: "1.0", procedure_spec: {} },
    { id: "2", code: "OLD-001", name: "停用项目", asset_kind: "MODULE", target_part_code: "SLEG", target_part_name: "单腿", enabled: false, domain: "RELIABILITY", version_id: null, version: null, procedure_spec: null }
  ]);
  const navigate = vi.fn();
  view(<CatalogPage assetKind="MODULE" navigate={navigate} />);
  await screen.findByText("单腿耐久");
  expect(screen.getByText("已停用")).toBeTruthy();
  fireEvent.click(screen.getByText("REL-SLEG-001"));
  expect(navigate).toHaveBeenCalledWith("1");
});

it("keeps all seven module groups with zero counts in an empty sample database", async () => {
  view(<AssetsPage assetKind="MODULE" part={null} selectPart={vi.fn()} clearPart={vi.fn()} navigate={vi.fn()} />);
  await screen.findByText("ARM");
  expect(screen.getAllByText("0 台")).toHaveLength(14);
  expect(screen.getByText("HEAD")).toBeTruthy();
});

it("shows empty plans and hides creation and station administration for viewers", async () => {
  identity.role = "VIEWER";
  vi.spyOn(setupApi, "campaigns").mockResolvedValue([]);
  vi.spyOn(setupApi, "options").mockResolvedValue(options);
  vi.spyOn(setupApi, "catalog").mockResolvedValue([]);
  view(<PlanningPage assetKind="MODULE" />);
  await screen.findByText("暂无可访问计划");
  expect(screen.queryByText("创建项目和计划")).toBeNull();
  expect(screen.queryByText("台架建档")).toBeNull();
});

it("reports save errors instead of claiming an invalid sample was created", async () => {
  vi.spyOn(setupApi, "options").mockResolvedValue(options);
  const create = vi.spyOn(setupApi, "create").mockRejectedValue(new Error("编号已存在"));
  view(<AssetCreateForm assetKind="MODULE" />);
  fireEvent.click(screen.getByText("新增样品"));
  const code = await screen.findByLabelText(/样品编号/);
  fireEvent.change(code, { target: { value: "RP1.3-SLEG-001" } });
  fireEvent.submit(code.closest("form")!);
  await screen.findByText("编号已存在");
  await waitFor(() => expect(create).toHaveBeenCalled());
  expect(screen.queryByText(/保存成功/)).toBeNull();
});
