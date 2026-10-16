# 第三方资料与依赖

本项目的许可仅覆盖贡献者有权授权的内容。下表记录主要外部组件和参考资料，供使用、集成与分发时查阅；不是完整依赖清单或逐文件权属证明。

## 硬件参考资料

| 资料与位置 | 来源及许可范围 |
| --- | --- |
| 原始台架工程中以规格或标准件命名的参考模型，例如共用 CAD 中的 `ABC03-5010-80B-40_001.sldprt`、`H10B-TG-4B-PG16（新）.SLDPRT`、`把手C-NUHFNS66.SLDPRT`，腿部 CAD 中的 `C-HLGW30-1400_r.sldprt`、`HGW30钳制器.SLDPRT`、轴承及紧固件模型 | 原始下载位置、版本和再分发许可尚未逐项确认，不能按文件名推定供应商或许可，也不纳入自有台架设计的 CERN 授权。 |
| 腿部工程中的 `RP1.3下肢-测试（右腿）.SLDASM` 及其机器人参考部件 | 供台架设计与装配参考；未逐件确认参考部件权属。仅项目自有台架设计部分受本项目硬件许可覆盖。 |
| CAD 工程附带的 `.p2m`、`.p2s`、`.hdr`、`.dds`、`softbox.png`、`denim1.jpg`、`grid10.jpg` 等外观和纹理文件 | 原始素材来源及再分发许可未记录，保留各自权利；不纳入软件、硬件或文档的统一授权。 |
| `rp-logo-blue（无底图）.png`、HMI `robo_party_logo.png` 与 `rp1-factory-hmi.png` 等品牌图 | 品牌素材单独保留权利；本项目许可不授予 RoboParty 商标使用权。 |
| 采购工作簿与 [system-bom.csv](hardware/electrical/system-bom.csv) | 自有 BOM 数据按硬件许可；工作簿内第三方模板、图片及无关供应商/客户资料不随之重新许可。系统 BOM 标注 `LFD835 V1.9 四通道电气参考配置`，相应外部源资料的授权范围仍需单独确认。元件型号只用于采购参考，不授予厂家模型、图纸或手册的权利。 |

取得对应来源、不可变版本、许可全文或明确再分发授权后，才能据此判断上述未确认材料的可分发范围；给仓库增加 LICENSE 不能替代这些记录。

## 外部设备 SDK 与机器人模型

| 组件 | 当前接入方式与许可记录 |
| --- | --- |
| `motors_py` 电机 SDK 与 `rp1_description` 机器人模型 | [执行工具依赖说明](software/test-runner/docs/dependencies.md)通过 `RP1_MOTORS_SDK_ROOT` / `RP1_DESCRIPTION_ROOT` 指定外部匹配版本；新版HMI使用 `software/host/motors/` 内保留GPL-3.0通知的SDK源码构建；test-runner仍支持外部匹配版本。仓库不附SDK二进制。准确发布版本、下载地址、许可全文及二进制再分发权限需由设备提供方补充，不推定为 Apache 或 CERN。 |
| ZLGCANFD 厂家驱动与 SDK | [上位机资料](software/host/README.md)中的设备接入依赖外部厂家文件；具体设备所需 SDK 版本、下载地址及再分发条件需由设备提供方提供。厂家的驱动、共享库和固件不纳入本项目软件许可。 |

将外部扩展放入 PyInstaller 目录、DEB 或其他安装包前，应记录实际版本与原许可，并满足其分发条件。仅在源码中提供加载接口不构成该 SDK 的授权。

## 主要软件依赖

版本依据仓库中的依赖与约束文件；安装环境及传递依赖以实际解析结果为准。以下来源链接用于查阅上游条款，不替代发行包应携带的许可证和通知。

| 组件 | 仓库使用记录 | 上游许可与来源 |
| --- | --- | --- |
| FastAPI | host/service 锁文件 `0.142.4` | MIT — [项目许可](https://github.com/fastapi/fastapi/blob/master/LICENSE) |
| NumPy | host 锁文件 `2.5.3`；test-runner `>=1.23` | BSD-3-Clause — [2.2.6 许可](https://github.com/numpy/numpy/blob/v2.2.6/LICENSE.txt)；二进制包附带的 BLAS 等组件另有通知。 |
| PyYAML | host 约束 `6.0.3`；test-runner `>=6.0` | MIT — [6.0.3 许可](https://github.com/yaml/pyyaml/blob/6.0.3/LICENSE) |
| pyroute2 | host 约束 `0.9.6` | `GPL-2.0-or-later OR Apache-2.0` — [0.9.6 许可](https://github.com/svinota/pyroute2/blob/0.9.6/LICENSE)，可按其 Apache 选项使用。 |
| PySide6 / Qt | host 可选桌面依赖；约束 `6.11.2` | LGPLv3、GPLv3 或商业许可，取决于实际模块和授权方式 — [Qt for Python 官方说明](https://doc.qt.io/qtforpython-6/)。打包时按所选条款处理通知、对应源码及替换或重新链接要求。 |
| PyInstaller | host 构建锁文件 `6.22.3` | GPL-2.0-or-later，带 bootloader 分发例外 — [6.22.2 COPYING](https://github.com/pyinstaller/pyinstaller/blob/v6.22.2/COPYING.txt)。例外不覆盖其他捆绑依赖或设备 SDK。 |
| MuJoCo | test-runner 可选仿真依赖 `>=3.0` | Apache-2.0 — [3.0.0 许可](https://github.com/google-deepmind/mujoco/blob/3.0.0/LICENSE)；外部机器人模型另按其许可。 |
| Psycopg | service / database `3.2.10`，含 binary 选项 | LGPL-3.0 — [3.2.10 许可](https://github.com/psycopg/psycopg/blob/3.2.10/LICENSE.txt)；[binary 安装说明](https://www.psycopg.org/psycopg3/docs/basic/install.html#binary-installation)列明捆绑客户端库，应保留其适用通知。 |
| PostgreSQL | Compose `postgres:16-alpine` | [PostgreSQL License](https://www.postgresql.org/about/licence/)；容器内其他组件各按原许可。 |
| InfluxDB OSS | Compose 默认 `influxdb:2.9.0` | MIT — [2.9.0 许可](https://github.com/influxdata/influxdb/blob/v2.9.0/LICENSE)；镜像及捆绑组件另核实。 |
| Grafana OSS | Compose 默认 `grafana/grafana:13.2.3` | 主要为 AGPL-3.0-only，部分目录有独立条款 — [官方许可说明](https://grafana.com/licensing/)、[项目范围](https://github.com/grafana/grafana/blob/main/LICENSING.md)。Grafana 服务自身按其许可分发和使用。 |

其他直接及传递依赖见 [host pyproject](software/host/pyproject.toml)、[host 依赖锁](software/host/requirements.lock)、[test-runner pyproject](software/test-runner/pyproject.toml)、[demo-client pyproject](software/demo-client/pyproject.toml)、[service requirements](software/service/requirements.txt)与[database requirements](software/database/requirements.txt)。发布 wheel、桌面安装包或镜像时，需要根据实际包含的组件补齐许可证及通知；该表不是安装包的完整许可清单。

当前整合的应用与SDK来源及原通知另见 [软件第三方说明](software/THIRD_PARTY_NOTICES.md)，随对应源码和二进制保留。
