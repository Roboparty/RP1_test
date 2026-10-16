import { ApiError, apiRequest } from "./client";
import type {
  AccessLevel,
  AccessRequest,
  AuditEvent,
  AvailableCampaign,
  CampaignAccess,
  Role,
  Session,
  User
} from "./security";

const roles = new Set<Role>(["VIEWER", "TEST_EXECUTOR", "SYSTEM_ADMIN"]);
const principalKinds = new Set<User["kind"]>(["HUMAN", "SERVICE"]);
const sessionTypes = new Set<Session["session_type"]>(["BROWSER", "DEVELOPMENT", "SERVICE_API"]);
const assetKinds = new Set<CampaignAccess["asset_kind"]>(["WHOLE_MACHINE", "MODULE"]);
const accessLevels = new Set<AccessLevel>(["VIEW", "EDIT"]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

export function parseSession(value: unknown): Session {
  if (!isRecord(value) || !isRecord(value.user) || !Array.isArray(value.campaign_access)) {
    throw new ApiError("认证服务返回了无效的会话数据。", 502, "invalid_response_schema");
  }
  const user = value.user;
  const validUser = [user.id, user.username, user.display_name].every((item) => typeof item === "string") &&
    roles.has(user.role as Role) &&
    principalKinds.has(user.kind as User["kind"]) &&
    typeof user.enabled === "boolean" &&
    typeof user.must_change_password === "boolean";
  const validCampaignAccess = value.campaign_access.every((item) =>
    isRecord(item) &&
    [item.campaign_id, item.campaign_code, item.campaign_name].every((field) => typeof field === "string") &&
    assetKinds.has(item.asset_kind as CampaignAccess["asset_kind"]) &&
    accessLevels.has(item.access_level as AccessLevel) &&
    isNullableString(item.granted_at) &&
    isNullableString(item.granted_by)
  );
  if (
    !validUser ||
    !validCampaignAccess ||
    !sessionTypes.has(value.session_type as Session["session_type"]) ||
    !isNullableString(value.expires_at)
  ) {
    throw new ApiError("认证服务返回了无效的会话数据。", 502, "invalid_response_schema");
  }
  return value as Session;
}

export const authApi = {
  login: (username: string, password: string) =>
    apiRequest<{ user: User; expires_at: string }>("/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password })
    }),
  me: (signal?: AbortSignal) => apiRequest<Session>("/api/v1/auth/me", { signal }, parseSession),
  logout: () => apiRequest<void>("/api/v1/auth/logout", { method: "POST" }),
  changePassword: (currentPassword: string, newPassword: string) =>
    apiRequest<User>("/api/v1/auth/change-password", {
      method: "POST",
      body: JSON.stringify({ current_password: currentPassword, new_password: newPassword })
    })
};

export const accessApi = {
  available: (signal?: AbortSignal) => apiRequest<AvailableCampaign[]>("/api/v1/campaign-access/available", { signal }),
  mine: (signal?: AbortSignal) => apiRequest<AccessRequest[]>("/api/v1/access-requests/mine", { signal }),
  submit: (campaignId: string, requestedLevel: AccessLevel, reason: string) =>
    apiRequest<AccessRequest>("/api/v1/access-requests", {
      method: "POST",
      body: JSON.stringify({
        campaign_id: campaignId,
        requested_level: requestedLevel,
        reason
      })
    }),
  cancel: (requestId: string) =>
    apiRequest<AccessRequest>(`/api/v1/access-requests/${requestId}/cancel`, { method: "POST" })
};

export const adminApi = {
  users: (signal?: AbortSignal) => apiRequest<User[]>("/api/v1/admin/users", { signal }),
  createUser: (input: {
    username: string;
    display_name: string;
    role: Role;
    password: string;
    must_change_password: boolean;
  }) => apiRequest<User>("/api/v1/admin/users", {
    method: "POST",
    body: JSON.stringify(input)
  }),
  disableUser: (userId: string, reason: string) =>
    apiRequest<User>(`/api/v1/admin/users/${userId}/disable`, {
      method: "POST",
      body: JSON.stringify({ reason })
    }),
  requests: (status = "PENDING", signal?: AbortSignal) =>
    apiRequest<AccessRequest[]>(`/api/v1/admin/access-requests?status=${encodeURIComponent(status)}`, { signal }),
  decide: (requestId: string, decision: "approve" | "reject", reason: string) =>
    apiRequest<AccessRequest>(`/api/v1/admin/access-requests/${requestId}/${decision}`, {
      method: "POST",
      body: JSON.stringify({ reason })
    }),
  grants: (userId: string, signal?: AbortSignal) =>
    apiRequest<CampaignAccess[]>(`/api/v1/admin/users/${encodeURIComponent(userId)}/campaign-grants`, { signal }),
  grant: (userId: string, campaignId: string, accessLevel: AccessLevel, reason: string) =>
    apiRequest<CampaignAccess>(`/api/v1/admin/users/${userId}/campaign-grants`, {
      method: "POST",
      body: JSON.stringify({ campaign_id: campaignId, access_level: accessLevel, reason })
    }),
  revoke: (userId: string, campaignId: string, reason: string) =>
    apiRequest<void>(`/api/v1/admin/users/${userId}/campaign-grants/${campaignId}`, {
      method: "DELETE",
      body: JSON.stringify({ reason })
    }),
  audit: async (limit = 200, offset = 0, signal?: AbortSignal): Promise<AuditEvent[]> => {
    const rows = await apiRequest<Array<AuditEvent & { metadata?: unknown }>>(
      `/api/v1/admin/audit/security?limit=${limit}&offset=${offset}`,
      { signal }
    );
    return rows.map(({ metadata: _metadata, ...event }) => event);
  }
};
