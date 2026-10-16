import type { JointRecordSummary } from "../types";
import { EmptyState, Panel } from "./Workbench";

function quantity(value: number | null, digits = 2) {
  if (value === null || !Number.isFinite(value)) return "—";
  return value.toLocaleString("zh-CN", { maximumFractionDigits: digits, minimumFractionDigits: digits });
}

function temperaturePoints(record: JointRecordSummary) {
  return [
    { label: "起始", value: record.temp_start_c },
    { label: "最高", value: record.temp_max_c },
    { label: "结束", value: record.temp_end_c }
  ].filter((point): point is { label: string; value: number } => point.value !== null && Number.isFinite(point.value));
}

function TemperatureSummary({ record }: { record: JointRecordSummary }) {
  const points = temperaturePoints(record);
  if (points.length < 2) return null;
  const values = points.map((point) => point.value);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const width = 220;
  const height = 72;
  const step = points.length === 1 ? 0 : (width - 24) / (points.length - 1);
  const coords = points.map((point, index) => {
    const x = 12 + index * step;
    const y = 12 + (1 - (point.value - min) / span) * (height - 28);
    return { ...point, x, y };
  });
  const path = coords.map((point, index) => `${index === 0 ? "M" : "L"}${point.x.toFixed(1)} ${point.y.toFixed(1)}`).join(" ");
  return (
    <figure className="joint-temp-summary">
      <figcaption>{record.joint} · 温度汇总点</figcaption>
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${record.joint} 起始、最高、结束温度`}>
        <path d={path} />
        {coords.map((point) => <circle key={point.label} cx={point.x} cy={point.y} r="3" />)}
      </svg>
      <ol>
        {coords.map((point) => <li key={point.label}>{point.label} {quantity(point.value, 1)} °C</li>)}
      </ol>
    </figure>
  );
}

export function JointRecordPanel({ records }: { records: JointRecordSummary[] }) {
  return (
    <Panel title="关节汇总量" eyebrow="UNREVIEWED JOINT SUMMARY">
      <p className="joint-record-note">数值来自旧 PostgreSQL 库 robot_test_meta 的关节汇总。温升图只连接起始、最高和结束三个点，不是工位上的采集曲线。这些数值未审核，不进入 MTBF。</p>
      {records.length === 0 ? (
        <EmptyState title="这条执行还没有关节汇总" description="平台没有这条编号对应的力矩、电气量和温升汇总。采集曲线仍保存在工位，不会整段转入平台。" />
      ) : (
        <>
          <div className="table-scroll">
            <table className="data-table joint-record-table">
              <thead>
                <tr>
                  <th>关节</th>
                  <th>平均力矩</th>
                  <th>峰值力矩</th>
                  <th>平均电压</th>
                  <th>电压范围</th>
                  <th>平均电流</th>
                  <th>峰值电流</th>
                  <th>估算功耗</th>
                  <th>温升</th>
                  <th>跟踪 RMS</th>
                  <th>样本</th>
                </tr>
              </thead>
              <tbody>
                {records.map((record) => (
                  <tr key={record.joint}>
                    <td>{record.joint}</td>
                    <td>{quantity(record.mean_abs_torque_nm)} N·m</td>
                    <td>{quantity(record.peak_abs_torque_nm)} N·m</td>
                    <td>{quantity(record.bus_voltage_mean_v)} V</td>
                    <td>{quantity(record.bus_voltage_min_v, 1)} – {quantity(record.bus_voltage_max_v, 1)} V</td>
                    <td>{quantity(record.bus_current_mean_abs_a)} A</td>
                    <td>{quantity(record.bus_current_peak_abs_a)} A</td>
                    <td>{quantity(record.estimated_mean_power_w)} W</td>
                    <td>{quantity(record.temp_rise_c, 1)} °C</td>
                    <td>{quantity(record.tracking_rms_deg)} °</td>
                    <td>{record.sample_count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="joint-temp-grid">
            {records.map((record) => <TemperatureSummary key={`${record.source_kind}-${record.joint}-temp`} record={record} />)}
          </div>
        </>
      )}
    </Panel>
  );
}
