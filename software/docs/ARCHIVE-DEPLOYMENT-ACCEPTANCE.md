# 中央归档完整隔离部署验收

本记录对应 2026-10-15 的完整部署、候选 HMI 安装与 mock 跨端验收，接续 [中央归档实现说明](CSV-ARCHIVE-IMPLEMENTATION.md)。源码工作区包含此前未提交修改，使用逐文件 SHA256 固定候选，不能仅用 Git HEAD 代表验收源码。

## 环境与边界

- 生产构建配置启动 PostgreSQL、API、常规 Worker、归档 Worker、Nginx 前端，迁移至 `20261015_0040`。数据库没有发布宿主端口，Web 仅绑定 `127.0.0.1` 的随机空闲端口。
- 全新项目、全新数据库及归档卷，独立生成 QA 账号与两天有效的本地 CA／证书。只读复制源码白名单，不读取站点 `.env`、真实采集 CSV 或现有数据库。
- 根目录 `.env`、已有安装包、现场容器和 PLC/CAN 保持原状。原始文件、凭据、备份、构建包均未加入版本库。
- API 的 `AUTH_COOKIE_SECURE=true`，真实 HTTPS 驱动校验 CA、主机名、Secure Cookie 和 CSRF。驱动加入本次 Web 容器的网络命名空间，不使用宿主网络或设备挂载。
- 浏览器工具拒绝自签证书，本轮没有绕过警告或修改系统信任。浏览器交互使用额外的 **localhost HTTP QA 入口**，加载相同前端镜像并代理到相同真实 API；该证据与独立 HTTPS 验收分开记录，不宣称浏览器完成证书验收。QA API 仍保留 Secure Cookie，浏览器使用 localhost 的例外规则。

## 已执行的业务链路

源码 HMI 验收产生五份合成记录：

| 记录 | 操作与预期 |
|---|---|
| A | `FactoryService` 显式选择 `FakeMotorBackend`，实际播放并采集 → 本地报告 → 核实 → 上传 → 独立 Worker 解析 → 待审 → 自审批准 → 查看者读取正式执行和曲线。 |
| B | 含已知 12 N·m 尖峰及末帧故障的 OTHER → 归档可看曲线 → 后补正式用例 → 驳回 → 追加核实备注 → 第 2 次提交 → 批准；机器失败事实及原始 SHA 不变。 |
| C | 保持未归类 READY，执行员可查曲线，查看者直接访问详情／曲线受拒绝。 |
| D | 保持待审，由真实浏览器填写审核意见并自审批准；此前不进入正式执行查询。 |
| E | `7,387,820` 字节合成 CSV，第一块 `4,194,304` 字节服务端提交后客户端注入丢回执 → SQLite 保存失败状态 → 重启 API → 重开客户端按中央偏移上传剩余 `3,193,516` 字节。归档 Worker 停止时为 QUEUED，启动后独立解析为 READY。 |

旧 ingestion 同时验证两层：HMI 客户端本地拦截为 410；另以真正的 HTTPS 请求检查服务端路由未开放（404），不能把本地拦截误当成服务端状态。

已安装的候选包另外产生四份合成记录，重复 A／B／C／D 流程。驱动无 HMI 源码挂载，使用 `/opt/rp1-test-hmi/hmi` 的实际安装模块及随包依赖；模块路径有运行时断言。安装模块的 49 个 Python 文件与验收源码逐一 SHA 相同。

## 候选包

