import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "../auth";
import { setupApi } from "../setupApi";
import { displayPartName, partsCompatible } from "../homeCarousel";
import { formatDate } from "../navigation";
import type { AssetKind } from "../types";
import { EmptyState, ErrorState, LoadingState, PageTitle, Panel } from "../components/Workbench";

type Field = { name: string; label: string; hint?: string; optional?: boolean; type?: string; value?: string; options?: { value: string; label: string }[] };
const reason: Field = { name: "reason", label: "建档或配置理由" };
const text = (name: string, label: string, hint?: string): Field => ({ name, label, hint });

function CreateForm({ title, path, fields, values = {}, after }: { title: string; path: string; fields: Field[]; values?: Record<string, unknown>; after?: () => void }) {
  const cache = useQueryClient();
  const mutation = useMutation({
    mutationFn: (payload: Record<string, unknown>) => setupApi.create(path, payload),
    onSuccess: async () => {
      await cache.invalidateQueries();
      after?.();
    }
  });
  const [inputError, setInputError] = useState("");
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const payload: Record<string, unknown> = { ...Object.fromEntries(new FormData(form)), ...values };
    try {
      for (const field of fields) {
        if (field.type === "json") payload[field.name] = JSON.parse(String(payload[field.name] || "{}"));
        if (field.type === "number") payload[field.name] = Number(payload[field.name]);
        if (field.type === "datetime-local") payload[field.name] = payload[field.name] ? new Date(String(payload[field.name])).toISOString() : null;
      }
      setInputError("");
      mutation.mutate(payload, { onSuccess: () => form.reset() });
    } catch {
      setInputError("配置清单必须是有效JSON对象，日期必须有效。");
    }
  }
  return <form className="setup-form" onSubmit={submit}>
    <h3>{title}</h3>
    <div className="setup-fields">{fields.map((field) => <label key={field.name}>{field.label}
      {field.options ? <select name={field.name} required={!field.optional} defaultValue=""><option value="">请选择</option>{field.options.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select> : field.type === "json" ? <textarea name={field.name} defaultValue="{}" rows={2} /> : <input name={field.name} type={field.type ?? "text"} required={!field.optional} defaultValue={field.value ?? ""} placeholder={field.hint} min={field.type === "number" ? 1 : undefined} max={field.type === "number" ? 100000 : undefined} maxLength={field.name === "reason" ? 2048 : 255} />}
      {field.hint && <small>{field.hint}</small>}
    </label>)}</div>
    {(mutation.error || inputError) && <p role="alert">{inputError || (mutation.error instanceof Error ? mutation.error.message : "保存失败")}</p>}
    {mutation.isSuccess && !mutation.isPending && <p role="status">保存成功。建档不会启动硬件或生成执行记录。</p>}
    <button className="primary-button" disabled={mutation.isPending}>{mutation.isPending ? "正在保存…" : "保存"}</button>
  </form>;
}

export function AssetCreateForm({ assetKind }: { assetKind: AssetKind }) {
  const { session } = useAuth();
  const [open, setOpen] = useState(false);
  const options = useQuery({ queryKey: ["setup-options"], queryFn: ({ signal }) => setupApi.options(signal), enabled: open });
  if (session?.user.role !== "SYSTEM_ADMIN") return null;
  return <section className="setup-section">
    <button className="primary-button" onClick={() => setOpen(!open)} type="button">{open ? "收起样品建档" : "新增样品"}</button>
    {open && (options.isLoading ? <LoadingState /> : options.error ? <ErrorState error={options.error} retry={() => void options.refetch()} /> : <CreateForm key={assetKind} title="样品建档与初始配置" path="assets" values={{ asset_kind: assetKind }} fields={[
      text("asset_code", "样品编号", "格式：RP1.3-SLEG-001；部位须与选择一致"),
      { name: "target_part_code", label: "样品部位", options: (options.data?.parts ?? []).filter((p) => p.asset_kind === assetKind).map((p) => ({ value: p.code, label: `${p.code} · ${p.name}` })) },
      text("display_name", "样品名称"), text("product_family", "产品系列"), text("serial_number", "序列号"), text("model", "型号或模块类型"), text("configuration_fingerprint", "配置标识", "填写可追溯的配置版本，配置影响初始化为未知"),
      { name: "hardware_manifest", label: "硬件配置清单（JSON）", type: "json" }, { name: "software_manifest", label: "软件配置清单（JSON）", type: "json" }, { name: "parameter_manifest", label: "参数配置清单（JSON）", type: "json" }, reason
    ]} />)}
  </section>;
}

