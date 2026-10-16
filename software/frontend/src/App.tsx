import { lazy, Suspense, useEffect, useRef, useState } from "react";
import {
  Boxes,
  ChevronDown,
  Clock3,
  Gauge,
  LayoutDashboard,
  LogOut,
  Maximize2,
  Minimize2,
  Moon,
  Activity,
  Monitor,
  ChartNoAxesCombined,
  ShieldCheck,
  Sun
} from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { useAuth } from "./auth";
import { HomePage } from "./pages/HomePage";
import { AuthLoading, LoginScreen, PasswordChangeScreen } from "./pages/AuthScreens";
import { EmptyState, LoadingState } from "./components/Workbench";
import {
  locationHref,
  readLocation,
  type LocationState,
  type View
} from "./navigation";
import type { AssetKind, TargetPartCode } from "./types";
import { adminApi } from "./securityApi";
import { roleLabels } from "./security";

const CatalogPage = lazy(() => import("./pages/SetupPages").then((module) => ({ default: module.CatalogPage })));
const PlanningPage = lazy(() => import("./pages/SetupPages").then((module) => ({ default: module.PlanningPage })));
const AccountPage = lazy(() => import("./pages/SecurityPages").then((module) => ({ default: module.AccountPage })));
const AccessRequestPage = lazy(() => import("./pages/SecurityPages").then((module) => ({ default: module.AccessRequestPage })));
const AdminWorkspace = lazy(() => import("./pages/SecurityPages").then((module) => ({ default: module.AdminWorkspace })));
const AssetDetailPage = lazy(() => import("./pages/ReadModelPages").then((module) => ({ default: module.AssetDetailPage })));
const AssetsPage = lazy(() => import("./pages/ReadModelPages").then((module) => ({ default: module.AssetsPage })));
const DurationsPage = lazy(() => import("./pages/ReadModelPages").then((module) => ({ default: module.DurationsPage })));
const ExecutionDetailPage = lazy(() => import("./pages/ReadModelPages").then((module) => ({ default: module.ExecutionDetailPage })));
const MtbfPage = lazy(() => import("./pages/ReadModelPages").then((module) => ({ default: module.MtbfPage })));
const TestCaseDetailPage = lazy(() => import("./pages/ReadModelPages").then((module) => ({ default: module.TestCaseDetailPage })));
const LiveRunsPage = lazy(() => import("./pages/LiveRunsPage"));
const HmiStationsPage = lazy(() => import("./pages/HmiStationsPage"));
const SubmissionsPage = lazy(() => import("./pages/SubmissionsPage"));
const CsvImportPage = lazy(() => import("./pages/CsvImportPage"));
const ArchivesPage = lazy(() => import("./pages/ArchivesPage"));
const ArchiveDetailPage = lazy(() => import("./pages/ArchivesPage").then((module) => ({ default: module.ArchiveDetailPage })));
const CurveAnalysisPage = lazy(() => import("./pages/CurveAnalysisPage"));

const navItems: Array<{ view: View; label: string; icon: typeof LayoutDashboard }> = [
  { view: "catalog", label: "测试目录", icon: Boxes },
  { view: "planning", label: "测试计划", icon: Clock3 },
  { view: "home", label: "测试总控", icon: LayoutDashboard },
  { view: "durations", label: "时长台账", icon: Clock3 },
  { view: "mtbf", label: "MTBF 结论", icon: Gauge },
  { view: "assets", label: "样品中心", icon: Boxes },
  { view: "live", label: "正在运行", icon: Activity },
  { view: "stations", label: "工位监控", icon: Monitor },
  { view: "archives", label: "数据归档", icon: ChartNoAxesCombined }
];

function initialTheme(): "day" | "night" {
  const requested = new URLSearchParams(window.location.search).get("theme");
  if (requested === "day" || requested === "night") return requested;
  const saved = window.localStorage.getItem("rp1-theme");
  if (saved === "day" || saved === "night") return saved;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "night" : "day";
}

