"""Column Mapping dialog — manual override when header auto-detection fails."""
from __future__ import annotations

from typing import Dict, Optional, Tuple

from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QInputDialog,
                               QLabel, QMessageBox, QPushButton, QSpinBox, QVBoxLayout)

from measurement import mapping_presets

from core.quantities import QUANTITIES
from excel.sheet_parser import SheetParseResult

MAPPABLE = ("pout", "pae", "gain", "pin")
REQUIRED = ("pout",)


class ColumnMappingDialog(QDialog):
    def __init__(self, result: SheetParseResult, parent=None, settings=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle(f"Column Mapping — {result.sheet_name}")
        self.result = result
        self.setMinimumWidth(420)

        lay = QVBoxLayout(self)
        info = QLabel("Header가 있는 행과 각 물리량의 Column을 지정하세요.\n"
                      "Header 행 아래의 숫자 행이 데이터로 사용됩니다.")
        info.setWordWrap(True)
        lay.addWidget(info)

        form = QFormLayout()
        self.header_spin = QSpinBox()
        self.header_spin.setRange(0, max(result.raw.n_rows, 1))
        self.header_spin.setSpecialValueText("없음 (1행부터 데이터)")
        self.header_spin.setValue((result.header_row + 1) if result.header_row is not None else 1)
        self.header_spin.valueChanged.connect(self._refresh_columns)
        form.addRow("Header Row", self.header_spin)

        self.combos: Dict[str, QComboBox] = {}
        for q in MAPPABLE:
            cb = QComboBox()
            cb.setMinimumWidth(240)
            self.combos[q] = cb
            req = " *" if q in REQUIRED else ""
            form.addRow(f"{QUANTITIES[q].label}{req}  →", cb)
        lay.addLayout(form)

        # ---- favourites: '맵핑1' … (saved once, applied in one click; also in the Datasets right-click menu)
        fav = QHBoxLayout()
        fav.addWidget(QLabel("즐겨찾기"))
        self.preset_combo = QComboBox()
        self.preset_load_btn = QPushButton("불러오기")
        self.preset_save_btn = QPushButton("현재 맵핑 저장…")
        self.preset_load_btn.clicked.connect(lambda *_: self.load_preset())
        self.preset_save_btn.clicked.connect(lambda *_: self.save_preset())
        fav.addWidget(self.preset_combo, 1)
        fav.addWidget(self.preset_load_btn)
        fav.addWidget(self.preset_save_btn)
        lay.addLayout(fav)
        self._fill_presets()

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        self._refresh_columns()
        for q, cb in self.combos.items():
            col = result.mapping.get(q)
            if col is not None:
                idx = cb.findData(col)
                if idx >= 0:
                    cb.setCurrentIndex(idx)

    # ------------------------------------------------------------ favourites
    def presets(self):
        return mapping_presets.load(self.settings) if self.settings is not None else []

    def _fill_presets(self):
        self.preset_combo.clear()
        for p in self.presets():
            self.preset_combo.addItem(p.get("name", "?"), p.get("name"))
        has = self.preset_combo.count() > 0
        self.preset_combo.setEnabled(has)
        self.preset_load_btn.setEnabled(has)

    def load_preset(self, name: Optional[str] = None):
        name = name or self.preset_combo.currentData()
        p = next((x for x in self.presets() if x.get("name") == name), None)
        if p is None:
            return
        hr, m = mapping_presets.resolve(p, self.result)
        if self.settings is not None:
            self.settings.setValue("mapping_last", p.get("name"))
        self.header_spin.setValue(0 if hr is None else hr + 1)
        self._refresh_columns()
        for q, cb in self.combos.items():
            idx = cb.findData(m.get(q, -1))
            cb.setCurrentIndex(idx if idx >= 0 else 0)

    def save_preset(self, name: Optional[str] = None):
        if self.settings is None:
            return
        presets = self.presets()
        if name is None:
            name, ok = QInputDialog.getText(self, "맵핑 즐겨찾기 저장", "이름 (같은 이름이면 덮어씀):",
                                            text=mapping_presets.next_name(presets))
            name = str(name).strip() if ok else ""
        if not name:
            return
        hr, m = self.values()
        mapping_presets.save(self.settings, mapping_presets.upsert(
            presets, mapping_presets.make_preset(name, self.result, hr, m)))
        self.settings.setValue("mapping_last", name)       # tried first when a paste is not recognised
        self._fill_presets()

    def _header_row(self) -> Optional[int]:
        v = self.header_spin.value()
        return None if v == 0 else v - 1

    def _refresh_columns(self):
        hr = self._header_row()
        cols = self.result.columns(-1 if hr is None else hr)
        for cb in self.combos.values():
            keep = cb.currentData()
            cb.blockSignals(True)
            cb.clear()
            cb.addItem("(사용 안 함)", -1)
            for c in cols:
                cb.addItem(c.display, c.index)
            idx = cb.findData(keep) if keep is not None else -1
            cb.setCurrentIndex(idx if idx >= 0 else 0)
            cb.blockSignals(False)

    def _accept(self):
        m = self.mapping()
        for q in REQUIRED:
            if m.get(q, -1) < 0:
                QMessageBox.warning(self, "Column Mapping", f"{QUANTITIES[q].label} column은 반드시 지정해야 합니다.")
                return
        used = [c for c in m.values() if c >= 0]
        if len(used) != len(set(used)):
            QMessageBox.warning(self, "Column Mapping", "같은 Column을 여러 물리량에 지정할 수 없습니다.")
            return
        self.accept()

    def mapping(self) -> Dict[str, int]:
        return {q: cb.currentData() for q, cb in self.combos.items() if cb.currentData() is not None}

    def values(self) -> Tuple[Optional[int], Dict[str, int]]:
        return self._header_row(), {q: c for q, c in self.mapping().items() if c >= 0}
