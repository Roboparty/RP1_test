import { FormEvent, useState } from "react";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { ArrowLeft, ExternalLink, Search } from "lucide-react";
import {
  getAsset,
  getAssetEvents,
  getAssetExecutions,
  getAssetPerformanceTrends,
  getAllAssets,
  getAssets,
  getExecution,
  getMtbfConclusions,
  getTestCase,
  getTestCaseDurations
} from "../api";
import { formatDate, formatDuration, isShowcaseValue } from "../navigation";
import { mergeUniquePages, paginationKey } from "../pagination";
import {
  linePath,
  performancePointSegments,
  performanceSeriesStatistics,
  sharedValueDomain,
  valueTicks
} from "../performanceChart";
import type { AssetKind, AssetPerformanceTrends, AssetSummary, DurationTotals, ExecutionListItem, ExecutionResult, MtbfConclusionItem, TargetPartCode } from "../types";
import {
  Cutoff,
  EmptyState,
  ErrorState,
  LoadMore,
  LoadingState,
  PageTitle,
  Panel,
  ShowcaseBadge
} from "../components/Workbench";
import { JointAnalysisPanel } from "../components/JointAnalysisPanel";
import { JointRecordPanel } from "../components/JointRecordPanel";
import { MODULE_DIRECTORY } from "../homeCarousel";
import { AssetCreateForm } from "./SetupPages";
import { StatusMark } from "../components/StatusMark";

type Navigate = (view: "asset" | "test-case" | "execution" | "assets" | "curves", id?: string) => void;

function DurationBand({ totals }: { totals: DurationTotals }) {
  return (
    <section className="duration-band" aria-label="时长口径">
      <div><span>累计闭合时长</span><strong>{formatDuration(totals.total_duration_seconds)}</strong></div>
      <div><span>有效暴露</span><strong>{formatDuration(totals.effective_exposure_seconds)}</strong></div>
      <div><span>排除</span><strong>{formatDuration(totals.excluded_seconds)}</strong></div>
      <div><span>待确认</span><strong>{formatDuration(totals.pending_seconds)}</strong></div>
      <div className="active"><span>运行中时长</span><strong>{formatDuration(totals.active_elapsed_seconds)}</strong></div>
    </section>
  );
}

function BackButton({ onClick, label = "返回列表" }: { onClick: () => void; label?: string }) {
  return <button type="button" className="back-button" onClick={onClick}><ArrowLeft aria-hidden="true" />{label}</button>;
}

function returnToPreviousPage(navigate: Navigate) {
  if (window.history.state?.rp1Navigation === true) {
    window.history.back();
    return;
  }
  navigate("assets");
}

const stageColors = ["#2f6fe4", "#16a6a1", "#c88918", "#3d9365"];

function PerformanceTrendPanel({
  data,
  loading,
  error,
  retry
}: {
  data?: AssetPerformanceTrends;
  loading: boolean;
  error: unknown;
  retry: () => void;
}) {
  const [selectedCode, setSelectedCode] = useState("");
  const metric = data?.metrics.find((item) => item.metric_code === selectedCode) ?? data?.metrics[0];
  const domain = sharedValueDomain(metric?.series ?? []);
  const ticks = valueTicks(domain);
  const width = 920;
  const height = 300;
  return (
    <Panel title="跨阶段性能曲线" className="performance-trend-panel">
      {loading ? <LoadingState /> : error ? <ErrorState error={error} retry={retry} /> : !data || !metric ? (
        <EmptyState title="暂无连续阶段指标" description="该样品尚无完整的多阶段连续观测数据。" />
      ) : (
        <>
          <div className="trend-toolbar">
            <label>性能指标
              <select value={metric.metric_code} onChange={(event) => setSelectedCode(event.target.value)}>
                {data.metrics.map((item) => <option key={item.metric_code} value={item.metric_code}>{item.metric_name} · {item.unit}</option>)}
              </select>
            </label>
            <p><strong>{data.asset_code}</strong><span>{data.execution_code} · {data.test_case_code}</span></p>
          </div>
          <div className="trend-chart-wrap">
            <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-labelledby="trend-chart-title trend-chart-desc">
              <title id="trend-chart-title">{metric.metric_name}跨阶段性能曲线</title>
              <desc id="trend-chart-desc">同一样品同一指标在预检、负载爬升、稳态运行和恢复复测阶段的连续观测，横轴为阶段内进度，纵轴单位为{metric.unit}。</desc>
              {ticks.map((tick, index) => {
                const y = 24 + (ticks.length - 1 - index) / (ticks.length - 1) * (height - 48);
                return <g key={tick}><line x1="24" x2={width - 24} y1={y} y2={y} className="trend-grid" /><text x="18" y={y + 3} textAnchor="end" className="trend-axis-label">{tick.toFixed(1)}</text></g>;
              })}
              {[0, 25, 50, 75, 100].map((progress) => {
                const x = 24 + progress / 100 * (width - 48);
                return <g key={progress}><line x1={x} x2={x} y1="24" y2={height - 24} className="trend-grid vertical" /><text x={x} y={height - 6} textAnchor="middle" className="trend-axis-label">{progress}%</text></g>;
              })}
              {metric.series.flatMap((series, index) => {
                const statistics = performanceSeriesStatistics(series);
                return performancePointSegments(series.points).map((segment, segmentIndex) => (
                  <path
                    key={`${series.stage_code}-${segmentIndex}`}
                    d={linePath(segment, width, height, domain)}
                    fill="none"
                    stroke={stageColors[index % stageColors.length]}
                    className="trend-line"
                  >
                    <title>{series.stage_name}：{statistics ? `${statistics.min.toFixed(2)}–${statistics.max.toFixed(2)} ${metric.unit}` : "无有效点"}</title>
                  </path>
                ));
              })}
            </svg>
          </div>
          <div className="trend-legend" aria-label="阶段图例及统计">
            {metric.series.map((series, index) => {
              const statistics = performanceSeriesStatistics(series);
              return (
                <div key={series.stage_code}>
                  <i style={{ backgroundColor: stageColors[index % stageColors.length] }} aria-hidden="true" />
                  <span>{String(series.sequence_no).padStart(2, "0")} · {series.stage_name}</span>
                  <strong>{statistics ? `${statistics.average.toFixed(2)} ${metric.unit}` : "无有效值"}</strong>
                  <small>{statistics ? `最小 ${statistics.min.toFixed(2)} / 最大 ${statistics.max.toFixed(2)} · ${statistics.count} 点` : "已排除无效和缺失数据"}</small>
                </div>
              );
            })}
          </div>
        </>
      )}
    </Panel>
  );
}

