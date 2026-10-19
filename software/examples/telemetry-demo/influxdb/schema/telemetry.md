# 关节遥测数据合同

Measurement：`joint_telemetry`。每点使用一个原始采集时间，写入 API 指定 `precision=ns`。补传沿用原始时间；服务接收时间保存在业务记录中。

## Tags

| 名称 | 含义 |
|---|---|
| `run_id` | 演示 execution UUID；独立合成示例为 `demo-synthetic` |
| `case_id` | 用例编号，例如 `SYS-REL-001` |
| `subject` | 关节或测点，对应演示 `subject_code` |
| `source_kind` | `synthetic` 合成，`real` 实测；转换时必须明确来源 |

## Fields

全部为浮点数；未采集的字段不写入。

| 名称 | 单位 | 含义 |
|---|---|---|
| `cmd_pos_deg` | ° | 位置指令 |
| `pos_deg` | ° | 实际反馈位置 |
| `tracking_error_deg` | ° | 反馈 − 指令；只在同一时刻两者均存在时计算 |
| `temp_c` | ℃ | 测点温度，传感器位置在运行配置中说明 |
| `spd_deg_s` | °/s | 实际速度；缺失时不对降采样位置求导冒充实测 |
| `torque_nm` | N·m | 实际力矩；不从电流或指令猜测 |

`None`/`null` 表示缺失，`0` 表示真实零值。编码器拒绝 NaN、Infinity、布尔值及未知字段。若一个时间点所有字段均缺失，省略整点并保留本地采集异常。来源或测点单位变化须伴随新运行或明确版本记录。

## 采样与展示

- 本地原始文件保留原始采样周期。演示桥接只导出已保存的采样/降采样点，不恢复丢失高频信号。
- Grafana 按显示窗口取均值，生成空窗并断开曲线，不填零、不保持前值。图例最大值是窗口均值的最大值，峰值检查使用原始记录。
- 默认保留期 `INFLUX_RETENTION_SECONDS=2592000`（30 天）。业务执行、报告和故障闭环不随该 bucket 自动删除。
- 长周期部署应评估测点数与运行数，避免把任意事件文字加为 tag。

示例 line protocol：

```text
joint_telemetry,run_id=demo-synthetic,case_id=SYS-REL-001,subject=demo-joint-01,source_kind=synthetic cmd_pos_deg=0.0,pos_deg=-0.5,tracking_error_deg=-0.5,temp_c=25.0 1800000000000000000
```

此时间戳仅展示格式。实际合成脚本使用运行时的当前时间，面板默认查询最近 15 分钟。