- HMI 版本保持 `2.0.9+ubuntu24.04`，平台源版本为 `2.0.6`，实际 QA 镜像标签采用独立的源码哈希后缀。
- 最终平台候选包含 375 份源码／配置文件，总 SHA256 `4a8c4c1f2bf11c746435f17a74ca95314462938cb0dc13fd8b49b2af29ad3b7a`，API 实际版本为 `archive-qa-4a8c4c1f2bf1`。前两版快照和失败证据保留，最终结论指向第 3 版。
- 候选 Deb：`rp1-test-hmi_2.0.9+ubuntu24.04_amd64.deb`，`100,688,528` 字节，SHA256 `ef72da747c85c30a146e204286daf66b538798e247d175a1db72ee4104c91c61`。
- 干净 Ubuntu 24.04 按包声明依赖安装，非 root、无网络时 mock gateway 与 Qt offscreen 实际启动并正常退出，CAN 禁用，未创建原生设备驱动。
- 校验后复用已有匹配的原生 SDK runtime，没有重新编译 SDK。包内 87 个应用文件与构建清单一致。
- 本轮没有发布 Releases、安装宿主系统或执行现场升级。真实显示器、PLC/CAN 与长期运行仍需现场验收。
- 此包用于按 SHA 识别的候选验收；正式分发前仍需确定发布版本，不能把相同版本号当成已验证的自动升级策略。

## 本轮发现与修复

| 文件 | 修改、原因及验证 |
|---|---|
| `scripts/up.sh` | 构建、启动清单加入 `archive-worker`，避免首次部署或恢复后没有归档解析进程；在全新 QA 项目实际执行启动及 `verify.py`。 |
| `scripts/test-archive-deployment.py` | 新增白名单候选快照、唯一 QA 标签、HTTPS 完整启动、队列中断恢复、安装包调用、服务器原件校验、交付脚本联合备份恢复及资源清理；修复后生成新候选目录和新镜像，保留上一版证据。 |
| `scripts/test-archive-deployment-guards.py`、CI | 新增状态路径、项目、符号链接、容器身份及启动失败清理的无 Docker 回归，防止误操作其他站点；CI 纳入守卫检查。 |
| `scripts/check-archive-deployment.py` | 新增只走真实 HTTPS 的 `queue/resume/seed/conflict/verify` 驱动；不直接连接数据库，不启动 API，不调用 `run_once`，不替换真实归档 Worker。 |
| `frontend/src/pages/SubmissionsPage.tsx` | 人工结论直接读取 `verification.verdict`，旧记录显示原始摘要结论；机器完成不等于自动或人工合格，不能只修改标签而继续读取机器 `source_status`。 |
| `frontend/src/components/TimeSeriesChart.tsx` | 更新数据前隐藏旧提示，清除 ECharts 保留的旧系列索引，防止视窗／系列替换时悬浮提示抛 `getDataParams` 错误。 |
| `frontend/src/pages/ArchivesPage.tsx` | 查看者隐藏无审批权限的导航按钮。 |
| `host/tools/build_local_deb.py`、`host/docs/LOCAL_CONTROL_BUILD.md` | 更新旧“只上传摘要”的说明，准确描述专用 CSV 归档和追加版本。 |
| `host/tools/verify_setpoint_input.py` | 离线验收替身补齐 V1.2 协议及设定值有效位；修正前 5 项失败，修正后 8 项通过，没有改 PLC 产品逻辑。 |

## 可复现入口

先构建验证工具镜像，完整部署验收使用交付的真实 `up.sh`、`verify.py`、`backup.py` 和 `restore.py`：

```bash
docker build -f deployment/ArchiveQa.Dockerfile -t rp1-archive-qa:local .
python3 scripts/test-archive-deployment.py
```

默认自动创建独立 `/tmp/rp1-archive-deploy-*` 目录，执行源码闭环、重启和新项目恢复，正常结束后清理自己的容器与卷。构建、启动或业务阶段失败时会尝试清理；清理失败可按已打印目录执行 `--phase cleanup` 复试。源码快照、无秘密的结果和私密 QA 设置保留在该目录；其中凭据／私钥／备份不能提交或公开。

需要分阶段浏览器／安装包验收时：