export function DurationsPage({ assetKind, navigate }: { assetKind: AssetKind; navigate: Navigate }) {
  const [draft, setDraft] = useState("");
  const [assetDraft, setAssetDraft] = useState("");
  const [testCaseDraft, setTestCaseDraft] = useState("");
  const [filters, setFilters] = useState({ search: "", asset: "", testCase: "" });
  const [status, setStatus] = useState("");
  const query = useInfiniteQuery({
    queryKey: paginationKey(
      "durations",
      assetKind,
      filters.search,
      filters.asset,
      filters.testCase,
      status
    ),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      getTestCaseDurations({
        assetKind,
        search: filters.search,
        asset: filters.asset,
        testCase: filters.testCase,
        status,
        cursor: pageParam ?? undefined
      }, signal),
    getNextPageParam: (lastPage) => lastPage.page.next_cursor
  });
  const data = query.data?.pages[0];
  const items = mergeUniquePages(query.data?.pages ?? [], (item) => `${item.id}-${item.asset_id}`);
  const hasShowcase = items.some((item) => isShowcaseValue(item.code) || isShowcaseValue(item.asset_code));
  function submit(event: FormEvent) {
    event.preventDefault();
    setFilters({
      search: draft.trim(),
      asset: assetDraft.trim(),
      testCase: testCaseDraft.trim()
    });
  }
  return (
    <>
      <PageTitle
        eyebrow="时长统计"
        title={`${assetKind === "MODULE" ? "模块" : "整机"}测试时长台账`}
        trailing={data && <div className="title-status">{hasShowcase && <ShowcaseBadge />}<Cutoff value={data.data_cutoff_at} asOf={data.as_of_at} /></div>}
      />
      <form className="filter-bar" onSubmit={submit} role="search">
        <label>全文搜索<input value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="样品或测试用例" /></label>
        <label>样品编号<input value={assetDraft} onChange={(event) => setAssetDraft(event.target.value)} placeholder="精确编号，可留空" /></label>
        <label>测试用例<input value={testCaseDraft} onChange={(event) => setTestCaseDraft(event.target.value)} placeholder="精确编号，可留空" /></label>
        <label>执行状态<select value={status} onChange={(event) => setStatus(event.target.value)}><option value="">全部状态</option><option value="RUNNING">运行中</option><option value="COMPLETED">已完成</option><option value="BLOCKED">阻塞</option><option value="FAILED">失败</option><option value="PAUSED">暂停</option></select></label>
        <button type="submit"><Search aria-hidden="true" />查询</button>
      </form>
      {query.isLoading ? <LoadingState /> : !data ? (
        <ErrorState error={query.error} retry={() => query.refetch()} />
      ) : (
        <>
          <DurationBand totals={data.totals} />
          <Panel title="按样品与用例核对">
            {items.length === 0 ? (
              <EmptyState title="当前筛选无匹配记录" description="可清空样品或测试用例条件后重试。" />
            ) : (
              <>
                <div className="table-scroll">
                  <table className="data-table ledger-table">
                    <thead><tr><th>#</th><th>测试用例</th><th>样品</th><th>状态</th><th>时长构成</th><th>有效</th><th>排除</th><th>待确认</th><th>运行中时长</th><th>执行</th></tr></thead>
                    <tbody>
                      {items.map((item, itemIndex) => (
                        <tr key={`${item.id}-${item.asset_id}`}>
                          <td className="rank-cell">{String(itemIndex + 1).padStart(2, "0")}</td>
                          <td><button className="link-button" type="button" onClick={() => navigate("test-case", item.code)}>{item.code}<ExternalLink aria-hidden="true" /></button><small>{item.name}</small></td>
                          <td><button className="link-button" type="button" onClick={() => navigate("asset", item.asset_code)}>{item.asset_code}</button><small>{item.asset_name}</small></td>
                          <td><StatusMark tone={item.status === "RUNNING" ? "normal" : item.status === "FAILED" || item.status === "BLOCKED" ? "danger" : "muted"} label={item.status} /></td>
                          <td className="duration-rank"><strong>{formatDuration(item.durations.total_duration_seconds)}</strong><progress max={Math.max(data.totals.total_duration_seconds, 1)} value={item.durations.total_duration_seconds} aria-label={`${item.code} 累计时长占当前筛选总时长`} /></td>
                          <td>{formatDuration(item.durations.effective_exposure_seconds)}</td>
                          <td>{formatDuration(item.durations.excluded_seconds)}</td>
                          <td>{formatDuration(item.durations.pending_seconds)}</td>
                          <td>{formatDuration(item.durations.active_elapsed_seconds)}</td>
                          <td>{item.execution_count}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <LoadMore hasMore={Boolean(query.hasNextPage)} loading={query.isFetchingNextPage} failed={query.isFetchNextPageError} onLoad={() => void query.fetchNextPage()} />
              </>
            )}
          </Panel>
          <p className="semantic-note">运行中时长仅供参考，不计入 MTBF 有效时长。</p>
        </>
      )}
    </>
  );
}

function mtbfStatisticsNote(item: MtbfConclusionItem): string {
  if (item.statistical.point_estimate_status === "NOT_CALCULATED") {
    return "尚无统计结果。";
  }
  if (item.statistical.point_estimate_status === "NO_EXPOSURE") {
    return "尚无有效暴露时长，暂不形成 MTBF 估计。";
  }
  if (item.observed.relevant_failure_count === 0) {
    return Object.values(item.statistical.lower_bounds_hours).some(Number.isFinite)
      ? "暂无相关故障，仅显示单侧置信下限。"
      : "暂无相关故障，尚无可显示的置信下限。";
  }
  return "结果对应本次统计截止时间。";
}

function MtbfConfidenceScale({ item }: { item: MtbfConclusionItem }) {
  const lower70 = item.statistical.lower_bounds_hours["0.7"] ?? null;
  const lower90 = item.statistical.lower_bounds_hours["0.9"] ?? null;
  const point = item.statistical.point_estimate_hours;
  const values = [lower70, lower90, point].filter((value): value is number => value !== null);
  const maxValue = Math.max(...values, 1) * 1.14;
  const position = (value: number) => 34 + (value / maxValue) * 712;
  return (
    <div className="confidence-scale">
      <header><h3>单侧置信下界与点估计</h3></header>
      <svg viewBox="0 0 780 190" role="img" aria-labelledby="mtbf-scale-title">
        <title id="mtbf-scale-title">MTBF 单侧置信下界与点估计刻度</title>
        <defs>
          <linearGradient id="confidence-fill" x1="0" x2="1">
            <stop offset="0" stopColor="var(--cyan)" stopOpacity=".2" />
            <stop offset="1" stopColor="var(--brand)" stopOpacity=".75" />
          </linearGradient>
        </defs>
        <line className="scale-axis" x1="34" y1="116" x2="746" y2="116" />
        {[0, .25, .5, .75, 1].map((tick) => (
          <g key={tick} className="scale-tick">
            <line x1={34 + tick * 712} y1="110" x2={34 + tick * 712} y2="124" />
            <text x={34 + tick * 712} y="148" textAnchor="middle">{(maxValue * tick).toFixed(0)} h</text>
          </g>
        ))}
        {lower90 !== null && <line className="bound-line bound-90" x1={position(lower90)} y1="54" x2={position(lower90)} y2="116" />}
        {lower70 !== null && <line className="bound-line bound-70" x1={position(lower70)} y1="38" x2={position(lower70)} y2="116" />}
        {point !== null && <rect className="point-band" x={position(lower90 ?? 0)} y="91" width={Math.max(position(point) - position(lower90 ?? 0), 4)} height="25" rx="4" />}
        {lower90 !== null && <text className="bound-label" x={position(lower90)} y="47" textAnchor="middle">90% · {lower90.toFixed(1)} h</text>}
        {lower70 !== null && <text className="bound-label" x={position(lower70)} y="30" textAnchor="middle">70% · {lower70.toFixed(1)} h</text>}
        {point !== null && <g><circle className="point-dot" cx={position(point)} cy="103" r="7" /><text className="point-label" x={position(point)} y="82" textAnchor="middle">点估计 {point.toFixed(1)} h</text></g>}
        {point === null && <text className="no-point-label" x="390" y="82" textAnchor="middle">尚未形成点估计</text>}
      </svg>
    </div>
  );
}

export function MtbfPage({ assetKind }: { assetKind: AssetKind }) {
  const [selectedKey, setSelectedKey] = useState("");
  const query = useInfiniteQuery({
    queryKey: paginationKey("mtbf-conclusions", assetKind),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) => getMtbfConclusions(assetKind, pageParam ?? undefined, signal),
    getNextPageParam: (lastPage) => lastPage.page.next_cursor
  });
  const data = query.data?.pages[0];
  const items = mergeUniquePages(
    query.data?.pages ?? [],
    (item) => `${item.campaign_id}-${item.scope}`
  );
  const selected = items.find((item) => `${item.campaign_id}-${item.scope}` === selectedKey) ?? items[0];
  const hasShowcase = selected ? isShowcaseValue(selected.campaign_code) : false;
  return (
    <>
      <PageTitle
        eyebrow="可靠性统计"
        title={`${assetKind === "MODULE" ? "模块" : "整机"} MTBF 结论中心`}
        trailing={data && <div className="title-status">{hasShowcase && <ShowcaseBadge />}<Cutoff value={data.data_cutoff_at} asOf={data.as_of_at} /></div>}
      />
      {query.isLoading ? <LoadingState /> : !data ? (
        <ErrorState error={query.error} retry={() => query.refetch()} />
      ) : !selected ? (
        <EmptyState title="暂无可查看的 MTBF 统计范围" description="请先配置测试计划及统计范围，或联系管理员获取查看权限。" />
      ) : (
        <>
          <article className="mtbf-display">
            <header className="mtbf-command">
              <div><span>{selected.campaign_code} · {selected.scope}</span><h2>{selected.campaign_name}</h2><p>{selected.scope_name} · {selected.statistical.method}</p></div>
              <label>测试计划 / 统计范围
                <select value={`${selected.campaign_id}-${selected.scope}`} onChange={(event) => setSelectedKey(event.target.value)}>
                  {items.map((item) => <option key={`${item.campaign_id}-${item.scope}`} value={`${item.campaign_id}-${item.scope}`}>{item.campaign_code} · {item.scope}</option>)}
                </select>
              </label>
            </header>
            <section className="mtbf-kpi-band" aria-label="MTBF 核心结论">
              <div><span>有效暴露 T</span><strong>{formatDuration(selected.observed.effective_exposure_seconds)}</strong></div>
              <div><span>相关故障 r</span><strong>{selected.observed.relevant_failure_count}</strong></div>
              <div className="primary"><span>MTBF 点估计</span><strong>{selected.statistical.point_estimate_hours === null ? "未形成" : `${selected.statistical.point_estimate_hours.toFixed(1)} h`}</strong><small>{selected.statistical.point_estimate_status}</small></div>
              <div><span>90% 单侧下界</span><strong>{selected.statistical.lower_bounds_hours["0.9"] === undefined ? "尚无" : `${selected.statistical.lower_bounds_hours["0.9"].toFixed(1)} h`}</strong></div>
            </section>
            <div className="mtbf-analysis-grid">
              <MtbfConfidenceScale item={selected} />
              <aside className="mtbf-decision">
                <header><span>结论摘要</span><StatusMark tone={selected.blockers.length ? "warning" : "normal"} label={selected.blockers.length ? `${selected.blockers.length} 个阻断` : "无已知阻断"} shape={selected.blockers.length ? "diamond" : "circle"} /></header>
                <section><small>统计状态</small><strong>{selected.statistical.calculation_status}</strong><p>{mtbfStatisticsNote(selected)}</p></section>
                <section><small>正式结论</small><strong>{selected.verified.status === "NOT_AVAILABLE" ? "尚无正式结论" : selected.verified.status}</strong><p>{selected.verified.published_at ? `发布于 ${formatDate(selected.verified.published_at)}` : "当前统计结果尚未正式发布。"}</p></section>
                <section className="blocker-list"><small>阻断因素</small>{selected.blockers.length ? selected.blockers.map((blocker) => <span key={blocker}>{blocker}</span>) : <span className="clear">当前未记录阻断项</span>}</section>
              </aside>
            </div>
            <footer className="semantic-layers">
              <div><span>01</span><strong>观测事实</strong><small>暴露、故障与排除事实</small></div>
              <div><span>02</span><strong>统计输出</strong><small>方法、点估计与置信下界</small></div>
              <div><span>03</span><strong>正式验证</strong><small>结论状态与发布时间</small></div>
              <Cutoff value={selected.observed.data_cutoff_at} asOf={data.as_of_at} />
            </footer>
          </article>
          <LoadMore hasMore={Boolean(query.hasNextPage)} loading={query.isFetchingNextPage} failed={query.isFetchNextPageError} onLoad={() => void query.fetchNextPage()} />
        </>
      )}
    </>
  );
}

const targetPartNames: Record<TargetPartCode, string> = {
  ARM: "单臂",
  SARM: "单臂",
  LEG: "单腿",
  SLEG: "单腿",
  SYS: "整机系统",
  UPPER: "上肢",
  LOWER: "下肢",
  CHEST: "胸腔",
  HEAD: "头部",
  BAT: "动力电池"
};

export function AssetsPage({
  assetKind,
  part,
  selectPart,
  clearPart,
  navigate
}: {
  assetKind: AssetKind;
  part: TargetPartCode | null;
  selectPart: (part: TargetPartCode) => void;
  clearPart: () => void;
  navigate: Navigate;
}) {
  const [draft, setDraft] = useState("");
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const showPartDirectory = assetKind === "MODULE" && !part;
  const query = useInfiniteQuery({
    queryKey: paginationKey("assets", assetKind, part, status, search),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      getAssets({
        assetKind,
        targetPartCode: part ?? undefined,
        status,
        search,
        limit: 100,
        cursor: pageParam ?? undefined
      }, signal),
    getNextPageParam: (lastPage) => lastPage.page.next_cursor,
    enabled: !showPartDirectory
  });
  const directory = useQuery({
    queryKey: ["asset-part-directory", assetKind, status, search],
    queryFn: ({ signal }) => getAllAssets({ assetKind, status, search }, signal),
    enabled: showPartDirectory
  });
  const data = showPartDirectory ? directory.data : query.data?.pages[0];
  const items = showPartDirectory
    ? directory.data?.items ?? []
    : mergeUniquePages(query.data?.pages ?? [], (item) => item.id);
  const hasShowcase = items.some(
    (item) => isShowcaseValue(item.batch_code) || isShowcaseValue(item.current_execution_code)
  );
  const partGroups = MODULE_DIRECTORY
    .map((group) => ({
      code: group.code as TargetPartCode,
      items: items.filter((item) => item.target_part_code !== null && (group.members as readonly string[]).includes(item.target_part_code))
    }));
  const isLoading = showPartDirectory ? directory.isLoading : query.isLoading;
  const loadError = showPartDirectory ? directory.error : query.error;
  return (
    <>
      <PageTitle eyebrow="样品管理" title={part ? `${targetPartNames[part]} / ${part} 样品` : `${assetKind === "MODULE" ? "模块" : "整机"}样品中心`} trailing={data && <div className="title-status">{hasShowcase && <ShowcaseBadge />}<Cutoff value={data.data_cutoff_at} asOf={data.as_of_at} /></div>} />
      <AssetCreateForm key={assetKind} assetKind={assetKind} />
      {part && <div className="active-part-filter"><span>部位筛选：{targetPartNames[part]} / {part}</span><button type="button" onClick={clearPart}>清除筛选</button></div>}
      <form className="filter-bar" role="search" onSubmit={(event) => { event.preventDefault(); setSearch(draft.trim()); }}>
        <label>样品搜索<input value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="编号、名称或序列号" /></label>
        <label>生命周期<select value={status} onChange={(event) => setStatus(event.target.value)}><option value="">全部状态</option><option value="ACTIVE">在测</option><option value="PAUSED">暂停</option><option value="MAINTENANCE">维修</option><option value="EXITED">退出</option></select></label>
        <button type="submit"><Search aria-hidden="true" />查询</button>
      </form>
      {isLoading ? <LoadingState /> : !data ? (
        <ErrorState
          error={loadError}
          retry={() => void (showPartDirectory ? directory.refetch() : query.refetch())}
        />
      ) : (
        <Panel className="fleet-panel" title={showPartDirectory ? "按部位浏览" : part ? `${targetPartNames[part]}部位样品` : "整机样品"}>
          {items.length === 0 && !showPartDirectory ? <EmptyState title={part ? `没有可见的${targetPartNames[part]}样品` : "没有匹配样品"} description="请检查筛选条件和测试计划授权。" /> : (
            <>
              {showPartDirectory ? (
                <div className="asset-part-directory">
                  {partGroups.map((group) => {
                    const totalSeconds = group.items.reduce((sum, item) => sum + item.durations.total_duration_seconds, 0);
                    const activeCount = group.items.filter((item) => item.lifecycle_status === "ACTIVE").length;
                    return (
                      <button type="button" key={group.code} onClick={() => selectPart(group.code)}>
                        <span>{targetPartNames[group.code]}</span>
                        <strong>{group.code}</strong>
                        <dl>
                          <div><dt>样品</dt><dd>{group.items.length} 台</dd></div>
                          <div><dt>在测</dt><dd>{activeCount} 台</dd></div>
                          <div><dt>累计时长</dt><dd>{formatDuration(totalSeconds)}</dd></div>
                        </dl>
                        <ExternalLink aria-hidden="true" />
                      </button>
                    );
                  })}
                </div>
              ) : <AssetWall items={items} navigate={navigate} />}
              {!showPartDirectory && (
                <LoadMore hasMore={Boolean(query.hasNextPage)} loading={query.isFetchingNextPage} failed={query.isFetchNextPageError} onLoad={() => void query.fetchNextPage()} />
              )}
            </>
          )}
        </Panel>
      )}
    </>
  );
}

function AssetWall({ items, navigate }: { items: AssetSummary[]; navigate: Navigate }) {
  return (
    <div className="asset-wall">
      {items.map((asset) => (
        <article key={asset.id} data-risk={asset.health_status === "ABNORMAL" || asset.lifecycle_status === "MAINTENANCE" ? "high" : asset.health_status === "ATTENTION" ? "medium" : "normal"}>
          <header><button className="link-button asset-code" type="button" onClick={() => navigate("asset", asset.code)}>{asset.code}<ExternalLink aria-hidden="true" /></button><StatusMark tone={asset.lifecycle_status === "ACTIVE" ? "normal" : asset.lifecycle_status === "MAINTENANCE" ? "warning" : "muted"} label={asset.lifecycle_status} /></header>
          <p>{asset.name}</p>
          <dl><div><dt>当前用例</dt><dd>{asset.current_test_case_code ?? "无"}</dd></div><div><dt>累计 / 有效</dt><dd>{formatDuration(asset.durations.total_duration_seconds)} / {formatDuration(asset.durations.effective_exposure_seconds)}</dd></div><div><dt>健康</dt><dd>{asset.health_status ?? "证据不足"}</dd></div><div><dt>数据</dt><dd>{asset.freshness.status}</dd></div></dl>
          <footer><span>风险级别</span><strong>{asset.health_status === "ABNORMAL" ? "需立即复核" : asset.health_status === "ATTENTION" ? "持续观察" : "当前稳定"}</strong></footer>
        </article>
      ))}
    </div>
  );
}

export function AssetDetailPage({ id, navigate }: { id: string; navigate: Navigate }) {
  const detail = useQuery({ queryKey: ["asset", id], queryFn: ({ signal }) => getAsset(id, signal) });
  const trends = useQuery({
    queryKey: ["asset-performance-trends", id],
    queryFn: ({ signal }) => getAssetPerformanceTrends(id, signal)
  });
  const executions = useInfiniteQuery({
    queryKey: paginationKey("asset-executions", id),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) => getAssetExecutions(id, pageParam ?? undefined, signal),
    getNextPageParam: (lastPage) => lastPage.page.next_cursor
  });
  const events = useInfiniteQuery({
    queryKey: paginationKey("asset-events", id),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) => getAssetEvents(id, pageParam ?? undefined, signal),
    getNextPageParam: (lastPage) => lastPage.page.next_cursor
  });
  const executionItems = mergeUniquePages(executions.data?.pages ?? [], (item) => item.id);
  const eventItems = mergeUniquePages(events.data?.pages ?? [], (item) => item.id);
  if (detail.isLoading) return <LoadingState />;
  if (detail.isError || !detail.data) return <ErrorState error={detail.error} retry={() => detail.refetch()} />;
  const data = detail.data;
  return (
    <>
      <BackButton onClick={() => navigate("assets")} />
      <PageTitle eyebrow="样品档案" title={`${data.asset.code} · ${data.asset.name}`} description={`${data.asset.asset_kind === "MODULE" ? "模块" : "整机"} · ${data.asset.product_family} · ${data.asset.serial_number}`} trailing={<div className="title-status">{(isShowcaseValue(data.asset.batch_code) || isShowcaseValue(data.asset.current_execution_code)) && <ShowcaseBadge />}<StatusMark tone={data.asset.lifecycle_status === "ACTIVE" ? "normal" : "muted"} label={data.asset.lifecycle_status} /></div>} />
      <DurationBand totals={data.asset.durations} />
      <Cutoff value={data.data_cutoff_at} asOf={data.as_of_at} />
      <div className="detail-columns">
        <Panel title="当前上下文">
          {!data.current_context.execution_id ? (
            <EmptyState title="当前没有活跃执行" description="这里显示运行中、暂停或阻塞的执行；历史记录见下方执行记录。" />
          ) : (
            <dl className="fact-grid"><div><dt>测试计划 / 周期</dt><dd>{data.current_context.campaign_code ?? "无"} / {data.current_context.cycle_code ?? "无"}</dd></div><div><dt>执行记录</dt><dd>{data.current_context.execution_code ?? "无"}</dd></div><div><dt>测试用例</dt><dd>{data.current_context.test_case_code ?? "无"}</dd></div><div><dt>阶段</dt><dd>{data.current_context.stage_name ?? "无"}</dd></div><div><dt>运行中时长</dt><dd>{formatDuration(data.current_context.current_elapsed_seconds)}</dd></div><div><dt>心跳</dt><dd>{data.current_context.is_stale ? "延迟" : "正常"}</dd></div></dl>
          )}
        </Panel>
        <Panel title="配置 / 健康 / 证据">
          <dl className="fact-grid"><div><dt>配置指纹</dt><dd>{data.configuration.fingerprint ?? "未冻结"}</dd></div><div><dt>可靠性影响</dt><dd>{data.configuration.reliability_impact ?? "未知"}</dd></div><div><dt>执行 / 事件</dt><dd>{data.execution_count} / {data.event_count}</dd></div><div><dt>证据可用</dt><dd>{data.evidence_summary.available_count} / {data.evidence_summary.total_count}</dd></div></dl>
        </Panel>
      </div>
      <PerformanceTrendPanel
        data={trends.data}
        loading={trends.isLoading}
        error={trends.error}
        retry={() => void trends.refetch()}
      />
      <Panel title="执行记录">
        {executions.isLoading ? <LoadingState /> : !executions.data ? <ErrorState error={executions.error} retry={() => executions.refetch()} /> : (
          <>
            <ExecutionTable items={executionItems} navigate={navigate} />
            {executionItems.length > 0 && <LoadMore hasMore={Boolean(executions.hasNextPage)} loading={executions.isFetchingNextPage} failed={executions.isFetchNextPageError} onLoad={() => void executions.fetchNextPage()} />}
          </>
        )}
      </Panel>
      <Panel title="事件时间线">
        {events.isLoading ? <LoadingState /> : !events.data ? <ErrorState error={events.error} retry={() => events.refetch()} /> : eventItems.length === 0 ? <EmptyState title="尚无事件记录" description="该样品暂无事件记录。" /> : (
          <>
            <ol className="timeline">{eventItems.map((event) => <li key={event.id}><time>{formatDate(event.normalized_time)}</time><strong>{event.event_type}</strong><span>{event.interruption_classification ?? event.data_quality}</span></li>)}</ol>
            <LoadMore hasMore={Boolean(events.hasNextPage)} loading={events.isFetchingNextPage} failed={events.isFetchNextPageError} onLoad={() => void events.fetchNextPage()} />
          </>
        )}
      </Panel>
    </>
  );
}

