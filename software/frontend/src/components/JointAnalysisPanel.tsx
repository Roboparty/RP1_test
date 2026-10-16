import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Pause, Play, Plus, X } from "lucide-react";
import { getExecutionJointAnalysis } from "../api";
import { valueTicks } from "../performanceChart";
import { isMetricQualityUsable } from "../metricQuality";
import type {
  ExecutionJointAnalysis,
  JointAnalysisPoint,
  JointAnalysisSeries
} from "../types";
import { EmptyState, ErrorState, LoadingState, Panel } from "./Workbench";

const DEFAULT_METRICS = [
  "JOINT-TARGET-POSITION",
  "JOINT-ACTUAL-POSITION",
  "JOINT-TRACKING-ERROR",
  "JOINT-TORQUE",
  "JOINT-TEMPERATURE"
];

const SUMMARY_ORDER = [
  "TRACKING_ERROR_START_RMS",
  "TRACKING_ERROR_END_RMS",
  "TRACKING_ERROR_CHANGE_RATE",
  "TRACKING_ERROR_P95_ABS",
  "TORQUE_MEAN_ABS",
  "TORQUE_PEAK_ABS",
  "TEMPERATURE_RISE",
  "VIBRATION_MAX",
  "CYCLE_DURATION"
];

const SERIES_COLORS: Record<string, string> = {
  "JOINT-TARGET-POSITION": "#6c86ad",
  "JOINT-ACTUAL-POSITION": "#1f6fd1",
  "JOINT-TRACKING-ERROR": "#c34f62",
  "JOINT-VELOCITY": "#3c8a70",
  "JOINT-TORQUE": "#b87722",
  "JOINT-CURRENT": "#178a91",
  "JOINT-TEMPERATURE": "#a15485",
  "JOINT-VIBRATION-RMS": "#7658a6"
};

type ChartGroup = {
  key: string;
  title: string;
  unit: string;
  series: JointAnalysisSeries[];
  wide?: boolean;
};

export function numericDomain(series: JointAnalysisSeries[]) {
  const values = series.flatMap((item) =>
    item.points
      .filter((point) =>
        point.value !== null &&
        Number.isFinite(point.value) &&
        isMetricQualityUsable(point.quality_status)
      )
      .map((point) => point.value as number)
  );
  if (values.length === 0) return { min: 0, max: 1 };
  const min = Math.min(...values);
  const max = Math.max(...values);
  if (min === max) {
    const padding = Math.max(Math.abs(min) * 0.08, 1);
    return { min: min - padding, max: max + padding };
  }
  const padding = (max - min) * 0.1;
  return { min: min - padding, max: max + padding };
}

export function pointSegments(points: JointAnalysisPoint[]) {
  const segments: Array<Array<{ progress_percent: number; value: number }>> = [];
  let current: Array<{ progress_percent: number; value: number }> = [];
  for (const point of points) {
    if (
      point.value === null ||
      point.progress_percent === null ||
      !Number.isFinite(point.value) ||
      !isMetricQualityUsable(point.quality_status)
    ) {
      if (current.length > 1) segments.push(current);
      current = [];
      continue;
    }
    current.push({ progress_percent: point.progress_percent, value: point.value });
  }
  if (current.length > 1) segments.push(current);
  return segments;
}

