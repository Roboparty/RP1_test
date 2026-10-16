# HMI 与 PLC 配套契约核对清单

本文件说明当前 HMI 源码的协议、启动预算和故障恢复边界，供配套 PLC 实现、离线验证及现场验收逐项核对。寄存器定义以 [Host Protocol V1.2](HOST_MODBUS_TCP_PROTOCOL_V1.2.md) 为准。软件版本、离线测试和安装结果不能替代实际下载 PLC 程序的身份、TIA 编译结果或实机验收。

本文件不授权连接 PLC/CAN、改变输出、清除故障、下载程序或启动测试。下面引用的 PLC 候选语义和既有反例仍需对应的完整源码、编译及现场时间线证据；没有因此调整主机期限、反馈有效期或故障门控。

## 1. 协议与配置边界

- 当前控制协议是 `Host Protocol V1.2 / HR1=0x0102`，配套目标 PLC 软件 V1.5；PDU 零起始，HR0～31 整帧 32 Word。
- `0x0100`、`0x0101` 仅监控；未知版本拒绝。首次控制写入前检查实际 PLC 版本。
- 电压为 0～50 V，1 mV/计数，`50000=50.000 V`；电流为 1 mA/计数。
- 源码模板与工位实际配置、安装程序、在线 PLC 身份应分别核对。旧 YAML 的声明不证明在线 PLC 版本；V1.2 客户端在内存中扩展为 32 Word 并识别实际 HR1，不自动覆盖用户 YAML。
- 核对源文件时使用当前候选的相对路径与哈希。HMI 与 PLC 分别对应各自已测试、已编译的输入，不要求 Python 和 SCL 文件的哈希互相相等。

## 2. 主机时间预算

依据 `factory_hmi/config/plc_cabinet.yaml`、`factory_hmi/plc/config.py` 及 `factory_hmi/plc/state_machine.py`。下面数值是源码模板默认值，工位投用时须核对实际配置。

| 配置 | 默认值（ms） |
|---|---:|
| heartbeat_ms | 500 |
| poll_ms | 200 |
| stale_timeout_ms（TCP 快照） | 1000 |
| command_timeout_ms | 5000 |
| km0_timeout_ms | 5000 |
| ps1_timeout_ms | 5000 |
| reconnect_ms | 2000 |
| modbus_tcp.timeout_ms | 2000 |
| modbus_tcp.retries | 1 次重试 |

`startup_budget_ms` 的冷启动总预算为：

```text
5000 + km0_timeout_ms + 3*ps1_timeout_ms
     + 9*command_timeout_ms + 2*poll_ms
= 70400ms
```

`request_start` 一次性建立 `_startup_deadline`，轮询和重试不能重建或延长；多窗口启动收据使用同一预算。局部阶段超时可以更早退出，启动完成后退出启动期限管理。

MainReady 后进入 `WAIT_PS1_COMM` 时，预算为 `5000 + ps1_timeout_ms + 2*poll_ms = 10400ms`。固定 5000ms 对应 PLC MainReady 后等待计时；HR28.bit0 只表示该计时完成，不表示 PS1 自检或通信完成。故障可以更早中断，等待窗口不提供忽略故障的宽限。

单命令等待 5000ms。复位接收确认和最终结果共用 5000ms deadline，PENDING 不续期，也不累加成 10 秒。KM0 和普通 PS1 阶段各 5000ms；通道许可/撤销、主回路撤销使用命令期限。

PS1 真实状态无推进 5 秒失效，重复 TCP 读取不续期，HR31 年龄也约束有效性；它与 TCP 快照的 1 秒过期检测对象不同。既有复位约定是安全条件满足后至少 100ms 新图像，仍锁存约 2 秒返回 4，NOT_SAFE 同扫描返回 3；PLC 当前编译版本仍须验证这一约定。

这些都是主机逻辑期限。同步 TCP 调用、轮询和调度影响超时判定时刻，不能据此宣称实机端到端硬上界，或在通信故障期间继续保电。

## 3. 分层核对重试

