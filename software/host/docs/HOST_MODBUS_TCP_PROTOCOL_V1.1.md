> 本文保留V1.1历史契约。当前HMI对0100/0101仅监控，控制须配套 [V1.2](HOST_MODBUS_TCP_PROTOCOL_V1.2.md)。

# LFD835 Host Protocol V1.1 — 上位机适配

Modbus TCP 使用 PDU 0 起始地址。标识为 HR0=0x4C46、HR1=0x0101；
控制键仍为 0x835A。状态读取窗口仍为 HR0～27，命令窗口仍为 HR32～37。
本文补充 V1.0 文档，未列出的字段布局和命令顺序保持不变。
PLC 软件版本与协议版本分别管理，协议寄存器为 **0x0101**。

| 地址 | V1.1 字段 | 含义 |
| --- | --- | --- |
| HR21 | ResetAckSeq | PLC 完成本次复位判定后回显触发复位的 16 位 CommandSeq |
| HR22 | ResetResult | 0 无结果；1 正在判定；2 成功；3 不满足安全条件；4 故障仍锁存 |
| HR23 | FaultSourceFlags | bit0 PLC 锁存；bit1 PS1 通信；bit2 PS1 设备；bit3 主机通信服务 |
| HR24 | AcceptedCommandSeq | 仅表示命令接收，不表示复位完成 |
| HR12～15 | VS1～VS4 电压 | **0.001 V/计数**，0～50 V；50000=50.000 V，与 V1.0 相同 |
| HR16～19 | CH1～CH4 电流 | 0.001 A/计数，0～50 A |

PS1 的 HR8～11 回读和 HR36～37 设定值保持 0.001 V/A 每计数。
VS1～VS4 的实测电压为 **0～50 V**，对应变送器 **0～10 V** 输出，PLC 端也使用 0～50 V 缩放。
V1.0/V1.1 的电压、电流单位相同，不存在 10 mV/计数的版本分支；单 Word 的 50000 可完整表示 50.000 V。
主机依据**实际返回的 HR1**启用复位回执/故障来源，不能只依据 YAML 的目标版本。
旧用户 YAML 的 100 V 上限不会放宽确认的 50 V 工程范围；文件本身不被自动覆盖。

## 复位事务

上位机要求主回路、PS1 输出及通道请求均关闭，发送一次 CommandFlags bit7
复位脉冲，提交新 CommandSeq；心跳不会重新触发脉冲。它先用 16 位环绕比较
等待 AcceptedCommandSeq 到达本次序号，再等待 ResetAckSeq 等于本次原始 16 位
序号以及 ResetResult 为最终值。IDLE/PENDING 和旧序号均继续等待。

只有 SUCCEEDED 标记 confirmed；NOT_SAFE 标记 rejected 并显示安全条件未满足；
STILL_LATCHED 标记 failed 并显示 PLC 控制故障仍锁存。没有最终回执时，达到
`command_timeout_ms` 才 timeout。等待期间不因旧 FaultLatched 为真而提前失败。
全部停止仍可中止事务，不会自动补发复位。

**复位只处理 PLC 锁存故障。** PS1 通信/设备故障保持独立；即使 PLC 复位成功，
这些故障仍会显示为红色并阻止使能。最终成功回执和 FaultLatched 相矛盾时，
仍保持 FAULT，不允许启动。

PLC 侧在接收新复位的**同一扫描**与 AcceptedCommandSeq 一起发布
ResetResult=PENDING，并在完成判定后发布匹配的最终 ResetAckSeq/ResetResult。
ResetAckSeq 和最终结果保持至下一次复位，普通心跳不能清空结果；
FaultSourceFlags 则**每扫描更新**，不因最终复位结果保持而冻结。SUCCEEDED 也适用于
本来没有 PLC 锁存故障；不能把它当作清除 PS1 故障的证明。

### 已确认的复位时间预算

| 路径 | PLC 侧判定时间 | 最终回执 |
| --- | --- | --- |
| 安全条件满足，PLC 锁存已清除或原本无锁存 | 至少等待 **100 ms** 的新状态图像后判定；正常约 100 ms | Result=2，SUCCEEDED |
| 安全条件满足，但 PLC 故障仍锁存 | `ResetOutcomeTimeout=2 s` 后判定；最坏约 2 s | Result=4，FAILED_STILL_LATCHED |
| 主回路/通道命令未全关或 KM0 反馈仍在 | 在接收命令的处理扫描内判定，不经过 100 ms 成功等待 | Result=3，REJECTED_NOT_SAFE |

上位机 `command_timeout_ms` 保持 **5000 ms**，默认轮询 **200 ms**。
PLC 正常和最坏判定预算均小于主机超时；最终结果还需经过一次主机轮询及
网络传输才能显示，不能把 PLC 的约 100 ms 判定时间当作页面必然在 100 ms
更新的保证。典型 200 ms 轮询下，主机可在随后轮询读到正常成功回执。
PLC 的 2 s 判定预算相对主机 5 s 留有约 3 s 余量，包含轮询和通信开销。
旧故障位或 AcceptedCommandSeq 单独变化都不能替代最终复位回执。


## 兼容与显示

当前 HMI 对 0x0100 和 0x0101 只提供监控，控制和复位写入要求 V1.2；旧协议字段仍按实际 HR1 解码。未知版本拒绝。

柜内故障分别显示 FaultCode、MB_Status、DeviceStatus。FaultCode=0 不能抵消
PS1 故障。顶部横幅汇总电机原有故障与柜内故障，两种状态独立刷新，不互相清除。
未定义的 FaultSourceFlags 位保留，不猜测其含义。
