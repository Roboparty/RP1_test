# RP1 测试大屏与 PLC 上位机

完整 React 大屏、FastAPI 可靠性平台、PostgreSQL、MTBF/归档 Worker 和 PySide6 PLC/CAN 上位机。平台协议为 `reliability_v1`。

仓库软件版本 **0.2.0**；平台组件 **2.0.6**，HMI源码包 **2.0.9+ubuntu24.04**。来源和文件清单见 [SOURCE-MANIFEST.json](SOURCE-MANIFEST.json)，功能见 [更新说明](docs/RELEASE-v0.2.0.md)。

## 启动平台

目标为 Ubuntu 24.04 amd64，准备 Docker Engine、Compose、Python 3 和 OpenSSL。以下命令在 `software/` 下运行：

```bash
python3 scripts/configure.py
# 局域网可改用：python3 scripts/configure.py --hostname SERVER_IP_OR_DNS --bind 0.0.0.0
bash scripts/up.sh
```

脚本生成新站点密码、`.env`、CA和HTTPS证书，拒绝覆盖已有安装。在浏览器/HMI机器信任 `deployment/secrets/root-ca.crt`，通过配置的地址访问（默认 `https://localhost:8443`）。初始账号位于本机 `deployment/secrets/initial-admin.txt`，首次登录须改密。

便捷启动为 `bash scripts/start.sh`；管理服务使用 `scripts/compose.sh`，停止用 `bash scripts/stop.sh`。

## 软件入口

| 模块 | 内容 |
|---|---|
| [frontend](frontend/README.md) | 总控、样品/计划、时长/MTBF、工位、审批与曲线 |
| [service](service/README.md) | 账号、上下文、心跳、归档、历史CSV导入与查询 |
| [database](database/README.md) | 当前迁移链、角色、目录与统计规则 |
| [host](host/README.md) | PLC/CAN控制、窗口、监控、记录、核实与同步 |
| [test-runner](test-runner/README.md) | 独立轨迹工具 |
| [离线分析](../data-analysis/README.md) | 独立记录分析与合成样例 |
| [遥测演示](examples/telemetry-demo/README.md) | 独立 ingestion/InfluxDB/Grafana 合成栈 |

网页历史CSV导入入口为 `?view=csv-import`，包含文件识别、上下文确认、核实和专用归档。

## HMI源码运行与构建

```bash
python3 -m venv .venv-hmi
.venv-hmi/bin/python -m pip install -r host/requirements.lock
.venv-hmi/bin/python host/launch.py --offline-check
```

`--offline-check` 使用专用mock PLC，禁用CAN和平台连接。真实工位参数、本站CA和账号按 [HMI说明](host/README.md) 配置。

Ubuntu 24.04安装包构建：

```bash
sudo bash scripts/install-build-deps-ubuntu24.sh
bash scripts/build-hmi.sh
```

产物位于 `host/dist/`，安装包与SHA256通过Releases分发；源码仓库保留构建工具和SDK源码。

## 数据与维护

工位先保存CSV/报告，核实并显式提交后中央归档，工位保留副本。PostgreSQL保存身份、索引、核实/归类/审批；文件由API和归档Worker共享持久卷。审批与在线状态独立，批准不自动形成合格MTBF暴露。

首次安装使用新项目和新卷。旧遥测演示数据库在0023之后使用另一迁移链，由独立示例管理。已有同架构平台升级先备份并在隔离恢复副本验证。详见 [运维](docs/OPERATIONS.md)。

```bash
python3 scripts/verify.py
python3 scripts/backup.py --maintenance
```

## 开发验证

```bash
(cd frontend && npm ci && npm run check)
PYTHONPATH=service python3 -m pytest -c service/pytest.ini service/tests -m 'not integration' -q
(cd host && QT_QPA_PLATFORM=offscreen python3 -m pytest tests -q)
python3 scripts/test-archive-deployment-guards.py
python3 scripts/check-release.py
```

隔离数据库验证：构建 `deployment/ArchiveQa.Dockerfile` 后执行 `python3 scripts/test-archives.py`。完整HTTPS部署及联合恢复入口为 `python3 scripts/test-archive-deployment.py`，使用新临时项目和合成样例。
