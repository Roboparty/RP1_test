// @vitest-environment jsdom
// Independent C-v3 interaction verification. Every archive and sample below is
// synthetic; no real backend, database, device, or production data is used.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { getInstanceByDom } from "echarts/core";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { archivesApi, type Archive, type ArchiveCatalogItem, type ArchiveCurves } from "../archiveApi";
import CurveAnalysisPage from "./CurveAnalysisPage";

vi.mock("../auth", () => ({ useAuth: () => ({ session: { user: { role: "TEST_EXECUTOR" } } }) }));
// Keep the real ECharts model, dataZoom events, component controls, and React
// Query lifecycle. Only substitute SVG for jsdom's unavailable Canvas surface.
vi.mock("echarts/core", async (importOriginal) => {
  const core = await importOriginal<typeof import("echarts/core")>();
  const { SVGRenderer } = await import("echarts/renderers");
  core.use([SVGRenderer]);
  core.setPlatformAPI({ measureText: (text) => ({ width: text.length * 7 }) });
  return { ...core, init: (host: HTMLElement) => core.init(host, undefined, { renderer: "svg", width: 900, height: 420 }) };
});

type Filters = Parameters<typeof archivesApi.curves>[1];
type ChartOption = {
  series: { id?: string; name?: string; data?: unknown[]; connectNulls?: boolean; xAxisIndex?: number; yAxisIndex?: number }[];
  grid: unknown[];
  dataZoom: { id?: string; startValue?: number; endValue?: number; xAxisIndex?: number[] | number }[];
};
const syntheticCatalog: ArchiveCatalogItem[] = [
  { key: "j1_torque_nm", subject: "j1", metric: "torque_nm", unit: "Nm" },
  { key: "j2_torque_nm", subject: "j2", metric: "torque_nm", unit: "Nm" },
  { key: "j1_temp_c", subject: "j1", metric: "temp_c", unit: "°C" },
  { key: "j1_pos_rad", subject: "j1", metric: "pos_rad", unit: "rad" },
  { key: "j1_cmd_pos_rad", subject: "j1", metric: "cmd_pos_rad", unit: "rad" },
];
function syntheticArchive(id = "synthetic-main", catalog = syntheticCatalog): Archive {
  return {
    id, execution_uuid: `execution-${id}`, archive_code: id === "synthetic-main" ? "ARC-SYN-001" : `ARC-${id}`,
    owner_public_id: "synthetic-owner", is_owner: true, can_edit: true, origin: "OTHER",
    test_name: "合成交互夹具（非测量数据）", actual_object: "虚拟样品", station_label: "合成工位",
    filename: "synthetic-only.csv", size_bytes: 1000, sha256: "a".repeat(64), received_bytes: 1000,
    upload_state: "COMPLETE", processing_state: "READY", processing_error: null,
    classification_state: "UNCLASSIFIED", revision: 1,
    facts: { started_at: "2026-10-12T00:00:00Z", ended_at: "2026-10-12T00:01:40Z", active_seconds: 100, completed_cycles: 2, exception_count: 0, machine_status: "completed", automatic_verdict: "unconfigured", data_quality: "PARTIAL", clock_quality: "VALID" },
    acceptance: { criteria_version: "UNCONFIGURED", max_torque_abs_nm: 8 },
    manifest: { catalog, time_range: { start: 0, end: 100 }, stages: [{ code: "warmup", name: "预热" }, { code: "load", name: "负载" }], cycles: [1, 2], row_count: 1001, analysis: { verdict: "unconfigured" } },
    current_verification: null, classification: null, submission_id: null, submission_revision: null,
    submission_status: null, execution_id: null, execution_code: null, created_at: "2026-10-12T00:02:00Z",
  };
}
function syntheticCurves(archive: Archive, filters: Filters): ArchiveCurves {
  const start = filters.start ?? 0, end = filters.end ?? 100;
  const narrow = end - start <= 20;
  const catalog = archive.manifest?.catalog ?? [];
  const gapStart = start + (end - start) / 2;
  return {
    archive_id: archive.id, execution_uuid: archive.execution_uuid, archive_code: archive.archive_code,
    range: { start, end }, catalog, time_range: { start: 0, end: 100 }, resolution: narrow ? "raw" : "minmax",
    gaps: [{ start: gapStart, end: gapStart + .1 }],
    series: catalog.filter((entry) => filters.metrics.split(",").includes(entry.key)).map((entry) => ({
      ...entry, points: [[start, 1], [gapStart - .1, 2], [gapStart, null], [gapStart + .1, 3], [end, 4]],
      sample_count: narrow ? 5 : 1001, returned_points: 5, downsampled: !narrow,
    })),
  };
}
const clients: QueryClient[] = [];
let records: Record<string, Archive>;

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("Independent fixtures prohibit network access")));
  const other = syntheticArchive("SYN-002", syntheticCatalog.map((column) => ({ ...column, key: column.key.replace("j1", "axisA").replace("j2", "axisB"), subject: column.subject.replace("j1", "axisA").replace("j2", "axisB") })));
  const mismatch = syntheticArchive("SYN-WRONG-UNIT", [{ key: "j1_torque_mnm", subject: "j1", metric: "torque_nm", unit: "mNm" }]);
  records = Object.fromEntries([syntheticArchive(), other, mismatch].map((entry) => [entry.id, entry]));
  vi.spyOn(archivesApi, "detail").mockImplementation(async (id) => records[id]);
  vi.spyOn(archivesApi, "list").mockResolvedValue({ items: [other, mismatch], next_cursor: null });
  vi.spyOn(archivesApi, "curves").mockImplementation(async (id, filters) => syntheticCurves(records[id], filters));
  // Any accidental write from the analysis page is an independent failure.
  for (const method of ["verify", "classify", "submit", "analyze"] as const) vi.spyOn(archivesApi, method).mockRejectedValue(new Error("Analysis interaction must not mutate business data"));
});
afterEach(() => {
  cleanup();
  for (const client of clients.splice(0)) client.clear();
  for (const method of ["verify", "classify", "submit", "analyze"] as const) expect(archivesApi[method]).not.toHaveBeenCalled();
  expect(fetch).not.toHaveBeenCalled();
  vi.restoreAllMocks(); vi.unstubAllGlobals();
});

