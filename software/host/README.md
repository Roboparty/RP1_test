# RP1 TEST HMI

PySide6 桌面、本机 FastAPI 网关、Modbus TCP PLC、Linux SocketCAN 与本地记录。包含多窗口共享 PLC 控制、回执、模拟/监控模式、趋势、电机故障恢复、用例选择、核实和 `reliability_v1` 归档。

## 离线运行

在 `software/` 执行：

```bash
python3 -m venv .venv-hmi
.venv-hmi/bin/python -m pip install -r host/requirements.lock
.venv-hmi/bin/python host/launch.py --offline-check
```

离线入口强制专用 mock PLC，禁用 CAN 和平台。仅验证网关可加 `--gateway-only --duration 3`；`--port`、`--state-dir` 可隔离实例。普通启动会读取控制配置，连接控制模式可能写 PLC。

安装包入口为 `rp1-test-hmi`；同一状态目录下用 `status`、`stop` 查询和正常停止。配置、日志、记录和队列保存在用户状态目录，升级流程见 [运维](../docs/operations.md)。

## 操作与协议

- [构建与配置](docs/build.md)：源码运行、依赖、SDK、安装包和设备范围。
- [PLC 对接](docs/plc-integration.md)：版本矩阵、反馈、时限、共享控制与停机。
- [电机故障恢复](docs/MOTOR_FAULT_RECOVERY.md)：保持失能复位、新反馈和重新使能。
- [平台数据流程](../docs/data-workflow.md)：本地记录、归档、核实、审批及失败恢复。

平台登录填写实际 HTTPS 地址与本站 CA。在线心跳独立于上传；记录结束并核实后显式提交，支持续传和驳回重提。“其他”须填写测试名称、对象和工位。

## 源码入口

| 目录 | 职责 |
|---|---|
| `factory_hmi/desktop/` | 测试/PLC 页面、多窗口、趋势和核实 |
| `factory_hmi/gateway/` | 本机 API、WebSocket、会话与租约 |
| `factory_hmi/plc/` | Modbus、状态机、共享控制、反馈和失效保护 |
| `factory_hmi/core/` | 电机后端、控制、轨迹、记录和报告 |
| `factory_hmi/sync/` | 平台会话、心跳、SQLite 队列与归档 |
| `scripts/`、`factory_hmi/config/` | 采样与设备配置模板 |
| `tools/`、`packaging/`、`motors/` | 构建、安装入口和 SDK 源码 |

安装包输出至 `host/dist/`，二进制通过 Releases 分发。HMI 有独立控制与记录实现，不依赖可选 test-runner。软件检查、构建或安装不授权启动真实 PLC/CAN。
