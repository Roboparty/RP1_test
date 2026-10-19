# Actions 平台测试数据库隔离

日期：2026-10-19。适用工作流：`.github/workflows/platform.yml`。

## 原故障与证据

`main` 提交 `d142aff0622fc00153363b87d0b4227e3a805b99` 的
[delivery 运行](https://github.com/Roboparty/RP1_test/actions/runs/38863303337)
在平台集成测试中得到 30 passed、1 failed。失败点为
`software/service/tests/test_setup_integration.py:37`：初始首页资产数与活动执行数应为零。

工作流先运行 `test_hmi_presence_integration.py`，再运行 setup 测试。
presence 文件中的 `test_only_a_complete_running_heartbeat_opens_one_official_execution`
会通过 API 提交模拟资产、计划及执行记录。两者共用 `rp1_presence_ci`，
`environment()` 结束时的 `engine.dispose()` 只关闭连接，不删除已提交的记录。
随后 setup 测试以管理员查询整个首页，不能继续假定业务数据库为空。

本地 `software/scripts/test-setup.py`、`test-approval.py` 和 `test-archives.py`
原本先跑 setup，再跑 presence。这解释了为什么本地通过与 CI 失败可以同时出现。
日志已确认失败，以上调用顺序与持久化行为由代码核对；修复前尚未在本机再次复现。

这组断言只由 pytest 执行，不属于平台的日常启动或查询流程。此次故障不能作为现场平台故障的证据。

## 修改后的安排

`platform` 改为两个独立的 matrix job，各自启动 PostgreSQL 16 服务容器，
分别创建应用角色、运行全部数据库迁移并在结束时由 GitHub 清理服务容器：

| Job | 临时数据库 | 检查范围 |
| --- | --- | --- |
| setup | `rp1_presence_setup_ci` | 仅全新平台 setup/权限/事务回滚集成测试 |
| lifecycle | `rp1_presence_lifecycle_ci` | presence、提交、归档集成测试，以及原有后端单测、部署守卫、前端与发布文件检查 |

每组的 PostgreSQL 初始化库名、健康检查、应用连接、管理员连接和迁移连接都引用
同一个 `matrix.database`。两组拥有不同数据库及独立服务容器，不依靠先后顺序。
`fail-fast: false` 保证一组失败时另一组继续收集结果；每组超时上限为 15 分钟。
前端与发布检查只执行一次。HMI 构建及容器验收 job 保持原有行为。

保留所有原始集成测试和空库断言。原有 31 项分为 setup 1 项与 lifecycle 30 项，
没有删除、跳过或放宽失败用例。其余测试内部仍使用本身的 fixture；本次隔离边界为两组 job，
不宣称每条测试函数均拥有独立数据库。

## 验证要求与边界

1. 对照旧工作流确认新旧集成测试文件集合一致，setup 只分配到自己的 job。
2. 检查全部数据库引用与各组角色初始化、迁移一致，工作流 YAML 和发布文件检查通过。
3. 在修复提交上重新运行 GitHub Actions，核对 setup、lifecycle、HMI 和原 Tests 的实际结果。
4. 两个 job 可并行、顺序不确定；各自的空数据库由独立容器保证。无需将 setup 挪到其他测试前面。

只使用临时数据库、模拟数据及 mock/offscreen HMI。没有读取站点 `.env`、
没有连接运行平台数据库、没有安装或重启上位机、没有启动真实 PLC/CAN 测试。
这次修改不包含应用功能、数据库迁移、审批规则或运行数据变更。