function mount(id = "synthetic-main") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0, refetchOnWindowFocus: false } } });
  clients.push(client);
  const navigate = vi.fn();
  const node = (nextId: string) => <QueryClientProvider client={client}><CurveAnalysisPage id={nextId} navigate={navigate} /></QueryClientProvider>;
  return { ...render(node(id)), navigate, node };
}
function chart() {
  const host = document.querySelector<HTMLElement>(".time-series-canvas");
  expect(host).not.toBeNull();
  const instance = getInstanceByDom(host!);
  expect(instance).toBeTruthy();
  return instance!;
}
function option() { return chart().getOption() as unknown as ChartOption; }
async function ready() {
  await screen.findByLabelText("参数");
  await waitFor(() => expect(option().series.some((entry) => (entry.data?.length ?? 0) > 0)).toBe(true));
}
function change(label: string, value: string) { fireEvent.change(screen.getByLabelText(label), { target: { value } }); }
function openXRange() {
  const trigger = screen.queryByRole("button", { name: "X 范围" });
  if (trigger && !screen.queryByLabelText("时间起点")) fireEvent.click(trigger);
}
function applyXRange(start: string, end: string) {
  openXRange(); change("时间起点", start); change("时间终点", end);
  fireEvent.click(screen.getByRole("button", { name: "应用范围" }));
}
function reveal(buttonName: string) {
  const button = screen.getByRole("button", { name: buttonName, hidden: true });
  const details = button.closest("details");
  if (details && !details.open) fireEvent.click(details.querySelector("summary")!);
  return button;
}
function revealSubject(name: string) {
  const input = screen.getByLabelText(name);
  const details = input.closest("details");
  if (details && !details.open) fireEvent.click(details.querySelector("summary")!);
  return input;
}
async function addComparison() {
  change("查找测试记录", "ARC-SYN-002");
  fireEvent.click(screen.getByRole("button", { name: "查找对比记录" }));
  fireEvent.click(await screen.findByRole("button", { name: /ARC-SYN-002.*添加/ }));
  await waitFor(() => expect(archivesApi.curves).toHaveBeenCalledWith("SYN-002", expect.objectContaining({ metrics: "axisA_torque_nm" }), expect.any(AbortSignal)));
}

