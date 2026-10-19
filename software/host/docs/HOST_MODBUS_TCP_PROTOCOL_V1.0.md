# LFD835 Linux 上位机通信协议 V1.0

当前 HMI 对 V1.0 仅监控。以下保留协议字段定义，不作为当前控制操作指引；控制要求见 [PLC 对接](plc-integration.md)。

## 1. 传输层

- 协议：Modbus TCP Server（PLC 为服务器，Linux 为客户端）
- PLC 地址：`192.168.137.10`
- TCP 端口：`502`
- Unit ID：`1`（PLC 端不区分 Unit ID，客户端固定发送 1）
- 允许功能码：`03` 读保持寄存器、`06` 写单寄存器、`16` 写多个寄存器
- 寄存器地址：本文和 Python 程序均使用 **0 起始 PDU 地址**，不要再加 40001
- 数据格式：每个寄存器为无符号 16 bit；Modbus 字节序，高字节在前

PLC 仅暴露一个 64 Word 的数据窗口，不允许上位机直接写物理 I/O。Modbus TCP 本身不提供认证或加密，只允许在隔离控制网络/VLAN内使用；寄存器 `32` 的控制键只是防误写标识，不是安全口令。

## 2. 状态寄存器（PLC 写、上位机只读）

| PDU地址 | 名称 | 单位/位定义 |
|---:|---|---|
| 0 | Magic | 固定 `0x4C46`（ASCII `LF`） |
| 1 | ProtocolVersion | `0x0100` = V1.0 |
| 2 | StatusFlags | bit0 PLCReady；bit1 HostConnected；bit2 MainReady；bit3 PS1CommReady；bit4 PS1OutputActual；bit5 FaultLatched；bit6 PS1CommFault；bit7 PS1DeviceFault；bit8 SetpointApplied；bit9 SetpointRejected；bit10 PS1CommBusy |
| 3 | ChannelFlags | bit0～3 = CH1～CH4 实际许可/继电器输出 |
| 4 | FaultCode | 控制柜故障字 |
| 5 | PS1_MB_Status | PLC↔直流源 Modbus RTU 状态码 |
| 6 | PS1_DeviceStatus | 直流源寄存器 1007 原始值 |
| 7 | PS1_ManagerState | PLC 串口状态机状态 |
| 8 | PS1_OutputVoltage | mV；直流源寄存器 1000 原始值 |
| 9 | PS1_OutputCurrent | mA；直流源寄存器 1001 原始值 |
| 10 | PS1_SetVoltage | mV；寄存器 2001 回读值 |
| 11 | PS1_SetCurrent | mA；寄存器 2002 回读值 |
| 12～15 | VS1～VS4 | 各支路电压，mV |
| 16～19 | IS1～IS4 | 各支路电流，mA |
| 20 | AcceptedCommandFlags | PLC 最近接受的命令位；断线时固定为 `0x0040`（AllStop） |
| 24 | AcceptedCommandSeq | PLC 最近接受的命令序号 |
| 25 | HeartbeatEcho | PLC 最近接受的心跳计数 |
| 26 | HostServerStatus | `MB_SERVER` 状态码 |
| 27 | HostServerEvents | bit0 NDR；bit1 DR；bit2 ERROR |

本柜所用 6236-60-60 电源显示分辨率为 `0.001 V / 0.001 A`，所以所有 PS1 电压/电流寄存器按 mV/mA 传输。例如 `5000` 表示 `5.000 V`，`500` 表示 `0.500 A`。

## 3. 命令寄存器（上位机写）

| PDU地址 | 名称 | 说明 |
|---:|---|---|
| 32 | ControlKey | 控制时固定写 `0x835A` |
| 33 | Heartbeat | 每 1 s 改变一次；连续 3 s 不变，PLC 自动 AllStop、关闭 PS1 输出、四路和 KM0 |
| 34 | CommandSeq | 命令提交序号；先写 35～37，再改变此值，PLC 才接受一组新命令 |
| 35 | CommandFlags | bit0 KM0；bit1～4 CH1～CH4；bit5 PS1输出；bit6 AllStop；bit7 ResetFault（脉冲）；bit8 ApplySetpoints（脉冲） |
| 36 | SetVoltage | mV，合法范围 `1..60000` |
| 37 | SetCurrent | mA，合法范围 `1..60000` |

命令规则：

1. 上位机先启动心跳，确认状态 bit1 `HostConnected=1`。
2. 每组命令先写地址 35～37，最后单独改变地址 34；PLC 用地址 24 回显已接受序号。
3. 电压/电流仅在 `PS1OutputActual=0`、PS1 通信就绪且已经确认发送过 STOP 后接受；否则 bit9 `SetpointRejected=1`。
4. 写设定值时同时写电压和电流，并仅在本次命令把 bit8 置 1。PLC 写寄存器 2001/2002 后再读回；一致时 bit8 `SetpointApplied=1`。
5. 端口重初始化、CPU 启动、心跳超时后绝不自动恢复输出；Linux 必须发送新的 CommandSeq。
6. `AllStop` 优先于所有使能。恢复前先发送所有使能为 0、AllStop=0 的新命令，再按顺序启动。
