import { useRef, useState } from "react";
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "../auth";
import { formatDate, formatDuration } from "../navigation";
import { submissionLabels, submissionsApi, type Submission } from "../submissionsApi";
import { archivesApi, verdictLabels } from "../archiveApi";
import { AnalysisValue } from "./ArchivesPage";
import { EmptyState, ErrorState, LoadingState, PageTitle, Panel } from "../components/Workbench";

function DecisionForm({ item, navigate, navigateArchive }: { item: Submission; navigate: (id: string) => void; navigateArchive?: (id: string) => void }) {
  const cache = useQueryClient();
  const latest = item.revisions![item.revisions!.length - 1];
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState("");
  const receipt = useRef({ signature: "", key: "" });
  const resubmit = item.is_owner && item.status === "REJECTED";
  const archive = useQuery({ queryKey: ["archives", "submission", item.execution_uuid], queryFn: ({ signal }) => archivesApi.list({ execution_uuid: item.execution_uuid }, signal), enabled: !item.archive_id && Boolean(navigateArchive) });
  const archiveId = item.archive_id ?? archive.data?.items[0]?.id;
  async function act(decision: "APPROVED" | "REJECTED") {
    if (busy) return;
    const signature = JSON.stringify({ id: item.id, revision: item.revision, decision, reason });
    if (receipt.current.signature !== signature) receipt.current = { signature, key: crypto.randomUUID() };
    setBusy(true); setError(null); setNotice("");
    try {
      await submissionsApi.review(item.id, item.revision, decision, reason, receipt.current.key);
      setNotice("审核结果已保存。");
    } catch (failure) { setError(failure); }
    finally { setBusy(false); await cache.invalidateQueries({ queryKey: ["submissions"] }); await cache.invalidateQueries({ queryKey: ["archives"] }); }
  }
  return <>
    <dl className="hmi-station-meta">
      <div><dt>提交人</dt><dd>{item.submitter_name}（{item.submitter}）</dd></div>
      <div><dt>计划</dt><dd>{item.campaign_code}</dd></div>
      <div><dt>样品</dt><dd>{item.asset_code ?? latest.summary.asset_id} · {item.asset_name}</dd></div>
      <div><dt>测试用例</dt><dd>{item.case_code ?? latest.summary.test_case_version_id} · {item.case_name} · {item.test_case_version}</dd></div>
      <div><dt>台架</dt><dd>{item.station_code ?? latest.summary.station_id}</dd></div>
      <div><dt>配置标识</dt><dd>{item.configuration_fingerprint ?? latest.summary.configuration_id}</dd></div>
      <div><dt>版本与状态</dt><dd>第 {item.revision} 版 · {submissionLabels[item.status]}</dd></div>
      <div><dt>申报运行时长</dt><dd>{formatDuration(latest.summary.active_seconds)}</dd></div>
      <div><dt>开始 / 结束</dt><dd>{formatDate(latest.summary.started_at)} / {formatDate(latest.summary.ended_at)}</dd></div>
      <div><dt>循环 / 异常次数</dt><dd>{latest.summary.completed_cycles} / {latest.summary.exception_count}</dd></div>
      <div><dt>{latest.verification ? "人工核实结论" : "原始摘要结论"}</dt><dd>{latest.verification ? verdictLabels[latest.verification.verdict] : { passed: "通过", failed: "失败", blocked: "阻塞" }[latest.summary.source_status]}</dd></div>
      <div><dt>数据 / 时钟质量</dt><dd>{latest.summary.data_quality} / {latest.summary.clock_quality}</dd></div>
    </dl>
    <p>{latest.summary.summary || "无补充摘要"}</p>
    {latest.analysis && <section className="submission-analysis"><h3>自动结果分析</h3><AnalysisValue value={latest.analysis} /></section>}
    {latest.verification && <section className="submission-analysis"><h3>人工核实</h3><dl className="fact-grid"><div><dt>实际测试人</dt><dd>{latest.verification.tester}</dd></div><div><dt>人工结论</dt><dd>{verdictLabels[latest.verification.verdict]}</dd></div><div><dt>差异说明</dt><dd>{latest.verification.discrepancy_reason || "无"}</dd></div></dl><h3>测试员记录</h3><p className="archive-notes">{latest.verification.notes}</p></section>}
    {archiveId && navigateArchive && <button type="button" className="link-button" onClick={() => navigateArchive(archiveId)}>查看原始归档、自动分析与人工核实</button>}
    <details><summary>上下文与本地证据校验摘要</summary><dl className="submission-evidence">{Object.entries(latest.summary).filter(([key]) => key.endsWith("_id") || key === "execution_uuid" || key === "evidence_sha256").map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{String(value)}</dd></div>)}</dl></details>
    {item.execution_id && <button type="button" className="link-button" onClick={() => navigate(item.execution_id!)}>查看正式执行：{item.execution_code}</button>}
    {resubmit && <div className="setup-section"><h3>核实后重新送审</h3><p>运行秒数、循环、异常次数及机器终态保持只读。请在归档核实页追加测试人、工位、人工结论和备注版本后送审。</p>{archive.isLoading ? <LoadingState label="正在查找原始归档" /> : archive.error ? <ErrorState error={archive.error} retry={() => void archive.refetch()} /> : archiveId && navigateArchive ? <button type="button" onClick={() => navigateArchive(archiveId)}>核实并重新送审</button> : <p>该历史摘要尚未关联原始归档，请通过上位机历史补录入口归档后再核实。</p>}</div>}
    {item.status === "PENDING" && item.can_review && <div className="setup-section">
      <label>审核意见<textarea maxLength={300} value={reason} onChange={(e) => setReason(e.target.value)} /></label>
      {reason.trim().length < 1 && <p>填写审核意见后即可批准或驳回。</p>}
      <div className="setup-tabs">
          <button type="button" disabled={busy || reason.trim().length < 1} onClick={() => void act("APPROVED")}>批准并发布正式记录</button>
          <button type="button" disabled={busy || reason.trim().length < 1} onClick={() => void act("REJECTED")}>驳回</button>
      </div>
    </div>}
    {notice && <p role="status">{notice}</p>}
    {error != null && <ErrorState error={error} retry={() => void cache.invalidateQueries({ queryKey: ["submissions"] })} />}
    <h3>提交与审批历史</h3>
    {item.revisions!.map((version) => <article key={version.revision}><strong>第 {version.revision} 版 · {version.decision ? submissionLabels[version.decision] : "待审核"}</strong><p>{formatDate(version.submitted_at)} · {version.submission_reason}</p><p>{version.reviewer ? `${version.reviewer} · ${formatDate(version.reviewed_at)} · ${version.review_reason}` : "尚无审核结果"}</p><details><summary>查看该版摘要</summary><pre>{JSON.stringify(version.summary, null, 2)}</pre></details></article>)}
  </>;
}

