import { useEffect, useId, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, CheckCircle2, FileUp, UploadCloud, X, ChartNoAxesCombined, AlertCircle } from "lucide-react";
import { PageTitle, Panel, ErrorState } from "../components/Workbench";
import { useAuth } from "../auth";
import { setupApi, type SetupOptions } from "../setupApi";
import { archivesApi, archiveTitle, missingArchiveAcquisitionFacts, type Archive, type ArchiveContext, type ArchiveVerdict } from "../archiveApi";
import { type CsvPreflight } from "../imports/csvPreflight";
import { preflightCsv } from "../imports/preflightClient";
import { importApi, importRequestKey, uploadCsv } from "../imports/importApi";
import { ArchiveStatus } from "./ArchivesPage";
import "./csvImport.css";

type Navigate = (view: "archives" | "archive" | "curves", id?: string) => void;
type Metadata = { actualObject: string; station: string; tester: string; verdict: ArchiveVerdict | ""; notes: string; discrepancy: string };
const blank: Metadata = { actualObject: "", station: "", tester: "", verdict: "", notes: "", discrepancy: "" };
const message = (error: unknown) => error instanceof Error ? error.message : "操作失败，请重试。";
const number = (value: number | null) => value === null ? "未记录" : value.toLocaleString("zh-CN", { maximumFractionDigits: 6 });

function MetadataFields({ value, onChange, options, prefix, disabled = false, verification = false, requiredBasic = false, needsDiscrepancy = false }: { value: Metadata; onChange: (value: Metadata) => void; options?: SetupOptions; prefix: string; disabled?: boolean; verification?: boolean; requiredBasic?: boolean; needsDiscrepancy?: boolean }) {
  const update = (key: keyof Metadata, next: string) => onChange({ ...value, [key]: next });
  return <div className="import-form-grid">
    {!verification && <><label htmlFor={`${prefix}-object`}>样品 / 实际对象<input required={requiredBasic} id={`${prefix}-object`} list={`${prefix}-assets`} maxLength={200} disabled={disabled} value={value.actualObject} onChange={(event) => update("actualObject", event.target.value)} placeholder="选择已有样品或填写实际对象" /></label><datalist id={`${prefix}-assets`}>{options?.assets.map((asset) => <option key={asset.id} value={asset.code}>{asset.name}</option>)}</datalist>
    <label htmlFor={`${prefix}-station`}>有效工位<select required={requiredBasic} id={`${prefix}-station`} disabled={disabled} value={value.station} onChange={(event) => update("station", event.target.value)}><option value="">选择工位</option>{options?.stations.map((station) => <option key={station.id} value={station.id}>{station.code} · {station.name}</option>)}</select></label></>}
    <label htmlFor={`${prefix}-tester`}>实际测试人<input required={verification} id={`${prefix}-tester`} maxLength={100} disabled={disabled} value={value.tester} onChange={(event) => update("tester", event.target.value)} placeholder="填写实际操作人员" /></label>
    <label htmlFor={`${prefix}-verdict`}>人工结论<select required={verification} id={`${prefix}-verdict`} disabled={disabled} value={value.verdict} onChange={(event) => update("verdict", event.target.value)}><option value="">请选择，不自动判定</option><option value="passed">通过</option><option value="failed">失败</option><option value="blocked">待判定 / 阻塞</option></select></label>
    <label className="import-wide" htmlFor={`${prefix}-notes`}>测试员备注<textarea required={verification} id={`${prefix}-notes`} maxLength={2000} disabled={disabled} value={value.notes} onChange={(event) => update("notes", event.target.value)} placeholder="记录测试情况；无补充事项请明确填写“无”" /></label>
    {value.verdict && <label className="import-wide" htmlFor={`${prefix}-discrepancy`}>人工结论依据 / 与自动结论差异原因<textarea required={verification && needsDiscrepancy} id={`${prefix}-discrepancy`} maxLength={1000} disabled={disabled} value={value.discrepancy} onChange={(event) => update("discrepancy", event.target.value)} placeholder="未配置自动判据时，请说明人工判断依据" /></label>}
  </div>;
}

