# RP1 Factory HMI 部署指南

Linux 工位部署采用 Ubuntu 22.04 x86_64 和版本化 Debian 安装包。源码开发与 `fake` 启动见 [README](../README.md)。

## 进程与权限

- **控制网关**：由 systemd 运行，负责会话、CAN 独占、轨迹回放、安全监测、遥测与标零。
- **桌面客户端**：从应用菜单启动，连接本机网关，显示状态并提交操作。
- **上传服务**：使用配置的无人值守身份处理 SQLite outbox；交互式上传由网关管理。同一 outbox 不得同时由两者处理。

桌面和控制服务以普通账户运行。CAN 配置助手通过 polkit 获取有限权限，只接受 [can-policy.yaml](../packaging/debian/can-policy.yaml) 白名单中的接口与位时序，不接受任意 shell 命令。

## 构建与安装

构建机须提供与 Python ABI 和目标平台一致的 `motors_py` 扩展：

```bash
python -m pip install -e ".[build,desktop]" -c constraints-tested.txt
python tools/build_factory_hmi.py all --motor-extension /path/to/motors_py.so
python tools/build_factory_hmi_deb.py --version <release-version>
sudo apt install ./dist/rp1-factory-hmi_<release-version>_amd64.deb
```

构建会检查控制服务、桌面、权限助手、上传服务和 SDK 扩展。安装包包含所配置的运行组件；第三方 SDK 的再分发须满足其许可要求。

安装后将操作员加入工位组并重新登录：

```bash
sudo usermod -aG rp1-factory <operator>
```

| 路径 | 内容 |
| --- | --- |
| `/etc/rp1-factory-hmi/stations` | 经审核的部位 YAML |
| `/etc/rp1-factory-hmi/gateway.env` | 网关与工位接入参数 |
| `/etc/rp1-factory-hmi/uploader.env` | 无人值守上传参数 |
| `/etc/rp1-factory-hmi/secrets` | 工位令牌、上传身份密钥文件 |
| `/var/lib/rp1-factory-hmi` | 轨迹、记录、执行包及同步队列 |

升级保留配置和运行数据。配置文件与密钥文件应按运行账户设置权限；密钥文件不得对其他用户开放。

## 平台配置

审批协议在 `gateway.env` 中配置 `RP1_FACTORY_PLATFORM_URL`、`RP1_FACTORY_STATION_ID`、`RP1_FACTORY_BENCH_ID` 和 `RP1_FACTORY_MACHINE_TOKEN_FILE`。工位令牌由平台注册流程提供；将文件路径指向 `/etc/rp1-factory-hmi/secrets/machine-token`。

台架编号与样机编号须符合应用的身份格式，例如 `RD-02` 和 `RP1.3-UPPER-01`。测试单号由 HMI 生成。接入本仓库的可靠性数据服务时使用 [执行包桥接](RELIABILITY_BRIDGE.md)，其上下文和授权配置独立于审批协议。

## 标准操作

1. 检查机构支撑、机械限位、防护、负载工装、物理急停和安全回路。
2. 在“样机配置与连接”页选择部位和样机，导入 YAML；核对电机 ID、型号、零偏、Kp、Kd 和方向。
3. 为每个总线组选择未占用的 CAN 接口、CAN/CAN-FD 模式和批准的位时序。
4. 应用模块配置并连接 CAN，确认接口状态、bus-off 和错误计数。
5. 扫描电机，确认数量与配置一致；再次确认工位安全后使能。
6. 导入 NPZ，检查总成、列数、时长、位移、速度和首帧位姿预检结果。
7. 进入 Armed 并启动老化，观察运行时长、循环、关节误差、力矩、温度和错误样本。
8. 停止后查看记录、导出 CSV 或执行包，按接入方式上传。
9. 软件失能、断开 CAN 后再拆卸样品。

“关节控制”用于整组目标角度与移动速度控制。“电机监控”和“电机标零”以独立弹窗打开；标零前需支撑机构并确认机械零位，程序先停止保持并失能，完成后仍保持失能。

并行测试使用独立模块窗口、轨迹和记录目录。不同窗口必须绑定不同 CAN 接口，占用冲突不会抢占已有会话。

## 配置与保护

提供左右长/短臂、左右长/短腿、腰髋、双腿腰部和上半身配置。左右侧零偏不可混用。YAML 保留总线共享拓扑，实际接口与位时序由工位人员在界面确认。

- 软件失能不能替代可切断驱动能源的硬件急停。
- 客户端失联后，网关按租约超时停止、失能并释放对应接口。
- 驱动错误、反馈停止更新和控制心跳超时触发停机；跟踪误差、温度等阈值由工位配置限定。
- 故障诊断显示证据和疑似原因，不能以推断替代确认根因。
- 记录优先落盘，平台不可用不影响本地停机。

## 维护

```bash
systemctl status rp1-factory-gateway.service
journalctl -u rp1-factory-gateway.service
```

更换 SDK、驱动、工位配置或控制参数后，按 [工位验收清单](FACTORY_HMI_ACCEPTANCE.md) 检查。Windows 原生 CAN-FD 的设备与 SDK 要求见 [Windows 接入](windows_zlg_canfd.md)。
