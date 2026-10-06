# Factory HMI 执行包接入可靠性服务

这里提供一个独立上传工具，将 **RP1 Factory HMI 已导出的执行包**接入本仓库的 `service`。
上位机负责 CAN 控制、轨迹预检、录制和本地报告。

## 接口与边界

```text
RP1 Factory HMI → 本机 Gateway → CAN / 电机
                       ↓
               CSV、报告、tar.gz
                       ↓  独立 bundle bridge + SQLite outbox
               /api/v1/ingestion
                       ↓
             PostgreSQL → 导出队列 → InfluxDB 2 → Grafana
```

- 桌面登录、分块上传和人工审批使用 `/api/auth/station-login`、`/api/submissions`，
  对应工厂提交平台协议。
- 执行包工具使用 SERVICE key 和数据库上下文，通过 `/api/v1/ingestion` 接入可靠性服务。
- 顺序为注册执行 → 遥测 → 判定事件 → 附件元信息 → 完成；每项使用稳定幂等键。
  中断后从 SQLite 队列重试，请求可重放，不修改原执行包。
- **当前服务没有二进制文件上传接口**。tar.gz 仍留在上位机，附件登记为 `MISSING`，
  不宣称远端已有原始 CSV。队列显示 `INGESTED` 表示摄取成功，不代表人工审批。
- 这是完成后的执行包接入。`log_motor_telemetry.py` 的实时直写使用 InfluxDB 3，
  与本仓库的 InfluxDB 2 写入协议不同。

## 最短演示

在 `software` 目录启动本地服务，并生成私有演示上下文：

```bash
sh scripts/start.sh
docker compose --profile demo run --rm bootstrap-demo
```

安装上位机的源码依赖后，在同一目录运行：

```bash
python host/tools/upload_reliability_bundle.py generate-demo \
  --output .runtime/factory-bundle-demo
```

它使用现有 `ReportManager` 生成七关节右臂的合成 CSV、报告和执行包。
输出包含执行包路径及 `subject-map.yaml`；这一步没有驱动 CAN，也没有机器人实测。
将输出中的包路径填入下面命令：

```bash
python host/tools/upload_reliability_bundle.py upload \
  --bundle .runtime/factory-bundle-demo/reports/exports/<执行包>.tar.gz \
  --context .runtime/context.json \
  --subject-map .runtime/factory-bundle-demo/subject-map.yaml \
  --source-kind synthetic \
  --outbox .runtime/factory-reliability-bridge.sqlite3 \
  --base-url http://127.0.0.1:18080
```

映射使用 `database/sql/025_whole_machine_demo_subjects.sql` 中七个 `SYS-*` 右臂对象，
仅演示整机坐标中的右臂遥测。它不是模块寿命测试，也不代表整机用例已实测。
Grafana 的 `run_id` 使用返回的服务 `execution_id`，不是本地执行包 UUID。

网络恢复后处理队列：

```bash
python host/tools/upload_reliability_bundle.py flush \
  --context .runtime/context.json \
  --outbox .runtime/factory-reliability-bridge.sqlite3 \
  --base-url http://127.0.0.1:18080
```

失败项保留在 SQLite，按原 outbox 的退避时间重试。每个 bridge outbox 使用一个上传进程；
不要与桌面的 `sync/outbox.sqlite3` 共用。已摄取的执行包重复提交会直接返回当前状态，
同一执行 UUID 的包内容、上下文或映射变化会被拒绝。

## 接入已有实测包

七项 UUID 必须来自同一套真实且相互一致的数据库记录：
`campaign_id`、`cycle_id`、`segment_id`、`asset_id`、`configuration_id`、
`test_case_version_id`、`station_id`。`subject-map` 的键为 CSV 中的 HMI 关节名，
值为该用例对应的 `catalog.telemetry_subject.subject_code`。模块测试与整机测试应使用各自对象，
不能借用演示上下文为真实测试背书。

默认 `source-kind` 为 `synthetic`；该模式强制提交 `blocked / INCONCLUSIVE`，即使本机报告为
`passed`。本机判定作为事件中的 `local_report_status` 保存。实测报告只有验收规则完整且
已评估时才能提交 `PASSED`；阈值未配置或存在未评估规则时，结论为 `INCONCLUSIVE`。

实测接入须显式传 `--source-kind real --evidence <文件>`，证据文件需要下列字段：

```yaml
backend: motors_py
measurement_origin: physical_robot
recording_reference: <实际运行记录或原始数据引用>
confirmed_by: <确认该记录来自实机的人员>
```

控制服务会将实际后端写入报告配置；`fake` 记录明确带有 `synthetic` 标识。
证据由实际测试人员确认；工具只做格式和一致性检查，不能自动证明实机真实性。
演示上下文、`fake` 标记或合成包不能改标为 `real`。这里只持久化证据摘要，
不发送任意证据内容或人员字段；真实测量阈值和报告结论须由使用者审核。

## 数据口径

原始 CSV 留在执行包中。上传前校验包内路径、文件类型、大小和 SHA-256，
不向任意目录解压。CSV 流式读取，按时间顺序均匀取样至最多 5,000 行，
每请求最多 20 条指标序列。当前支持：

| 指标 | 来源与单位 |
| --- | --- |
| 目标/反馈位置 | 已录 `*_cmd_pos_rad`、`*_pos_rad`，rad → deg |
| 跟踪误差 | 同一帧的反馈减目标，rad → deg；两者齐全才计算 |
| 速度 | 已录 `*_spd_rad_s`，rad/s → deg/s |
| 力矩、温度、电流 | 已录字段，N·m、°C、A；无字段则不提交 |

NaN、Inf、空值及不合法时间不会补成零；存在缺帧的序列标为 `PARTIAL`。
电流可入 PostgreSQL，但当前 Influx 投影没有电流字段。
有效时长沿用报告中 `active_seconds`，并检查它不超过起止时间；不从 CSV 行数推算寿命。
均匀抽样适合趋势展示，不能替代原始高频数据中的峰值、故障或瞬态分析。

## 凭据

SERVICE key 只从权限为 `0600` 的私有 `context.json` 的 `api_key`，或私有 env 文件中的
`RP1_RELIABILITY_SERVICE_KEY` 读取。env 文件通过 `--key-env-file` 指定，不执行 shell。
凭据不会进入 SQLite manifest、事件、附件元信息或命令输出。
