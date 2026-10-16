# Reliability Platform API

FastAPI提供人类会话、账号权限、样品/计划、在线HMI、中央CSV归档、历史导入、审批、业务查询和MTBF。部署从 [软件入口](../README.md) 开始。

| 入口 | 职责 |
|---|---|
| `app/main.py`、`app/core/` | 应用、会话/CSRF、权限与事务 |
| `app/api/routes/setup.py` | 样品、计划和执行上下文 |
| `app/api/routes/hmi_presence.py` | 在线、并行运行与状态 |
| `app/api/routes/archives.py` | 专用归档、核实、归类、分析与曲线 |
| `app/api/routes/archive_imports.py` | 网页历史CSV识别、预览与归档 |
| `app/api/routes/submissions.py` | 提交版本与审批 |
| `app/repositories/read_models.py` | 大屏与执行查询 |
| `app/services/archive_worker.py` | 文件校验与派生分析 |
| `app/services/worker.py` | MTBF重算 |

```bash
cd software/service
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt -c requirements.lock
python -m pytest -c pytest.ini tests -m 'not integration' -q
```

部署注入数据库、认证和归档目录设置。健康入口 `/healthz`、`/readyz`，OpenAPI `/docs`。文件位于共享持久目录，数据库保存索引及版本。

旧SERVICE ingestion使用 [独立示例](../examples/telemetry-demo/README.md) 的服务，不连接新版平台数据库。