export function CatalogPage({ assetKind, navigate }: { assetKind: AssetKind; navigate: (id: string) => void }) {
  const [search, setSearch] = useState("");
  const catalog = useQuery({ queryKey: ["setup-catalog"], queryFn: ({ signal }) => setupApi.catalog(signal) });
  const items = (catalog.data ?? []).filter((c) => c.asset_kind === assetKind && `${c.code} ${c.name} ${c.target_part_name ?? ""}`.toLowerCase().includes(search.toLowerCase()));
  return <>
    <PageTitle eyebrow="TEST CASE CATALOG" title={`${assetKind === "MODULE" ? "模块" : "整机"}测试目录`} description="独立展示全部测试项目及发布状态。样品和执行记录为零时也可浏览。" />
    <div className="filter-bar"><label>搜索测试项目<input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="编号、名称或部位" /></label><span>{items.length} 项 / 全目录 {catalog.data?.length ?? 0} 项</span></div>
    {catalog.isLoading ? <LoadingState /> : catalog.error ? <ErrorState error={catalog.error} retry={() => void catalog.refetch()} /> : <Panel title="测试项目与版本">
      {items.length ? <div className="table-scroll"><table className="data-table"><thead><tr><th>编号</th><th>名称</th><th>部位</th><th>领域</th><th>发布版本</th><th>状态</th></tr></thead><tbody>{items.map((c) => <tr key={c.id}><td><button className="link-button" onClick={() => navigate(c.id)}>{c.code}</button></td><td>{c.name}</td><td>{c.target_part_code ? displayPartName(c.target_part_code, c.target_part_name ?? "未指定") : "未指定"}</td><td>{c.domain}</td><td>{c.version ?? "未发布"}</td><td>{c.enabled ? c.version_id ? "可配置" : "待发布版本" : "已停用"}</td></tr>)}</tbody></table></div> : <EmptyState title="没有匹配测试项目" description="请调整搜索条件。" />}
    </Panel>}
  </>;
}

