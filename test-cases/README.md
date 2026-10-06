# 测试用例

先通过站立和行走老化暴露热、磨损与连接问题，再叠加负载、环境和典型任务，检查机器人能否持续完成工作以及试后是否退化。

包含 18 项方法草案，参数与判据需在实测前确认。用例 ID 保持稳定，编号不连续。配置简称：24/28/30 为自由度，运控/交互为头部配置。

## 用例索引

| 方向 | 用例 | 核心条件 / 目标 |
|---|---|---|
| 持续运行 | [001 站立老化](whole-body/SYS-REL-001-standing-aging.md) | 3 h × 3 周期；有效站立 9 h |
| 持续运行 | [002 行走老化](whole-body/SYS-REL-002-walking-aging.md) | 50 km；≥300 次姿态循环 |
| 持续运行 | [003 带载站立](whole-body/SYS-REL-003-loaded-standing.md) | 固定批准负载；3 h × 3 周期 |
| 温湿环境 | [004 高温运行](whole-body/SYS-REL-004-high-temperature-operation.md) | 40±2 ℃；带电运行 2 h |
| 温湿环境 | [005 低温启动](whole-body/SYS-REL-005-cold-start.md) | −10±2 ℃；5 次启动；末次运行 60 min |
| 温湿环境 | [006 温度循环](whole-body/SYS-REL-006-temperature-cycling.md) | −10～40 ℃；端点各 2 h；5 循环 |
| 温湿环境 | [007 湿热](whole-body/SYS-REL-007-damp-heat.md) | 25～40 ℃、≥93%RH；48 h |
| 存储恢复 | [008 高温存储](whole-body/SYS-REL-008-high-temperature-storage.md) | 无电池机身 55±2 ℃；16 h |
| 存储恢复 | [009 低温存储](whole-body/SYS-REL-009-low-temperature-storage.md) | −20±2 ℃；16 h |
| 机械应力 | [010 整机与包装振动](whole-body/SYS-REL-010-vibration.md) | 整机 5～55 Hz；包装随机振动 0.604 g |
| 机械应力 | [011 整机冲击](whole-body/SYS-REL-011-shock.md) | 半正弦 150 m/s²、11 ms；18 次 |
| 保护与恢复 | [012 热保护与降额](whole-body/SYS-REL-012-thermal-protection.md) | 风道、过载、温度传感器故障注入 |
| 保护与恢复 | [013 双电池热插拔](whole-body/SYS-REL-013-battery-hot-swap.md) | 按任务谱确定次数；安全姿态换电 |
| 保护与恢复 | [014 电源循环与恢复](whole-body/SYS-REL-014-power-cycling.md) | 开关机、异常断电、欠压、急停恢复 |
| 带载任务 | [016 负载行走](whole-body/SYS-REL-016-loaded-walking.md) | 50 km；300 次转向＋100 次附加姿态动作 |
| 机械应力 | [017 倾倒与受控跌倒](whole-body/SYS-REL-017-tip-over.md) | min(100 mm, 30°对应高度)；最多 4 方向 |
| 动态任务 | [020 动态运动与跑步](whole-body/SYS-REL-020-dynamic-motion.md) | 30 min：热身 5 + 动态 20 + 恢复 5 |
| 操作任务 | [021 抓取搬运](whole-body/SYS-REL-021-manipulation.md) | 作业 60 min；每 10 min 切换工件/位姿 |

## 文件与使用方式

| 文件/目录 | 用途 |
|---|---|
| [common.md](common.md) | 配置冻结、基线、计数、记录与故障闭环 |
| [open-items.md](open-items.md) | 执行前需确定的参数、样本与判据 |
| [_template.md](_template.md) | 新用例的统一结构 |
| [whole-body](whole-body/README.md) | 每项整机用例的目的、条件、步骤、记录和通过/中止要求 |

1. 选择与要验证的问题对应的用例，先确认适用配置与设备条件。
2. 阅读公共规则，解决相关待确认项，固定试前基线与判据。
3. 执行时记录有效时长/里程/次数、暂停、异常与人工干预。
4. 试后复测，改进后在相同工况复测受影响项目，保留异常和处置关系。

## 与软件的对应

用例文档定义方法，[test-runner](../software/test-runner/README.md) 实现自动化部分，[上位机](../software/host/README.md) 负责交互与采集。每次运行关联用例ID和配置，再进入 [Service](../software/service/README.md) 与 [离线分析](../data-analysis/README.md)。

当前18项方法不等于18项自动化脚本。环境暴露、机械应力、人工检查与故障注入按各用例的设备和批准边界执行。

## 新增或修订用例

使用模板，说明变更原因、参数来源、适用配置和复测安排。新ID保持稳定，已有ID不因排序而改号；未确定的数值放入待确认项，不补成假定判据。

参数采用 RP1 参考工况，使用者应按设备与任务目标确定适用范围。
