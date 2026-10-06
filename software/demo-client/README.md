# 合成采集与补传演示

`rp1-demo` 生成合成双关节记录，用于开发和联调采集格式、离线队列及可靠性平台接口。桌面上位机见 [RP1 Factory HMI](../host/README.md)。

## 安装

在 `software/` 下执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e './test-runner[test]' -e './demo-client[test]'
source .venv/bin/activate
rp1-demo --help
```

也可使用 `python -m rp1_demo`。真实轨迹生成、录制、preflight和回放使用同环境中的 `rp1-test`，不重复设备控制代码。

## 本地演示

```bash
rp1-demo demo --config demo-client/config/demo.yaml \
  --output-dir .runtime/runs --run-id DEMO-local --duration 12 --pause 4:2
```

输出遥测 CSV、事件 JSONL、配置快照、metadata、manifest 和 native log。演示在数秒内生成一个 12 秒的**模拟 UTC 区间**；4–6 秒为暂停，有效时间为 10 秒。事件和遥测使用同一时间起点，记录和元数据标记为 `synthetic`，用于软件联调。

## 接入平台与离线补传

Compose默认API端口为`18080`；直接启动开发API时通常为`8000`，按部署修改`--base-url`。DEMO配置将两个关节映射到整机右臂主体`SYS-SHOULDER-PITCH-R`、`SYS-ELBOW-PITCH-R`（数据库0025），与整机演示上下文对应；真实单臂配置继续使用SARM主体。

先按 [Service 说明](../service/README.md)启动演示平台，运行 bootstrap 得到 `.runtime/context.json`。该文件包含数据库生成的七个 public ID；配置中的占位 UUID 需要替换。

```bash
rp1-demo demo --config demo-client/config/demo.yaml \
  --context .runtime/context.json --output-dir .runtime/runs \
  --run-id DEMO-connected --duration 12 --pause 4:2 \
  --enqueue --outbox .runtime/outbox.sqlite3
rp1-demo flush --base-url http://127.0.0.1:18080 \
  --context .runtime/context.json --outbox .runtime/outbox.sqlite3
rp1-demo status --outbox .runtime/outbox.sqlite3
```

可用 `--upload --base-url ...` 替代 `--enqueue`，生成后立即尝试上传。凭据按 `--token-file`、`--token-env RP1_TEST_API_TOKEN`、`context.api_key` 读取；不会写入运行快照或命令输出。运行编号不可重复，用于目录与稳定幂等键。

断网时入队不需要网络，失败请求与后续依赖项留在SQLite。重试沿用2–300秒退避；稍后再次执行 `flush`，直到输出 `pending=0`。重复入队不会增加相同请求；create响应中的execution/stage ID会持久化，遥测上传时补齐stage ID。文件内容仍保存在本地，仅上传文件元数据。

## 输入输出

| 内容 | 约定 |
|---|---|
| 演示配置 | `config/demo.yaml`，只允许DEMO双关节 |
| 运行上下文 | 七个public ID；可平铺或置于`execution_context`对象中 |
| 遥测 | `time_s`、关节名/索引、cmd/pos、speed、torque、temperature、error_id；本地rad |
| 阶段信息 | `phase`、`task_time_s`、`source_kind` |
| 计时 | `confirmed_active_intervals`与`active_seconds`；暂停不计入 |
| 上传结果 | `source_status=blocked`、`outcome=INCONCLUSIVE`，不自动判定通过 |

接真机时采用runner的硬件入口、配置、SDK延迟加载、门控和watchdog。平台接收的角度为deg，转换集中在runner协议模块。演示客户端不直连PostgreSQL或InfluxDB。

## 读代码从哪里开始

| 路径 | 职责 |
|---|---|
| `src/rp1_demo/cli.py` | CLI参数与命令分发 |
| `src/rp1_demo/demo.py` | 合成记录、阶段与确认计时 |
| `src/rp1_demo/platform.py` | public ID上下文、凭据读取与补传调用 |
| `../test-runner/src/rp1_test/` | 共享采集、设备执行、数据协议与SQLite队列 |

采集记录和补传复用 `test-runner` 的 `RunArtifacts`、`enqueue_run`、`SQLiteOutbox` 和 `IngestionClient`。在本目录运行 `python -m pytest` 执行开发测试；[examples](examples/README.md)说明演示输出。
