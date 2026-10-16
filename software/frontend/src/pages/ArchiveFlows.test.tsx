// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { archivesApi, type Archive, type ArchiveContext, type ArchiveCurves, type ArchiveVerification } from "../archiveApi";
import { setupApi, type PlannedContext, type SetupOptions } from "../setupApi";
import { ArchiveDetailPage } from "./ArchivesPage";
import CurveAnalysisPage, { compatibleCatalog, configuredReferences } from "./CurveAnalysisPage";

vi.mock("../auth", () => ({ useAuth: () => ({ session: { user: { role: "TEST_EXECUTOR" } } }) }));
vi.mock("../components/TimeSeriesChart", () => ({ TimeSeriesChart: ({ series, onViewport, resolution }: { series: { id: string }[]; onViewport: (value: { start: number; end: number }) => void; resolution: string }) => <div><span>{resolution}</span><span data-testid="series-count">{series.length}</span><button type="button" onClick={() => onViewport({ start: 10, end: 20 })}>缩放到 10–20 秒</button></div> }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

const archive: Archive = {
  id: "archive-1", execution_uuid: "execution-1", archive_code: "ARC-001", owner_public_id: "owner", is_owner: true, can_edit: true,
  origin: "OTHER", test_name: "临时负载测试", actual_object: "左臂样品", station_label: "工位 A", filename: "test.csv", size_bytes: 1024, sha256: "a".repeat(64), received_bytes: 1024,
  upload_state: "COMPLETE", processing_state: "READY", processing_error: null, classification_state: "UNCLASSIFIED", revision: 2,
  facts: { started_at: "2026-10-12T00:00:00Z", ended_at: "2026-10-12T00:01:40Z", active_seconds: 100, completed_cycles: 5, exception_count: 0, machine_status: "completed", automatic_verdict: "unconfigured", data_quality: "VALID", clock_quality: "VALID" },
  acceptance: { criteria_version: "UNCONFIGURED", max_torque_abs_nm: 8 },
  manifest: { time_range: { start: 0, end: 100 }, catalog: [{ key: "j1_torque_nm", subject: "j1", metric: "torque_nm", unit: "Nm" }, { key: "j2_torque_nm", subject: "j2", metric: "torque_nm", unit: "Nm" }, { key: "j1_temp_c", subject: "j1", metric: "temp_c", unit: "°C" }], stages: [], cycles: [], row_count: 1000, analysis: { verdict: "unconfigured" } },
  current_verification: null, classification: null, submission_id: null, submission_revision: null, submission_status: null, execution_id: null, execution_code: null, created_at: "2026-10-12T00:02:00Z"
};
const options = { stations: [{ id: "station-1", code: "ST-1", name: "工位 A", station_type: "RIG" }], assets: [], configurations: [], parts: [], programs: [] } satisfies SetupOptions;
function mount(node: React.ReactNode) { return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{node}</QueryClientProvider>); }
beforeEach(() => {
  vi.spyOn(archivesApi, "detail").mockResolvedValue(archive);
  vi.spyOn(setupApi, "options").mockResolvedValue(options);
  vi.spyOn(setupApi, "campaigns").mockResolvedValue([]);
});

it("appends a published-rule analysis on an approved archive without reopening verification", async () => {
  const approved = { ...archive, can_edit: false, can_analyze: true, submission_status: "APPROVED" as const };
  vi.mocked(archivesApi.detail).mockResolvedValue(approved);
  vi.spyOn(setupApi, "catalog").mockResolvedValue([{ id: "case-id", code: "REL-LEG-001", name: "负载试验", asset_kind: "MODULE", target_part_code: "LEG", target_part_name: "腿", enabled: true, domain: "QA", version_id: "published-version", version: "2", procedure_spec: null }]);
  const reports = vi.spyOn(archivesApi, "analyses").mockResolvedValue({ items: [], original_analysis: { verdict: "unconfigured" } });
  const report = { id: "analysis-1", version: 1, source_test_case_version_id: "published-version", source_case_code: "REL-LEG-001", source_case_name: "负载试验", source_version: "2", mapping_status: "UNCONFIGURED" as const, mapping_notes: "此版本未配置可计算的数值判据", acceptance: {}, report: { verdict: "unconfigured" }, original_sha256: archive.sha256, original_analysis_version: 1, actor_name: "测试员", reason: "回顾比较", created_at: archive.created_at };
  const analyze = vi.spyOn(archivesApi, "analyze").mockImplementation(async () => {
    reports.mockResolvedValue({ items: [report], original_analysis: { verdict: "unconfigured" } });
    return report;
  });
  mount(<ArchiveDetailPage id={archive.id} navigate={vi.fn()} />);
  const version = await screen.findByLabelText("分析使用的发布用例");
  await screen.findByText("REL-LEG-001 · 负载试验 · 2");
  fireEvent.change(version, { target: { value: "published-version" } });
  fireEvent.change(screen.getByLabelText("重新分析说明"), { target: { value: "回顾比较" } });
  fireEvent.click(screen.getByRole("button", { name: "追加分析版本" }));
  await waitFor(() => expect(analyze).toHaveBeenCalledWith(archive.id, { test_case_version_id: "published-version", reason: "回顾比较" }, expect.any(String)));
  await screen.findByText(/分析第 1 版/);
  expect(screen.queryByLabelText("人工核实结论")).toBeNull();
  expect(screen.queryByRole("button", { name: "送审" })).toBeNull();
  expect(approved.sha256).toBe(archive.sha256);
});

it("shows appended analysis versions to readers without a reanalysis mutation form", async () => {
  vi.mocked(archivesApi.detail).mockResolvedValue({ ...archive, can_edit: false, can_analyze: false, submission_status: "APPROVED" });
  vi.spyOn(archivesApi, "analyses").mockResolvedValue({ items: [], original_analysis: { verdict: "unconfigured" } });
  mount(<ArchiveDetailPage id={archive.id} navigate={vi.fn()} />);
  await screen.findByText("按新用例判据重新分析");
  expect(screen.queryByLabelText("分析使用的发布用例")).toBeNull();
  expect(screen.queryByRole("button", { name: "追加分析版本" })).toBeNull();
});

it("requires explicit manual verdict and discrepancy reason without exposing measured-fact edits", async () => {
  const verify = vi.spyOn(archivesApi, "verify").mockResolvedValue(archive);
  mount(<ArchiveDetailPage id={archive.id} navigate={vi.fn()} />);
  const verdict = await screen.findByLabelText("人工核实结论");
  expect((verdict as HTMLSelectElement).value).toBe("");
  expect(screen.queryByLabelText("运行秒数")).toBeNull();
  expect(screen.queryByLabelText("循环次数")).toBeNull();
  fireEvent.change(screen.getByLabelText("实际测试人"), { target: { value: "张工" } });
  fireEvent.change(screen.getByLabelText("核实工位"), { target: { value: "station-1" } });
  fireEvent.change(verdict, { target: { value: "passed" } });
  fireEvent.change(screen.getByLabelText("测试员记录"), { target: { value: "无" } });
  fireEvent.change(screen.getByLabelText("核实说明"), { target: { value: "核对本地采集记录" } });
  expect((screen.getByRole("button", { name: "保存核实版本" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(screen.getByLabelText("与自动结论不同的原因"), { target: { value: "探索测试人工评估" } });
  fireEvent.click(screen.getByRole("button", { name: "保存核实版本" }));
  await waitFor(() => expect(verify).toHaveBeenCalled());
  expect(verify.mock.calls[0][1]).toEqual({ expected_revision: 2, tester: "张工", station_id: "station-1", verdict: "passed", notes: "无", discrepancy_reason: "探索测试人工评估", reason: "核对本地采集记录" });
  expect((screen.getByRole("button", { name: "送审" }) as HTMLButtonElement).disabled).toBe(true);
});

it.each(["APPROVED", "PENDING"] as const)("hides all revision forms for %s archives", async (status) => {
  vi.mocked(archivesApi.detail).mockResolvedValue({ ...archive, submission_status: status });
  mount(<ArchiveDetailPage id={archive.id} navigate={vi.fn()} />);
  await screen.findByText("采集事实");
  expect(screen.queryByLabelText("人工核实结论")).toBeNull();
  expect(screen.queryByLabelText("归类说明")).toBeNull();
  expect(screen.queryByRole("button", { name: "送审" })).toBeNull();
});

it("reloads a zoomed viewport from the backend and only overlays matching parameter units", async () => {
  const curves: ArchiveCurves = { archive_id: archive.id, execution_uuid: archive.execution_uuid, archive_code: archive.archive_code, range: { start: 0, end: 100 }, series: [{ key: "j1_torque_nm", subject: "j1", metric: "torque_nm", unit: "Nm", points: [[0, 1], [100, 2]], sample_count: 1000, returned_points: 2, downsampled: true }], gaps: [], resolution: "minmax", catalog: archive.manifest!.catalog!, time_range: { start: 0, end: 100 } };
  const fetchCurves = vi.spyOn(archivesApi, "curves").mockResolvedValue(curves);
  vi.spyOn(archivesApi, "list").mockResolvedValue({ items: [{ ...archive, id: "archive-2", archive_code: "ARC-002", manifest: { ...archive.manifest, catalog: [{ key: "j1_torque", subject: "j1", metric: "torque_nm", unit: "mNm" }] } }], next_cursor: null });
  mount(<CurveAnalysisPage id={archive.id} navigate={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: "缩放到 10–20 秒" }));
  await waitFor(() => expect(fetchCurves).toHaveBeenCalledWith(archive.id, expect.objectContaining({ metrics: "j1_torque_nm", start: 10, end: 20 }), expect.any(AbortSignal)));
  fireEvent.change(screen.getByLabelText("查找测试记录"), { target: { value: "ARC-002" } });
  fireEvent.click(screen.getByRole("button", { name: "查找对比记录" }));
  const mismatch = await screen.findByRole("button", { name: /ARC-002.*缺少同参数、同单位/ });
  expect((mismatch as HTMLButtonElement).disabled).toBe(true);
});

it("does not turn an RMS acceptance criterion into an instantaneous reference limit", () => {
  expect(configuredReferences({ ...archive, acceptance: { max_tracking_rms_rad: 0.1 } }, "tracking_error_rad")).toEqual([]);
  expect(compatibleCatalog([{ key: "x", subject: "j1", metric: "torque_nm", unit: "mNm" }], "torque_nm", "Nm", ["j1"])).toEqual([]);
});

const plannedContext: ArchiveContext = { campaign_id: "campaign-1", cycle_id: "cycle-1", segment_id: "segment-1", asset_id: "asset-1", configuration_id: "config-1", test_case_version_id: "case-1", station_id: "station-1" };
const verification: ArchiveVerification = { revision: 2, tester: "张工", station_id: "station-2", station_code: "ST-2", station_name: "工位 B", verdict: "passed", notes: "无", discrepancy_reason: "人工核实", created_at: "2026-10-12T00:02:00Z" };
function mockClassificationOptions() {
  vi.mocked(setupApi.options).mockResolvedValue({ ...options, stations: [...options.stations, { id: "station-2", code: "ST-2", name: "工位 B", station_type: "RIG" }] });
  vi.mocked(setupApi.campaigns).mockResolvedValue([{ id: "campaign-1", code: "PLAN-1", name: "计划", asset_kind: "MODULE", status: "ACTIVE", program_code: null, program_name: null, can_edit: true, asset_count: 1, case_count: 1, planned_start: null, planned_end: null }]);
  vi.spyOn(setupApi, "contexts").mockResolvedValue([{ id: "context-1", asset_id: "asset-1", asset_code: "ARM-1", case_code: "CASE-1", case_name: "负载测试", version: "v1", planned_runs: 1, status: "PLANNED", context: plannedContext } as PlannedContext]);
}

it("classifies OTHER using verified station B while retaining the planned station A context and permits submission", async () => {
  mockClassificationOptions();
  let current: Archive = { ...archive, current_verification: verification };
  vi.mocked(archivesApi.detail).mockImplementation(async () => current);
  const classify = vi.spyOn(archivesApi, "classify").mockImplementation(async (_id, payload) => {
    current = { ...current, revision: 3, classification_state: "CLASSIFIED", classification: { ...payload.context, revision: 3 } }; return current;
  });
  const submit = vi.spyOn(archivesApi, "submit").mockResolvedValue({ ...current, submission_status: "PENDING" });
  mount(<ArchiveDetailPage id={archive.id} navigate={vi.fn()} />);
  await screen.findByRole("option", { name: /PLAN-1/ });
  fireEvent.change(screen.getByLabelText("测试计划"), { target: { value: "campaign-1" } });
  await screen.findByRole("option", { name: /CASE-1/ });
  fireEvent.change(screen.getByLabelText("用例与样品"), { target: { value: "context-1" } });
  expect(await screen.findByText(/计划工位：ST-1.*实际工位：ST-2/)).toBeTruthy();
  fireEvent.change(screen.getByLabelText("归类说明"), { target: { value: "计划 A 工位占用，实际使用 B 工位" } });
  fireEvent.click(screen.getByRole("button", { name: "保存正式归属" }));
  await waitFor(() => expect(classify).toHaveBeenCalledWith(archive.id, expect.objectContaining({ expected_revision: 2, context: { ...plannedContext, station_id: "station-2" } }), expect.any(String)));
  expect(plannedContext.station_id).toBe("station-1");
  await screen.findByText("追加正式归属修正");
  fireEvent.change(screen.getByLabelText("提交说明"), { target: { value: "核实归类完成" } });
  fireEvent.click(screen.getByRole("button", { name: "送审" }));
  await waitFor(() => expect(submit).toHaveBeenCalledWith(archive.id, { expected_revision: 3, expected_submission_revision: 0, reason: "核实归类完成" }, expect.any(String)));
});

it("lets a rejected classified archive append the verified station correction before resubmission", async () => {
  mockClassificationOptions();
  let current: Archive = { ...archive, revision: 4, submission_status: "REJECTED", submission_revision: 1, classification_state: "CLASSIFIED", classification: { ...plannedContext, revision: 1 }, current_verification: { ...verification, revision: 4 } };
  vi.mocked(archivesApi.detail).mockImplementation(async () => current);
  const classify = vi.spyOn(archivesApi, "classify").mockImplementation(async (_id, payload) => {
    current = { ...current, revision: 5, classification: { ...payload.context, revision: 5 } }; return current;
  });
  const submit = vi.spyOn(archivesApi, "submit").mockResolvedValue({ ...current, submission_status: "PENDING" });
  mount(<ArchiveDetailPage id={archive.id} navigate={vi.fn()} />);
  await screen.findByText("追加正式归属修正");
  await screen.findByRole("option", { name: /CASE-1/ });
  expect((screen.getByLabelText("用例与样品") as HTMLSelectElement).value).toBe("context-1");
  fireEvent.change(screen.getByLabelText("提交说明"), { target: { value: "修正工位后重提" } });
  expect((screen.getByRole("button", { name: "送审" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(screen.getByLabelText("归类说明"), { target: { value: "驳回后核对实际工位为 B，保留计划 A" } });
  fireEvent.click(screen.getByRole("button", { name: "保存归属修正版本" }));
  await waitFor(() => expect(classify).toHaveBeenCalledWith(archive.id, expect.objectContaining({ expected_revision: 4, context: { ...plannedContext, station_id: "station-2" } }), expect.any(String)));
  await waitFor(() => expect((screen.getByRole("button", { name: "送审" }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole("button", { name: "送审" }));
  await waitFor(() => expect(submit).toHaveBeenCalledWith(archive.id, { expected_revision: 5, expected_submission_revision: 1, reason: "修正工位后重提" }, expect.any(String)));
});

it("preserves declared historical dates when a rejected verification changes only notes", async () => {
  const started = "2026-10-11T01:02:03.456Z";
  const ended = "2026-10-11T01:03:43.456Z";
  vi.mocked(archivesApi.detail).mockResolvedValue({ ...archive, origin: "HISTORY", submission_status: "REJECTED", facts: { ...archive.facts, started_at: null, ended_at: null }, current_verification: { ...verification, station_id: "station-1", declared_started_at: started, declared_ended_at: ended } });
  const verify = vi.spyOn(archivesApi, "verify").mockResolvedValue(archive);
  mount(<ArchiveDetailPage id={archive.id} navigate={vi.fn()} />);
  const startedInput = await screen.findByLabelText("人工补录开始时间");
  expect(new Date((startedInput as HTMLInputElement).value).toISOString()).toBe(started);
  expect(new Date((screen.getByLabelText("人工补录结束时间") as HTMLInputElement).value).toISOString()).toBe(ended);
  fireEvent.change(screen.getByLabelText("测试员记录"), { target: { value: "补充驳回后的测试备注" } });
  fireEvent.change(screen.getByLabelText("核实说明"), { target: { value: "只补充备注，保留历史补录时间" } });
  fireEvent.click(screen.getByRole("button", { name: "保存核实版本" }));
  await waitFor(() => expect(verify).toHaveBeenCalledWith(archive.id, expect.objectContaining({ declared_started_at: started, declared_ended_at: ended, notes: "补充驳回后的测试备注" }), expect.any(String)));
});
