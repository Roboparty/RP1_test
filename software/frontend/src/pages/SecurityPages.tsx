import { useMemo, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Ban,
  Check,
  FileClock,
  KeyRound,
  LockKeyhole,
  Send,
  ShieldCheck,
  UserPlus,
  Users,
  X
} from "lucide-react";
import { useAuth } from "../auth";
import { formatDate } from "../navigation";
import { accessApi, adminApi } from "../securityApi";
import type { AccessLevel, Role } from "../security";
import { roleLabels } from "../security";

function ErrorLine({ error }: { error: unknown }) {
  if (!error) return null;
  return <p className="form-error" role="alert">{error instanceof Error ? error.message : "操作失败，请重试。"}</p>;
}

export function ForbiddenState() {
  return (
    <section className="security-state forbidden-state" aria-labelledby="forbidden-title">
      <LockKeyhole aria-hidden="true" />
      <span className="eyebrow">ACCESS BOUNDARY</span>
      <h1 id="forbidden-title" data-page-heading tabIndex={-1}>无权访问管理工作台</h1>
      <p>当前账户没有系统管理权限。页面未加载任何管理数据。</p>
    </section>
  );
}

export function AccountPage() {
  const { session, changePassword } = useAuth();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setError(null);
    setMessage("");
    if (next.length < 12 || next !== confirm || current === next) {
      setError(new Error(next.length < 12 ? "新密码至少 12 个字符。" : next !== confirm ? "两次新密码不一致。" : "新密码不能与当前密码相同。"));
      return;
    }
    setBusy(true);
    try {
      await changePassword(current, next);
      setCurrent(""); setNext(""); setConfirm("");
      setMessage("密码已更新，其他登录会话已撤销。");
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="security-page">
      <header className="security-page-title"><span className="eyebrow">ACCOUNT SECURITY</span><h1 data-page-heading tabIndex={-1}>账户设置</h1></header>
      <div className="account-layout">
        <section className="security-section identity-summary">
          <h2>身份信息</h2>
          <dl><div><dt>显示名称</dt><dd>{session?.user.display_name}</dd></div><div><dt>用户名</dt><dd>{session?.user.username}</dd></div><div><dt>固定角色</dt><dd><span className={`role-pill ${session?.user.role.toLowerCase()}`}>{session && roleLabels[session.user.role]}</span></dd></div><div><dt>会话过期</dt><dd>{formatDate(session?.expires_at)}</dd></div></dl>
        </section>
        <section className="security-section">
          <h2><KeyRound aria-hidden="true" />修改密码</h2>
          <form className="compact-form" onSubmit={submit} aria-busy={busy}>
            <label>当前密码<input type="password" autoComplete="current-password" value={current} onChange={(event) => setCurrent(event.target.value)} required /></label>
            <label>新密码<input type="password" autoComplete="new-password" minLength={12} value={next} onChange={(event) => setNext(event.target.value)} required /></label>
            <label>确认新密码<input type="password" autoComplete="new-password" value={confirm} onChange={(event) => setConfirm(event.target.value)} required /></label>
            <ErrorLine error={error} />{message && <p className="form-success">{message}</p>}
            <button className="primary-button" type="submit" disabled={busy}>{busy ? "正在更新…" : "更新密码"}</button>
          </form>
        </section>
      </div>
      <section className="security-section grants-section"><h2>当前 Campaign 授权</h2>{session?.user.role === "SYSTEM_ADMIN" && <p className="inline-note">系统管理员具有所有 ACTIVE Campaign 的全局 EDIT 能力，无需单独申请。</p>}<div className="grant-list">{session?.campaign_access.length ? session.campaign_access.map((grant) => <div className="grant-row" key={grant.campaign_id}><div><strong>{grant.campaign_code}</strong><span>{grant.campaign_name}</span></div><span className={`access-pill ${grant.access_level.toLowerCase()}`}>{grant.access_level}</span><time>{formatDate(grant.granted_at)}</time></div>) : <p className="empty-line">暂无 Campaign 授权。</p>}</div></section>
    </div>
  );
}