function PreflightDetails({ result }: { result: CsvPreflight }) {
  return <><dl className="import-facts"><div><dt>数据行</dt><dd>{number(result.rows)}</dd></div><div><dt>原始字段</dt><dd>{result.columns.length}</dd></div><div><dt>相对采样范围 / s</dt><dd>{number(result.start)} – {number(result.end)}</dd></div><div><dt>最大采样间隔 / s</dt><dd>{number(result.maxGap)}</dd></div></dl>
    {!!result.errors.length && <ul className="import-errors" role="alert">{result.errors.map((item) => <li key={item}>{item}</li>)}</ul>}
    {!!result.warnings.length && <details className="import-warnings" open><summary>数据完整性提示 · {result.warnings.length}</summary><ul>{result.warnings.map((item) => <li key={item}>{item}</li>)}</ul></details>}
    <details className="import-column-details"><summary>查看只读字段、单位、缺失与峰谷</summary><div className="table-scroll"><table className="data-table"><thead><tr><th>原始字段</th><th>单位</th><th>有效值</th><th>缺失</th><th>最小值</th><th>最大值</th></tr></thead><tbody>{result.columns.map((column) => <tr key={column.name}><td>{column.name}</td><td>{column.unit}</td><td>{number(column.count)}</td><td>{column.count === 0 && column.missing > 0 ? `全部缺失（${column.missing}）` : number(column.missing)}</td><td>{number(column.min)}</td><td>{number(column.max)}</td></tr>)}</tbody></table></div></details>
    <details className="import-hash"><summary>SHA-256 原文件校验</summary><code>{result.sha256}</code></details>
  </>;
}

