# RP1 可靠性平台与 PLC 上位机

React 测试大屏、FastAPI、PostgreSQL、MTBF/归档 Worker 和 PySide6 PLC/CAN 上位机，平台协议为 `reliability_v1`。

仓库版本 **0.2.0**，平台组件 **2.0.6**，HMI 包 **2.0.9+ubuntu24.04**。原始导入来源由 [SOURCE-MANIFEST.json](SOURCE-MANIFEST.json) 固定，当前源码身份以 Git 提交为准。

## 快速启动

Ubuntu 24.04 amd64 准备 Docker Engine、Compose、Python 3 和 OpenSSL，在 `software/` 执行：

```bash
python3 scripts/configure.py
bash scripts/up.sh
python3 scripts/verify.py
```

默认入口 `https://localhost:8443`；浏览器和 HMI 需信任本站 CA，初始账号首次登录必须改密。已有安装按升级流程处理，不重复初始化。

## 核心模块

| 模块 | 职责 |
|---|---|
| [frontend](frontend/README.md) | 总控、样品/计划、时长/MTBF、工位、审批、归档与曲线 |
| [service](service/README.md) | 会话/权限、执行上下文、监控、归档、历史 CSV 导入和业务查询 |
| [database](database/README.md) | 角色、迁移、静态用例目录、版本与统计约束 |
| [host](host/README.md) | PLC/CAN 桌面、本机网关、控制、记录、核实与平台连接 |

## 操作与开发

- [安装与运维](docs/operations.md)：配置、升级、备份恢复和排障。
- [数据流程](docs/data-workflow.md)：本地记录、归档、核实、审批和正式查询。
- [HMI 构建](host/docs/build.md)：源码、离线运行与安装包。
- [架构](../docs/architecture.md) · [开发与验证](../docs/development.md)。

CSV 和报告先保存工位，核实并显式提交后集中归档，工位保留副本。在线监控、账号权限和数据审批相互独立；批准不自动形成合格 MTBF 暴露。

## 可选工具

[test-runner](tools/test-runner/README.md) 提供独立轨迹、仿真、回放与记录；[离线分析](../data-analysis/README.md) 分析本地运行目录。它们不属于平台/HMI 默认运行依赖。

[telemetry-demo](examples/telemetry-demo/README.md) 集中保存独立旧遥测服务、数据库、演示客户端、InfluxDB 和 Grafana。示例使用自己的凭据、端口和数据卷，其 ingestion 不能用于当前平台正式归档。