export function PlanningPage({ assetKind }: { assetKind: AssetKind }) {
  const { session } = useAuth();
  const admin = session?.user.role === "SYSTEM_ADMIN";
  const [selected, setSelected] = useState("");
  const [assetId, setAssetId] = useState("");
  const [caseId, setCaseId] = useState("");
  const [tab, setTab] = useState("plans");
  const plans = useQuery({ queryKey: ["setup-campaigns"], queryFn: ({ signal }) => setupApi.campaigns(signal) });
  const options = useQuery({ queryKey: ["setup-options"], queryFn: ({ signal }) => setupApi.options(signal) });
  const catalog = useQuery({ queryKey: ["setup-catalog"], queryFn: ({ signal }) => setupApi.catalog(signal) });
  const contexts = useQuery({ queryKey: ["setup-contexts", selected], queryFn: ({ signal }) => setupApi.contexts(selected, signal), enabled: Boolean(selected) });
  const campaign = plans.data?.find((p) => p.id === selected && p.asset_kind === assetKind);
  const asset = options.data?.assets.find((a) => a.id === assetId && a.asset_kind === assetKind);
  const cases = (catalog.data ?? []).filter((c) => c.enabled && c.version_id && c.asset_kind === assetKind && partsCompatible(c.target_part_code, asset?.target_part_code));
  const chosenCase = cases.find((c) => c.version_id === caseId);
  const error = plans.error ?? options.error ?? catalog.error;
  if (error) return <ErrorState error={error} retry={() => { void plans.refetch(); void options.refetch(); void catalog.refetch(); }} />;
  return <>
    <PageTitle eyebrow="TEST PLANNING" title={`${assetKind === "MODULE" ? "模块" : "整机"}测试计划`} description="建立项目、测试计划，再配置样品、已发布用例、台架和周期。配置不启动测试；结果及正式审批仍走独立流程。" />
    <nav className="setup-tabs" aria-label="计划管理">{[["plans", "计划与关联"], ...(admin ? [["create", "创建项目和计划"], ["stations", "台架建档"]] : [])].map(([value, label]) => <button type="button" key={value} aria-pressed={tab === value} onClick={() => setTab(value)}>{label}</button>)}</nav>
    {tab === "create" && admin && <div className="setup-columns">
      <Panel title="1 · 创建项目"><CreateForm title="项目是测试计划的归属" path="programs" fields={[text("program_code", "项目编号"), text("name", "项目名称"), { name: "objective", label: "项目目标", optional: true }, reason]} /></Panel>
      <Panel title="2 · 创建测试计划"><CreateForm key={assetKind} title="计划初始状态：待执行" path="campaigns" values={{ asset_kind: assetKind }} fields={[
        { name: "program_id", label: "所属项目", options: (options.data?.programs ?? []).filter((p) => !["CLOSED", "CANCELLED"].includes(p.status)).map((p) => ({ value: p.id, label: `${p.code} · ${p.name}` })) }, text("campaign_code", "计划编号"), text("name", "计划名称"),
        { name: "planned_start", label: "计划开始时间", type: "datetime-local", optional: true }, { name: "planned_end", label: "计划结束时间", type: "datetime-local", optional: true }, reason
      ]} /></Panel>
    </div>}
    {tab === "stations" && admin && <Panel title="台架与组织信息"><CreateForm title="同一站点或实验室编号须使用相同名称" path="stations" fields={[
      text("site_code", "站点编号"), text("site_name", "站点名称"), text("lab_code", "实验室编号"), text("lab_name", "实验室名称"), text("station_code", "台架编号"), text("name", "台架名称"), { name: "station_type", label: "台架类型", options: [{ value: "WHOLE_MACHINE", label: "整机" }, { value: "MODULE", label: "模块" }, { value: "SHARED", label: "共享" }] }, reason
    ]} /></Panel>}
    {tab === "plans" && <>
      <Panel title="可访问的计划">
        {plans.isLoading ? <LoadingState /> : !(plans.data ?? []).some((p) => p.asset_kind === assetKind) ? <EmptyState title="暂无可访问计划" description={admin ? "请使用“创建项目和计划”建立真实测试计划。" : "请联系管理员创建计划并授权。"} /> : <div className="setup-plan-list">{plans.data?.filter((p) => p.asset_kind === assetKind).map((p) => <button key={p.id} type="button" aria-pressed={selected === p.id} onClick={() => { setSelected(p.id); setAssetId(""); setCaseId(""); }}><strong>{p.code} · {p.name}</strong><span>{p.program_code ?? "项目详情不可见"} · {p.status} · {(p.stages ?? []).map((stage) => stage.stage_name).join("、") || "测试阶段1"} · {p.asset_count} 个样品 · {p.case_count} 个配置项</span><small>{formatDate(p.planned_start)} 至 {formatDate(p.planned_end)}</small></button>)}</div>}
      </Panel>
      {campaign && <Panel title={`${campaign.code} · 样品与用例关联`} description={campaign.can_edit ? "配置所需样品和台架须先建档；执行员需要计划与样品的编辑权限。" : "当前计划为只读。"}>
        {admin && campaign.status === "PLANNED" && <CreateForm title="开放计划使用与权限申请（不启动硬件）" path={`campaigns/${campaign.id}/activate`} fields={[reason]} />}
        {campaign.can_edit && <>
          <div className="setup-fields"><label>选择样品<select value={assetId} onChange={(e) => { setAssetId(e.target.value); setCaseId(""); }}><option value="">请选择</option>{options.data?.assets.filter((a) => a.asset_kind === assetKind).map((a) => <option key={a.id} value={a.id}>{a.code} · {a.name}</option>)}</select></label><label>选择已发布用例<select value={chosenCase?.version_id ?? ""} onChange={(e) => setCaseId(e.target.value)}><option value="">请选择</option>{cases.map((c) => <option key={c.id} value={c.version_id!}>{c.code} · {c.name} · {c.version}</option>)}</select></label></div>
          {asset && chosenCase && <CreateForm key={`${campaign.id}-${asset.id}-${chosenCase.id}`} title="配置执行上下文" path={`campaigns/${campaign.id}/contexts`} values={{ asset_id: asset.id, test_case_version_id: chosenCase.version_id }} fields={[
            { name: "configuration_id", label: "配置快照", options: (options.data?.configurations ?? []).filter((c) => c.asset_id === asset.id).map((c) => ({ value: c.id, label: `${c.fingerprint} · 影响${c.reliability_impact}` })) },
            { name: "station_id", label: "台架", options: (options.data?.stations ?? []).filter((s) => s.station_type === "SHARED" || s.station_type === assetKind).map((s) => ({ value: s.id, label: `${s.code} · ${s.name}` })) }, text("cycle_code", "周期编号", "全站唯一；配置后状态为待执行"), { name: "planned_runs", label: "计划执行次数", type: "number", value: "1" }, reason
          ]} />}
        </>}
        {contexts.isLoading ? <LoadingState /> : contexts.error ? <ErrorState error={contexts.error} retry={() => void contexts.refetch()} /> : contexts.data?.length ? <div className="setup-context-list">{contexts.data.map((c) => <article key={c.id}><strong>{c.asset_code} · {c.case_code}</strong><p>{c.case_name} · 版本{c.version} · {c.planned_runs}次 · {c.status}</p><details><summary>采集接入所需标识</summary><pre>{JSON.stringify(c.context, null, 2)}</pre></details></article>)}</div> : <EmptyState title="尚未配置样品与用例" description="建立关联后可查看执行所需的上下文标识，不会生成运行记录。" />}
      </Panel>}
    </>}
  </>;
}