function ImportFile({ file, initial, bulk, options, owner, navigate, remove }: { file: File; initial: Metadata; bulk: { value: Metadata; revision: number }; options?: SetupOptions; owner: string; navigate: Navigate; remove: () => void }) {
  const cache = useQueryClient();
  const fieldId = useId();
  const [meta, setMeta] = useState(initial);
  const [name, setName] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [preflight, setPreflight] = useState<CsvPreflight | null>(null);
  const [percent, setPercent] = useState(0);
  const [preflightError, setPreflightError] = useState("");
  const [archive, setArchive] = useState<Archive | null>(null);
  const [reused, setReused] = useState(false);
  const [lookupDone, setLookupDone] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [notice, setNotice] = useState("");
  const [campaign, setCampaign] = useState("");
  const [contextId, setContextId] = useState("");
  const [reason, setReason] = useState("");
  const abort = useRef<AbortController | null>(null);
  const active = useRef(true);
  const receipt = useRef({ signature: "", key: "" });
  const previousBulk = useRef(bulk.revision);
  const campaigns = useQuery({ queryKey: ["setup", "campaigns"], queryFn: ({ signal }) => setupApi.campaigns(signal) });
  const contexts = useQuery({ queryKey: ["setup", "contexts", campaign], queryFn: ({ signal }) => setupApi.contexts(campaign, signal), enabled: Boolean(campaign) });
  const context = contexts.data?.find((item) => item.id === contextId);
  const detail = useQuery({ queryKey: ["archives", "detail", archive?.id], queryFn: ({ signal }) => archivesApi.detail(archive!.id, signal), enabled: Boolean(archive), refetchInterval: (query) => ["READY", "FAILED"].includes(query.state.data?.processing_state ?? "") ? false : 3000 });
  const current = detail.data ?? archive;
  useEffect(() => {
    active.current = true;
    const controller = new AbortController();
    void preflightCsv(file, setPercent, controller.signal).then(async (result) => {
      if (!active.current) return;
      setPreflight(result);
      if (result.errors.length) return;
      try { const match = await importApi.lookup(result.sha256, file.size, controller.signal); if (active.current) { setArchive(match.archive); setReused(Boolean(match.archive)); setLookupDone(true); } }
      catch (failure) { if (!controller.signal.aborted) setError(`查重失败：${message(failure)}`); }
    }).catch((failure) => { if (!controller.signal.aborted) setPreflightError(message(failure)); });
    return () => { active.current = false; controller.abort(); abort.current?.abort(); };
  }, [file]);
  useEffect(() => {
    if (bulk.revision !== previousBulk.current) {
      previousBulk.current = bulk.revision;
      if (!archive && !busy) setMeta(bulk.value);
    }
  }, [bulk, archive, busy]);
  async function retryLookup() {
    if (!preflight) return;
    setError("");
    try { const match = await importApi.lookup(preflight.sha256, file.size); setArchive(match.archive); setReused(Boolean(match.archive)); setLookupDone(true); }
    catch (failure) { setError(`查重失败：${message(failure)}`); }
  }
  async function upload() {
    if (!preflight || busy) return;
    const controller = new AbortController(); abort.current = controller;
    setBusy(true); setUploading(true); setError(""); setNotice("");
    try {
      let target = current;
      if (!target) {
        const classification = context ? { ...context.context, station_id: meta.station } as ArchiveContext : null;
        const result = await importApi.create({ filename: file.name, size_bytes: file.size, sha256: preflight.sha256, test_name: name.trim(), actual_object: meta.actualObject.trim(), station_id: meta.station, classification }, importRequestKey(owner, preflight.sha256), controller.signal);
        target = result.archive; setArchive(target); setReused(result.reused);
        if (result.reused) { setNotice("发现相同文件，已复用原归档。请核对原信息后再选择续传。"); return; }
      }
      const updated = await uploadCsv(file, target, controller.signal, (value) => { if (active.current) { setArchive(value); cache.setQueryData(["archives", "detail", value.id], value); } });
      setArchive(updated); setNotice("原文件已接收，等待服务器校验和解析。人工核实尚未提交。");
      await cache.invalidateQueries({ queryKey: ["archives"] });
    } catch (failure) { if (active.current) setError(controller.signal.aborted ? "已暂停。重新选择同一文件或点击续传，将按服务器回执继续。" : message(failure)); }
    finally { abort.current = null; if (active.current) { setBusy(false); setUploading(false); } }
  }
  async function verify() {
    if (!current || !meta.verdict || busy) return;
    const station = current.current_verification?.station_id ?? meta.station;
    const payload = { expected_revision: current.revision, tester: meta.tester.trim(), station_id: station, verdict: meta.verdict, notes: meta.notes.trim(), discrepancy_reason: meta.discrepancy.trim(), reason: reason.trim() };
    const signature = JSON.stringify(payload);
    if (receipt.current.signature !== signature) receipt.current = { signature, key: crypto.randomUUID() };
    setBusy(true); setError("");
    try { const saved = await archivesApi.verify(current.id, payload, receipt.current.key); setArchive(saved); cache.setQueryData(["archives", "detail", current.id], saved); setNotice("核实版本已保存。归档与审批状态独立，尚未送审。"); await cache.invalidateQueries({ queryKey: ["archives"] }); }
    catch (failure) { setError(message(failure)); }
    finally { setBusy(false); }
  }
  const validBasic = Boolean(name.trim() && meta.actualObject.trim() && meta.station && options?.stations.some((item) => item.id === meta.station));
  const contextMismatch = Boolean(context && (context.asset_code !== meta.actualObject.trim() || context.context.station_id !== meta.station));
  const canUpload = preflight && !preflight.errors.length && lookupDone && confirmed && validBasic && (!campaign || context) && !contextMismatch;
  const canResume = current?.upload_state === "UPLOADING" && current.is_owner && current.can_edit;
  const canVerify = current?.processing_state === "READY" && current.is_owner && current.can_edit && !reused && !current.current_verification;
  const different = meta.verdict !== (current?.manifest?.analysis?.verdict ?? current?.facts.automatic_verdict);
  const verificationComplete = meta.tester.trim() && meta.verdict && meta.notes.trim() && (!different || meta.discrepancy.trim()) && reason.trim() && meta.station;
  const uploadMissing = [
    !name.trim() && "测试名称", !meta.actualObject.trim() && "样品 / 实际对象",
    (!meta.station || !options?.stations.some((item) => item.id === meta.station)) && "有效工位",
    !confirmed && "独立执行确认", Boolean(campaign && !context) && "完整计划上下文",
    contextMismatch && "样品 / 工位与计划一致", !lookupDone && "完成文件查重"
  ].filter(Boolean);
  const verificationMissing = [
    !meta.tester.trim() && "实际测试人", !meta.verdict && "人工结论", !meta.notes.trim() && "测试员备注",
    !reason.trim() && "核实说明", Boolean(meta.verdict && different && !meta.discrepancy.trim()) && "与自动结论差异原因"
  ].filter(Boolean);
  return <article className="import-file" aria-label={`补录文件 ${file.name}`}>
    <header className="import-file-heading"><FileUp aria-hidden="true" /><div><h3>{file.name}</h3><p>{(file.size / 1024 / 1024).toFixed(2)} MiB · 原文件保持不变</p></div><button className="import-icon" type="button" aria-label={`移除 ${file.name}`} disabled={busy} onClick={remove}><X aria-hidden="true" /></button></header>
    {!preflight && !preflightError && <div role="status" aria-live="polite" aria-atomic="true" className="import-progress"><span>正在本地预检与计算校验值 · {percent}%</span><progress aria-label={`${file.name} 本地预检与校验进度`} value={percent} max={100} /></div>}
    {preflightError && <p className="import-errors" role="alert">{preflightError}</p>}
    {preflight && <PreflightDetails result={preflight} />}
    {!current && preflight && !preflight.errors.length && <div className="import-file-form"><h4>执行信息</h4><p className="import-hint">上传必填：测试名称、样品 / 实际对象、有效工位及独立执行确认。实际测试人、人工结论和备注可在核实时补齐。</p><label>测试名称<input required maxLength={200} value={name} disabled={busy} onChange={(event) => setName(event.target.value)} placeholder="填写这一次测试的名称" /></label><MetadataFields value={meta} onChange={setMeta} options={options} prefix={`file-${fieldId}`} disabled={busy} requiredBasic />
      <details><summary>可选：关联正式计划与用例</summary><div className="import-form-grid"><label>测试计划<select value={campaign} disabled={busy} onChange={(event) => { setCampaign(event.target.value); setContextId(""); }}><option value="">暂不关联，作为待归类记录</option>{campaigns.data?.filter((entry) => entry.can_edit).map((entry) => <option key={entry.id} value={entry.id}>{entry.code} · {entry.name}</option>)}</select></label><label>已发布用例与样品<select value={contextId} disabled={!campaign || busy || contexts.isLoading} onChange={(event) => setContextId(event.target.value)}><option value="">选择完整计划上下文</option>{contexts.data?.filter((entry) => entry.status !== "CANCELLED").map((entry) => <option key={entry.id} value={entry.id}>{entry.case_code} · {entry.case_name} · {entry.version} · {entry.asset_code}</option>)}</select></label></div>{contextMismatch && <p className="import-errors">所选计划中的样品或工位与本次填写不一致，请核对后再上传。</p>}{(campaigns.error || contexts.error) && <p role="alert" className="import-errors">正式上下文读取失败：{message(campaigns.error || contexts.error)}</p>}<p className="import-hint">仅保存已有计划的完整归属；缺少机器采集事实时不能进入正式审批。</p></details>
      <label className="import-confirm"><input type="checkbox" required checked={confirmed} disabled={busy} onChange={(event) => setConfirmed(event.target.checked)} />我确认此文件对应一次独立执行，不与其他所选文件合并</label>
      <p className="import-hint">上传为待核实记录。实际测试人、人工结论和备注可在解析后确认；不会自动批准或计入正式统计 / MTBF。</p>
      {uploadMissing.length > 0 && <p className="import-hint" id={`${fieldId}-upload-missing`}>上传前还需：{uploadMissing.join("、")}。</p>}
      <button type="button" className="import-primary" aria-describedby={uploadMissing.length ? `${fieldId}-upload-missing` : undefined} disabled={!canUpload || busy} onClick={() => void upload()}><UploadCloud aria-hidden="true" />{busy ? "正在上传…" : "上传并解析待核实记录"}</button>
    </div>}
    {current && <div className="import-receipt"><div className="import-receipt-title"><CheckCircle2 aria-hidden="true" /><strong>{reused ? "相同文件已存在" : "归档记录已建立"} · {archiveTitle(current)}</strong></div><p>{current.test_name} · {current.actual_object} · {current.station_label}</p><ArchiveStatus item={current} /><p className="import-hint">执行 UUID：{current.execution_uuid}</p>
      {reused && <p className="import-hint">显示原记录。批量填写的信息未写入此归档，修改需进入原归档追加核实版本。</p>}
      {current.upload_state === "UPLOADING" && <div role="status" aria-live="polite" aria-atomic="true" className="import-progress"><span>服务器已接收 {(current.received_bytes / 1024 / 1024).toFixed(2)} / {(current.size_bytes / 1024 / 1024).toFixed(2)} MiB</span><progress aria-label={`${file.name} 原文件上传进度`} max={current.size_bytes} value={current.received_bytes} /></div>}
      {current.processing_error && <p role="alert" className="import-errors">服务器校验 / 解析失败：{current.processing_error}</p>}
      <div className="import-actions">{canResume && <button type="button" disabled={busy} onClick={() => void upload()}>继续上传同一原文件</button>}{uploading && <button type="button" onClick={() => abort.current?.abort()}>暂停上传</button>}<button type="button" onClick={() => navigate("archive", current.id)}>查看归档 / 核实详情</button><button type="button" disabled={current.processing_state !== "READY"} onClick={() => navigate("curves", current.id)}><ChartNoAxesCombined aria-hidden="true" />查看曲线</button></div>
      {canVerify && <div className="import-file-form"><h4>核实本次记录</h4><p className="import-hint">核实必填：实际测试人、人工结论、测试员备注、核实说明。人工结论与自动分析不一致时，差异原因也必填。</p><MetadataFields value={meta} onChange={setMeta} options={options} prefix={`verify-${current.id}`} disabled={busy} verification needsDiscrepancy={different} /><label>核实说明<input required value={reason} maxLength={300} onChange={(event) => setReason(event.target.value)} placeholder="说明本次核实的依据" /></label>{verificationMissing.length > 0 && <p className="import-hint" id={`${fieldId}-verify-missing`}>核实前还需：{verificationMissing.join("、")}。</p>}<button type="button" className="import-primary" aria-describedby={verificationMissing.length ? `${fieldId}-verify-missing` : undefined} disabled={!verificationComplete || busy} onClick={() => void verify()}>保存人工核实版本</button></div>}
      {current.processing_state === "READY" && missingArchiveAcquisitionFacts(current).length > 0 && <p className="import-hint">源文件存在缺失的机器采集事实（运行秒数、完成循环、异常数或明确机器终态），当前不能正式送审。已归档曲线可查看，正式统计与 MTBF 需满足各自准入条件。</p>}
    </div>}
    {notice && <p role="status" className="import-notice">{notice}</p>}{error && <div role="alert" className="import-errors">{error}{!lookupDone && preflight && <button type="button" onClick={() => void retryLookup()}>重新查重</button>}{current && <button type="button" onClick={() => void detail.refetch()}>刷新归档状态</button>}</div>}
  </article>;
}

