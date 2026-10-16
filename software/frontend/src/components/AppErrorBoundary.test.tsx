// @vitest-environment jsdom

import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AppErrorBoundary } from "./AppErrorBoundary";

function BrokenView(): never {
  throw new Error("render failed");
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("application error boundary", () => {
  it("shows a recoverable fallback without exposing stack details", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    render(
      <AppErrorBoundary>
        <BrokenView />
      </AppErrorBoundary>
    );

    expect(screen.getByRole("alert").textContent).toContain("页面暂时无法显示");
    expect(screen.getByRole("button", { name: "重新加载" })).toBeTruthy();
    expect(screen.queryByText("render failed")).toBeNull();
  });
});
