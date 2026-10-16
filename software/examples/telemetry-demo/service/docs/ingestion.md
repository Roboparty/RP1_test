# 采集接入契约

上位机通过Service接入。采集端没有数据库凭据；每个服务主体绑定采集源及可写Campaign。

## 一次运行

| 顺序 | POST路径（前缀 `/api/v1/ingestion`） | 关键内容 |
|---|---|---|
| 1 | `/executions` | 七个上下文UUID、来源运行键、开始时间、阶段名称 |
| 2 | `/executions/{execution_id}/heartbeat` | 状态、时间、数据质量 |
| 3 | `/executions/{execution_id}/events` | 事件键、类型、时间与载荷 |
| 4 | `/executions/{execution_id}/telemetry` | 阶段UUID、规范指标序列及降采样点 |
| 5 | `/executions/{execution_id}/artifacts` | 外部文件位置、大小、SHA-256、可用状态 |
| 6 | `/executions/{execution_id}/finish` | 结束原因、结果、已确认活跃秒数 |

七个UUID是 `campaign_id`、`cycle_id`、`segment_id`、`asset_id`、`configuration_id`、`test_case_version_id`、`station_id`。必须来自同一有效上下文；演示初始化会生成这些值。

每个请求携带 `Authorization: Bearer <service-key>` 和稳定的 `Idempotency-Key`。请求内容包括稳定 `producer_key`。同键同内容返回已有结果及 `replayed=true`，不同内容返回409；不要为同一离线批次每次生成新键。也可通过 `/producer-executions/{producer_execution_key}/...` 解析自己的来源运行键。

## 遥测规则

- 遥测主体需属于测试对象：整机使用带左右侧的 `SYS-*`；单臂使用 `SARM-*`，单腿使用 `SLEG-*`。主体与部位不符返回422；适配配置明确映射物理关节。
- 采集端先完成rad→deg、rad/s→deg/s；API按已发布指标目录验证单位。
- 序列采样间隔至少10ms，索引从0连续，所有点落在序列时间区间内。
- 时间携带UTC偏移；Influx精度为ns，当前Python时间保留到µs。
- `raw_data_reference`只保存引用，不内嵌高频样本。每个序列最多5000点，每批最多20序列。
- `source_metadata.source_kind`声明 `synthetic` 或 `real`；默认real。它是采集端来源标记，并不证明数据已通过实测审核。

Influx投影使用 measurement `joint_telemetry`，标签为 `run_id=execution UUID`、`case_id=用例编码`、`subject=subject_code`、`source_kind`：

| PostgreSQL metric_code | 规范单位 | Influx字段 |
|---|---|---|
| JOINT-TARGET-POSITION | deg | cmd_pos_deg |
| JOINT-ACTUAL-POSITION | deg | pos_deg |
| JOINT-TRACKING-ERROR | deg | tracking_error_deg |
| JOINT-TEMPERATURE | °C | temp_c |
| JOINT-VELOCITY | deg/s | spd_deg_s |
| JOINT-TORQUE | N·m | torque_nm |

只投影VALID/PARTIAL点；缺失、无效或尚未映射的指标保留PostgreSQL事实，不伪造零值。跟踪误差沿用实际值减目标值。队列 `point_count` 计数的是字段写入条目，多个字段可归并为同一时刻的Influx点。

## 演示身份

Compose的 `bootstrap-demo` 显式加载[合成seed](../../database/seeds/demo_context.sql)，把上下文和新生成Bearer写到本地 `.runtime/context.json`（0600）。重新执行会撤销旧演示密钥并生成新密钥；已配置的采集端需重新加载文件。

## 失败与重试

422表示上下文、时间或单位不符合契约，修正后使用新的请求键；409表示内容冲突，应检查原始离线批次。网络超时后可重试原请求。

Influx失败不回滚已经接受的事实；持久队列重试2、4、8…最多300秒。未发送队列保留，不自动丢弃。成功表示Influx收到204响应；连接恢复后重复写入具有相同点身份。仅监控投影写入，不把图表刷新作为测试通过条件。
