# 机械台架

[硬件资料](../README.md) · [项目首页](../../README_CN.md)

三个台架按“腿部”和“手臂胸腔”两类组织。

| 台架 | 资料入口 | 用途 |
| --- | --- | --- |
| 手臂测试平台 | [手臂工装](手臂胸腔/手臂测试平台/README.md) | 手臂总成固定与接口集成 |
| 胸腔测试平台 | [胸腔结构](手臂胸腔/胸腔测试平台/README.md) | 胸腔总成固定与接口集成 |
| 腿部负载测试平台 | [腿部平台](腿部/腿部负载测试平台/README.md) | 腿部固定与加载机构集成 |

手臂与胸腔使用 [共用总装和配套资料](手臂胸腔/共用资料/README.md)。打开装配时保持 CAD 文件及依赖资源的相对位置。

## 下载 CAD

原生 SolidWorks 工程及依赖资源通过 [Releases](https://github.com/Roboparty/rp1_test/releases/tag/v0.1.0) 下载，加工图和 BOM 保留在仓库中。

只需当前资料时，可下载[源码 ZIP](https://github.com/Roboparty/rp1_test/archive/refs/heads/main.zip)，或用 `git clone --depth 1 https://github.com/Roboparty/rp1_test.git`。已有 Git 历史仍保留原 CAD 文件，完整克隆会包含这部分体积。

| 附件 | 总装入口（解压后） |
| --- | --- |
| [RP1_Test_Arm_Torso_CAD.zip](https://github.com/Roboparty/rp1_test/releases/download/v0.1.0/RP1_Test_Arm_Torso_CAD.zip) | `hardware/mechanical/手臂胸腔/共用资料/cad/solidworks/手部腿部胸部支架总装.SLDASM` |
| [RP1_Test_Leg_CAD.zip](https://github.com/Roboparty/rp1_test/releases/download/v0.1.0/RP1_Test_Leg_CAD.zip) | `hardware/mechanical/腿部/腿部负载测试平台/cad/solidworks/腿部老化测试平台总装.SLDASM` |

在仓库根目录解压所需附件即可，仓库文件夹可以任意命名。保留包内路径，零件、材质和场景资源随总装一起使用。附件提供适用许可及第三方范围说明。

台架适配需核对安装孔、总质量、重心、反力矩和运动包络；允许载荷由结构计算与适用工况确定。柜端与总成连接见 [机电集成](../docs/integration.md)。
