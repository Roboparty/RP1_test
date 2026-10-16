# 电脑3号部署、布局与历史迁移验收

部署主机：`shn-System-Product-Name`，Ubuntu 24.04.5 amd64。
部署目录：`~/桌面/rp1_test_shn-e56cabd`。
上游基础提交：`1f76c2781289aee359beaf0ede93f21568e11403`（已核验的 v1.0.1）。
本次为本地补丁，镜像版本 `1.0.1-pc3-setup2`，现已纳入 v2.0.0 源码发布。API现已更新历史实时计时修复，构建标签 `rp1-shn-api:pc3-history-clock`；Worker仍运行原已验证镜像，计算逻辑未改动。

## 当前访问和可见内容

电脑3号浏览器访问 **https://localhost:8443**，旧页面请 Ctrl+F5 刷新。
实际监听 `127.0.0.1:8443`；API 和数据库不发布宿主端口。
PostgreSQL、API、Worker、Web 四个服务均使用 `unless-stopped`。

总控基于静态测试目录展示整机及7个模块的模型外观，样品为零也可浏览。
现有真实图片来自 `frontend/public/assets` 与 `frontend/public/model-posters`。
图片表示产品外观参考；样品数量、执行和时长来自有权限查看的数据库记录。

历史导入后的线上只读核对结果（历史均属于模块，切换顶部“模块”后查看）：

| 部位 | 实际样品 | 已启用且发布的用例 |
| --- | ---: | ---: |
| SYS 整机 | 0 | 18 |
| SARM 单臂 | 3 | 11 |
| SLEG 单腿 | 2 | 10 |
| UPPER 上肢 | 1 | 10 |
| LOWER 下肢 | 1 | 10 |
| CHEST 胸腔 | 0 | 9 |
| HEAD 头部 | 0 | 0 |
| BAT 电池 | 0 | 0 |

“测试目录”共展示69项，包含1项停用项目；总控为68项。相比空库新增源历史需要的PERF-SARM-001遗留导入版本，其他目录未覆盖。
头部、电池没有现成启用用例，不制造资料或运行数据。
已导入7个历史模块样品和4个历史计划，135条执行属于9个用例、3个台架。整机、胸腔、头部、电池没有此次迁移范围内的历史样品；管理员仍可使用新建流程建档。

## 操作流程及保存规则

1. 管理员在“样品中心 → 新增样品”录入真实编号、部位、序列号和配置。
2. 在“测试计划 → 创建项目和计划”先创建项目，再创建待执行计划。
3. 在“台架建档”建立真实站点、实验室及台架；已有组织编号必须保持相同名称。
4. 在“计划与关联”选择计划、样品、已发布用例、配置快照及台架，配置周期。
5. 管理员可将待执行计划开放为 ACTIVE，供现有权限审批工作流使用；不会发出硬件指令。

填写期间内容只在浏览器表单内；点击保存后通过本站 HTTPS 提交 API。
后端使用登录会话、角色、CSRF 和现有 PostgreSQL RLS 判断权限。
样品、项目、计划和台架创建仅限 SYSTEM_ADMIN。
执行员配置关联需拥有计划及样品 EDIT 权限；查看遵守原有授权。
项目创建选项只向管理员返回；没有项目元数据权限时，已授权计划仍可显示。
未修改既有数据库授权策略或系统证书、安全设置。

每个创建操作在单个数据库事务内完成，并记录操作者、请求标识及理由。
唯一编号重复返回409，字段或关联不匹配返回422；任何失败回滚整个操作。
创建样品写入 CANDIDATE 样品、对应类型档案及 UNKNOWN 影响的初始配置。
项目初始 DRAFT，计划和周期初始 PLANNED，覆盖项初始 NOT_STARTED。
关联会建立初始分析段及采集所需公开 UUID 标识；不会生成执行、时长或暴露评估。
建档及账号权限审批不代表测试数据已审批，不自动形成正式 MTBF 结论。

## 文件级实现

