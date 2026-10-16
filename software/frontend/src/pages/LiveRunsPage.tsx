import { useQuery } from "@tanstack/react-query";
import { ExternalLink, RefreshCw } from "lucide-react";
import { getLiveExecutions } from "../api";
import { EmptyState, ErrorState, LoadingState, PageTitle, Panel } from "../components/Workbench";
import { StatusMark } from "../components/StatusMark";
import { formatDate, formatDuration, type View } from "../navigation";

export default function LiveRunsPage({
  navigate
}: {
  navigate: (view: View, id?: string) => void;
}) {
  const query = useQuery({
    queryKey: ["live-executions"],
    queryFn: ({ signal }) => getLiveExecutions(signal),
    refetchInterval: 5000
  });
  const items = query.data?.items ?? [];

  return (
    <>
      <PageTitle
        eyebrow="LIVE EXECUTIONS"
        title="正在运行的测试"
        description="集中列出最近仍有上位机心跳的正式执行。单号是数据库生成的执行编号。"
        trailing={
          <button type="button" onClick={() => void query.refetch()} disabled={query.isFetching}>
            <RefreshCw aria-hidden="true" />刷新
          </button>
        }
      />
      {query.isLoading ? <LoadingState /> : query.isError ? (
        <ErrorState error={query.error} retry={() => query.refetch()} />
      ) : items.length === 0 ? (
        <EmptyState title="当前没有正在运行的测试" description="上位机开始正式测试并保持心跳后，执行会出现在这里。" />
      ) : (
        <Panel title={`${items.length} 项正在运行`}>
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>执行编号</th>
                  <th>用例</th>
                  <th>样品</th>
                  <th>工位</th>
                  <th>状态</th>
                  <th>开始</th>
                  <th>已运行</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.id}>
                    <td>
                      <button className="link-button" type="button" onClick={() => navigate("execution", item.id)}>
                        {item.code}<ExternalLink aria-hidden="true" />
                      </button>
                    </td>
                    <td>
                      <button className="link-button" type="button" onClick={() => navigate("test-case", item.test_case_code)}>
                        {item.test_case_code}
                      </button>
                      <small>{item.test_case_name}</small>
                    </td>
                    <td>
                      <button className="link-button" type="button" onClick={() => navigate("asset", item.asset_code)}>
                        {item.asset_code}
                      </button>
                    </td>
                    <td>{item.station_code || "—"}</td>
                    <td><StatusMark tone="normal" label="运行中" /></td>
                    <td>{formatDate(item.started_at)}</td>
                    <td>{formatDuration(item.active_elapsed_seconds)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      )}
      <p className="semantic-note">这里只显示仍在心跳窗口内的正式执行。工位监控里的文件名是工位本地记录名，不作为这里的执行编号。</p>
    </>
  );
}
