> 当前控制适配以 [Host Protocol V1.2](HOST_MODBUS_TCP_PROTOCOL_V1.2.md) 为准；下面的旧版实施记录不代表当前控制兼容范围。

> 此文是 PLC 功能的来源设计记录；其中 reliability_v1 部分为另一套平台的可选适配器。本交付使用 legacy 审批模式，部署以根 README 为准。

# 人形机器人老化配电柜上位机：审查与实现说明

## 1. 现有工程审查

本分支来自 原 Factory HMI 工程 的当前工作副本，原目录源码未修改。新工程位于原目录下的 rp1_test_hmi。

### 基础信息

- 框架：Python 3.10、PySide6 桌面端、FastAPI 本机控制网关。
- 构建与交付：pyproject.toml、setuptools、PyInstaller、Debian 打包脚本。
- 状态与路由：没有前端路由框架；桌面使用 QStackedWidget，控制动作通过 GatewayClient 调用本机 REST/WebSocket。
- 数据：本地 CSV、报告、tar.gz 执行包和 SQLite outbox；原有上传器为独立后台线程。
- 类型：Python 类型标注；无 TypeScript/tsconfig，因此 TS 维度不适用。
- 测试：pytest/unittest，修改前基线为 134 passed, 105 subtests passed。

### 架构现状

- 桌面端不直接持有电机硬件，控制通过本机网关执行；这个安全边界适合继续承载 PLC 子系统。
- CAN 控制核心、报告生成、outbox 和上传器已经分层，新增功能可以复用而不必重写。
- factory_hmi/desktop/app.py 超过 4500 行，页面、对话框、网络状态和业务交互集中在一个类中。PLC 页面已放入独立的 desktop/plc_page.py，避免继续扩大该文件。
- 原有上传协议使用 /api/submissions 和分片上传路径，与当前 rp1-reliability-platform 的 /api/v1/ingestion 合同不一致。新适配器保留旧实现，同时增加当前接口模式。

## 2. PLC 子系统边界

    PySide6 PLC 页面
           │ 逻辑 REST 命令 / 状态快照
           ▼
    FastAPI 本机网关（单例 PlcCabinetService）
           │
    PlcCabinetController（顺序、超时、ACK、STALE、日志）
           │ PlcClient 逻辑接口
           ├── MockPlcClient（当前默认，可完整测试）
           ├── S7PlcClient（DB 映射未冻结，仍拒绝连接）
           └── ModbusTcpPlcClient（LFD835 Modbus TCP V1.0）

状态机和页面只使用 MainEnable、PS1OutputEnable、ChannelEnable[4] 等逻辑字段。
协议地址只允许出现在 factory_hmi/config/plc_cabinet.yaml，业务代码中没有 DB 号、
DB 偏移、硬件 ID、PLC IP 或 Modbus TCP 寄存器地址。Modbus 适配器使用
FC03 读取 0–27，使用 FC16 写入心跳和命令负载，最后使用 FC06 单写序号提交事务。

## 3. 控制顺序

启动：

1. 连接 PLC，建立轮询和心跳，先发送全关闭初始化命令。
2. 等待 PLCReady。
3. 发送 MainEnable，等待 AcknowledgedSequence。
4. 等待 MainReady。当协议后续单独暴露 MainContactorFB 时同时校验；V1.0 中该值显示 INVALID，不用 MainReady 冒充。
5. 发送 PS1OutputEnable，等待 ACK。
6. 等待 PS1CommOK 和 PS1ActualOutput。
7. 若协议提供 PhysicalPermit，仅对成立的选中通道发送请求；V1.0 未提供原始 S1–S4，因此由 PLC 最终 ChannelFlags 确认，超时的单路会被切除并标记 rejected。

正常停止：

1. 四通道请求关闭并等待 ACK/许可撤销。
2. PS1 请求关闭并等待 ACK 和 PS1ActualOutput=0。
3. 最后关闭 MainEnable 并等待 KM0 反馈释放。

KM0 不参与 PS1 日常频繁启停。紧急的 AllStop 可以覆盖尚在等待确认的普通命令。

## 4. 通信与数据质量

- 心跳默认 500 ms、状态轮询默认 200 ms、STALE 默认 1000 ms，均在 YAML 中配置。
- 每个控制事务递增 CommandSequence；适配器支持 16 bit 序号回卷；按钮保持 pending，直到 AcknowledgedSequence
  确认，随后显示 confirmed、rejected 或 timeout。
- 超过 STALE 时限后，电压、电流和功率返回 null，AnalogValidMask 返回 0，启动命令被拒绝。
- 单路模拟量无效时，该路三项测量显示 INVALID，不会沿用最后正常值。V1.0 暂无 PLC 有效位，只能使用新鲜读取与 0–50 V/0–50 A 范围判定，映射表保留高优先级 TODO。
- 通信中断进入 COMM_LOST，停止发送启动操作；重连后强制全部请求为关闭，必须由操作员重新启动。
- ResetFaultPulse 仅执行一次 TRUE 写入，保留命令镜像与后续心跳立即恢复 FALSE。
- 操作日志保存 UTC 时间、用户、字段、旧值、新值、命令序号、确认结果和详情，并写入
  data_root/plc/operations.jsonl。
