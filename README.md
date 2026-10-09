# RP1 Test

English | [简体中文](README_CN.md)

Test rigs, software and methods for RP1 module aging and whole-robot reliability testing. The repository includes arm, torso and leg-load rig files, RP1 Factory HMI, trajectory and data-collection tools, data services, and 18 draft whole-robot test methods.

![Test platform hardware architecture](hardware/mechanical/手臂胸腔/共用资料/architecture/platform-overview.png)

## Start Here

| Task | Entry point |
| --- | --- |
| Try the software without a robot | [Factory HMI with the fake backend](software/host/README.md#快速开始) · [Data services and synthetic demo](software/README.md#快速开始) |
| Build a module test rig | [Hardware files and build guide](hardware/README.md) |
| Plan and record a test | [18 test methods](test-cases/README.md) · [Inspection and run record](test-cases/checklist.md) |

The HMI fake backend needs no motor SDK. The data-service demo generates a synthetic two-joint record over 12 simulated seconds, including a 2-second pause and 10 seconds of active time. Its result remains `INCONCLUSIVE`.

## Files and Scope

- **Hardware:** mechanical CAD, drawings and BOMs for three rigs, plus a control-cabinet reference architecture. Electrical construction schematics, I/O and terminal schedules, and pin-by-pin harness drawings are not included.
- **Software:** desktop HMI, local control gateway, trajectory tools, FastAPI service, PostgreSQL, InfluxDB and Grafana. Real-device SDKs, CAN setup and the control-cabinet PLC interface need integration on the target equipment.
- **Test methods:** 18 drafts with conditions, steps and records. F0–F3 inspection levels and acceptance thresholds require project approval; the repository does not supply completed whole-robot qualification results.
- **Offline analysis:** a [standalone Python tool and worked example](data-analysis/README.md) for active time, tracking error, temperature, missing samples and events. It needs no data services; cross-run comparison and HMI package conversion are not yet provided.

Test methods use RP1 reference conditions. Start with the [common execution rules](test-cases/common.md) and [parameters to confirm](test-cases/open-items.md) before applying them to equipment.

Native CAD packages are available in [Releases](https://github.com/Roboparty/rp1_test/releases/tag/v0.1.0). Extract the required package into the repository root; the outer folder name does not matter. See [CAD downloads](hardware/mechanical/README.md#下载-cad) for assembly paths. Manufacturing files and BOMs remain in the repository.

## License and Contributions

See [LICENSES.md](LICENSES.md) for the scope of the software, hardware, documentation and third-party licences.

For a file error or a contribution, open an [issue](https://github.com/Roboparty/rp1_test/issues) with the module or test-case ID, relevant file, and configuration or software version.
