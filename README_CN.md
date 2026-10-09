# RP1 Test

[English](README.md) | 简体中文

RP1 模块老化与整机可靠性测试的台架资料、软件和方法。包含手臂、胸腔、腿部负载三类台架，RP1 Factory HMI、轨迹与数据采集工具、数据服务，以及 18 项整机测试方法草案。

![RP1 可靠性测试上位机：老化测试界面](docs/media/factory-hmi.jpg)

## 从这里开始

| 要做什么 | 入口 |
| --- | --- |
| 无需机器人试用软件 | [HMI 仿真后端](software/host/README.md#快速开始) · [数据服务与合成演示](software/README.md#快速开始) |
| 搭建模块测试台架 | [硬件文件与搭建说明](hardware/README.md) |
| 选择方法并记录测试 | [18 项测试方法](test-cases/README.md) · [检查与执行记录](test-cases/checklist.md) |

HMI 的 fake 后端不需要电机 SDK。数据服务演示生成双关节合成记录，模拟 12 秒运行，其中暂停 2 秒、有效任务时间 10 秒；结果保持为 `INCONCLUSIVE`，用于验证软件链路。

## 测试大屏

| 下肢测试大屏 | 上肢测试大屏 |
| --- | --- |
| ![下肢模块测试大屏](docs/media/test-dashboard-lower-body.jpg) | ![上肢模块测试大屏](docs/media/test-dashboard-upper-body.jpg) |

截图展示配套平台的模块测试汇总界面，大屏前端代码未包含在本仓库中。

## 台架测试演示

| 手臂测试 | 腿部测试 |
| --- | --- |
| <img src="docs/media/arm-test.gif" width="320" alt="手臂模块台架测试动图"> | <img src="docs/media/leg-test.gif" width="320" alt="腿部模块台架测试动图"> |

## 资料与范围

- **硬件：** 三类台架的机械 CAD、图纸和 BOM，以及控制柜参考架构。尚未提供电气施工原理图、I/O 表、端子表和逐芯线束图。
- **软件：** 桌面 HMI、本机控制网关、轨迹工具、FastAPI、PostgreSQL、InfluxDB 和 Grafana。真机 SDK、CAN 配置及控制柜 PLC 接口需在目标设备上适配。
- **测试方法：** 18 项草案，包含条件、步骤和记录要求。F0～F3 检查分级与通过判据需由项目负责人批准；仓库未提供已完成的整机资格测试结果。
- **离线分析：** [独立 Python 工具与核对样例](data-analysis/README.md)，统计有效时长、跟随误差、温度、缺失样本和事件，无需数据服务；尚未提供跨运行对比和 HMI 执行包自动转换。

用例参数为 RP1 参考工况。执行前阅读 [公共规则](test-cases/common.md) 和 [执行前参数清单](test-cases/open-items.md)，确认对实际设备的适用范围。

原生 CAD 包从 [Releases](https://github.com/Roboparty/rp1_test/releases/tag/v0.1.0) 下载，在仓库根目录解压所需附件即可，外层文件夹可以任意命名。总装路径见 [CAD 下载](hardware/mechanical/README.md#下载-cad)，加工图和 BOM 保留在仓库中。

## 许可与贡献

软件、硬件、说明文档及第三方资料的适用许可见 [LICENSES.md](LICENSES.md)。

发现文件错误或希望参与开发，可提交 [Issue](https://github.com/Roboparty/rp1_test/issues)，注明模块或用例 ID、对应文件、配置及软件版本。
