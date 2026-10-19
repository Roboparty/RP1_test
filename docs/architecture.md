# 系统架构

RP1 Test 包含台架、测试方法、PLC/CAN 上位机、可靠性平台和离线分析。当前平台协议为 `reliability_v1`。

```text
工位 PC：HMI ── Modbus TCP ── PLC / 电柜
            ├── SocketCAN ── 电机
            ├── 本地 CSV / 报告 / SQLite 队列
            └── HTTPS ── Web / FastAPI
                           ├── PostgreSQL：身份、索引、版本、审批与统计
                           ├── MTBF Worker：暴露、故障组与计算快照
                           └── 归档 Worker ── 文件卷：原件与派生文件
```

| 目录 | 职责 |
|---|---|
| `hardware/` | 台架机械、电气接口和搭建说明 |
| `test-cases/` | 测试方法、共用要求、执行记录和执行前参数 |
| `software/frontend/` | 网页大屏、计划、审批、归档与曲线 |
| `software/service/` | API、会话、权限、业务查询及两类 Worker |
| `software/database/` | 角色、迁移、静态用例目录和数据库约束 |
| `software/host/` | 本机网关、桌面、PLC/CAN 控制、记录与同步 |
| `software/tools/test-runner/` | 可选命令行轨迹、仿真、回放和记录 |
| `software/examples/telemetry-demo/` | 独立旧遥测演示、数据库、客户端与 InfluxDB/Grafana |
| `data-analysis/` | 独立本地分析与合成样例 |

核心平台和 HMI 不依赖 test-runner；演示客户端依赖其采集协议和队列。演示数据库在 0023 后使用另一迁移链，其卷、凭据、端口与正式平台独立。

## 控制与数据

设备控制留在工位。桌面通过本机网关操作电机和 PLC，多窗口共享同一 PLC 状态机、指令映像及回执；两台独立网关不会自动协调对同一 PLC 的写入。

PLC 控制需要 Host Protocol V1.2，V1.0/V1.1 仅监控。命令接受、实际输出确认和通信健康是不同状态；反馈过期、锁态或故障阻止准入。软件停止不代替物理急停，见 [PLC 对接](../software/host/docs/plc-integration.md)。

原件在工位保存，核实并显式提交后归档。文件不由 Web 静态公开；数据库保存身份、权限、版本和审批。测量事实不通过应用接口改写，核实和重新分析追加版本，见 [数据流程](../software/docs/data-workflow.md)。

在线监控、账号权限审批和测试数据审批独立；上传不等于批准，批准不自动取得 MTBF 暴露资格。

## 版本与来源

根 `VERSION` 是仓库版本，`software/VERSION` 是平台组件版本，`software/host/packaging/LOCAL_VERSION` 是 HMI 包版本，分别管理。

`software/SOURCE-MANIFEST.json` 固定原始导入来源、摘要和目录映射，不是当前源码清单。当前源码身份使用 Git 提交，HMI 构建生成包内源码清单。许可见 [LICENSES](../LICENSES.md) 和 [第三方说明](../THIRD_PARTY.md)。

文档只保留现行使用说明、接口和规则。变更过程通过 Git、PR 和 CI 查询，源码树不增加计划、交接、实施过程或验收收据。