| 文件 | 函数或组件与行为 |
| --- | --- |
| `service/app/repositories/read_models.py`（修改） | `dashboard_home` 从测试部位、用例目录出发左连接样品；空库保留模型范围与用例，聚合仍受RLS约束。导入后根据execution_import_record溯源标识排除历史实时计时、当前运行上下文及活跃执行；历史状态和累计时长保留，原生实时执行不受影响。 |
| `service/app/schemas/setup.py`（新增） | 创建请求严格校验编号、类型、部位、时间、UUID、配置及理由；拒绝额外字段。 |
| `service/app/repositories/setup.py`（新增） | `SetupRepository` 实现目录、可见计划、建档与关联；锁定计划防重复并发，复用既有审计和RLS。 |
| `service/app/api/routes/setup.py`（新增） | `/api/v1/setup` 路由执行角色校验，将数据库约束错误映射为稳定409/422/403。 |
| `service/app/main.py`（修改） | 注册 setup 路由。 |
| `frontend/src/setupApi.ts`（新增） | 同源 API 读写，沿用会话和 CSRF 请求；写入附变更理由。 |
| `frontend/src/pages/SetupPages.tsx`（新增） | `CatalogPage`、`PlanningPage`、`AssetCreateForm` 展示目录、计划及真实建档表单；成功刷新查询，失败保留表单。 |
| `frontend/src/pages/ReadModelPages.tsx`（修改） | 样品中心保留全部7个模块分组及零计数，并提供管理员建档入口。 |
| `frontend/src/App.tsx`、`navigation.ts`（修改） | 新增测试目录、计划导航；首次改密完成前不请求管理员待处理列表。 |
| `frontend/src/styles.css`（修改） | 增加表单、计划列表布局及窄屏导航。 |
| `frontend/src/homeLayout.css`、`main.tsx`（新增/修改） | 总控外围、模型及统计采用响应式网格，替换重复的尺寸调节规则；模型等比例显示，桌面两栏等高，平板/手机自动堆叠。右侧恢复原半透明机械翻页走马灯，保留原卡片内容及过渡。 |
| `frontend/src/pages/HomePage.tsx`（修改） | 为图片统计增加底部容器；从模块切回整机时使用实际范围索引，避免出现06/01。保留原自动轮播、暂停、翻页、键盘操作和真实数据。 |
| `service/tests/test_setup_integration.py`（新增） | 空库HTTP、真实登录、角色、RLS、CSRF、重复、事务回滚、审计及零运行数据回归。 |
| `frontend/src/pages/SetupPages.test.tsx`、`HomePage.test.tsx`（新增） | 空库目录和部位、角色入口、保存错误、零样品模型图与用例翻页。 |
| `scripts/test-setup.py`（新增） | 创建并迁移临时 `rp1_presence_setup` 数据库；finally 删除临时库，不写生产测试事实。 |
| `scripts/verify-setup.py`（新增） | 线上只读目录及图片、API鉴权检查，指定本地CA验证TLS；不创建登录会话。 |
| `scripts/preflight-history.py`（新增） | 仅校验源文件及聚合数量，不写数据库，也不输出原始记录。 |
| `scripts/verify-history.py`（新增） | 使用实际API镜像、现有管理员审计上下文和READ ONLY事务验证导入后查询、历史计时及正式统计排除；指定本地CA核验HTTPS和匿名401，不创建登录会话。 |
| `service/tests/test_history_migration_integration.py`、`scripts/test-history.py`（新增） | 在独立临时库核验实际源文件、迁移、幂等、碰撞回滚、HTTP权限和Worker；finally 删除临时库。 |
| `scripts/import-history.py`（新增，生产已完成一次） | 再核对源摘要和备份，锁定业务表并确认空库及保留身份无碰撞；使用已有管理员审计上下文调用原导入器，数量与逐行溯源验证通过才提交。未创建账号、会话或配置正式MTBF。 |

数据库结构维持 `20261008_0024`，本次无需新迁移。

## 验证与限制

