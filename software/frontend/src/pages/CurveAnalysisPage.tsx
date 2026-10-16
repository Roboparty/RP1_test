import { useRef, useState, type CSSProperties, type FormEvent } from "react";
import { useQueries, useQuery } from "@tanstack/react-query";
import { ArrowLeft, ChevronDown, History, Info, Plus, Search, X } from "lucide-react";
import { archivesApi, archiveTitle, archiveStation, type Archive, type ArchiveCatalogItem, type ArchiveCurves } from "../archiveApi";
import { EmptyState, ErrorState, LoadingState } from "../components/Workbench";
import { TimeSeriesChart, type CurveViewport, type PlotSeries, type ReferenceLine } from "../components/TimeSeriesChart";
import { ArchiveStatus } from "./ArchivesPage";
import "./curveAnalysis.css";

const metricNames: Record<string, string> = { pos_rad: "实际位置", position_rad: "实际位置", cmd_pos_rad: "目标位置", target_pos_rad: "目标位置", target_position_rad: "目标位置", pos_ref_rad: "目标位置", spd_rad_s: "速度", vel_rad_s: "速度", velocity_rad_s: "速度", torque_nm: "力矩", temp_c: "温度", temperature_c: "温度", current_a: "电流", bus_current_a: "母线电流", voltage_v: "电压", bus_voltage_v: "母线电压", error_id: "故障码", tracking_error_rad: "跟踪误差" };
function metricIdentity(item: ArchiveCatalogItem) { return `${item.metric}\u0000${item.unit}`; }
export function compatibleCatalog(catalog: ArchiveCatalogItem[], metric: string, unit: string, subjects: string[]) {
  return catalog.filter((item) => item.metric === metric && item.unit === unit && subjects.includes(item.subject));
}
function choices(values: unknown[]) {
  return values.flatMap((entry) => {
    if (typeof entry === "string" || typeof entry === "number") return [{ value: String(entry), label: String(entry) }];
    if (entry && typeof entry === "object") {
      const value = entry as Record<string, unknown>;
      const id = value.code ?? value.stage ?? value.cycle ?? value.cycle_no ?? value.id;
      if (id !== undefined) return [{ value: String(id), label: String(value.name ?? value.label ?? id) }];
    }
    return [];
  });
}
function startTime(item: Archive) {
  const date = item.facts.started_at ?? item.current_verification?.declared_started_at;
  const time = date ? new Date(String(date)).getTime() : NaN;
  return Number.isFinite(time) ? time : null;
}
export function configuredReferences(item: Archive, metric: string): ReferenceLine[] {
  const value = metric.includes("torque") ? item.acceptance.max_torque_abs_nm : metric.includes("tracking") ? item.acceptance.max_tracking_rms_rad : undefined;
  if (typeof value !== "number" || !Number.isFinite(value)) return [];
  if (metric.includes("tracking")) return []; // RMS is an aggregate criterion, not an instantaneous bound.
  return [{ id: "case-upper", name: "用例力矩上限", value }, { id: "case-lower", name: "用例力矩下限", value: -value }];
}
function targetColumns(catalog: ArchiveCatalogItem[], metric: string, unit: string, subjects: string[]) {
  if (!/^(pos_rad|position_rad|actual_position_rad)$/.test(metric)) return [];
  return catalog.filter((item) => item.unit === unit && subjects.includes(item.subject) && /^(cmd_pos_rad|target_pos_rad|target_position_rad|pos_ref_rad|target_rad)$/.test(item.metric));
}
type ComparedArchive = Archive & { comparison_id?: string; reference_stage?: string; reference_cycle?: string; reference_metric?: string; reference_unit?: string; reference_subjects?: string[] };
function recordKey(item: ComparedArchive) { return item.comparison_id ?? item.id; }
function recordLabel(item: ComparedArchive) { return `${archiveTitle(item)}${item.reference_stage ? ` · 阶段 ${item.reference_stage}` : ""}${item.reference_cycle ? ` · 循环 ${item.reference_cycle}` : ""}`; }

const SERIES_COLORS = ["#3274d9", "#149e98", "#9365c7", "#d08b24", "#b75579", "#548a47", "#6376ad", "#916d4c"];
const SERIES_DASHES = ["solid", "dashed", "dotted"] as const;

