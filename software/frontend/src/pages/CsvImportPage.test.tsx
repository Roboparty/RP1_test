// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import CsvImportPage from "./CsvImportPage";
import { setupApi, type SetupOptions } from "../setupApi";
import { archivesApi, type Archive } from "../archiveApi";
import { importApi, uploadCsv } from "../imports/importApi";
import { preflightCsv } from "../imports/preflightClient";
import type { CsvPreflight } from "../imports/csvPreflight";
const auth = vi.hoisted(() => ({ role: "TEST_EXECUTOR" }));
vi.mock("../auth", () => ({ useAuth: () => ({ session: { user: { id: "submitter", display_name: "登录提交人", role: auth.role } } }) }));
vi.mock("../imports/preflightClient", () => ({ preflightCsv: vi.fn() }));
vi.mock("../imports/importApi", async () => ({ ...await vi.importActual("../imports/importApi"), uploadCsv: vi.fn() }));
const preview: CsvPreflight = { sha256: "a".repeat(64), rows: 2, columns: [{ name: "time_s", unit: "s", count: 2, missing: 0, min: 0, max: 1 }, { name: "j1_bus_voltage_v", unit: "V", count: 0, missing: 2, min: null, max: null }], timeKey: "time_s", start: 0, end: 1, maxGap: 1, invalidTime: 0, nonMonotonic: 0, errors: [], warnings: ["1 个参数全部缺失"] };
const options: SetupOptions = { stations: [{ id: "station", code: "RD-01", name: "有效工位", station_type: "RIG" }], assets: [{ id: "asset", code: "SAMPLE-A", name: "样品 A", asset_kind: "MODULE", target_part_code: "ARM" }], configurations: [], parts: [], programs: [] };
const archive: Archive = { id: "arc", execution_uuid: "stable-execution", archive_code: "ARC-001", owner_public_id: "submitter", is_owner: true, can_edit: true, origin: "HISTORY", test_name: "原测试", actual_object: "原样品", station_label: "RD-01", filename: "first.csv", size_bytes: 4, sha256: preview.sha256, received_bytes: 0, upload_state: "UPLOADING", processing_state: "WAITING", processing_error: null, classification_state: "UNCLASSIFIED", revision: 1, facts: { active_seconds: null, completed_cycles: null, exception_count: null, machine_status: "unknown", automatic_verdict: "unconfigured" }, acceptance: {}, manifest: null, current_verification: null, classification: null, submission_id: null, submission_revision: null, submission_status: null, execution_id: null, execution_code: null, created_at: "2026-10-16T00:00:00Z" };
const ready: Archive = { ...archive, upload_state: "COMPLETE", processing_state: "READY", received_bytes: 4, manifest: { row_count: 2 } };
const navigate = vi.fn();
function mount() { render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}><CsvImportPage navigate={navigate} /></QueryClientProvider>); }
async function pick(name = "first.csv") { fireEvent.change(screen.getByLabelText("选择本地 CSV 文件"), { target: { files: [new File(["data"], name)] } }); return screen.findByRole("article", { name: `补录文件 ${name}` }); }
function basics(card: HTMLElement) { const row = within(card); fireEvent.change(row.getByLabelText("测试名称"), { target: { value: "一次负载测试" } }); fireEvent.change(row.getByLabelText("样品 / 实际对象"), { target: { value: "SAMPLE-A" } }); fireEvent.change(row.getByLabelText("有效工位"), { target: { value: "station" } }); }
beforeEach(() => {
  auth.role = "TEST_EXECUTOR"; navigate.mockReset(); sessionStorage.clear();
  vi.mocked(preflightCsv).mockResolvedValue(preview);
  vi.spyOn(setupApi, "options").mockResolvedValue(options);
  vi.spyOn(setupApi, "campaigns").mockResolvedValue([]);
  vi.spyOn(importApi, "lookup").mockResolvedValue({ archive: null });
  vi.spyOn(importApi, "create").mockResolvedValue({ archive, reused: false });
  vi.spyOn(archivesApi, "detail").mockResolvedValue(ready);
  vi.spyOn(archivesApi, "verify").mockResolvedValue(ready);
  vi.spyOn(archivesApi, "submit").mockResolvedValue(ready);
  vi.mocked(uploadCsv).mockImplementation(async (_file, _initial, _signal, progress) => { progress(ready); return ready; });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.clearAllMocks(); });
