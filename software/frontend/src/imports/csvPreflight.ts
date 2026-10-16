import { Sha256 } from "./sha256";
export type CsvColumn = { name: string; unit: string; count: number; missing: number; min: number | null; max: number | null };
export type CsvPreflight = { sha256: string; rows: number; columns: CsvColumn[]; timeKey: string; start: number | null; end: number | null; maxGap: number; invalidTime: number; nonMonotonic: number; warnings: string[]; errors: string[] };
const units: Record<string,string> = { tracking_error_rad: "rad", cmd_pos_rad: "rad", pos_rad: "rad", spd_rad_s: "rad/s", vel_rad_s: "rad/s", torque_nm: "N·m", temp_c: "°C", error_id: "无量纲", bus_voltage_v: "V", bus_current_a: "A", current_a: "A", voltage_v: "V" };
const finite = (value: string) => {
  // Match decimal floats accepted by the converter; JavaScript also accepts hex
  // and binary literals, which Python float() correctly treats as missing.
  const decimal = value.trim().replace(/(\d)_(?=\d)/g, "$1");
  if (!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i.test(decimal)) return null;
  const n = Number(decimal); return Number.isFinite(n) && Math.abs(n) <= 1e100 ? n : null;
};
export class CsvScanner {
  private cell = "";
  private row: string[] = [];
  private quoted = false;
  private closedQuote = false;
  private skipLf = false;
  private lineLength = 0;
  private previous: number | null = null;
  private header: string[] | null = null;
  private timeIndex = -1;
  private excluded = new Set<number>();
  result: CsvPreflight = { sha256: "", rows: 0, columns: [], timeKey: "", start: null, end: null, maxGap: 0, invalidTime: 0, nonMonotonic: 0, warnings: [], errors: [] };
  feed(text: string) {
    for (const char of text) {
      if (this.skipLf) { this.skipLf = false; if (char === "\n") continue; }
      if (++this.lineLength > 2 * 1024 * 1024) throw new Error("CSV 单行超过 2 MiB 限制。");
      if (this.quoted) {
        if (char === '"') { this.quoted = false; this.closedQuote = true; } else this.cell += char;
      } else if (this.closedQuote && char === '"') { this.cell += '"'; this.quoted = true; this.closedQuote = false;
      } else if (char === ",") { this.row.push(this.cell); this.cell = ""; this.closedQuote = false;
      } else if (char === "\n" || char === "\r") { this.finishRow(); this.skipLf = char === "\r";
      } else if (char === '"' && !this.cell && !this.closedQuote) { this.quoted = true;
      } else { if (this.closedQuote) throw new Error("CSV 引号后存在非法字符。"); this.cell += char; }
    }
  }
  private finishRow() {
    const values = [...this.row, this.cell]; this.row = []; this.cell = ""; this.closedQuote = false; this.lineLength = 0;
    if (!this.header) {
      values[0] = values[0].replace(/^\uFEFF/, ""); this.header = values;
      if (values.length < 2 || values.length > 512 || new Set(values).size !== values.length) throw new Error("CSV 必须包含 2–512 个不重复的表头。");
      if (values.some((name) => !name || name.length > 120 || name.startsWith("_") || [...name].some((c) => c.charCodeAt(0) < 32))) throw new Error("CSV 表头含非法或保留字段。");
      this.result.timeKey = ["time_s", "elapsed_s", "elapsed_seconds"].find((name) => values.includes(name)) ?? "";
      if (!this.result.timeKey) throw new Error("缺少 time_s、elapsed_s 或 elapsed_seconds 秒时间列。");
      this.timeIndex = values.indexOf(this.result.timeKey);
      this.excluded = new Set(values.map((name, i) => [this.result.timeKey, "sample", "stage", "stage_code", "cycle", "cycle_no", "cycle_index"].includes(name) ? i : -1));
      this.result.columns = values.map((name) => ({ name, unit: name === this.result.timeKey ? "s" : Object.entries(units).find(([suffix]) => name.endsWith(`_${suffix}`))?.[1] ?? "未声明", count: 0, missing: 0, min: null, max: null }));
      if (!values.some((name) => ["stage", "stage_code"].includes(name))) this.result.warnings.push("文件未记录阶段，阶段筛选不可用。");
      if (!values.some((name) => ["cycle", "cycle_no", "cycle_index"].includes(name))) this.result.warnings.push("文件未记录循环；sample 是采样索引，不作为循环数。");
      return;
    }
    this.result.rows++;
    if (values.length !== this.header.length) throw new Error(`CSV 第 ${this.result.rows + 1} 行有 ${values.length} 列，应为 ${this.header.length} 列。`);
    const time = finite(values[this.timeIndex]);
    if (time === null || time < 0 || time > 315360000) this.result.invalidTime++;
    else {
      this.result.start = this.result.start === null ? time : Math.min(this.result.start, time);
      this.result.end = this.result.end === null ? time : Math.max(this.result.end, time);
      if (this.previous !== null) { if (time <= this.previous) this.result.nonMonotonic++; this.result.maxGap = Math.max(this.result.maxGap, time - this.previous); }
      this.previous = time;
    }
    values.forEach((raw, index) => {
      if (this.excluded.has(index) && index !== this.timeIndex) return;
      const column = this.result.columns[index], value = finite(raw);
      if (value === null) column.missing++;
      else { column.count++; column.min = column.min === null ? value : Math.min(value, column.min); column.max = column.max === null ? value : Math.max(value, column.max); }
    });
  }
  finish() {
    if (this.quoted) throw new Error("CSV 存在未闭合引号，文件可能不完整。");
    if (this.cell || this.row.length || this.closedQuote) this.finishRow();
    if (!this.result.rows || this.result.start === null) this.result.errors.push("文件没有有效的采样时间与数据行。");
    if (this.result.invalidTime) this.result.errors.push(`${this.result.invalidTime} 行时间无效。请核对原始文件。`);
    if (this.result.nonMonotonic) this.result.warnings.push(`${this.result.nonMonotonic} 行时间未严格递增，服务端将记录数据质量问题。`);
    const missing = this.result.columns.filter((column, i) => !this.excluded.has(i) && column.count === 0);
    if (missing.length) this.result.warnings.push(`${missing.length} 个参数全部缺失（空值 / NaN / 非有限值），不能显示测量曲线。`);
    this.result.warnings.push("相对采样时间不代表机器运行时长；实际测试人、绝对日期、机器终态不会从文件名推断。");
    return this.result;
  }
}
export async function inspectCsv(file: Blob, progress: (percent: number) => void = () => {}) {
  const hash = new Sha256(), scanner = new CsvScanner(), decoder = new TextDecoder("utf-8", { fatal: true });
  const size = 1024 * 1024;
  for (let offset = 0; offset < file.size; offset += size) {
    const bytes = new Uint8Array(await file.slice(offset, offset + size).arrayBuffer());
    hash.update(bytes); scanner.feed(decoder.decode(bytes, { stream: true }));
    progress(Math.min(100, Math.round((offset + bytes.length) / file.size * 100)));
  }
  scanner.feed(decoder.decode());
  const result = scanner.finish(); result.sha256 = hash.digest(); return result;
}
