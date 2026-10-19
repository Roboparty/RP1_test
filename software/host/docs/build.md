# HMI 源码与构建

目标为 Ubuntu 24.04 amd64，源码根目录为 `software/host/`。包版本取自 `packaging/LOCAL_VERSION`；平台版本独立管理。所有命令从 `software/` 执行。

## 离线运行

```bash
python3 -m venv .venv-hmi
.venv-hmi/bin/python -m pip install -r host/requirements.lock
.venv-hmi/bin/python host/launch.py --offline-check --gateway-only --duration 3
```

去掉 `--gateway-only --duration 3` 可以打开离线桌面。`--offline-check` 强制专用 mock PLC，禁用 CAN 和平台；`--offline-access monitor` 验证只读模式。用 `--port` 与 `--state-dir` 隔离实例。

普通 `host/launch.py` 或 `host/start-hmi.sh` 会读取用户/模板控制配置，可能连接实际 PLC；不能用于无设备软件验证。控制模式连接会发送心跳和全 OFF 安全初始化，连接不是只读动作。

## 安装包构建

```bash
sudo bash scripts/install-build-deps-ubuntu24.sh
bash scripts/build-hmi.sh
```

脚本在 `host/build/` 建立 Python 运行库、从 `host/motors/` 构建 SDK，执行 mock/offscreen、SDK 加载、输入与 CAN 策略检查，再组装 deb。使用全新的构建目录，不能覆盖正在使用的运行库。

已有匹配、通过原生加载验证的 Python 3.12 运行库时，可直接组装到新批次目录：

```bash
python3 host/tools/build_local_deb.py --runtime-dir /已验证运行库 \
  --output-dir host/dist/本次批次
```

构建不启动设备。源码、配置和启动器来自本 checkout，SDK 在打包前检查实际加载依赖与驱动 API，不创建电机或 CAN socket。产物包含 deb、SHA256SUMS、build-report.json 和包内 source-manifest.json；公开二进制通过 Releases 分发，产物不进入 Git。

## 配置与设备范围

首次启动复制模板到用户状态目录，之后保留用户配置。YAML 引用相对配置根目录，越界路径拒绝。默认状态目录为 `~/.local/state/rp1-test-hmi/`，实例查询和停止须使用相同目录。

- PLC 使用 Modbus TCP；实际 IP、Unit ID、访问模式和现场参数须逐项核对。控制要求 V1.2，V1.0/V1.1 仅监控，见 [协议与对接](plc-integration.md)。
- 物理电机使用 Linux SocketCAN，配置接口通过 root 所有的受限 CAN 助手和管理员认证；“连接 CAN”不等于扫描、使能或运动。
- 平台连接填写实际 HTTPS 地址与本站 CA；数据核实和提交见 [数据流程](../../docs/data-workflow.md)。
- Windows 原生周立功 CAN-FD 不作为现成后端提供，见 [接入要求](windows_zlg_canfd.md)。

`--runtime-dir` 只选择依赖，不切换应用源码。安装包不自动启动硬件，升级后已有进程仍须正常退出再启动，见 [安装与运维](../../docs/operations.md)。

## 验证入口

```bash
cd host
QT_QPA_PLATFORM=offscreen python3 -m pytest tests -q
python3 tools/verify_native_sdk.py --runtime-dir /已验证运行库
python3 tools/verify_setpoint_input.py --runtime-dir /已验证运行库
python3 tools/verify_can_support.py --runtime-dir /已验证运行库
```

测试环境须安装锁文件中的 pytest/Qt 等依赖。SDK 加载、容器安装和 mock 回归分别验证软件条件，不证明真实反馈、急停、安全停机或长期稳定性。配套 PLC 源码、TIA 编译身份和现场验收需要独立核对。
