# 软件 0.2.0 本地整合验证

日期：2026-10-16。对象为桌面 RP1_test 的本地软件整合分支。输入源码由
`SOURCE-MANIFEST.json` 固定，目标源码清单见 `INTEGRATION-MANIFEST.json`。

## 本次变更

- 新增 `software/frontend/`，同步当前大屏、曲线及网页历史CSV导入。
- 更新 `software/service/`、`database/`、`host/`，配套当前API、0041迁移、PLC HMI、核实归档与在线监控。
- 适配 `compose.yaml`、启动/备份/恢复、HMI构建、隔离验收和CI路径；保留站点override加载。
- 原遥测服务、数据库与脚本移至 `software/examples/telemetry-demo/`；轨迹工具、离线分析、硬件和方法目录保留。
- 历史执行CSV的数据库单测使用临时合成数据，不读取或携带工位历史文件。

## 已执行

| 验证 | 结果 |
|---|---|
| 前端 `npm run check` | lint、类型检查、21文件/132测试、生产构建通过 |
| 后端非集成 | 100 passed；37项数据库用例由非集成筛选排除，未计入通过数 |
| 新建隔离PostgreSQL | 完整迁移至 `20261016_0041`；31项选定API/归档/审批/presence/历史导入集成通过 |
| HMI mock/offscreen回归 | 477 passed、119 subtests passed |
| 数据库导入/目录单测 | 16项通过，全部使用静态目录或临时合成数据 |
| 部署守卫 | 6项通过，无Docker动作；新增compose helper有/无override两分支通过 |
| HTTPS完整部署 | 新临时项目/数据库/文件卷，五服务healthy、TLS/CA/主机名校验、真实up/verify入口通过 |
| 跨端归档 | mock采集、本地核实、分块上传、独立Worker解析、自审、OTHER归类/驳回重提及查看者查询通过 |
| 恢复 | 上传丢回执、API/Worker重启、真实backup/restore到另一新项目通过；原件SHA/大小/权限及查询核对通过 |
| HMI候选包 | 从整合后的host源码生成DEB；原生SDK加载和源码来源校验通过，复用匹配运行库，未重新编译SDK |
| 安装后HMI | Ubuntu24.04容器重新安装候选包，无外网、非root运行mock control/monitor和Qt offscreen界面通过 |
| 原独立工具 | 离线分析8、遥测脚本7、Influx合同5项通过；Grafana静态合同通过 |
| 文件/目录 | 发布文件检查、工作流YAML、Shell/Python语法、入口链接和Git空白检查通过 |

候选DEB版本 `2.0.9+ubuntu24.04`，100,682,756字节，SHA256：
`84949fe6639904c52d855c86a173527090d733c5e039b962caecad7acd2653ce`。
安装包保存在仓库外的临时产物目录，不进入Git。

前端构建保留约630kB曲线chunk提示；Python测试保留Starlette/httpx弃用提示，相关命令均成功。

## 范围

本轮只在独立测试项目、合成数据、mock设备和容器内验证；没有更新运行站点或操作真实PLC/CAN。
未重新运行旧遥测演示全栈、现场设备及容量测试。安装验证使用已备好系统依赖的Ubuntu容器，实际GUI模式为offscreen；CI安装镜像同时配置了Xvfb路径。

本地提交包含源码、静态资源、依赖、文档和清单，不含 `.env`、私钥、运行CSV、备份、虚拟环境、构建目录或DEB。未推送GitHub或发布Releases。