- 前端 lint、类型检查、47项测试和生产构建通过。
- 后端64项非集成检查及4项临时库集成检查通过；临时库已删除。
- 项目 `scripts/verify.py` 的 HTTPS、API、PostgreSQL、Worker 检查通过。
- 导入前 `scripts/verify-setup.py` 的68目录项、8部位、67启用用例、8个WebP资源及匿名API拒绝检查通过。导入后改用verify-history.py，69/68及实际历史查询通过。
- 实际135条源文件的7项CSV检查通过；新增隔离历史综合检查通过，涵盖24次迁移、135/7/4/3/124数量、逐行溯源、幂等、碰撞回滚、HTTP权限和真实Worker计算；临时库已删除。
- 使用电脑3号 Codex 内置浏览器可正常打开本站登录页，无证书绕过。
- Chrome 未连接自动化工具；用户已在电脑3号Chrome登录，截图确认模型和用例已显示。自动布局更新后的登录页面仍需用户在Chrome刷新核对。
- 首次自适应版本在电脑3号 Codex 内置浏览器完成1366×768、1920×1080、2048×1150桌面，1024×768平板、390×844手机及全屏检查。用户随后明确要求保留原走马灯，因此已撤回平面用例卡片及新增页码提示，恢复相邻卡片42%不透明度、轻微模糊、立体翻页及边缘渐隐。
- 恢复后再次核对2048×1150、1366×768及390×844：无横向溢出，当前用例内容未裁切；观察到用例从REL-UPPER-001自动推进到REL-UPPER-007，手动下一项为008。默认自动翻页仍启用，7项相关测试及生产构建通过。原有47项完整检查的结果保留，没有重复后端检查。
- 尺寸验证使用本机127.0.0.1临时只读预览，载入当前空库目录与真实模型资源，未创建登录凭据或业务记录。截图明确标注“只读布局预览”，不代表Chrome登录后截图。预览完成即停止并关闭临时浏览器标签。
- 线上HTML及CSS与恢复走马灯后的生产构建逐字节一致；资源名与SHA256见 `.qa/pc3-setup/carousel-restoration-live-build.json`。本次仍只替换Web，API、Worker及PostgreSQL容器ID不变。
- 恢复后的只读截图为 `artifacts/layout/desktop-2048.jpg`、`carousel-restored-1366.jpg`、`carousel-restored-mobile.jpg`；其余截图属于先前平面卡片版本。对应新日志为 `.qa/pc3-setup/carousel-restoration-*`。Library附件 `libfile_615251bd7718819192d387388ec690ed` 已更新为恢复后截图（版本1）。
- 未启动 HMI、控制网关或硬件动作，未完成硬件联调；用户直接确认后，生产已导入135条非SHOWCASE历史执行元数据，未导入历史曲线或源IAM账号、会话、presence。
- 原仓库 `program_select` 的子查询有未限定的 `id`，且 `program_write FOR ALL` 同时影响读取；本次新接口限定管理员项目选项并避免项目不可见时隐藏已授权计划。全局RLS整改属于后续事项。

## 历史源文件与验收结果

来源为主会话已核验的固定提交 `e954c5ce327d85f342a719d15249495549f1be00`，分支 `history-transfer/20261008`；应用部署基础和本地布局补丁未切换或覆盖。只提取执行元数据CSV、独立摘要和交接清单三个文件，没有拉取或恢复电脑1号整库。
Library接收及一次受限刷新均返回HTTP403，随后主会话指定GitHub固定提交作为替代来源；没有继续尝试其他下载身份或绕过TLS。

本机接收目录 `.qa/history-incoming/` 已创建为0700，三个文件均为0600，Git忽略：

| 文件 | 字节数 | SHA256 |
| --- | ---: | --- |
| `executions_202609181314.csv` | 63678 | `d4b4c6e96ed9fb9e05267d77cea0f747931ea72458f30994e538e24f1cd68c0c` |
| `executions_202609181314.csv.sha256` | 94 | `1ee49635f5a12292276ff604ea80246a8657292b15862961be68b439c0746d50` |
| `source-audit.json` | 9663 | `9397d058a3e4fb9111a751a33999fced17b873244457cf0f4e1fde62e3dd90fb` |

所有字节数和摘要通过核对，CSV未改写。源端交接清单报告新备份及完整临时恢复83项检查通过；该完整恢复发生在电脑1号，不是电脑3号。源端完整备份和敏感配置留在电脑1号。

只读预检和隔离库均核验：135条非SHOWCASE执行、7样品、4计划、3台架、9用例、124条已结束 `LEGACY_REPORTED` 时段。源状态为passed89、failed17、blocked18、scheduled9、running2；running是历史字段，不表示当前机器人在线，也不触发HMI。
7样品规范化为SARM001/002/003、SLEG001/002、UPPER001、LOWER001，前缀RP1.3-；台架RD-01/02/03分别30/72/33条。
保留6项申报时长冲突、49项源状态语义冲突，以及其他规范化提示；不修改源记录以消除警告。每条来源编号、摘要、原始元数据和提示均在隔离库逐行核对。

