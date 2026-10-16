// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import SubmissionsPage from "./SubmissionsPage";
import { submissionsApi, type Submission } from "../submissionsApi";

const identity = vi.hoisted(() => ({ role: "SYSTEM_ADMIN" }));
vi.mock("../auth", () => ({ useAuth: () => ({ session: { user: identity } }) }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); identity.role = "SYSTEM_ADMIN"; });

function renderQueue(overrides: Partial<Submission> = {}, navigateArchive = vi.fn()) {
  const item = { id: "submission-1", execution_uuid: "local-1", revision: 1, status: "PENDING", submitter: "operator", submitter_name: "提交员", campaign_code: "PLAN-1", created_at: "2026-10-12T00:00:00Z", updated_at: "2026-10-12T00:00:00Z", can_review: true, is_owner: false,
    revisions: [{ revision: 1, submission_reason: "本地测试完成", submitted_at: "2026-10-12T00:00:00Z", decision: null, review_reason: null, reviewed_at: null, reviewer: null,
      summary: { execution_uuid: "local-1", campaign_id: "plan-1", cycle_id: "cycle-1", segment_id: "segment-1", asset_id: "asset-1", configuration_id: "config-1", test_case_version_id: "case-1", station_id: "station-1", started_at: "2026-10-11T00:00:00Z", ended_at: "2026-10-11T00:01:00Z", active_seconds: 50, completed_cycles: 2, source_status: "passed", data_quality: "VALID", clock_quality: "VALID", exception_count: 0, summary: "有界测试摘要", evidence_sha256: "a".repeat(64) } }], ...overrides } as Submission;
  vi.spyOn(submissionsApi, "list").mockResolvedValue({ items: [item], next_cursor: null });
  vi.spyOn(submissionsApi, "detail").mockResolvedValue(item);
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={cache}><SubmissionsPage navigate={vi.fn()} navigateArchive={navigateArchive} /></QueryClientProvider>);
  return item;
}

it("requires a review reason and reports a publication conflict without claiming success", async () => {
  const review = vi.spyOn(submissionsApi, "review").mockRejectedValue(new Error("版本已变化，发布已回滚"));
  renderQueue();
  fireEvent.click(await screen.findByRole("button", { name: /PLAN-1/ }));
  const approve = await screen.findByRole("button", { name: "批准并发布正式记录" });
  expect((approve as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(screen.getByLabelText("审核意见"), { target: { value: "可以" } });
  expect((approve as HTMLButtonElement).disabled).toBe(false);
  fireEvent.change(screen.getByLabelText("审核意见"), { target: { value: "核对摘要与本地证据" } });
  fireEvent.click(approve);
  await screen.findByText("版本已变化，发布已回滚");
  expect(screen.queryByText("审核结果已保存。")).toBeNull();
  expect(review.mock.calls[0].slice(0, 4)).toEqual(["submission-1", 1, "APPROVED", "核对摘要与本地证据"]);
});

it("lets the submitter review their own pending summary", async () => {
  renderQueue({ is_owner: true, can_review: true });
  fireEvent.click(await screen.findByRole("button", { name: /PLAN-1/ }));
  expect(await screen.findByRole("button", { name: "批准并发布正式记录" })).toBeTruthy();
  expect(screen.queryByText(/禁止自审/)).toBeNull();
});

it("keeps rejected measured facts read-only and routes resubmission through archive verification", async () => {
  identity.role = "TEST_EXECUTOR";
  const submit = vi.spyOn(submissionsApi, "submit");
  const navigateArchive = vi.fn();
  renderQueue({ status: "REJECTED", is_owner: true, can_review: false, archive_id: "archive-1" }, navigateArchive);
  fireEvent.click(await screen.findByRole("button", { name: /PLAN-1/ }));
  fireEvent.click(await screen.findByRole("button", { name: "核实并重新送审" }));
  expect(screen.queryByLabelText("修正申报运行秒数")).toBeNull();
  expect(screen.queryByLabelText("修正循环次数")).toBeNull();
  expect(screen.queryByLabelText("修正异常次数")).toBeNull();
  expect(screen.queryByLabelText("修正测试结论")).toBeNull();
  await waitFor(() => expect(navigateArchive).toHaveBeenCalledWith("archive-1"));
  expect(submit).not.toHaveBeenCalled();
});

it("does not request the approval queue for a viewer", () => {
  identity.role = "VIEWER";
  renderQueue();
  expect(screen.getByText("无测试数据审批权限")).toBeTruthy();
  expect(submissionsApi.list).not.toHaveBeenCalled();
});

it.each(["blocked", "failed"] as const)("shows manual %s independently of a passed machine summary and unconfigured analysis", async (verdict) => {
  const item = renderQueue({ archive_id: "archive-1" });
  item.revisions![0].verification = { revision: 2, tester: "核实员", station_id: "station-1", verdict, notes: "机器执行完成，人工核实发现待处理事项", discrepancy_reason: "人工核对实物结果", created_at: "2026-10-12T00:00:00Z" };
  item.revisions![0].analysis = { verdict: "unconfigured", criteria_version: "UNCONFIGURED" };
  expect(item.revisions![0].summary.source_status).toBe("passed");
  fireEvent.click(await screen.findByRole("button", { name: /PLAN-1/ }));
  const conclusion = await screen.findByText("人工核实结论");
  const row = conclusion.closest("div")!;
  expect(within(row).getByRole("definition").textContent).toBe(verdict === "failed" ? "失败" : "待判定 / 阻塞");
  expect(within(row).queryByText("通过")).toBeNull();
  expect(screen.getByText("自动结果分析")).toBeTruthy();
  expect(screen.getByText("未配置")).toBeTruthy();
});