```bash
python3 scripts/test-archive-deployment.py --phase start
# 以下 RUN_DIR 使用上一步打印的独立目录，不能指向现场目录。
python3 scripts/test-archive-deployment.py --phase exercise --run-dir RUN_DIR
python3 scripts/test-archive-deployment.py --phase package --run-dir RUN_DIR --hmi-image CLEAN_HMI_IMAGE
python3 scripts/test-archive-deployment.py --phase browser --run-dir RUN_DIR
# 完成浏览器操作后进行联合恢复。
python3 scripts/test-archive-deployment.py --phase recover --run-dir RUN_DIR
python3 scripts/test-archive-deployment.py --phase cleanup --run-dir RUN_DIR
```

`CLEAN_HMI_IMAGE` 必须是已安装候选 Deb 的干净 Ubuntu 镜像，应用路径遵循现有 `/opt/rp1-test-hmi/hmi` 布局。`refresh` 阶段用于验收发现修复后冻结下一版源码、构建新镜像并重新验证，沿用同一独立 QA 项目的合成数据。

## 验证收据状态

以下均已执行通过：

- **完整部署**：实际交付 `up.sh`／`verify.py`，五服务 healthy、数据库 0040、API 与归档 Worker 共用本项目新卷；真实 HTTPS 验证 CA 与主机名、Secure Cookie、缺少 CSRF 的 403。
- **跨端闭环**：源码 HMI 五记录和已安装包四记录，核实、上传、独立解析、自审、OTHER 后补归类、失败记录、驳回重提及查看者隔离。
- **失败恢复**：两块合成文件的首块已提交但客户端丢回执，SQLite 持久化，API 重启后按中央偏移续传，独立 Worker 重启后处理排队记录。
- **浏览器**：真实自审、35–37 秒的 401 个原始点及 12 N·m 尖峰、同参数叠加、X/Y 输入、参考线、实际滚轮及最终窄屏；悬浮期间系列 `2→0→1→0→2`，新候选控制台警告／错误为 0。冲突记录实际显示人工待判定与自动未配置，保持待审。
- **前端回归**：17 个文件、72 项测试及 lint／类型检查／构建通过。真实 ECharts 引擎回归在旧代码复现 `getDataParams` 异常，修复后通过；另有人工待判定／失败与机器通过冲突的两项回归。
- **联合备份恢复**：实际 `backup.py --maintenance` 暂停全部四类写服务，生成数据库及中央归档快照；`restore.py` 恢复至另一个新项目、新数据库卷、新归档卷、新端口。恢复后五服务 healthy、0040、源码及安装包两套查询验证通过。
- **原件实证**：原／恢复两个 API 容器逐一读取 9 份 `raw.csv`，实际 SHA256、字节数、0440 权限和 UID 10001 完全一致；批准 UUID、核实／提交版本及曲线内容保持一致，原项目仍独立可查询。
- **复现与清理**：默认单命令另外从头执行成功；本轮四个自有 QA 项目的容器、卷及网络全部清理。6 项无 Docker 守卫测试通过，错误路径／项目／容器身份在动作前被拒绝。

本轮独立浏览器复核 PASS（92/100），独立最终部署／恢复复核 PASS（94/100），无剩余阻断项。证据包括 `candidate-files-v3.json`、`stack-health*.json`、`deployment-result.json`、`installed/deployment-result.json`、`browser-acceptance.json`、`raw-original.json`／`raw-restored.json` 和各阶段日志；私密账号设置及备份不公开。

候选 Deb、SHA256 文件、最终截图和公开收据已白名单保存至被 Git 忽略的 `artifacts/archive-acceptance-20261015-4a8c4c1f2bf1/`。该目录不含账号密码、站点配置、私钥、数据库备份或采集 CSV；不是公开发布渠道。

技术机制核对：[Docker Compose 启动顺序](https://docs.docker.com/compose/how-tos/startup-order)、[Nginx 请求体限制](https://nginx.org/en/docs/http/ngx_http_core_module.html#client_max_body_size)、[localhost Secure Cookie 例外](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Set-Cookie)。实际通过与否以本项目执行证据为准。

容量上限配置不是压测结果。7.39 MB 文件用于验证多块上传和故障恢复；现场文件大小及同时上传工位数尚待确认，不据此宣称完成 50 GiB 或多工位容量验收。