it("applies a narrower viewport once despite repeated clicks and resets to the full domain", async () => {
  mount(); await ready();
  applyXRange("10", "20");
  await waitFor(() => expect(archivesApi.curves).toHaveBeenCalledWith("synthetic-main", expect.objectContaining({ start: 10, end: 20 }), expect.any(AbortSignal)));
  applyXRange("10", "20"); applyXRange("10", "20");
  await waitFor(() => expect(screen.getAllByText(/原始分辨率/).length).toBeGreaterThan(0));
  expect(vi.mocked(archivesApi.curves).mock.calls.filter(([, filters]) => filters.start === 10 && filters.end === 20)).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "重置视图" }));
  await waitFor(() => expect(option().dataZoom.find((entry) => entry.id === "x-inside")?.startValue).toBe(0));
  expect(option().dataZoom.find((entry) => entry.id === "x-inside")?.endValue).toBe(100);
});

it("debounces real ECharts X zoom events, ignores Y-only zoom, and requests only the final viewport", async () => {
  mount(); await ready(); vi.mocked(archivesApi.curves).mockClear();
  act(() => {
    chart().dispatchAction({ type: "dataZoom", dataZoomId: "y-slider", start: 10, end: 90 });
    chart().dispatchAction({ type: "dataZoom", dataZoomId: "x-inside", startValue: 10, endValue: 40 });
    chart().dispatchAction({ type: "dataZoom", dataZoomId: "x-inside", startValue: 20, endValue: 30 });
  });
  await waitFor(() => expect(archivesApi.curves).toHaveBeenCalledWith("synthetic-main", expect.objectContaining({ start: 20, end: 30 }), expect.any(AbortSignal)));
  expect(archivesApi.curves).toHaveBeenCalledTimes(1);
});

it("keeps a separately bounded full overview while only the main chart requeries a narrower viewport", async () => {
  mount(); await ready();
  const overviewHost = document.querySelector<HTMLElement>(".chart-overview-canvas");
  expect(overviewHost).not.toBeNull();
  const overview = getInstanceByDom(overviewHost!);
  await waitFor(() => expect((overview?.getOption() as ChartOption).series[0].data?.length).toBeGreaterThan(0));
  const fullPoints = structuredClone((overview!.getOption() as ChartOption).series[0].data);
  const overviewCalls = () => vi.mocked(archivesApi.curves).mock.calls.filter(([, filters]) => filters.max_points === 800);
  expect(overviewCalls()).toHaveLength(1);
  expect(overviewCalls()[0][1]).toMatchObject({ start: 0, end: 100, max_points: 800 });
  applyXRange("10", "20");
  await waitFor(() => expect(archivesApi.curves).toHaveBeenCalledWith("synthetic-main", expect.objectContaining({ start: 10, end: 20, max_points: 4000 }), expect.any(AbortSignal)));
  await waitFor(() => expect(option().series[0].data?.[0]).toEqual([10, 1]));
  expect(overviewCalls()).toHaveLength(1);
  expect((overview!.getOption() as ChartOption).series[0].data).toEqual(fullPoints);
});

it("navigates back using the stable archive identity and clears filters when the execution changes", async () => {
  const view = mount(); await ready();
  change("阶段", "load"); change("循环", "2"); applyXRange("10", "20");
  fireEvent.click(screen.getByRole("button", { name: "返回归档详情" }));
  expect(view.navigate).toHaveBeenCalledWith("archive", "synthetic-main");
  view.rerender(view.node("SYN-002"));
  await waitFor(() => expect(archivesApi.curves).toHaveBeenCalledWith("SYN-002", expect.objectContaining({ metrics: "axisA_torque_nm", start: 0, end: 100 }), expect.any(AbortSignal)));
  expect((screen.getByLabelText("阶段") as HTMLSelectElement).value).toBe("");
  expect((screen.getByLabelText("循环") as HTMLSelectElement).value).toBe("");
});

it("hides and restores a legend series without fetching or replacing its missing samples", async () => {
  mount(); await ready();
  const original = structuredClone(option().series[0].data);
  const calls = vi.mocked(archivesApi.curves).mock.calls.length;
  const legend = within(screen.getByLabelText("曲线图例"));
  const toggle = legend.getByRole("checkbox", { name: /ARC-SYN-001/ });
  fireEvent.click(toggle);
  await waitFor(() => expect(option().series[0].data).toEqual([]));
  fireEvent.click(toggle);
  await waitFor(() => expect(option().series[0].data).toEqual(original));
  expect(option().series[0].connectNulls).toBe(false);
  expect((option().series[0].data as [number, number | null][]).some(([, value]) => value === null)).toBe(true);
  expect(archivesApi.curves).toHaveBeenCalledTimes(calls);
});