function pathFor(
  points: Array<{ progress_percent: number; value: number }>,
  width: number,
  height: number,
  domain: { min: number; max: number },
  insetX = 52,
  insetY = 24
) {
  const xSpan = width - insetX * 2;
  const ySpan = height - insetY * 2;
  const valueSpan = Math.max(Number.EPSILON, domain.max - domain.min);
  return points.map((point, index) => {
    const x = insetX + Math.max(0, Math.min(100, point.progress_percent)) / 100 * xSpan;
    const y = insetY + (domain.max - point.value) / valueSpan * ySpan;
    return `${index === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(" ");
}

function closestPoint(series: JointAnalysisSeries, progress: number) {
  return series.points.reduce<JointAnalysisPoint | null>((closest, point) => {
    if (
      point.progress_percent === null ||
      point.value === null ||
      !Number.isFinite(point.value) ||
      !isMetricQualityUsable(point.quality_status)
    ) return closest;
    if (!closest || closest.progress_percent === null) return point;
    return Math.abs(point.progress_percent - progress) <
      Math.abs(closest.progress_percent - progress) ? point : closest;
  }, null);
}

function formatMetricValue(value: number | null, unit: string) {
  if (value === null || !Number.isFinite(value)) return "—";
  const absolute = Math.abs(value);
  const digits = absolute >= 100 ? 1 : absolute >= 10 ? 2 : 3;
  return `${value.toLocaleString("zh-CN", { maximumFractionDigits: digits })}${unit ? ` ${unit}` : ""}`;
}

function JointCurveChart({
  group,
  cursor,
  onCursor
}: {
  group: ChartGroup;
  cursor: number;
  onCursor: (progress: number) => void;
}) {
  const width = 920;
  const height = 218;
  const insetX = 52;
  const insetY = 24;
  const geometry = useMemo(() => {
    const domain = numericDomain(group.series);
    return {
      ticks: valueTicks(domain, 4),
      paths: group.series.flatMap((series) =>
        pointSegments(series.points).map((segment, index) => ({
          key: `${series.id}-${index}`,
          path: pathFor(segment, width, height, domain, insetX, insetY),
          color: SERIES_COLORS[series.metric_code] ?? "#2f6fe4",
          target: series.metric_code === "JOINT-TARGET-POSITION"
        }))
      )
    };
  }, [group]);
  const cursorX = insetX + cursor / 100 * (width - insetX * 2);
  const cursorValues = group.series.map((series) => ({
    series,
    point: closestPoint(series, cursor)
  }));
  return (
    <section className={`joint-chart${group.wide ? " wide" : ""}`}>
      <header>
        <div><strong>{group.title}</strong><span>{group.unit}</span></div>
        <small>{group.series[0]?.display_point_count ?? 0} / {group.series[0]?.point_count ?? 0} 点</small>
      </header>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label={`${group.title}运动周期采样曲线`}
        onPointerMove={(event) => {
          const rect = event.currentTarget.getBoundingClientRect();
          const viewX = (event.clientX - rect.left) / rect.width * width;
          onCursor(Math.max(0, Math.min(100, (viewX - insetX) / (width - insetX * 2) * 100)));
        }}
      >
        {geometry.ticks.map((tick, index) => {
          const y = insetY + (geometry.ticks.length - 1 - index) / (geometry.ticks.length - 1) * (height - insetY * 2);
          return (
            <g key={tick}>
              <line className="joint-chart-grid" x1={insetX} x2={width - insetX} y1={y} y2={y} />
              <text className="joint-chart-label" x={insetX - 8} y={y + 3} textAnchor="end">{tick.toFixed(1)}</text>
            </g>
          );
        })}
        {[0, 25, 50, 75, 100].map((progress) => {
          const x = insetX + progress / 100 * (width - insetX * 2);
          return (
            <g key={progress}>
              <line className="joint-chart-grid vertical" x1={x} x2={x} y1={insetY} y2={height - insetY} />
              <text className="joint-chart-label" x={x} y={height - 5} textAnchor="middle">{progress}%</text>
            </g>
          );
        })}
        {geometry.paths.map((path) => (
          <path
            key={path.key}
            d={path.path}
            fill="none"
            stroke={path.color}
            className={`joint-chart-line ${path.target ? "target" : ""}`}
          />
        ))}
        <line className="joint-chart-cursor" x1={cursorX} x2={cursorX} y1={insetY} y2={height - insetY} />
        <circle className="joint-chart-cursor-dot" cx={cursorX} cy={insetY} r="3.5" />
      </svg>
      <footer>
        {cursorValues.map(({ series, point }) => (
          <span key={series.id}>
            <i style={{ backgroundColor: SERIES_COLORS[series.metric_code] }} />
            {series.metric_name}
            <strong>{formatMetricValue(point?.value ?? null, series.unit)}</strong>
          </span>
        ))}
      </footer>
    </section>
  );
}

export function analysisGroups(
  data: ExecutionJointAnalysis,
  visibleMetricCodes: string[]
): ChartGroup[] {
  const visible = data.series.filter((series) => visibleMetricCodes.includes(series.metric_code));
  const groups: ChartGroup[] = [];
  const positions = visible.filter((series) =>
    ["JOINT-TARGET-POSITION", "JOINT-ACTUAL-POSITION"].includes(series.metric_code)
  );
  if (positions.length) {
    groups.push({ key: "position", title: "目标 / 实际位置跟踪", unit: positions[0].unit, series: positions, wide: true });
  }
  for (const series of visible) {
    if (positions.includes(series)) continue;
    groups.push({
      key: series.metric_code,
      title: series.metric_name,
      unit: series.unit,
      series: [series]
    });
  }
  return groups;
}

export function JointAnalysisPanel({ executionId }: { executionId: string }) {
  const [subjectCode, setSubjectCode] = useState<string>();
  const [stageCode, setStageCode] = useState<string>();
  const [cycleIndex, setCycleIndex] = useState<number>();
  const [visibleMetricCodes, setVisibleMetricCodes] = useState(DEFAULT_METRICS);
  const [cursor, setCursor] = useState(0);
  const [playing, setPlaying] = useState(false);

  const query = useQuery({
    queryKey: ["execution-joint-analysis", executionId, subjectCode, stageCode, cycleIndex],
    queryFn: ({ signal }) => getExecutionJointAnalysis(
      executionId,
      { subjectCode, stageCode, cycleIndex },
      signal
    )
  });
  const data = query.data;
  const resolved = data?.resolved_selection;

  useEffect(() => {
    if (!playing) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      setPlaying(false);
      return;
    }
    let previous = performance.now();
    let frame = 0;
    const advance = (now: number) => {
      const elapsed = Math.min(100, now - previous);
      previous = now;
      setCursor((value) => value >= 100 ? 0 : Math.min(100, value + elapsed / 75));
      frame = window.requestAnimationFrame(advance);
    };
    frame = window.requestAnimationFrame(advance);
    return () => window.cancelAnimationFrame(frame);
  }, [playing]);

  useEffect(() => {
    if (!data) return;
    const available = new Set(data.metrics.map((metric) => metric.metric_code));
    if (!visibleMetricCodes.some((code) => available.has(code)) && data.metrics[0]) {
      setVisibleMetricCodes([data.metrics[0].metric_code]);
    }
  }, [data, visibleMetricCodes]);

  const groups = useMemo(
    () => data ? analysisGroups(data, visibleMetricCodes) : [],
    [data, visibleMetricCodes]
  );
  const selectedSubject = data?.subjects.find((item) => item.subject_code === resolved?.subject_code);
  const summary = SUMMARY_ORDER
    .map((code) => data?.summary.find((item) => item.code === code))
    .filter((item) => item !== undefined);
  const addableMetrics = data?.metrics.filter(
    (metric) => !visibleMetricCodes.includes(metric.metric_code)
  ) ?? [];

  return (
    <Panel
      className="joint-analysis-panel"
      title="关节运动周期分析"
      eyebrow="JOINT MOTION CYCLE ANALYSIS"
      trailing={data && <span className="analysis-sample-count">{data.series.reduce((sum, series) => sum + series.point_count, 0).toLocaleString("zh-CN")} 个采样点</span>}
    >
      {query.isLoading ? <LoadingState label="正在加载关节周期数据" /> : query.isError ? (
        <ErrorState error={query.error} retry={() => query.refetch()} />
      ) : !data || data.subjects.length === 0 ? (
        <EmptyState title="尚无关节周期数据" description="该执行记录尚未关联降采样运动周期序列。" />
      ) : (
        <>
          <div className="joint-analysis-toolbar">
            <label>
              <span>分析对象</span>
              <select
                value={resolved?.subject_code ?? ""}
                onChange={(event) => {
                  setSubjectCode(event.target.value);
                  setStageCode(undefined);
                  setCycleIndex(undefined);
                  setCursor(0);
                }}
              >
                {data.subjects.map((subject) => <option key={subject.subject_code} value={subject.subject_code}>{subject.name}</option>)}
              </select>
            </label>
            <label>
              <span>测试阶段</span>
              <select
                value={resolved?.stage_code ?? ""}
                onChange={(event) => {
                  setSubjectCode(resolved?.subject_code ?? undefined);
                  setStageCode(event.target.value);
                  setCycleIndex(undefined);
                  setCursor(0);
                }}
              >
                {data.stages.map((stage) => <option key={stage.stage_code} value={stage.stage_code}>{stage.sequence_no.toString().padStart(2, "0")} · {stage.stage_name}</option>)}
              </select>
            </label>
            <div className="cycle-selector" role="group" aria-label="运动周期">
              <span>运动周期</span>
              <div>
                {data.cycles.map((cycle) => (
                  <button
                    type="button"
                    key={cycle.cycle_index}
                    className={cycle.cycle_index === resolved?.cycle_index ? "active" : ""}
                    aria-pressed={cycle.cycle_index === resolved?.cycle_index}
                    onClick={() => {
                      setSubjectCode(resolved?.subject_code ?? undefined);
                      setStageCode(resolved?.stage_code ?? undefined);
                      setCycleIndex(cycle.cycle_index);
                      setCursor(0);
                    }}
                  >
                    C{cycle.cycle_index.toString().padStart(2, "0")}
                    <small>{cycle.quality_status}</small>
                  </button>
                ))}
              </div>
            </div>
            <div className="metric-adder">
              <span>曲线指标</span>
              <label>
                <Plus aria-hidden="true" />
                <select
                  value=""
                  disabled={addableMetrics.length === 0}
                  onChange={(event) => {
                    if (event.target.value) {
                      setVisibleMetricCodes((codes) => [...codes, event.target.value]);
                    }
                  }}
                >
                  <option value="">{addableMetrics.length ? "添加指标" : "已全部添加"}</option>
                  {addableMetrics.map((metric) => <option key={metric.metric_code} value={metric.metric_code}>{metric.metric_name}</option>)}
                </select>
              </label>
            </div>
          </div>

          <div className="analysis-context-line">
            <strong>{selectedSubject?.name}</strong>
            <span>{selectedSubject?.subject_code} · {selectedSubject?.subject_kind}</span>
            <span>{data.series[0]?.downsample_method} · {data.series[0]?.sampling_interval_ms} ms</span>
          </div>

          <section className="joint-summary-band" aria-label="关节分析摘要">
            {summary.map((item) => (
              <div key={item.code} data-quality={item.quality_status}>
                <span>{item.label}</span>
                <strong>{formatMetricValue(item.value, item.unit)}</strong>
                <small>{item.quality_status} · {item.sample_count} 点</small>
              </div>
            ))}
          </section>

          <div className="visible-metrics" aria-label="已显示曲线指标">
            {data.metrics.filter((metric) => visibleMetricCodes.includes(metric.metric_code)).map((metric) => (
              <button
                type="button"
                key={metric.metric_code}
                onClick={() => setVisibleMetricCodes((codes) => codes.filter((code) => code !== metric.metric_code))}
              >
                <i style={{ backgroundColor: SERIES_COLORS[metric.metric_code] }} />
                {metric.metric_name}
                <X aria-hidden="true" />
              </button>
            ))}
          </div>

          <div className="cycle-playback">
            <button type="button" onClick={() => setPlaying((value) => !value)} aria-label={playing ? "暂停周期游标" : "播放周期游标"}>
              {playing ? <Pause aria-hidden="true" /> : <Play aria-hidden="true" />}
              {playing ? "暂停" : "播放"}
            </button>
            <input
              type="range"
              min="0"
              max="100"
              step="0.1"
              value={cursor}
              aria-label="运动周期进度"
              onChange={(event) => {
                setPlaying(false);
                setCursor(Number(event.target.value));
              }}
            />
            <output>{cursor.toFixed(1)}%</output>
          </div>

          {groups.length === 0 ? (
            <EmptyState title="尚未选择曲线指标" description="从“添加指标”中选择需要查看的关节分析曲线。" />
          ) : (
            <div className="joint-chart-grid-layout">
              {groups.map((group) => <JointCurveChart key={group.key} group={group} cursor={cursor} onCursor={(progress) => { setPlaying(false); setCursor(progress); }} />)}
            </div>
          )}
        </>
      )}
    </Panel>
  );
}
