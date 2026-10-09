# InfluxDB 时序数据

提供 InfluxDB OSS **2.9.0** 的初始化、合成数据、批量写入与查询工具。统一部署使用 [software/compose.yaml](../compose.yaml)，查询语言固定为 Flux。执行状态和故障记录仍由 [Service](../service/README.md) 与 [PostgreSQL](../database/README.md) 管理。

## 已提供的代码

```text
influxdb/
├── scripts/
│   ├── client.py       # v2 HTTP API、字段编码、纳秒时间转换
│   ├── init.py         # 首次初始化；已有实例只校验
│   └── telemetry.py    # health / generate / write / query
├── schema/telemetry.md # 数据合同、单位、缺失值与保留策略
├── examples/query.flux
├── tests/test_contract.py
└── .env.example       # 占位参数
```

Python 工具仅依赖标准库，通过 `/api/v2/setup` 初始化。固定 v2 镜像与 Flux 配套，升级时应同时检查 API 和查询语言的兼容性，见 [InfluxDB v2 文档](https://docs.influxdata.com/influxdb/v2/)。

## 最小流程

在 `software/` 下按 [总览](../README.md) 初始化本地 `.env` 并启动部署。以下命令从同一目录执行：

```bash
INFLUX_URL=http://127.0.0.1:18086 python3 influxdb/scripts/telemetry.py --env-file .env health
python3 influxdb/scripts/telemetry.py generate --output output/demo.lp
INFLUX_URL=http://127.0.0.1:18086 python3 influxdb/scripts/telemetry.py --env-file .env write output/demo.lp
INFLUX_URL=http://127.0.0.1:18086 python3 influxdb/scripts/telemetry.py --env-file .env query --run-id demo-synthetic --output output/query.csv
```

预期生成 **575 点**：最近两分钟、5 Hz、保留 5 秒断档，全部标记 `source_kind=synthetic`。`torque_nm` 完全缺失，其余五个字段各有 575 个值；Flux 查询返回按字段展开的结果。打开 [Grafana 面板](../grafana/README.md)，选择 `demo-synthetic` 与 `demo-joint-01`。重新生成会产生新时间戳；写入同一个文件才是同一批次重放。

初始化服务自动运行。需要手动检查时：

```bash
INFLUX_URL=http://127.0.0.1:18086 python3 influxdb/scripts/init.py --env-file .env
python3 -m unittest discover -s influxdb/tests -v
```

空实例创建组织、bucket、管理员和 token。已有实例验证 token 与 bucket，不重置密码、不修改保留策略。更新 `.env` 不会迁移已有数据卷；不匹配时脚本报错，需按原有身份处理。

## 采集接入

结构见 [遥测合同](schema/telemetry.md)。使用 `client.line_protocol()` 编码与 `InfluxClient.write()` 批量上传，遵循 [官方写入 API](https://docs.influxdata.com/influxdb/v2/write-data/developer-tools/api/) 的纳秒精度。平台桥接导出使用原始 `observed_at`；导出曲线已降采样，不能当作 200 Hz 原始记录。本地 CSV/JSONL 继续保留用于峰值和故障分析。

默认组织 `rp1_test`、bucket `telemetry`、保留 30 天，服务于本地演示。长期试验应按采样量和留存目标确定容量与备份。重复写入相同 measurement、tag 集和 timestamp 会合并字段或覆盖同名字段；后续空值不会删除旧值。读写异常时脚本返回非零状态。

时序曲线展示遥测字段，执行状态与故障记录由 Service / PostgreSQL 管理，保护动作由设备控制侧执行。