| 层次 | 当前约束 |
|---|---|
| Modbus TCP 请求 | 单次响应超时 2000ms，retries=1，含首次最多两次发送；不能声称所有写载荷绝不重发。 |
| 启动/复位业务 | 不自动重新执行结果不确定的操作；同一 client_id/request_id 返回既有结果，普通冲突不排队重放。 |
| TCP 断线重连 | 服务运行期间至少间隔 2 秒尝试，没有总次数/总时长上限；成功不自动恢复上次启动，控制重连进入输出 OFF 安全路径。 |
| PLC→PS1 串口事务/恢复 | 必须从配套 PLC 源码核对超时、次数、退避、端口重装和总期限；TCP 重试次数不能替代这一层的设置。 |

两次 TCP 响应等待可能合计约 4 秒，但连接、顺序调用、处理和调度另有耗时，不构成端到端硬上限。

## 4. 人工锁与输出准入

HR28.bit4 为 `MANUAL_RESTART_LOCK`，bit5 为 `RECOVERY_EXHAUSTED`；任一为 1，`ps1_manual_restart_locked` 为真。

- `request_start` 在发启动指令前拒绝锁态；同时要求 LIVE、未过期、OFF 阶段、无 pending、HostConnected、实际协议 0102 及无故障。
- `_write_checked` 拒绝锁态中的普通 MainEnable、PS1OutputEnable 或任一通道 ON，覆盖保留值的脉冲和心跳；`_output_admission_reason` 同样检查 PS1/通道 ON。
- 已活动的 ON 请求读到锁态时进入故障路径，不继续推进成功确认。
- AllStop 是优先安全路径，不能夹带有效 ON；当前命令没有独立解除人工锁/恢复计数的字段，不得猜测未约定的解锁指令。

MainEnable 本身也受锁态拒绝。若配套 PLC 必须通过新 MainEnable 上升沿清锁，则与主机门槛构成准入循环，必须先正式补充双方契约，不能各自放宽互锁。

## 5. 全 OFF 复位与人工锁解除分开

`request_reset_fault` 要求 TCP 已连接、LIVE、未过期、实际协议 0102、无其它 pending，且 MainEnable、PS1OutputEnable 和全部通道请求为 OFF。它不单独以 bit4/5 拒绝全 OFF 的复位脉冲；UI 对应按钮是“复位 PLC 控制故障”。

AcceptedCommandSeq 到达本次序号后，还需 ResetAckSeq 匹配该 16 位序号。IDLE/PENDING 继续等待；只有 SUCCEEDED 确认成功，NOT_SAFE 和 STILL_LATCHED 分别报告拒绝、失败。业务层仅请求一次复位，随即清除保留命令图像中的脉冲；底层 TCP 重试仍按第 3 节处理。

复位成功不代表 PS1 通信或设备故障清除。仍有故障则进入 FAULT，否则进入 OFF；OFF 阶段和 ResetResult=2 都不能证明 bit4/5 已清。锁仍存在时下次 Start/MainEnable 继续被拒绝。

## 6. 清锁证明与反馈重建

合法 0102、LIVE、未过期状态图像中 bit4/5 都为零，仅证明当前回读未报告锁态，不证明某次解锁动作已成功。当前没有独立 UnlockAckSeq/UnlockResult；ResetAckSeq/ResetResult 不能充当清锁回执。

解除门槛不自动恢复运行。仍须显式启动、OFF、无 pending/故障等条件；PS1/通道 ON 还须本会话/当前上电周期状态序号推进、合法 HR31 年龄、全部反馈有效位、Applied/无 Rejected、模拟量有效及其余安全准入。

16 位 HR29/30 不是持久 CPU 启动身份；新会话和已知重启必须重新建立反馈，相同计数不能证明没有重启。应明确清锁动作、安全条件、bit5 计数重置及有效位撤销/重建时机，不能由锁位归零反推这些动作。

## 7. 配套 PLC 必须提供的契约与证据