function rangeFor(record: Archive) {
  const raw = record.manifest?.time_range;
  const start = raw && Number.isFinite(raw.start) ? raw.start : 0;
  const end = raw && Number.isFinite(raw.end) ? raw.end : Number(record.facts.active_seconds) || 1;
  return { start, end: end > start ? end : start + 1 };
}

// These are visible sample excursions beyond the displayed instantaneous
// reference, not formal anomaly events or a replacement for an analysis report.
export function sampleLimitMarkers(points: PlotSeries["points"], limits: ReferenceLine[]) {
  if (!limits.length) return [];
  const low = Math.min(...limits.map((line) => line.value));
  const high = Math.max(...limits.map((line) => line.value));
  let lower: [number, number] | undefined;
  let upper: [number, number] | undefined;
  for (const [time, value] of points) {
    if (value === null || !Number.isFinite(value)) continue;
    if (value < low && (!lower || value < lower[1])) lower = [time, value];
    if (value > high && (!upper || value > upper[1])) upper = [time, value];
  }
  return [lower, upper].filter((point): point is [number, number] => !!point)
    .map(([time, value]) => ({ time, value, label: "采样越限" }));
}

function Explorer({ item, navigate }: { item: Archive; navigate: (view: "archive" | "curves", id: string) => void }) {
  const catalog = item.manifest?.catalog ?? [];
  const metrics = [...new Map(catalog.map((entry) => [metricIdentity(entry), entry])).values()];
  const [selection, setSelection] = useState(metrics[0] ? metricIdentity(metrics[0]) : "");
  const chosen = metrics.find((entry) => metricIdentity(entry) === selection);
  const subjects = [...new Set(catalog.filter((entry) => metricIdentity(entry) === selection).map((entry) => entry.subject))];
  const [selectedSubjects, setSelectedSubjects] = useState<string[]>(subjects.slice(0, 1));
  const [stage, setStage] = useState("");
  const [cycle, setCycle] = useState("");
  const [alignment, setAlignment] = useState<"elapsed" | "absolute">("elapsed");
  const [layout, setLayout] = useState<"overlay" | "facets">("overlay");
  const [viewport, setViewport] = useState<CurveViewport | null>(null);
  const [maxPoints, setMaxPoints] = useState(4000);
  const [showLimits, setShowLimits] = useState(true);
  const [showTargets, setShowTargets] = useState(false);
  const [showExcursions, setShowExcursions] = useState(true);
  const [showHistory, setShowHistory] = useState(true);
  const [references, setReferences] = useState<ReferenceLine[]>([]);
  const [referenceName, setReferenceName] = useState("");
  const [referenceValue, setReferenceValue] = useState("");
  const [compareDraft, setCompareDraft] = useState("");
  const [compareSearch, setCompareSearch] = useState("");
  const [compared, setCompared] = useState<ComparedArchive[]>([]);
  const [excluded, setExcluded] = useState<string[]>([]);
  const [compareSubjects, setCompareSubjects] = useState<Record<string, string[]>>({});
  const [selectionNote, setSelectionNote] = useState("");
  const searchInput = useRef<HTMLInputElement>(null);
  const stages = choices(item.manifest?.stages ?? []);
  const cycles = choices(item.manifest?.cycles ?? []);
  const compareQuery = useQuery({
    queryKey: ["archives", "compare", compareSearch],
    queryFn: ({ signal }) => archivesApi.list({ q: compareSearch, processing_state: "READY" }, signal),
    enabled: Boolean(compareSearch)
  });
  const allRecords: ComparedArchive[] = [item, ...compared];
  function recordSubjects(record: ComparedArchive) {
    if (recordKey(record) === item.id) return selectedSubjects;
    if (record.reference_subjects) return record.reference_subjects.slice(0, showTargets ? 16 : 32);
    const available = (record.manifest?.catalog ?? []).filter((entry) => chosen && entry.metric === chosen.metric && entry.unit === chosen.unit).map((entry) => entry.subject);
    return compareSubjects[recordKey(record)]?.filter((subject) => available.includes(subject)) ?? [...new Set(available)].slice(0, 1);
  }
  function recordCompatible(record: ComparedArchive) {
    if (!chosen) return false;
    if (record.reference_metric && (record.reference_metric !== chosen.metric || record.reference_unit !== chosen.unit)) return false;
    return compatibleCatalog(record.manifest?.catalog ?? [], chosen.metric, chosen.unit, recordSubjects(record)).length > 0;
  }
  const records = allRecords.filter((record) => !excluded.includes(recordKey(record)) && (!record.comparison_id || showHistory) && recordCompatible(record));
  const absoluteUnavailable = records.some((record) => startTime(record) === null);
  const absolute = alignment === "absolute" && !absoluteUnavailable;
  const ranges = (records.length ? records : [item]).map((record) => {
    const range = rangeFor(record);
    const offset = absolute ? startTime(record) ?? 0 : 0;
    return { start: offset + range.start * (absolute ? 1000 : 1), end: offset + range.end * (absolute ? 1000 : 1) };
  });
  const domain = { start: Math.min(...ranges.map((range) => range.start)), end: Math.max(...ranges.map((range) => range.end)) };
  const visible = viewport && viewport.start < domain.end && viewport.end > domain.start
    ? { start: Math.max(viewport.start, domain.start), end: Math.min(viewport.end, domain.end) } : domain;
  function curveQuery(record: ComparedArchive, overview: boolean) {
    const matching = chosen ? compatibleCatalog(record.manifest?.catalog ?? [], chosen.metric, chosen.unit, recordSubjects(record)) : [];
    const targets = chosen && showTargets ? targetColumns(record.manifest?.catalog ?? [], chosen.metric, chosen.unit, recordSubjects(record)) : [];
    const keys = [...matching, ...targets].map((entry) => entry.key).slice(0, 32);
    const offset = absolute ? startTime(record)! : 0;
    const full = rangeFor(record);
    const start = overview ? full.start : Math.max(full.start, (visible.start - offset) / (absolute ? 1000 : 1));
    const end = overview ? full.end : Math.min(full.end, (visible.end - offset) / (absolute ? 1000 : 1));
    const queryStage = record.reference_stage ?? stage;
    const queryCycle = record.reference_cycle ?? cycle;
    const pointLimit = overview ? 800 : maxPoints;
    return {
      queryKey: ["archives", overview ? "curve-overview" : "curves", recordKey(record), keys.join(","), start, end, pointLimit, queryStage, queryCycle],
      queryFn: ({ signal }: { signal: AbortSignal }) => archivesApi.curves(record.id, {
        metrics: keys.join(","), start, end, max_points: pointLimit,
        ...(queryStage ? { stage: queryStage } : {}), ...(queryCycle ? { cycle: queryCycle } : {})
      }, signal),
      enabled: record.processing_state === "READY" && keys.length > 0 && start <= end,
      staleTime: overview ? 60_000 : 15_000
    };
  }
  // Keep observer slots stable: query-core 5.87 can retain a removed trailing
  // observer when useQueries shrinks. Replacing it with a disabled slot releases
  // the previous query and aborts its request without cancelling other consumers.
  function querySlots(overview: boolean) {
    return Array.from({ length: 8 }, (_, index) => records[index] ? curveQuery(records[index], overview) : {
      queryKey: ["archives", overview ? "inactive-overview" : "inactive-curves", item.id, index],
      queryFn: async (): Promise<ArchiveCurves> => { throw new Error("Inactive curve slot"); },
      enabled: false
    });
  }
  const queries = useQueries({ queries: querySlots(false) }).slice(0, records.length);
  const overviewQueries = useQueries({ queries: querySlots(true) }).slice(0, records.length);
  const configured = chosen ? configuredReferences(item, chosen.metric) : [];
  function plotSeries(responses: { data?: ArchiveCurves; isError: boolean }[], overview: boolean): PlotSeries[] {
    return responses.flatMap((query, index) => {
      if (query.isError) return [];
      const record = records[index];
      const colorIndex = allRecords.findIndex((candidate) => recordKey(candidate) === recordKey(record));
      const transformTime = (time: number) => absolute ? startTime(record)! + time * 1000 : time;
      return query.data?.series.filter((curve) => chosen && curve.unit === chosen.unit
        && (curve.metric === chosen.metric || showTargets && targetColumns(record.manifest?.catalog ?? [], chosen.metric, chosen.unit, recordSubjects(record)).some((column) => column.key === curve.key)))
        .map((curve) => {
          const target = curve.metric !== chosen?.metric;
          const points = curve.points.map(([time, value]): [number, number | null] => [transformTime(time), value]);
          return {
            id: `${recordKey(record)}:${curve.key}`,
            name: `${recordLabel(record)} · ${curve.subject || "全局"}${target ? " · 目标" : ""}`,
            unit: curve.unit, reference: !!record.comparison_id || target,
            color: target ? "#8895a7" : SERIES_COLORS[colorIndex % SERIES_COLORS.length],
            dash: target ? "dashed" as const : SERIES_DASHES[(colorIndex + Math.max(0, recordSubjects(record).indexOf(curve.subject))) % SERIES_DASHES.length],
            points, gaps: (query.data?.gaps ?? []).map((gap) => ({ start: transformTime(gap.start), end: transformTime(gap.end) })),
            anomalies: !overview && !target && showExcursions && showLimits ? sampleLimitMarkers(points, configured) : []
          };
        }) ?? [];
    });
  }
  const series = plotSeries(queries, false);
  const overviewSeries = plotSeries(overviewQueries, true);
  const failures = queries.flatMap((query, index) => query.error ? [{ error: query.error, index }] : []);
  const loading = queries.some((query) => query.isFetching);
  const availableResponses = queries.filter((query) => !query.isError);
  const sampleCount = availableResponses.reduce((total, query) => total + (query.data?.series.reduce((sum, entry) => sum + entry.sample_count, 0) ?? 0), 0);
  const shownCount = availableResponses.reduce((total, query) => total + (query.data?.series.reduce((sum, entry) => sum + entry.returned_points, 0) ?? 0), 0);
  const downsampled = availableResponses.some((query) => query.data?.resolution === "minmax");
  const gapRecords = availableResponses.filter((query) => query.data && (query.data.gaps.length || query.data.series.some((curve) => curve.points.some((point) => point[1] === null)))).length;
  const resolution = loading && !shownCount ? "正在读取当前视窗…" : failures.length && !shownCount ? "当前视窗读取失败" : `${downsampled ? "峰谷概览" : "原始分辨率"} · ${shownCount.toLocaleString("zh-CN")} / ${sampleCount.toLocaleString("zh-CN")} 系列采样点${gapRecords ? ` · 缺失：${gapRecords} 次执行` : ""}`;
  const targetsAvailable = chosen ? records.some((record) => targetColumns(record.manifest?.catalog ?? [], chosen.metric, chosen.unit, recordSubjects(record)).length > 0) : false;
  const fixedRecords = compared.filter((record) => record.comparison_id);
  const incompatible = allRecords.filter((record) => !excluded.includes(recordKey(record)) && !recordCompatible(record));
  const label = chosen ? `${metricNames[chosen.metric] ?? chosen.metric} · ${chosen.unit || "无单位"}` : "选择参数";

  function switchMetric(value: string) {
    setSelection(value);
    setSelectedSubjects([...new Set(catalog.filter((entry) => metricIdentity(entry) === value).map((entry) => entry.subject))].slice(0, 1));
    setCompareSubjects({}); setReferences([]); setShowTargets(false); setViewport(null); setSelectionNote("");
  }
  function addRecord(record: Archive) {
    setCompared((current) => current.some((entry) => entry.id === record.id && !entry.comparison_id) || current.length >= 7 ? current : [...current, record]);
    setExcluded((current) => current.filter((key) => key !== record.id));
    setViewport(null);
  }
  function pinReference() {
    if (!chosen || !selectedSubjects.length) return;
    const key = `reference:${JSON.stringify([item.id, chosen.metric, chosen.unit, [...selectedSubjects].sort(), stage, cycle])}`;
    setCompared((current) => current.some((entry) => recordKey(entry) === key) || current.length >= 7 ? current : [...current, {
      ...item, comparison_id: key, reference_stage: stage, reference_cycle: cycle,
      reference_metric: chosen.metric, reference_unit: chosen.unit, reference_subjects: [...selectedSubjects]
    }]);
    setExcluded((current) => current.filter((entry) => entry !== key));
    setShowHistory(true); setSelectionNote("已固定当前参数、关节、阶段与循环；重复固定相同选择不会新增记录。");
  }
  function addReference(event: FormEvent) {
    event.preventDefault(); const value = Number(referenceValue);
    if (!referenceValue.trim() || !Number.isFinite(value) || !referenceName.trim()) return;
    const name = referenceName.trim();
    setReferences((current) => current.some((line) => line.name === name && line.value === value) ? current : [...current, { id: crypto.randomUUID(), name, value, temporary: true }]);
    setReferenceName(""); setReferenceValue("");
  }
  function toggleRecord(record: ComparedArchive) {
    const key = recordKey(record);
    setExcluded((current) => current.includes(key) ? current.filter((entry) => entry !== key) : [...current, key]);
    setViewport(null);
  }
  const referenceControls = <div className="curve-reference-toolbar">
    <label title={configured.length ? "当前归档的瞬时参考限值" : "当前参数未配置瞬时上下限"}><input type="checkbox" checked={showLimits && configured.length > 0} disabled={!configured.length} onChange={(event) => setShowLimits(event.target.checked)} />用例上下限</label>
    <label title={targetsAvailable ? "叠加采集文件中的目标轨迹" : "当前选择未提供同单位目标轨迹"}><input type="checkbox" disabled={!targetsAvailable} checked={showTargets && targetsAvailable} onChange={(event) => { setShowTargets(event.target.checked); if (event.target.checked) { setSelectionNote("目标轨迹同时展示时，每次执行最多显示 16 个关节，测量值与目标合计不超过 32 列。"); setSelectedSubjects((current) => current.slice(0, 16)); setCompareSubjects((current) => Object.fromEntries(Object.entries(current).map(([key, values]) => [key, values.slice(0, 16)]))); } }} />目标轨迹</label>
    <details className="curve-disclosure"><summary><History aria-hidden="true" />历史参考<ChevronDown aria-hidden="true" /></summary><div className="curve-popover">
      <button type="button" className="curve-button" disabled={compared.length >= 7 || !selectedSubjects.length} onClick={pinReference}>固定当前曲线为参考</button>
      <label><input type="checkbox" checked={showHistory} disabled={!fixedRecords.length} onChange={(event) => setShowHistory(event.target.checked)} />显示固定参考</label>
      <p>固定参数、关节、阶段与循环后，可切换当前筛选进行比较。不同参数或单位的固定参考暂不显示。</p>
    </div></details>
    <details className="curve-disclosure"><summary className="curve-button"><Plus aria-hidden="true" />临时参考线{references.length > 0 && ` · ${references.length}`}</summary><div className="curve-popover">
      <form onSubmit={addReference}><label>临时参考名称<input maxLength={80} value={referenceName} onChange={(event) => setReferenceName(event.target.value)} placeholder="例如：观察阈值" /></label><label>参考值（{chosen?.unit || "当前单位"}）<input type="number" step="any" value={referenceValue} onChange={(event) => setReferenceValue(event.target.value)} /></label><button className="curve-button primary" type="submit" disabled={!referenceName.trim() || !referenceValue.trim() || !Number.isFinite(Number(referenceValue))}>添加参考线</button></form>
      <ul className="curve-reference-items">{references.map((entry) => <li key={entry.id}><span>{entry.name} · {entry.value} {chosen?.unit}</span><button className="curve-row-action" type="button" aria-label={`移除参考线 ${entry.name}`} onClick={() => setReferences((current) => current.filter((line) => line.id !== entry.id))}><X aria-hidden="true" /></button></li>)}</ul><p>临时参考不改变正式判据。</p>
    </div></details>
    <label title="仅标出当前视窗采样中超出显示参考限值的最大/最小点，不代表完整异常事件或正式结论"><input type="checkbox" checked={showExcursions && showLimits && configured.length > 0} disabled={!showLimits || !configured.length} onChange={(event) => setShowExcursions(event.target.checked)} />采样越限</label>
  </div>;

  return <>
    <header className="curve-page-heading"><div className="curve-heading-copy"><h1 data-page-heading tabIndex={-1}>多执行对比</h1><p>{selectedSubjects.join(" / ").toUpperCase() || "未选关节"} · {label}</p></div><div className="curve-heading-actions"><button className="curve-button" type="button" aria-label="返回归档详情" onClick={() => navigate("archive", item.id)}>归档详情</button><button className="curve-button primary" type="button" onClick={() => { searchInput.current?.focus(); searchInput.current?.scrollIntoView({ block: "nearest", behavior: "smooth" }); }}><Plus aria-hidden="true" />添加执行</button></div></header>
    <div className="curve-workbench">
      <aside className="curve-execution-rail" aria-label="执行比较选择">
        <h2>已选执行 <span>{records.length} / 8</span></h2>
        <form className="curve-execution-search" onSubmit={(event) => { event.preventDefault(); setCompareSearch(compareDraft.trim()); }}><input ref={searchInput} aria-label="查找测试记录" value={compareDraft} onChange={(event) => setCompareDraft(event.target.value)} placeholder="搜索执行编号" /><button className="curve-button" type="submit" disabled={!compareDraft.trim()} aria-label="查找对比记录"><Search aria-hidden="true" /></button></form>
        {compareQuery.isLoading && <LoadingState label="正在查找对比记录" />}
        {compareQuery.error && <ErrorState error={compareQuery.error} retry={() => void compareQuery.refetch()} />}
        {compareQuery.data && <div className="curve-results" aria-label="对比搜索结果">{compareQuery.data.items.filter((entry) => entry.id !== item.id && !compared.some((added) => added.id === entry.id && !added.comparison_id)).map((entry) => {
          const compatible = !!chosen && (entry.manifest?.catalog ?? []).some((column) => column.metric === chosen.metric && column.unit === chosen.unit);
          return <button key={entry.id} type="button" disabled={!compatible || compared.length >= 7 || entry.processing_state !== "READY"} onClick={() => addRecord(entry)}><span><strong>{archiveTitle(entry)}</strong><small>{entry.test_name} · {compatible ? "添加叠加" : "缺少同参数、同单位的曲线"}</small></span><Plus aria-hidden="true" /></button>;
        })}{compareQuery.data.items.length === 0 && <p className="curve-reference-help">没有找到记录。</p>}<button type="button" className="curve-record-link" onClick={() => { setCompareSearch(""); setCompareDraft(""); }}>收起搜索结果</button></div>}
        <ul className="curve-execution-list">{allRecords.map((record, index) => {
          const key = recordKey(record);
          const compatible = recordCompatible(record);
          const selected = !excluded.includes(key) && (!record.comparison_id || showHistory) && compatible;
          const recordUnit = record.reference_unit ?? chosen?.unit;
          return <li key={key} className={`curve-execution-item${selected ? " selected" : ""}`}>
            <div className="curve-execution-row"><label><input aria-label={`参与对比 ${recordLabel(record)}`} type="checkbox" checked={selected} disabled={!compatible || !!record.comparison_id && !showHistory} onChange={() => toggleRecord(record)} /><span className="curve-series-dot" style={{ "--series-color": SERIES_COLORS[index % SERIES_COLORS.length] } as CSSProperties} /><strong>{archiveTitle(record)}</strong></label>{index === 0 ? <span className="curve-execution-badge">基准</span> : <><span className="curve-execution-badge">{record.comparison_id ? "固定" : record.origin === "HISTORY" ? "历史" : "对比"}</span><button className="curve-row-action" type="button" aria-label={`移除对比 ${recordLabel(record)}`} onClick={() => { setCompared((current) => current.filter((entry) => recordKey(entry) !== key)); setExcluded((current) => current.filter((entry) => entry !== key)); setViewport(null); }}><X aria-hidden="true" /></button></>}</div>
            <div className="curve-execution-meta"><span>{recordSubjects(record).join(" / ").toUpperCase() || "未选对象"} · {recordUnit || "无单位"}</span>{record.comparison_id && <p>阶段 {record.reference_stage || "全部"} · 循环 {record.reference_cycle || "全部"}</p>}{!compatible && <p>当前选择不兼容</p>}
              {index > 0 && !record.comparison_id && <details><summary>对比关节</summary><div className="curve-execution-subjects">{[...new Set((record.manifest?.catalog ?? []).filter((column) => chosen && column.metric === chosen.metric && column.unit === chosen.unit).map((column) => column.subject))].map((subject) => <label key={subject}><input type="checkbox" checked={recordSubjects(record).includes(subject)} disabled={!recordSubjects(record).includes(subject) && recordSubjects(record).length >= (showTargets ? 16 : 32)} onChange={() => setCompareSubjects((current) => ({ ...current, [key]: recordSubjects(record).includes(subject) ? recordSubjects(record).filter((value) => value !== subject) : [...recordSubjects(record), subject] }))} />{subject || "全局"}</label>)}</div><button className="curve-record-link" type="button" onClick={() => navigate("curves", record.id)}>打开此执行曲线</button></details>}
            </div>
          </li>;
        })}</ul>
        <section className="curve-comparison-options"><h3>对比方式</h3><div className="curve-mode-toggle"><button type="button" aria-pressed={layout === "overlay"} onClick={() => setLayout("overlay")}>叠加</button><button type="button" aria-pressed={layout === "facets"} onClick={() => setLayout("facets")}>分面</button></div><label>时间对齐<select value={alignment} onChange={(event) => { setAlignment(event.target.value as "elapsed" | "absolute"); setViewport(null); }}><option value="elapsed">测试开始</option><option value="absolute" disabled={absoluteUnavailable}>绝对时间</option></select></label><label>视窗点数上限<select value={maxPoints} onChange={(event) => setMaxPoints(Number(event.target.value))}><option value={1000}>1,000</option><option value={4000}>4,000</option><option value={10000}>10,000</option><option value={20000}>20,000</option></select></label></section>
        <p className="curve-contract-note"><Info aria-hidden="true" />仅同参数、同单位可比较。取消勾选只移出当前对比。</p>
        {absoluteUnavailable && <p className="curve-reference-help">选中执行缺少开始时间，绝对时间对齐不可用。</p>}
      </aside>
      <div className="curve-main-stage">
        <div className="curve-filter-strip"><label>参数<select value={selection} disabled={!metrics.length} onChange={(event) => switchMetric(event.target.value)}>{metrics.map((entry) => <option key={metricIdentity(entry)} value={metricIdentity(entry)}>{metricNames[entry.metric] ?? entry.metric} · {entry.unit || "无单位"}</option>)}</select></label>
          <details className="curve-disclosure curve-joint-picker"><summary><span>关节 {selectedSubjects.join(" / ").toUpperCase() || "未选择"}</span><ChevronDown aria-hidden="true" /></summary><div className="curve-popover">{subjects.map((subject) => <label key={subject}><input type="checkbox" checked={selectedSubjects.includes(subject)} disabled={!selectedSubjects.includes(subject) && selectedSubjects.length >= (showTargets ? 16 : 32)} onChange={() => setSelectedSubjects((current) => current.includes(subject) ? current.filter((entry) => entry !== subject) : [...current, subject])} />{subject || "全局"}</label>)}</div></details>
          <label title={!stages.length ? "当前归档未提供阶段索引" : undefined}>阶段<select value={stage} disabled={!stages.length} onChange={(event) => { setStage(event.target.value); setViewport(null); }}><option value="">{stages.length ? "全部" : "未记录"}</option>{stages.map((entry) => <option key={entry.value} value={entry.value}>{entry.label}</option>)}</select></label>
          <label title={!cycles.length ? "当前归档未提供循环索引" : undefined}>循环<select value={cycle} disabled={!cycles.length} onChange={(event) => { setCycle(event.target.value); setViewport(null); }}><option value="">{cycles.length ? "全部" : "未记录"}</option>{cycles.map((entry) => <option key={entry.value} value={entry.value}>{entry.label}</option>)}</select></label>
        </div>
        {(!stages.length || !cycles.length) && <p className="curve-filter-context">当前归档未提供{!stages.length && "阶段"}{!stages.length && !cycles.length && "、"}{!cycles.length && "循环"}索引，对应筛选不可用。</p>}
        {selectionNote && <p role="status" className="curve-selection-note">{selectionNote}</p>}
        {alignment === "absolute" && absoluteUnavailable && <p role="status" className="curve-selection-note">存在无开始时间的执行，当前按测试开始对齐。</p>}
        {catalog.length === 0 ? <div className="curve-state-card"><EmptyState title="归档中没有可绘制参数" description="解析结果没有数值采样列，请检查归档详情。" /></div> : !selectedSubjects.length && records.length === 0 ? <div className="curve-state-card"><EmptyState title="请选择采集对象" description="展开关节选项，勾选需要查看的采集对象。" /></div> : records.length === 0 ? <div className="curve-state-card"><EmptyState title="请选择参与对比的执行" description="勾选左侧执行后加载曲线。" /></div> : <TimeSeriesChart key={`${selection}:${alignment}`} title={`${selectedSubjects.join(" / ").toUpperCase() || "多关节"} · ${label} 对比`} series={series} overviewSeries={overviewSeries} overviewLoading={overviewQueries.some((query) => query.isFetching)} domain={domain} viewport={visible} references={[...(showLimits ? configured : []), ...references]} referenceControls={referenceControls} onViewport={setViewport} loading={loading} resolution={resolution} absolute={absolute} layout={layout} xLabel={absolute ? "采集时间" : "相对测试开始 / s"} />}
        {!loading && series.length > 0 && series.every((curve) => !curve.points.length || curve.points.every((point) => point[1] === null)) && !failures.length && <div className="curve-state-card" role="status"><h2>当前范围没有有效采样</h2><p>可重置时间范围或调整阶段、循环条件；缺失数据不会连线。</p><button className="curve-button" type="button" onClick={() => setViewport(null)}>重置时间范围</button></div>}
        {!loading && !series.length && records.length > 0 && !failures.length && <div className="curve-state-card" role="status"><h2>当前筛选没有采样点</h2><p>请重置时间范围或调整筛选。</p><button className="curve-button" type="button" onClick={() => setViewport(null)}>重置时间范围</button></div>}
        {failures.map(({ error, index }) => <div key={recordKey(records[index])}><p className="curve-error">{archiveTitle(records[index])} 当前视窗读取失败</p><ErrorState error={error} retry={() => void queries[index].refetch()} /></div>)}
        {overviewQueries.some((query) => query.isError) && <div className="curve-small-status" role="status"><span>全程概览读取失败，可单独重试概览。</span><button className="curve-record-link" type="button" onClick={() => void Promise.all(overviewQueries.filter((query) => query.isError).map((query) => query.refetch()))}>重试概览</button></div>}
        {incompatible.length > 0 && <p className="curve-filter-context">{incompatible.map((record) => archiveTitle(record)).join("、")} 未加入当前图表：参数、单位或所选关节不兼容。</p>}
        <div className="curve-small-status"><span>当前视窗预算：{maxPoints.toLocaleString("zh-CN")} 点/执行 · 全程概览预算：800 点/执行</span><span>放大后重查当前视窗细节</span></div>
        <details className="curve-detail-summary"><summary>执行信息与数据说明</summary><div><p>{archiveTitle(item)} · {item.test_name} · {item.actual_object} · {archiveStation(item)}</p><ArchiveStatus item={item} /><p>采样越限只标记视窗返回点中的最大/最小越限值；采样标记不代表完整异常事件或正式结论；请结合归档中的分析与核实信息判断。概览及缺失展示取决于服务返回数据。</p></div></details>
      </div>
    </div>
  </>;
}

export default function CurveAnalysisPage({ id, navigate }: { id: string; navigate: (view: "archive" | "curves", id: string) => void }) {
  const query = useQuery({ queryKey: ["archives", "detail", id], queryFn: ({ signal }) => archivesApi.detail(id, signal) });
  if (query.isLoading) return <LoadingState label="正在加载曲线目录" />;
  if (query.error || !query.data) return <ErrorState error={query.error} retry={() => void query.refetch()} />;
  const item = query.data;
  return <div className="curve-analysis-page"><nav className="curve-breadcrumb" aria-label="曲线页导航"><button type="button" onClick={() => navigate("archive", id)}><ArrowLeft aria-hidden="true" />数据归档</button><span>/</span><span>曲线分析</span></nav>{item.processing_state !== "READY" ? <><header className="curve-page-heading"><h1>{archiveTitle(item)}</h1><button className="curve-button" type="button" onClick={() => navigate("archive", id)}>返回归档详情</button></header><div className="curve-state-card"><EmptyState title="归档曲线尚未就绪" description={item.processing_error || "文件上传与解析完成后可在此查看详细曲线。"} /></div></> : <Explorer key={id} item={item} navigate={navigate} />}</div>;
}
