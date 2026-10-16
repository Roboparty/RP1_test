import { inspectCsv } from "./csvPreflight";
self.onmessage = async (event: MessageEvent<{ file: File }>) => {
  try {
    const result = await inspectCsv(event.data.file, (percent) => self.postMessage({ type: "progress", percent }));
    self.postMessage({ type: "done", result });
  } catch (error) { self.postMessage({ type: "error", message: error instanceof Error ? error.message : "文件预检失败" }); }
};