| 待确认项 | 必须提供的内容 |
|---|---|
| 编译与身份 | 隔离工程、SCL/交付源码逐文件哈希、协议版本/哈希、导入输入与编译工程一致证明、TIA 编译结果及错误/警告数；入口环境检查不能代替编译。 |
| 启动时间线 | MainReady、固定 5 秒、端口装载、STOP、1007、测量/设定回读顺序及最大时间，对应状态/定时器位置。 |
| 串口重试与恢复 | 每类超时、普通 ERROR 退避、F001 恢复、重装端口次数、各轮/总期限、bit5 置位和计数重置条件。 |
| bit4/5 置清 | 故障来源、解除动作及触发端、安全请求与真实反馈条件、逐扫描逻辑、跨 MainReady 下降/电源周期/CPU 重启是否保持；不能只说“人工重启”。 |
| MainEnable/ResetFault | 解锁是否依赖 MainEnable 上升沿，ResetFault 是否影响 bit4/5/计数，各自独立回读证明；ResetResult=2 不暗示 PS1 故障已清。 |
| 解锁后重建 | HR28 有效位、HR29/30/31 更新、旧 AcceptedCommandSeq/ResetAckSeq 保留及同扫描图像一致性。 |
| 反馈与复位期限 | 5 秒失效、100ms/2s/5s 约定，以及边界扫描、环绕、旧回执和同时故障的离线证据。 |

## 8. 交叉核对与投用

记录候选源码、编译输入哈希、报告时间和协议版本；逐项对照 HR1、HR21～31、命令 HR32～37、单位、时间预算、重试上限、bit4/5 解除动作及回执。任何差异先形成差异表，再串行修改和验证共享协议。

投用前必须核对实际安装 HMI、工位配置和实际下载 PLC 程序，完成现场批准的硬件验收。文档、离线模拟、安装成功以及相同协议版本号不能替代这些结果。

## 9. 故障诊断的证据边界

HR7 表示当前 ManagerState，不是锁存的原始失败步骤。停机或故障后单帧 HR7 不能定位最先失败的端口装载、STOP 或 1007；应保留带时间戳的故障前后状态序列。

候选 PLC V1.5 中 F001 表示事务看门狗超时；未确认在线源码身份时，不能直接把候选含义当作在线程序已核实。HR1 版本号也不能替代工程/源码身份。

从 Start 提交到 PLC F001 的总经过时间，与 HMI 进入 WAIT_PS1_COMM 后的 10400ms 阶段期限是两个诊断分支；不得混用起点或据 F001 直接延长主机期限。需要对齐 HR5 首次 F001、故障前后 HR7、同期 HR29/31、HR1 原值和实际下载源码身份的既有材料。缺少材料表示待取得/待核验，不表示证据不存在，也不要求为补文档通电复现。

## 10. 11.4 秒反例与 HMI 阶段预算的边界

本节保留既有 PLC 离线模型反例及主机源码的预算比较。没有重跑该反例，没有 Siemens 内部重试仿真、实机测时或配套 TIA 编译证据；不据此宣称联合验证已完成。

### 10.1 既有模型反例

模型起点是 Manager Enable=True、MainReady 已真，固定扫描周期 100ms，不含前置接触器和 TCP 时间。

| 相对 Manager Enable 的时间 | 模型事件 |
|---:|---|
| 0ms | 开始固定等待 |
| 5000ms | 固定等待结束，进入状态 1 |
| 5100ms | 端口桩立即完成，进入状态 40 |
| 5200ms | 排入 STOP，进入状态 10 |
| 5300ms | 发 STOP，进入状态 11 |
| 8300ms | 注入成功响应，进入状态 20 |
| 8400ms | 发 1007，进入状态 21 |
| 10400ms | 第二次请求已等 2000ms，未完成也未故障 |
| 11400ms | 注入成功 OFF 响应，置 StopConfirmed/CommReady |

`5000 + 4×100 + 3000 + 3000 = 11400ms`。四个 100ms 是模型步骤推进，不是实机通用上界。两个 3000ms 是人为注入成功响应延迟；即使冻结参数是 RESP_TO=1000ms、RETRIES=2、事务看门狗 5000ms，也不能称为实测“1 秒×3 次重试”。模型没有模拟 Siemens 内部重试或状态 45/50，端口桩也不证明实物装载无耗时。

终点只是通信就绪且 OFF，不包括测量/设定回读有效、主机额外 OFF 确认、设定应用、PS1 实际 ON、通道许可或完整启动。

