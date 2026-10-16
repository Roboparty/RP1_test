# 中央 CSV 归档与详细曲线分析

后续完整部署、已安装 HMI 候选包、真实浏览器及数据库／文件联合恢复的最新结果见[完整隔离部署验收](ARCHIVE-DEPLOYMENT-ACCEPTANCE.md)。本页下方测试收据保留本轮初始实现时的数量。

本轮依据 2026-10-15 用户确认的两轮需求实施。代码位于当前工作区，包含此前未提交修改；尚未部署到生产服务、安装现场新版 HMI、导入真实历史 CSV 或进行 PLC/CAN 实物联调。平台源码 VERSION 为 2.0.6，HMI 本地包版本仍按其独立版本文件管理。本轮没有发布安装包或修改真实运行数据。

## 确定的需求与数据边界

- 原始 CSV 允许集中归档，工位保留副本；仅在用户核实并显式提交后发送。原始文件经哈希校验，应用不提供测得数据编辑入口。
- PostgreSQL 管理身份、索引、核实、归类与审批；服务器持久目录保存 CSV、Parquet 与多级峰谷概览。默认不自动删除原始文件，中央归档纳入备份。
- 执行 UUID 稳定不变。归档编号、正式执行编号和工位纠正前的编号均能解析至同一记录。用例编号用于分类，不能代替单次执行身份。
- 实际测试人、有效工位、人工结论、备注可以核实修正；机器终态、采样时间、运行秒数、循环数、异常数等冻结。实际测试人与登录提交人分别记录，不允许借公开 UUID 接管其他账号的采集身份。
- “其他”必须填写测试名称、实际对象和工位；可先归档看曲线，后补正式用例、样品和计划关联。待归类、待审核、驳回记录不进入正式统计。
- 保留自审：执行员与管理员可审核包括本人提交的记录。执行员/管理员可查看全部待审归档；查看者只能查看已批准且完成正式归类的归档。既有平台读权限默认覆盖全部计划，本轮没有把默认计划阅读权限改成可撤销的成员隔离。
- 历史 CSV 支持明确选择后的批量补录和重复提示。没有上下文的测试人、工位、用例关联需要补填；不从文件名或 mtime 推断绝对采集时间。人工补录日期单独标记，正式摘要时钟质量为 INVALID，原始时间字段仍为空。

## 跨端流程与恢复

正式用例：本地结束、落盘和分析 → 用户核实 → SQLite 队列冻结请求 → 创建中央归档 → 专用接口分块上传 → 异步 SHA256 校验与解析 → 核实版本／必要的实际工位归类修正 → 送审 → APPROVED 或 REJECTED。

“其他”：本地结束与核实 → 中央归档和曲线查询 → UNCLASSIFIED 待归类 → 原提交人或管理员补正式关联 → 原提交人送审 → 现有执行员／管理员审核。管理员可以代补核实、归类；自动续传和正式送审仍绑定原提交账号。

上传与审批是不同状态。网络失败保留本地原件和冻结正文；丢回执重放同一幂等键，续传以服务器已接收字节数为准。工位关机后，已经校验完成的中央归档仍可查询。SHA 错误可重新传输同一原件；解析错误停止自动重试，支持明确重试处理。应用不会为了恢复而修改声明哈希或原始测量事实。

入队清单绑定已核实原件的大小与 SHA。首次上传及重试均再次与该证据比较；即使尚未创建中央记录，入队后被替换的本地文件也会拒绝上传，不能通过重新计算哈希接受新文件。

校验失败时不在数据库回执前清空临时文件，避免文件操作成功、事务未提交的崩溃窗口。派生文件可重建；已校验 raw.csv 不覆盖。文件系统管理员仍可能修改磁盘文件，哈希校验用于发现变化，本实现不宣称硬件 WORM 存储。

驳回后追加核实与提交版本，保留旧备注、旧结论和审核意见；PENDING 与 APPROVED 不允许覆盖。正式发布、审核记录、编号关联和时长发布在同一事务完成，外键、重叠时段或版本错误均回滚。数据库统一采用 UUID 锁、归档锁、提交锁顺序；旧摘要接口也受归档完成、核实、归类和冻结字段检查约束。旧 ingestion 与旧执行包直接上传保持关闭。

在线监控只是一层运行投影。新的心跳携带采集 UUID，断线重连复用记录，批准后的迟到心跳不会重开测试；新采集不会误用上一轮记录。批准将实际时间、实际工位与申报时长关联到该执行；旧编号保留永久别名。批准不自动产生 MTBF 合格暴露，在线时段和正式统计仍有独立资格门禁。

## 页面与分析

