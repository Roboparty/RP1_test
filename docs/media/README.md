# README 展示素材

`showcase/` 保存首页使用的台架渲染、软件截图和原视频动图节选。中英文首页共享同一套文件。

| 文件 | 来源素材 | 用途 |
|---|---|---|
| `arm-rig-quick-release.jpg` | 台架渲染2+快拆结构.jpg | 手臂台架与快拆结构 |
| `torso-rig.jpg` | 胸腔台架渲染.jpg | 胸腔台架 |
| `leg-load-rig.jpg` | 腿部台架渲染.jpg | 腿部负载台架 |
| `arm-dashboard.jpg` | 手臂测试数据大屏.jpg | 单臂模块总控 |
| `lower-body-dashboard.jpg` | 下肢测试数据大屏.jpg | 下肢模块总控 |
| `plc-control.jpg`、`aging-workspace.jpg`、`motor-monitor.jpg` | 上位机1/2/3.jpg | PLC、老化测试和电机监控 |
| `arm-load-2_5kg.gif`、`arm-load-3_5kg.gif`、`arm-load-5_5kg.gif` | 对应单臂负载.mp4 | 三组负载演示 |
| `leg-load-50kg-stepping.gif`、`leg-load-50kg-rise.gif` | 50kg负载踏步/顿起.mp4 | 下肢负载演示 |
| `upper-body-dynamic-front.gif`、`upper-body-dynamic-side.gif` | 上肢3ms跑步测试视频/视频2.mp4 | 上肢动态工况两视角 |

JPEG为原图副本。GIF为6秒原速节选，宽300px、8fps，保持画面比例和源视频显示方向，不包含音频。源文件名中的负载用于对应展示素材，不额外解释运动速度。原视频不包含在源码仓库中。

`showcase/manifest.json` 记录源文件名、大小、SHA256、动图区间及输出哈希，供核对已提交的展示素材。

`factory-hmi.jpg`、`test-dashboard-*.jpg` 与 `arm-test.gif/leg-test.gif` 按现有路径使用。素材适用范围见 [LICENSES](../../LICENSES.md) 和 [THIRD_PARTY](../../THIRD_PARTY.md)。
