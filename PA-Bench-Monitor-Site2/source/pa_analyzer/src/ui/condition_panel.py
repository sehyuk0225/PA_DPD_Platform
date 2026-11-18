"""Measurement Condition panel (always shows FREQ, VGS,CA, VGS,PK, VGS,DRV)."""
from __future__ import annotations

from typing import List, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QGridLayout, QGroupBox, QLabel, QWidget

_FREQ_SOURCE_TEXT = {
    "cell": "Sheet 내부 값",
    "column": "Sheet 내부 Column",
    "sheet_name": "Sheet 이름에서 인식",
    "file_name": "파일 이름에서 인식",
    "user": "사용자 지정",
}


class ConditionPanel(QGroupBox):
    def __init__(self, parent: QWidget = None):
        super().__init__("Measurement Condition", parent)
        self._grid = QGridLayout(self)
        self._grid.setHorizontalSpacing(18)
        self._grid.setVerticalSpacing(3)
        self._grid.setContentsMargins(12, 8, 12, 8)
        self._label_font = QFont()
        self._label_font.setBold(True)
        self._value_font = QFont("Consolas")
        self._value_font.setStyleHint(QFont.Monospace)
        self._value_font.setPointSizeF(self._value_font.pointSizeF() + 1)
        self._widgets: List[QLabel] = []
        self.set_items([("VG_DRV", "N/A"), ("VG_CA", "N/A"), ("VG_PK", "N/A"), ("IQ_DRV", "N/A"), ("IQ_CA", "N/A")])

    def set_rows(self, rows, freq_source: str = "") -> None:
        """rows: [[(label, value), ...], ...] — same alignment as the plot header (IQ under VG)."""
        for w in self._widgets:
            self._grid.removeWidget(w)
            w.deleteLater()
        self._widgets = []
        for r, row in enumerate(rows):
            for j, (label, value) in enumerate(row):
                if not label:
                    continue
                lab = QLabel(label)
                lab.setFont(self._label_font)
                val = QLabel(value)
                val.setFont(self._value_font)
                val.setTextInteractionFlags(Qt.TextSelectableByMouse)
                if value == "N/A":
                    val.setStyleSheet("color:#999;")
                self._grid.addWidget(lab, r, j * 2)
                self._grid.addWidget(val, r, j * 2 + 1)
                self._widgets += [lab, val]
        self._grid.setColumnStretch(8, 1)

    def set_items(self, items: List[Tuple[str, str]], freq_source: str = "") -> None:
        for w in self._widgets:
            self._grid.removeWidget(w)
            w.deleteLater()
        self._widgets = []
        # 4 mandatory items in column pair 0/1, extras in column pair 2/3
        per_col = 3 if len(items) > 4 else 4
        for i, (label, value) in enumerate(items):
            col = (i // per_col) * 2
            row = i % per_col
            lab = QLabel(label)
            lab.setFont(self._label_font)
            val = QLabel(value)
            val.setFont(self._value_font)
            val.setTextInteractionFlags(Qt.TextSelectableByMouse)
            if value == "N/A":
                val.setStyleSheet("color:#999;")
            self._grid.addWidget(lab, row, col)
            self._grid.addWidget(val, row, col + 1)
            self._widgets += [lab, val]
        if freq_source:
            src = QLabel(f"FREQ: {_FREQ_SOURCE_TEXT.get(freq_source, freq_source)}")
            src.setStyleSheet("color:#777; font-size:9pt;")
            self._grid.addWidget(src, 0, 8, 1, 1, Qt.AlignRight | Qt.AlignTop)
            self._widgets.append(src)
        self._grid.setColumnStretch(8, 1)
