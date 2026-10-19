# 系统架构

RP1 Test 包含台架、测试方法、PLC/CAN 上位机和可靠性平台。当前平台协议为 `reliability_v1`。

```text
工位 PC：HMI ── Modbus TCP ── PLC / 电柜
            ├── SocketCAN ── 电机
            ├── 本地 CSV / 报告 / SQLite 队列
            └── HTTPS ── Web / FastAPI
                           ├── PostgreSQL：身份、索引、版本、审批与统计
                           ├── MTBF Worker：暴露、故障组与计算快照
                           └── 归档 Worker ── 文件卷：原件与派生文件
```

## 控制与数据

设备控制留在工位。桌面通过本机网关操作电机和 PLC，多窗口共享同一 PLC 状态机、指令映像及回执；两台独立网关不会自动协调对同一 PLC 的写入。

PLC 控制需要 Host Protocol V1.2，V1.0/V1.1 仅监控。命令接受、实际输出确认和通信健康是不同状态；反馈过期、锁态或故障阻止准入。软件停止不代替物理急停，见 [PLC 对接](../software/host/docs/plc-integration.md)。

原件在工位保存，核实并显式提交后归档。文件不由 Web 静态公开；数据库保存身份、权限、版本和审批。测量事实不通过应用接口改写，核实和重新分析追加版本，见 [数据流程](../software/docs/data-workflow.md)。
