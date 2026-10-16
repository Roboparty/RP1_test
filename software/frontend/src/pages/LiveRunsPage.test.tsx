// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import LiveRunsPage from "./LiveRunsPage";

const query = vi.hoisted(() => ({
  data: {
    items: [{
      id: "execution-1",
      code: "REL-SARM-002_2610141526_RP1.3-SARM-001_RD-01_E136",
      asset_id: "asset-1",
      asset_code: "RP1.3-SARM-001",
      test_case_id: "case-1",
      test_case_code: "REL-SARM-002",
      test_case_name: "单臂耐久",
      campaign_code: "CLI-SARM-20261014",
      cycle_code: "C1",
      stage_count: 0,
      status: "RUNNING",
      started_at: "2026-10-14T07:26:00Z",
      ended_at: null,
      closed_duration_seconds: 0,
      active_elapsed_seconds: 188,
      data_quality: "PARTIAL",
      clock_quality: "VALID",
      station_code: "RD-01"
    }],
    page: { limit: 100, next_cursor: null },
    as_of_at: "2026-10-14T07:30:00Z",
    data_cutoff_at: "2026-10-14T07:30:00Z"
  },
  isLoading: false,
  isError: false,
  isFetching: false,
  error: null,
  refetch: vi.fn()
}));

vi.mock("@tanstack/react-query", () => ({ useQuery: () => query }));

afterEach(() => cleanup());

describe("live executions", () => {
  it("lists the official execution code for every running test", () => {
    const navigate = vi.fn();
    render(<LiveRunsPage navigate={navigate} />);
    expect(screen.getByText("正在运行的测试")).toBeTruthy();
    expect(screen.getByText("REL-SARM-002_2610141526_RP1.3-SARM-001_RD-01_E136")).toBeTruthy();
    expect(screen.getByText("RP1.3-SARM-001")).toBeTruthy();
    expect(screen.getByText("RD-01")).toBeTruthy();
    fireEvent.click(screen.getByText("REL-SARM-002_2610141526_RP1.3-SARM-001_RD-01_E136"));
    expect(navigate).toHaveBeenCalledWith("execution", "execution-1");
  });

  it("shows an empty state when nothing is running", () => {
    query.data = { ...query.data, items: [] };
    render(<LiveRunsPage navigate={vi.fn()} />);
    expect(screen.getByText("当前没有正在运行的测试")).toBeTruthy();
    query.data = {
      ...query.data,
      items: [{
        id: "execution-1",
        code: "REL-SARM-002_2610141526_RP1.3-SARM-001_RD-01_E136",
        asset_id: "asset-1",
        asset_code: "RP1.3-SARM-001",
        test_case_id: "case-1",
        test_case_code: "REL-SARM-002",
        test_case_name: "单臂耐久",
        campaign_code: "CLI-SARM-20261014",
        cycle_code: "C1",
        stage_count: 0,
        status: "RUNNING",
        started_at: "2026-10-14T07:26:00Z",
        ended_at: null,
        closed_duration_seconds: 0,
        active_elapsed_seconds: 188,
        data_quality: "PARTIAL",
        clock_quality: "VALID",
        station_code: "RD-01"
      }]
    };
  });
});