export function AccessRequestPage() {
  const queryClient = useQueryClient();
  const { session, refetchMe } = useAuth();
  const campaigns = useQuery({ queryKey: ["access-campaigns"], queryFn: ({ signal }) => accessApi.available(signal) });
  const requests = useQuery({ queryKey: ["my-access-requests"], queryFn: ({ signal }) => accessApi.mine(signal) });
  const [campaignId, setCampaignId] = useState("");
  const [level, setLevel] = useState<AccessLevel>("VIEW");
  const [reason, setReason] = useState("");
  const submit = useMutation({
    mutationFn: () => accessApi.submit(campaignId, level, reason),
    onSuccess: async () => {
      setReason("");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["access-campaigns"] }),
        queryClient.invalidateQueries({ queryKey: ["my-access-requests"] }),
        queryClient.invalidateQueries({ queryKey: ["admin-pending"] }),
        refetchMe()
      ]);
    }
  });
  const cancel = useMutation({
    mutationFn: accessApi.cancel,
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["access-campaigns"] }),
        queryClient.invalidateQueries({ queryKey: ["my-access-requests"] }),
        queryClient.invalidateQueries({ queryKey: ["admin-pending"] })
      ]);
    }
  });
  const selected = campaigns.data?.find((item) => item.campaign_id === campaignId);
  const alreadySatisfied = selected?.current_grant === "EDIT" || (selected?.current_grant === "VIEW" && level === "VIEW");
  const cannotSubmit = !campaignId || !reason.trim() || Boolean(selected?.pending_request_id) || alreadySatisfied;

  return (
    <div className="security-page">
      <header className="security-page-title"><span className="eyebrow">CAMPAIGN ACCESS</span><h1 data-page-heading tabIndex={-1}>权限申请</h1></header>
      {session?.user.role === "SYSTEM_ADMIN" && <p className="admin-global-note"><ShieldCheck aria-hidden="true" />系统管理员已有全局权限；此页面仅用于查看申请流程，管理员不能审批自己的申请。</p>}
      <section className="security-section request-composer">
        <h2><Send aria-hidden="true" />提交新申请</h2>
        <div className="request-grid">
          <label>Campaign<select value={campaignId} onChange={(event) => setCampaignId(event.target.value)}><option value="">选择可申请 Campaign</option>{campaigns.data?.map((item) => <option key={item.campaign_id} value={item.campaign_id}>{item.campaign_code} · {item.campaign_name}</option>)}</select></label>
          <label>申请级别<select value={level} onChange={(event) => setLevel(event.target.value as AccessLevel)}><option value="VIEW">VIEW · 读取</option><option value="EDIT">EDIT · 编辑</option></select></label>
          <label className="reason-field">申请理由<textarea value={reason} onChange={(event) => setReason(event.target.value)} maxLength={2048} placeholder="说明业务用途和所需范围" /></label>
        </div>
        {selected?.pending_request_id && <p className="inline-note">该 Campaign 已有 {selected.pending_requested_level} 待审批申请。</p>}
        {alreadySatisfied && <p className="inline-note">当前授权已满足所选级别。</p>}
        <ErrorLine error={submit.error ?? campaigns.error} />
        <button className="primary-button" type="button" disabled={cannotSubmit || submit.isPending} onClick={() => submit.mutate()}>{submit.isPending ? "正在提交…" : "提交权限申请"}</button>
      </section>
      <section className="security-section"><h2>我的申请</h2><div className="request-list">{requests.data?.length ? requests.data.map((item) => <article className="request-row" key={item.id}><div><strong>{item.campaign_code}</strong><span>{item.campaign_name}</span></div><span className={`access-pill ${item.requested_level.toLowerCase()}`}>{item.requested_level}</span><span className={`status-pill ${item.status.toLowerCase()}`}>{item.status}</span><p>{item.reason}</p><time>{formatDate(item.created_at)}</time>{item.decision_reason && <small>审批意见：{item.decision_reason}</small>}{item.status === "PENDING" && <button className="subtle-button" type="button" onClick={() => cancel.mutate(item.id)} disabled={cancel.isPending}><X aria-hidden="true" />取消</button>}</article>) : <p className="empty-line">{requests.isLoading ? "正在加载…" : "暂无权限申请。"}</p>}</div><ErrorLine error={requests.error ?? cancel.error} /></section>
    </div>
  );
}

