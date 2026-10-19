# RP1 命令行工具操作规程

本工具为可选轨迹与本地记录工具。`platform` 命令只支持[独立遥测演示](../../../examples/telemetry-demo/README.md)的旧 ingestion，不适配当前 `reliability_v1`；当前平台的数据提交与审批见[数据流程](../../../docs/data-workflow.md)。

## 1. 准备

1. 复制 `config/rp1_test.example.yaml`，填写测试肢体、显式四级映射、默认姿态和电机适配器。每个 `telemetry_subject_code` 必须使用演示数据库 0020 catalog 中的通用码，`target_part_code` 只能使用演示允许枚举。
2. 将 `platform.execution_context` 的 campaign/cycle/segment/asset/configuration/test-case-version/station 七个占位 UUID 替换为对应演示上下文的 public ID；确认 stage code/name 和 source metadata。`started_at: "$RUN_STARTED_AT"` 自动引用运行 metadata。
3. 确认角度输入均为 rad；deg/rad 转换只能调用 `rp1_test.units`。
4. 生成或录制轨迹后使用同一 config/model 运行 `trajectory validate`，确认配置/模型指纹、位置/速度/加速度和 fps/time 均通过，保留 NPZ 与 `.metadata.json`。
5. 先运行 `sim replay --dry-run --headless`；有模型和 MuJoCo 时再进行实际仿真。

## 2. 真机前检查

1. 执行 `hardware preflight --model ...`，确认 SHA、limb、配置/模型指纹、关节顺序、动态限值、200 Hz 控制频率和遥测缺口。
2. 确认机械限位、急停、台架固定、人员净空、供电和 CAN 状态。
3. 设置短轨迹、低增益并安排一名操作员持续掌握急停。
4. 未完成真实模型和电机适配器验收前，只允许 `--dry-run`。

## 3. 本地门控

- `R`：minimum-jerk 回默认姿态。
- `P`：渐入轨迹并开始播放。
- `Space`：保持当前目标。
- `Q`：正常退出时，从实际反馈位置受控回默认，再逐台失能。

渐入与回默认使用有效实际反馈作为起点，并按已配置的位置、速度和加速度限值校验。过渡超限时拒绝执行，不自动延长时长或放宽限值。反馈、预检、执行异常或键盘中断时停止新增运动目标，直接逐台尝试失能，不继续回默认；一台失能失败不跳过其余电机。

真机命令必须显式提供 `--real-hardware --acknowledge I_UNDERSTAND_RP1_MOTION`。轨迹 SHA 校验不可关闭。

## 4. 本地数据与演示补传

本地运行目录仅允许 `telemetry.csv`、`events.jsonl`、`native.log`、`manifest.json`、`metadata.json` 和配置快照。连续遥测至少包含 cmd/pos/speed/torque-or-current/temperature/error_id；不支持的可选 getter 写 NaN 并在 metadata 中声明 capability gap。`error_id` 同时作为事件写入，不作为连续指标解释。

运行结束执行 `platform enqueue-run`；命令会先校验 manifest、execution context 与 producer key，再将本地 rows 转成演示 series/points。目标/实际位置为 deg，速度可用时按 catalog 的 `JOINT-VELOCITY` 发送 deg/s。create 响应的 stage UUID 会保存在 SQLite，重启后 telemetry 仍能自动补齐 `stage_id`。本地文件仅以 `MISSING` artifact 元数据登记，不能在没有对象存储时标记 `AVAILABLE`。随后用 Bearer API `platform flush` 发送；不得直连 PG、InfluxDB 或 NAS。

## 5. 结束

1. 正常结束时确认回默认与失能状态；异常退出时检查实际姿态及所有驱动状态，按现场恢复流程处置。
2. 校验 manifest 中每个文件的大小和 SHA。
3. 记录母线电压、振动缺口是否仍存在。
4. 按[单次运行分析](../../../../data-analysis/README.md)生成时长、温度、误差和数据质量摘要，再结合批准判据与检查记录形成测试结论。
