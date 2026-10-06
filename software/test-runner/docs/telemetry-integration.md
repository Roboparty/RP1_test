# 遥测与平台接入

## 数据链路

```text
轨迹执行 / 合成演示 → CSV、JSONL、SQLite outbox
    → Service ingestion API → PostgreSQL
    → 导出 Worker → InfluxDB → Grafana
```

部署方式见[软件总览](../../README.md)。采集端调用 HTTP API；本地工具负责运动执行，Grafana 展示遥测。

## 模块职责

| 模块 | 入口与用途 |
|---|---|
| [test-runner](../README.md) | 轨迹校验、仿真与设备回放、反馈 watchdog、运行落盘和补传 |
| [Factory HMI](../../host/README.md) | 桌面操作、本机控制、模块老化、监控、标零和执行包；通过独立执行包桥接接入可靠性平台 |
| [demo-client](../../demo-client/README.md) | 合成双关节记录、暂停区间、有效时间和补传联调 |
| [Service](../../service/README.md) | 接收执行、事件、遥测、文件元数据和结论，持久化业务记录并导出时序数据 |

## 采集与展示

- 目标/实际位置、速度，以及设备可提供的力矩、温度；平台单位为 deg、deg/s、N·m、°C。
- 跟随误差取同一行 `actual − target`，保留源列与公式；任一位置缺失或非有限时不生成该点。
- SQLite 保存幂等键和服务器 stage ID，按 create、events、telemetry、artifacts、finish 顺序补传。
- 文件登记包含文件名、大小和 SHA-256；对象存储上传成功后才能标记文件可下载。
- `test-runner` 在运行结束后批量入队，Grafana 的刷新周期不改变采集端的发送时机。

## 扩展方式

| 需求 | 实现位置与约定 |
|---|---|
| 运行中持续展示 | 采集端增加周期 heartbeat 与小批次遥测发送，网络任务独立于控制循环 |
| 电流、母线电压、振动 | 增加真实传感器或 SDK getter，并注明单位、校准、时钟与设备能力 |
| 文件下载 | 接入对象存储、访问控制和下载链接，上传成功后更新 artifact 可用性 |
| 多阶段任务 | 按测试协议划分阶段，记录各阶段确认的执行区间 |
| 可靠性统计 | 使用审核的实测记录，明确暴露时间、故障定义及统计范围 |

合成记录标记 `synthetic`，结论为 `INCONCLUSIVE`。缺失传感量保留缺失状态；SERVICE 密钥只在采集端配置，不进入浏览器和运行快照。
