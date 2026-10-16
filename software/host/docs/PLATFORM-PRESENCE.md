> v2.0.0 使用用户 GUI/网关入口，当前安装与配置见 [LOCAL_CONTROL_BUILD.md](LOCAL_CONTROL_BUILD.md)。本页记录旧版 systemd 安装的接入方法。

# Reliability Platform 工位监控

新版网关在 `reliability_v1` 模式下，使用平台 `TEST_EXECUTOR` 或 `SYSTEM_ADMIN` 账号登录后立即上报，此后每 10 秒上报当前所有测试会话及 PLC 状态。平台“工位监控”每 5 秒刷新；管理员看全部，其他账号只看自己登录的工位。

维护员在 `/etc/rp1-test-hmi/gateway.env` 设置：

```dotenv
RP1_FACTORY_PLATFORM_MODE=reliability_v1
RP1_FACTORY_PLATFORM_URL=http://127.0.0.1:8088
RP1_FACTORY_STATION_NAME=SHN-PLC-01
RP1_FACTORY_BENCH_ID=RD-01
```

该地址是当前同机 Reliability Platform。分机使用实际平台服务器地址，正式部署使用可信 HTTPS；旧平台 8080 地址不能接入当前 8088 平台。状态监控不需要 service key 或旧平台 machine token；密码通过 HMI 输入，不写入配置。首次改密须先在平台网页完成。

安装新版包并修改配置后，在设备完成测试且允许维护时执行 `sudo systemctl restart rp1-test-gateway.service`，重新打开桌面并登录账号。已安装的旧二进制不会因源码改变而自动升级。

## 当前电脑升级到 1.0.1

2026-10-08 已完成本机升级：`0.1.0+plc4` → `1.0.1+ubuntu24.04`，网关服务正常，平台地址为 `http://127.0.0.1:8088`。旧程序、配置和本地数据备份位于 `/var/backups/rp1-test-hmi/20261008-122622-presence/`。本机无需重复执行下面的安装步骤；下列命令供其他工位/后续维护参考。

新版桌面显示“监控在线”或连接异常提示，并提供“退出平台”按钮。登录监控后，测试上下文仍可选择“未选择（仅本地保存）”，正常开始本地测试，无须配置用于数据提交的档案。若明确选择了上传档案，则仍校验其完整性。

本次生成 Ubuntu 24.04 amd64 安装包 `rp1-test-hmi_1.0.1+ubuntu24.04_amd64.deb`。**安装过程会自动重启网关，须在测试结束后执行。** 在安装包所在目录运行：

```bash
sha256sum -c rp1-test-hmi_1.0.1+ubuntu24.04_amd64.deb.sha256
sudo apt install -o Dpkg::Options::=--force-confold ./rp1-test-hmi_1.0.1+ubuntu24.04_amd64.deb
sudo cp -a /etc/rp1-test-hmi/gateway.env /etc/rp1-test-hmi/gateway.env.before-presence
sudo sed -i \
  -e 's|^RP1_FACTORY_PLATFORM_URL=.*|RP1_FACTORY_PLATFORM_URL=http://127.0.0.1:8088|' \
  -e 's|^RP1_FACTORY_PLATFORM_MODE=.*|RP1_FACTORY_PLATFORM_MODE=reliability_v1|' \
  /etc/rp1-test-hmi/gateway.env
sudo systemctl restart rp1-test-gateway.service
dpkg-query -W rp1-test-hmi
systemctl status rp1-test-gateway.service --no-pager
```

`--force-confold` 保留这台电脑已有的 PLC、CAN 与工位配置；后两项替换只修正平台地址与协议。该地址适用于当前同机部署，分机/正式 HTTPS 安装必须使用自己的平台地址。操作完毕重新打开 HMI 并登录账号，在网页 `http://localhost:8088/?view=stations` 核对账号、工位与心跳。平台前后端已升级但 HMI 包未安装时，旧上位机不会自动出现。

工位安装标识位于 `<data-root>/sync/installation-id`，正常升级保留，新工位不能复制该文件。多模块状态由网关集中上报，关闭某个桌面页面不会停止网关心跳。退出账号立即通知离线；断网超过 45 秒、登录会话过期/撤销、账号停用也显示离线。控制器采样超过 20 秒未更新则为“状态待确认”。离线只表示无法确认当前状态，不表示设备停止；本机平台默认账号会话 8 小时，过期后重新登录。

心跳包含工位/台架、测试单号、样品号、测试对象、运行状态、累计运行秒数、循环数、PLC 通信/供电状态。字段采用白名单，不包含 CSV、CSV 内容、轨迹、报告、路径或密码。平台根据登录会话确定操作员身份，不信任客户端传入账号名。PLC 仅供电会显示“电柜已供电”，不会当成测试运行；mock 明确标记为模拟。

实现文件：`factory_hmi/sync/presence.py` 的 `HmiPresencePublisher/build_presence_snapshot`，`factory_hmi/sync/reliability_platform.py` 的登录会话客户端，`factory_hmi/gateway/app.py` 的全会话采样提供器。平台须同时部署 `20261008_0024` 迁移、新 presence API 和“工位监控”页。

本功能只读当前状态，不控制硬件、不写入正式测试统计，不实施测试数据审批。旧数据提交链路仍须按独立审批方案改造，不能把在线监控当成审批完成。
