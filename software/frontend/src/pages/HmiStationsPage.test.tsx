// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import HmiStationsPage, { HmiStationCard } from "./HmiStationsPage";
import type { HmiStation } from "../hmiPresence";

const query = vi.hoisted(() => ({ dataUpdatedAt: 0, data: { pages: [] as unknown[] }, isPending: false, isError: false, refetch: vi.fn() }));
vi.mock("@tanstack/react-query", () => ({useInfiniteQuery: () => query}));
vi.mock("../auth", () => ({useAuth: () => ({session: {user: {role: "SYSTEM_ADMIN"}}})}));

const station: HmiStation = {
  connection_id: "connection", installation_id: "installation", station_name: "QA 工作站", bench_id: "RD-01",
  operator_name: "alice", operator_display_name: "操作员", online: true, snapshot_fresh: true, state: "running",
  connected_at: "2026-10-08T00:00:00Z", last_seen_at: "2026-10-08T00:00:10Z", snapshot_at: "2026-10-08T00:00:10Z",
  runs: [{session_key: "left", state: "running", test_id: "QA-TEST", sample_id: "QA-SAMPLE", test_case: "SLEG", active_seconds: 5400, completed_cycles: 25}],
  plc: {driver: "mock", communication_state: "LIVE", phase: "RUNNING", powered_channels: [1], stale: false}
};

afterEach(() => { cleanup(); vi.useRealTimers(); });

describe("workstation status", () => {
  it("shows real reported test identity and duration with explicit mock PLC labeling", () => {
    render(<HmiStationCard station={station} />);
    expect(screen.getByText("测试运行中")).toBeTruthy();
    expect(screen.getByText("QA-TEST")).toBeTruthy();
    expect(screen.getByText("QA-SAMPLE")).toBeTruthy();
    expect(screen.getByText("1.50 h")).toBeTruthy();
    expect(screen.getByText("模拟 PLC")).toBeTruthy();
  });

  it("does not claim last reported running state is live after disconnect", () => {
    render(<HmiStationCard station={{...station, online: false, snapshot_fresh: false, state: "offline"}} />);
    expect(screen.getByText("离线")).toBeTruthy();
    expect(screen.queryByText("测试运行中")).toBeNull();
    expect(screen.getByText("最后状态：运行中")).toBeTruthy();
    expect(screen.getByText(/不能据此判断设备当前是否仍在运行/)).toBeTruthy();
  });

  it("labels paused monitoring as a historical snapshot", () => {
    render(<HmiStationCard station={station} live={false} />);
    expect(screen.getByText("历史快照")).toBeTruthy();
    expect(screen.queryByText("测试运行中")).toBeNull();
  });

  it("stops claiming a live test when a browser refresh request hangs", () => {
    vi.useFakeTimers();
    query.dataUpdatedAt = Date.now();
    query.data.pages = [{items: [station], as_of_at: station.last_seen_at}];
    render(<HmiStationsPage />);
    const card = within(screen.getByRole("article", {name: "QA 工作站"}));
    expect(card.getByText("测试运行中")).toBeTruthy();
    act(() => vi.advanceTimersByTime(16000));
    expect(card.queryByText("测试运行中")).toBeNull();
    expect(screen.getByText("历史快照")).toBeTruthy();
    expect(screen.getByText(/页面暂未获取最新状态/)).toBeTruthy();
  });

  it("keeps an active parallel test visible in running filter when another session faults", () => {
    query.dataUpdatedAt = Date.now();
    query.data.pages = [{items: [{...station, state: "fault", runs: [...station.runs, {...station.runs[0], session_key: "right", state: "fault", test_id: "FAULTED-TEST"}]}], as_of_at: station.last_seen_at}];
    render(<HmiStationsPage />);
    fireEvent.change(screen.getByLabelText("显示工位"), {target: {value: "running"}});
    expect(screen.getByText("QA-TEST")).toBeTruthy();
    expect(screen.getByText("FAULTED-TEST")).toBeTruthy();
    expect(screen.getByText("1 台在线 · 1 台测试运行中")).toBeTruthy();
  });
});
