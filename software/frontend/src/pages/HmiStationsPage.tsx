import { useEffect, useState } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { Monitor, RefreshCw } from "lucide-react";
import { useAuth } from "../auth";
import { getHmiStations, isStationTesting, runLabels, stationLabels, type HmiStation } from "../hmiPresence";
import { formatDate, formatDuration } from "../navigation";
import { EmptyState, ErrorState, LoadingState, PageTitle } from "../components/Workbench";

const limbLabels: Record<string, string> = {
  left_arm: "左臂（7自由度）",
  right_arm: "右臂（7自由度）",
  left_short_arm: "左臂（5自由度）",
  right_short_arm: "右臂（5自由度）",
  left_leg: "左腿（6自由度）",
  right_leg: "右腿（6自由度）",
  left_short_leg: "左腿（5自由度）",
  right_short_leg: "右腿（5自由度）",
  upper_body: "上肢",
  lower_body: "下肢",
  waist_hip: "腰髋",
  biped_waist: "双足腰"
};

function limbLabel(value: string) {
  return limbLabels[value] || value || "—";
}

export function HmiStationCard({ station, live = true }: { station: HmiStation; live?: boolean }) {
  const current = live && station.online && station.snapshot_fresh;
  const label = live ? stationLabels[station.state] : "历史快照";
  return (
    <article className="hmi-station-card" aria-label={station.station_name}>
      <header>
        <div className="hmi-station-identity"><Monitor aria-hidden="true" /><div><h2>{station.station_name}</h2><span>{station.bench_id || "工位编号未设置"}</span></div></div>
        <span className={`hmi-state hmi-state-${live ? station.state : "unknown"}`}>{label}</span>
      </header>
      <dl className="hmi-station-meta">
        <div><dt>登录账号</dt><dd>{station.operator_display_name} <span>({station.operator_name})</span></dd></div>
        <div><dt>最近心跳</dt><dd>{formatDate(station.last_seen_at)}</dd></div>
        <div><dt>PLC 通信</dt><dd>{current ? station.plc.communication_state : "当前状态未知"}{station.plc.driver === "mock" && <span className="hmi-mock-label">模拟 PLC</span>}</dd></div>
        <div><dt>电柜状态</dt><dd>{current && !station.plc.stale ? station.plc.phase : "当前状态未知"}</dd></div>
      </dl>
      {!current && <p className="hmi-snapshot-note">以下为最后上报信息，不能据此判断设备当前是否仍在运行。</p>}
      <div className="hmi-runs">
        {station.runs.length ? station.runs.map((run) => {
          const official = run.execution_code || "";
          const localName = run.test_id && run.test_id !== official ? run.test_id : "";
          return (
          <section className="hmi-run" key={run.session_key}>
            <div className="hmi-run-heading"><strong>{official || run.test_id || "尚未开始测试"}</strong><span>{current ? "" : "最后状态："}{runLabels[run.state]}</span></div>
            {localName && <p className="hmi-snapshot-note">本地记录：{localName}</p>}
            <dl><div><dt>样品</dt><dd>{run.sample_id || "—"}</dd></div><div><dt>用例</dt><dd>{run.case_code || "—"}</dd></div><div><dt>总成</dt><dd>{limbLabel(run.test_case)}</dd></div><div><dt>已运行</dt><dd>{formatDuration(run.active_seconds)}</dd></div><div><dt>已完成循环</dt><dd>{run.completed_cycles}</dd></div></dl>
          </section>
          );
        }) : <p className="hmi-no-run">{current ? "上位机已登录，尚无测试会话。" : "最后快照没有测试会话。"}</p>}
      </div>
      <footer>状态采样：{formatDate(station.snapshot_at)} · 运行时长为上位机上报值</footer>
    </article>
  );
}

export default function HmiStationsPage() {
  const { session } = useAuth();
  const [automatic, setAutomatic] = useState(true);
  const [filter, setFilter] = useState("all");
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const query = useInfiniteQuery({
    queryKey: ["hmi-stations"],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) => getHmiStations(pageParam, signal),
    getNextPageParam: (page) => page.next_cursor,
    refetchInterval: automatic ? 5000 : false,
    refetchOnWindowFocus: true,
    retry: 1
  });
  const stations = [...new Map((query.data?.pages.flatMap((page) => page.items) ?? []).map((station) => [station.installation_id, station])).values()];
  const visible = stations.filter((station) => filter === "all" || (filter === "online" ? station.online : filter === "running" ? isStationTesting(station) : station.state === filter));
  const currentResponse = now - query.dataUpdatedAt < 15000;
  const healthy = !query.isError && automatic && currentResponse;
  const latest = query.data?.pages[0]?.as_of_at;
  return (
    <div className="workbench hmi-stations-page">
      <PageTitle eyebrow="WORKSTATION MONITOR" title="工位监控" description={session?.user.role === "SYSTEM_ADMIN" ? "查看全部上位机的在线状态与当前测试。" : "查看使用当前账号登录的上位机。"}
        trailing={<button className="subtle-button" type="button" onClick={() => void query.refetch()} disabled={query.isFetching}><RefreshCw aria-hidden="true" />{query.isFetching ? "刷新中…" : "立即刷新"}</button>} />
      <p className="hmi-scope-note">此页展示实时运行状态，不计入正式测试结果或审批后的统计。原始 CSV 保存在上位机本地。</p>
      <section className="hmi-monitor-toolbar" aria-label="监控筛选">
        <label>显示工位<select value={filter} onChange={(event) => setFilter(event.target.value)}><option value="all">全部</option><option value="online">在线</option><option value="running">测试运行中</option><option value="offline">离线</option></select></label>
        <label className="hmi-auto-refresh"><input type="checkbox" checked={automatic} onChange={(event) => setAutomatic(event.target.checked)} />每 5 秒自动刷新</label>
        <p>{healthy ? `${stations.filter((station) => station.online).length} 台在线 · ${stations.filter(isStationTesting).length} 台测试运行中` : "当前显示历史快照"}<span>已加载 {stations.length} 台 · 最近读取：{formatDate(latest)}</span></p>
      </section>
      {query.isPending ? <LoadingState label="正在读取上位机状态" /> : query.isError ? <ErrorState error={query.error} retry={() => void query.refetch()} /> : (
        <>
          {!automatic && <p className="hmi-snapshot-note" role="status">自动刷新已暂停，设备可能已改变状态。</p>}
          {automatic && !currentResponse && <p className="hmi-snapshot-note" role="status">页面暂未获取最新状态，以下为历史快照。</p>}
          {visible.length ? <div className="hmi-station-grid">{visible.map((station) => <HmiStationCard key={station.installation_id} station={station} live={healthy} />)}</div> : <EmptyState title={stations.length ? "没有符合筛选条件的工位" : "暂无上位机连接记录"} description="在已升级的 RP1 TEST HMI（PLC）中登录平台账号后，工位会自动出现在此处。" />}
          {query.hasNextPage && <button type="button" className="subtle-button" disabled={query.isFetchingNextPage} onClick={() => void query.fetchNextPage()}>加载更多工位</button>}
        </>
      )}
      <p className="hmi-monitor-footnote">上位机每 10 秒上报；超过 45 秒未收到心跳视为离线。状态采样超过 20 秒未更新时显示“状态待确认”。电柜供电不代表测试已开始。</p>
    </div>
  );
}
