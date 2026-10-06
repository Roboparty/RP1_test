"""Run the RP1 execution outbox uploader."""

from __future__ import annotations

import os
import signal
import threading
from pathlib import Path

from factory_hmi.sync.uploader import worker_from_environment


def main() -> int:
    data_root = Path(
        os.environ.get("RP1_FACTORY_DATA_ROOT", "/var/lib/rp1-factory-hmi")
    )
    worker = worker_from_environment(data_root)
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