function ExecutionTable({ items, navigate }: { items: ExecutionListItem[]; navigate: Navigate }) {
  if (items.length === 0) return <EmptyState title="尚无执行记录" description="当前范围内暂无执行记录。" />;
  return <div className="table-scroll"><table className="data-table"><thead><tr><th>执行记录</th><th>测试用例</th><th>样品</th><th>状态</th><th>起止</th><th>闭合时长</th><th>运行中时长</th><th>质量</th></tr></thead><tbody>{items.map((item) => <tr key={item.id}><td><button className="link-button" type="button" onClick={() => navigate("execution", item.id)}>{item.code}<ExternalLink aria-hidden="true" /></button></td><td><button className="link-button" type="button" onClick={() => navigate("test-case", item.test_case_code)}>{item.test_case_code}</button></td><td>{item.asset_code}</td><td><StatusMark tone={item.status === "RUNNING" ? "normal" : item.status === "FAILED" || item.status === "BLOCKED" ? "danger" : "muted"} label={item.status} /></td><td>{formatDate(item.started_at)}<small>{formatDate(item.ended_at)}</small></td><td>{formatDuration(item.closed_duration_seconds)}</td><td>{formatDuration(item.active_elapsed_seconds)}</td><td>{item.data_quality} / {item.clock_quality}</td></tr>)}</tbody></table></div>;
}

