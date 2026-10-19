# 安装与运维

适用于 `reliability_v1` 平台与 Ubuntu 24.04 amd64 上位机。平台命令从 `software/` 执行。

## 首次安装

准备 Docker Engine、Compose、Python 3、OpenSSL，确定访问平台的 DNS/IP。使用新克隆目录、新项目名和新数据卷：

```bash
python3 scripts/configure.py
# 局域网：python3 scripts/configure.py --hostname SERVER_IP_OR_DNS --bind 0.0.0.0
bash scripts/up.sh
python3 scripts/verify.py
```

`configure.py` 生成站点密码、`.env`、CA 和 HTTPS 证书，拒绝覆盖已有安装。默认入口为 `https://localhost:8443`；初始账号位于本机 `deployment/secrets/initial-admin.txt`，首次登录必须改密。这些文件不能提交或共享。

在浏览器和 HMI 机器信任本站 `deployment/secrets/root-ca.crt`。地址须匹配证书 SAN；分机部署连接实际平台服务器，不能填写 PLC 地址或工位自己的 localhost。禁止关闭 TLS 校验。

`up.sh` 构建组件、执行完整 Alembic 迁移，启动 PostgreSQL、API、MTBF Worker、归档 Worker 和 Web；API 与数据库不发布宿主端口。便捷启动为 `bash scripts/start.sh`，停止为 `bash scripts/stop.sh`。

## 站点与文件配置

通过网页建立真实样品、项目、计划、工位和执行上下文。权限与审批边界见 [数据流程](data-workflow.md)，首次安装不导入现场记录或演示业务数据。

中央归档默认使用项目专用 `archive-data` 卷，API 和归档 Worker 共享 `/var/lib/rp1/archives`。`RP1_ARCHIVE_VOLUME` 可指向事先准备的绝对绑定目录，容器 UID 10001 需有读写权限。原件及派生查询文件放在 Git 工作区之外，不由 Web 静态公开。

站点覆盖配置放在 `compose.override.yml`，由 `scripts/compose.sh` 加载；机器地址、账号和凭据不能写回源码模板。备份脚本不包含 override，须单独受控备份，恢复或升级切换前重新核对其绑定目录与配置。

## 日常检查

```bash
bash scripts/compose.sh ps
python3 scripts/verify.py
bash scripts/compose.sh logs --tail=100 api worker archive-worker web
```

`verify.py` 检查 HTTPS、API、数据库 readiness 和两类 Worker 的进程/连接，不能证明业务结果正确。归档须分别检查上传、解析、核实、归类和审批状态。

## 备份与恢复

`python3 scripts/backup.py --maintenance` 暂停 Web、API 和两类 Worker 的写入，生成 PostgreSQL custom dump、中央归档 `archive.tar.gz`、站点配置与哈希，再恢复原运行服务。默认目录 `backups/UTC时间/` 含私钥，须限制访问并另存受控异机存储。

平台备份覆盖已中央归档文件。尚未提交的工位 CSV、报告和 outbox 需要单独备份。平台维护不停止 HMI 控制；网页离线不证明设备停止。

恢复使用与备份匹配的版本，在另一个全新克隆目录进行；不要先运行 configure，不要复用原项目名或数据卷：

```bash
git clone https://github.com/Roboparty/RP1_test.git rp1-restore
cd rp1-restore/software
python3 scripts/restore.py /安全备份目录 --project rp1-restored --port 9443
python3 scripts/verify.py
```

脚本校验哈希、恢复原站点凭据、创建新数据库卷和角色、保留 ACL，把归档恢复到新项目专用卷，再执行迁移和启动检查。新备份缺少可校验归档快照会被拒绝；旧备份不能恢复当时未集中保存的工位文件。

恢复到新 DNS/IP 时重新签发匹配证书并更新 `PLATFORM_URL`。核对账号、记录数量、原件哈希、权限、审批和业务结果后，再切换客户端。

## 平台升级与证书

固定目标提交或标签，先备份，再在隔离恢复副本验证完整迁移、历史读取与审批。平台、前端、数据库及 Worker 配套升级，不能重新开放旧 ingestion。

正式切换执行 `bash scripts/up.sh`。不可逆业务规则的回退通过已校验备份恢复到新项目，核对切换后的新写入；不要盲目 downgrade 或删除审批历史。

默认服务器证书有效期一年、CA 十年。到期前换发 `server.crt/server.key`，保留正确 SAN，重启 Web 并验证；更换 CA 后同步更新浏览器和工位信任。离线部署需事先准备匹配镜像、依赖和安装包。

## 上位机配置、备份与升级

源码与构建见 [HMI](../host/README.md)、[构建指南](../host/docs/build.md)。默认状态目录 `~/.local/state/rp1-test-hmi/` 保存配置、日志、记录和 SQLite/outbox。使用自定义 `--state-dir` 时，查询、停止和备份必须指向同一目录。

1. 现场确认测试停止、电机失能、PLC 正常总停止的实际反馈，再正常关闭桌面和网关。
2. 执行同一状态目录的 `rp1-test-hmi status`，确认 `running=false`，保留单实例保护。
3. 网关停止后备份完整状态目录及 `/etc/rp1-test-hmi/can-policy.yaml`。正常升级保留 installation-id，克隆到其他工位不能复制该身份。
4. 校验 Releases 安装包 SHA256，再用系统包管理器安装。可用 `-o Dpkg::Options::=--force-confold` 保留修改后的 conffile，安装后仍须核对实际配置。
5. 用 `dpkg-query -W rp1-test-hmi` 查询安装版本，按现场允许的模式启动，核对实际进程启动时间、程序版本和健康状态。

安装不会自动启动设备，也不会更新仍在运行的旧进程。客户端和网关配套更新；不能强杀活动控制进程，或删除锁、用户配置、运行记录来启动。不要让独立 uploader 与网关同时消费同一 outbox。安装成功不能替代真实设备验收。

## 排障入口

| 现象 | 核对内容 |
|---|---|
| HTTPS 不通 | Web、监听地址、证书 SAN/有效期和客户端 CA |
| HMI 登录或心跳失败 | 实际平台地址、首次改密、账号/会话和工位权限；401/403/409 后核对登录 |
| CSV 已上传、曲线未就绪 | 原件完整性/哈希、归档 Worker、共享目录权限和解析错误 |
| 未进入正式查询或统计 | 当前核实/归类版本、审批状态及独立 MTBF 暴露资格 |
| HMI 无法重开 | 同一状态目录的实例状态、端口与正常停止结果 |
| PLC 启动拒绝或反馈待确认 | 实际 HR1、故障来源、反馈新鲜性、锁态及回执，见 [PLC 对接](../host/docs/plc-integration.md) |

日志保留时间、版本、请求号和状态变化，避免公开密码、Cookie、令牌及完整配置。