it("sends parameter, joint, stage, cycle, and point-budget changes to the viewport query", async () => {
  mount(); await ready();
  fireEvent.click(revealSubject("j2"));
  change("阶段", "load"); change("循环", "2"); change("视窗点数上限", "1000");
  await waitFor(() => expect(archivesApi.curves).toHaveBeenCalledWith("synthetic-main", expect.objectContaining({ metrics: "j1_torque_nm,j2_torque_nm", stage: "load", cycle: "2", max_points: 1000 }), expect.any(AbortSignal)));
  change("参数", "temp_c\u0000°C");
  await waitFor(() => expect(archivesApi.curves).toHaveBeenCalledWith("synthetic-main", expect.objectContaining({ metrics: "j1_temp_c", stage: "load", cycle: "2" }), expect.any(AbortSignal)));
  expect(screen.queryByLabelText("j2")).toBeNull();
});

it("keeps absent stage/cycle context visibly disabled without inventing sample identifiers", async () => {
  records["synthetic-main"].manifest = { ...records["synthetic-main"].manifest, stages: [], cycles: [] };
  mount(); await ready();
  for (const label of ["阶段", "循环"]) {
    const select = screen.getByLabelText(label) as HTMLSelectElement;
    expect(select.disabled).toBe(true);
    expect(select.textContent).toMatch(/未记录/);
  }
  expect(vi.mocked(archivesApi.curves).mock.calls.every(([, filters]) => filters.stage === undefined && filters.cycle === undefined)).toBe(true);
});

it("shows no-data results without keeping stale curves or reporting synthetic points as measurements", async () => {
  mount(); await ready();
  vi.mocked(archivesApi.curves).mockImplementation(async (id, filters) => ({ ...syntheticCurves(records[id], filters), series: [], gaps: [] }));
  change("阶段", "load");
  await screen.findByText("当前筛选没有采样点");
  await waitFor(() => expect(option().series).toHaveLength(0));
});

it("labels an all-missing viewport and preserves its nulls instead of manufacturing a connected curve", async () => {
  vi.mocked(archivesApi.curves).mockImplementation(async (id, filters) => {
    const response = syntheticCurves(records[id], filters);
    return { ...response, series: response.series.map((entry) => ({ ...entry, points: entry.points.map(([time]) => [time, null] as [number, null]) })) };
  });
  mount(); await ready();
  await screen.findByText("当前范围没有有效采样");
  expect(option().series[0].connectNulls).toBe(false);
  expect((option().series[0].data as [number, number | null][]).every(([, value]) => value === null)).toBe(true);
});

it.each(["empty-catalog", "not-ready"] as const)("does not request curves for %s archives", async (state) => {
  const archive = records["synthetic-main"];
  if (state === "empty-catalog") archive.manifest = { ...archive.manifest, catalog: [] };
  else archive.processing_state = "PROCESSING";
  mount();
  await screen.findByText(state === "empty-catalog" ? "归档中没有可绘制参数" : "归档曲线尚未就绪");
  expect(archivesApi.curves).not.toHaveBeenCalled();
});

it("surfaces a viewport read error and retries the same query without business mutations", async () => {
  let mainFailed = false;
  vi.mocked(archivesApi.curves).mockImplementation(async (id, filters) => {
    if (filters.max_points === 4000 && !mainFailed) {
      mainFailed = true;
      throw new Error("合成夹具：视窗读取失败");
    }
    return syntheticCurves(records[id], filters);
  });
  mount();
  await screen.findByText(/合成夹具：视窗读取失败/);
  fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
  await ready();
  const mainCalls = vi.mocked(archivesApi.curves).mock.calls.filter(([, filters]) => filters.max_points === 4000);
  expect(mainCalls).toHaveLength(2);
  expect(mainCalls[0].slice(0, 2)).toEqual(mainCalls[1].slice(0, 2));
  expect(vi.mocked(archivesApi.curves).mock.calls.filter(([, filters]) => filters.max_points === 800)).toHaveLength(1);
});