数据归档页可查看状态、原始文件校验、冻结事实、自动分析、测试员记录、核实／归类历史，完成正式关联并送审。执行详情提供详细曲线入口。

曲线页使用 ECharts：选择参数、关节、阶段、循环；同参数同单位多曲线叠加；历史执行和固定当前曲线作为参考；目标轨迹、用例瞬时上下限、临时参考线；横纵范围输入、滑块、平移、图例显隐与联动游标。全程视图使用保留峰谷的概览，放大后向后台重新请求细节，显示原始／返回点数、分辨率和缺失段。

中央分析支持运行秒数、循环、完整度、缺口、热峰、跟踪误差、绝对力矩、故障与缺失测量检查。温升判据采用峰值减初始值，结束温升另存；不能用回落后的终值掩盖过程过热。未配置、缺失或无效判据显示未配置，不伪造自动合格结论。

本机 generic-v1 中的 p999 力矩利用率与中央绝对峰值判据不是同一指标。本轮没有强行将其等价映射；中央结论与本机结果不同会在 HMI 核实页回显，必要时由用户重新核实并填写实际差异原因，不自动编造理由。

后补用例只改变归属。用户可显式选择已发布用例追加回顾分析；来源规则、统计、文件哈希、操作者和说明另存不可变版本。现有目录中的中文文字判据不会自动推断为数值：仅映射 `decision_rules.archive_acceptance` 的固定数值字段，必要时读取 `sampling_requirements.record_rate_hz`。缺少机器可计算规则时新分析显示 UNCONFIGURED。回顾分析不覆盖原始分析、人工结论、已批准结果或正式统计。

## 文件级实现

| 位置 | 新增／修改及连接 |
|---|---|
| `AGENTS.md` | 按用户确认更新原始 CSV 中央归档约定，保留审批与硬件边界。 |
| `database/sql/037_execution_archives.sql`、Alembic 0037 | 归档、核实、归类、幂等请求、审批版本快照、RLS、审计、异步处理租约与严格 JSON 边界。 |
| `database/sql/038_archive_publication_gate.sql`、Alembic 0038 | `archive_summary`、冻结字段校验、审批版本绑定、实际测试人与人工结论发布、旧接口门禁、统计资格门禁。 |
| `database/sql/039_archive_execution_identity.sql`、Alembic 0039 | `archive_context`、实际工位修正、稳定采集身份、在线重连、批准挂接与旧单号别名。 |
| `database/sql/040_archive_analysis_versions.sql`、Alembic 0040 | 回顾分析不可变版本与发布规则来源检查。 |
| `service/app/schemas/archives.py`、`repositories/archives.py`、`api/routes/archives.py` | 有界元数据契约、权限和版本检查、归档／查询／核实／归类／送审／重新分析接口。 |
| `service/app/services/archive_storage.py` | `ArchiveStorage`：安全路径、分块幂等、文件锁、fsync、哈希验证和不可覆盖原件。 |
| `service/app/services/archive_worker.py` | `run_once`：独立事务领取、租约恢复、流式文件校验与派生查询文件生成；不占用上传请求进行大文件解析。 |
| `service/app/services/curve_query.py`、`archive_analysis.py` | `CurveProcessor`、`CurveQueryService`、发布规则映射与追加分析；分块读取和视窗点数预算。 |
| `service/app/repositories/submissions.py`、`read_models.py`、HMI presence 路由／schema | 审批核实／分析快照、跨审核人身份显示、旧编号解析、稳定在线身份及冲突回执。 |
| `frontend/src/archiveApi.ts`、`pages/ArchivesPage.tsx` | 归档、核实、归类、重新送审、追溯、追加分析版本；历史人工日期保留和实际工位差异显示。 |
| `frontend/src/pages/CurveAnalysisPage.tsx`、`components/TimeSeriesChart.tsx`、`archiveLayout.css` | ECharts 曲线工作台，按视窗重查、同参数叠加、横纵缩放、参考、异常／缺失段与响应式布局。 |
| `frontend/src/pages/SubmissionsPage.tsx`、`ReadModelPages.tsx`、`App.tsx`、`navigation.ts` | 保留自审；去掉测得字段编辑；驳回重提转到归档核实；增加分析入口。 |
| `host/factory_hmi/core/{service,report}.py` | 全入口必选用例、历史补录、原始事实冻结、核实版本追加、原始哈希验证、中央分析回显。 |
| `host/factory_hmi/desktop/{operator_review,case_filter,app}.py` | 无默认成功、有效工位选择、只读事实与分析、必填备注／差异原因、“其他”名称与批量历史补录。 |
| `host/factory_hmi/gateway/{schemas,app}.py`、`client.py` | 强制核实与严格字段契约，阻止绕过桌面界面提交；新增明确的历史导入入口。 |
| `host/factory_hmi/sync/{outbox,reliability_platform,presence}.py` | 账号绑定、冻结创建与核实请求、专用 CSV 上传、恢复回执、实际工位追加归类、采集 UUID 心跳。 |
| `compose.yaml`、`service/Dockerfile`、`service/scripts/check_archive_worker.py` | 中央持久存储、专用归档 Worker、UID 10001 目录权限和健康检查。 |
| `scripts/{archive_backup,backup,restore,verify}.py` | 数据库与中央文件一致维护备份，恢复到全新专用卷并保留原件只读权限；防覆盖已有卷。 |
| `scripts/test-archives.py`、`test-archive-storage.py`、`deployment/ArchiveQa.Dockerfile` 及专用 `.dockerignore`、CI | 完全隔离的迁移、权限、HTTP、恢复与跨端回归，不读取站点 `.env`；验证镜像只接收依赖清单。 |

