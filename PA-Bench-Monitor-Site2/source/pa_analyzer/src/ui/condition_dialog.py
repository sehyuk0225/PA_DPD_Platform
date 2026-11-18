"""Choose which measurement conditions appear in the Condition panel and the plot header."""
from __future__ import annotations

from PySide6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QGridLayout, QLabel, QVBoxLayout

from core.quantities import CONDITION_FIELDS, DEFAULT_DISPLAY_CONDITIONS


class ConditionDialog(QDialog):
    def __init__(self, selected, parent=None):
        super().__init__(parent)
        self.setWindowTitle("표시할 Measurement Condition")
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("체크한 항목만 Condition 칸과 Plot 이미지에 표시됩니다 (표시 순서 = 아래 순서)."))
        grid = QGridLayout()
        self.checks = {}
        for i, (k, f) in enumerate(CONDITION_FIELDS.items()):
            cb = QCheckBox(f"{f.label}" + (f"  ({f.unit})" if f.unit else ""))
            cb.setChecked(k in selected)
            self.checks[k] = cb
            grid.addWidget(cb, i % 8, i // 8)
        lay.addLayout(grid)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel | QDialogButtonBox.RestoreDefaults)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        bb.clicked.connect(self._clicked)
        self._bb = bb
        lay.addWidget(bb)

    def _clicked(self, button):
        if self._bb.buttonRole(button) == QDialogButtonBox.ResetRole:
            for k, cb in self.checks.items():
                cb.setChecked(k in DEFAULT_DISPLAY_CONDITIONS)

    def values(self):
        # lab bias order first, then the rest in registry order
        order = list(DEFAULT_DISPLAY_CONDITIONS) + [k for k in CONDITION_FIELDS if k not in DEFAULT_DISPLAY_CONDITIONS]
        return [k for k in order if self.checks[k].isChecked()]
