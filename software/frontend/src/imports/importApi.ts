import { apiRequest } from "../client";
import { archivesApi, type Archive, type ArchiveContext } from "../archiveApi";
export type ImportCreate = { filename: string; size_bytes: number; sha256: string; test_name: string; actual_object: string; station_id: string; classification?: ArchiveContext | null };
export const importApi = {
  lookup: (sha256: string, size: number, signal?: AbortSignal) => apiRequest<{ archive: Archive | null }>(`/api/v1/archive-imports/lookup?${new URLSearchParams({ sha256, size_bytes: String(size) })}`, { signal }),
  create: (payload: ImportCreate, key: string, signal?: AbortSignal) => apiRequest<{ archive: Archive; reused: boolean }>("/api/v1/archive-imports", { method: "POST", headers: { "Idempotency-Key": key }, body: JSON.stringify(payload), signal }),
  chunk: (id: string, offset: number, body: Blob, key: string, signal?: AbortSignal) => apiRequest<Archive>(`/api/v1/archives/${encodeURIComponent(id)}/content?offset=${offset}`, { method: "PUT", headers: { "Content-Type": "application/octet-stream", "Idempotency-Key": key }, body, signal }),
  complete: (id: string, key: string, signal?: AbortSignal) => apiRequest<Archive>(`/api/v1/archives/${encodeURIComponent(id)}/complete`, { method: "POST", headers: { "Idempotency-Key": key }, signal })
};
const CHUNK_SIZE = 4 * 1024 * 1024;
export async function uploadCsv(file: File, initial: Archive, signal: AbortSignal, progress: (archive: Archive) => void): Promise<Archive> {
  // Re-read the durable receipt: an interrupted request may have committed its bytes.
  let archive = await archivesApi.detail(initial.id, signal);
  if (archive.sha256 !== initial.sha256 || archive.size_bytes !== file.size) throw new Error("续传文件与原始归档身份不一致，请重新选择原文件。");
  if (!archive.is_owner || !archive.can_edit) throw new Error("此归档不可由当前账户继续上传。请查看原归档。");
  if (archive.upload_state === "CORRUPT") throw new Error("原归档校验失败，请保留原件并联系管理员；不会覆盖原归档。");
  if (archive.upload_state === "COMPLETE") { progress(archive); return archive; }
  while (archive.received_bytes < file.size) {
    signal.throwIfAborted();
    const offset = archive.received_bytes;
    const next = await importApi.chunk(archive.id, offset, file.slice(offset, Math.min(file.size, offset + CHUNK_SIZE)), `${archive.execution_uuid}:chunk:${offset}`, signal);
    if (next.received_bytes <= offset || next.received_bytes > file.size) throw new Error("服务器上传回执偏移异常，请重试核对进度。");
    archive = next; progress(archive);
  }
  archive = await importApi.complete(archive.id, `${archive.execution_uuid}:complete`, signal); progress(archive); return archive;
}
export function importRequestKey(owner: string, sha256: string) {
  const name = `rp1:csv-import:${owner}:${sha256}`;
  try {
    const old = sessionStorage.getItem(name);
    if (old) return old;
    const key = crypto.randomUUID(); sessionStorage.setItem(name, key); return key;
  } catch { return crypto.randomUUID(); }
}
