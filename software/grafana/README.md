# Grafana 测试遥测面板

提供可导入的 JSON 与 provisioning，配套 **Grafana OSS 13.2.3 + InfluxDB OSS 2.9.0 / Flux**。固定镜像 `grafana/grafana:13.2.3` 来自 [官方 Docker 下载页](https://grafana.com/grafana/download/13.2.3?edition=oss&platform=docker)。

```text
grafana/
├── dashboards/joint-telemetry.json
├── provisioning/
│   ├── datasources/influxdb.yaml
│   └── dashboards/telemetry.yaml
├── scripts/validate_dashboard.py
└── .env.example
```

## 已提供的面板

| 面板 | 内容 |
|---|---|
| 数据来源 | 当前筛选中的 `synthetic`/`real` |
| 关节温度 | 已采集测点温度 |
| 指令与反馈 | 同一测点的目标与实际位置 |
| 跟随误差 | 反馈 − 指令，单位 ° |
| 关节速度 | 实际速度，单位 °/s |
| 关节力矩 | 已采集力矩；缺失时保持空白 |

筛选项为数据源、运行 `run_id`、关节/测点 `subject` 与时间范围。[变量查询](https://grafana.com/docs/grafana/latest/datasources/influxdb/template-variables/)使用 Flux `schema.tagValues()`；bucket 从数据源 `defaultBucket` 读取。曲线按窗口取均值，缺失区间断开；图例最大值不能代替原始峰值。没有数据时显示“无数据”。

## 启动与使用

从 `software/` 按 [总览](../README.md) 部署。Compose 挂载：

```text
./grafana/provisioning → /etc/grafana/provisioning（只读）
./grafana/dashboards   → /var/lib/grafana/dashboards（只读）
```

1. 按 [InfluxDB 示例](../influxdb/README.md) 生成并写入合成遥测。
2. 打开 `http://127.0.0.1:13000`，使用本地 `.env` 的管理员账户登录。
3. 打开 `RP1 Test / 人形机器人测试 · 关节遥测`，选择 `demo-synthetic`、`demo-joint-01`，时间范围“最近 15 分钟”。
4. 核对来源为 `synthetic`、温度从约 25 ℃ 到 29.8 ℃、位置曲线有 5 秒断档、力矩为空。缩短时间窗口可观察更多采样点。

配置使用 [官方数据源配置](https://grafana.com/docs/grafana/latest/datasources/influxdb/configure/)与[仪表盘加载机制](https://grafana.com/docs/grafana/latest/administration/provisioning/)。数据源 UID 为 `rp1-influxdb`；URL、org、bucket 与 token 从容器环境注入，token 位于 `secureJsonData`，不写入面板 JSON。管理员登录开启，匿名访问关闭。部署关闭 InfluxDB 插件启动时自动更新。

## 手动导入与检查

已有 Grafana 可添加 Flux 数据源，再导入 `dashboards/joint-telemetry.json`，在“数据源”变量中选择它；组织和 bucket 由该数据源配置提供。Provisioning 创建的面板不能直接保存 UI 修改，应导出 JSON 后更新仓库文件。

```bash
python3 grafana/scripts/validate_dashboard.py
```

开发检查覆盖 JSON 结构、变量、字段与单位、Flux bucket 引用、空窗处理、来源和敏感字段。

结合 [测试用例](../../test-cases/README.md)、运行事件及本地原始数据分析异常。
