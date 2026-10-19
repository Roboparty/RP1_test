# Third-party components

This repository combines application source and third-party components. Existing copyright and license notices remain in force. Publishing this repository does not grant a new blanket license over all components.

| Component | License / provenance | Delivery form |
|---|---|---|
| `host/motors` | GPL-3.0; original copyright headers and full text in `host/motors/LICENSE` | Source, CMake build recipe, Python extension in HMI package |
| PySide6 / Shiboken / Qt | LGPL-3.0 / GPL / commercial options and component-specific notices from The Qt Company | Dynamically loaded runtime in the HMI dependency directory |
| FastAPI, React, Vite, NumPy and other libraries | Their respective upstream licenses | Exact versions in Python lockfiles and `frontend/package-lock.json` |
| PostgreSQL / Nginx container images | Respective upstream licenses and notices | Version/digest-pinned images obtained from upstream registries |

Motor source and modifications are provided in this repository. Keep these sources, build scripts, dependency lockfiles and notices with every corresponding binary delivery. The HMI package contains Python application source and a dependency directory with shared Qt libraries. Preserve applicable LGPL notices, library replacement/relinking rights, and source availability obligations when redistributing. Obtain any commercial Qt license directly from its rights holder if that is the chosen distribution model.

Vendor CAN drivers, PLC programming software, PLC firmware, motor firmware and customer data are not licensed by this repository and are not included. Obtain them from the relevant rights holder for the actual hardware.

Application files without an explicit license retain their existing rights; no MIT/Apache license has been invented for them. Before a downstream commercial redistribution, the distributor must confirm rights and applicable third-party obligations for its complete product. This file records dependencies, not a certification of compliance.
