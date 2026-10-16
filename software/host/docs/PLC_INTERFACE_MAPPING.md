# PLC 接口映射表

> 当前协议：`LFD835 Linux 上位机通信协议 V1.0`。Linux 作为 Modbus TCP Client，PLC 作为 Server。下表地址均为 **0 起始 PDU 地址**，不叠加 `40001`。已冻结项已写入 `factory_hmi/config/plc_cabinet.yaml`；协议 V1.0 未提供的项仍保留 `TODO(PLC-V1-GAP)`，上位机不会用其他位冒充。

## 传输层

| 项目 | V1.0 值 | 实现状态 |
|---|---:|---|
| PLC IPv4 | `192.168.137.10` | 已配置；仅用于隔离控制网/VLAN |
| TCP 端口 | `502` | 已配置 |
| Unit ID | `1` | 已配置 |
| 超时 / 重试 | `2000 ms` / `1` | 依参考客户端已配置，可调 |
| 功能码 | `03` / `06` / `16` | 读状态 / 提交序号 / 写心跳与负载 |
| 字格式 | `UINT16` | Modbus 高字节在前 |
| Magic / Version | `0x4C46` / `0x0100` | 连接及每次读取都校验，不匹配立即报错 |

## 上位机→PLC 命令

| 逻辑字段 | PDU 地址/位 | 写入方式 | 状态 |
|---|---|---|---|
| `ControlKey` | `32`, `0x835A` | 与心跳使用 FC16 连续写 | 已冻结 |
| `HeartbeatCounter` | `33` | 与 `ControlKey` 连续写，并截为 16 bit | 已冻结 |
| `CommandSequence` | `34` | 负载写成功后最后使用 FC06 单写 | 已冻结 |
| `MainEnable` | `35 bit0` | 命令位 | 已冻结 |
| `ChannelEnable[1..4]` | `35 bit1..4` | 命令位 | 已冻结 |
| `PS1OutputEnable` | `35 bit5` | 命令位 | 已冻结 |
| `AllStop` | `35 bit6` | 高优先级；适配器发送时屏蔽所有使能位 | 已冻结 |
| `ResetFaultPulse` | `35 bit7` | 仅在新事务中置 1，心跳不重发脉冲 | 已冻结 |
| `ApplySetpoints` | `35 bit8` | 仅在系统 OFF、PS1 实际输出关闭时发送单次应用事务 | 已冻结，UI 已启用 |
| `SetVoltage` | `36`, mV | UI 按配置范围校验；普通命令沿用 PLC 回读值 | 已冻结 |
| `SetCurrent` | `37`, mA | UI 按配置范围校验；普通命令沿用 PLC 回读值 | 已冻结 |

事务顺序固定为：先使用 FC16 写 `35..37`，再使用 FC06 写 `34`。心跳仅写 `32..33`，不重写 `CommandSequence`。
设定值应用后，上位机还会校验 `SetpointApplied`、`SetpointRejected` 和 PLC 回读值；
按钮反馈保持 pending，直到序号及应用结果全部确认。

## PLC→上位机状态

| 逻辑字段 | PDU 地址/位 | 缩放/语义 | 状态 |
|---|---|---|---|
| `PLCReady` | `2 bit0` | PLC 逻辑就绪 | 已冻结 |
| `HostConnected` | `2 bit1` | PLC 已确认上位机心跳 | 已冻结 |
| `MainReady` | `2 bit2` | 主回路可用 | 已冻结 |
| `MainContactorFB` | — | V1.0 未单独暴露 I0.1/KM0 反馈，UI 显示 `INVALID` | **TODO(PLC-V1-GAP)** |
| `PhysicalPermit[4]` | — | V1.0 未暴露 I0.2–I0.5/S1–S4 原始许可，UI 显示 `INVALID` | **TODO(PLC-V1-GAP)** |
| `ChannelPermit[4]` | `3 bit0..3` | CH1–CH4 最终许可/继电器输出 | 已冻结 |
| `PS1Request` | `20 bit5` 且 `bit6=0` | PLC 最近接受的输出请求 | 已冻结 |
| `PS1ActualOutput` | `2 bit4` | PS1 实际输出 | 已冻结 |
| `PS1CommOK` | `2 bit3=1` 且 `bit6=0` | PLC↔PS1 RTU 通信正常 | 已冻结 |
| `PS1Fault` | `2 bit6 OR bit7` | 通信故障或设备故障 | 已冻结 |
| `FaultLatched` | `2 bit5` | PLC 故障锁存 | 已冻结 |
| `FaultCode` | `4` | 控制柜故障字 | 已冻结；故障码字典待 PLC 方提供 |
| `Voltage[4]` | `12..15` | V1.0/V1.1 均按 mV × `0.001` = V 解码；VS1～VS4 为 0–50 V 对应 0–10 V 输出，50000=50.000 V | 已确认 |
| `Current[4]` | `16..19` | mA × `0.001` = A，0–50 A | 已冻结 |
| `Power[4]` | 派生 | `Voltage × Current`，W | 上位机派生 |
| `AnalogValidMask` | — | V1.0 无有效位；当前仅以“本次 FC03 成功+工程量未超界”判定 | **TODO(PLC-V1-GAP)** |
| `AcknowledgedSequence` | `24` | PLC 最近接受序号；适配器处理 16 bit 回卷 | 已冻结 |
| `StatusCounter` | — | V1.0 无 PLC 计数器；当前为本地成功轮询计数 | **TODO(PLC-V1-GAP)** |

额外原始状态也被保留在快照中：`PS1_MB_Status(5)`、`PS1_DeviceStatus(6)`、`PS1_ManagerState(7)`、PS1 实际/设定电压电流 `8..11`、`AcceptedCommandFlags(20)`、`HeartbeatEcho(25)`、`HostServerStatus(26)` 和 `HostServerEvents(27)`。

## 明确禁止的跨层映射

- Linux 不读写 PLC 物理 `Q` 地址。
- PS1 的 RTU 从站 1、9600 8N1、寄存器 1007/2016 只属于 PLC↔PS1 内部实现，不得放入 Linux Modbus TCP 客户端。
- QF0 辅助反馈实体未接线，不得用于 `PLCReady` 或 `MainReady`。
- QFDC1–QFDC4 没有辅助触点，UI 不宣称它们已获 PLC 确认。

## PLC V1.1 建议补充项

| 待补字段 | 建议类型 | 验收目的 |
|---|---|---|
| `MainContactorFB` | 1 bit | 单独暴露 I0.1，与 `MainReady` 分离 |
| `PhysicalPermit[4]` | 4 bit | 单独暴露 I0.2–I0.5，用于投入前预检 |
| `AnalogValidMask` | 4 bit | 区分传感器/模块无效与合法的 0 V/0 A |
| `StatusCounter` | `UINT16` | 识别 PLC 状态快照是否真正更新 |
| `FaultCode` 字典 | 文档 | UI 显示可执行的故障原因和处置 |

补充后只需在 YAML 中填入新地址/位并切换有效位策略，不需要改动业务状态机。
