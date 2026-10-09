# 单次运行分析

读取 `rp1-test` 的本地运行文件，输出关节跟随误差、全程温度、有效时长、缺失计数和事件摘要。使用 Python 3.10+ 标准库。

在仓库根目录执行：

```bash
python data-analysis/scripts/analyze_run.py data-analysis/examples/synthetic-run \
  --output outputs/analysis-demo --max-gap-s 1.5
python -m unittest discover -s data-analysis/tests
```

生成 `summary.json` 和 `report.md`。合成输入、预期结果和手算核对见[样例说明](examples/README.md)。

分析实际运行时，将输入路径换为 [test-runner](../software/test-runner/README.md) 生成的运行目录。Factory HMI 执行包先转换为下表的 CSV 与元数据格式。

## 输入与统计

| 文件/字段 | 用途 |
|---|---|
| `telemetry.csv` | 必需：`time_s`、`motor_index`、`cmd_pos_rad`、`pos_rad`；可选 `joint_name`、`temp_c`。位置为 rad，时间为相对采集时间 s |
| `metadata.json` | 必需。提供运行编号、`source_kind`、起止时间或 `elapsed_seconds`；`confirmed_active_intervals` 使用同一相对时间轴 |
| `events.jsonl` | 可选。每行一个事件对象，读取 `type`/`event_type`、`elapsed_s` 和绝对时间；缺少文件时注明 |
| `ambient_temperature_c` | 元数据中的环境温度；缺失时仅输出温度，不计算温升 |
| `--max-gap-s` | 明确指定相邻同关节样本的最大允许间隔；未指定时不评价断采 |

- 有效时长来自已确认区间之和，区间按 `[start_s,end_s)` 统计。不从 CSV 首尾或样本数推断任务持续运行；没有区间时有效时长和 active 误差为空。
- 误差为同一行 `pos_rad - cmd_pos_rad`，仅统计 active 区间的有效配对；RMS 按有效样本等权计算，不做角度环绕或时间加权。
- 温度保留全程，包括暂停和结束采样点。缺失、NaN、Inf 保持未知，不补零。
- 同关节重复/倒序行计数并从指标中排除；不自动重排原始记录。采样间隔与缺失字段分别报告，未检测到时间间隔超限不等于数据完整。
- 逐行读取 CSV，内存主要取决于关节数；事件预览最多 1000 条，事件计数覆盖全部记录。输出保存工具版本、选项与输入文件指纹。

输出为单次运行的描述性统计。测试结论按[公共检查和批准判据](../test-cases/common.md)形成，基线对比使用一致的配置与工况。
