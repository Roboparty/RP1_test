# 上位机在线与测试运行监控

适用源码：本 `rp1-reliability-platform` 前后端，以及独立 `rp1-factory-hmi/rp1_test_hmi` 的 RP1 TEST HMI（PLC）。入口：平台导航 **工位监控**，或 `/?view=stations`。

## 功能与数据边界

上位机使用平台账号登录成功后立即发送状态，此后由网关独立线程每 10 秒发送一次。无须打开特定桌面页面，也不依赖测试记录上传队列。网页前台每 5 秒读取一次，通常在测试状态改变后的 15 秒内更新。

每个工位显示工位名称、台架编号、实际登录账号、最后心跳、PLC 通信与电柜状态；每个并行测试会话显示测试单号、样品号、测试对象、运行/暂停/故障状态、实际累计运行秒数与已完成循环。PLC 模拟驱动明确标为“模拟 PLC”。仅电柜供电时显示“电柜已供电”，不认定为测试运行中。

- 管理员 `SYSTEM_ADMIN` 可以查看全部工位；普通账号只看当前由自己登录的工位。
- `TEST_EXECUTOR` 或 `SYSTEM_ADMIN` 可以在上位机登录并上报；`VIEWER` 无上报权限。
- 网关退出账号时立即关闭连接并撤销其登录会话；网页下一次刷新即可看到离线。桌面窗口关闭不等同于账号退出，网关服务仍可继续上报。
- 断网/进程退出后，45 秒无心跳则离线。登录会话过期、被撤销或账号停用也会离线。本机默认登录有效期为 8 小时；过期后须重新登录上位机。
- 控制器采样超过 20 秒未更新时显示“状态待确认”。离线时仍显示最后快照，但明确标记历史状态；暂停自动刷新时也不声称显示实时状态。
- 页面超过 15 秒未成功取得新响应，即使浏览器请求仍挂起，也切换为历史快照。并行测试中某会话故障、另一会话仍运行时，保留故障提示，同时仍纳入“测试运行中”筛选和台数。
- **离线不等于硬件已停止。** 监控只读取状态，不提供远程电机/PLC 控制。

仅传输上述有限结构化状态；原始 CSV、CSV 内容、文件路径、轨迹、报告和执行包均不经此功能上传。实时运行时长不写入正式测试、统计或 MTBF 表。本功能**不包含测试数据审批**；既有 ingestion/报告提交链路也未被本功能改成审批链路。审批流程仍需单独实施，不能把登录成功或在线显示当成测试数据获批。

## 接入步骤

1. 平台部署本版 API/前端，并执行数据库迁移 `20261008_0024`。
2. 工位安装包含 `factory_hmi/sync/presence.py` 的新版网关；只刷新网页不能升级旧的已安装上位机。
3. 维护员编辑 `/etc/rp1-test-hmi/gateway.env`（现有 PLC/CAN 配置保持原值）：

```dotenv
RP1_FACTORY_PLATFORM_MODE=reliability_v1
RP1_FACTORY_PLATFORM_URL=http://127.0.0.1:8088
RP1_FACTORY_STATION_NAME=SHN-PLC-01
RP1_FACTORY_BENCH_ID=RD-01
```

这里的 `127.0.0.1:8088` 适用于当前同机开发部署。分机时改成平台服务器的实际地址，不能填写 PLC 地址或工位自己的 localhost。正式部署使用可信 HTTPS，并在工位系统中安装对应 CA，不能关闭证书校验。**旧配置的 8080 端口不是当前 Reliability Platform 的 8088 端口。**

本监控使用账号会话与 CSRF，不要求 service API key 或旧平台 machine token。缺少服务密钥不影响在线登录；既有测试记录提交可能仍要求该密钥。不要把账号密码写入环境文件；登录 Cookie 仅保存在网关内存中。

4. 完成现场测试并使设备处于可维护状态后，重启网关以加载程序和配置：

```bash
sudo systemctl restart rp1-test-gateway.service
systemctl status rp1-test-gateway.service --no-pager
```

5. 在网页用管理员创建/启用执行员账号，执行员先完成首次登录改密，再在 HMI 的平台登录区域输入账号密码。
   HMI 显示“监控在线”，并提供“退出平台”按钮。只需在线监控时，测试上下文选择“未选择（仅本地保存）”即可开始本地测试；无需为了监控填写上传档案。显式选中的上传档案仍执行原有完整性校验。
6. 在平台打开“工位监控”。管理员看到全部上位机；执行员看到自己登录的上位机。工位身份保存在 `/var/lib/rp1-test-hmi/sync/installation-id`，升级保留；克隆工位镜像前不要复制这个运行数据文件，否则两台机器会被识别成同一工位。

## 文件、实现与连接方式

