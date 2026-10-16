import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  CirclePause,
  CirclePlay,
  ExternalLink
} from "lucide-react";
import { getDashboardHome } from "../api";
import { formatDate, formatDuration, isShowcaseValue } from "../navigation";
import type { AssetKind, HomeScope, TargetPartCode } from "../types";
import {
  caseSlot,
  circularIndex,
  initialCaseIndex,
  recordedCases,
  shouldAutoAdvance,
  displayPartName,
  foldDegreeScopes,
  uniquePartScopes
} from "../homeCarousel";
import {
  EmptyState,
  ErrorState,
  FreshnessMark,
  LoadingState,
  PageTitle,
  Panel,
  ShowcaseBadge
} from "../components/Workbench";
import { StatusMark } from "../components/StatusMark";
import { getPartModelAsset } from "../modelAssets";

function useReducedMotion() {
  const [reducedMotion, setReducedMotion] = useState(
    () => window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );

  useEffect(() => {
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReducedMotion(media.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  return reducedMotion;
}

export function HomePage({
  assetKind,
  navigate
}: {
  assetKind: AssetKind;
  navigate: (view: "assets" | "test-case", id?: string, part?: TargetPartCode) => void;
}) {
  const query = useQuery({
    queryKey: ["dashboard-home"],
    queryFn: ({ signal }) => getDashboardHome(signal),
    refetchInterval: 30_000
  });
  const [index, setIndex] = useState(0);
  const [paused, setPaused] = useState(false);
  const [focusPaused, setFocusPaused] = useState(false);
  const [caseIndex, setCaseIndex] = useState(0);
  const [casePaused, setCasePaused] = useState(false);
  const [caseFocusPaused, setCaseFocusPaused] = useState(false);
  const [caseHoverPaused, setCaseHoverPaused] = useState(false);
  const [partHoverPaused, setPartHoverPaused] = useState(false);
  const [caseDirection, setCaseDirection] = useState<"previous" | "next">("next");
  const [hideZeroDuration, setHideZeroDuration] = useState(false);
  const reducedMotion = useReducedMotion();
  const regionRef = useRef<HTMLDivElement>(null);
  const objects = useMemo(
    () => foldDegreeScopes(uniquePartScopes(
      query.data?.groups.find((group) => group.asset_kind === assetKind)?.objects ?? []
    )),
    [assetKind, query.data]
  );
  const activeIndex = objects.length > 0 ? index % objects.length : 0;
  const current = objects[activeIndex] as HomeScope | undefined;
  const modelAsset = current ? getPartModelAsset(current.target_part_code) : null;
  const allCaseItems = useMemo(() => current?.test_cases ?? [], [current]);
  const caseItems = useMemo(
    () => hideZeroDuration ? recordedCases(allCaseItems) : allCaseItems,
    [allCaseItems, hideZeroDuration]
  );
  const scopeRecordFacts = useMemo(
    () => allCaseItems.reduce(
      (facts, item) => ({
        executionCount: facts.executionCount + item.execution_count,
        exceptionCount: facts.exceptionCount + (item.exception_count ?? 0),
        latestRecordedAt:
          !facts.latestRecordedAt ||
          (item.latest_recorded_at && item.latest_recorded_at > facts.latestRecordedAt)
            ? item.latest_recorded_at
            : facts.latestRecordedAt
      }),
      {
        executionCount: 0,
        exceptionCount: 0,
        latestRecordedAt: null as string | null
      }
    ),
    [allCaseItems]
  );
  const activeCaseIndex = caseIndex < caseItems.length ? caseIndex : 0;
  const activeCase = caseItems[activeCaseIndex];
  useEffect(() => {
    if (!shouldAutoAdvance({
      itemCount: objects.length,
      paused,
      focusPaused,
      hoverPaused: partHoverPaused || caseHoverPaused,
      reducedMotion
    })) {
      return;
    }
    const timer = window.setInterval(() => {
      setIndex((value) => (value + 1) % objects.length);
    }, 8000);
    return () => window.clearInterval(timer);
  }, [caseHoverPaused, focusPaused, objects.length, partHoverPaused, paused, reducedMotion]);

  useEffect(() => {
    setCaseDirection("next");
    setCaseIndex(initialCaseIndex(caseItems, null));
  }, [caseItems, current?.id]);

  useEffect(() => {
    if (!shouldAutoAdvance({
      itemCount: caseItems.length,
      paused: casePaused,
      focusPaused: caseFocusPaused,
      hoverPaused: caseHoverPaused || partHoverPaused,
      reducedMotion
    })) {
      return;
    }
    const timer = window.setInterval(() => {
      setCaseDirection("next");
      setCaseIndex((value) => circularIndex(value, 1, caseItems.length));
    }, 4500);
    return () => window.clearInterval(timer);
  }, [
    caseFocusPaused,
    caseHoverPaused,
    partHoverPaused,
    caseItems.length,
    casePaused,
    current?.id,
    reducedMotion
  ]);

  function move(step: number) {
    setIndex((value) => circularIndex(value, step, objects.length));
  }

  function moveCase(step: number) {
    setCaseDirection(step < 0 ? "previous" : "next");
    setCaseIndex((value) => circularIndex(value, step, caseItems.length));
  }

  if (query.isLoading) return <LoadingState label="正在加载总控首页" />;
  if (query.isError || !query.data) {
    return <ErrorState error={query.error} retry={() => query.refetch()} />;
  }
  const data = query.data;
  const isShowcase = data.data_sources.sources.some((source) => isShowcaseValue(source.code));

  return (
    <div className="home-page">
      <PageTitle
        eyebrow="RELIABILITY TEST CONTROL"
        title={`${assetKind === "MODULE" ? "模块部位" : "整机系统"}测试总控`}
        trailing={<div className="title-status">{isShowcase && <ShowcaseBadge />}<FreshnessMark freshness={data.freshness} /></div>}
      />

      <section className="metadata-strip" aria-label="数据状态摘要">
        <span><b>DATA CUTOFF</b> {data.freshness.status === "FRESH" ? "新鲜" : data.freshness.status === "STALE" ? "延迟" : "尚无数据"} · {data.freshness.data_cutoff_at ? formatDate(data.freshness.data_cutoff_at) : formatDate(data.freshness.as_of_at)}</span>
        <span><b>SOURCES</b> {data.data_sources.healthy_count}/{data.data_sources.sources.length} 健康 · {data.data_sources.warning_count} 需关注</span>
        <span className={data.anomaly_count ? "risk" : ""}>
          {data.anomaly_count > 0 && <AlertTriangle aria-hidden="true" />}
          <b>ANOMALIES</b> {data.anomaly_count} · {data.anomalies[0] ?? "未发现已记录异常"}
        </span>
      </section>
      <section className="source-ribbon" aria-label="数据源健康明细">
        <strong>数据源</strong>
        {data.data_sources.sources.length === 0 ? (
          <span>尚未配置 ingestion source</span>
        ) : data.data_sources.sources.map((source) => (
          <StatusMark
            key={source.id}
            tone={source.freshness_status === "FRESH" ? "normal" : source.freshness_status === "DISABLED" ? "muted" : "warning"}
            label={`${source.code} · ${source.freshness_status}`}
            shape={source.freshness_status === "FRESH" ? "circle" : "clock"}
          />
        ))}
      </section>

      <div className="home-workbench">
        <Panel
          eyebrow={assetKind === "MODULE" ? "MODULE PART SCOPES" : "WHOLE-MACHINE SCOPE"}
          title={`${assetKind === "MODULE" ? "模块部位" : "整机系统"}范围`}
          className="object-carousel"
          trailing={
            <div className="carousel-controls">
              <button type="button" onClick={() => move(-1)} disabled={objects.length < 2} aria-label="上一个测试对象">
                <ChevronLeft aria-hidden="true" />
              </button>
              <button type="button" onClick={() => setPaused((value) => !value)} aria-label={paused ? "继续自动轮播" : "暂停自动轮播"} aria-pressed={paused}>
                {paused ? <CirclePlay aria-hidden="true" /> : <CirclePause aria-hidden="true" />}
              </button>
              <button type="button" onClick={() => move(1)} disabled={objects.length < 2} aria-label="下一个测试对象">
                <ChevronRight aria-hidden="true" />
              </button>
            </div>
          }
        >
          {objects.length > 1 && (
            <div
              className="module-selector"
              role="group"
              aria-label="选择部位范围"
              onPointerEnter={() => setPartHoverPaused(true)}
              onPointerLeave={() => setPartHoverPaused(false)}
            >
              {objects.map((object, objectIndex) => {
                const active = objectIndex === activeIndex;
                return (
                  <button
                    key={object.id}
                    type="button"
                    aria-pressed={active}
                    data-scope-code={object.code}
                    data-target-part={object.target_part_code}
                    onClick={() => {
                      setIndex(objectIndex);
                      setPaused(true);
                    }}
                  >
                    <span>{object.target_part_name}</span>
                    <strong>{object.code} · {object.part_asset_count} 台</strong>
                  </button>
                );
              })}
            </div>
          )}
          {!current ? (
            <EmptyState
              title={`尚无可访问的${assetKind === "MODULE" ? "模块部位" : "整机系统"}范围`}
              description="数据库返回了空集合。请先完成样品接入或检查当前用户的 Campaign 授权。"
            />
          ) : (
            <div
              className="carousel-stage"
              ref={regionRef}
              tabIndex={0}
              onPointerEnter={() => setPartHoverPaused(true)}
              onPointerLeave={() => setPartHoverPaused(false)}
              aria-roledescription="走马灯"
              aria-label={`${assetKind === "MODULE" ? "模块部位" : "整机系统"}范围，${current.target_part_name}`}
              onFocus={() => setFocusPaused(true)}
              onBlur={(event) => {
                if (!event.currentTarget.contains(event.relatedTarget)) setFocusPaused(false);
              }}
              onKeyDown={(event) => {
                if (event.key === "ArrowLeft") {
                  event.preventDefault();
                  move(-1);
                } else if (event.key === "ArrowRight") {
                  event.preventDefault();
                  move(1);
                } else if (event.key === " ") {
                  event.preventDefault();
                  setPaused((value) => !value);
                }
              }}
            >
              <div className={`object-visual ${assetKind === "MODULE" ? "module-visual" : "whole-visual"}`}>
                <div className="stage-grid" aria-hidden="true" />
                <div className="stage-orbit orbit-one" aria-hidden="true" />
                <div className="stage-orbit orbit-two" aria-hidden="true" />
                <div className="asset-index">
                  <span>{assetKind === "MODULE" ? "MODULE PART SCOPE" : "SYSTEM SCOPE"} / {String(activeIndex + 1).padStart(2, "0")}</span>
                  <strong>{current.target_part_name} · {current.code}</strong>
                </div>
                {modelAsset ? (
                  <img
                    key={current.target_part_code}
                    className={`scope-hero-image ${assetKind === "WHOLE_MACHINE" ? "scope-hero-whole" : "scope-hero-module"}`}
                    src={modelAsset.poster}
                    alt={`${current.target_part_name}外观`}
                  />
                ) : assetKind === "WHOLE_MACHINE" ? (
                  <img
                    className="robot-asset"
                    src="/assets/humanoid-robot.webp"
                    width="512"
                    height="768"
                    alt={`${current.target_part_name}范围通用示意图`}
                  />
                ) : (
                  <>
                    <img
                      className="module-assembly-ghost"
                      src="/assets/humanoid-robot.webp"
                      width="512"
                      height="768"
                      alt=""
                      aria-hidden="true"
                    />
                    <div className="module-schematic" role="img" aria-label={`${current.name} 装配关系拓扑`}>
                      <span className="module-core"><i />{current.name}</span>
                      <span className="module-node node-control">控制器</span>
                      <span className="module-node node-load">负载端</span>
                      <span className="module-node node-sense">采集链</span>
                      <span className="module-axis axis-horizontal" aria-hidden="true" />
                      <span className="module-axis axis-vertical" aria-hidden="true" />
                    </div>
                  </>
                )}
                <div className="stage-metrics">
                  <div className="stage-callout callout-elapsed">
                    <span>运行中 elapsed</span>
                    <strong>{formatDuration(current.part_durations.active_elapsed_seconds)}</strong>
                  </div>
                  <div className="stage-callout callout-exposure">
                    <span>有效暴露</span>
                    <strong>{formatDuration(current.part_durations.effective_exposure_seconds)}</strong>
                  </div>
                </div>
                <div className="stage-floor" aria-hidden="true" />
              </div>
              <div className="object-console">
                <div className="object-identity">
                  <span>{String(activeIndex + 1).padStart(2, "0")} / {String(objects.length).padStart(2, "0")} · {assetKind}</span>
                  <h2>{current.target_part_name} / {current.code}</h2>
                  <StatusMark
                    tone={current.status === "RUNNING" ? "normal" : current.status === "BLOCKED" || current.status === "FAILED" ? "danger" : "muted"}
                    label={current.status}
                    shape={current.status === "RUNNING" ? "circle" : "square"}
                  />
                </div>
                <dl className="object-facts">
                  <div className="part-total-fact"><dt>部位累计总时长</dt><dd>{formatDuration(current.part_durations.total_duration_seconds)}</dd></div>
                  <div><dt>有效暴露</dt><dd>{formatDuration(current.part_durations.effective_exposure_seconds)}</dd></div>
                  <div><dt>排除</dt><dd>{formatDuration(current.part_durations.excluded_seconds)}</dd></div>
                  <div><dt>待确认</dt><dd>{formatDuration(current.part_durations.pending_seconds)}</dd></div>
                  <div className="record-time-fact"><dt>最近事实时间</dt><dd>{formatDate(scopeRecordFacts.latestRecordedAt)}</dd></div>
                  <div><dt>范围构成</dt><dd>{current.part_asset_count} 台样品 / {current.test_cases.length} 项用例</dd></div>
                  <div><dt>执行记录</dt><dd>{scopeRecordFacts.executionCount} 条</dd></div>
                  <div><dt>源记录异常</dt><dd>{scopeRecordFacts.exceptionCount} 次</dd></div>
                </dl>
                <div className="object-actions">
                  <button type="button" className="text-action" onClick={() => navigate("assets", undefined, current.target_part_code)}>
                    查看该部位样品 <ExternalLink aria-hidden="true" />
                  </button>
                </div>
              </div>
            </div>
          )}
        </Panel>

        <div
          className="case-rail-wrap"
          onPointerEnter={() => setCaseHoverPaused(true)}
          onPointerLeave={() => setCaseHoverPaused(false)}
          onFocusCapture={() => setCaseFocusPaused(true)}
          onBlurCapture={(event) => {
            if (!event.currentTarget.contains(event.relatedTarget)) setCaseFocusPaused(false);
          }}
        >
          <Panel
            eyebrow="TEST CASE NAVIGATION"
            title="当前部位用例"
            className="case-rail"
            trailing={
              <div className="case-rail-controls">
                <button
                  type="button"
                  className="case-zero-toggle"
                  aria-pressed={hideZeroDuration}
                  aria-label={hideZeroDuration ? "显示累计时长为 0 的用例" : "隐藏累计时长为 0 的用例"}
                  onClick={() => setHideZeroDuration((value) => !value)}
                >
                  0
                </button>
                {data.anomaly_count > 0 && <AlertTriangle aria-label="存在异常" />}
                <label className="case-jump">
                  用例
                  <select
                    aria-label="跳到用例"
                    value={activeCase?.code ?? ""}
                    disabled={caseItems.length === 0}
                    onChange={(event) => {
                      const next = caseItems.findIndex((item) => item.code === event.target.value);
                      if (next < 0) return;
                      setCaseDirection(next < activeCaseIndex ? "previous" : "next");
                      setCaseIndex(next);
                      setCasePaused(true);
                      setPaused(true);
                    }}
                  >
                    {caseItems.map((item) => <option key={item.id} value={item.code}>{item.code}</option>)}
                  </select>
                </label>
                <button type="button" onClick={() => moveCase(-1)} disabled={caseItems.length < 2} aria-label="上一个 Test Case">
                  <ChevronUp aria-hidden="true" />
                </button>
                <button
                  type="button"
                  onClick={() => setCasePaused((value) => !value)}
                  aria-label={casePaused ? "继续 Test Case 自动翻页" : "暂停 Test Case 自动翻页"}
                  aria-pressed={casePaused}
                >
                  {casePaused ? <CirclePlay aria-hidden="true" /> : <CirclePause aria-hidden="true" />}
                </button>
                <button type="button" onClick={() => moveCase(1)} disabled={caseItems.length < 2} aria-label="下一个 Test Case">
                  <ChevronDown aria-hidden="true" />
                </button>
              </div>
            }
          >
            {!current || caseItems.length === 0 ? (
              <EmptyState
                title={hideZeroDuration && allCaseItems.length > 0 ? "累计时长为 0 的用例已隐藏" : "该部位暂无已启用测试用例"}
                description={hideZeroDuration && allCaseItems.length > 0 ? "再按一次 0 可恢复这些用例。" : "当前部位没有已启用且已发布的测试用例。"}
              />
            ) : (
              <div
                className="case-flip-viewport"
                tabIndex={0}
                data-active-index={activeCaseIndex}
                data-active-scope={current.id}
                data-target-part={current.target_part_code}
                data-direction={caseDirection}
                data-reduced-motion={reducedMotion ? "true" : "false"}
                aria-roledescription="纵向机械翻页走马灯"
                aria-label={`${current.target_part_name} ${current.code} Test Case 导航`}
                onKeyDown={(event) => {
                  if (event.key === "ArrowUp") {
                    event.preventDefault();
                    moveCase(-1);
                  } else if (event.key === "ArrowDown") {
                    event.preventDefault();
                    moveCase(1);
                  } else if (event.key === " " && event.target === event.currentTarget) {
                    event.preventDefault();
                    setCasePaused((value) => !value);
                  }
                }}
              >
                <p className="visually-hidden" aria-live="polite" aria-atomic="true">
                  当前 Test Case：{activeCase?.code}，{activeCase?.name}
                </p>
                <ol className="case-flip-track">
                  {caseItems.map((item, itemIndex) => {
                    const slot = caseSlot(itemIndex, activeCaseIndex, caseItems.length);
                    const isCurrent = slot === "current";
                    const isHidden = slot === "hidden";
                    return (
                      <li
                        key={item.id}
                        className="case-flip-item"
                        data-slot={slot}
                        aria-hidden={isHidden ? "true" : undefined}
                      >
                        <button
                          type="button"
                          tabIndex={isHidden ? -1 : 0}
                          data-case-code={item.code}
                          aria-current={isCurrent ? "true" : undefined}
                          aria-label={isCurrent ? `打开 ${item.code} 详情` : `切换到 ${item.code}`}
                          onClick={() => {
                            if (isCurrent) {
                              navigate("test-case", item.code);
                            } else {
                              setCaseDirection(slot === "previous" ? "previous" : "next");
                              setCaseIndex(itemIndex);
                            }
                          }}
                        >
                          <span className="case-code">{item.code}</span>
                          <StatusMark
                            tone={item.status === "RUNNING" ? "normal" : item.status === "BLOCKED" || item.status === "FAILED" ? "danger" : "muted"}
                            label={item.status}
                            shape={item.status === "RUNNING" ? "circle" : "square"}
                          />
                          <strong>{item.name}</strong>
                          <span className="case-target-part">适用部位：{displayPartName(item.target_part_code, item.target_part_name)}</span>
                          <div className="case-primary-duration">
                            <span>部位累计总时长</span>
                            <strong>{formatDuration(item.cumulative_duration_seconds)}</strong>
                          </div>
                          <dl>
                            <div><dt>通过</dt><dd>{item.passed_execution_count ?? 0}</dd></div>
                            <div><dt>失败</dt><dd>{item.failed_execution_count ?? 0}</dd></div>
                            <div><dt>执行</dt><dd>{item.execution_count} 次</dd></div>
                            <div><dt>覆盖</dt><dd>{item.scope_asset_count} 台</dd></div>
                          </dl>
                          <span className="progress-label">
                            {item.execution_count === 0
                              ? "尚无执行事实"
                              : `最近 ${formatDate(item.latest_recorded_at)} · 台架 ${item.station_codes?.join(" / ") || "未记录"} · 异常 ${item.exception_count ?? 0}`}
                          </span>
                          {item.target_progress_percent !== null && item.target_progress_percent !== undefined && (
                            <progress max="100" value={Math.min(item.target_progress_percent, 100)} aria-label={`${item.code} 目标进度`} />
                          )}
                        </button>
                      </li>
                    );
                  })}
                </ol>
              </div>
            )}
          </Panel>
        </div>
      </div>
    </div>
  );
}