type AdminTab = "pending" | "users" | "grants" | "audit";

export function AdminWorkspace() {
  const { session } = useAuth();
  const [tab, setTab] = useState<AdminTab>("pending");
  if (session?.user.role !== "SYSTEM_ADMIN") return <ForbiddenState />;
  return (
    <div className="admin-workspace">
      <header><div><span className="eyebrow">SECURITY ADMINISTRATION</span><h1 data-page-heading tabIndex={-1}>管理工作台</h1></div><ShieldCheck aria-hidden="true" /></header>
      <nav aria-label="管理工作台页面">
        <button className={tab === "pending" ? "active" : ""} onClick={() => setTab("pending")}><Check />待审批</button>
        <button className={tab === "users" ? "active" : ""} onClick={() => setTab("users")}><Users />用户管理</button>
        <button className={tab === "grants" ? "active" : ""} onClick={() => setTab("grants")}><ShieldCheck />Campaign 授权</button>
        <button className={tab === "audit" ? "active" : ""} onClick={() => setTab("audit")}><FileClock />安全审计</button>
      </nav>
      <div className="admin-panel">{tab === "pending" && <PendingPanel />}{tab === "users" && <UsersPanel />}{tab === "grants" && <GrantsPanel />}{tab === "audit" && <AuditPanel />}</div>
    </div>
  );
}

function PendingPanel() {
  const queryClient = useQueryClient();
  const queue = useQuery({
    queryKey: ["admin-pending"],
    queryFn: ({ signal }) => adminApi.requests("PENDING", signal),
    refetchInterval: 30_000,
    refetchOnWindowFocus: true
  });
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const decide = useMutation({
    mutationFn: ({ id, action }: { id: string; action: "approve" | "reject" }) => adminApi.decide(id, action, reasons[id] ?? ""),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["admin-pending"] }),
        queryClient.invalidateQueries({ queryKey: ["admin-grants"] })
      ]);
    }
  });
  function act(id: string, action: "approve" | "reject") {
    if (!(reasons[id] ?? "").trim()) return;
    if (window.confirm(action === "approve" ? "确认批准这项权限申请？" : "确认拒绝这项权限申请？")) decide.mutate({ id, action });
  }
  return <section><div className="panel-heading"><div><h2>待审批申请</h2><p>审批理由必填；管理员不能审批自己的申请。</p></div><span className="count-badge">{queue.data?.length ?? 0}</span></div><div className="approval-list">{queue.data?.length ? queue.data.map((item) => <article key={item.id}><div className="approval-main"><strong>{item.requester_username}</strong><span>{item.campaign_code} · {item.campaign_name}</span><p>{item.reason}</p><time>{formatDate(item.created_at)}</time></div><span className={`access-pill ${item.requested_level.toLowerCase()}`}>{item.requested_level}</span><label>审批理由<input value={reasons[item.id] ?? ""} onChange={(event) => setReasons((value) => ({ ...value, [item.id]: event.target.value }))} placeholder="填写决定依据" /></label><div className="approval-actions"><button className="approve-button" disabled={!reasons[item.id]?.trim() || decide.isPending} onClick={() => act(item.id, "approve")}><Check />批准</button><button className="reject-button" disabled={!reasons[item.id]?.trim() || decide.isPending} onClick={() => act(item.id, "reject")}><X />拒绝</button></div></article>) : <p className="empty-line">{queue.isLoading ? "正在加载审批队列…" : "当前没有待审批申请。"}</p>}</div><ErrorLine error={queue.error ?? decide.error} /></section>;
}