function SubmissionDetail({ id, navigate, navigateArchive }: { id: string; navigate: (id: string) => void; navigateArchive?: (id: string) => void }) {
  const query = useQuery({ queryKey: ["submissions", "detail", id], queryFn: ({ signal }) => submissionsApi.detail(id, signal), refetchInterval: 10000 });
  return <Panel title="测试摘要与审核">{query.isLoading ? <LoadingState /> : query.error ? <ErrorState error={query.error} retry={() => void query.refetch()} /> : query.data && <DecisionForm key={`${id}:${query.data.revision}:${query.data.status}`} item={query.data} navigate={navigate} navigateArchive={navigateArchive} />}</Panel>;
}

export default function SubmissionsPage({ navigate, navigateArchive }: { navigate: (id: string) => void; navigateArchive?: (id: string) => void }) {
  const { session } = useAuth();
  const allowed = session?.user.role !== "VIEWER";
  const [status, setStatus] = useState("PENDING");
  const [selected, setSelected] = useState("");
  const queue = useInfiniteQuery({ queryKey: ["submissions", "queue", status], initialPageParam: null as number | null,
    queryFn: ({ pageParam, signal }) => submissionsApi.list(status, pageParam, signal), getNextPageParam: (page) => page.next_cursor,
    enabled: allowed, refetchInterval: 10000 });
  if (!allowed) return <EmptyState title="无测试数据审批权限" description="查看者请在正式执行页面查看已发布数据。" />;
  const items = queue.data?.pages.flatMap((page) => page.items) ?? [];
  return <>
    <PageTitle eyebrow="TEST DATA APPROVAL" title="测试数据审批" description="执行员和管理员可以查看全部测试摘要，包括本人提交的记录，并直接审核。" />
    <p>原始 CSV 归档后可查看详细曲线与分析。待审与驳回数据不进入正式查询或统计；批准记录不会自动形成合格 MTBF 暴露。</p>
    <label>审批状态<select value={status} onChange={(e) => { setStatus(e.target.value); setSelected(""); }}><option value="PENDING">待审核</option><option value="APPROVED">已批准</option><option value="REJECTED">已驳回</option><option value="">全部</option></select></label>
    {queue.isLoading ? <LoadingState /> : queue.error ? <ErrorState error={queue.error} retry={() => void queue.refetch()} /> : !items.length ? <EmptyState title="暂无符合条件的提交" description="在上位机登录并选择平台上下文后，可提交已完成测试的摘要。" /> : <Panel title="提交列表"><div className="setup-plan-list">{items.map((item) => <button type="button" key={item.id} aria-pressed={item.id === selected} onClick={() => setSelected(item.id)}><strong>{item.campaign_code} · {item.submitter_name}</strong><span>第 {item.revision} 版 · {submissionLabels[item.status]} · {formatDate(item.updated_at)}</span></button>)}</div>{queue.hasNextPage && <button type="button" disabled={queue.isFetchingNextPage} onClick={() => void queue.fetchNextPage()}>加载更多</button>}</Panel>}
    {selected && <SubmissionDetail id={selected} navigate={navigate} navigateArchive={navigateArchive} />}
  </>;
}