export function TestCaseDetailPage({ id, navigate }: { id: string; navigate: Navigate }) {
  const query = useInfiniteQuery({
    queryKey: paginationKey("test-case", id),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) => getTestCase(id, pageParam ?? undefined, signal),
    getNextPageParam: (lastPage) => lastPage.executions.page.next_cursor
  });
  if (query.isLoading) return <LoadingState />;
  if (!query.data) return <ErrorState error={query.error} retry={() => query.refetch()} />;
  const data = query.data.pages[0];
  const executionItems = mergeUniquePages(
    query.data.pages.map((page) => page.executions),
    (item) => item.id
  );
  const mtbf = data.mtbf_observation;
  return (
    <>
      <BackButton onClick={() => returnToPreviousPage(navigate)} label="返回上一页" />
      <PageTitle eyebrow="测试用例" title={`${data.code} · ${data.name}`} description={`${data.asset_kind === "MODULE" ? "模块" : "整机"} · 适用部位：${data.target_part_name ?? "未绑定"} · ${data.domain} · 版本 ${data.latest_version ?? "未发布"}`} trailing={<div className="title-status">{isShowcaseValue(data.code) && <ShowcaseBadge />}<Cutoff value={data.data_cutoff_at} asOf={data.as_of_at} /></div>} />
      <DurationBand totals={data.durations} />
      <div className="detail-columns">
        <Panel title="状态统计"><dl className="fact-grid">{Object.entries(data.status_counts).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}</dl></Panel>
        <Panel title="MTBF 观察口径">
          {!mtbf ? <EmptyState title="尚无观察结果" description="当前范围尚无可用于 MTBF 计算的数据。" /> : <dl className="fact-grid"><div><dt>有效 T</dt><dd>{formatDuration(mtbf.effective_exposure_seconds)}</dd></div><div><dt>相关故障 r</dt><dd>{mtbf.relevant_failure_count}</dd></div><div><dt>点估计</dt><dd>{mtbf.point_estimate_hours === null ? "未形成有限点估计" : `${mtbf.point_estimate_hours.toFixed(2)} h`}</dd></div><div><dt>90% 单侧下界</dt><dd>{mtbf.lower_bounds_hours["0.9"] === undefined ? "尚无" : `${mtbf.lower_bounds_hours["0.9"].toFixed(2)} h`}</dd></div></dl>}
        </Panel>
      </div>
      <Panel title="执行记录">
        <ExecutionTable items={executionItems} navigate={navigate} />
        {executionItems.length > 0 && <LoadMore hasMore={Boolean(query.hasNextPage)} loading={query.isFetchingNextPage} failed={query.isFetchNextPageError} onLoad={() => void query.fetchNextPage()} />}
      </Panel>
    </>
  );
}

