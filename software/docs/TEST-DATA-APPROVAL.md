# 测试数据摘要审批

2026-10-15 后续改造已将新提交升级为中央 CSV 归档与强制核实。当前实现以 [中央归档与曲线分析](CSV-ARCHIVE-IMPLEMENTATION.md) 为准；下文保留原摘要审批实施记录，其中旧“禁止原始 CSV 上传”边界已由用户明确变更。

实施日期：2026-10-12。位置：电脑3桌面项目工作区。代码与隔离验证完成不代表已经生产部署、安装新版HMI或通过实物联调。

## 状态与权限

本地完成 → 显式提交摘要 → PENDING → 独立审核 → APPROVED或REJECTED。REJECTED可由原提交人修正后以新版本重提；APPROVED不可覆盖、撤回或改写。账号权限申请审批、在线presence、测试数据审核是三个独立流程。

- 提交：已登录HUMAN执行员或系统管理员。平台重新核对已配置的计划、周期、样品、配置、用例版本、分析段及启用台架；不另要求计划或样品EDIT授权。提交人从会话获取，不能在请求中指定他人身份。
- 审核：已登录HUMAN执行员或系统管理员可以审核任意待审摘要，包括本人提交的记录。提交和审批均要求CSRF。查看者不能审核。
- 可见：执行员和管理员查看全部提交。VIEWER不能读取待审区，只能按现有计划权限查看正式记录。提交人账号停用或执行上下文停用后，批准失败；仍可驳回。
- 原始CSV、报告和执行包留在工位。允许发送的内容仅为18个固定字段的有界摘要：八个身份/上下文UUID、开始与结束时间、申报运行秒数、循环数、测试状态、数据与时钟质量、异常计数、最多500字摘要、本地证据SHA256。没有附件、二进制、任意JSON、遥测点或曲线字段。

## 数据何时进入正式查询

`integration.data_submission`保存当前状态；`data_submission_revision`保留每次不可改写的提交摘要；`data_submission_review`保存每个版本的审核人、结论、理由和时间。它们不属于既有正式查询和Worker的数据源。

批准时，数据库函数在一次事务中验证当前版本、审核权限、提交人权限与真实上下文，写入正式执行、阶段、结果、申报运行时段及审批记录，并更新状态。任何外键、时段重叠或其他发布错误都会回滚全部写入、审批状态、审计和重算队列。正式执行通过唯一`approved_submission_id`回溯提交；数据库生成正式单号，HMI在批准回执后才将其确认。

批准摘要进入正式执行查询和申报时长台账，完成循环保存在`execution_result.completed_cycles`。运行时段标记为`MANUAL / approved-summary-unqualified`，不自动创建暴露资格评估、scope归属或故障归类。累计运行秒数不能证明连续无暂停的暴露，不能据摘要把整个墙钟区间认定为有效暴露。后续暴露评估仍需独立证据和既有规则。已结束的BLOCKED摘要不会成为“当前运行”。

本迁移不修改已有历史执行、时段、LEGACY_REPORTED标记或既有统计结果，不重复导入135条历史。旧记录不会被补造为已批准的数据。

## 幂等、并发和恢复

- 提交需要`Idempotency-Key`。同一提交人/幂等键的相同内容返回既有提交；同键改内容返回409。稳定execution UUID只能归属一个提交人。
- 初次`expected_revision=0`；仅REJECTED当前版本可重提，并必须携带该版本号。重提追加版本，旧摘要和驳回意见保留。
- 审核携带revision、decision、reason与幂等键。数据库行锁与唯一约束保证并发只发布一次；另一请求返回409。重复的相同审核请求重放既有结果，不重复写正式记录。
- HMI将原提交账号、版本与精确请求正文保存在本地SQLite。网络请求前先冻结正文，丢失回执或本地重建报告后仍以同一正文/幂等键恢复。账号切换不消费他人的队列；没有绑定账号的旧队列不会自动发送。
- PENDING只轮询状态，不重做提交。驳回后停止自动重试，用户修正报告后显式重提；也可在网页修改有界摘要并说明原因后重提。网页重提的版本与本地原报告分别保留，通过证据摘要和审批历史溯源。
- 网络失败保留本地队列并退避重试；旧接口410、权限403、版本409和参数422都会显示错误，不能显示为批准成功。

## 文件与接口

