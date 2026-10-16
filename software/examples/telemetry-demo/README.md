# 独立遥测演示

本目录保留原有 SERVICE key / ingestion → PostgreSQL → InfluxDB → Grafana 合成演示。
其 API、数据库迁移、凭据、数据卷和端口独立于当前 `reliability_v1` 平台。

```bash
cd software/examples/telemetry-demo
sh scripts/start.sh
sh scripts/run-demo.sh
python3 scripts/verify_stack.py --demo
sh scripts/stop.sh
```

默认项目名 `rp1-test-telemetry-demo`；API/Grafana/Influx 分别绑定回环地址的
18080/13000/18086，PostgreSQL 为 15432。配置在首次启动时生成，不复制平台 `.env`。

共享工具位于 `../../demo-client/`、`../../test-runner/`、`../../influxdb/` 和
`../../grafana/`。新版测试大屏、PLC HMI和中央CSV归档从 [软件主入口](../../README.md) 启动。
