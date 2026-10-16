const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);
let unauthorizedHandler: (() => void) | null = null;

const SESSION_INVALIDATION_CODES = new Set([
  "authentication_required",
  "invalid_session",
  "session_expired",
  "session_revoked"
]);

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: unknown;

  constructor(message: string, status: number, code = "api_error", details: unknown = []) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export function setUnauthorizedHandler(handler: (() => void) | null) {
  unauthorizedHandler = handler;
  return () => {
    if (unauthorizedHandler === handler) unauthorizedHandler = null;
  };
}

function cookieValue(name: string) {
  const prefix = `${encodeURIComponent(name)}=`;
  const match = document.cookie.split("; ").find((item) => item.startsWith(prefix));
  return match ? decodeURIComponent(match.slice(prefix.length)) : "";
}

function requestId() {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }

  const bytes = new Uint8Array(16);
  if (typeof globalThis.crypto?.getRandomValues === "function") {
    globalThis.crypto.getRandomValues(bytes);
  } else {
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256);
    }
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0"));
  return [
    hex.slice(0, 4).join(""),
    hex.slice(4, 6).join(""),
    hex.slice(6, 8).join(""),
    hex.slice(8, 10).join(""),
    hex.slice(10, 16).join("")
  ].join("-");
}

function headerValue(value: string) {
  for (let index = 0; index < value.length; index += 1) {
    if (value.charCodeAt(index) > 255) return encodeURIComponent(value);
  }
  return value;
}

function buildHeaders(source: HeadersInit | undefined) {
  const headers = new Headers();
  if (!source) return headers;
  const pairs = source instanceof Headers
    ? source.entries()
    : Array.isArray(source)
      ? source
      : Object.entries(source);
  for (const [name, value] of pairs) headers.set(name, headerValue(String(value)));
  return headers;
}

export async function apiRequest<T>(
  path: string,
  init: RequestInit = {},
  parse?: (data: unknown) => T
): Promise<T> {
  const headers = buildHeaders(init.headers);
  const method = (init.method ?? "GET").toUpperCase();
  if (init.body != null && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  headers.set("X-Request-Id", requestId());
  if (!SAFE_METHODS.has(method) && !headers.has("X-CSRF-Token")) {
    const csrf = cookieValue("rp1_csrf");
    if (csrf) headers.set("X-CSRF-Token", csrf);
  }
  const configuredDevUserId = import.meta.env.DEV
    ? import.meta.env.VITE_DEV_USER_PUBLIC_ID?.trim()
    : "";
  if (configuredDevUserId) headers.set("X-User-Public-Id", configuredDevUserId);

  const response = await fetch(path, { ...init, credentials: "include", headers });
  if (response.status === 204 && response.ok) return undefined as T;
  const rawBody = await response.text();
  let payload: unknown = null;
  try {
    payload = rawBody ? JSON.parse(rawBody) : null;
  } catch {
    // Converted to a stable error below; response bodies are never logged.
  }
  const record = payload && typeof payload === "object" ? payload as Record<string, unknown> : null;
  const failureRecord = record?.error && typeof record.error === "object"
    ? record.error as Record<string, unknown>
    : null;
  const failure = failureRecord &&
    typeof failureRecord.code === "string" &&
    typeof failureRecord.message === "string"
    ? {
        code: failureRecord.code,
        message: failureRecord.message,
        details: failureRecord.details
      }
    : null;
  if (!response.ok || !record || !("data" in record)) {
    const invalidSuccessResponse = response.ok;
    if (
      response.status === 401 &&
      failure?.code &&
      SESSION_INVALIDATION_CODES.has(failure.code)
    ) {
      unauthorizedHandler?.();
    }
    throw new ApiError(
      failure?.message ?? (invalidSuccessResponse
        ? "服务返回了无法识别的数据格式。"
        : `服务暂时不可用（HTTP ${response.status}）。`),
      invalidSuccessResponse ? 502 : response.status,
      failure?.code ?? "invalid_response",
      failure?.details ?? []
    );
  }
  return parse ? parse(record.data) : record.data as T;
}