it("deduplicates searches and refuses same-parameter curves whose units differ", async () => {
  mount(); await ready();
  change("查找测试记录", "ARC-SYN");
  const search = screen.getByRole("button", { name: "查找对比记录" });
  fireEvent.click(search); fireEvent.click(search);
  const mismatch = await screen.findByRole("button", { name: /ARC-SYN-WRONG-UNIT.*缺少同参数、同单位/ });
  expect((mismatch as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(mismatch);
  expect(archivesApi.list).toHaveBeenCalledTimes(1);
  expect(vi.mocked(archivesApi.curves).mock.calls.some(([id]) => id === "SYN-WRONG-UNIT")).toBe(false);
});

it("includes and excludes a comparison without deleting it, then removes it without mutating the archive", async () => {
  mount(); await ready(); await addComparison();
  await waitFor(() => expect(option().series.filter((entry) => (entry.data?.length ?? 0) > 0)).toHaveLength(2));
  const participation = screen.getByRole("checkbox", { name: "参与对比 ARC-SYN-002" });
  fireEvent.click(participation);
  await waitFor(() => expect(option().series.filter((entry) => (entry.data?.length ?? 0) > 0)).toHaveLength(1));
  expect(screen.getByRole("button", { name: "移除对比 ARC-SYN-002" })).toBeTruthy();
  fireEvent.click(participation);
  await waitFor(() => expect(option().series.filter((entry) => (entry.data?.length ?? 0) > 0)).toHaveLength(2));
  fireEvent.click(screen.getByRole("button", { name: "移除对比 ARC-SYN-002" }));
  await waitFor(() => expect(option().series.filter((entry) => (entry.data?.length ?? 0) > 0)).toHaveLength(1));
  expect(screen.queryByRole("button", { name: "移除对比 ARC-SYN-002" })).toBeNull();
});

it("aborts a pending comparison query when the user removes that execution", async () => {
  mount(); await ready();
  let pendingSignal: AbortSignal | undefined;
  vi.mocked(archivesApi.curves).mockImplementation((id, filters, signal) => {
    if (id !== "SYN-002" || filters.max_points === 800) return Promise.resolve(syntheticCurves(records[id], filters));
    pendingSignal = signal;
    return new Promise<ArchiveCurves>((_resolve, reject) => signal?.addEventListener("abort", () => reject(new DOMException("Synthetic request cancelled", "AbortError")), { once: true }));
  });
  await addComparison();
  const pendingQuery = clients[clients.length - 1].getQueryCache().getAll().find((query) => query.queryKey[1] === "curves" && query.queryKey[2] === "SYN-002");
  await waitFor(() => expect(pendingQuery?.getObserversCount()).toBe(1));
  fireEvent.click(screen.getByRole("button", { name: "移除对比 ARC-SYN-002" }));
  await waitFor(() => expect(pendingQuery?.getObserversCount()).toBe(0));
  await waitFor(() => expect(pendingSignal?.aborted).toBe(true));
  expect(screen.queryByText(/读取失败/)).toBeNull();
});

it("deduplicates identical pinned references and keeps a different cycle as a distinct reference", async () => {
  mount(); await ready();
  fireEvent.click(reveal("固定当前曲线为参考"));
  fireEvent.click(reveal("固定当前曲线为参考"));
  expect(screen.getAllByRole("button", { name: /移除对比 ARC-SYN-001/, hidden: true })).toHaveLength(1);
  change("循环", "2");
  fireEvent.click(reveal("固定当前曲线为参考"));
  expect(screen.getAllByRole("button", { name: /移除对比 ARC-SYN-001/, hidden: true })).toHaveLength(2);
});

it("switches to real ECharts facets without refetching and keeps each visible series on a distinct grid", async () => {
  mount(); await ready(); await addComparison();
  await waitFor(() => expect(option().series.filter((entry) => (entry.data?.length ?? 0) > 0)).toHaveLength(2));
  const calls = vi.mocked(archivesApi.curves).mock.calls.length;
  fireEvent.click(screen.getByRole("button", { name: "分面" }));
  await waitFor(() => expect(option().grid.length).toBeGreaterThanOrEqual(2));
  expect(screen.getByRole("button", { name: "分面" }).getAttribute("aria-pressed")).toBe("true");
  expect(new Set(option().series.map((entry) => entry.yAxisIndex ?? 0)).size).toBeGreaterThanOrEqual(2);
  expect(archivesApi.curves).toHaveBeenCalledTimes(calls);
  fireEvent.click(screen.getByRole("button", { name: "叠加" }));
  await waitFor(() => expect(option().grid).toHaveLength(1));
});

it("rejects reversed X ranges locally and never sends an invalid viewport", async () => {
  mount(); await ready();
  const calls = vi.mocked(archivesApi.curves).mock.calls.length;
  applyXRange("30", "10");
  await screen.findByRole("alert");
  expect(archivesApi.curves).toHaveBeenCalledTimes(calls);
});
