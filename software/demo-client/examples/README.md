# 演示运行

运行命令见[演示客户端 README](../README.md)，输出写到 `--output-dir` 指定的目录。

12秒运行、4–6秒暂停的预期结果：

- `telemetry.csv`：20 Hz、两个关节，共482行数据；本地角度rad。
- `events.jsonl`：启动、执行区间、暂停、恢复、结束；`occurred_at`与`time_s`对齐。
- `metadata.json`：`source_kind=synthetic`、`active_seconds=10`、两个确认区间、`test_outcome=INCONCLUSIVE`。
- `config.snapshot.yaml`：public ID和采集配置，不含API key。
- `manifest.json`：每个文件的字节数和SHA-256。

补传后平台仍为INCONCLUSIVE。服务API的完成状态只说明记录接入，不能作为整机可靠性通过结果。
