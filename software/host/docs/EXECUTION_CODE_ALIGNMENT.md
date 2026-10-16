# 上位机与平台执行单号

2026-10-12更新：当前工作区已将提交改为独立摘要审批。以下“上传与恢复”章节保留此前编号适配的历史行为；当前流程以 [测试数据审批](../../docs/TEST-DATA-APPROVAL.md) 为准。正式单号仅在批准发布后返回，使用人类账户会话与CSRF，旧ingestion直接写入和执行包上传均已停用。该更新尚未部署到生产或已安装HMI。

数据库编号规则来源为 `database/sql/017_execution_code_invariant.sql` 和
`database/sql/021_execution_code_collision_fix.sql`。无需修改平台数据库规则。

正式执行单号为：

```text
测试用例编号_北京时间YYMMDDHHMM_样品编号_工位编号_E数据库执行ID
REL-SARM-001_2610101800_RP1.3-SARM-001_MP-01_E42
```

以上为格式示例，不代表目标数据库已存在这些对象。

- 测试用例、样品、工位编号统一为大写；时间显式转换为 `Asia/Shanghai`。
- 样品格式为 `RP数字.数字-部位-三位序号`；部位为
  `SARM/SLEG/SYS/UPPER/LOWER/CHEST/HEAD/BAT`。`left_arm` 是控制部位名，
  `RP1.3-LARM-01` 是旧上位机格式，都不能用作正式编号。
- 工位编号使用平台 `test.station.station_code`；工位 UUID 用于关联，不能用于拼接单号。
- `_E42` 中的数字由数据库分配，上位机不会预造数据库执行 ID。

## 档案与运行

上位机当前仍从 `RP1_RELIABILITY_PROFILES` 指定的 YAML（未指定时使用内置
`factory_hmi/config/reliability_platform.yaml`）读取档案，不自动拉取平台计划。
默认 `profiles: []` 仍表示没有正式档案，界面会显示 `0/0`。

管理员须从实际平台关联填写原有的 campaign/cycle/segment/asset/configuration/
test_case_version/station UUID，并补充三个实际业务编号：

```yaml
test_case_code: REL-SARM-001
asset_code: RP1.3-SARM-001
station_code: MP-01
```

兼容字段为 `test_case_id` 或 `case_code`、`sample_id`、`bench_id`；同时提供新旧字段时
必须一致。缺少业务编号、格式不合法或别名冲突的档案不可用于正式运行。
编号和 UUID 必须指向同一组平台对象；后台回执不一致时会显示同步失败。
档案本身不替代平台权限。当前摘要审批使用提交人的登录会话与CSRF，不使用服务凭据代替人类提交/审核身份。

选择档案后，界面展示该档案的真实样品编号并锁定，开始运行发送实际用例编号和版本。
开始记录时显示不带 `_E数字` 的预编号。CSV、报告、执行包使用附加执行 UUID 的独立
本地记录标识，避免同一分钟多次执行互相覆盖；记录标识与界面的测试单号分别保存。
原始采集 CSV 留在本机，不新增原始数据上传路径。

## 上传与恢复

运行完成先生成本地 CSV、报告和历史记录；完成本地记录不会自动提交。
点击“上传平台”后，上传适配器通过原有 ingestion API 注册执行，数据库通过
`test_case_version_id` 关联到正式测试用例，返回正式 `execution_id`、
`execution_code` 和 `stage_id`。上位机校验编号的用例、北京时间、样品和工位部分，
保存正式编号到本地 SQLite outbox 的 `execution_code` 字段。

历史表和当前测试单号显示后台返回值；导出、重建、重试继续使用稳定的本地记录标识。
重启保留正式编号；中断重试沿用执行 UUID 和相同幂等键；上传完成后再次提交不重复入队。
旧 outbox 自动增加编号列，旧会话 outbox 导入兼容没有该列的数据库，不改历史文件名。

当前 `reliability_v1` 接口完成上传后状态为 `uploaded`，中文为“平台已接收”。
该接口没有人工审批回执，上位机不会把接收完成标为“已批准”。正式查询、统计仍由
现行后台 ingestion 和查询规则决定，本次不新增审批、审核人或数据库审批状态。
旧协议适配器已有的审批状态仍保留。

原来的 `LOCAL_...` 记录保持本地用途，不自动绑定正式用例，也不自动提交。
本次编号对齐不包含历史本地记录补录归档功能。

## 离线验证

验证使用 fake 电机、fake 平台、临时 SQLite 和 Qt offscreen；不连接 PLC/CAN。
覆盖北京时间换日、数据库编号格式、真实档案传参、同分钟独立记录、回执保存和显示、
旧队列升级与导入、上传中断恢复、缺失/错误回执、本地记录禁止提交以及重复提交。
