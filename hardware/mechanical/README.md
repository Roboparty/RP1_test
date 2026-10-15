# 机械台架

[硬件资料](../README.md) · [项目首页](../../README_CN.md)

三个台架按“腿部”和“手臂胸腔”两类组织。

| 台架 | 资料入口 | 用途 |
| --- | --- | --- |
| 手臂测试平台 | [手臂工装](手臂胸腔/手臂测试平台/README.md) | 手臂总成固定与接口集成 |
| 胸腔测试平台 | [胸腔结构](手臂胸腔/胸腔测试平台/README.md) | 胸腔总成固定与接口集成 |
| 腿部负载测试平台 | [腿部平台](腿部/腿部负载测试平台/README.md) | 腿部固定与加载机构集成 |

手臂与胸腔的通用加工资料和采购表见 [共用资料](手臂胸腔/共用资料/README.md)。STEP 模型与 PDF 加工图按料号对应，采购时核对零件号和修订。

## 总装模型

下载 [RP1_Test_Parasolid_CAD.zip](https://github.com/Roboparty/rp1_test/releases/download/v0.1.0/RP1_Test_Parasolid_CAD.zip)，解压后使用支持 Parasolid 的 CAD 软件打开对应 `.x_t` 文件。

| 总装 | 文件 |
| --- | --- |
| 腿部老化测试平台 | `腿部老化测试平台总装.x_t` |
| 胸部支架 | `胸部支架总装.x_t` |
| 手部腿部支架 | `手部腿部支架总装.x_t` |

压缩包提供总装几何模型；零件加工图和 BOM 见上方各台架资料入口。

## 获取资料

下载[源码 ZIP](https://github.com/Roboparty/rp1_test/archive/refs/heads/main.zip)，或运行 `git clone --depth 1 https://github.com/Roboparty/rp1_test.git`。

台架适配需核对安装孔、总质量、重心、反力矩和运动包络；允许载荷由结构计算与适用工况确定。柜端与总成连接见 [机电集成](../docs/integration.md)。
