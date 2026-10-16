import { apiRequest } from "./client";

export type HmiRun = {
  session_key: string;
  state: "idle" | "armed" | "running" | "paused" | "stopping" | "fault" | "completed" | "unknown";
  test_id: string;
  execution_code?: string;
  case_code?: string;
  sample_id: string;
  test_case: string;
  active_seconds: number;
  completed_cycles: number;
};

export type HmiStation = {
  connection_id: string;
  installation_id: string;
  station_name: string;
  bench_id: string;
  operator_name: string;
  operator_display_name: string;
  online: boolean;
  snapshot_fresh: boolean;
  state: "offline" | "unknown" | "running" | "paused" | "fault" | "powered" | "idle";
  connected_at: string;
  last_seen_at: string;
  snapshot_at: string;
  runs: HmiRun[];
  plc: { driver: string; communication_state: string; phase: string; powered_channels: number[]; stale: boolean };
};

export type HmiStationList = {
  items: HmiStation[];
  next_cursor: string | null;
  as_of_at: string;
  offline_after_seconds: number;
  snapshot_stale_after_seconds: number;
};

export const stationLabels: Record<HmiStation["state"], string> = {
  offline: "离线", unknown: "状态待确认", running: "测试运行中",
  paused: "测试已暂停", fault: "故障", powered: "电柜已供电", idle: "在线空闲"
};

export const runLabels: Record<HmiRun["state"], string> = {
  idle: "空闲", armed: "就绪", running: "运行中", paused: "已暂停",
  stopping: "正在停止", fault: "故障", completed: "已结束", unknown: "未知"
};

export function isStationTesting(station: HmiStation) {
  return station.online && station.snapshot_fresh && station.runs.some((run) => run.state === "running" || run.state === "stopping");
}

export function getHmiStations(cursor: string | null, signal?: AbortSignal) {
  const query = new URLSearchParams({ limit: "100" });
  if (cursor) query.set("cursor", cursor);
  return apiRequest<HmiStationList>(`/api/v1/hmi/stations?${query}`, { signal });
}