- 页面将系统总启动、正常总停止与四路独立控制分区展示；单路按钮在前置条件不满足时
  禁用并直接显示原因，不再允许点击后才返回 rejected。
- PS1 目标电压和限流值仅可在 OFF 且实际输出关闭时修改。范围来自地址无关配置，
  应用命令等待 AcknowledgedSequence、SetpointApplied 和设定回读三重确认。

## 5. 当前可靠性平台适配

当前大屏后端要求服务凭据访问以下真实路由：

- POST /api/v1/ingestion/executions
- POST /api/v1/ingestion/executions/by-producer/{key}/events
- POST /api/v1/ingestion/executions/by-producer/{key}/telemetry
- POST /api/v1/ingestion/executions/by-producer/{key}/artifacts
- POST /api/v1/ingestion/executions/by-producer/{key}/finish

设置：

    export RP1_FACTORY_PLATFORM_MODE=reliability_v1
    export RP1_FACTORY_PLATFORM_URL=http://<可靠性平台地址>
    export RP1_RELIABILITY_SERVICE_KEY_FILE=/etc/rp1-factory-hmi/secrets/reliability-service-key
    export RP1_RELIABILITY_PROFILES=/etc/rp1-factory-hmi/reliability-platform.yaml

测试上下文档案必须由平台管理员填写数据库中真实且相互匹配的
campaign/cycle/segment/asset/configuration/test-case-version/station UUID。
默认文件不包含示例 UUID，避免把虚构标识写进数据库。

遥测适配会：

- 将记录 CSV 的关节目标/实际位置从 rad 转换为平台规范的 deg；
- 上传位置、力矩、温度和电流序列；
- 单序列最多 5000 点，单批最多 20 个序列；
- 通过档案的 subject_map 将本地关节名映射为平台 catalog subject code；
- 上报测试判定事件、执行结束结果与数据质量。

## 6. 风险与后续工作

【风险等级：高】

问题描述：Modbus TCP V1.0 已冻结 0–37 的主要映射，但没有单独暴露 KM0 反馈、S1–S4 原始许可、模拟量有效位和 PLC 状态计数。

影响：上位机可以真实连接并完成序列控制，但不能声称已独立确认 KM0/S1–S4，也无法识别仍在合法量程内的传感器无效值。

优化方案：按 [PLC_INTERFACE_MAPPING.md](PLC_INTERFACE_MAPPING.md) 的 V1.1 补充表增加独立位；在此之前 UI 对缺失状态显示 INVALID，不使用其他状态伪造确认。

【已确认的 VS 量程与编码】

VS1～VS4 实测电压为 0–50 V，对应变送器输出 0–10 V。V1.0 与 V1.1 的
HR12～15 均为 UINT16 mV，50000=50.000 V，可完整表示实际量程；不需要改成 10 mV 编码。
超出 50 V 的工程值标记无效。V1.1 只扩展 HR21～23 的复位结果和故障来源，
详细约定见 [HOST_MODBUS_TCP_PROTOCOL_V1.1.md](HOST_MODBUS_TCP_PROTOCOL_V1.1.md)。

【风险等级：高】

问题描述：当前可靠性平台只有制品元数据登记接口，没有执行包二进制/对象存储上传接口。

影响：结构化遥测、事件和执行结果可以入库，但本地 tar.gz 只能登记为远端
MISSING，不能声称已上传。

优化方案：平台增加签名对象上传或受控分片上传接口后，再把制品状态改为
AVAILABLE；在此之前保留本地执行包和 SQLite outbox。

【风险等级：中】

问题描述：平台 ingestion 登记需要多个数据库 UUID，而现有读模型接口不能提供完整、
一致的 cycle/segment/configuration/version/station 组合。

影响：无法仅根据样机编号或测试用例显示名安全推导上传上下文。

优化方案：短期由受审 YAML 档案提供；长期由平台增加面向采集站的“可执行测试上下文”
只读接口，返回一个不可拆分的上下文 ID。

【风险等级：中】

问题描述：desktop/app.py 仍是大体量窗口类。

影响：后续新增页面时容易产生控件状态耦合和回归风险。

优化方案：后续按页面逐步迁移为独立 QWidget/ViewModel；本次只抽离新增 PLC 页面，
没有重写旧功能。

【风险等级：中】

问题描述：Mock 能验证业务顺序和界面反馈，不能替代真实 S7-1200 G2、CB1241、PS1
与 USBCANFD-400U 的电气联调。

影响：现场时延、字节序、PLC 扫描周期、接触器动作和断线行为仍需实测。

优化方案：使用已冻结的 Modbus TCP V1.0 映射增加硬件在环测试，沿用当前同一组状态机用例。

## 7. 自动化覆盖

- 正常启动；
- 正常停止；
- KM0/MainReady 超时；
- PS1 通信超时；
- PLC 掉线与重连后全关闭；
- 通道物理许可缺失；
- 单路模拟量无效和整体 STALE；
- ResetFault 单次脉冲；
- 网关端到端 Mock 启停；
- 当前平台上下文校验、遥测合同转换和 outbox 完整上传流程。
