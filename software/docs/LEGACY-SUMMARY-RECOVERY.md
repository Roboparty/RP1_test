# 移动硬盘历史摘要恢复

本恢复将历史数据保存到新系统 PostgreSQL 的独立待核验区。`UNREVIEWED` 仅表示来源证据尚未核验，没有实现或替代产品测试数据审批。不会新增正式执行、正式指标、故障事件、运行暴露或 MTBF 数据；不导入原始采集 CSV 和完整时序。

## 文件与行为

- `database/sql/028_legacy_recovery_staging.sql`：新增 `integration.legacy_recovery_batch`、`legacy_recovered_execution`、`legacy_joint_summary`、`legacy_recovered_event`。执行编号精确关联现有记录，缺失执行和无法映射的编号分别标记。关节指标使用有单位的标量列，两份来源分别保留。所有记录固定 `UNREVIEWED`、`analysis_eligible=false`，应用角色只有受 RLS 限制的管理员读取权限，没有写权限。源证据禁止更新或删除，修正应追加批次。快照和来源信息拒绝嵌套数组。
- `scripts/recover-legacy-summaries.py`：`prepare()` 核对执行编号、工位、用例和开始时间，保留来源校验和、设备冲突及时间范围标记；`apply()` 在事务中加锁后重新核对上下文，按批次校验和幂等导入。正式表在导入前后逐表计算指纹，不一致则整批回滚。审计通过 `iam.set_request_context()` 和 `audit.capture_row_change()` 关联到操作账号，操作账号不被视为数据审批人。
- `scripts/test-legacy-recovery.py`：只接受 `rp1-legacy-recovery-check-*` 隔离容器，验证备份恢复后的导入、并发重复、冲突、RLS、不可变证据、原始数组拒绝和整批回滚。
- `database/alembic/versions/20261012_0028_legacy_recovery_staging.py`：未来正常升级时应用相同的幂等结构扩展。此次维护可独立应用 SQL，不为此部署尚未上线的 0025–0027，也不修改生产库 `alembic_version`。

## 指标口径

`mean_abs_torque_nm` 是平均绝对力矩；`peak_abs_torque_nm` 是峰值绝对力矩；`tracking_rms_rad` 是弧度单位的跟踪 RMS，换算角度时乘 `180/pi()`。Influx 摘要在本地按测试编号、关节、设备和肢体分组，用首末温度差计算温升、实际与目标位置之差计算 RMS。保存有效样本数、时间端点和聚合 SQL 校验和。`error_sample_count` 保留来源计数，不等于独立故障数。

旧 PostgreSQL 的聚合窗口和采样率未验证；Influx 中的数据也可能经过抽样。因此两份摘要不能直接拼成同一统计窗口，不覆盖旧值、不将不同来源的样本数相加。`proposed_subject_code` 只提供明确的待核验关节映射，不宣称当前目录已经支持该关节。

## 执行与查询

先对新库执行 `pg_dump -Fc` 并验证可恢复，在无网络且无发布端口的临时 PostgreSQL 中恢复备份。旧 PostgreSQL 只读导出执行元数据和已保存的逐关节摘要；原 Influx 服务保持停止，在本地副本计算 `INFLUX_AGGREGATE_SQL`，只输出标量摘要及测试事件。运行数据、备份、清单和验证报告保存在被 Git 忽略、权限受限的 `.qa/legacy-recovery/`，不得提交。

```bash
python3 scripts/recover-legacy-summaries.py prepare --directory <本地清单目录> --container <隔离库>
python3 scripts/test-legacy-recovery.py --directory <本地清单目录> --container rp1-legacy-recovery-check-<日期>
python3 scripts/recover-legacy-summaries.py apply --directory <本地清单目录> --container <新库容器> --sha256 <已验证的数据校验和> --actor admin
```

数据库维护账号可使用以下查询核对来源值。应用角色需设置合法管理员上下文；普通账号、内部 worker 和无上下文连接看不到待核验数据。

```sql
SELECT e.mapping_status, e.existing_execution_id,
       m.source_test_id, m.source_joint, m.source_kind,
       m.temp_rise_c, m.tracking_rms_rad,
       m.tracking_rms_rad * 180 / pi() AS tracking_rms_deg,
       m.mean_abs_torque_nm, m.peak_abs_torque_nm,
       m.bus_voltage_mean_v, m.bus_current_mean_abs_a,
       m.sample_count, m.review_state, m.provenance
FROM integration.legacy_joint_summary m
JOIN integration.legacy_recovered_execution e USING (batch_key, source_test_id)
ORDER BY m.source_test_id, m.source_joint, m.source_kind;
```

现有页面和读模型尚未接入这些待核验表。上线逐关节数据提交、独立审核、驳回重提和正式发布接口前，保持待核验状态；不能通过本脚本把数据置为审批通过。写入失败时事务自动回滚，原正式表保持原值；同一校验和重试不新增记录或审计，内容变更复用批次键会报错。