it("preflights locally, keeps facts read only and requires explicit independent execution confirmation", async () => {
  mount(); const card = await pick(); await within(card).findByText("全部缺失（2）");
  expect(importApi.create).not.toHaveBeenCalled(); expect((within(card).getByLabelText("实际测试人") as HTMLInputElement).value).toBe("");
  expect(within(card).queryByLabelText("运行秒数")).toBeNull();
  basics(card); const button = within(card).getByRole("button", { name: "上传并解析待核实记录" }); expect((button as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(within(card).getByRole("checkbox"));
  await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false)); fireEvent.click(button);
  await waitFor(() => expect(importApi.create).toHaveBeenCalled());
  expect(vi.mocked(importApi.create).mock.calls[0][0]).toEqual({ filename: "first.csv", size_bytes: 4, sha256: preview.sha256, test_name: "一次负载测试", actual_object: "SAMPLE-A", station_id: "station", classification: null });
  expect(archivesApi.verify).not.toHaveBeenCalled(); expect(archivesApi.submit).not.toHaveBeenCalled();
  await within(card).findByText(/当前不能正式送审/);
});
it("reuses an existing file without writing draft metadata or creating a duplicate", async () => {
  vi.mocked(importApi.lookup).mockResolvedValue({ archive: ready });
  mount(); const card = await pick(); await within(card).findByText(/相同文件已存在/);
  expect(within(card).getByText("原测试 · 原样品 · RD-01")).toBeTruthy();
  expect(within(card).queryByRole("button", { name: "上传并解析待核实记录" })).toBeNull();
  fireEvent.click(within(card).getByRole("button", { name: "查看曲线" })); expect(navigate).toHaveBeenCalledWith("curves", "arc");
  expect(importApi.create).not.toHaveBeenCalled(); expect(uploadCsv).not.toHaveBeenCalled(); expect(archivesApi.verify).not.toHaveBeenCalled();
});
it("does not upload when a race returns an existing incomplete archive; requires explicit resume", async () => {
  vi.mocked(importApi.create).mockResolvedValue({ archive, reused: true }); vi.mocked(archivesApi.detail).mockResolvedValue(archive);
  mount(); const card = await pick(); await within(card).findByLabelText("测试名称"); basics(card); fireEvent.click(within(card).getByRole("checkbox"));
  fireEvent.click(within(card).getByRole("button", { name: "上传并解析待核实记录" }));
  await within(card).findByText(/已复用原归档/); expect(uploadCsv).not.toHaveBeenCalled();
  fireEvent.click(within(card).getByRole("button", { name: "继续上传同一原文件" })); await waitFor(() => expect(uploadCsv).toHaveBeenCalledTimes(1));
});
it("keeps failed preflight and unavailable deduplication from creating server records", async () => {
  vi.mocked(preflightCsv).mockResolvedValue({ ...preview, errors: ["1 行时间无效"] });
  mount(); const card = await pick(); await within(card).findByText("1 行时间无效");
  expect(importApi.lookup).not.toHaveBeenCalled(); expect(importApi.create).not.toHaveBeenCalled(); expect(within(card).queryByRole("checkbox")).toBeNull();
});
it("requires real tester, notes, conclusion and explanation before saving verification", async () => {
  mount(); const card = await pick(); await within(card).findByLabelText("测试名称"); basics(card); fireEvent.click(within(card).getByRole("checkbox"));
  fireEvent.click(within(card).getByRole("button", { name: "上传并解析待核实记录" }));
  const save = await within(card).findByRole("button", { name: "保存人工核实版本" }); expect((save as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(within(card).getByLabelText("实际测试人"), { target: { value: "真实测试员" } });
  fireEvent.change(within(card).getByLabelText("人工结论"), { target: { value: "blocked" } });
  fireEvent.change(within(card).getByLabelText("测试员备注"), { target: { value: "缺机器终态，仅核对原文件" } });
  fireEvent.change(within(card).getByLabelText("人工结论依据 / 与自动结论差异原因"), { target: { value: "无正式判据" } });
  fireEvent.change(within(card).getByLabelText("核实说明"), { target: { value: "依据本地记录核实" } });
  fireEvent.click(save); await waitFor(() => expect(archivesApi.verify).toHaveBeenCalled());
  expect(vi.mocked(archivesApi.verify).mock.calls[0][1]).toEqual({ expected_revision: 1, tester: "真实测试员", station_id: "station", verdict: "blocked", notes: "缺机器终态，仅核对原文件", discrepancy_reason: "无正式判据", reason: "依据本地记录核实" });
  expect(archivesApi.submit).not.toHaveBeenCalled();
});
it("does not give viewers a file picker or call operator APIs", () => {
  auth.role = "VIEWER"; mount(); expect(screen.queryByLabelText("选择本地 CSV 文件")).toBeNull(); expect(setupApi.options).not.toHaveBeenCalled();
});
it("requires per-file execution confirmation even after applying batch metadata", async () => {
  mount(); await pick("first.csv"); await pick("second.csv");
  const fields = screen.getAllByLabelText("样品 / 实际对象"); fireEvent.change(fields[0], { target: { value: "SAMPLE-A" } });
  fireEvent.change(screen.getAllByLabelText("有效工位")[0], { target: { value: "station" } });
  fireEvent.click(screen.getByRole("button", { name: "应用到未上传文件" }));
  await waitFor(() => expect((screen.getAllByLabelText("样品 / 实际对象")[1] as HTMLInputElement).value).toBe("SAMPLE-A"));
  expect(screen.getAllByRole("checkbox").every((input) => !(input as HTMLInputElement).checked)).toBe(true);
  expect(importApi.create).not.toHaveBeenCalled();
});

it("preserves approval and formal execution status when deduplication finds a complete HMI archive", async () => {
  const existing: Archive = { ...ready, origin: "HMI", submission_status: "APPROVED", execution_id: "formal-id", execution_code: "EXE-2026-01", can_edit: false, facts: { active_seconds: 20, completed_cycles: 1, exception_count: 0, machine_status: "completed" } };
  vi.mocked(importApi.lookup).mockResolvedValue({ archive: existing }); vi.mocked(archivesApi.detail).mockResolvedValue(existing);
  mount(); const card = await pick(); await within(card).findByText(/EXE-2026-01/);
  expect(within(card).getByText("已批准")).toBeTruthy(); expect(within(card).queryByText(/不能正式送审/)).toBeNull();
  expect(importApi.create).not.toHaveBeenCalled(); expect(uploadCsv).not.toHaveBeenCalled();
});
it("allows retrying failed lookup without creating any record before duplicate identity is known", async () => {
  vi.mocked(importApi.lookup).mockRejectedValueOnce(new Error("连接中断")).mockResolvedValueOnce({ archive: ready });
  mount(); const card = await pick(); await within(card).findByText(/查重失败/);
  basics(card); fireEvent.click(within(card).getByRole("checkbox"));
  expect((within(card).getByRole("button", { name: "上传并解析待核实记录" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(within(card).getByRole("button", { name: "重新查重" })); await within(card).findByText(/相同文件已存在/);
  expect(importApi.create).not.toHaveBeenCalled();
});
it("gives preflight and upload progress accessible names and announces upload receipts", async () => {
  let resolvePreview: (value: CsvPreflight) => void = () => {};
  vi.mocked(preflightCsv).mockImplementationOnce(() => new Promise((resolve) => { resolvePreview = resolve; }));
  mount(); const card = await pick();
  expect(within(card).getByRole("progressbar", { name: "first.csv 本地预检与校验进度" })).toBeTruthy();
  expect(within(card).getByRole("status").getAttribute("aria-live")).toBe("polite");
  vi.mocked(importApi.lookup).mockResolvedValue({ archive }); vi.mocked(archivesApi.detail).mockResolvedValue(archive);
  resolvePreview(preview);
  await within(card).findByRole("progressbar", { name: "first.csv 原文件上传进度" });
  expect(within(card).getByText(/服务器已接收/).closest('[role="status"]')?.getAttribute("aria-live")).toBe("polite");
});
it("explains missing upload fields while keeping actual tester optional for the explicit draft step", async () => {
  mount(); const card = await pick(); await within(card).findByLabelText("测试名称");
  await waitFor(() => expect(within(card).getByText(/上传前还需/).textContent).not.toContain("完成文件查重"));
  expect(within(card).getByText(/上传前还需/).textContent).toContain("测试名称、样品 / 实际对象、有效工位、独立执行确认");
  expect(within(card).getByText(/上传前还需/).textContent).not.toContain("实际测试人");
  expect((within(card).getByLabelText("测试名称") as HTMLInputElement).required).toBe(true);
  expect((within(card).getByLabelText("实际测试人") as HTMLInputElement).required).toBe(false);
  expect((screen.getByLabelText("选择本地 CSV 文件") as HTMLInputElement).tabIndex).toBe(-1);
});
it("lists verification requirements and adds a discrepancy requirement only for a different manual verdict", async () => {
  const analyzed = { ...ready, manifest: { analysis: { verdict: "blocked" } } };
  vi.mocked(archivesApi.detail).mockResolvedValue(analyzed);
  vi.mocked(uploadCsv).mockImplementation(async (_file, _initial, _signal, progress) => { progress(analyzed); return analyzed; });
  mount(); const card = await pick(); await within(card).findByLabelText("测试名称"); basics(card); fireEvent.click(within(card).getByRole("checkbox"));
  fireEvent.click(within(card).getByRole("button", { name: "上传并解析待核实记录" }));
  await within(card).findByRole("button", { name: "保存人工核实版本" });
  expect(within(card).getByText(/核实前还需/).textContent).toContain("实际测试人、人工结论、测试员备注、核实说明");
  fireEvent.change(within(card).getByLabelText("人工结论"), { target: { value: "passed" } });
  expect(within(card).getByText(/核实前还需/).textContent).toContain("与自动结论差异原因");
  expect((within(card).getByLabelText("人工结论依据 / 与自动结论差异原因") as HTMLTextAreaElement).required).toBe(true);
  fireEvent.change(within(card).getByLabelText("人工结论"), { target: { value: "blocked" } });
  expect(within(card).getByText(/核实前还需/).textContent).not.toContain("与自动结论差异原因");
  expect((within(card).getByLabelText("人工结论依据 / 与自动结论差异原因") as HTMLTextAreaElement).required).toBe(false);
});