export function ExecutionDetailPage({ id, navigate }: { id: string; navigate: Navigate }) {
  const query = useQuery({ queryKey: ["execution", id], queryFn: ({ signal }) => getExecution(id, signal) });
  if (query.isLoading) return <LoadingState />;
  if (query.isError || !query.data) return <ErrorState error={query.error} retry={() => query.refetch()} />;
  const data = query.data;
  const execution = data.execution;
  return (
    <>
      <BackButton onClick={() => returnToPreviousPage(navigate)} label="返回上一页" />
      <PageTitle eyebrow="执行记录" title={execution.code} description={`${execution.test_case_code} · ${execution.asset_code} · ${execution.campaign_code}`} trailing={<div className="title-status">{isShowcaseValue(execution.campaign_code) && <ShowcaseBadge />}<StatusMark tone={execution.status === "RUNNING" ? "normal" : execution.status === "FAILED" || execution.status === "BLOCKED" ? "danger" : "muted"} label={execution.status} /></div>} />
      <button type="button" className="archive-curve-link" onClick={() => navigate("curves", execution.id)}>打开归档详细曲线</button>
      <section className="metric-strip">
        <div><span>闭合时长</span><strong>{formatDuration(execution.closed_duration_seconds)}</strong></div>
        <div><span>运行中时长</span><strong>{formatDuration(execution.active_elapsed_seconds)}</strong></div>
        {data.result && <div><span>测试结论</span><strong>{executionOutcomeNames[data.result.outcome]}</strong></div>}
        {data.result && <div><span>结束方式</span><strong>{terminationNames[data.result.termination_kind]}</strong></div>}
        {data.peak_summary.length === 0 && <div><span>峰值摘要</span><strong>暂无指标观测</strong></div>}
        {data.peak_summary.slice(0, 4).map((metric) => <div key={metric.metric_code}><span>{metric.metric_name}</span><strong>{metric.value.toLocaleString("zh-CN")} {metric.unit}</strong></div>)}
      </section>
      {data.result && <ExecutionResultSummary result={data.result} />}
      <JointRecordPanel records={data.joint_records ?? []} />
      <JointAnalysisPanel executionId={id} />
      <Panel title="执行元数据"><dl className="fact-grid"><div><dt>测试用例</dt><dd><button className="link-button" type="button" onClick={() => navigate("test-case", execution.test_case_code)}>{execution.test_case_code}</button></dd></div><div><dt>样品</dt><dd><button className="link-button" type="button" onClick={() => navigate("asset", execution.asset_code)}>{execution.asset_code}</button></dd></div><div><dt>台架</dt><dd>{data.station_code ?? "未记录"} · {data.station_name ?? "—"}</dd></div><div><dt>执行人</dt><dd>{data.operator_name ?? data.result?.executor_display ?? "未记录"}</dd></div><div><dt>数据源</dt><dd>{data.source_code ?? "未记录"}</dd></div><div><dt>配置指纹</dt><dd>{data.configuration.fingerprint ?? "未记录"}</dd></div></dl></Panel>
      <Panel title="逐关节 / 指标表">
        {data.metrics.length === 0 ? <EmptyState title="尚无指标观测" description="该执行记录暂无关联指标数据。" /> : <div className="table-scroll"><table className="data-table"><thead><tr><th>关节</th><th>指标</th><th>值</th><th>时间</th><th>质量</th><th>正式证据资格</th></tr></thead><tbody>{data.metrics.map((metric) => <tr key={metric.id}><td>{metric.joint ?? "全局"}</td><td>{metric.metric_code}<small>{metric.metric_name}</small></td><td>{metric.value ?? metric.text_value ?? "缺失"} {metric.unit}</td><td>{formatDate(metric.observed_at)}</td><td>{metric.quality_status}</td><td>{metric.formal_eligible ? "具备" : "不具备"}</td></tr>)}</tbody></table></div>}
      </Panel>
      <div className="detail-columns">
        <Panel title="数据质量"><pre className="json-block">{JSON.stringify(data.data_quality_summary, null, 2)}</pre></Panel>
        <Panel title="证据摘要">{data.evidence.length === 0 ? <EmptyState title="尚无证据文件" description="该执行记录尚未关联证据文件。" /> : <ul className="evidence-list">{data.evidence.map((item) => <li key={item.id}><strong>{item.file_name}</strong><span>{item.kind} · {item.availability_status}</span></li>)}</ul>}</Panel>
      </div>
      <Cutoff value={data.data_cutoff_at} asOf={data.as_of_at} />
    </>
  );
}

