"""PySide6 desktop application for the factory ageing HMI.

The package itself stays importable without Qt.  Importing ``app`` or invoking
``python -m factory_hmi.desktop`` reports the optional dependency clearly.
"""

from __future__ import annotations

from typing import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    try:
        from .app import main as run
    except ImportError as exc:
        if exc.name and exc.name.startswith("PySide6"):
            raise RuntimeError(
                "PySide6 is required for the factory desktop client. "
                "Install it with: python -m pip install PySide6"
            ) from exc
        raise
    return run(argv)


__all__ = ["main"]