### 10.2 10400ms 的实际起终点

主机在 WAIT_MAIN_READY 观察到 HR2.bit2 MainReady=True，且可选 KM0 反馈不是明确 False 时，进入 WAIT_PS1_COMM 并设期限；不是等待 HR28.bit0 后才开始，也不是点击启动、连接 TCP、状态 40 或首次 1007 时刻。

期限使用该 tick 的 `current`；服务在同步读取状态之前取得时间戳。因此时间戳、PLC 发布以及主机实际读到 MainReady 的时刻并不相同。阶段终点是 `ps1_comm_ok`：HR2.bit3 为真，且 HR2.bit6/HR23.bit1 没有通信故障。之后主机显式发 OFF，再进入 WAIT_PS1_STOP_READY，等待真实 OFF、非 Busy、新鲜反馈、STOP_OFF_CONFIRMED、MEASUREMENT_VALID、SETPOINT_VALID。

两者区间都覆盖固定等待、端口装载、首次 STOP/1007，但仍须证明 Manager Enable 与 MainReady 门控/启动映射；时钟偏移和发布/读取延迟未量化。令 `T0` 为模型 Manager Enable 起点，`TH` 为主机 COMM 期限 tick 时间戳，`δ=TH−T0`，`LH` 为终态发布到主机采用的观测延迟，则理想严格截止比较为：

```text
T0 + 11400ms + LH ≤ TH + 10400ms
δ ≥ 1000ms + LH
```

它不是 δ/LH 的实测值，不能依赖偶然较晚观察到 MainReady 保证兼容。δ=0 且及时轮询时，10400ms 尚未就绪，即使 PLC 未报故障，主机也可能阶段超时；70.4 秒总期限不能替代局部期限。若旧通信故障已阻断 Start，就不会进入这个阶段，应另查 10.5 的协议问题。

实现先检查 `ps1_comm_ok`，再检查阶段超时；同步读取阻塞或晚轮询可能先接受迟到 Ready，且 `current` 早于读完。因此公式用于预算适配，不能冒充每次实现结果。需要共同时间线或离线联合模型验证，不以阻塞延迟作为恢复裕量。

### 10.3 启动阶段与事务覆盖

记 `C=command_timeout_ms=5000`、`K=km0_timeout_ms=5000`、`P=ps1_timeout_ms=5000`、`p=poll_ms=200`。

| 阶段 | 等待预算（ms） | 结束事件及应覆盖工作 |
|---|---:|---|
| START_STOP_ACK | C | 全 OFF/AllStop 的 PLC 接收确认。 |
| START_CLEAR_ACK | C | 保持全 OFF、清 AllStop 的 PLC 接收确认。 |
| WAIT_PLC_READY | C | PLCReady 发布及主机读取，不能只凭 TCP 连上。 |
| WAIT_MAIN_ACK | C | MainEnable 接收确认，不等于 KM0 吸合。 |
| WAIT_MAIN_READY | K | 接触器反馈、主回路就绪与状态发布。 |
| WAIT_PS1_COMM | 5000+P+2p=10400 | 固定等待、端口装载、首次 STOP、首次 1007/OFF、CommReady 发布及读取；允许的恢复路径须明确。 |
| WAIT_PS1_STOP_ACK | C | 主机显式 OFF 接收确认；是否触发额外 STOP 由 PLC 代码决定，不能从 force=True 推断。 |
| WAIT_PS1_STOP_READY | P | 真实 OFF、StopConfirmed、本会话/周期 1007 推进、新鲜状态、测量/设定有效，不能只看输出位 False。 |
| Setpoints pending | C | 设定写入及读回匹配、Applied、无 Rejected、有效新鲜反馈；已匹配且有效时可跳过。 |
| WAIT_PS1_ACK | C | PS1 ON 接收确认，不等于实际输出 ON。 |
| WAIT_PS1_OUTPUT | P | 实际开启、后续真实 1007 ON 与读取，且其它 ON 准入反馈仍有效。 |
| WAIT_CHANNEL_ACK | C | 通道接收确认，未选通道可跳过。 |
| WAIT_CHANNEL_PERMIT | C | 所请求通道许可全部满足及主机读取，不能只看命令接收。 |

