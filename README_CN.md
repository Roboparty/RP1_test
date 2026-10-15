# RP1 Test

[English](README.md) | 简体中文

RP1 模块老化与整机可靠性测试的台架资料、软件和方法。包含手臂、胸腔、腿部负载三类台架，RP1 Factory HMI、轨迹与数据采集工具、数据服务，以及 18 项整机测试方法草案。

![RP1 可靠性测试上位机：老化测试界面](docs/media/factory-hmi.jpg)

## 从这里开始

| 要做什么 | 入口 |
| --- | --- |
| 运行软件 | [HMI 仿真后端](software/host/README.md#快速开始) · [数据服务与合成演示](software/README.md#快速开始) |
| 搭建模块测试台架 | [硬件文件与搭建说明](hardware/README.md) |
| 选择方法并记录测试 | [18 项测试方法](test-cases/README.md) · [检查与执行记录](test-cases/checklist.md) |

## 测试大屏

| 下肢测试大屏 | 上肢测试大屏 |
| --- | --- |
| ![下肢模块测试大屏](docs/media/test-dashboard-lower-body.jpg) | ![上肢模块测试大屏](docs/media/test-dashboard-upper-body.jpg) |

配套平台的模块测试总控与汇总界面。

## 台架测试演示

| 手臂测试 | 腿部测试 |
| --- | --- |
| <img src="docs/media/arm-test.gif" width="320" alt="手臂模块台架测试动图"> | <img src="docs/media/leg-test.gif" width="320" alt="腿部模块台架测试动图"> |

## 项目内容

- **硬件：** 模块测试台架的加工资料、BOM 和控制柜参考架构。
- **软件：** 桌面 HMI、本机控制网关、轨迹工具、FastAPI、PostgreSQL、InfluxDB 和 Grafana。
- **测试方法：** 18 项整机测试方法草案，包含条件、步骤和记录要求。
- **离线分析：** [独立 Python 工具与核对样例](data-analysis/README.md)，统计有效时长、跟随误差、温度、缺失样本和事件。

测试准备与执行参见 [公共规则](test-cases/common.md) 和 [参数清单](test-cases/open-items.md)。

Parasolid 总装模型、STEP 零件模型、加工图纸和 BOM 的入口见 [机械资料](hardware/mechanical/README.md)。三个 `.x_t` 总装模型下载：[RP1_Test_Parasolid_CAD.zip](https://github.com/Roboparty/rp1_test/releases/download/v0.1.0/RP1_Test_Parasolid_CAD.zip)。

## 许可与贡献

软件、硬件、说明文档及第三方资料的适用许可见 [LICENSES.md](LICENSES.md)。

发现文件错误或希望参与开发，可提交 [Issue](https://github.com/Roboparty/rp1_test/issues)，注明模块或用例 ID、对应文件、配置及软件版本。
