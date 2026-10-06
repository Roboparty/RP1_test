from __future__ import annotations

import os
import unittest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from factory_hmi.desktop.table_models import GridTableModel, ZeroTableModel


class FactoryHmiTableModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_grid_model_exposes_rows_headers_and_cell_tones(self) -> None:
        model = GridTableModel(("关节", "error_id"))
        model.set_rows(
            (("左肩", "7"),),
            tones={(0, 1): "danger"},
            metadata=({"motor_index": 3},),
        )

        self.assertEqual(model.rowCount(), 1)
        self.assertEqual(model.columnCount(), 2)
        self.assertEqual(
            model.headerData(
                0,
                Qt.Orientation.Horizontal,
                Qt.ItemDataRole.DisplayRole,
            ),
            "关节",
        )
        self.assertEqual(
            model.index(0, 1).data(Qt.ItemDataRole.DisplayRole),
            "7",
        )
        self.assertIsNotNone(
            model.index(0, 1).data(Qt.ItemDataRole.BackgroundRole)
        )
        self.assertEqual(model.metadata(0)["motor_index"], 3)
        model.set_rows(
            (("左肩", "0"),),
            tones={(0, 1): "success"},
            metadata=({"motor_index": 3},),
        )
        self.assertEqual(
            model.index(0, 1).data(Qt.ItemDataRole.DisplayRole),
            "0",
        )

    def test_zero_action_enablement_is_kept_in_row_metadata(self) -> None:
        model = ZeroTableModel(("关节", "操作"))
        model.set_rows(
            (("左肩", "标零"),),
            metadata=({"action_enabled": True},),
        )

        self.assertTrue(model.action_enabled(0))
        self.assertFalse(model.action_enabled(1))


if __name__ == "__main__":
    unittest.main()
