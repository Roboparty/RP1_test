# RP1 Factory HMI

RP1 模块老化与调试的桌面上位机，提供样机配置、CAN 连接、轨迹回放、关节控制、电机监控、标零和执行记录管理。桌面使用 PySide6，控制网关使用 FastAPI。

## 架构

```text
PySide6 桌面 ── HTTP / WebSocket ── 本机控制网关
                                      ├─ 控制状态机、租约、安全保护
                                      ├─ SocketCAN / motors_py
                                      └─ CSV、报告、执行包、SQLite outbox
                                                       └─ 平台接入
```

网关负责运动控制，桌面负责交互与显示。每个模块窗口拥有独立会话和控制租约；同一 CAN 接口只能被一个会话占用。轨迹工具见 [test-runner](../test-runner/README.md)，平台链路示例见 [demo-client](../demo-client/README.md)。

## 快速开始

需要 Python 3.10 及以上。桌面需要 Qt 图形环境；无硬件开发使用 `fake` 后端。

```bash
cd rp1_test/software/host
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[desktop,test]" -c constraints-tested.txt
```

先启动本机网关，再在另一个终端激活同一虚拟环境并启动桌面：

```bash
# 终端 1
RP1_FACTORY_ALLOWED_CONFIG_ROOT="$PWD/scripts/config" \
  python -m factory_hmi.gateway --port 8765 --data-root "$PWD/.runtime/fake"

# 终端 2
RP1_FACTORY_ALLOWED_CONFIG_ROOT="$PWD/scripts/config" \
  python -m factory_hmi.desktop --gateway-url http://127.0.0.1:8765
```

在“样机配置与连接”页选择**仿真（fake）**和示例部位配置，使用合成遥测调试界面与功能。多个开发实例应使用不同端口和数据目录。

命令行入口：`rp1-factory-gateway`、`rp1-factory-hmi`、`rp1-factory-uploader`、`rp1-factory-can-helper`。

## 目录与阅读入口

| 目录 | 入口 | 职责 |
| --- | --- | --- |
| `factory_hmi/desktop` | [app.py](factory_hmi/desktop/app.py)、[client.py](factory_hmi/client.py) | 工作页、监控/标零弹窗、模块窗口和网关客户端 |
| `factory_hmi/core` | [service.py](factory_hmi/core/service.py)、[controller.py](factory_hmi/core/controller.py) | 状态机、轨迹预检与回放、手动移动、安全保护 |
| `factory_hmi/gateway` | [app.py](factory_hmi/gateway/app.py)、[lease.py](factory_hmi/gateway/lease.py) | API、状态推送、控制租约、CAN 独占与失联处理 |
| `factory_hmi/contracts` | [执行包契约](factory_hmi/contracts) | 执行包和老化摘要的 JSON Schema |
| `factory_hmi/sync` | [outbox.py](factory_hmi/sync/outbox.py)、[uploader.py](factory_hmi/sync/uploader.py) | 持久化队列、分块上传、重试及审批状态 |
| `scripts` | [部位配置](scripts/config)、[遥测记录](scripts/log_motor_telemetry.py) | 运动映射、反馈采集、看门狗与电机配置 |
| `tools / packaging / tests` | [工具](tools)、[打包模板](packaging)、[测试](tests) | 构建、部署和开发测试 |

[station_defaults.yaml](factory_hmi/config/station_defaults.yaml) 提供通用配置。实际工位应覆盖关节限位、电气阈值、温度阈值和老化验收条件；200 Hz 是默认控制目标，实际周期需在设备上测量。

## 数据与平台接入

遥测、报告和执行包先保存到本地数据根，上传状态保存到 SQLite outbox。平台不可用时保留待上传记录；同一个 outbox 只由一个上传进程消费。

支持两类接入协议：

- **审批上传**：`/api/auth/station-login`、`/api/submissions`、`/api/uploads`，用于交互式身份与审批流程。
- **可靠性数据接入**：通过执行包桥接调用 [service](../service/README.md) 的 `/api/v1/ingestion`。用法见 [可靠性平台接入](docs/RELIABILITY_BRIDGE.md)。

两类协议的身份、上下文和数据结构不同，需使用对应客户端。

## 真机与打包

Linux 网关通过 SocketCAN 和外部 `motors_py` 控制电机。SDK 必须匹配驱动、Python ABI 和目标平台，并安装到可导入的 Python 路径。仓库提供应用代码、配置和打包模板，SDK、共享库、固件及厂商驱动由使用者另行提供。

```bash
python -m pip install -e ".[build,desktop]" -c constraints-tested.txt
python tools/build_factory_hmi.py all --motor-extension /path/to/motors_py.so
python tools/build_factory_hmi_deb.py --version <release-version>
```

分发包含第三方二进制的安装包时，应遵循对应许可及源码提供要求。现场操作见 [部署指南](docs/FACTORY_DEPLOYMENT.md)，Windows 设备接入要求见 [Windows CAN-FD](docs/windows_zlg_canfd.md)。

软件失能不能替代物理急停。网关保留租约超时、轨迹预检、反馈看门狗、使能失败回滚和标零前失能等保护。

## 开发测试

```bash
PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen \
  python -m pytest -q -p no:cacheprovider tests
```

测试覆盖控制状态机、CAN 策略与租约、客户端、报告和补传、桌面模型及打包模板。硬件相关检查见 [工位验收清单](docs/FACTORY_HMI_ACCEPTANCE.md)。
