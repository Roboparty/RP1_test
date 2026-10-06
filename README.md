# RP1 Test

面向人形机器人模块老化与整机可靠性测试的方法和软件工具。以 RP1 为参考平台，将测试用例、设备控制、数据采集和试后分析连接起来，帮助发现热、磨损、连接和控制问题，并通过相同工况的复测评估改进。

包含 **18 项整机测试用例**、RP1 Factory HMI 桌面上位机、轨迹工具、FastAPI 后端、PostgreSQL、InfluxDB 和 Grafana。离线分析目录提供指标与数据接口设计，分析程序处于规划阶段。

## 内容与入口

| 目录 | 内容 | 使用入口 |
|---|---|---|
| [test-cases](test-cases/README.md) | 老化、环境、机械应力、保护与任务循环 | 用例索引、公共规则与参数清单 |
| [software](software/README.md) | 上位机、API、测试执行与数据服务 | 安装、部署与合成演示 |
| [data-analysis](data-analysis/README.md) | 温升、误差、有效时间、事件及基线对比 | 分析规则与接口设计 |

```text
rp1_test/
├── README.md
├── test-cases/
│   ├── common.md
│   ├── open-items.md
│   ├── _template.md
│   └── whole-body/
├── software/
│   ├── host/
│   ├── service/
│   ├── test-runner/
│   ├── database/
│   ├── influxdb/
│   ├── grafana/
│   └── demo-client/
└── data-analysis/
    ├── docs/
    ├── scripts/
    ├── examples/
    └── config/
```

各模块在本仓库统一维护，模块 README 提供功能、配置和代码入口。

## 如何开始

1. 从 [用例索引](test-cases/README.md) 选择需要暴露的问题，阅读 [公共规则](test-cases/common.md) 和相关 [待确认项](test-cases/open-items.md)。
2. 固定机器人配置、负载、环境、任务目标和判据，建立试前基线。
3. 按 [软件架构](software/README.md) 确定采集、执行、上传、存储和展示的责任。
4. 按 [分析方法](data-analysis/docs/README.md) 保存运行阶段和异常记录，比较相同工况下的基线、试后与改进后结果。

软件安装和运行见 [快速开始](software/README.md)。示例使用合成数据；用例中的 RP1 参数需要结合设备和任务目标确认，实测结果不随方法文档提供。

## 测试与软件怎样对应

- `test-cases` 定义测试目的、应力、步骤和判据。
- `software/host` 是 RP1 Factory HMI，提供桌面、CAN控制、采集和报告；`software/test-runner` 提供独立轨迹与执行工具。
- Service 关联用例、运行、配置和事件；PostgreSQL 保存业务记录，Worker 将遥测导出至 InfluxDB，Grafana 展示曲线。
- `data-analysis` 对导出记录做离线分析，保留暂停、故障与人工干预的语义。

桌面操作入口为 RP1 Factory HMI，监控入口为 Grafana；`demo-client` 提供无需机器人的采集演示。

## 接入自己的机器人

用实际设备适配替换通信与反馈接口，确认关节顺序、单位、零位和保护参数。按本机的负载、温度边界及使用任务谱确定测试应力，并保持基线和复测条件一致。

用例中的数值是 RP1 参考工况，不是通用验收标准。整机方法仍需实测，并在执行前确定性能判据与安全边界。
