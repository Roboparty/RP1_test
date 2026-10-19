# 独立遥测演示

本目录提供 SERVICE key / ingestion → PostgreSQL → InfluxDB → Grafana 的合成演示。
API、数据库迁移、凭据、数据卷和端口独立于当前 `reliability_v1` 平台。
旧 ingestion 只用于此演示，不能代替正式 CSV 归档、核实、审批与正式统计。
当前平台和 PLC HMI 从[软件主入口](../../README.md)进入。

## 启动和停止

从仓库根目录执行，需安装 Docker Compose：

```bash
cd software/examples/telemetry-demo
sh scripts/start.sh
sh scripts/run-demo.sh
python3 scripts/verify_stack.py --demo
sh scripts/stop.sh
```

默认项目名为 `rp1-test-telemetry-demo`；API、Grafana、InfluxDB 分别绑定回环地址的
18080、13000、18086 端口，PostgreSQL 为 15432。首次启动生成本目录的 `.env`，
不复制平台 `.env`；凭据已存在时保留原值。停止脚本保留数据卷。

演示初始化生成合成上下文和专用服务凭据，保存为 `.runtime/context.json`；
合成客户端将 CSV、事件及配置快照写入 `.runtime/runs/`，再通过本演示 API 上传
降采样指标与文件元数据。原始文件保留本地，合成结果始终为 `INCONCLUSIVE`。
网络失败时 SQLite 队列保留请求，后续 `flush` 重试相同幂等键。

## 模块

| 目录 | 用途 |
|---|---|
| [service](service/README.md) | 独立演示 API 与导出 Worker |
| [database](database/README.md) | 演示数据库迁移和合成上下文 |
| [demo-client](demo-client/README.md) | 合成双关节记录与离线补传 |
| [influxdb](influxdb/README.md) | 演示时序数据初始化、写入与查询 |
| [grafana](grafana/README.md) | 演示曲线与仪表盘配置 |
| [../../tools/test-runner](../../tools/test-runner/README.md) | 客户端复用的可选命令行工具 |

离线检查与容器联调由[测试工作流](../../../.github/workflows/tests.yml)执行。
这些检查验证软件契约和合成数据链路，不代表真实 PLC/CAN 或长期稳定性验收。
