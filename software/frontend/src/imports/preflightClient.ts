import type { CsvPreflight } from "./csvPreflight";
export function preflightCsv(file: File, progress: (percent: number) => void, signal: AbortSignal): Promise<CsvPreflight> {
  return new Promise((resolve, reject) => {
    const worker = new Worker(new URL("./csvPreflight.worker.ts", import.meta.url), { type: "module" });
    const close = () => { worker.terminate(); signal.removeEventListener("abort", cancel); };
    const cancel = () => { close(); reject(new DOMException("预检已取消", "AbortError")); };
    if (signal.aborted) { cancel(); return; }
    signal.addEventListener("abort", cancel, { once: true });
    worker.onerror = () => { close(); reject(new Error("无法启动 CSV 预检，请重新选择文件。")); };
    worker.onmessage = (event: MessageEvent<{ type: string; percent: number; result: CsvPreflight; message: string }>) => {
      if (event.data.type === "progress") progress(event.data.percent);
      if (event.data.type === "done") { close(); resolve(event.data.result); }
      if (event.data.type === "error") { close(); reject(new Error(event.data.message)); }
    };
    worker.postMessage({ file });
  });
}
