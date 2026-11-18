"""Dialog shown after each paste: dataset name + FREQ (pre-filled from what was detected),
and which Column Mapping to use: automatic recognition or one of the saved favourites (맵핑1 …)."""
from __future__ import annotations

from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QMessageBox,
                               QVBoxLayout)

from core.quantities import QUANTITIES
from utils.freq import format_freq, parse_frequency

AUTO = "자동 인식"


def describe(result) -> str:
    found = ", ".join(f"{QUANTITIES[q].label} ← {result.raw.cell(result.header_row, c)}"
                      for q, c in result.mapping.items() if q in ("pout", "gain", "pae", "pin")) \
        if result.header_row is not None and result.header_row >= 0 else \
        ", ".join(f"{QUANTITIES[q].label} ← {c + 1}번째 열" for q, c in result.mapping.items()
                  if q in ("pout", "gain", "pae", "pin"))
    n = result.dataset.n_points if result.dataset else 0
    return (f"인식한 열: {found}\n데이터 {n}행" if result.ok else
            "⚠ Pout / Gain / PAE 열을 찾지 못했습니다. 아래 '맵핑'에서 즐겨찾기를 고르거나,\n"
            "저장 후 [Column Mapping…]으로 지정하세요.")


class PasteDialog(QDialog):
    def __init__(self, result, parent=None, last_name: str = "", presets=(), preset=None, on_preset=None):
        """presets: saved Column Mapping favourites (names); preset: the one already applied
        (automatic recognition failed -> the last used favourite was tried); on_preset(name|None) -> new result."""
        super().__init__(parent)
        self.setWindowTitle("붙여넣은 데이터")
        self.setMinimumWidth(440)
        self.result = result
        self._on_preset = on_preset
        lay = QVBoxLayout(self)
        self.info = QLabel(describe(result))
        self.info.setWordWrap(True)
        lay.addWidget(self.info)
        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.name_edit.setText(last_name or result.label)      # the name used for the previous paste
        self.name_edit.setToolTip("바로 전에 붙여넣을 때 쓴 이름이 들어가 있습니다. 같은 이름을 여러 번 써도 됩니다")
        hz = result.freq_hz
        self.freq_edit = QLineEdit(format_freq(hz) if hz else "")
        self.freq_edit.setPlaceholderText("예: 3.6  또는 3600MHz")
        form.addRow("이름", self.name_edit)
        form.addRow("FREQ", self.freq_edit)
        self.map_combo = QComboBox()
        self.map_combo.addItem(AUTO, None)
        for p in presets:
            self.map_combo.addItem(f"★ {p}", p)
        self.map_combo.setToolTip("Column Mapping 즐겨찾기 (Column Mapping 창에서 [현재 맵핑 저장…])\n"
                                  "자동 인식이 안 되면 마지막에 쓴 즐겨찾기가 미리 적용됩니다")
        if preset is not None:
            i = self.map_combo.findData(preset)
            self.map_combo.setCurrentIndex(max(i, 0))
        self.map_combo.currentIndexChanged.connect(self._preset_changed)
        self.map_combo.setEnabled(bool(presets) and on_preset is not None)
        form.addRow("맵핑", self.map_combo)
        lay.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.name_edit.selectAll()

    def choose_preset(self, name):
        """Use this favourite (None = automatic recognition) -> the data is read again right away."""
        i = self.map_combo.findData(name)
        self.map_combo.setCurrentIndex(max(i, 0))
        self._preset_changed()

    def _preset_changed(self, *_):
        if self._on_preset is None:
            return
        res = self._on_preset(self.map_combo.currentData())
        if res is not None:
            self.result = res
            self.info.setText(describe(res))
            if not self.freq_edit.text().strip() and res.freq_hz:
                self.freq_edit.setText(format_freq(res.freq_hz))

    def _accept(self):
        if self.freq_edit.text().strip() and parse_frequency(self.freq_edit.text()) is None:
            QMessageBox.warning(self, "FREQ", "FREQ를 인식할 수 없습니다. 예: 3.6, 3.6GHz, 3600MHz")
            return
        self.accept()

    def values(self):
        txt = self.freq_edit.text().strip()
        return self.name_edit.text().strip(), (parse_frequency(txt) if txt else None)
