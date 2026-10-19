# 遥测演示数据库

此数据库仅供独立遥测演示，与当前 `reliability_v1` 使用独立凭据和数据卷，不包含当前平台的 CSV 归档、核实及审批流程。

保存演示用例、配置、执行、事件、统计及降采样遥测。使用 SQL 与 Alembic 管理结构，提供用例目录导入和合成数据初始化；InfluxDB 承载遥测的监控投影。

## 代码阅读入口

| 路径 | 内容 |
|---|---|
| [sql/001_foundation.sql](sql/001_foundation.sql) / [sql/002_domain.sql](sql/002_domain.sql) | 扩展、UUID和业务表 |
| [sql/003_security_and_logic.sql](sql/003_security_and_logic.sql) | 请求身份、RLS、触发器与权限 |
| [sql/018_execution_joint_telemetry.sql](sql/018_execution_joint_telemetry.sql) | 指标目录、序列与观测 |
| [sql/020_edge_collector_ingestion.sql](sql/020_edge_collector_ingestion.sql) | 服务凭据、幂等批次及证据关联 |
| [sql/024_influx_export_queue.sql](sql/024_influx_export_queue.sql) | 合成来源标记、持久导出队列与RLS |
| [sql/025_whole_machine_demo_subjects.sql](sql/025_whole_machine_demo_subjects.sql) | 整机右臂示例测点，保留模块与整机边界 |
| [sql/026_influx_source_select.sql](sql/026_influx_source_select.sql) | 采集端仅可读取自身队列，支持冲突安全的幂等插入 |
| [alembic/versions](alembic/versions) | 有序升级，当前head为 `20261006_0026` |
| [imports](imports) | 用例目录及导入/版本修订代码 |
| [imports/sql_migrations.py](imports/sql_migrations.py) | 角色参数验证与SQL模板引用，避免部署角色名写死 |
| [seeds/demo_context.sql](seeds/demo_context.sql) | 显式合成上下文；不创建测试执行 |
| [scripts/verify_schema.py](scripts/verify_schema.py) | 只读检查版本、表与队列权限 |
| [tests](tests) | 用例目录与修订校验 |

## 初始化

建议使用[演示总览](../README.md)的隔离Compose环境。自行部署时，先由数据库管理员建立两个非超级用户角色，再使用迁移管理员账号升级：

```bash
pip install -r requirements.txt
# DATABASE_URL=postgresql+psycopg://<migration-user>:<password>@<host>/<new-db>
alembic upgrade head
python scripts/verify_schema.py
python -m unittest discover -s tests
```

`RP1_APP_USER` / `RP1_APP_PASSWORD` 和 `RP1_READONLY_USER` / `RP1_READONLY_PASSWORD` 由部署注入；[初始化脚本](docker-entrypoint-initdb.d/00-create-roles.sh)用于PostgreSQL镜像首次建立空数据卷。基础迁移不自动创建演示Campaign或测试结果。

## 事实与投影

```text
用例版本 + 样品 + 配置
    → Campaign → Cycle → Segment → Execution → Stage
    → 事件 / 时间区间 / 结果 / 外部证据
    → 指标序列与观测
    → 持久导出队列 → InfluxDB监控投影
```

API在一个事务内保存幂等收据、遥测事实和导出队列。应用角色只获得队列SELECT/INSERT/UPDATE及序列权限，不获得DELETE；INSERT受采集源与执行关联限制，采集服务身份仅可读取本源队列，内部Worker可读取并更新。该SELECT策略满足PostgreSQL冲突插入的可见性要求。只读角色不读取导出队列。

迁移管理员用于初始化和升级；日常Service使用应用角色。演示客户端通过API写入，不能直连PostgreSQL。

## 用例版本与演示数据

用例目录包含模块与整机条目、整机 V1.0 结构化方案和版本修订。完整执行方法见 [测试用例](../../../../test-cases/README.md)；目录版本与文档版本分别管理，导入前选择对应版本。

合成初始化创建 `DEMO-SYNTHETIC` 上下文及 `RP0.0-SYS-001` 虚拟样品；演示执行经 API 创建并带 `synthetic` 标签。

## 备份与恢复

使用PostgreSQL客户端工具，密码从环境注入，备份留在仓库之外：

```bash
# DATABASE_URL使用libpq格式 postgresql://（不含+psycopg）
sh scripts/backup.sh /secure/path/demo.dump
# 将DATABASE_URL显式改为另一个空库后恢复。
sh scripts/restore.sh /secure/path/demo.dump
```

脚本不执行DROP或 `--clean`。多数业务迁移禁止直接降级删除证据；升级前保存独立快照，恢复后核对版本与记录。修改SQL时追加迁移，保留已发布编号及顺序。
