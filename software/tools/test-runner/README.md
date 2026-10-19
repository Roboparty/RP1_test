# RP1 测试工具

`rp1-test` 提供轨迹生成与录制、插值、单位和边界校验、MuJoCo 回放、硬件回放、反馈 watchdog、运行记录及 SQLite 离线补传。

[Factory HMI](../../host/README.md)负责桌面交互与本机控制；本模块提供独立的命令行轨迹与采集工具。

## 安装

在本目录执行：

```bash
./scripts/bootstrap.sh
source .venv/bin/activate
rp1-test --help
```

运行依赖 Python 3.10+、NumPy 和 PyYAML；测试依赖见 `pyproject.toml`。MuJoCo 为可选仿真依赖，SDK 与机器人模型的安装见 [外部依赖说明](docs/dependencies.md)。

## 合成演示

使用[合成演示配置](../../examples/telemetry-demo/demo-client/config/demo.yaml)，关闭模型指纹要求仅用于合成演示：

```bash
rp1-test trajectory generate --config ../../examples/telemetry-demo/demo-client/config/demo.yaml \
  --profile joint-inspect --params '{"joint":"demo_elbow","amplitude_rad":0.2}' \
  --output demo.npz
rp1-test trajectory validate demo.npz --config ../../examples/telemetry-demo/demo-client/config/demo.yaml
rp1-test sim replay demo.npz --config ../../examples/telemetry-demo/demo-client/config/demo.yaml --headless --dry-run
```

模拟器 dry-run 校验输入；硬件 dry-run 校验与机器人匹配的真实模型指纹。具体配置与门控见 [SOP](docs/SOP.md)。

## 真机入口

```bash
rp1-test hardware preflight motion.npz --config config/rp1_test.example.yaml \
  --model /path/to/approved-release/rp1_description/mjcf/rp1_flat.xml
rp1-test hardware replay motion.npz --config config/rp1_test.example.yaml \
  --model /path/to/approved-release/rp1_description/mjcf/rp1_flat.xml --dry-run
```

真实执行需同时提供 `--real-hardware --acknowledge I_UNDERSTAND_RP1_MOTION`。交互键为 R 回默认、P 播放、Space 暂停、Q 正常回默认后退出。执行前检查 SHA-256、关节顺序、指纹、默认位姿和位置/速度/加速度；渐入与回默认从实际反馈位置生成，并按同一限值校验。异常或中断时直接逐台尝试失能，不再回默认；NaN/Inf 位置反馈触发故障。SDK 只在真机确认后加载。示例限位和增益必须替换为该设备已批准参数。

## 本地数据与可选演示补传

`platform` 子命令仅兼容[独立遥测演示](../../examples/telemetry-demo/README.md)的旧 ingestion 接口，不适配当前 `reliability_v1`。当前平台的正式 CSV 归档、核实、审批与统计流程见[数据流程](../../docs/data-workflow.md)，不得用本工具的 ingestion 或附件元数据替代。

演示 Compose 的默认 API 端口为 `18080`；按演示部署修改 `--base-url`。

本地输出 `telemetry.csv`、`events.jsonl`、`native.log`、配置快照、`metadata.json`、`manifest.json`。本地角度为rad，上传时统一转换为deg；跟随误差按同一行`actual−target`派生为`JOINT-TRACKING-ERROR`（deg），两者均有限时才生成，缺失不补零；缺失的可选反馈写NaN。硬件 `active_seconds` 只累计轨迹播放区间，排除渐入、回默认和操作员等待；是否能纳入某个用例有效时间由用例规则确认。

```bash
rp1-test platform enqueue-run runs/RUN_ID --outbox .rp1-test/outbox.sqlite3
rp1-test platform flush --base-url http://127.0.0.1:18080 --outbox .rp1-test/outbox.sqlite3
```

补传使用 `RP1_TEST_API_TOKEN` 环境变量；入队前需要快照中的七个演示 public ID。[摄取协议](docs/ingestion-contract.md)使用 `/api/v1/ingestion`；SQLite 保存幂等键和服务器返回的 execution/stage ID，断网后可继续重试。本地文件通过附件元数据登记，原始文件保存在运行目录。

`completed` 表示工具完成，上传判定为 `INCONCLUSIVE`；仅显式 `passed` 状态表示按判据确认通过。合成运行始终为 `INCONCLUSIVE`。整机[用例](../../../test-cases/README.md)中的站立、行走、抓取和环境箱试验按各自步骤执行；轨迹回放只是其中一种测试手段。

## 读代码从哪里开始

| 路径 | 功能 |
|---|---|
| `src/rp1_test/cli.py` | 参数解析与命令分发 |
| `src/rp1_test/commands/` | 按轨迹、仿真、硬件、平台拆分的命令实现 |
| `trajectory.py`、`generators.py`、`recording.py` | 轨迹格式、生成和录制 |
| `validation.py`、`mapping.py`、`units.py` | 校验、映射和单位 |
| `hardware.py`、`watchdog.py`、`adapters/` | 设备回放、反馈保护与SDK适配 |
| `artifacts.py`、`run_ingestion.py`、`api.py`、`outbox.py` | 落盘、协议转换和补传 |
| `tests/` | 轨迹、门控、计时、落盘与演示契约验证 |

在本目录运行 `python -m pytest`。演示契约测试读取 [演示 Service schema](../../examples/telemetry-demo/service/app/schemas/ingestion.py)和[演示数据库 catalog](../../examples/telemetry-demo/database/sql/020_edge_collector_ingestion.sql)。采集范围见[遥测接入](docs/telemetry-integration.md)，分析方法见 [data-analysis](../../../data-analysis/README.md)。