| 文件 | 新增/修改及关键实现 |
|---|---|
| `database/alembic/versions/20261008_0024_hmi_presence.py`、`database/sql/024_hmi_presence.sql` | 新增迁移与 `integration.hmi_presence`。绑定真实用户和登录会话；仅保留每次登录的最新快照，以递增序号拒绝旧状态覆盖。写操作经权限受限的数据库函数执行，读取仅本人/管理员可见。 |
| `service/app/schemas/hmi_presence.py` | 新增有限字段契约与大小/类型校验；拒绝原始 CSV 等额外字段。 |
| `service/app/repositories/hmi_presence.py` | 新增 `station_view` 状态判定及数据库调用；优先处理离线、采样过期、故障与测试运行，区分 PLC 供电。 |
| `service/app/api/routes/hmi_presence.py`、`service/app/main.py` | 新增并注册 PUT/DELETE `/api/v1/host/presence/{connection_id}` 与 GET `/api/v1/host/stations`；使用现有 Cookie、CSRF 和账号权限。列表采用游标分页。 |
| `frontend/src/hmiPresence.ts`、`frontend/src/pages/HmiStationsPage.tsx` | 新增类型、接口封装和 `HmiStationsPage`/`HmiStationCard`；定时读取、筛选、暂停、加载更多及历史快照提示。 |
| `frontend/src/App.tsx`、`navigation.ts`、`styles.css` | 修改主导航、深链和样式；增加“工位监控”，支持手机宽度和昼夜主题。 |
| HMI `factory_hmi/sync/presence.py` | 新增 `HmiPresencePublisher`、`build_presence_snapshot`；持久安装标识、每次登录独立连接、字段白名单、10 秒心跳与离线通知。 |
| HMI `factory_hmi/sync/reliability_platform.py` | 修改 `ReliabilityPlatformClient` 与 `ReliabilityUploaderWorker.authenticate/clear_credentials`；保留内存 Cookie、附带 CSRF、登录后启动心跳，退出撤销登录。 |
| HMI `factory_hmi/gateway/app.py` | 修改 `GatewayHub.presence_snapshot` 与 `PlatformSyncCoordinator`；从全部控制会话和 PLC 收集快照，独立于网页/桌面页面轮询。 |
| HMI `factory_hmi/desktop/app.py` | 修改 `_render_platform_presence/_platform_logout/_start_playback`：显示网关真实连接状态、提供退出入口，并允许登录监控时执行未选择上传档案的本地测试。 |

每次登录生成新连接 UUID；同一安装标识取最新登录，不允许旧登录的延迟心跳重新占据当前工位。重复/乱序心跳不回退状态，不刷新旧序号的在线时间。断网时不积压历史心跳，恢复后直接发当前快照；401/403/409 会终止该登录的心跳，需要重新登录。

## 验收与排查

2026-10-08 当前电脑的平台 API、前端与数据库迁移已经部署到 `http://localhost:8088`。升级前数据库备份为 `backups/presence-preupgrade-20261008-120810.dump`。同日本机 HMI 已从 `0.1.0+plc4` 升级至 `1.0.1+ubuntu24.04`，网关健康检查通过，平台地址已由 8080 改为 8088。旧程序、配置及本地数据备份在 `/var/backups/rp1-test-hmi/20261008-122622-presence/`，其中 `upgrade-result.json` 记录验收结果；19 份原有配置已校验保留（网关环境文件仅更新平台地址/协议）。没有启动真实硬件测试。其他工位的安装命令见 HMI 的 `docs/PLATFORM-PRESENCE.md`。

已经执行的隔离验证包括：真实 PostgreSQL 迁移、真实 Cookie/CSRF HTTP 登录、管理员/本人/其他账号权限、账号停用、会话撤销、重复/乱序心跳、45 秒超时、新登录覆盖旧登录，以及运行→暂停→仅供电→退出离线。浏览器验证 1440px/375px 布局和深色主题。模拟数据全部使用独立 QA 数据库，未写入当前平台的正式业务记录；这不代表真实 PLC/CAN 硬件已做联调。

自动化文件：`service/tests/test_hmi_presence.py`、`service/tests/test_hmi_presence_integration.py`、`frontend/src/pages/HmiStationsPage.test.tsx`、HMI `tests/test_hmi_presence.py`。数据库集成测试必须显式提供隔离 `HMI_TEST_ADMIN_URL`，库名须以 `rp1_presence` 开头，禁止指向业务库。

额外执行 HMI `tests/test_factory_hmi_responsive.py`，验证已登录的本地测试不被上传档案要求阻断、不完整上传档案仍被阻断、连接状态与退出控件跟随网关实际状态。Ubuntu 24.04 容器中安装新版 deb、以非 root 账号启动网关和 Qt xcb 桌面，并通过包内网关验证真实账号登录、独立心跳及退出离线；没有连接真实 PLC/CAN。

无工位记录时依次核对：网关是否升级、地址是否指向本平台、执行员是否完成改密、是否已在 HMI 登录、网页账号是否有可见范围。最后心跳不更新时查看网关日志 `journalctl -u rp1-test-gateway.service -n 100 --no-pager`。请勿分享包含密码、Cookie 或服务密钥的日志/配置。
