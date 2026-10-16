import { describe, expect, it } from "vitest";
import { Sha256 } from "./sha256";
import { CsvScanner, inspectCsv } from "./csvPreflight";

const referenceHash = async (text: string) => Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text))), (n) => n.toString(16).padStart(2, "0")).join("");

describe("incremental original-file SHA-256", () => {
  it.each(["", "abc", "a".repeat(1000000), "测试原件\r\n".repeat(10000)])("matches independent Web Crypto", async (text) => {
    const bytes = new TextEncoder().encode(text), hash = new Sha256();
    for (let i = 0; i < bytes.length; i += 127) hash.update(bytes.slice(i, i + 127));
    expect(hash.digest()).toBe(await referenceHash(text));
  });
});

describe("bounded CSV preflight", () => {
  it("handles quoted fields, split escaped quotes, CRLF and BOM without changing original bytes", async () => {
    const text = '\uFEFFtime_s,j1_torque_nm,stage\r\n0,3,"a,""b"""\r\n1,-5,hold\r\n';
    const scanner = new CsvScanner();
    for (const char of text) scanner.feed(char);
    const result = scanner.finish();
    expect(result.rows).toBe(2); expect(result.columns[1]).toMatchObject({ min: -5, max: 3, unit: "N·m" });
    const inspected = await inspectCsv(new Blob([text]));
    expect(inspected.sha256).toBe(await referenceHash(text));
  });
  it("marks all-NaN parameters missing and never turns sample or elapsed span into machine facts", async () => {
    const result = await inspectCsv(new Blob(["time_s,sample,j1_bus_voltage_v,j1_torque_nm\n0,0,nan,-12\n4,200,nan,17\n"]));
    expect(result).toMatchObject({ rows: 2, start: 0, end: 4, maxGap: 4, errors: [] });
    expect(result.columns[2]).toMatchObject({ count: 0, missing: 2, min: null, max: null, unit: "V" });
    expect(result.warnings.join(" ")).toContain("全部缺失");
    expect(result).not.toHaveProperty("active_seconds"); expect(result).not.toHaveProperty("completed_cycles"); expect(result).not.toHaveProperty("automatic_verdict");
  });
  it.each([
    ["time_s,a,a\n0,1,2", "不重复"],
    ["when,a\n0,1", "缺少"],
    ["time_s,a\n0,1,2", "第 2 行"],
    ['time_s,a\n0,"oops', "未闭合"],
    ["time_s,_private\n0,1", "保留字段"]
  ])("rejects malformed input: %s", async (csv, expected) => { await expect(inspectCsv(new Blob([csv]))).rejects.toThrow(expected); });
  it("flags invalid times and non-monotonic samples", async () => {
    const result = await inspectCsv(new Blob(["time_s,a\n3,1\n2,4\nNaN,8\n"]));
    expect(result.invalidTime).toBe(1); expect(result.nonMonotonic).toBe(1); expect(result.errors).toHaveLength(1);
  });
  it("rejects non UTF-8 rather than silently repairing bytes", async () => { await expect(inspectCsv(new Blob([new Uint8Array([0xff, 0x80])]))).rejects.toThrow(); });
});

it("uses decimal parsing consistent with server conversion rather than JavaScript hex/binary coercion", async () => {
  const result = await inspectCsv(new Blob(["time_s,a\n0,0x10\n1,0b10\n2,1_000.5\n3,+1e2\n4,1__0\n"]));
  expect(result.columns[1]).toMatchObject({ count: 2, missing: 3, min: 100, max: 1000.5 });
});
