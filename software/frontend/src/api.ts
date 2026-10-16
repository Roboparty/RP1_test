import type {
  AssetDetail,
  AssetList,
  AssetPerformanceTrends,
  AssetKind,
  DashboardHome,
  DataSourceHealth,
  EventList,
  ExecutionDetail,
  ExecutionJointAnalysis,
  ExecutionList,
  MtbfConclusionList,
  TestCaseDetail,
  TestCaseDurationList
} from "./types";
import { apiRequest } from "./client";

function queryString(values: Record<string, string | number | null | undefined>) {
  const params = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => {
    if (value !== null && value !== undefined && value !== "") params.set(key, String(value));
  });
  const value = params.toString();
  return value ? `?${value}` : "";
}

export function getDashboardHome(signal?: AbortSignal) {
  return apiRequest<DashboardHome>("/api/v1/dashboard/home", { signal });
}

export function getDataSourceHealth(signal?: AbortSignal) {
  return apiRequest<DataSourceHealth>("/api/v1/data-sources/health", { signal });
}

export function getTestCaseDurations(filters: {
  assetKind?: AssetKind;
  asset?: string;
  testCase?: string;
  status?: string;
  search?: string;
  cursor?: string;
  limit?: number;
}, signal?: AbortSignal) {
  return apiRequest<TestCaseDurationList>(
    `/api/v1/test-cases/durations${queryString({
      asset_kind: filters.assetKind,
      asset: filters.asset,
      test_case: filters.testCase,
      status: filters.status,
      search: filters.search,
      cursor: filters.cursor,
      limit: filters.limit ?? 50
    })}`,
    { signal }
  );
}

export function getAssets(filters: {
  assetKind?: AssetKind;
  targetPartCode?: string;
  status?: string;
  search?: string;
  cursor?: string;
  limit?: number;
}, signal?: AbortSignal) {
  return apiRequest<AssetList>(
    `/api/v1/assets${queryString({
      asset_kind: filters.assetKind,
      target_part_code: filters.targetPartCode,
      status: filters.status,
      search: filters.search,
      cursor: filters.cursor,
      limit: filters.limit ?? 50
    })}`,
    { signal }
  );
}

export async function getAllAssets(
  filters: Omit<Parameters<typeof getAssets>[0], "cursor" | "limit">,
  signal?: AbortSignal
) {
  const items = new Map<string, AssetList["items"][number]>();
  const seenCursors = new Set<string>();
  let cursor: string | undefined;
  let firstPage: AssetList | undefined;

  do {
    const page = await getAssets({ ...filters, cursor, limit: 100 }, signal);
    firstPage ??= page;
    page.items.forEach((item) => items.set(item.id, item));
    const nextCursor = page.page.next_cursor ?? undefined;
    if (!nextCursor || seenCursors.has(nextCursor)) break;
    seenCursors.add(nextCursor);
    cursor = nextCursor;
  } while (cursor);

  if (!firstPage) throw new Error("样品目录未返回有效分页。");
  return {
    ...firstPage,
    items: [...items.values()],
    page: { limit: items.size, next_cursor: null }
  } satisfies AssetList;
}

export function getAsset(identifier: string, signal?: AbortSignal) {
  return apiRequest<AssetDetail>(`/api/v1/assets/${encodeURIComponent(identifier)}`, { signal });
}

export function getAssetPerformanceTrends(identifier: string, signal?: AbortSignal) {
  return apiRequest<AssetPerformanceTrends>(
    `/api/v1/assets/${encodeURIComponent(identifier)}/performance-trends`,
    { signal }
  );
}

export function getAssetExecutions(identifier: string, cursor?: string, signal?: AbortSignal) {
  return apiRequest<ExecutionList>(
    `/api/v1/assets/${encodeURIComponent(identifier)}/executions${queryString({ cursor, limit: 50 })}`,
    { signal }
  );
}

export function getAssetEvents(identifier: string, cursor?: string, signal?: AbortSignal) {
  return apiRequest<EventList>(
    `/api/v1/assets/${encodeURIComponent(identifier)}/events${queryString({ cursor, limit: 50 })}`,
    { signal }
  );
}

export function getTestCase(identifier: string, cursor?: string, signal?: AbortSignal) {
  return apiRequest<TestCaseDetail>(
    `/api/v1/test-cases/${encodeURIComponent(identifier)}${queryString({ cursor, limit: 50 })}`,
    { signal }
  );
}

export function getLiveExecutions(signal?: AbortSignal) {
  return apiRequest<ExecutionList>("/api/v1/executions/live", { signal });
}

export function getExecution(identifier: string, signal?: AbortSignal) {
  return apiRequest<ExecutionDetail>(`/api/v1/executions/${encodeURIComponent(identifier)}`, { signal });
}

export function getExecutionJointAnalysis(
  identifier: string,
  filters: { subjectCode?: string; stageCode?: string; cycleIndex?: number } = {},
  signal?: AbortSignal
) {
  return apiRequest<ExecutionJointAnalysis>(
    `/api/v1/executions/${encodeURIComponent(identifier)}/joint-analysis${queryString({
      subject_code: filters.subjectCode,
      stage_code: filters.stageCode,
      cycle_index: filters.cycleIndex
    })}`,
    { signal }
  );
}

export function getMtbfConclusions(assetKind: AssetKind, cursor?: string, signal?: AbortSignal) {
  return apiRequest<MtbfConclusionList>(
    `/api/v1/mtbf/conclusions${queryString({ asset_kind: assetKind, cursor, limit: 50 })}`,
    { signal }
  );
}
