# RP1 v2.0.0

包含 Reliability Platform 前端、后端、数据库迁移、MTBF Worker，以及 RP1 上位机完整源码。
安装包由本次源码构建，目标 Ubuntu 24.04 amd64。

## 更新

- PLC 使用 Modbus TCP 实机控制配置，提供总控、通道控制、PS1 电压/电流设定、故障复位和 AllStop；保留 monitor 只读模式与原联锁。
- YAML 模块引用、预览和生效状态使用相对路径，保留配置目录边界检查。
- 修复 PS1 电压、电流输入被轮询覆盖的问题，应用前在界面线程提交并捕获输入值。
- 支持经管理员认证的物理 SocketCAN 配置，桌面入口统一为 `rp1-test-hmi`。
- 修复电机 SDK 的 spdlog/fmt ABI 依赖，增加打包前原生加载检查。
- 修复多模块并行时原生标零调用占用 Python GIL，避免阻塞其它模块的控制与刷新线程。
- 全局故障显示实际异常和最高严重度诊断，优先展示关键通信故障。
- 平台增加样品、项目、计划和台架建档，改进总控响应式布局及用例轮播。
- 历史迁移保留来源标记，修复历史 RUNNING 记录被当作当前实时运行的问题。

## 下载与安装

下载 `rp1-test-hmi_2.0.0+ubuntu24.04_amd64.deb` 和同名 `.sha256`：

```bash
sha256sum -c rp1-test-hmi_2.0.0+ubuntu24.04_amd64.deb.sha256
sudo apt install -o Dpkg::Options::=--force-confold ./rp1-test-hmi_2.0.0+ubuntu24.04_amd64.deb
```

安装前正常停止现场测试并关闭自己的上位机。安装不会自动启动设备；首次启动后核对本站 PLC 参数和配置。
源码可从本标签的 Source code 下载，构建与部署方法见仓库 README。
本地运行数据、采集 CSV、账号凭据、私钥和数据库备份不包含在源码或安装包中。