隔离验收完成全部24次迁移；重跑后数量和执行/时段身份不变；模拟已有真实样品身份碰撞会回滚前序写入和审计。实际HTTP验证管理员可见全部7样品/4计划/135执行，未授权观察员不可见，单计划VIEW授权仅开放对应数据。
实际Worker在隔离且最终回滚的事务内为4计划计算，所有历史时段归为LEGACY_REPORTED：正式有效暴露为0，点估计为空，70%下限为0，状态 `NO_EXPOSURE`。临时计算配置和结果未写入生产；隔离库在finally删除。
没有导入SHOWCASE样品、曲线、在线presence、源账号/会话/凭据，也没有把历史时长当作正式合格暴露。

最初备份 `backups/pre-history-135-20261008T082259Z/` 已保留。重试前再次核对生产为空，并生成当前新备份 `backups/pre-history-authorized-20261008T083534Z/`，包含775641字节的 `postgres.dump`、敏感配置包、元信息及摘要。全部SHA256和pg_restore恢复目录可读检查通过；电脑3号尚未做整库恢复演练。备份暂时停止写入服务，finally已恢复API/Worker/Web健康；PostgreSQL持续运行。

## 生产导入已完成

早先两次自动审批拒绝均发生在创建进程前，未写入生产。用户随后在当前执行会话直接撤销不导入历史的限制，明确批准135条非SHOWCASE执行及关联7样品/4计划/3台架，保留历史标记且不计入正式暴露或MTBF。原导入调用获准，于2026-10-08 **16:40:28（Asia/Shanghai）** 提交成功一次，没有重复生产导入。

单个事务锁定业务表，重新确认空库、迁移版本0024和保留身份无冲突；调用既有导入器后校验135行的源编号、row_hash、原始元数据和规范化提示，核对数量及正式统计排除，最后提交。审计请求 `pc3-history-import-20261008T084028Z` 关联现有管理员及理由，共679条审计变更；IAM主体数量未变。

| 已提交内容 | 数量 |
| --- | ---: |
| 样品 / 计划 / 台架 | 7 / 4 / 3 |
| 执行 / 结果 / 原始溯源 | 135 / 135 / 135 |
| LEGACY_REPORTED已结束时段 | 124 |
| 历史问题事件 / 报告引用 | 85 / 7 |
| 正式暴露评估 / MTBF配置及结果 / HMI presence / 遥测序列 | 均0 |

7项报告引用只是源端链接元数据，本机没有导入报告文件或曲线，不保证引用目标可用。历史累计2527185.511秒，约701.996小时；正式有效暴露为0。原源状态passed89/failed17/blocked18/scheduled9/running2完整保留；规范化结果为PASSED40/FAILED17/INCONCLUSIVE67/NOT_EVALUATED11，原49项语义矛盾没有强行算作通过。6项申报时长冲突及其他提示仍可从执行详情的“源数据规范化记录”查看。

首次生产读取验收发现历史RUNNING记录被当作当前运行计时。已仅修复 `service/app/repositories/read_models.py`：历史溯源记录不产生实时elapsed、当前运行上下文或活跃执行；原始/规范化状态不改写，历史累计仍可查询。使用隔离库重跑实际HTTP及读取仓库，验证历史计时均0，同时插入一个随后回滚的原生mock执行，验证原生实时计时和活跃计数仍有效；13项读取模型单元回归通过，隔离综合检查通过。没有创建生产mock或新凭据。

API修复从原已验证镜像复制唯一读取仓库文件构建，复用原依赖和安全设置。只更新API容器，Web、Worker和PostgreSQL保持原容器；API文件摘要与本机源码一致（`ea7ae504a5b644089f7fdcb047dd57326792aee2c9abc3efd54c1aef94244ead`）。未修改前端代码、轮播时间、卡片透明度或翻页设计。

最终生产只读验收通过：真实API仓库可读7样品、4计划、135执行；8部位68启用用例、69目录项；累计时长与数据库及首页一致；实时elapsed、正式有效暴露、MTBF结果、presence和曲线均0。数据库仍为0024，临时历史库已删除。项目verify.py的严格HTTPS、Web到API就绪及Worker数据库连接检查通过；匿名业务查询返回401。四服务健康，Web仅发布127.0.0.1:8443。

电脑3号内置浏览器能正常打开实际生产登录页，没有绕过证书；Chrome未连接自动化且没有取得用户现有登录会话。为检查呈现，临时仅本机只读预览载入实际生产首页查询快照和原UI：确认模块数量SARM3/SLEG2/UPPER1/LOWER1；上肢显示251.09小时、43次执行、有效暴露和实时计时0；1366×768无横向溢出，图片加载成功，原机械翻页和半透明相邻卡片保留。截图临时暂停以便读取，随后已恢复两处自动轮播并关闭预览。该截图明确标注只读预览，不是Chrome登录生产页。

