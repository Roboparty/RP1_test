# 可选工具与演示遥测接入

## 数据链路

```text
轨迹执行 / 合成演示 → CSV、JSONL、SQLite outbox
    → 独立演示 ingestion API → 演示 PostgreSQL
    → 导出 Worker → InfluxDB → Grafana
```

部署见[独立遥测演示](../../../examples/telemetry-demo/README.md)。
本工具的 `platform` 命令使用旧 `/api/v1/ingestion`，不适配当前 `reliability_v1`。
当前平台的正式数据通过专用 CSV 归档、核实、审批流程进入正式查询，见[数据流程](../../../docs/data-workflow.md)。

## 模块职责

| 模块 | 入口与用途 |
|---|---|
| [test-runner](../README.md) | 轨迹校验、仿真与设备回放、反馈 watchdog、运行落盘和演示补传 |
| [Factory HMI](../../../host/README.md) | 当前平台桌面操作、PLC/CAN 本机控制和本地 CSV 记录；不依赖本工具 |
| [demo-client](../../../examples/telemetry-demo/demo-client/README.md) | 合成双关节记录、暂停区间、有效时间和演示补传 |
| [演示 Service](../../../examples/telemetry-demo/service/README.md) | 接收演示执行、事件、遥测和文件元数据，导出时序数据 |

## 采集与展示

- 目标/实际位置、速度，以及设备可提供的力矩、温度；演示 API 单位为 deg、deg/s、N·m、°C。
- 跟随误差取同一行 `actual − target`，保留源列与公式；任一位置缺失或非有限时不生成该点。
- SQLite 保存幂等键和服务器 stage ID，按 create、events、telemetry、artifacts、finish 顺序补传。
- 原始文件保存在本地；演示仅登记文件名、大小和 SHA-256，使用 `MISSING` 状态，不提供原始文件归档或下载。
- `test-runner` 在运行结束后批量入队，Grafana 的刷新周期不改变采集端的发送时机。

合成记录标记 `synthetic`，结论为 `INCONCLUSIVE`。缺失传感量保留缺失状态；
SERVICE 密钥只在演示采集端配置，不进入浏览器和运行快照。
