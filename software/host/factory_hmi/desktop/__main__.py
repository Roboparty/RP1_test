from __future__ import annotations

import sys

from factory_hmi.desktop import main


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"factory HMI cannot start: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
