# Ingestion API contract

本文件以本仓库 `software/service/app/api/routes/ingestion.py`、
`software/service/app/schemas/ingestion.py` 和 `software/database/sql/020_edge_collector_ingestion.sql`
为准。所有请求使用 JSON、`Authorization: Bearer <token>` 与 8–200 字符的
`Idempotency-Key`。body 不发送 `schema_version` 或
`producer_execution_key`。本工具不直连 PostgreSQL、InfluxDB、NAS 或对象存储。

`producer_execution_key` 通常取本地 `run_id`，必须匹配
`^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$`。客户端先校验后直接放入路径，不接受
斜杠，也不依赖 percent-encoding 绕过路由限制。

Endpoints:

- `POST /api/v1/ingestion/executions`
- `POST /api/v1/ingestion/executions/by-producer/{producer_execution_key}/heartbeat`
- `POST /api/v1/ingestion/executions/by-producer/{producer_execution_key}/events`
- `POST /api/v1/ingestion/executions/by-producer/{producer_execution_key}/telemetry`
- `POST /api/v1/ingestion/executions/by-producer/{producer_execution_key}/artifacts`
- `POST /api/v1/ingestion/executions/by-producer/{producer_execution_key}/finish`

## 离线 stage 解析

`enqueue-run` 按 create → events → telemetry → artifacts → finish 完整写入
SQLite。create 返回的 `data.execution_id` 和 `data.stage_id` 由 outbox 按
producer execution key 持久化；发送 telemetry 时才将已解析的 `stage_id` 加入
`TelemetryBatchRequest`。若 create 失败，严格顺序 flush 会停止；重启后映射仍存在。
若进程在 HTTP 成功后、标记 outbox sent 前退出，幂等重放 create 的响应会再次安全保存
同一映射。首请求在退避期间，后续请求仍保持等待，不能跳过create。

## Payload

- create 完全来自 `platform.execution_context`，包含七个关联 UUID、
  `started_at`、`source_status=running`、stage 字段与 `source_metadata`。
- events 外层为 `producer_key + events`；本地事件转换为稳定
  `producer_event_key`、`event_type`、UTC `occurred_at`、`payload` 与质量字段。
- telemetry 外层为 `producer_key + stage_id + series`。每关节发送
  `JOINT-TARGET-POSITION`、`JOINT-ACTUAL-POSITION`，两者同一行均有限时派生actual−target并发送`JOINT-TRACKING-ERROR`（deg，保留源列与公式）；可选反馈有效时发送
  `JOINT-VELOCITY`、`JOINT-TORQUE` 和 `JOINT-TEMPERATURE`。单位分别为
  `deg`、`deg/s`、`N·m`、`°C`；缺失可选反馈时不生成对应 series。每批最多 20 series，
  每 series 最多 5000 points，`sample_index` 从零连续，采样间隔不小于 10 ms。
  `raw_data_reference` 只登记本地 CSV 哈希/列/stride，不嵌 samples。
- 本地文件和 trajectory 通过 `ArtifactMetadataRequest` 登记。当前无共享对象存储，
  使用 `availability_status=MISSING` 和 `edge-local:`/`edge-input:` 来源，不谎报
  `AVAILABLE`。
- finish 使用 `source_status/outcome/termination_kind` 合法组合，并把
  `active_seconds` 截断到 execution 墙钟区间内。计数必须来自确认的执行区间，缺失时为0；不能用最后一个遥测点时间代替。工具`completed`为INCONCLUSIVE，只有明确`passed`才可通过；synthetic始终INCONCLUSIVE。

## 开发测试

在 `software/test-runner` 运行 `python -m pytest tests/test_platform_contract.py`。
契约测试加载 Service schema，检查每类 outbox payload 的 `model_validate` 结果、指标 catalog 与字段约定。
