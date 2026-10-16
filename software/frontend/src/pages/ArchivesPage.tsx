import { useRef, useState, type FormEvent } from "react";
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ChartNoAxesCombined, FileCheck2, FileUp, Search } from "lucide-react";
import { archivesApi, archiveTitle, archiveStation, missingArchiveAcquisitionFacts, archiveProcessingLabels, archiveUploadLabels, verdictLabels, type Archive, type ArchiveContext, type ArchiveVerdict } from "../archiveApi";
import { useAuth } from "../auth";
import { setupApi } from "../setupApi";
import { formatDate, formatDuration } from "../navigation";
import { EmptyState, ErrorState, LoadingState, PageTitle, Panel } from "../components/Workbench";
import { StatusMark } from "../components/StatusMark";

type Navigate = (view: "import" | "archives" | "archive" | "curves" | "execution" | "submissions", id?: string) => void;
const machineLabels: Record<string, string> = { completed: "执行完成", failed: "故障结束", stopped: "人工停止", interrupted: "执行中断", unknown: "未知" };
const displayValue = (value: unknown) => value === null || value === undefined ? "未记录" : String(value);
function localDateTimeInput(value: string | null | undefined) {
  if (!value) return "";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "";
  const pad = (part: number, length = 2) => String(part).padStart(length, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}.${pad(date.getMilliseconds(), 3)}`;
}

export function ArchiveStatus({ item }: { item: Archive }) {
  return <div className="archive-statuses"><StatusMark tone={item.upload_state === "CORRUPT" ? "danger" : item.upload_state === "COMPLETE" ? "normal" : "warning"} label={archiveUploadLabels[item.upload_state]} /><StatusMark tone={item.processing_state === "FAILED" ? "danger" : item.processing_state === "READY" ? "normal" : "muted"} label={archiveProcessingLabels[item.processing_state]} /><StatusMark tone={item.classification_state === "UNCLASSIFIED" ? "warning" : "muted"} label={item.classification_state === "UNCLASSIFIED" ? "待归类" : "已归类"} /><StatusMark tone={item.submission_status === "APPROVED" ? "normal" : item.submission_status === "REJECTED" ? "danger" : "muted"} label={item.submission_status === "APPROVED" ? "已批准" : item.submission_status === "REJECTED" ? "已驳回" : item.submission_status === "PENDING" ? "待审核" : "未送审"} /></div>;
}

const analysisLabels: Record<string, string> = { active_seconds: "运行秒数", completed_cycles: "完成循环", exception_count: "异常次数", row_count: "采样行数", sample_count: "采样点数", max_gap_seconds: "最大采样间隔 / s", completeness: "采样完整率", sample_completeness: "采样完整率", criteria_version: "判据版本", verdict: "分析结论", automatic_verdict: "自动判定", checks: "规则检查", subjects: "对象分析", gaps: "缺失段", metrics: "指标统计", min: "最小值", max: "最大值", mean: "均值", peak_abs: "绝对峰值", rms: "均方根", temperature_rise_c: "温升 / °C", tracking_rms_rad: "跟踪误差 RMS / rad", start: "起点", end: "终点", unit: "单位", key: "参数", status: "状态", configured: "已配置", passed: "通过", failed: "失败", unconfigured: "未配置" };
export function AnalysisValue({ value, label }: { value: unknown; label?: string }) {
  if (value === null || value === undefined) return <span>未配置</span>;
  if (Array.isArray(value)) return <ul className="archive-analysis-list">{value.map((item, index) => <li key={index}><AnalysisValue value={item} /></li>)}</ul>;
  if (typeof value === "object") return <dl className="archive-analysis-grid">{Object.entries(value).map(([key, entry]) => <div key={key}><dt>{analysisLabels[key] ?? key}</dt><dd><AnalysisValue value={entry} label={key} /></dd></div>)}</dl>;
  if (typeof value === "boolean") return <span>{value ? "是" : "否"}</span>;
  if (typeof value === "number") return <span>{value.toLocaleString("zh-CN", { maximumFractionDigits: 6 })}</span>;
  return <span>{label === "verdict" || label === "automatic_verdict" || label === "status" ? analysisLabels[String(value)] ?? String(value) : String(value)}</span>;
}

export function ArchiveEvidence({ item }: { item: Archive }) {
  return <div className="archive-evidence">
    <Panel title="采集事实" eyebrow="ACQUISITION FACTS" description="测得数据与机器终态在归档后冻结。">
      <dl className="fact-grid"><div><dt>执行是否完成</dt><dd>{machineLabels[String(item.facts.machine_status)] ?? displayValue(item.facts.machine_status)}</dd></div><div><dt>自动判定</dt><dd>{item.facts.automatic_verdict === "unconfigured" ? "未配置" : verdictLabels[item.facts.automatic_verdict as ArchiveVerdict] ?? "未记录"}</dd></div><div><dt>运行时长</dt><dd>{formatDuration(typeof item.facts.active_seconds === "number" ? item.facts.active_seconds : null)}</dd></div><div><dt>循环 / 异常</dt><dd>{displayValue(item.facts.completed_cycles)} / {displayValue(item.facts.exception_count)}</dd></div><div><dt>采集开始</dt><dd>{formatDate(item.facts.started_at as string | null)}</dd></div><div><dt>采集结束</dt><dd>{formatDate(item.facts.ended_at as string | null)}</dd></div><div><dt>数据 / 时钟质量</dt><dd>{displayValue(item.facts.data_quality)} / {displayValue(item.facts.clock_quality)}</dd></div></dl>
    </Panel>
    <Panel title="自动结果分析" eyebrow="REPRODUCIBLE ANALYSIS" description="分析与归档判据一起保存；未配置的判据显示为未配置。">{item.manifest?.analysis ? <AnalysisValue value={item.manifest.analysis} /> : <p className="archive-muted">{item.processing_state === "FAILED" ? "解析失败，尚未生成分析。" : "等待归档解析完成。"}</p>}<details className="archive-rules"><summary>查看本次归档使用的判据</summary><AnalysisValue value={item.acceptance} /></details></Panel>
    <Panel title="人工核实与测试员记录" eyebrow="VERIFICATION & NOTES">{item.current_verification ? <><dl className="fact-grid"><div><dt>实际测试人</dt><dd>{item.current_verification.tester}</dd></div><div><dt>人工核实结论</dt><dd>{verdictLabels[item.current_verification.verdict]}</dd></div><div><dt>核实时间 / 版本</dt><dd>{formatDate(item.current_verification.created_at)} · 第 {item.current_verification.revision} 版</dd></div><div><dt>与自动结论差异的原因</dt><dd>{item.current_verification.discrepancy_reason || "无"}</dd></div>{item.current_verification.declared_started_at && <div><dt>历史人工补录时间</dt><dd>{formatDate(item.current_verification.declared_started_at)} 至 {formatDate(item.current_verification.declared_ended_at)}</dd></div>}</dl><p className="archive-notes">{item.current_verification.notes}</p></> : <p className="archive-muted">尚未保存核实记录。</p>}</Panel>
  </div>;
}

function VerificationForm({ item, onSaved }: { item: Archive; onSaved: () => Promise<void> }) {
  const options = useQuery({ queryKey: ["setup", "options"], queryFn: ({ signal }) => setupApi.options(signal) });
  const [tester, setTester] = useState(item.current_verification?.tester ?? "");
  const [station, setStation] = useState(item.current_verification?.station_id ?? item.classification?.station_id ?? "");
  const [verdict, setVerdict] = useState<ArchiveVerdict | "">(item.current_verification?.verdict ?? "");
  const [notes, setNotes] = useState(item.current_verification?.notes ?? "");
  const [discrepancy, setDiscrepancy] = useState(item.current_verification?.discrepancy_reason ?? "");
  const [started, setStarted] = useState(() => localDateTimeInput(item.current_verification?.declared_started_at));
  const [ended, setEnded] = useState(() => localDateTimeInput(item.current_verification?.declared_ended_at));
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const receipt = useRef({ signature: "", key: "" });
  const different = verdict !== "" && verdict !== (item.manifest?.analysis?.verdict ?? item.facts.automatic_verdict);
  async function save(event: FormEvent) {
    event.preventDefault(); if (!verdict || busy) return;
    if ((started || ended) && (!started || !ended || new Date(started) >= new Date(ended))) { setError(new Error("人工补录开始与结束时间必须同时填写，且结束晚于开始。")); return; }
    const payload = { expected_revision: item.revision, tester: tester.trim(), station_id: station, verdict, notes: notes.trim(), discrepancy_reason: discrepancy.trim(), reason: reason.trim(), ...(started ? { declared_started_at: new Date(started).toISOString(), declared_ended_at: new Date(ended).toISOString() } : {}) };
    const signature = JSON.stringify(payload); if (receipt.current.signature !== signature) receipt.current = { signature, key: crypto.randomUUID() };
    setBusy(true); setError(null);
    try { await archivesApi.verify(item.id, payload, receipt.current.key); await onSaved(); } catch (failure) { setError(failure); } finally { setBusy(false); }
  }
  return <Panel title={item.current_verification ? "追加核实版本" : "保存上传数据核实"} description="核实测试人、工位和人工结论，补充本次备注。">
    <form className="archive-form" onSubmit={(event) => void save(event)}><div className="archive-form-grid">
      <label>实际测试人<input required maxLength={100} value={tester} onChange={(event) => setTester(event.target.value)} /></label>
      <label>核实工位<select required value={station} onChange={(event) => setStation(event.target.value)}><option value="">选择有效工位</option>{options.data?.stations.map((entry) => <option key={entry.id} value={entry.id}>{entry.code} · {entry.name}</option>)}</select></label>
      <label>人工核实结论<select required value={verdict} onChange={(event) => setVerdict(event.target.value as ArchiveVerdict | "")}><option value="">请选择结论</option><option value="passed">通过</option><option value="failed">失败</option><option value="blocked">待判定 / 阻塞</option></select></label>
      {different && <label>与自动结论不同的原因<textarea required maxLength={1000} value={discrepancy} onChange={(event) => setDiscrepancy(event.target.value)} /></label>}
      <label className="archive-form-wide">测试员记录<textarea required maxLength={2000} value={notes} onChange={(event) => setNotes(event.target.value)} placeholder="记录本次相关备注，无补充事项填写“无”" /></label>
      {item.origin === "HISTORY" && !item.facts.started_at && <><label>人工补录开始时间<input type="datetime-local" step="0.001" value={started} onChange={(event) => setStarted(event.target.value)} /></label><label>人工补录结束时间<input type="datetime-local" step="0.001" value={ended} onChange={(event) => setEnded(event.target.value)} /></label><p className="archive-form-wide archive-muted">原文件缺少采集日期；补录时间单独留痕，原始采集时间保持缺失。已有补录时间会带入新核实版本，修改需填写核实说明。</p></>}
      <label className="archive-form-wide">核实说明<input required maxLength={300} value={reason} onChange={(event) => setReason(event.target.value)} placeholder="说明此次核实或修改的原因" /></label>
    </div>{options.error && <ErrorState error={options.error} retry={() => void options.refetch()} />}<button type="submit" disabled={busy || options.isLoading || !tester.trim() || !station || !verdict || !notes.trim() || !reason.trim() || different && !discrepancy.trim()}>{busy ? "正在保存…" : "保存核实版本"}</button>{error != null && <ErrorState error={error} retry={() => setError(null)} />}</form>
  </Panel>;
}

function ClassificationForm({ item, onSaved }: { item: Archive; onSaved: () => Promise<void> }) {
  const campaigns = useQuery({ queryKey: ["setup", "campaigns"], queryFn: ({ signal }) => setupApi.campaigns(signal) });
  const options = useQuery({ queryKey: ["setup", "options"], queryFn: ({ signal }) => setupApi.options(signal) });
  const [campaign, setCampaign] = useState(item.classification?.campaign_id ?? "");
  const [contextId, setContextId] = useState("");
  const [reason, setReason] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const receipt = useRef({ signature: "", key: "" });
  const contexts = useQuery({ queryKey: ["setup", "contexts", campaign], queryFn: ({ signal }) => setupApi.contexts(campaign, signal), enabled: Boolean(campaign) });
  const context = contexts.data?.find((entry) => entry.id === contextId) ?? (!contextId && item.classification ? contexts.data?.find((entry) =>
    ["campaign_id", "cycle_id", "segment_id", "asset_id", "configuration_id", "test_case_version_id"].every((key) => entry.context[key] === item.classification?.[key as keyof ArchiveContext])) : undefined);
  const stationMismatch = Boolean(context && item.current_verification && context.context.station_id !== item.current_verification.station_id);
  const plannedStation = options.data?.stations.find((entry) => entry.id === context?.context.station_id);
  const plannedStationLabel = plannedStation ? `${plannedStation.code} · ${plannedStation.name}` : context?.context.station_id;
  const actualStationLabel = item.current_verification?.station_code ? `${item.current_verification.station_code}${item.current_verification.station_name ? ` · ${item.current_verification.station_name}` : ""}` : item.current_verification?.station_id;
  async function save(event: FormEvent) {
    event.preventDefault(); if (!context || !item.current_verification || busy || !reason.trim()) return;
    const payload = { expected_revision: item.revision, context: { ...context.context, station_id: item.current_verification.station_id } as ArchiveContext, reason: reason.trim() };
    const signature = JSON.stringify(payload); if (receipt.current.signature !== signature) receipt.current = { signature, key: crypto.randomUUID() };
    setBusy(true); setError(null);
    try { await archivesApi.classify(item.id, payload, receipt.current.key); await onSaved(); } catch (failure) { setError(failure); } finally { setBusy(false); }
  }
  return <Panel title={item.classification_state === "CLASSIFIED" ? "追加正式归属修正" : "归入正式测试用例"} description="选择已有计划中的正式用例与样品，使用已核实的实际工位归类。保存追加版本，历史归属、判据和结论继续保留。"><form className="archive-form" onSubmit={(event) => void save(event)}><div className="archive-form-grid">
    <label>测试计划<select required value={campaign} onChange={(event) => { setCampaign(event.target.value); setContextId(""); }}><option value="">选择计划</option>{campaigns.data?.map((entry) => <option key={entry.id} value={entry.id}>{entry.code} · {entry.name}</option>)}</select></label>
    <label>用例与样品<select required value={context?.id ?? ""} disabled={!campaign || contexts.isLoading} onChange={(event) => setContextId(event.target.value)}><option value="">选择已发布用例的计划上下文</option>{contexts.data?.filter((entry) => entry.status !== "CANCELLED").map((entry) => <option key={entry.id} value={entry.id}>{entry.case_code} · {entry.case_name} · {entry.version} · {entry.asset_code}</option>)}</select></label>
    <label className="archive-form-wide">归类说明<input required maxLength={300} value={reason} onChange={(event) => setReason(event.target.value)} placeholder={stationMismatch ? "请说明计划工位与实际工位不同的原因" : "说明本次归类或归属修正的原因"} /></label>
  </div>{stationMismatch && <p role="status" className="archive-muted">计划工位：{plannedStationLabel}；已核实实际工位：{actualStationLabel}。本次归类采用实际工位，请在归类说明中记录差异原因；原计划上下文保留。</p>}{campaigns.error && <ErrorState error={campaigns.error} retry={() => void campaigns.refetch()} />}{contexts.error && <ErrorState error={contexts.error} retry={() => void contexts.refetch()} />}<button type="submit" disabled={busy || !context || !reason.trim() || !item.current_verification}>{busy ? "正在归类…" : item.classification_state === "CLASSIFIED" ? "保存归属修正版本" : "保存正式归属"}</button>{!item.current_verification && <p className="archive-muted">请先保存核实记录。</p>}{error != null && <ErrorState error={error} retry={() => setError(null)} />}</form></Panel>;
}

export function ArchiveDetailPage({ id, navigate }: { id: string; navigate: Navigate }) {
  const { session } = useAuth();
  const cache = useQueryClient();
  const query = useQuery({ queryKey: ["archives", "detail", id], queryFn: ({ signal }) => archivesApi.detail(id, signal), refetchInterval: (entry) => ["READY", "FAILED"].includes(entry.state.data?.processing_state ?? "") ? false : 5000 });
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState("");
  const receipt = useRef({ signature: "", key: "" });
  async function saved() { await cache.invalidateQueries({ queryKey: ["archives"] }); }
  async function submit() {
    const item = query.data; if (!item || busy) return;
    const payload = { expected_revision: item.revision, expected_submission_revision: item.submission_revision ?? 0, reason: reason.trim() };
    const signature = JSON.stringify(payload); if (receipt.current.signature !== signature) receipt.current = { signature, key: crypto.randomUUID() };
    setBusy(true); setError(null); setNotice("");
    try { await archivesApi.submit(id, payload, receipt.current.key); setNotice("已送审。请在测试数据审批页面核对并审核。"); await saved(); await cache.invalidateQueries({ queryKey: ["submissions"] }); } catch (failure) { setError(failure); } finally { setBusy(false); }
  }
  if (query.isLoading) return <LoadingState label="正在加载归档记录" />;
  if (query.error || !query.data) return <ErrorState error={query.error} retry={() => void query.refetch()} />;
  const item = query.data;
  const editable = item.can_edit && item.submission_status !== "APPROVED" && item.submission_status !== "PENDING";
  const classificationStationMismatch = Boolean(item.current_verification && item.classification && item.current_verification.station_id !== item.classification.station_id);
  const missingMachineFacts = item.origin === "HISTORY" && missingArchiveAcquisitionFacts(item).length > 0;
  const canSubmit = !missingMachineFacts && editable && item.is_owner && item.processing_state === "READY" && item.upload_state === "COMPLETE" && item.classification_state === "CLASSIFIED" && Boolean(item.current_verification) && !classificationStationMismatch;
  return <div className="archives-page"><button type="button" className="back-button" onClick={() => navigate("archives")}><ArrowLeft aria-hidden="true" />返回数据归档</button><PageTitle eyebrow="TEST ARCHIVE" title={archiveTitle(item)} description={`${item.test_name} · ${item.actual_object} · ${archiveStation(item)}`} trailing={<button type="button" disabled={item.processing_state !== "READY"} onClick={() => navigate("curves", item.id)}><ChartNoAxesCombined aria-hidden="true" />详细曲线</button>} /><ArchiveStatus item={item} />
    {item.processing_error && <p role="alert" className="curve-error">解析失败：{item.processing_error}</p>}
    <ArchiveEvidence item={item} />
    {typeof item.can_analyze === "boolean" && item.processing_state === "READY" && <ReanalysisPanel item={item} />}
    {editable && item.processing_state === "READY" && <VerificationForm key={`verify:${item.revision}`} item={item} onSaved={saved} />}
    {editable && item.processing_state !== "READY" && <p className="archive-muted">文件归档校验和自动分析完成后，可以保存核实版本。</p>}
    {editable && <ClassificationForm key={`classify:${item.revision}`} item={item} onSaved={saved} />}
    {editable && classificationStationMismatch && <p role="status" className="archive-muted">核实工位与当前归类版本不同，请追加正式归属修正后重新送审。</p>}
    {editable && <Panel title={item.submission_status === "REJECTED" ? "核实后重新送审" : "提交正式审批"} description="归档、核实与正式归属齐全后可送审。审批通过后进入正式查询。"><div className="archive-submit-row"><label>提交说明<input maxLength={300} value={reason} onChange={(event) => setReason(event.target.value)} /></label><button type="button" disabled={!canSubmit || !reason.trim() || busy} onClick={() => void submit()}><FileCheck2 aria-hidden="true" />{busy ? "正在送审…" : "送审"}</button></div>{!canSubmit && <p className="archive-muted">{missingMachineFacts ? "原文件缺少运行秒数、循环数、异常数或明确机器终态，不能正式送审。可以保留归档、核实与曲线；人工补录日期不替代机器事实。" : !item.is_owner ? "管理员可以补齐核实与归类，请由原提交人送审。" : "请完成文件归档解析、人工核实及正式用例归类。"}</p>}{notice && <p role="status">{notice}</p>}{error != null && <ErrorState error={error} retry={() => void query.refetch()} />}</Panel>}
    {item.submission_id && session?.user.role !== "VIEWER" && <button type="button" className="link-button" onClick={() => navigate("submissions")}>查看测试数据审批</button>}{item.execution_id && <button type="button" className="link-button" onClick={() => navigate("execution", item.execution_id!)}>查看正式执行：{item.execution_code}</button>}
    <Panel title="文件与追溯"><dl className="fact-grid"><div><dt>原始 CSV</dt><dd>{item.filename}</dd></div><div><dt>归档大小</dt><dd>{(item.size_bytes / 1024 / 1024).toLocaleString("zh-CN", { maximumFractionDigits: 2 })} MiB</dd></div><div><dt>来源</dt><dd>{item.origin === "HISTORY" ? "历史补录" : item.origin === "OTHER" ? "其他测试" : "上位机测试"}</dd></div><div><dt>稳定执行标识</dt><dd>{item.execution_uuid}</dd></div><div><dt>归档编号</dt><dd>{item.archive_code}</dd></div><div><dt>原始文件校验</dt><dd className="archive-hash">{item.sha256}</dd></div></dl></Panel>
    {(item.verification_history?.length || item.classification_history?.length) ? <Panel title="核实与归类历史"><div className="archive-history">{item.verification_history?.map((entry) => <details key={`v:${entry.revision}`}><summary>核实第 {entry.revision} 版 · {formatDate(entry.created_at)} · {entry.actor_name ?? entry.payload.tester}</summary><p>{entry.reason}</p><dl className="fact-grid"><div><dt>实际测试人</dt><dd>{entry.payload.tester}</dd></div><div><dt>核实工位标识</dt><dd>{entry.payload.station_id}</dd></div><div><dt>人工结论</dt><dd>{verdictLabels[entry.payload.verdict]}</dd></div><div><dt>差异说明</dt><dd>{entry.payload.discrepancy_reason || "无"}</dd></div></dl><p className="archive-notes">{entry.payload.notes}</p></details>)}{item.classification_history?.map((entry) => <details key={`c:${entry.revision}`}><summary>归类第 {entry.revision} 版 · {formatDate(entry.created_at)}{entry.actor_name ? ` · ${entry.actor_name}` : ""}</summary><p>{entry.reason}</p><dl className="submission-evidence">{Object.entries(entry.payload).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}</dl></details>)}</div></Panel> : null}
  </div>;
}

function ReanalysisPanel({ item }: { item: Archive }) {
  const cache = useQueryClient();
  const reports = useQuery({ queryKey: ["archives", "analyses", item.id], queryFn: ({ signal }) => archivesApi.analyses(item.id, signal) });
  const catalog = useQuery({ queryKey: ["setup", "catalog"], queryFn: ({ signal }) => setupApi.catalog(signal), enabled: item.can_analyze === true });
  const [version, setVersion] = useState(item.classification?.test_case_version_id ?? "");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState("");
  const receipt = useRef({ signature: "", key: "" });
  async function append(event: FormEvent) {
    event.preventDefault();
    if (!version || !reason.trim() || busy) return;
    const payload = { test_case_version_id: version, reason: reason.trim() };
    const signature = JSON.stringify(payload);
    if (receipt.current.signature !== signature) receipt.current = { signature, key: crypto.randomUUID() };
    setBusy(true); setError(null); setNotice("");
    try {
      await archivesApi.analyze(item.id, payload, receipt.current.key);
      setNotice("已追加分析版本。原始报告与正式审批结果保持原有版本。");
      await cache.invalidateQueries({ queryKey: ["archives", "analyses", item.id] });
    } catch (failure) { setError(failure); } finally { setBusy(false); }
  }
  return <Panel title="按新用例判据重新分析" description="新分析用于回顾比较，另存来源与规则快照；不会覆盖原报告，也不会改变已批准结果或统计。">
    {item.can_analyze && <form className="archive-form archive-form-grid" onSubmit={(event) => void append(event)}>
      <label>分析使用的发布用例<select required value={version} disabled={catalog.isLoading || busy} onChange={(event) => setVersion(event.target.value)}><option value="">选择已发布用例版本</option>{catalog.data?.filter((entry) => entry.enabled && entry.version_id).map((entry) => <option key={entry.version_id} value={entry.version_id!}>{entry.code} · {entry.name} · {entry.version}</option>)}</select></label>
      <label>重新分析说明<input required maxLength={300} value={reason} onChange={(event) => setReason(event.target.value)} placeholder="说明为何使用这个用例版本进行回顾分析" /></label>
      <button type="submit" disabled={!version || !reason.trim() || busy || catalog.isLoading}>{busy ? "正在追加分析…" : "追加分析版本"}</button>
    </form>}
    {catalog.error && <ErrorState error={catalog.error} retry={() => void catalog.refetch()} />}
    {reports.isLoading && <LoadingState label="正在加载分析版本" />}
    {reports.error && <ErrorState error={reports.error} retry={() => void reports.refetch()} />}
    {reports.data?.items.length === 0 && <p className="archive-muted">尚未追加新判据分析。当前原始分析保留在上方。</p>}
    {reports.data?.items.map((entry) => <details className="archive-rules" key={entry.id}><summary>分析第 {entry.version} 版 · {entry.source_case_code} · {entry.source_version} · {formatDate(entry.created_at)}</summary><p>{entry.reason} · {entry.actor_name}</p><p className="archive-muted">{entry.mapping_notes}</p><AnalysisValue value={entry.report} /><details><summary>查看该版本判据快照</summary><AnalysisValue value={entry.acceptance} /></details></details>)}
    {notice && <p role="status">{notice}</p>}
    {error != null && <ErrorState error={error} retry={() => void reports.refetch()} />}
  </Panel>;
}

export default function ArchivesPage({ navigate }: { navigate: Navigate }) {
  const { session } = useAuth();
  const [draft, setDraft] = useState("");
  const [search, setSearch] = useState("");
  const [classification, setClassification] = useState("");
  const [origin, setOrigin] = useState("");
  const query = useInfiniteQuery({ queryKey: ["archives", "list", search, classification, origin], initialPageParam: null as number | null, queryFn: ({ pageParam, signal }) => archivesApi.list({ q: search, classification_state: classification, origin, before: pageParam }, signal), getNextPageParam: (page) => page.next_cursor });
  const items = query.data?.pages.flatMap((page) => page.items) ?? [];
  return <div className="archives-page"><PageTitle eyebrow="CENTRAL TEST ARCHIVE" title="数据归档" description="按执行编号查找原始记录，核实、归类并进入详细曲线分析。" trailing={session?.user.role !== "VIEWER" ? <button type="button" onClick={() => navigate("import")}><FileUp aria-hidden="true" />数据补录</button> : undefined} /><p className="archive-scope-note">{session?.user.role === "VIEWER" ? "显示你有计划权限的已批准记录。" : "可查看待审、驳回及待归类记录；这些记录在批准前不计入正式统计。"}</p>
    <form className="filter-bar archive-filter" role="search" onSubmit={(event) => { event.preventDefault(); setSearch(draft.trim()); }}><label>执行编号 / 测试名称<input value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="搜索执行编号、测试或样品" /></label><label>归属状态<select value={classification} onChange={(event) => setClassification(event.target.value)}><option value="">全部归属</option><option value="UNCLASSIFIED">待归类</option><option value="CLASSIFIED">已归类</option></select></label><label>记录来源<select value={origin} onChange={(event) => setOrigin(event.target.value)}><option value="">全部来源</option><option value="HMI">上位机</option><option value="OTHER">其他测试</option><option value="HISTORY">历史补录</option></select></label><button type="submit"><Search aria-hidden="true" />查询</button></form>
    {query.isLoading ? <LoadingState /> : query.error ? <ErrorState error={query.error} retry={() => void query.refetch()} /> : !items.length ? <EmptyState title="暂无符合条件的归档" description="上位机完成测试、核实并上传后，记录会出现在这里。可尝试清空搜索条件。" /> : <Panel title="归档记录" trailing={<span className="archive-muted">当前已加载 {items.length} 条</span>}><div className="table-scroll"><table className="data-table archive-table"><thead><tr><th>执行编号 / 测试</th><th>对象 / 工位</th><th>归档与审批</th><th>采集时间</th><th>操作</th></tr></thead><tbody>{items.map((item) => <tr key={item.id}><td><button type="button" className="link-button" onClick={() => navigate("archive", item.id)}>{archiveTitle(item)}</button><small>{item.test_name}</small></td><td>{item.actual_object}<small>{archiveStation(item)}</small></td><td><ArchiveStatus item={item} /></td><td>{formatDate(item.facts.started_at as string | null)}<small>{item.origin === "HISTORY" ? "历史补录" : "工位采集"}</small></td><td><button type="button" className="archive-curve-link" disabled={item.processing_state !== "READY"} onClick={() => navigate("curves", item.id)}><ChartNoAxesCombined aria-hidden="true" />曲线</button></td></tr>)}</tbody></table></div>{query.hasNextPage && <button type="button" disabled={query.isFetchingNextPage} onClick={() => void query.fetchNextPage()}>{query.isFetchingNextPage ? "加载中…" : "加载更多"}</button>}</Panel>}
  </div>;
}
