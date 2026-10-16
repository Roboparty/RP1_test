# 软件 0.2.0 更新

本版将当前大屏、PLC上位机和可靠性平台整合至同一软件入口。

- 新增完整React大屏：总控、样品/计划、时长/MTBF、工位、审批、归档和曲线。
- 更新PLC/CAN HMI：Modbus、多窗口共享控制、趋势、故障处理、记录、核实与平台连接。
- 同步中央CSV归档、独立解析Worker、视窗曲线、多执行比较与追加分析。
- 纳入网页历史CSV导入：识别/预览、上下文确认、核实和专用归档。
- 配套数据库0041、HTTPS部署、备份恢复、源码构建与隔离验证。
- 独立保留旧Influx/Grafana演示、轨迹工具、离线分析、硬件和方法目录。

仓库软件版本0.2.0；平台组件2.0.6；HMI源码包2.0.9+ubuntu24.04。
源提交及当前CSV导入增量按 [SOURCE-MANIFEST.json](../SOURCE-MANIFEST.json) 文件SHA固定。
`frontend/backend/database/hmi` 映射至 `software/frontend/service/database/host`，脚本与构建上下文同步适配。

安装见 [软件入口](../README.md)，本地验证见 [整合验证](INTEGRATION-VALIDATION.md)。交付为本地源码提交，安装包由随仓库脚本生成，公开分发使用Releases。