| 文件 | 新增/修改及行为 |
|---|---|
| `database/alembic/versions/20261012_0025_test_data_approval.py`、`database/sql/025_test_data_approval.sql` | 新增迁移、待审/版本/审核表、RLS、审计；`submit_test_data()`及`review_test_data()`实现权限、并发、版本与原子发布 |
| `service/app/schemas/submissions.py` | 新增严格有界摘要、提交及审核契约，禁止额外字段、NaN、超墙钟时长和无时区时间 |
| `service/app/repositories/submissions.py`、`api/routes/submissions.py`、`main.py` | 新增队列、详情、提交和审核接口，沿用Cookie、CSRF及数据库事务 |
| `service/app/api/routes/ingestion.py` | 修改：全部旧直接接入接口返回410；包括by-producer、telemetry、events、finish和artifact路径 |
| `service/app/repositories/read_models.py`、`schemas/read_models.py` | 修改：返回正式申报循环；已结束记录不形成当前运行上下文 |
| `frontend/src/submissionsApi.ts`、`pages/SubmissionsPage.tsx`、`App.tsx`、`navigation.ts` | 新增网页审批入口、分页筛选、审核、驳回重提及版本历史；失败不显示成功，自审无操作按钮 |
| `frontend/src/pages/ReadModelPages.tsx`、`types.ts` | 修改：正式执行详情展示申报完成循环；旧记录显示未记录 |
| `host/factory_hmi/sync/reliability_platform.py` | 修改：`ReliabilityUploaderWorker`仅读取本地汇总并提交摘要，使用人类会话；移除CSV转时序点代码，批准前无正式单号 |
| `host/factory_hmi/sync/outbox.py`、`gateway/app.py`、`desktop/app.py` | 修改：账号绑定、持久化版本/冻结正文、断线恢复、显式重提及状态说明 |
| `host/factory_hmi/sync/uploader.py` | 修改：旧适配器真实网络的执行包/分块上传在发送前拒绝，防止切换legacy模式上传原始CSV |
| `scripts/test-approval.py`、相关测试、`.github/workflows/ci.yml` | 新增全新独立PostgreSQL容器验收入口并扩展CI测试；不读取站点.env，不使用生产容器、卷或数据库 |

接口：`POST /api/v1/submissions`提交；`GET /api/v1/submissions`分页列表；`GET /api/v1/submissions/{id}`查看全部版本；`POST /api/v1/submissions/{id}/review`审核。审核理由至少1个字、最多300字。

数据库应用账号撤销对正式执行、阶段、结果、运行时段、事件、遥测及附件表的直接写权限；仅通过检查人类权限的审批函数发布。管理员角色的普通应用连接也不能靠直接SQL写正式记录。数据库所有者的受控迁移/恢复权限仍保留，不属于应用接口。

## 验证与上线边界

2026-10-12本机验证结果：前端lint/类型检查/51项测试/生产构建通过；后端65项非集成检查与9项隔离集成检查通过；HMI完整mock及离屏回归376项测试和119项子测试通过。后端与HMI各有既有Starlette/httpx弃用警告，无测试失败。`scripts/check-release.py`及`git diff --check`通过。以上数量是本次各自测试集的结果，不与历史版本结果相加。

可复现软件验证入口：`python3 scripts/test-approval.py`。它创建随机命名独立PostgreSQL容器、不开放宿主端口、生成临时凭据，执行全部25次迁移，使用应用角色运行HTTP/RLS与并发验证，最终删除临时容器。复用已有迁移和QA工具镜像，不启动真实PLC/CAN。

前端执行`npm run check`；HMI执行专用mock配置、fake传输和Qt offscreen回归。测试覆盖摘要边界、正式查询隔离、禁止自审、撤权/停用、幂等冲突、驳回重提、并发审核、发布失败回滚、真实Worker零有效暴露、旧接口与数据库权限防绕过、账号切换、丢失回执和旧二进制上传拒绝。

上线须配套应用迁移0025及新API、Worker读取代码、网页和HMI适配器。迁移与API不应在旧采集写入仍运行时分开切换。先按现有运维流程完成维护备份，在隔离恢复副本验证迁移和历史查询，再切换匹配版本；本次未执行生产迁移或更新用户正在运行的程序。

0025不提供丢弃审批历史的downgrade；需要回退时使用核验过的备份恢复到独立环境并核对审批记录与切换后写入，不能直接恢复旧写入权限来绕过审批。生产部署、安装包构建/发布/安装、真实浏览器账户验收及现场长时间稳定性仍需后续完成。
