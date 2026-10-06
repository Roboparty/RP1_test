"""Start the RP1 factory aging-test control gateway."""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from factory_hmi.gateway.app import create_app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--lease-timeout", type=float, default=2.0)
    parser.add_argument("--status-rate", type=float, default=10.0)
    parser.add_argument("--data-root", type=Path, default=None)
    parser.add_argument("--log-level", default="info")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be in [1, 65535]")
    if args.lease_timeout <= 0.0:
        raise SystemExit("--lease-timeout must be positive")
    if args.status_rate <= 0.0:
        raise SystemExit("--status-rate must be positive")
    uvicorn.run(
        create_app(
            lease_timeout_s=float(args.lease_timeout),
            status_rate_hz=float(args.status_rate),
            data_root=args.data_root,
        ),
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