export default function CsvImportPage({ navigate }: { navigate: Navigate }) {
  const { session } = useAuth();
  const [files, setFiles] = useState<{ id: string; file: File; initial: Metadata }[]>([]);
  const [meta, setMeta] = useState<Metadata>(blank);
  const [bulk, setBulk] = useState({ value: blank, revision: 0 });
  const [dragging, setDragging] = useState(false);
  const [notice, setNotice] = useState("");
  const input = useRef<HTMLInputElement>(null);
  const allowed = Boolean(session && session.user.role !== "VIEWER");
  const options = useQuery({ queryKey: ["setup", "options"], queryFn: ({ signal }) => setupApi.options(signal), enabled: allowed });
  function selectFiles(selected: File[]) {
    const next: typeof files = [], issues: string[] = [];
    for (const file of selected) {
      if (!/\.csv$/i.test(file.name)) { issues.push(`${file.name}：请选择 CSV 文件。`); continue; }
      if (!file.size) { issues.push(`${file.name}：文件为空。`); continue; }
      if ([...files, ...next].some((item) => item.file === file)) { issues.push(`${file.name}：已在当前队列。`); continue; }
      if (files.length + next.length >= 20) { issues.push("单批最多 20 份，请分批补录。"); break; }
      next.push({ id: crypto.randomUUID(), file, initial: { ...meta } });
    }
    setFiles((previous) => [...previous, ...next]); setNotice(issues.join(" "));
  }
  if (!allowed) return <Panel title="数据补录"><p>仅执行员和管理员可以补录测试数据，请联系有权限的人员。</p></Panel>;
  return <div className="csv-import-page"><button type="button" className="back-button" onClick={() => navigate("archives")}><ArrowLeft aria-hidden="true" />返回数据归档</button><PageTitle eyebrow="LOCAL CSV IMPORT" title="数据补录" description="选择本地 CSV，核对每次测试的信息，再归档查看曲线。" />
    <ol className="import-steps"><li><span>1</span>选择文件与预检</li><li><span>2</span>填写执行信息</li><li><span>3</span>上传与服务器校验</li><li><span>4</span>人工核实与归类</li></ol>
    <div className="import-workbench"><aside><Panel title="批量填写" description="应用到尚未上传的文件，每份文件仍独立确认。"><MetadataFields value={meta} onChange={setMeta} options={options.data} prefix="bulk" /><button type="button" className="import-bulk-button" disabled={!files.length} onClick={() => { setBulk({ value: { ...meta }, revision: bulk.revision + 1 }); setNotice("已应用到尚未建立归档的文件；现有归档信息保持原样。"); }}>应用到未上传文件</button><p className="import-hint">登录提交人：{session?.user.display_name ?? "当前账户"}。实际测试人需另行填写。</p>{options.error && <ErrorState error={options.error} retry={() => void options.refetch()} />}</Panel></aside>
    <div className="import-queue"><div className={`import-dropzone${dragging ? " dragging" : ""}`} onDragOver={(event) => { event.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={(event) => { event.preventDefault(); setDragging(false); selectFiles(Array.from(event.dataTransfer.files)); }}><UploadCloud aria-hidden="true" /><h2>拖入 CSV 文件</h2><p>支持多份文件 · 先在本机预检，不自动上传</p><button type="button" onClick={() => input.current?.click()}>选择本地 CSV</button><input ref={input} tabIndex={-1} type="file" accept=".csv,text/csv" multiple aria-label="选择本地 CSV 文件" onChange={(event) => { selectFiles(Array.from(event.target.files ?? [])); event.target.value = ""; }} /></div>
      <p className="import-execution-note"><AlertCircle aria-hidden="true" />一份文件对应一次独立执行。属于同一次执行的分片请保留原件，勿作为多次测试补录。</p>
      {notice && <p role="status" className="import-notice">{notice}</p>}
      {files.length > 0 && <div className="import-queue-heading"><h2>待处理文件</h2><span>{files.length} 份</span></div>}
      {files.map(({ id, file, initial }) => <ImportFile key={id} file={file} initial={initial} bulk={bulk} options={options.data} owner={session!.user.id} navigate={navigate} remove={() => setFiles((previous) => previous.filter((item) => item.id !== id))} />)}
    </div></div>
  </div>;
}
