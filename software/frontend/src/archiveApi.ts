import { apiRequest } from "./client";

export type ArchiveOrigin = "HMI" | "HISTORY" | "OTHER";
export type ArchiveVerdict = "passed" | "failed" | "blocked";
export type ArchiveCatalogItem = { key: string; subject: string; metric: string; unit: string };
export type ArchiveVerification = { revision: number; tester: string; station_id: string; station_code?: string; station_name?: string; verdict: ArchiveVerdict; notes: string; discrepancy_reason: string; declared_started_at?: string | null; declared_ended_at?: string | null; created_at: string };
export type ArchiveContext = { campaign_id: string; cycle_id: string; segment_id: string; asset_id: string; configuration_id: string; test_case_version_id: string; station_id: string };
export type Archive = {
  id: string; execution_uuid: string; archive_code: string; owner_public_id: string; is_owner: boolean; can_edit: boolean; can_analyze?: boolean;
  origin: ArchiveOrigin; test_name: string; actual_object: string; station_label: string; filename: string; size_bytes: number; sha256: string; received_bytes: number;
  upload_state: "UPLOADING" | "COMPLETE" | "CORRUPT"; processing_state: "WAITING" | "QUEUED" | "PROCESSING" | "READY" | "FAILED";
  processing_error: string | null; classification_state: "UNCLASSIFIED" | "CLASSIFIED"; revision: number;
  facts: Record<string, unknown>; acceptance: Record<string, unknown>;
  manifest: { analysis?: Record<string, unknown>; catalog?: ArchiveCatalogItem[]; time_range?: { start: number; end: number }; stages?: unknown[]; cycles?: unknown[]; row_count?: number; [key: string]: unknown } | null;
  current_verification: ArchiveVerification | null; classification: (ArchiveContext & { revision: number }) | null;
  submission_id: string | null; submission_revision: number | null; submission_status: "PENDING" | "APPROVED" | "REJECTED" | null;
  execution_id: string | null; execution_code: string | null; created_at: string;
  verified_station_code?: string | null; verified_station_name?: string | null;
  verification_history?: { revision: number; payload: ArchiveVerification; reason: string; created_at: string; actor_name?: string }[];
  classification_history?: { revision: number; payload: ArchiveContext; reason: string; created_at: string; actor_name?: string }[];
};
export type ArchiveCurve = { key: string; subject: string; metric: string; unit: string; points: [number, number | null][]; sample_count: number; returned_points: number; downsampled: boolean };
export type ArchiveAnalysis = { id: string; version: number; source_test_case_version_id: string; source_case_code: string; source_case_name: string; source_version: string; mapping_status: "MAPPED" | "UNCONFIGURED" | "INVALID"; mapping_notes: string; acceptance: Record<string, unknown>; report: Record<string, unknown>; original_sha256: string; original_analysis_version: string | number; actor_name: string; reason: string; created_at: string };
export type ArchiveCurves = { archive_id: string; execution_uuid: string; archive_code: string; range: { start: number; end: number }; series: ArchiveCurve[]; gaps: { start: number; end: number }[]; resolution: "raw" | "minmax"; catalog: ArchiveCatalogItem[]; time_range: { start: number; end: number } };
export const archiveUploadLabels = { UPLOADING: "上传中", COMPLETE: "归档完成", CORRUPT: "校验失败" };
export const archiveProcessingLabels = { WAITING: "等待归档", QUEUED: "等待解析", PROCESSING: "解析中", READY: "可查询", FAILED: "解析失败" };
export const verdictLabels: Record<ArchiveVerdict, string> = { passed: "通过", failed: "失败", blocked: "待判定 / 阻塞" };

function queryString(values: Record<string, string | number | null | undefined>) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) if (value !== undefined && value !== null && value !== "") search.set(key, String(value));
  return search.toString();
}
function mutate(id: string, action: string, payload: unknown, reason: string, key: string) {
  return apiRequest<Archive>(`/api/v1/archives/${encodeURIComponent(id)}/${action}`, { method: "POST", headers: { "Idempotency-Key": key, "X-Change-Reason": reason }, body: JSON.stringify(payload) });
}
export const archivesApi = {
  list: (filters: { q?: string; origin?: string; classification_state?: string; processing_state?: string; execution_uuid?: string; before?: number | null } = {}, signal?: AbortSignal) => apiRequest<{ items: Archive[]; next_cursor: number | null }>(`/api/v1/archives?${queryString({ limit: 50, ...filters })}`, { signal }),
  detail: (id: string, signal?: AbortSignal) => apiRequest<Archive>(`/api/v1/archives/${encodeURIComponent(id)}`, { signal }),
  analyses: (id: string, signal?: AbortSignal) => apiRequest<{ items: ArchiveAnalysis[]; original_analysis: Record<string, unknown> }>(`/api/v1/archives/${encodeURIComponent(id)}/analyses`, { signal }),
  analyze: (id: string, payload: { test_case_version_id: string; reason: string }, key: string) => apiRequest<ArchiveAnalysis>(`/api/v1/archives/${encodeURIComponent(id)}/analyses`, { method: "POST", headers: { "Idempotency-Key": key, "X-Change-Reason": payload.reason }, body: JSON.stringify(payload) }),
  curves: (id: string, filters: { metrics: string; subjects?: string; start?: number; end?: number; max_points?: number; stage?: string; cycle?: string }, signal?: AbortSignal) => apiRequest<ArchiveCurves>(`/api/v1/archives/${encodeURIComponent(id)}/curves?${queryString(filters)}`, { signal }),
  verify: (id: string, payload: { expected_revision: number; tester: string; station_id: string; verdict: ArchiveVerdict; notes: string; discrepancy_reason: string; reason: string; declared_started_at?: string; declared_ended_at?: string }, key: string) => mutate(id, "verification", payload, payload.reason, key),
  classify: (id: string, payload: { expected_revision: number; context: ArchiveContext; reason: string }, key: string) => mutate(id, "classification", payload, payload.reason, key),
  submit: (id: string, payload: { expected_revision: number; expected_submission_revision: number; reason: string }, key: string) => mutate(id, "submit", payload, payload.reason, key)
};

export function archiveTitle(item: Pick<Archive, "execution_code" | "archive_code" | "test_name">) {
  return item.execution_code || item.archive_code || item.test_name;
}

export function archiveStation(item: Archive) {
  const code = item.current_verification?.station_code ?? item.verified_station_code;
  const name = item.current_verification?.station_name ?? item.verified_station_name;
  return code ? `${code}${name ? ` · ${name}` : ""}` : item.station_label;
}

export function missingArchiveAcquisitionFacts(item: Archive) {
  const missing = ["active_seconds", "completed_cycles", "exception_count"].filter((key) => typeof item.facts[key] !== "number" || !Number.isFinite(item.facts[key]));
  if (!item.facts.machine_status || item.facts.machine_status === "unknown") missing.push("machine_status");
  return missing;
}
