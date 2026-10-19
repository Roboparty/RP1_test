# 开发与验证

以下从仓库根目录执行。测试使用合成数据、mock/fake 设备和独立临时数据库，不连接运行站点或真实 PLC/CAN。

## 核心平台

前端使用 Node.js/npm，服务和 HMI 使用各自 Python 环境，按模块的锁文件和开发依赖安装。

```bash
cd software/frontend
npm ci
npm run check
```

后端环境安装 `software/service/requirements-dev.txt` 并以 `requirements.lock` 约束后：

```bash
PYTHONPATH=software/service python3 -m pytest -c software/service/pytest.ini software/service/tests -m 'not integration' -q
PYTHONPATH=software/database python3 -m unittest discover -s software/database/tests
python3 software/scripts/test-archive-deployment-guards.py
python3 software/scripts/check-release.py
```

`not integration` 排除的数据库用例不计入通过项。`DATABASE_URL`、`HMI_TEST_ADMIN_URL` 等不得指向正式站点。

在 `software/` 执行隔离数据库验证：

```bash
docker build -f deployment/ArchiveQa.Dockerfile -t rp1-archive-qa .
python3 scripts/test-archives.py
```

审批、空库建档及历史元数据分别有 `test-approval.py`、`test-setup.py`、`test-history.py`。先核对所选脚本输入要求，未指定授权来源时不使用现场文件替代合成样例。完整 HTTPS 归档、审批与联合备份恢复入口为 `test-archive-deployment.py`，使用新临时项目，完成后清理其容器、网络和卷。

## HMI

安装 `software/host/requirements.lock` 后：

```bash
cd software/host
PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen python3 -m pytest tests -q
python3 launch.py --offline-check --gateway-only --duration 3
```

`--offline-check` 使用专用 mock PLC，禁用 CAN 和平台。普通源码启动可能读取控制配置，不能替代离线检查。构建见 [HMI 构建](../software/host/docs/build.md)。

## 可选工具与演示

在独立 Python 环境，从仓库根目录安装并验证：

```bash
python3 -m pip install -e './software/tools/test-runner[test]' -e './software/examples/telemetry-demo/demo-client[test]'
python3 -m pytest -q software/tools/test-runner/tests
python3 -m pytest -q software/examples/telemetry-demo/demo-client/tests
python3 -m unittest discover -s software/examples/telemetry-demo/scripts/tests
python3 -m unittest discover -s software/examples/telemetry-demo/influxdb/tests
python3 software/examples/telemetry-demo/grafana/scripts/validate_dashboard.py
```

test-runner 的 ingestion 契约测试针对演示 API/schema，不针对当前正式归档 API。演示部署见 [telemetry-demo](../software/examples/telemetry-demo/README.md)。

## CI 与交付

- `.github/workflows/tests.yml`：Python 3.10/3.12 可选工具和演示合同，另行运行 HMI fake 后端及独立演示栈。
- `.github/workflows/platform.yml`：空库 setup 和 lifecycle 使用不同 PostgreSQL 容器，另有前后端、部署守卫、HMI 构建和容器验收。
- `software/scripts/check-release.py`：源码卫生、现行文档和仓库内部链接。

修改后检查 `git diff --check`、失效路径、文档链接和相关构建/测试。CI 以对应提交实际结果为准；离线测试、容器安装、长期稳定性和现场设备验收分别确认。

凭据、运行 CSV、备份、环境和构建产物不进入 Git。HMI 本地交付位于 `software/host/dist/` 的版本/批次子目录，包含安装包、SHA256、构建报告与源码清单；二进制公开分发使用 Releases。
