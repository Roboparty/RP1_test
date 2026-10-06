# Service / API

FastAPI 服务连接上位机、PostgreSQL 和监控链路，提供运行注册、心跳、事件、遥测、附件元数据、结束记录和业务查询接口，管理身份权限、幂等写入与可靠性统计。

## 代码阅读入口

按“接口 → 数据校验 → 业务 → 持久化”的顺序阅读：

| 入口 | 职责 |
|---|---|
| [app/main.py](app/main.py) | 应用、路由、错误响应与管理员初始化 |
| [app/api/routes/ingestion.py](app/api/routes/ingestion.py) | 采集端HTTP入口与身份要求 |
| [app/schemas/ingestion.py](app/schemas/ingestion.py) | 请求字段、单位、时间区间与批次约束 |
| [app/services/ingestion.py](app/services/ingestion.py) | 采集业务调用，含离线来源键解析 |
| [app/repositories/ingestion.py](app/repositories/ingestion.py) | 关联检查、幂等收据及事实写入 |
| [app/services/influx.py](app/services/influx.py) | 规范遥测转换为Influx line protocol |
| [app/integrations/influxdb.py](app/integrations/influxdb.py) | Influx OSS 2 HTTP写入适配 |
| [app/repositories/influx_exports.py](app/repositories/influx_exports.py) | 导出队列、租约与失败退避 |
| [app/services/influx_worker.py](app/services/influx_worker.py) | 调度导出与确认完成 |
| [app/domain/mtbf.py](app/domain/mtbf.py) / [app/services/worker.py](app/services/worker.py) | 可靠性统计规则与重算任务 |
| [scripts/bootstrap_demo.py](scripts/bootstrap_demo.py) | 显式建立合成上下文与采集服务凭据 |

## 启动与开发

推荐使用[软件总览](../README.md)的Compose流程。单独开发可在本目录运行：

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
# DATABASE_URL及其他配置从部署环境注入；数据库先完成迁移。
uvicorn app.main:app --host 127.0.0.1 --port 8000
python -m pytest -q -m "not integration"
```

API文档位于 `/docs`，健康检查为 `/healthz`、数据库就绪检查为 `/readyz`。

后台任务使用同一应用数据库账号，分别启动：

```bash
python -m app.services.worker
python -m app.services.influx_worker
```

## 数据链路

```text
上位机 / test-runner
    → API：Bearer身份 + Idempotency-Key
    → PostgreSQL：事实 + 幂等收据 + 导出队列（同一事务）
    → influx-worker：重试写入
    → InfluxDB：joint_telemetry
    → Grafana：监控曲线
```

`/api/v1/ingestion/executions` 注册后返回 `execution_id` 和 `stage_id`；后续接口使用这两个标识。也支持以来源运行键补传。详见[接入契约](docs/ingestion.md)。

开启 `INFLUX_ENABLED=true` 时，成功响应表示事实和导出任务已持久化；Influx投影由Worker异步完成。停机、超时或401响应保留队列并退避重试。Worker崩溃后的租约5分钟到期，可被其他Worker重新领取。重放相同batch不会新增队列；重复导出以相同标签、时间和字段覆盖同一Influx点。

## 配置

| 环境变量 | 说明 |
|---|---|
| `DATABASE_URL` | PostgreSQL应用角色连接，SQLAlchemy `postgresql+psycopg://` 格式 |
| `AUTH_MODE` | 默认 `session`；采集端使用独立服务Bearer |
| `SERVICE_API_KEY_HMAC_SECRET` | 服务凭据HMAC密钥；API与演示初始化保持一致 |
| `SEED_ADMIN_USERNAME` / `SEED_ADMIN_PASSWORD` | 首次管理员初始化；密码至少12字符 |
| `WORKER_USER_PUBLIC_ID` | 内部重算/导出主体，默认迁移创建的Worker UUID |
| `WORKER_POLL_SECONDS` | 空闲轮询间隔，默认1秒 |
| `INFLUX_ENABLED` | 默认false；Compose开启持久导出 |
| `INFLUX_URL` | 默认 `http://influxdb:8086` |
| `INFLUX_ORG` / `INFLUX_BUCKET` | 默认 `rp1_test` / `telemetry` |
| `INFLUX_TOKEN` | Influx写入token，由部署注入 |
| `INFLUX_TIMEOUT_SECONDS` | HTTP超时，默认20秒 |

原始高频CSV/视频继续由上位机保存；API登记文件引用与SHA-256，当前不上传文件字节。有效运行时间、可靠性暴露时间和墙钟时长采用各自口径。合成运行在注册时设置 `source_metadata.source_kind=synthetic`，监控与分析应按此标签筛选。

真机控制由本机设备适配层负责，用例结果由事先确定的规则判定。业务查询与遥测展示不触发设备运动。
