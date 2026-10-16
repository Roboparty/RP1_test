import { apiRequest } from "./client";
import type { AssetKind, TargetPartCode } from "./types";

export type CatalogCase = { id: string; code: string; name: string; asset_kind: AssetKind; target_part_code: TargetPartCode | null; target_part_name: string | null; enabled: boolean; domain: string; version_id: string | null; version: string | null; procedure_spec: Record<string, unknown> | null };
export type CampaignStage = { sequence_no: number; stage_name: string };
export type Campaign = { id: string; code: string; name: string; asset_kind: AssetKind; status: string; program_code: string | null; program_name: string | null; can_edit: boolean; asset_count: number; case_count: number; stages?: CampaignStage[]; planned_start: string | null; planned_end: string | null };
export type SetupOptions = {
  parts: { code: TargetPartCode; name: string; asset_kind: AssetKind }[];
  programs: { id: string; code: string; name: string; status: string }[];
  stations: { id: string; code: string; name: string; station_type: string }[];
  assets: { id: string; code: string; name: string; asset_kind: AssetKind; target_part_code: TargetPartCode }[];
  configurations: { id: string; asset_id: string; fingerprint: string; reliability_impact: string }[];
};
export type PlannedContext = { id: string; asset_id: string; asset_code: string; case_code: string; case_name: string; version: string; planned_runs: number; status: string; context: Record<string, string> };

export const setupApi = {
  catalog: (signal?: AbortSignal) => apiRequest<CatalogCase[]>("/api/v1/setup/catalog", { signal }),
  options: (signal?: AbortSignal) => apiRequest<SetupOptions>("/api/v1/setup/options", { signal }),
  campaigns: (signal?: AbortSignal) => apiRequest<Campaign[]>("/api/v1/setup/campaigns", { signal }),
  contexts: (id: string, signal?: AbortSignal) => apiRequest<PlannedContext[]>(`/api/v1/setup/campaigns/${encodeURIComponent(id)}/contexts`, { signal }),
  create: (path: string, payload: Record<string, unknown>) => apiRequest<{ id: string }>(`/api/v1/setup/${path}`, {
    method: "POST", headers: { "X-Change-Reason": String(payload.reason ?? "") }, body: JSON.stringify(payload)
  })
};