历史日期距当前较远，所以首页“同步延迟”符合源事实截止时间；数据源NO_DATA表示没有采集接收包，不表示已导入的135条历史丢失。历史RUNNING徽标保留源状态，活跃执行和实时计时均为0，不代表机器人正在运行。

可核验文件均在 `.qa/pc3-setup/`：

- `history-received.json`、`history-preflight.json`：接收与预检；
- `history-source-tests.log`、`history-isolation.log`：原7项CSV及隔离验收；
- `history-live-clock-regression.log`、`history-read-model-unit.log`：实时计时问题修复后的隔离与13项单元检查；
- `history-production-import.json`、`history-production-verification.json`：已提交数量及最终只读应用验收；
- `history-backup-path.txt`：最新有效导入前备份；
- `history-final-target.log`、`history-final-health.log`：最终数据库与服务验收；
- `history-ui-unchanged.json`：线上原走马灯CSS摘要一致，临时预览已停止；
- `history-browser-library.json`：导入后截图文件库身份，`artifacts/layout/history-dashboard-1366.jpg`为本机原图；
- `history-production-blocker.json`：早先审批拒绝的记录，目前已通过用户直接授权解决。

## 运行、日志、备份与回滚

进入上述部署目录后：

```bash
./scripts/compose.sh up -d --wait
./scripts/compose.sh stop
./scripts/compose.sh logs --tail=200 -f api worker web
python3 scripts/verify.py
python3 scripts/verify-history.py
```

停止命令保留数据库卷。不要使用 `down -v`。
Docker日志使用 json-file，单文件10MiB、最多5份。
本次检查日志已另存到 `.qa/pc3-setup/`，该目录被Git忽略。
导入已完成，日常启动无需再运行import-history.py；其空库保护会拒绝向现有业务库重复写入。原verify-setup.py是空库初始68/67口径，当前应使用verify-history.py核对69/68及历史记录。

部署前备份位于 `backups/pre-setup-0025/`：`postgres.dump`、`config.tar.gz`、校验和及元信息。
目录名是预留编号；数据库仍为0024。备份含敏感配置，应只在本机受控目录保管。
历史计时修复前的API镜像 `rp1-shn-api:pc3-before-history-clock` 已保留；回退该镜像会恢复已知的历史实时计时问题，不会撤销数据库历史导入。
历史数据恢复点为 `backups/pre-history-authorized-20261008T083534Z/`。若需恢复，应先保存当时最新备份并在独立数据库验证，再审查恢复范围；本次没有恢复生产库或清除卷，电脑3号未做完整恢复演练。

旧镜像已保留：`rp1-shn-api:pc3-before-setup`、`rp1-shn-web:pc3-before-setup`。
布局更新前的前端镜像另保存为 `rp1-shn-web:pc3-before-layout`；布局更新仅替换 Web 容器，API、Worker及PostgreSQL持续运行。仅回退布局时将该镜像重新标记为 `rp1-shn-web:1.0.1-pc3-setup2` 后执行 `./scripts/compose.sh up -d --no-deps --wait web`，无需数据库操作。

```bash
docker image tag rp1-shn-web:pc3-before-layout rp1-shn-web:1.0.1-pc3-setup2
./scripts/compose.sh up -d --no-deps --wait web
```
如需回退应用，在本机只修改 `.env` 的 `RP1_VERSION` 为 `pc3-before-setup`，随后：

```bash
./scripts/compose.sh up -d --no-deps --wait api worker web
```

无需恢复或降级数据库。恢复本次版本时仅将版本字段改回 `1.0.1-pc3-setup2`，再运行同一应用更新命令。
初始密码不再适用：用户已完成改密；本说明及日志不记录用户密码或任何密钥。

## 电脑3号上位机安装

桌面项目为新版上位机源码与 deb 的统一构建输入。v2.0.0 的安装与构建方法见
[当前上位机构建说明](../host/docs/LOCAL_CONTROL_BUILD.md)。旧 PLC 模拟安装和快捷方式已移除。
应用菜单名称为 `rp1-test-hmi`。状态、日志、数据与配置位于当前用户的
`~/.local/state/rp1-test-hmi/`，系统安装提供受管理员认证保护的 CAN 助手。
软件发布不会更新现有 Docker 服务、生产数据库或启动现场设备。