function UsersPanel() {
  const { session } = useAuth();
  const queryClient = useQueryClient();
  const users = useQuery({ queryKey: ["admin-users"], queryFn: ({ signal }) => adminApi.users(signal) });
  const [form, setForm] = useState({ username: "", display_name: "", role: "VIEWER" as Role, password: "", must_change_password: true });
  const [disableReasons, setDisableReasons] = useState<Record<string, string>>({});
  const create = useMutation({ mutationFn: () => adminApi.createUser(form), onSuccess: async () => { setForm({ username: "", display_name: "", role: "VIEWER", password: "", must_change_password: true }); await queryClient.invalidateQueries({ queryKey: ["admin-users"] }); } });
  const disable = useMutation({ mutationFn: (id: string) => adminApi.disableUser(id, disableReasons[id] ?? ""), onSuccess: async () => queryClient.invalidateQueries({ queryKey: ["admin-users"] }) });
  function disableUser(id: string) {
    if (!disableReasons[id]?.trim() || !window.confirm("停用后该用户的所有会话将立即撤销。确认继续？")) return;
    disable.mutate(id);
  }
  return <section><div className="panel-heading"><div><h2>用户管理</h2><p>固定三角色；不提供任意角色编辑。</p></div><span className="count-badge">{users.data?.length ?? 0}</span></div><form className="admin-create-form" onSubmit={(event) => { event.preventDefault(); create.mutate(); }}><h3><UserPlus />创建本地账户</h3><label>用户名<input value={form.username} onChange={(event) => setForm({ ...form, username: event.target.value })} pattern="[a-z0-9][a-z0-9._-]{0,127}" required /></label><label>显示名称<input value={form.display_name} onChange={(event) => setForm({ ...form, display_name: event.target.value })} required /></label><label>角色<select value={form.role} onChange={(event) => setForm({ ...form, role: event.target.value as Role })}><option value="VIEWER">VIEWER</option><option value="TEST_EXECUTOR">TEST_EXECUTOR</option><option value="SYSTEM_ADMIN">SYSTEM_ADMIN</option></select></label><label>初始密码<input type="password" autoComplete="new-password" minLength={12} value={form.password} onChange={(event) => setForm({ ...form, password: event.target.value })} required /></label><label className="check-label"><input type="checkbox" checked={form.must_change_password} onChange={(event) => setForm({ ...form, must_change_password: event.target.checked })} />首次登录强制改密</label><button className="primary-button" disabled={create.isPending}>{create.isPending ? "创建中…" : "创建账户"}</button><ErrorLine error={create.error} /></form><div className="user-table"><div className="table-head"><span>账户</span><span>角色 / 状态</span><span>停用控制</span></div>{users.data?.map((user) => <div className="user-row" key={user.id}><div><strong>{user.display_name}</strong><span>{user.username}</span></div><div><span className={`role-pill ${user.role.toLowerCase()}`}>{roleLabels[user.role]}</span><small>{user.enabled ? "ENABLED" : "DISABLED"}{user.must_change_password ? " · 待改密" : ""}</small></div><div>{user.id === session?.user.id ? <span className="inline-note">当前账户不可停用</span> : user.enabled ? <><input aria-label={`停用 ${user.username} 的理由`} value={disableReasons[user.id] ?? ""} onChange={(event) => setDisableReasons((value) => ({ ...value, [user.id]: event.target.value }))} placeholder="停用理由" /><button className="reject-button" onClick={() => disableUser(user.id)} disabled={!disableReasons[user.id]?.trim() || disable.isPending}><Ban />停用</button></> : <span>已停用</span>}</div></div>)}</div><ErrorLine error={users.error ?? disable.error} /></section>;
}

