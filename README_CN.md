<div align="center">

# RP1 Test

**RP1 模块老化与整机可靠性测试**

台架设计 · PLC/CAN 上位机 · 测试大屏 · 数据归档与分析

[English](README.md) · **简体中文**

[运行软件](software/README.md) · [搭建台架](hardware/README.md) · [测试方法](test-cases/README.md)

</div>

RP1 Test 将台架、设备控制、过程记录和可靠性数据放在同一套工程中。软件 0.2.0 包含 React 测试大屏、PySide6 上位机、FastAPI 与 PostgreSQL；硬件资料覆盖手臂、胸腔和腿部负载台架，配套 18 项整机测试方法与离线分析工具。

| 手臂台架与快拆结构 | 胸腔台架 | 腿部负载台架 |
| :---: | :---: | :---: |
| <img src="docs/media/showcase/arm-rig-quick-release.jpg" width="260" alt="手臂台架和快拆安装结构渲染"> | <img src="docs/media/showcase/torso-rig.jpg" width="260" alt="胸腔及上肢台架渲染"> | <img src="docs/media/showcase/leg-load-rig.jpg" width="260" alt="腿部负载台架渲染"> |

## 从这里开始

| 要做什么 | 入口 |
|---|---|
| 启动测试大屏与可靠性平台 | [软件总览](software/README.md) · [安装与运维](software/docs/operations.md) |
| 运行或构建 PLC/CAN 上位机 | [HMI 源码、mock 运行与打包](software/host/README.md) |
| 加工、装配模块测试台架 | [硬件总览](hardware/README.md) · [机械文件](hardware/mechanical/README.md) |
| 选择方法并记录测试 | [18 项测试方法](test-cases/README.md) · [检查与执行记录](test-cases/checklist.md) |
| 分析本地记录 | [离线分析工具与样例](data-analysis/README.md) |

架构、数据流程和开发说明见 [文档导航](docs/README.md)。

## 软件界面

### 测试大屏

按模块查看测试项目、样品、执行状态和时长，进入台账、MTBF、工位监控、归档及详细曲线。

| 单臂模块测试总控 | 下肢模块测试总控 |
| :---: | :---: |
| [<img src="docs/media/showcase/arm-dashboard.jpg" width="520" alt="单臂模块测试大屏">](docs/media/showcase/arm-dashboard.jpg) | [<img src="docs/media/showcase/lower-body-dashboard.jpg" width="520" alt="下肢模块测试大屏">](docs/media/showcase/lower-body-dashboard.jpg) |

### PLC 上位机

本机网关连接 PLC/CAN，桌面提供测试控制、电源与通道状态、电压/电流趋势、关节监控和本地记录。

[![PLC 控制、电源设定、四通道状态与实时趋势](docs/media/showcase/plc-control.jpg)](docs/media/showcase/plc-control.jpg)

| 老化测试工作区 | 电机监控 |
| :---: | :---: |
| [<img src="docs/media/showcase/aging-workspace.jpg" width="520" alt="上位机老化测试工作区">](docs/media/showcase/aging-workspace.jpg) | [<img src="docs/media/showcase/motor-monitor.jpg" width="520" alt="电机位置、速度、力矩、温度及故障监控">](docs/media/showcase/motor-monitor.jpg) |

截图可点击查看原图。新版还包括中央 CSV 归档、核实审批、网页历史导入、视窗曲线查询和多执行比较，入口见 [软件说明](software/README.md)。

## 台架运行演示

### 单臂负载

| 2.5 kg 负载 | 3.5 kg 负载 | 5.5 kg 负载 |
| :---: | :---: | :---: |
| <img src="docs/media/showcase/arm-load-2_5kg.gif" width="300" alt="2.5 kg 单臂负载运行片段"> | <img src="docs/media/showcase/arm-load-3_5kg.gif" width="300" alt="3.5 kg 单臂负载运行片段"> | <img src="docs/media/showcase/arm-load-5_5kg.gif" width="300" alt="5.5 kg 单臂负载运行片段"> |

### 下肢负载

| 50 kg 负载踏步 | 50 kg 负载起落 |
| :---: | :---: |
| <img src="docs/media/showcase/leg-load-50kg-stepping.gif" width="300" alt="50 kg 下肢负载踏步片段"> | <img src="docs/media/showcase/leg-load-50kg-rise.gif" width="300" alt="50 kg 下肢负载起落片段"> |

### 上肢动态工况

<img src="docs/media/showcase/upper-body-dynamic-front.gif" width="300" alt="上肢动态测试">

动图为原视频的 6 秒节选，保持原速和画面比例。素材索引与复现方式见 [media](docs/media/README.md)。

## 运行软件

Ubuntu 24.04 amd64，准备 Docker Engine、Compose、Python 3 和 OpenSSL 后：

```bash
cd software
python3 scripts/configure.py
bash scripts/up.sh
```

按 [软件安装说明](software/README.md) 信任本站 CA，访问配置的 HTTPS 地址，默认 `https://localhost:8443`。HMI 可使用专用 mock 模式启动，源码运行与安装包构建见 [上位机说明](software/host/README.md)。

工位先保存 CSV 和报告，核实并显式提交后归档至平台，工位保留副本。账号权限、在线监控和测试数据审批各自独立；详细流程见 [软件入口](software/README.md)。

## 仓库内容

| 目录 | 内容 |
|---|---|
| [hardware/](hardware/README.md) | 台架模型、加工图纸、BOM、电气和装配资料 |
| [software/frontend/](software/frontend/README.md) | React 测试大屏与曲线工作区 |
| [software/host/](software/host/README.md) | PLC/CAN 上位机、本机网关及构建工具 |
| [software/service/](software/service/README.md) · [database/](software/database/README.md) | API、中央归档、Worker、数据库与迁移 |
| [test-cases/](test-cases/README.md) | 整机方法、公共规则及执行检查表 |
| [data-analysis/](data-analysis/README.md) · [test-runner/](software/tools/test-runner/README.md) | 离线分析与独立轨迹工具 |
| [独立遥测演示](software/examples/telemetry-demo/README.md) | InfluxDB/Grafana 合成示例 |

测试准备参见 [公共规则](test-cases/common.md) 和 [参数清单](test-cases/open-items.md)。三个 Parasolid 总装模型下载：[RP1_Test_Parasolid_CAD.zip](https://github.com/Roboparty/rp1_test/releases/download/v0.1.0/RP1_Test_Parasolid_CAD.zip)；STEP、图纸和 BOM 见 [机械资料](hardware/mechanical/README.md)。

## 许可与贡献

各类软件、硬件、文档和第三方资料的许可见 [LICENSES.md](LICENSES.md) 与 [THIRD_PARTY.md](THIRD_PARTY.md)。

欢迎提交 [Issue](https://github.com/Roboparty/RP1_test/issues)，附模块或用例 ID、相关文件及软件/配置版本。
