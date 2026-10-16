# 运维与恢复

## 日常检查

在 `software/` 目录执行 `./scripts/compose.sh ps`、`python3 scripts/verify.py`。日志用 `./scripts/compose.sh logs --tail=100 api worker archive-worker web`。不要共享原始凭据或完整 `.env`。

`verify.py` 检查 HTTPS、API、PostgreSQL readiness、MTBF 与归档 worker 进程及数据库连接；它不证明重算业务结果正确或真实硬件已验收。业务验收还需检查应用结果与队列故障。

## 平台备份

`python3 scripts/backup.py --maintenance` 暂停 Web/API/MTBF Worker/归档 Worker 写入，创建 PostgreSQL custom dump、中央归档 `archive.tar.gz`、站点配置和校验文件，再恢复原来运行的服务。备份目录默认 `backups/UTC时间/`，包含私钥，限制访问并另存受控异机存储。此命令不停止 HMI 控制；平台维护期间监控可能显示离线。

## 恢复演练

先保留原部署，在另一个全新克隆目录中恢复。不要先执行 configure.py，不要复用已有项目名或数据卷：

```bash
git clone https://github.com/Roboparty/RP1_test.git rp1-restore
cd rp1-restore/software
# 使用与备份匹配的软件版本
python3 scripts/restore.py /安全备份目录 --project rp1-restored --port 9443
python3 scripts/verify.py
```

restore.py 校验文件哈希、恢复原站点凭据、创建新 PostgreSQL 卷和数据库角色，保留数据库 ACL，恢复中央归档到全新项目专用卷，再执行迁移和启动检查。恢复不会复用原站点 NAS/绑定目录；新备份缺少经校验的归档快照会被拒绝。旧备份无法恢复当时尚未集中保存的工位 CSV。恢复到新 DNS/IP 时，重新签发匹配地址的证书并更新 PLATFORM_URL；仅改端口不影响证书 SAN。确认账户、记录数量、权限和业务结果后，才切换客户端地址。恢复演练不能使用正式项目名。

## 上位机备份和升级

停止测试后，备份用户状态目录 `~/.local/state/rp1-test-hmi/` 和 `/etc/rp1-test-hmi/can-policy.yaml`；复制 SQLite/outbox 前停止网关以确保一致性。平台备份覆盖已中央归档的 CSV；仅工位本地、尚未提交的 CSV 和队列仍须单独保留。正常升级保留 installation-id；克隆给另一台工位时不要复制该身份文件。本版安装不会自动启动设备；升级前正常停止测试并关闭自己的HMI/网关，升级后由用户启动。

`--force-confold` 保留已修改的 conffile；未修改的默认文件可能由 dpkg 更新，因此仍须备份并核对 PLC/CAN/上传配置。不要自动启用独立 uploader 与网关同时处理同一 outbox。

## 证书与升级

默认服务器证书有效期 365 天，CA 10 年；到期前换发 server.crt/server.key，保留正确 SAN，重启 web 并验证。CA 变更时同步更新各浏览器与工位信任。不要关闭 TLS 校验。

升级平台前备份，固定目标 tag，执行 `./scripts/up.sh`。迁移包含不可逆业务规则变更，回退应恢复到新项目中，不盲目运行 downgrade。离线环境需要预先准备固定版本 Docker 镜像和 HMI deb；源码构建依赖缓存也需提前准备，本版不提供完整离线介质。
