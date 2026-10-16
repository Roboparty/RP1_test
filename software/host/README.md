# RP1 TEST HMI（PLC/CAN）

PySide6桌面、本机FastAPI网关、Modbus TCP PLC、SocketCAN控制和本地记录源码。本版包含共享PLC控制、命令版本/回执、模拟/监控模式、趋势、故障处理、用例选择、核实、在线工位和 `reliability_v1` 归档。

## 离线运行

在 `software/` 中：

```bash
python3 -m venv .venv-hmi
.venv-hmi/bin/python -m pip install -r host/requirements.lock
.venv-hmi/bin/python host/launch.py --offline-check
```

该入口强制专用mock配置，禁用CAN和平台。仅验证网关可加 `--gateway-only --duration 3`。默认端口8766；`--port`、`--state-dir` 可隔离实例。

安装包使用 `rp1-test-hmi` 启动；`rp1-test-hmi status`、`rp1-test-hmi stop` 管理自己的实例。配置、日志和数据保存在用户状态目录。

## 源码

| 目录 | 职责 |
|---|---|
| `factory_hmi/desktop/` | 测试/PLC页、模块窗口、监控和核实 |
| `factory_hmi/gateway/` | 本机API、WebSocket、会话与租约 |
| `factory_hmi/plc/` | Modbus、状态机、共享控制、时效与故障 |
| `factory_hmi/core/` | 后端、测试服务、记录与报告 |
| `factory_hmi/sync/` | 平台会话、presence、SQLite队列与归档 |
| `scripts/`、`factory_hmi/config/` | 采样、部位与设备配置模板 |
| `tools/`、`packaging/`、`motors/` | 构建、安装入口与SDK源码 |

## 平台与协议

从 [软件主入口](../README.md) 启动匹配平台，网页登录改密后在HMI登录。配置实际HTTPS地址和本站CA。在线心跳独立；CSV先保存本地，核实并显式提交后归档，支持恢复、审批和驳回重提。“其他”填写名称、对象和工位后可先归档。

[V1.0](docs/HOST_MODBUS_TCP_PROTOCOL_V1.0.md) · [V1.1](docs/HOST_MODBUS_TCP_PROTOCOL_V1.1.md) · [V1.2](docs/HOST_MODBUS_TCP_PROTOCOL_V1.2.md) · [PLC映射](docs/PLC_INTERFACE_MAPPING.md) · [本地构建](docs/LOCAL_CONTROL_BUILD.md) · [故障恢复](docs/MOTOR_FAULT_RECOVERY.md)

在 `software/` 执行 `bash scripts/build-hmi.sh`，输出到 `host/dist/`。SDK从源码构建，保留原许可通知。

```bash
cd software/host
PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen python3 -m pytest tests -q
```

软件修改和安装不启动现场PLC/CAN；实际工位按协议与设备配置操作。
