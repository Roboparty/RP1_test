# 边缘采集工具接入契约

## 身份与凭据

采集端使用 `Authorization: Bearer <service-api-key>`。该凭据只允许访问
`/api/v1/ingestion/**`；浏览器 Cookie session、双提交 CSRF 和强制改密流程不变。
服务主体固定为 `principal_kind=SERVICE`、`role=TEST_EXECUTOR`，并通过 Campaign EDIT
grant 和 PostgreSQL RLS 限定写入范围。数据库只保存带服务端 pepper 的
HMAC-SHA256，不保存或记录明文 key。

系统管理员通过以下 API 创建、轮换和吊销凭据：

- `POST /api/v1/admin/service-principals`
- `POST /api/v1/admin/service-principals/{service_id}/rotate`
- `POST /api/v1/admin/service-principals/{service_id}/revoke`

创建和轮换响应只显示一次 `api_key`。调用方必须立即写入受控的 secrets manager；
不要写入仓库、日志、截图或工单。请求示例应使用 `$SERVICE_API_KEY` 占位符，不得放入
真实 key。

## 幂等与批次

所有 ingestion POST 必须同时提供：

- `Idempotency-Key` 请求头：一次 HTTP 操作的幂等标识，8–200 字符。
- body 中的 `producer_key`：采集端稳定生成的业务批次/操作标识。

相同 source 下重复请求返回第一次保存的响应，不重复创建 execution、event、
metric series、observation、artifact 或 runtime interval。相同 key 搭配不同 payload
返回 `409 idempotency_conflict`。事件和遥测还分别要求稳定且唯一的
`producer_event_key`、`producer_series_key`。

## API

1. `POST /api/v1/ingestion/executions`
   - 绑定已有 campaign/cycle/analysis segment/asset/configuration/test-case
     version/station。
   - 创建或幂等恢复 RUNNING execution 和首个 stage。
2. `POST /api/v1/ingestion/executions/{id}/heartbeat`
   - 写入 HEARTBEAT/status event；支持 `running`、`blocked`。
3. `POST /api/v1/ingestion/executions/{id}/events`
   - 最多 1000 个事件的批次。
4. `POST /api/v1/ingestion/executions/{id}/telemetry`
   - 每批最多 20 个 series、每个 series 最多 5000 个降采样点。
   - `sampling_interval_ms >= 10`，方法只允许 `UNIFORM`、`MIN_MAX`、
     `MEAN_WINDOW`、`RMS_WINDOW`。
5. `POST /api/v1/ingestion/executions/{id}/artifacts`
   - 登记 manifest/evidence 外部对象元数据、SHA-256；可同时登记 trajectory
     version。当前 compose 不提供 NAS/对象存储，接口不上传文件字节。
6. `POST /api/v1/ingestion/executions/{id}/finish`
   - `running/passed/failed/blocked` 分别映射
     `RUNNING/COMPLETED/FAILED/BLOCKED`，并在 `test.execution_result` 保留
     `source_status`。
   - `active_seconds` 是采集端报告的实际活跃时长，写入 `runtime_interval`；
     后端只校验其不超过墙钟区间，不会把墙钟时长推导为 active time，也不会自动创建
     MTBF exposure assessment。

断网队列不需要预先知道服务端 execution UUID。注册请求的 `producer_key` 同时作为
`source_execution_key`；后续操作可使用以下等价路径：

- `POST /api/v1/ingestion/executions/by-producer/{producer_execution_key}/heartbeat`
- `POST /api/v1/ingestion/executions/by-producer/{producer_execution_key}/events`
- `POST /api/v1/ingestion/executions/by-producer/{producer_execution_key}/telemetry`
- `POST /api/v1/ingestion/executions/by-producer/{producer_execution_key}/artifacts`
- `POST /api/v1/ingestion/executions/by-producer/{producer_execution_key}/finish`

`producer_execution_key` 长度为 1–200，只允许字母、数字及 `._:@+-`，且首字符必须为
字母或数字。查询始终附带 `execution.source_id =
iam.current_ingestion_source_id()`；相同 producer key 在其他 source 下不可见。原 UUID
路径继续保留。

## 单位责任

API 采用严格规范单位，不做隐式换算。关节角和角速度必须分别发送 `deg`、`deg/s`；
采集工具负责在请求前把 rad、rad/s 转换为规范单位。body 的 `canonical_unit` 必须与
已发布 metric version 完全一致，否则返回 `422 unit_mismatch`。数据库以
`conversion_method=identity:collector-normalized` 记录该责任边界。

## 稳定 telemetry subject 目录

以下代码由 0018/0020 共同维护，`subject_code` 和每个 target part 内的
`display_order` 均稳定且唯一。`rp1_test` 应直接使用这些代码。

- SARM 7DoF：`SARM-SHOULDER-PITCH`、`SARM-ELBOW-PITCH`、
  `SARM-WRIST-ROLL`、`SARM-SHOULDER-ROLL`、`SARM-SHOULDER-YAW`、
  `SARM-WRIST-YAW`、`SARM-WRIST-PITCH`。
- SLEG 6DoF：`SLEG-HIP-PITCH`、`SLEG-KNEE-PITCH`、
  `SLEG-ANKLE-PITCH`、`SLEG-HIP-ROLL`、`SLEG-HIP-YAW`、
  `SLEG-ANKLE-ROLL`。
- UPPER 12DoF：`UPPER-SHOULDER-L`、`UPPER-SHOULDER-R`、
  `UPPER-WAIST-YAW`、`UPPER-SHOULDER-PITCH-L`、
  `UPPER-SHOULDER-PITCH-R`、`UPPER-ELBOW-PITCH-L`、
  `UPPER-ELBOW-PITCH-R`、`UPPER-WRIST-ROLL-L`、
  `UPPER-WRIST-ROLL-R`、`UPPER-WRIST-YAW-L`、
  `UPPER-WRIST-YAW-R`、`UPPER-WAIST-PITCH`。
- LOWER 14DoF：`LOWER-HIP-L`、`LOWER-HIP-R`、`LOWER-WAIST-PITCH`、
  `LOWER-HIP-PITCH-L`、`LOWER-HIP-PITCH-R`、`LOWER-HIP-ROLL-L`、
  `LOWER-HIP-ROLL-R`、`LOWER-HIP-YAW-L`、`LOWER-HIP-YAW-R`、
  `LOWER-KNEE-PITCH-L`、`LOWER-KNEE-PITCH-R`、
  `LOWER-ANKLE-PITCH-L`、`LOWER-ANKLE-PITCH-R`、`LOWER-WAIST-YAW`。

## 前端后续计划（本轮不实现）

- 管理工作台增加服务主体列表、Campaign grant、创建/轮换/吊销操作。
- key 创建/轮换后提供一次性展示与复制确认，不允许再次读取。
- execution 详情增加 ingestion batch、轨迹版本、artifact 哈希和 source status。
- 数据源健康页增加 last-used、最近批次状态和幂等冲突计数。
- 前端不得持有或调用 service key；管理员操作继续使用浏览器 session + CSRF。

## 基础设施边界

当前 compose 只有 PostgreSQL、API、worker 和 frontend，没有 InfluxDB、NAS 或对象
存储。遥测点落 PostgreSQL 的 `health.metric_observation`；artifact/trajectory 仅登记
外部引用和哈希。后续接入外部存储时，不应改变本 ingestion API 的身份、幂等和元数据
语义。
