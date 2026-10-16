import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, apiRequest, setUnauthorizedHandler } from "./client";

afterEach(() => {
  vi.unstubAllGlobals();
  setUnauthorizedHandler(null);
});

describe("authenticated API client", () => {
  it("includes cookies and double-submit CSRF on mutations", async () => {
    vi.stubGlobal("document", { cookie: "rp1_csrf=csrf-value" });
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ data: { ok: true } }), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      })
    );
    vi.stubGlobal("fetch", fetchMock);
    await apiRequest("/api/v1/example", { method: "POST", body: "{}" });
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.credentials).toBe("include");
    expect(new Headers(init.headers).get("X-CSRF-Token")).toBe("csrf-value");
  });

  it("encodes a Chinese approval reason so the browser can send the header", async () => {
    vi.stubGlobal("document", { cookie: "" });
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ data: { ok: true } }), { status: 200 })
    );
    vi.stubGlobal("fetch", fetchMock);
    await apiRequest("/api/v1/submissions/example/review", {
      method: "POST",
      headers: { "X-Change-Reason": "可以确认" },
      body: "{}"
    });
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(new Headers(init.headers).get("X-Change-Reason")).toBe(encodeURIComponent("可以确认"));
  });

  it("generates a UUID request id when randomUUID is unavailable over LAN HTTP", async () => {
    vi.stubGlobal("document", { cookie: "" });
    vi.stubGlobal("crypto", {
      getRandomValues: (bytes: Uint8Array) => {
        bytes.fill(0x11);
        return bytes;
      }
    });
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ data: { ok: true } }), { status: 200 })
    );
    vi.stubGlobal("fetch", fetchMock);

    await apiRequest("/api/v1/example");

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(new Headers(init.headers).get("X-Request-Id")).toBe(
      "11111111-1111-4111-9111-111111111111"
    );
  });

  it("preserves structured 403 errors without invalidating the session", async () => {
    vi.stubGlobal("document", { cookie: "" });
    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      new Response(JSON.stringify({
        error: { code: "forbidden", message: "denied", details: [{ field: "role" }] }
      }), { status: 403 })
    ));
    const error = await apiRequest("/api/v1/admin/users").catch((caught) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 403, code: "forbidden" });
    expect(onUnauthorized).not.toHaveBeenCalled();
  });

  it("invalidates the global session only for session-related 401 responses", async () => {
    vi.stubGlobal("document", { cookie: "" });
    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      new Response(JSON.stringify({
        error: { code: "authentication_required", message: "login required", details: [] }
      }), { status: 401 })
    ));
    await expect(apiRequest("/api/v1/auth/me")).rejects.toMatchObject({ status: 401 });
    expect(onUnauthorized).toHaveBeenCalledOnce();
  });

  it("keeps the session when a credential verification endpoint rejects input", async () => {
    vi.stubGlobal("document", { cookie: "" });
    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      new Response(JSON.stringify({
        error: { code: "invalid_credentials", message: "current password is wrong", details: [] }
      }), { status: 401 })
    ));

    await expect(apiRequest("/api/v1/auth/change-password", {
      method: "POST",
      body: JSON.stringify({ current_password: "wrong", new_password: "long-new-password" })
    })).rejects.toMatchObject({ status: 401, code: "invalid_credentials" });
    expect(onUnauthorized).not.toHaveBeenCalled();
  });

  it("rejects malformed success envelopes as an upstream contract failure", async () => {
    vi.stubGlobal("document", { cookie: "" });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ unexpected: true }), { status: 200 })
    ));

    await expect(apiRequest("/api/v1/example")).rejects.toMatchObject({
      status: 502,
      code: "invalid_response"
    });
  });
});
