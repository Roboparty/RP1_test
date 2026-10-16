"""Run the RP1 execution outbox uploader."""

from __future__ import annotations

import os
import signal
import threading
from pathlib import Path

from factory_hmi.sync.outbox import Outbox
from factory_hmi.sync.uploader import worker_from_environment
from factory_hmi.sync.reliability_platform import (
    reliability_worker_from_environment,
)


def main() -> int:
    data_root = Path(
        os.environ.get("RP1_FACTORY_DATA_ROOT", "/var/lib/rp1-factory-hmi")
    )
    mode = os.environ.get(
        "RP1_FACTORY_PLATFORM_MODE", "reliability_v1"
    ).strip().lower()
    if mode == "reliability_v1":
        worker = reliability_worker_from_environment(
            Outbox(data_root / "sync" / "outbox.sqlite3")
        )
        worker.authorize_service_identity()
    elif mode == "legacy":
        worker = worker_from_environment(data_root)
    else:
        raise SystemExit(
            "RP1_FACTORY_PLATFORM_MODE must be legacy or reliability_v1"
        )
    stopped = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        stopped.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    worker.start()
    try:
        stopped.wait()
    finally:
        worker.stop()
        worker.clear_credentials()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
