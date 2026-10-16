# 当前平台数据库

PostgreSQL 16 / Alembic，配套大屏与PLC HMI，迁移链至 `20261016_0041`。包含在线状态、数据审批、中央归档、执行身份/别名、追加分析和历史CSV导入。

从 [软件入口](../README.md) 配置启动，`migrate` 执行 `alembic upgrade head`。应用、只读和迁移角色分开；权限、RLS、审计与统计规则位于 `sql/`。

`imports/test_cases_*.csv` 是静态用例目录；采集CSV位于工位和平台文件卷。数据库保存执行、文件索引、核实、归类与审批版本。

旧演示在0023之后使用另一迁移链，保留于 [独立遥测示例](../examples/telemetry-demo/README.md)。首次安装新版使用新项目、新卷；已有同架构平台按备份/迁移流程更新。

```bash
cd software/database
python3 -m unittest discover -s tests
```
