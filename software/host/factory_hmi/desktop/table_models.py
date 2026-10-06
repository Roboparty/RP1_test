"""Model/View helpers used by the factory desktop tables."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QEvent, QModelIndex, Qt, Signal
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPainter
from PySide6.QtWidgets import (
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
)


TONE_FOREGROUNDS = {
    "success": QColor("#1F9D69"),
    "warning": QColor("#9A6013"),
    "danger": QColor("#B42323"),
    "muted": QColor("#667789"),
}
TONE_BACKGROUNDS = {
    "warning": QColor("#FFF5DD"),
    "danger": QColor("#FFE8E5"),
}


class GridTableModel(QAbstractTableModel):
    """Small immutable-row table model with semantic cell tones."""

    def __init__(self, headers: Sequence[str], parent: Any = None) -> None:
        super().__init__(parent)
        self.headers = tuple(str(item) for item in headers)
        self._rows: list[tuple[Any, ...]] = []
        self._tones: dict[tuple[int, int], str] = {}
        self._metadata: list[dict[str, Any]] = []

    def rowCount(self, _parent: QModelIndex = QModelIndex()) -> int:
        return len(self._rows)

    def columnCount(self, _parent: QModelIndex = QModelIndex()) -> int:
        return len(self.headers)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if (
            role == Qt.ItemDataRole.DisplayRole
            and orientation == Qt.Orientation.Horizontal
            and 0 <= section < len(self.headers)
        ):
            return self.headers[section]
        return None

    def data(
        self,
        index: QModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid():
            return None
        row = index.row()
        column = index.column()
        if row >= len(self._rows) or column >= len(self.headers):
            return None
        value = self._rows[row][column]
        tone = self._tones.get((row, column))
        if role == Qt.ItemDataRole.DisplayRole:
            return str(value)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return (
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
                if column == 0
                else int(Qt.AlignmentFlag.AlignCenter)
            )
        if role == Qt.ItemDataRole.ForegroundRole and tone:
            return TONE_FOREGROUNDS.get(tone)
        if role == Qt.ItemDataRole.BackgroundRole and tone:
            return TONE_BACKGROUNDS.get(tone)
        if role == Qt.ItemDataRole.UserRole:
            return self._metadata[row] if row < len(self._metadata) else {}
        return None

    def set_rows(
        self,
        rows: Sequence[Sequence[Any]],
        *,
        tones: Mapping[tuple[int, int], str] | None = None,
        metadata: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        normalized = [tuple(row) for row in rows]
        if any(len(row) != len(self.headers) for row in normalized):
            raise ValueError("table row width does not match headers")
        same_shape = len(normalized) == len(self._rows)
        if not same_shape:
            self.beginResetModel()
        self._rows = normalized
        self._tones = dict(tones or {})
        self._metadata = [dict(item) for item in (metadata or ())]
        if same_shape:
            if self._rows and self.headers:
                self.dataChanged.emit(
                    self.index(0, 0),
                    self.index(len(self._rows) - 1, len(self.headers) - 1),
                    [
                        int(Qt.ItemDataRole.DisplayRole),
                        int(Qt.ItemDataRole.ForegroundRole),
                        int(Qt.ItemDataRole.BackgroundRole),
                        int(Qt.ItemDataRole.UserRole),
                    ],
                )
        else:
            self.endResetModel()

    def metadata(self, row: int) -> dict[str, Any]:
        if 0 <= row < len(self._metadata):
            return dict(self._metadata[row])
        return {}


class ZeroTableModel(GridTableModel):
    def __init__(self, headers: Sequence[str], parent: Any = None) -> None:
        super().__init__(headers, parent)
        self.action_column = len(self.headers) - 1

    def action_enabled(self, row: int) -> bool:
        return bool(self.metadata(row).get("action_enabled"))


class ZeroButtonDelegate(QStyledItemDelegate):
    """Paint and activate the zero action without per-refresh cell widgets."""

    activated = Signal(int)

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex,
    ) -> None:
        if index.column() != index.model().columnCount() - 1:
            super().paint(painter, option, index)
            return
        model = index.model()
        enabled = bool(
            isinstance(model, ZeroTableModel)
            and model.action_enabled(index.row())
        )
        super().paint(painter, option, index)
        rect = option.rect.adjusted(12, 6, -12, -6)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if enabled:
            painter.setPen(QColor("#C94A44"))
            painter.setBrush(QColor("#FFF2F1") if hovered else QColor("#FFFFFF"))
            text_color = QColor("#B42323")
        else:
            painter.setPen(QColor("#D9E0E7"))
            painter.setBrush(QColor("#EDF1F5"))
            text_color = QColor("#8A98A8")
        painter.drawRoundedRect(rect, 6, 6)
        painter.setPen(text_color)
        painter.drawText(
            rect,
            int(Qt.AlignmentFlag.AlignCenter),
            str(index.data(Qt.ItemDataRole.DisplayRole) or "标零"),
        )
        painter.restore()

    def editorEvent(
        self,
        event: QEvent,
        model: QAbstractTableModel,
        option: QStyleOptionViewItem,
        index: QModelIndex,
    ) -> bool:
        if (
            not isinstance(model, ZeroTableModel)
            or index.column() != model.action_column
            or not model.action_enabled(index.row())
        ):
            return False
        if isinstance(event, QMouseEvent):
            if (
                event.type() == QEvent.Type.MouseButtonRelease
                and event.button() == Qt.MouseButton.LeftButton
                and option.rect.adjusted(8, 4, -8, -4).contains(
                    event.position().toPoint()
                )
            ):
                self.activated.emit(index.row())
                return True
        if isinstance(event, QKeyEvent) and event.type() == QEvent.Type.KeyRelease:
            if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space}:
                self.activated.emit(index.row())
                return True
        return False


__all__ = [
    "GridTableModel",
    "ZeroButtonDelegate",
    "ZeroTableModel",
]
