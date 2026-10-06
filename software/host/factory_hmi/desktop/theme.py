"""Shared visual theme helpers for the factory desktop."""

from __future__ import annotations

from pathlib import Path
from typing import Any


THEME_PATH = Path(__file__).resolve().parent / "assets" / "factory_hmi.qss"


def apply_theme(widget: Any) -> None:
    widget.setStyleSheet(THEME_PATH.read_text(encoding="utf-8"))


def repolish(widget: Any) -> None:
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def set_tone(widget: Any, tone: str) -> None:
    if widget.property("tone") == tone:
        return
    widget.setProperty("tone", tone)
    repolish(widget)


__all__ = ["THEME_PATH", "apply_theme", "repolish", "set_tone"]
