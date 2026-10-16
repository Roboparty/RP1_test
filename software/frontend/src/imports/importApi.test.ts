// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import { archivesApi, type Archive } from "../archiveApi";
import { importApi, uploadCsv } from "./importApi";
const base = { id: "arc", execution_uuid: "uuid", sha256: "a".repeat(64), size_bytes: 9 * 1024 * 1024, received_bytes: 0, upload_state: "UPLOADING", is_owner: true, can_edit: true } as Archive;
afterEach(() => vi.restoreAllMocks());
it("resumes from server receipt after a lost response and uses 4 MiB chunks", async () => {
  const file = new File([new Uint8Array(base.size_bytes)], "raw.csv");
  vi.spyOn(archivesApi, "detail").mockResolvedValue({ ...base, received_bytes: 4 * 1024 * 1024 });
  const chunk = vi.spyOn(importApi, "chunk").mockImplementation(async (_id, offset, body) => ({ ...base, received_bytes: offset + body.size }));
  const complete = vi.spyOn(importApi, "complete").mockResolvedValue({ ...base, received_bytes: file.size, upload_state: "COMPLETE" });
  const result = await uploadCsv(file, base, new AbortController().signal, vi.fn());
  expect(chunk.mock.calls.map((call) => [call[1], call[2].size])).toEqual([[4 * 1024 * 1024, 4 * 1024 * 1024], [8 * 1024 * 1024, 1024 * 1024]]);
  expect(complete).toHaveBeenCalledWith("arc", "uuid:complete", expect.any(AbortSignal)); expect(result.upload_state).toBe("COMPLETE");
});
it("does not retransmit a completed archive", async () => {
  vi.spyOn(archivesApi, "detail").mockResolvedValue({ ...base, received_bytes: base.size_bytes, upload_state: "COMPLETE" });
  const chunk = vi.spyOn(importApi, "chunk");
  await uploadCsv(new File([new Uint8Array(base.size_bytes)], "raw.csv"), base, new AbortController().signal, vi.fn());
  expect(chunk).not.toHaveBeenCalled();
});
it("rejects mismatched file identity and another owner's upload", async () => {
  vi.spyOn(archivesApi, "detail").mockResolvedValue({ ...base, sha256: "b".repeat(64) });
  await expect(uploadCsv(new File([new Uint8Array(base.size_bytes)], "raw.csv"), base, new AbortController().signal, vi.fn())).rejects.toThrow("不一致");
  vi.mocked(archivesApi.detail).mockResolvedValue({ ...base, is_owner: false });
  await expect(uploadCsv(new File([new Uint8Array(base.size_bytes)], "raw.csv"), base, new AbortController().signal, vi.fn())).rejects.toThrow("当前账户");
});
it("never completes after an aborted upload", async () => {
  vi.spyOn(archivesApi, "detail").mockResolvedValue(base);
  const complete = vi.spyOn(importApi, "complete"); const controller = new AbortController(); controller.abort();
  await expect(uploadCsv(new File([new Uint8Array(base.size_bytes)], "raw.csv"), base, controller.signal, vi.fn())).rejects.toThrow();
  expect(complete).not.toHaveBeenCalled();
});
