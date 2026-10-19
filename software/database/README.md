# 当前平台数据库

PostgreSQL 16 / Alembic，配套 `reliability_v1`，完整迁移链至 `20261016_0041`。从 [软件入口](../README.md) 启动，由 `migrate` 执行 `alembic upgrade head`。

## 存储与安全

- `alembic/versions/` 和 `sql/` 是空库初始化及升级输入；历史迁移不能按旧文档清理。
- `imports/test_cases_*.csv` 是静态测试目录，不是采集数据。
- `iam/catalog/test/reliability/health/integration/audit` 分别保存身份权限、目录、执行、统计、健康、接入和审计。
- 数据库保存身份、索引、核实/归类/提交/审批版本；原始 CSV 和派生文件保存在工位与仓库之外的持久目录，见 [数据流程](../docs/data-workflow.md)。

迁移/运维、应用和只读角色分开。应用角色无超级用户和 BYPASSRLS 权限，浏览器和设备不持有数据库账号。请求使用已验证会话设置 `iam.set_request_context(user_public_id, request_id, change_reason)`，不相信客户端指定的操作者。

角色、Campaign 资源权限和 RLS 共同生效；默认阅读策略见 `sql/026_default_campaign_view.sql`，不能据角色名推断任意资源可写。正式写入通过 API 和受控数据库函数，发布、审批、审计和重算任务原子提交，错误全部回滚。测量事实不通过应用接口改写。

## 时长与 MTBF

实现键 `mtbf.poisson.exposure_estimate.v1`。纯计算位于 `service/app/domain/mtbf.py`，聚合/调度位于 `service/app/repositories/mtbf.py` 和 `service/app/services/worker.py`。

累计测试时长、运行时长和合格暴露分开。有效暴露由 `exposure_assessment`、scope 归属筛选，同一样品/范围区间求并集；待确认或排除时间不进入 T。历史 `LEGACY_REPORTED` 不因导入或审批自动获得暴露资格，受控确认遵循 `sql/027_legacy_exposure_rule.sql` 和 `sql/027_confirm_legacy_reported_exposure.sql`。

相关故障依据确认分类、scope 和 `failure_group_key` 去重，不使用报警条数或严重度权重代替独立故障数。

```text
T = 合格、去重暴露秒数 / 3600
r > 0：MTBF 点估计 = T / r
r = 0：点估计为空，不显示无穷大
单侧置信下限 = 2T / chi_square_ppf(C, 2(r+1))
r = 0 时等价为 T / [-ln(1-C)]
```

默认展示 70% 与 90% 下限。`T=0,r=0` 为 `NO_EXPOSURE`；`T=0,r>0` 拒绝计算。目标时长只用于进度，不截断 T 或自动形成通过/拒收结论。

Worker 用 `FOR UPDATE SKIP LOCKED` 领取重算任务，保留输入/输出快照与当前结果。配置、明细、重算接口以 `service/app/api/routes/mtbf.py` 和平台 `/openapi.json` 为准；预览带 `formal=false`。

## 升级与检查

独立 [遥测演示](../examples/telemetry-demo/README.md) 在 0023 后使用另一迁移链。新平台使用新项目、新卷，已有平台先备份并在隔离恢复副本验证，见 [运维](../docs/operations.md)。

```bash
cd software/database
python3 -m unittest discover -s tests
```

集成检查见 [开发指南](../../docs/development.md)，不得连接正式数据库。备份、运行数据和凭据不进入 Git。