function GrantsPanel() {
  const queryClient = useQueryClient();
  const users = useQuery({ queryKey: ["admin-users"], queryFn: ({ signal }) => adminApi.users(signal) });
  const campaigns = useQuery({ queryKey: ["access-campaigns"], queryFn: ({ signal }) => accessApi.available(signal) });
  const [userId, setUserId] = useState("");
  const grants = useQuery({ queryKey: ["admin-grants", userId], queryFn: ({ signal }) => adminApi.grants(userId, signal), enabled: Boolean(userId) });
  const [campaignId, setCampaignId] = useState("");
  const [level, setLevel] = useState<AccessLevel>("VIEW");
  const [reason, setReason] = useState("");
  const current = grants.data?.find((grant) => grant.campaign_id === campaignId);
  const mutation = useMutation({
    mutationFn: () => adminApi.grant(userId, campaignId, level, reason),
    onSuccess: async () => {
      setReason("");
      await queryClient.invalidateQueries({ queryKey: ["admin-grants", userId] });
    }
  });
  const revoke = useMutation({
    mutationFn: ({ campaign, reasonText }: { campaign: string; reasonText: string }) => adminApi.revoke(userId, campaign, reasonText),
    onSuccess: async () => queryClient.invalidateQueries({ queryKey: ["admin-grants", userId] })
  });
  const selectableUsers = useMemo(() => users.data?.filter((user) => user.kind === "HUMAN" && user.enabled) ?? [], [users.data]);
  function revokeGrant(campaign: string) {
    const reasonText = window.prompt("请输入撤销理由：")?.trim() ?? "";
    if (reasonText && window.confirm("确认撤销该 Campaign 授权？")) revoke.mutate({ campaign, reasonText });
  }
  return <section><div className="panel-heading"><div><h2>Campaign 授权</h2><p>直接授权和撤销均要求理由；已有 EDIT 不会被 VIEW 静默降级。</p></div></div><div className="grant-controls"><label>目标用户<select value={userId} onChange={(event) => { setUserId(event.target.value); setCampaignId(""); }}><option value="">选择用户</option>{selectableUsers.map((user) => <option key={user.id} value={user.id}>{user.username} · {roleLabels[user.role]}</option>)}</select></label><label>Campaign<select value={campaignId} onChange={(event) => setCampaignId(event.target.value)} disabled={!userId}><option value="">选择 Campaign</option>{campaigns.data?.map((item) => <option key={item.campaign_id} value={item.campaign_id}>{item.campaign_code} · {item.campaign_name}</option>)}</select></label><label>级别<select value={level} onChange={(event) => setLevel(event.target.value as AccessLevel)}><option value="VIEW">VIEW</option><option value="EDIT">EDIT</option></select></label><label>授权理由<input value={reason} onChange={(event) => setReason(event.target.value)} placeholder="填写业务依据" /></label><button className="primary-button" onClick={() => mutation.mutate()} disabled={!userId || !campaignId || !reason.trim() || mutation.isPending || (current?.access_level === "EDIT" && level === "VIEW")}>保存授权</button></div>{current?.access_level === "EDIT" && level === "VIEW" && <p className="inline-note">禁止隐式 EDIT → VIEW 降级；请先明确撤销，再重新授权。</p>}<div className="grant-list">{grants.data?.length ? grants.data.map((grant) => <div className="grant-row" key={grant.campaign_id}><div><strong>{grant.campaign_code}</strong><span>{grant.campaign_name}</span></div><span className={`access-pill ${grant.access_level.toLowerCase()}`}>{grant.access_level}</span><time>{formatDate(grant.granted_at)}</time><button className="reject-button" onClick={() => revokeGrant(grant.campaign_id)} disabled={revoke.isPending}>撤销</button></div>) : <p className="empty-line">{userId ? "该用户暂无直接授权。" : "请选择用户查看授权。"}</p>}</div><ErrorLine error={users.error ?? campaigns.error ?? grants.error ?? mutation.error ?? revoke.error} /></section>;
}

function AuditPanel() {
  const events = useQuery({ queryKey: ["admin-audit"], queryFn: ({ signal }) => adminApi.audit(200, 0, signal), refetchOnWindowFocus: true });
  return <section><div className="panel-heading"><div><h2>安全审计</h2><p>最近 200 条事件；前端不会渲染 metadata、密码、Cookie 或令牌。</p></div><span className="count-badge">{events.data?.length ?? 0}</span></div><div className="audit-table"><div className="table-head"><span>事件 / 结果</span><span>操作者</span><span>请求证据</span><span>时间</span></div>{events.data?.map((event) => <div className="audit-row" key={event.id}><div><strong>{event.event_type}</strong><span className={`outcome ${event.outcome.toLowerCase()}`}>{event.outcome}</span></div><span>{event.actor_username || (event.actor_id ? event.username : "匿名 / 系统")}</span><div><code>{event.request_id || "—"}</code><small>{event.source_ip || "—"}</small></div><time>{formatDate(event.created_at)}</time></div>)}</div><ErrorLine error={events.error} /></section>;
}
