export type Role = "VIEWER" | "TEST_EXECUTOR" | "SYSTEM_ADMIN";
export type AccessLevel = "VIEW" | "EDIT";
export type AccessRequestStatus = "PENDING" | "APPROVED" | "REJECTED" | "CANCELLED";

export type User = {
  id: string;
  username: string;
  display_name: string;
  role: Role;
  kind: "HUMAN" | "SERVICE";
  enabled: boolean;
  must_change_password: boolean;
  created_at?: string | null;
  disabled_at?: string | null;
};

export type CampaignAccess = {
  campaign_id: string;
  campaign_code: string;
  campaign_name: string;
  asset_kind: "WHOLE_MACHINE" | "MODULE";
  access_level: AccessLevel;
  granted_at: string | null;
  granted_by: string | null;
};

export type Session = {
  user: User;
  campaign_access: CampaignAccess[];
  session_type: "BROWSER" | "DEVELOPMENT" | "SERVICE_API";
  expires_at: string | null;
};

export type AvailableCampaign = {
  campaign_id: string;
  campaign_code: string;
  campaign_name: string;
  asset_kind: "WHOLE_MACHINE" | "MODULE";
  current_grant: AccessLevel | null;
  pending_request_id: string | null;
  pending_requested_level: AccessLevel | null;
};

export type AccessRequest = {
  id: string;
  requester_id: string | null;
  requester_username: string | null;
  campaign_id: string;
  campaign_code: string;
  campaign_name: string;
  requested_level: AccessLevel;
  reason: string;
  status: AccessRequestStatus;
  decision_reason: string | null;
  created_at: string;
  updated_at: string;
  decided_at: string | null;
  decided_by: string | null;
};

export type AuditEvent = {
  id: string;
  event_type: string;
  actor_id: string | null;
  actor_username: string | null;
  username: string;
  outcome: "SUCCESS" | "FAILURE" | "DENIED";
  request_id: string;
  source_ip: string | null;
  created_at: string;
};

export const roleLabels: Record<Role, string> = {
  VIEWER: "查看者",
  TEST_EXECUTOR: "测试执行者",
  SYSTEM_ADMIN: "系统管理员"
};

export const capabilities: Record<Role, { view: boolean; edit: boolean; admin: boolean }> = {
  VIEWER: { view: true, edit: false, admin: false },
  TEST_EXECUTOR: { view: true, edit: true, admin: false },
  SYSTEM_ADMIN: { view: true, edit: true, admin: true }
};
