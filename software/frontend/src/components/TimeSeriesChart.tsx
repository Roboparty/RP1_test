import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { init, use as registerEcharts, type EChartsType } from "echarts/core";
import type { EChartsOption } from "echarts";
import { LineChart, type LineSeriesOption } from "echarts/charts";
import { AxisPointerComponent, BrushComponent, DataZoomComponent, GridComponent, MarkAreaComponent, MarkLineComponent, MarkPointComponent, TitleComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { ChevronDown, Move, RotateCcw, Scan } from "lucide-react";
import "./timeSeriesChart.css";

registerEcharts([LineChart, AxisPointerComponent, BrushComponent, DataZoomComponent, GridComponent, MarkAreaComponent, MarkLineComponent, MarkPointComponent, TitleComponent, TooltipComponent, CanvasRenderer]);
export type CurveViewport = { start: number; end: number };
export type PlotSeries = {
  id: string; name: string; unit: string; reference?: boolean; points: [number, number | null][];
  color?: string; dash?: "solid" | "dashed" | "dotted";
  gaps?: { start: number; end: number }[];
  anomalies?: { time: number; value: number; label: string }[];
};
export type ReferenceLine = { id: string; name: string; value: number; temporary?: boolean };
const COLORS = ["#237ce5", "#16a6a1", "#8970d0", "#c88218", "#b55c8b", "#d26a48", "#529456", "#697f9d"];
const EMPTY_SERIES: PlotSeries[] = [];
const sameViewport = (a: CurveViewport, b: CurveViewport) => Math.abs(a.start - b.start) < 1e-6 && Math.abs(a.end - b.end) < 1e-6;
const numberText = (value: number) => value.toLocaleString("zh-CN", { maximumFractionDigits: 4 });

export function validViewport(start: number, end: number): start is number {
  return Number.isFinite(start) && Number.isFinite(end) && start < end;
}
export function clampViewport(value: CurveViewport, domain: CurveViewport): CurveViewport | null {
  const start = Math.max(domain.start, value.start), end = Math.min(domain.end, value.end);
  return validViewport(start, end) ? { start, end } : null;
}
function normalizedGaps(gaps: PlotSeries["gaps"] = []) {
  const result: { start: number; end: number }[] = [];
  for (const gap of gaps.filter((item) => validViewport(item.start, item.end)).map((item) => ({ ...item })).sort((a, b) => a.start - b.start)) {
    const previous = result[result.length - 1];
    if (previous && gap.start < previous.end) previous.end = Math.max(previous.end, gap.end);
    else result.push(gap);
  }
  return result;
}
// Each explicit gap gets its own null separator, even when several gaps fall in
// one downsample bucket. Preserve null samples as supplied; never infer gaps
// from long intervals, and never mutate the caller's arrays.
export function breakCurveAtGaps(points: PlotSeries["points"], gaps: PlotSeries["gaps"] = []): PlotSeries["points"] {
  const ordered = points.filter(([time]) => Number.isFinite(time)).map(([time, value]): [number, number | null] => [time, value !== null && Number.isFinite(value) ? value : null]).sort((a, b) => a[0] - b[0]);
  if (!ordered.length) return ordered;
  const intervals = normalizedGaps(gaps);
  let gapIndex = 0;
  const broken = ordered.map(([time, value]): [number, number | null] => {
    while (gapIndex < intervals.length && intervals[gapIndex].end <= time) gapIndex++;
    const gap = intervals[gapIndex];
    return [time, gap && time > gap.start && time < gap.end ? null : value];
  });
  const first = ordered[0][0], last = ordered[ordered.length - 1][0];
  for (const gap of intervals) {
    const start = Math.max(first, gap.start), end = Math.min(last, gap.end);
    if (start < end) broken.push([start + (end - start) / 2, null]);
  }
  return broken.sort((a, b) => a[0] - b[0]);
}

type Props = {
  series: PlotSeries[]; domain: CurveViewport; viewport: CurveViewport; references: ReferenceLine[];
  onViewport: (next: CurveViewport) => void; loading: boolean; resolution: string; xLabel?: string; absolute?: boolean;
  title?: string; referenceControls?: ReactNode; layout?: "overlay" | "facets";
  overviewSeries?: PlotSeries[]; overviewLoading?: boolean;
};
type ZoomItem = { dataZoomId?: string; dataZoomIndex?: number; start?: number; end?: number; startValue?: number; endValue?: number };

export function TimeSeriesChart({ series, domain, viewport, references, onViewport, loading, resolution, xLabel = "相对测试开始 / s", absolute = false, title = "曲线对比", referenceControls, layout = "overlay", overviewSeries, overviewLoading = false }: Props) {
  const host = useRef<HTMLDivElement>(null), overviewHost = useRef<HTMLDivElement>(null);
  const chart = useRef<EChartsType | null>(null), overviewChart = useRef<EChartsType | null>(null);
  const callback = useRef(onViewport), bounds = useRef(domain), currentViewport = useRef(viewport);
  const debounce = useRef<ReturnType<typeof setTimeout> | null>(null), pending = useRef<CurveViewport | null>(null);
  const yZoom = useRef({ start: 0, end: 100 });
  const rangeControls = useRef<HTMLDivElement>(null);
  const [hidden, setHidden] = useState<string[]>([]);
  const [mode, setMode] = useState<"pan" | "box">("pan"), [linked, setLinked] = useState(true);
  const [rangeOpen, setRangeOpen] = useState<"x" | "y" | null>(null);
  const [xStart, setXStart] = useState(String(viewport.start)), [xEnd, setXEnd] = useState(String(viewport.end));
  const [yMin, setYMin] = useState(""), [yMax, setYMax] = useState("");
  const [yRange, setYRange] = useState<{ min?: number; max?: number }>({});
  const [rangeError, setRangeError] = useState("");
  const unitMismatch = new Set(series.map((item) => item.unit)).size > 1;
  const visibleSeries = useMemo(() => unitMismatch ? EMPTY_SERIES : series.filter((item) => !hidden.includes(item.id)), [series, hidden, unitMismatch]);
  const visibleOverview = useMemo(() => unitMismatch ? EMPTY_SERIES : (overviewSeries ?? series).filter((item) => !hidden.includes(item.id) && (!series.length || item.unit === series[0].unit)), [series, overviewSeries, hidden, unitMismatch]);
  const rows = layout === "facets" ? Math.max(1, visibleSeries.length) : 1;
  const chartHeight = layout === "facets" ? Math.max(330, rows * 190 + 40) : 440;

  function cancelPending() {
    if (debounce.current !== null) clearTimeout(debounce.current);
    debounce.current = null; pending.current = null;
  }
  function commitViewport(next: CurveViewport) {
    cancelPending(); currentViewport.current = next;
    callback.current(next);
  }
  useEffect(() => { callback.current = onViewport; }, [onViewport]);
  useEffect(() => {
    cancelPending(); bounds.current = { start: domain.start, end: domain.end }; currentViewport.current = { start: viewport.start, end: viewport.end };
    setXStart(String(viewport.start)); setXEnd(String(viewport.end));
  }, [domain.start, domain.end, viewport.start, viewport.end]);
  useEffect(() => {
    if (!rangeOpen) return;
    const close = (event: PointerEvent) => { if (!rangeControls.current?.contains(event.target as Node)) setRangeOpen(null); };
    document.addEventListener("pointerdown", close);
    return () => document.removeEventListener("pointerdown", close);
  }, [rangeOpen]);
  useEffect(() => {
    if (!host.current || !overviewHost.current) return;
    const instance = init(host.current, undefined, { renderer: "canvas" });
    const overview = init(overviewHost.current, undefined, { renderer: "canvas" });
    chart.current = instance; overviewChart.current = overview;
    const resize = new ResizeObserver(() => { instance.resize(); overview.resize(); });
    resize.observe(host.current); resize.observe(overviewHost.current);
    function queueViewport(value: CurveViewport) {
      const next = clampViewport(value, bounds.current);
      if (!next) return;
      if (sameViewport(next, currentViewport.current)) { cancelPending(); return; }
      if (pending.current && sameViewport(next, pending.current)) return;
      cancelPending(); pending.current = next;
      debounce.current = setTimeout(() => {
        debounce.current = null; pending.current = null;
        if (sameViewport(next, currentViewport.current)) return;
        currentViewport.current = next; callback.current(next);
      }, 220);
    }
    const zoomHandler = (owner: EChartsType) => (event: unknown) => {
      const payload = event as ZoomItem & { batch?: ZoomItem[] };
      for (const item of payload.batch ?? [payload]) {
        const options = owner.getOption().dataZoom as { id?: string }[] | undefined;
        const id = item.dataZoomId ?? (item.dataZoomIndex !== undefined ? options?.[item.dataZoomIndex]?.id : undefined);
        if (id?.startsWith("y-")) {
          if (item.start !== undefined) yZoom.current.start = item.start;
          if (item.end !== undefined) yZoom.current.end = item.end;
          continue;
        }
        if (item.start === undefined && item.startValue === undefined) continue;
        const full = bounds.current;
        queueViewport({ start: item.startValue ?? full.start + (item.start ?? 0) / 100 * (full.end - full.start), end: item.endValue ?? full.start + (item.end ?? 100) / 100 * (full.end - full.start) });
        break;
      }
    };
    instance.on("datazoom", zoomHandler(instance)); overview.on("datazoom", zoomHandler(overview));
    instance.on("brushEnd", (event: unknown) => {
      const area = (event as { areas?: { coordRange?: number[] }[] }).areas?.[0];
      if (area?.coordRange?.length === 2) queueViewport({ start: area.coordRange[0], end: area.coordRange[1] });
      instance.dispatchAction({ type: "brush", areas: [] });
    });
    return () => { cancelPending(); resize.disconnect(); instance.dispose(); overview.dispose(); chart.current = null; overviewChart.current = null; };
  }, []);

  useEffect(() => {
    if (!chart.current || loading && !series.length) return;
    const axes = Array.from({ length: rows }, (_, index) => index);
    const rowHeight = (chartHeight - 40) / rows;
    const refs = references.filter((item) => Number.isFinite(item.value));
    const colorFor = (item: PlotSeries) => item.color ?? COLORS[Math.max(0, series.findIndex((entry) => entry.id === item.id)) % COLORS.length];
    const option: EChartsOption = {
      animation: false, backgroundColor: "#fff", textStyle: { color: "#596b82", fontFamily: "Inter, system-ui, sans-serif", fontSize: 12 },
      title: layout === "facets" ? visibleSeries.map((item, index) => ({ text: item.name, left: 58, top: index * rowHeight + 6, textStyle: { color: colorFor(item), fontSize: 12, fontWeight: 600 } })) : [],
      grid: axes.map((index) => ({ left: 58, right: 48, top: layout === "facets" ? index * rowHeight + 34 : 24, ...(layout === "facets" ? { height: rowHeight - 64 } : { bottom: 46 }) })),
      tooltip: { trigger: "axis", confine: true, renderMode: "richText", hideDelay: 0, axisPointer: { type: linked ? "cross" : "line", snap: false }, valueFormatter: (value) => typeof value === "number" ? numberText(value) : "缺失" },
      axisPointer: { link: linked ? [{ xAxisIndex: axes }] : [] },
      brush: { xAxisIndex: axes, brushType: "lineX", brushMode: "single", transformable: false, removeOnClick: true, toolbox: [], inBrush: { colorAlpha: 1 }, outOfBrush: { colorAlpha: 1 }, brushStyle: { color: "rgba(35,124,229,.1)", borderColor: "#237ce5", borderWidth: 1 } },
      xAxis: axes.map((index) => ({ type: absolute ? "time" : "value", gridIndex: index, min: domain.start, max: domain.end, name: index === rows - 1 ? xLabel : undefined, nameLocation: "middle", nameGap: 29, axisLine: { lineStyle: { color: "#d6e0eb" } }, axisTick: { show: false }, axisLabel: { color: "#596d86", fontSize: 12, show: index === rows - 1, formatter: absolute ? undefined : numberText }, splitLine: { lineStyle: { color: "#edf1f6", width: 1 } } })),
      yAxis: axes.map((index) => ({ type: "value", gridIndex: index, name: series[0]?.unit || "数值", scale: true,
        min: yRange.min ?? (refs.length ? (extent: { min: number }) => Math.min(Number.isFinite(extent.min) ? extent.min : refs[0].value, ...refs.map((item) => item.value)) : undefined),
        max: yRange.max ?? (refs.length ? (extent: { max: number }) => Math.max(Number.isFinite(extent.max) ? extent.max : refs[0].value, ...refs.map((item) => item.value)) : undefined),
        nameTextStyle: { color: "#596d86", align: "right", fontSize: 12 }, axisLabel: { color: "#596d86", fontSize: 12 }, axisTick: { show: false }, axisLine: { show: false }, splitLine: { lineStyle: { color: "#e8edf4", width: 1 } } })),
      dataZoom: [
        { id: "x-inside", type: "inside", xAxisIndex: axes, filterMode: "none", startValue: viewport.start, endValue: viewport.end, zoomOnMouseWheel: "ctrl", moveOnMouseMove: mode === "pan", moveOnMouseWheel: false },
        { id: "x-slider", type: "slider", show: false, xAxisIndex: axes, filterMode: "none", startValue: viewport.start, endValue: viewport.end, showDataShadow: false },
        { id: "y-inside", type: "inside", yAxisIndex: axes, filterMode: "none", start: yZoom.current.start, end: yZoom.current.end, zoomOnMouseWheel: "shift", moveOnMouseMove: false, moveOnMouseWheel: false },
        { id: "y-slider", type: "slider", yAxisIndex: axes, filterMode: "none", start: yZoom.current.start, end: yZoom.current.end, right: 9, width: 12, top: 30, bottom: 48, showDataShadow: false, borderColor: "#dce5ef", backgroundColor: "#f6f8fb", fillerColor: "rgba(35,124,229,.08)", handleStyle: { color: "#fff", borderColor: "#a9bed5" }, brushSelect: false }
      ],
      series: series.filter(() => !unitMismatch).map((item): LineSeriesOption => {
        const visibleIndex = visibleSeries.findIndex((entry) => entry.id === item.id), isHidden = visibleIndex < 0;
        const index = layout === "facets" ? Math.max(0, visibleIndex) : 0;
        const gaps = normalizedGaps(item.gaps), color = colorFor(item);
        return {
          id: item.id, name: item.name, type: "line", xAxisIndex: index, yAxisIndex: index, showSymbol: false, connectNulls: false, smooth: false,
          data: isHidden ? [] : breakCurveAtGaps(item.points, gaps), lineStyle: { width: item.reference ? 1.5 : 1.8, type: item.dash ?? (item.reference ? "dashed" : "solid"), color }, itemStyle: { color }, emphasis: { focus: "series" },
          markLine: !isHidden && (layout === "facets" || visibleIndex === 0) ? { silent: true, symbol: "none", label: { position: "insideEndTop", formatter: "{b}", fontSize: 11 }, lineStyle: { type: "dashed", color: "#cc7278", width: 1 }, data: refs.map((reference) => ({ name: reference.name, yAxis: reference.value, lineStyle: { color: reference.temporary ? "#b18b43" : "#cc7278" } })) } : undefined,
          markArea: !isHidden && gaps.length ? { silent: true, itemStyle: { color: "rgba(150,164,183,.075)" }, label: { show: true, position: "insideTop", color: "#65758b", fontSize: 11 }, data: gaps.map((gap) => [{ name: "缺失段", xAxis: gap.start }, { xAxis: gap.end }]) } : undefined,
          markPoint: !isHidden && item.anomalies?.length ? { symbol: "triangle", symbolRotate: 180, symbolSize: 9, itemStyle: { color: "#d95c67" }, label: { show: true, formatter: "{b}", position: "top", distance: 8, color: "#b54b58", fontSize: 11, backgroundColor: "rgba(255,255,255,.9)", padding: [2, 3], borderRadius: 3 }, tooltip: { trigger: "item", formatter: "{b}: {c}" }, data: item.anomalies.filter((point) => Number.isFinite(point.time) && Number.isFinite(point.value) && point.time >= viewport.start && point.time <= viewport.end && !gaps.some((gap) => point.time > gap.start && point.time < gap.end)).map((point) => ({ name: point.label, coord: [point.time, point.value], value: point.value })) } : undefined
        };
      })
    };
    // Clear the cached hovered series indices before async responses replace
    // series. The next pointer event recomputes the tooltip from the new model.
    chart.current.dispatchAction({ type: "hideTip" });
    chart.current.setOption(option, { replaceMerge: ["series", "grid", "xAxis", "yAxis", "dataZoom", "title"], lazyUpdate: false });
    chart.current.dispatchAction({ type: "takeGlobalCursor", key: "brush", brushOption: { brushType: mode === "box" ? "lineX" : false, brushMode: "single" } });
    chart.current.resize();
  }, [series, visibleSeries, domain, viewport, references, yRange, linked, mode, layout, rows, chartHeight, xLabel, absolute, loading, unitMismatch]);

  useEffect(() => {
    if (!overviewChart.current) return;
    // The slider controls a separate, empty axis. Thus the overview keeps the
    // complete domain while its handles select the main chart's time window.
    const option: EChartsOption = {
      animation: false, grid: { left: 58, right: 48, top: 8, bottom: 25 },
      xAxis: [{ type: absolute ? "time" : "value", min: domain.start, max: domain.end, axisTick: { show: false }, axisLine: { lineStyle: { color: "#dce5ef" } }, axisLabel: { fontSize: 11, color: "#66788f", formatter: absolute ? undefined : numberText }, splitLine: { show: false } }, { type: "value", min: domain.start, max: domain.end, show: false }],
      yAxis: { type: "value", scale: true, show: false },
      dataZoom: [{ id: "overview-slider", type: "slider", xAxisIndex: 1, filterMode: "none", startValue: viewport.start, endValue: viewport.end, left: 58, right: 48, top: 7, height: 44, showDataShadow: false, showDetail: false, brushSelect: true, borderColor: "#dce5ef", backgroundColor: "transparent", fillerColor: "rgba(35,124,229,.07)", handleStyle: { color: "#fff", borderColor: "#91accb" }, moveHandleSize: 0 }],
      series: visibleOverview.map((item): LineSeriesOption => ({ id: item.id, name: item.name, type: "line", xAxisIndex: 0, yAxisIndex: 0, silent: true, showSymbol: false, connectNulls: false, smooth: false, data: breakCurveAtGaps(item.points, item.gaps), lineStyle: { width: 1, opacity: .62, color: item.color ?? COLORS[Math.max(0, series.findIndex((entry) => entry.id === item.id)) % COLORS.length], type: item.dash ?? (item.reference ? "dashed" : "solid") } }))
    };
    overviewChart.current.setOption(option, { replaceMerge: ["series"], lazyUpdate: false });
  }, [series, visibleOverview, domain, viewport, absolute]);

  function applyRange() {
    const next = clampViewport({ start: Number(xStart), end: Number(xEnd) }, domain);
    if (!xStart.trim() || !xEnd.trim() || !next) { setRangeError("请输入起点小于终点且与采集范围相交的有效时间范围。"); return; }
    if (yMin && !Number.isFinite(Number(yMin)) || yMax && !Number.isFinite(Number(yMax)) || yMin && yMax && Number(yMin) >= Number(yMax)) { setRangeError("纵轴下限必须小于上限。"); return; }
    yZoom.current = { start: 0, end: 100 };
    setRangeError(""); setYRange({ min: yMin ? Number(yMin) : undefined, max: yMax ? Number(yMax) : undefined });
    setRangeOpen(null); commitViewport(next);
  }
  function reset() {
    cancelPending(); yZoom.current = { start: 0, end: 100 };
    setYMin(""); setYMax(""); setYRange({}); setRangeError(""); setRangeOpen(null);
    setXStart(String(domain.start)); setXEnd(String(domain.end));
    chart.current?.dispatchAction({ type: "brush", areas: [] });
    chart.current?.dispatchAction({ type: "dataZoom", dataZoomId: "y-slider", start: 0, end: 100 });
    commitViewport({ ...domain });
  }
  return <section className={`time-series-workspace c-v3-chart c-v3-chart--${layout}`} aria-label="可交互曲线">
    <header className="chart-heading"><h3>{title}</h3><span className="chart-resolution">{resolution}</span></header>
    <div className="chart-toolbar">
      <div className="chart-range-controls" ref={rangeControls} onKeyDown={(event) => { if (event.key === "Escape") { const kind = rangeOpen; setRangeOpen(null); rangeControls.current?.querySelector<HTMLButtonElement>(`button[data-range="${kind}"]`)?.focus(); } }}>
        <button type="button" data-range="x" aria-label="X 范围" aria-expanded={rangeOpen === "x"} onClick={() => setRangeOpen(rangeOpen === "x" ? null : "x")}><b>X 范围</b><span>{numberText(viewport.start)} – {numberText(viewport.end)}</span><ChevronDown aria-hidden="true" /></button>
        <button type="button" data-range="y" aria-label="Y 范围" aria-expanded={rangeOpen === "y"} onClick={() => setRangeOpen(rangeOpen === "y" ? null : "y")}><b>Y 范围</b><span>{yRange.min === undefined && yRange.max === undefined ? "自动" : `${yRange.min ?? "自动"} – ${yRange.max ?? "自动"}`}</span><ChevronDown aria-hidden="true" /></button>
        {rangeOpen && <form className="chart-range-popover" aria-label="坐标范围" onSubmit={(event) => { event.preventDefault(); applyRange(); }}>
          <label>时间起点<input autoFocus={rangeOpen === "x"} type="number" step="any" value={xStart} onChange={(event) => setXStart(event.target.value)} /></label>
          <label>时间终点<input type="number" step="any" value={xEnd} onChange={(event) => setXEnd(event.target.value)} /></label>
          <label>纵轴下限<input autoFocus={rangeOpen === "y"} type="number" step="any" placeholder="自动" value={yMin} onChange={(event) => setYMin(event.target.value)} /></label>
          <label>纵轴上限<input type="number" step="any" placeholder="自动" value={yMax} onChange={(event) => setYMax(event.target.value)} /></label>
          {rangeError && <p role="alert">{rangeError}</p>}<button type="submit">应用范围</button>
        </form>}
        <button type="button" className="chart-reset" onClick={reset}><RotateCcw aria-hidden="true" />重置视图</button>
      </div>
      <div className="chart-interaction-controls"><button type="button" aria-pressed={mode === "box"} onClick={() => setMode("box")}><Scan aria-hidden="true" />框选缩放</button><button type="button" aria-pressed={mode === "pan"} onClick={() => setMode("pan")}><Move aria-hidden="true" />平移</button><label><input type="checkbox" checked={linked} onChange={(event) => setLinked(event.target.checked)} />联动游标</label></div>
    </div>
    {referenceControls && <div className="chart-reference-controls">{referenceControls}</div>}
    {unitMismatch && <p role="alert" className="curve-error">单位不一致，无法比较。请选择同参数、同单位的曲线。</p>}
    <div className="curve-canvas-wrap"><div ref={host} className="time-series-canvas" style={{ height: chartHeight }} role="img" aria-label={`${visibleSeries.length} 条${series[0]?.unit ?? ""}曲线，可通过下方图例控制显示，通过时间与纵轴范围控制缩放`} />{loading && <span className="curve-refresh" role="status">正在读取当前视窗…</span>}</div>
    <div className="curve-legend" aria-label="曲线图例">{series.map((item, index) => <label key={item.id}><input type="checkbox" aria-label={`显示曲线 ${item.name}`} checked={!hidden.includes(item.id)} onChange={() => setHidden((current) => current.includes(item.id) ? current.filter((id) => id !== item.id) : [...current, item.id])} /><i aria-hidden="true" style={{ borderColor: item.color ?? COLORS[index % COLORS.length], borderTopStyle: item.dash ?? (item.reference ? "dashed" : "solid") }} /><span>{item.name}{item.reference && <small>参考</small>}</span><strong>{item.unit || "无单位"}</strong></label>)}</div>
    <div className="chart-overview"><div className="chart-overview-label"><span>{overviewSeries ? "全程概览" : "概览 · 已加载数据"}</span>{overviewLoading && <span role="status">概览读取中…</span>}<span>拖动两端手柄调整时间范围</span></div><div ref={overviewHost} className="chart-overview-canvas" role="img" aria-label="时间范围概览" /></div>
    <footer className="curve-chart-footer"><span>Ctrl＋滚轮横向缩放 · Shift＋滚轮纵向缩放 · 右侧滑块调整纵轴</span><span>缺失段保持断线</span></footer>
  </section>;
}
