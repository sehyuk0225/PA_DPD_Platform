"""Plot title / X-axis / Y-axis text of one plot (double-click on it, or Plot right-click → 제목 / 축 이름)."""
from __future__ import annotations

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout


class LabelsDialog(QDialog):
    def __init__(self, current: dict, defaults: dict, parent=None, focus: str = "title"):
        super().__init__(parent)
        self.setWindowTitle("제목 / 축 이름")
        self.setMinimumWidth(420)
        self.defaults = dict(defaults)
        lay = QVBoxLayout(self)
        note = QLabel("이 Plot의 이 그래프에만 적용됩니다. 비우면 기본 이름.")
        note.setWordWrap(True)
        lay.addWidget(note)
        form = QFormLayout()
        self.edits = {}
        for k, lab in (("title", "제목"), ("x", "X축"), ("y", "Y축")):
            ed = QLineEdit(current.get(k, ""))
            ed.setPlaceholderText(defaults.get(k, ""))
            self.edits[k] = ed
            form.addRow(lab, ed)
        lay.addLayout(form)
        reset = QPushButton("기본 이름으로")
        reset.clicked.connect(lambda *_: [ed.setText(self.defaults.get(k, "")) for k, ed in self.edits.items()])
        lay.addWidget(reset)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        if focus in self.edits:
            self.edits[focus].setFocus()
            self.edits[focus].selectAll()

    def values(self) -> dict:
        return {k: ed.text().strip() for k, ed in self.edits.items()}
