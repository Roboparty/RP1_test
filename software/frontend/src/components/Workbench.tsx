import { AlertTriangle, Database, RefreshCw } from "lucide-react";
import type { ReactNode } from "react";
import { formatDate } from "../navigation";
import type { Freshness } from "../types";
import { StatusMark } from "./StatusMark";

export function PageTitle({
  eyebrow,
  title,
  description,
  trailing
}: {
  eyebrow: string;
  title: string;
  description?: string;
  trailing?: ReactNode;
}) {
  return (
    <header className="page-title">
      <div>
        <span>{eyebrow}</span>
        <h1 data-page-heading tabIndex={-1}>{title}</h1>
        {description && <p>{description}</p>}
      </div>
      {trailing}
    </header>
  );
}

export function Panel({
  eyebrow,
  title,
  description,
  trailing,
  children,
  className = ""
}: {
  eyebrow?: string;
  title: string;
  description?: string;
  trailing?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      <header className="panel-head">
        <div>
          {eyebrow && <span>{eyebrow}</span>}
          <h2>{title}</h2>
          {description && <p>{description}</p>}
        </div>
        {trailing}
      </header>
      {children}
    </section>
  );
}

export function LoadingState({ label = "正在加载业务数据" }: { label?: string }) {
  return (
    <div className="skeleton-stack" aria-busy="true" aria-label={label}>
      <i />
      <i />
      <i />
    </div>
  );
}

export function ErrorState({
  error,
  retry
}: {
  error: unknown;
  retry: () => void;
}) {
  return (
    <section className="state-panel error-state" role="alert">
      <AlertTriangle aria-hidden="true" />
      <h2>业务读取失败</h2>
      <p>{error instanceof Error ? error.message : "服务暂时不可用。"}</p>
      <button type="button" onClick={retry}>
        <RefreshCw aria-hidden="true" />
        重新加载
      </button>
    </section>
  );
}

export function EmptyState({
  title,
  description
}: {
  title: string;
  description: string;
}) {
  return (
    <div className="empty-state">
      <Database aria-hidden="true" />
      <div>
        <h3>{title}</h3>
        <p>{description}</p>
      </div>
      <span>NO FABRICATED DATA</span>
    </div>
  );
}

export function FreshnessMark({ freshness }: { freshness: Freshness }) {
  const tone =
    freshness.status === "FRESH"
      ? "normal"
      : freshness.status === "STALE"
        ? "warning"
        : "muted";
  const label =
    freshness.status === "FRESH"
      ? `数据新鲜 · 事实截止 ${formatDate(freshness.data_cutoff_at)}`
      : freshness.status === "STALE"
        ? `同步延迟 · 事实截止 ${formatDate(freshness.data_cutoff_at)}`
        : `尚无接入数据 · 查询于 ${formatDate(freshness.as_of_at)}`;
  return <StatusMark tone={tone} label={label} shape="clock" />;
}

export function Cutoff({
  value,
  asOf
}: {
  value: string | null | undefined;
  asOf?: string | null;
}) {
  return (
    <small className="cutoff">
      事实截止：{value ? formatDate(value) : "尚无事实"}
      {asOf ? ` · 查询于 ${formatDate(asOf)}` : ""}
    </small>
  );
}

export function ShowcaseBadge() {
  return (
    <span className="showcase-badge" aria-label="当前页面包含开发环境示例数据">
      <strong>SHOWCASE</strong>
      示例数据
    </span>
  );
}

export function LoadMore({
  hasMore,
  loading,
  failed,
  onLoad
}: {
  hasMore: boolean;
  loading: boolean;
  failed: boolean;
  onLoad: () => void;
}) {
  return (
    <div className="load-more" aria-live="polite">
      {failed && <span role="alert">追加加载失败，已保留当前记录。</span>}
      <button type="button" onClick={onLoad} disabled={!hasMore || loading}>
        {loading ? "正在加载…" : failed ? "重试加载更多" : hasMore ? "加载更多" : "已加载全部"}
      </button>
    </div>
  );
}