export default function App() {
  const { status, session, logout } = useAuth();
  const [location, setLocation] = useState<LocationState>(() => readLocation());
  const [theme, setTheme] = useState<"day" | "night">(initialTheme);
  const [accountMenuOpen, setAccountMenuOpen] = useState(false);
  const [fullscreen, setFullscreen] = useState(() => Boolean(document.fullscreenElement));
  const fullscreenAvailable = typeof document.documentElement.requestFullscreen === "function";
  const accountMenuRef = useRef<HTMLDivElement>(null);
  const accountTriggerRef = useRef<HTMLButtonElement>(null);
  const pending = useQuery({
    queryKey: ["admin-pending"],
    queryFn: ({ signal }) => adminApi.requests("PENDING", signal),
    enabled: status === "authenticated" && session?.user.role === "SYSTEM_ADMIN" && !session.user.must_change_password,
    refetchInterval: 30_000,
    refetchOnWindowFocus: true
  });

  useEffect(() => {
    const onPopState = () => setLocation(readLocation());
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    window.localStorage.setItem("rp1-theme", theme);
  }, [theme]);

  useEffect(() => {
    const syncFullscreen = () => setFullscreen(Boolean(document.fullscreenElement));
    document.addEventListener("fullscreenchange", syncFullscreen);
    return () => document.removeEventListener("fullscreenchange", syncFullscreen);
  }, []);

  useEffect(() => {
    setAccountMenuOpen(false);
    const frame = window.requestAnimationFrame(() => {
      const heading = document.querySelector<HTMLElement>("[data-page-heading]");
      (heading ?? document.getElementById("main-content"))?.focus();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [location]);

  useEffect(() => {
    if (!accountMenuOpen) return;
    const frame = window.requestAnimationFrame(() => {
      accountMenuRef.current?.querySelector<HTMLElement>("[role='menuitem']")?.focus();
    });
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!accountMenuRef.current?.contains(event.target as Node)) setAccountMenuOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setAccountMenuOpen(false);
      accountTriggerRef.current?.focus();
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      window.cancelAnimationFrame(frame);
      document.removeEventListener("pointerdown", closeOnOutsidePointer);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [accountMenuOpen]);

  function go(
    view: View,
    id: string | null = null,
    kind = location.kind,
    part: TargetPartCode | null = null
  ) {
    const next = { view, id, kind, part };
    window.history.pushState({ rp1Navigation: true }, "", locationHref(next));
    setLocation(next);
  }

  function switchKind(kind: AssetKind) {
    const detail = ["asset", "test-case", "execution"].includes(location.view);
    go(detail ? "assets" : location.view, null, kind);
  }

  async function toggleFullscreen() {
    try {
      if (document.fullscreenElement) {
        await document.exitFullscreen();
      } else {
        await document.documentElement.requestFullscreen();
      }
    } catch {
      setFullscreen(Boolean(document.fullscreenElement));
    }
  }

  const navView = location.view === "asset" ? "assets" : ["archive", "curves", "import"].includes(location.view) ? "archives" : location.view;

  if (status === "booting") return <AuthLoading />;
  if (status === "anonymous") return <LoginScreen />;
  if (session?.user.must_change_password) return <PasswordChangeScreen />;

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">跳到主要内容</a>
      <header className="app-header">
        <button className="brand" type="button" onClick={() => go("home")} aria-label="返回测试总控首页">
          <img src="/assets/roboparty-logo.png" alt="RoboParty" width="122" height="42" />
          <span><strong>RP1 可靠性测试平台</strong><small>RELIABILITY CONTROL</small></span>
        </button>
        <div className="object-switch" role="group" aria-label="测试对象类型">
          <button type="button" className={location.kind === "WHOLE_MACHINE" ? "active" : ""} aria-pressed={location.kind === "WHOLE_MACHINE"} onClick={() => switchKind("WHOLE_MACHINE")}>整机</button>
          <button type="button" className={location.kind === "MODULE" ? "active" : ""} aria-pressed={location.kind === "MODULE"} onClick={() => switchKind("MODULE")}>模块</button>
        </div>
        <nav className="primary-nav" aria-label="主要页面">
          {[...navItems, ...(session?.user.role !== "VIEWER" ? [{ view: "submissions" as View, label: "测试数据审批", icon: ShieldCheck }] : [])].map((item) => {
            const Icon = item.icon;
            const active = navView === item.view;
            return <button key={item.view} type="button" className={active ? "active" : ""} aria-current={active ? "page" : undefined} onClick={() => go(item.view)}><Icon aria-hidden="true" />{item.label}</button>;
          })}
        </nav>
        <div className="header-actions">
          {session?.user.role === "SYSTEM_ADMIN" && (
            <button className="admin-entry" type="button" onClick={() => go("admin")}>
              <ShieldCheck aria-hidden="true" />管理工作台
              {(pending.data?.length ?? 0) > 0 && <span>{pending.data?.length}</span>}
            </button>
          )}
          {fullscreenAvailable && (
            <button
              className="icon-button"
              type="button"
              onClick={() => void toggleFullscreen()}
              aria-label={fullscreen ? "退出全屏" : "进入全屏"}
              title={fullscreen ? "退出全屏" : "进入全屏"}
            >
              {fullscreen ? <Minimize2 aria-hidden="true" /> : <Maximize2 aria-hidden="true" />}
            </button>
          )}
          <button className="icon-button" type="button" onClick={() => setTheme((value) => value === "day" ? "night" : "day")} aria-label={theme === "day" ? "切换到夜间模式" : "切换到日间模式"}>
            {theme === "day" ? <Moon aria-hidden="true" /> : <Sun aria-hidden="true" />}
          </button>
          <div className="account-menu" ref={accountMenuRef}>
            <button
              ref={accountTriggerRef}
              className="account-trigger"
              type="button"
              aria-haspopup="menu"
              aria-expanded={accountMenuOpen}
              onClick={() => setAccountMenuOpen((value) => !value)}
              onKeyDown={(event) => {
                if (event.key === "ArrowDown") {
                  event.preventDefault();
                  setAccountMenuOpen(true);
                }
              }}
            >
              <span><strong>{session?.user.display_name}</strong><small>{roleLabels[session!.user.role]}</small></span><ChevronDown aria-hidden="true" />
            </button>
            {accountMenuOpen && (
              <div
                className="account-popover"
                role="menu"
                aria-label="账户操作"
                onKeyDown={(event) => {
                  if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
                  event.preventDefault();
                  const items = [...event.currentTarget.querySelectorAll<HTMLElement>("[role='menuitem']")];
                  const current = items.indexOf(document.activeElement as HTMLElement);
                  const next = event.key === "Home"
                    ? 0
                    : event.key === "End"
                      ? items.length - 1
                      : (current + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
                  items[next]?.focus();
                }}
              >
                <div role="presentation"><strong>{session?.user.display_name}</strong><span>@{session?.user.username}</span><small>{roleLabels[session!.user.role]}</small></div>
                <button role="menuitem" type="button" onClick={() => go("account")}>账户设置</button>
                <button role="menuitem" type="button" onClick={() => go("access-request")}>权限申请</button>
                <button role="menuitem" type="button" onClick={() => void logout()}><LogOut aria-hidden="true" />退出</button>
              </div>
            )}
          </div>
        </div>
      </header>
      <main id="main-content" tabIndex={-1}>
        <Suspense fallback={<LoadingState label="正在加载页面" />}>
        {location.view === "catalog" && <CatalogPage assetKind={location.kind} navigate={(id) => go("test-case", id)} />}
        {location.view === "planning" && <PlanningPage key={location.kind} assetKind={location.kind} />}
        {location.view === "home" && <HomePage assetKind={location.kind} navigate={(view, id, part) => go(view, id ?? null, location.kind, part ?? null)} />}
        {location.view === "durations" && <DurationsPage assetKind={location.kind} navigate={(view, id) => go(view, id ?? null)} />}
        {location.view === "mtbf" && <MtbfPage assetKind={location.kind} />}
        {location.view === "assets" && <AssetsPage assetKind={location.kind} part={location.part} selectPart={(nextPart) => go("assets", null, location.kind, nextPart)} clearPart={() => go("assets", null, location.kind)} navigate={(view, id) => go(view, id ?? null)} />}
        {location.view === "asset" && location.id && <AssetDetailPage id={location.id} navigate={(view, id) => go(view, id ?? null)} />}
        {location.view === "test-case" && location.id && <TestCaseDetailPage id={location.id} navigate={(view, id) => go(view, id ?? null)} />}
        {location.view === "execution" && location.id && <ExecutionDetailPage id={location.id} navigate={(view, id) => go(view, id ?? null)} />}
        {location.view === "account" && <AccountPage />}
        {location.view === "live" && <LiveRunsPage navigate={(view, id) => go(view, id ?? null)} />}
        {location.view === "stations" && <HmiStationsPage />}
        {location.view === "submissions" && <SubmissionsPage navigate={(id) => go("execution", id)} navigateArchive={(id) => go("archive", id)} />}
        {location.view === "archives" && <ArchivesPage navigate={(view, id) => go(view, id ?? null)} />}
        {location.view === "import" && <CsvImportPage navigate={(view, id) => go(view, id ?? null)} />}
        {location.view === "archive" && location.id && <ArchiveDetailPage id={location.id} navigate={(view, id) => go(view, id ?? null)} />}
        {location.view === "curves" && location.id && <CurveAnalysisPage id={location.id} navigate={(view, id) => go(view, id)} />}
        {location.view === "access-request" && <AccessRequestPage />}
        {location.view === "admin" && <AdminWorkspace />}
        {["asset", "test-case", "execution", "archive", "curves"].includes(location.view) && !location.id && (
          <EmptyState title="页面地址缺少业务标识" description="请通过样品、Test Case 或 Execution 列表重新进入详情。" />
        )}
        </Suspense>
      </main>
    </div>
  );
}
