import { apiRequest } from "./client";
import type { ArchiveVerification } from "./archiveApi";

export type TestSummary = {
  execution_uuid: string; campaign_id: string; cycle_id: string; segment_id: string;
  asset_id: string; configuration_id: string; test_case_version_id: string; station_id: string;
  started_at: string; ended_at: string; active_seconds: number; completed_cycles: number;
  source_status: "passed" | "failed" | "blocked"; data_quality: string; clock_quality: string;
  exception_count: number; summary: string; evidence_sha256: string;
};
export type SubmissionState = "PENDING" | "APPROVED" | "REJECTED";
export type Submission = {
  id: string; cursor?: number; execution_uuid: string; revision: number; status: SubmissionState;
  submitter: string; submitter_name: string; campaign_code: string; created_at: string; updated_at: string;
  can_review: boolean; is_owner?: boolean; execution_id?: string; execution_code?: string;
  archive_id?: string | null; archive_code?: string | null;
  asset_code?: string; asset_name?: string; case_code?: string; case_name?: string;
  test_case_version?: string; station_code?: string; configuration_fingerprint?: string;
  revisions?: { revision: number; summary: TestSummary; submission_reason: string; submitted_at: string;
    decision: SubmissionState | null; review_reason: string | null; reviewed_at: string | null; reviewer: string | null;
    verification?: ArchiveVerification | null; analysis?: Record<string, unknown> | null }[];
};
export const submissionLabels: Record<SubmissionState, string> = { PENDING: "待审核", APPROVED: "已批准", REJECTED: "已驳回" };
export const submissionsApi = {
  list: (status: string, before: number | null, signal?: AbortSignal) => apiRequest<{ items: Submission[]; next_cursor: number | null }>(
    `/api/v1/submissions?limit=50${status ? `&status=${status}` : ""}${before ? `&before=${before}` : ""}`, { signal }),
  detail: (id: string, signal?: AbortSignal) => apiRequest<Submission>(`/api/v1/submissions/${encodeURIComponent(id)}`, { signal }),
  review: (id: string, revision: number, decision: "APPROVED" | "REJECTED", reason: string, key: string) => apiRequest<Submission>(
    `/api/v1/submissions/${encodeURIComponent(id)}/review`, { method: "POST", headers: { "Idempotency-Key": key, "X-Change-Reason": reason }, body: JSON.stringify({ revision, decision, reason }) }),
  submit: (summary: TestSummary, expected_revision: number, reason: string, key: string) => apiRequest<Submission>(
    "/api/v1/submissions", { method: "POST", headers: { "Idempotency-Key": key, "X-Change-Reason": reason }, body: JSON.stringify({ summary, expected_revision, reason }) })
};
