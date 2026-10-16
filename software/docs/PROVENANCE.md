# 版本来源

1.0.1 整合了当前 `rp1-reliability-platform` 工作副本的前端/后端/数据库，以及独立 `rp1-factory-hmi/rp1_test_hmi` 工作副本的 PLC 上位机。不是旧 `rp1-test-platform-dashboard` 的简单复制。

来源工作副本包含此前未提交的开发内容；本仓库初始提交作为这次工程快照的版本边界。交付副本额外包含 Ubuntu 24.04 构建修正、独立 Compose/HTTPS 配置、监控功能、发布检查及新机操作文档。

源码和静态测试项目定义进入 Git；安装包作为 Release 附件；真实采集 CSV、执行历史、数据库、密钥、机器配置运行副本和备份不随仓库发布。既有代码的第三方许可保持不变。