## 部署配置与验收入口

必须配套应用完整迁移链至 `20261015_0040`、匹配版本 API／归档 Worker／前端／HMI。先在隔离恢复副本演练，不应先开放旧采集写接口，也不提供删除审批或归档历史的 downgrade。

Compose 默认将项目专用 `archive-data` 卷挂到 `/var/lib/rp1/archives`。可用 `RP1_ARCHIVE_VOLUME` 指向事先配置的绝对绑定目录／NAS；外部目录须允许容器 UID 10001 读写。API 与归档 Worker 使用同一目录。存储目录不通过 Nginx 静态暴露。

可调设置：`ARCHIVE_MAX_FILE_BYTES`（默认 50 GiB）、`ARCHIVE_MAX_CHUNK_BYTES`（默认 4 MiB）、`ARCHIVE_QUERY_MEMORY_MB`（默认 512 MiB）、`ARCHIVE_WORKER_POLL_SECONDS`（默认 2 秒）。大小上限是配置边界，不代表已经完成该尺寸／工位数量的容量验收。

```bash
# 构建隔离软件验证工具；不会启动设备或生产数据库。
docker build -f deployment/ArchiveQa.Dockerfile -t rp1-archive-qa:local .
python3 scripts/test-archives.py
python3 scripts/test-archive-storage.py
cd frontend
npm run check
```

`test-archives.py` 使用随机命名 PostgreSQL 容器和文件卷，无宿主数据库端口、无生产卷、无站点 `.env`；执行全迁移、后端回归及真实 HMI TCP／Cookie／CSRF 链路后清理。真实 HTTP 回归的 CSV、账号、对象和审核理由均为合成数据。`test-archive-storage.py` 在新的随机卷中验证备份恢复及拒绝覆盖。

本轮真实浏览器验收使用合成 API：1280×720 与 390×700，12,001 个采样点，验证范围重查、尖峰、缺失段、跨执行叠加、图例、参考线、核实归类、错误重试和窄屏无横向溢出；该验收不代替真实后台集成或现场测试。

## 本轮验证收据

- 完整迁移至 0040；后端单元 83 通过，隔离集成 27 通过，包含 7 个真实 TCP HMI 场景，无集成跳过。非集成命令排除的 33 项不计入通过数量。
- HMI 完整回归 477 通过，另有 119 个子测试通过；包含入队后同大小／改大小篡改文件时零中央请求的回归。
- 前端 69 项测试、lint、类型检查及生产构建通过；生产 API 与隔离验证镜像构建通过，API 非 root 身份、归档路由和目录写权限检查通过。
- 独立浏览器验证详细曲线、参考、缩放、核实归类、请求失败恢复与窄屏；追加分析实际 POST、版本展示和查看者只读已复核。
- 随机独立卷的备份／恢复、文件所有权及只读权限、拒绝覆盖已有卷通过；Compose 配置、差异空白及现有发布文件检查通过。

实现核对参考：[PostgreSQL 事务内触发器](https://www.postgresql.org/docs/16/trigger-definition.html)、[DuckDB Parquet 查询](https://duckdb.org/docs/stable/data/parquet/overview)、[ECharts 坐标轴](https://echarts.apache.org/handbook/en/concepts/axis/)、[Docker 构建上下文与专用忽略文件](https://docs.docker.com/build/concepts/context/)。这些文档用于核对实现机制；本项目行为以实际代码与上述执行证据为准。

生产部署、真实历史数据批量导入、大文件／多工位容量压测及 PLC/CAN 实物联调仍需在相应环境验收。