const executionOutcomeNames: Record<ExecutionResult["outcome"], string> = {
  PASSED: "通过",
  FAILED: "失败",
  INCONCLUSIVE: "待判定",
  NOT_EVALUATED: "未评估"
};

const terminationNames: Record<ExecutionResult["termination_kind"], string> = {
  NORMAL: "正常结束",
  MANUAL_STOP: "人工停止",
  SAFETY_WATCHDOG: "安全监控终止",
  DATA_TIMEOUT: "数据超时",
  SYSTEM_CRASH: "系统异常",
  SCHEDULED: "待执行",
  RUNNING: "执行中",
  UNKNOWN: "原因待确认"
};

function importedNarrative(value: string) {
  return value.replace(/\*\*/g, "").replace(/^- /gm, "• ");
}

function ExecutionResultSummary({ result }: { result: ExecutionResult }) {
  const outcomeTone = result.outcome === "PASSED" ? "normal" : result.outcome === "FAILED" ? "danger" : result.outcome === "INCONCLUSIVE" ? "warning" : "muted";
  return (
    <Panel className="execution-result-panel" title="执行结论">
      <div className="execution-result-heading">
        <StatusMark tone={outcomeTone} label={executionOutcomeNames[result.outcome]} />
        <span>{terminationNames[result.termination_kind]}</span>
      </div>
      {result.summary && <p className="execution-result-summary">{importedNarrative(result.summary)}</p>}
      {result.issues && <div className="execution-result-issue"><span>问题记录</span><strong>{importedNarrative(result.issues)}</strong></div>}
      <dl className="fact-grid execution-result-facts">
        <div><dt>源记录状态</dt><dd>{result.source_status}</dd></div>
        <div><dt>源记录时长</dt><dd>{formatDuration(result.reported_duration_seconds)}</dd></div>
        <div><dt>最后数据时间</dt><dd>{result.last_data_at ? formatDate(result.last_data_at) : "未记录"}</dd></div>
        <div><dt>异常计数</dt><dd>{result.exception_count}</dd></div>
        <div><dt>申报完成循环</dt><dd>{result.completed_cycles ?? "未记录"}</dd></div>
        <div><dt>环境</dt><dd>{result.environment_label || "未记录"}</dd></div>
        <div><dt>遥测来源</dt><dd>{result.telemetry_source || "未记录"}</dd></div>
        <div><dt>归档状态</dt><dd>{result.archive_status}</dd></div>
        <div><dt>报告引用</dt><dd>{result.report_reference || "未记录"}</dd></div>
      </dl>
      {result.normalization_flags.length > 0 && (
        <details className="normalization-details">
          <summary>源数据规范化记录 · {result.normalization_flags.length} 项</summary>
          <ul>
            {result.normalization_flags.map((flag, index) => (
              <li key={`${String(flag.code ?? "normalization")}-${index}`}>
                <strong>{String(flag.code ?? "normalization")}</strong>
                {flag.source_value !== undefined && <span>{String(flag.source_value)} → {String(flag.normalized_value ?? "按规范修正")}</span>}
              </li>
            ))}
          </ul>
        </details>
      )}
    </Panel>
  );
}