完整设定及通道分支是 `9C+K+3P+5000+2p=70400ms`。总期限从 request_start 建立，不含此前连接初始化。普通命令和设定在同步发送后才建立 pending 期限；新事务包括控制键/心跳、载荷、序号提交三个 TCP 写请求。发送耗时计入启动经过时间，却不在发送后新建的 C 等待内，总预算不构成含所有 TCP 阻塞和调度的实测上界。

### 10.4 最坏路径表达式与缺少的量

定义 `D=5000ms`；`Lport` 为真实端口装载/恢复耗时，`Qj` 为排队和 PLC 状态推进，`τj` 为事务发起到 DONE/ERROR 的真实时间，`Lj` 为状态发布、读取和调度，`Rpath` 为恢复路径额外时间。不能全部用 poll_ms 或 RESP_TO 替代。

- `TCOMM = D + Lport + QSTOP + τSTOP + Q1007 + τ1007 + LCOMM + Rpath`；反例没有实测 Lport、内部重试、45/50 恢复及观测延迟。
- `TSTOP_READY = max(t真实OFF, t测量有效, t设定有效, t本会话状态推进) − tOFF接收 + L观察`；按真实串口串行调度计算剩余量，既有有效回读不重复计时，未完成读数不忽略，额外 STOP 仍须核对。
- `TAPPLY = Q应用 + Στ设定写入 + Q回读 + τ设定回读 + L匹配确认`；全部共用 C，事务顺序和 Applied 发布时机待核验。
- `TON = Q启动 + τ启动 + Q1007 + τ1007 + L真实ON发布`；同时验证其它反馈持续新鲜。接触器和通道许可各需自己的真实反馈时间。
- 含恢复最坏路径是允许路径中各轮固定等待、装载、排队、事务、退避、发布之和的最大值，须有明确次数及总期限；耗尽即按终止处理，不能无限延长等待。

RESP_TO=1000、RETRIES=2 只有在“初发加两次重试”的解释获确认后，才给出名义等待 3000ms；它不证明真实 Siemens 请求总时长≤3 秒。发起、退避、帧传输、BUSY/DONE 扫描、排队、取消仍待证据。5000ms 看门狗是另一层检测，应核对起点、复位点、DONE/超时同扫描优先级和恢复次数，不能当作成功事务上界。

服务的 200ms 循环还包含同步读取、心跳等工作；poll_ms 不保证最大观测延迟 200ms，两个轮询余量不自动覆盖 TCP 重试、线程/锁和主机调度。持续健康仍独立受 5 秒有效性约束；延长启动不能保留过期反馈，HR29 推进也不证明测量/设定刷新。须核对首帧与过期规则及整个串口序列的刷新间隔。

### 10.5 锁态补核与待决定事项

PLC 候选只读结论指出：全部使能 OFF、KM0 反馈断开、MainReady=0 时解除 bit4/5，不依赖新 MainEnable 上升沿；通信故障仍等待下一周期真实 STOP/OFF 才清。该结论尚需对应完整源码与编译证据。即使锁清零，只要 HR2.bit6 或 HR23.bit1 仍为真，主机 has_fault 仍在 Start、最终发送和轮询中拒绝/撤销 MainEnable；ResetResult=2 不清 PS1 通信故障。

这一配套循环须正式选择受控恢复许可/结果，或区分历史诊断与本周期阻断状态。不得只放开按钮、屏蔽 F001、提前宣称故障清除或先发 MainEnable 再等故障消失；当前未实施新方案。

仍需提供：PLC 源码/反例输入哈希与既有运行记录；统一时间轴中的 Manager Enable、MainReady、Host ACK、COMM tick、CommReady 发布/读取及最大扫描；真实端口装载、STOP/1007 和内部重试、45/50、退避/重装次数；就绪后测量/设定的残余时间与刷新规则；允许恢复路径及双方期限决定；TIA 导入/编译结果和现场验收。

在这些证据及正式变更到位前，保持 10400/70400ms 与 5 秒反馈有效期，不将反例、旧离线检查或入口环境检查写成 TIA 编译/实机通过。
