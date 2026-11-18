"""Data editor: see and change the numbers behind a curve (double-click a dataset / a curve).

Shared by the PA tab (raw cell grid of a measurement dataset) and the S-Parameter tab
(Freq + dB / phase columns of a file). Editing works like a small spreadsheet:
  * type into cells, Ctrl+V pastes a block copied from Excel at the selected cell, rows can be added / deleted
  * formulas like Excel:  =A3+B3*2,  =(C5-C4)/2,  =$B$2*C7  (+ - * / and parentheses)
    while typing a formula, CLICK a cell to insert its reference (e.g. after '=' or an operator)
  * fill handle: the small square at the bottom-right of the selection - drag it down / right
    to copy the formula (references move like in Excel), or double-click it to fill down
Nothing changes until OK; formulas are stored as their computed values.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from utils.cells import fmt_cell, to_cell  # noqa: F401  (re-exported for the windows)
from utils.formula import col_name, evaluate_all, is_formula, ref_name, shift_formula
from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QGuiApplication, QKeySequence, QPainter, QPen
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QStyledItemDelegate, QTableWidget, QTableWidgetItem,
                               QVBoxLayout)

FORMULA_ROLE = 0x0100 + 7          # Qt.UserRole + 7: the '=...' text of a formula cell
REF_AFTER = "=+-*/("                # clicking a cell right after one of these inserts its reference
HANDLE = 7                          # fill handle size (px)


class FormulaDelegate(QStyledItemDelegate):
    """Shows the formula (not the value) while editing; keeps the editor open while you click
    cells to insert references."""

    def __init__(self, table):
        super().__init__(table)
        self.table = table

    def createEditor(self, parent, option, index):
        ed = QLineEdit(parent)
        self.table.editor = ed
        ed.destroyed.connect(lambda *_: setattr(self.table, "editor", None))
        return ed

    def setEditorData(self, editor, index):
        it = self.table.item(index.row(), index.column())
        f = it.data(FORMULA_ROLE) if it is not None else None
        editor.setText(f if f else (it.text() if it is not None else ""))

    def eventFilter(self, editor, event):
        from PySide6.QtCore import QEvent
        if event.type() == QEvent.FocusOut and self.table.picking():
            return True                     # clicking a cell to insert a reference: stay in the editor
        return super().eventFilter(editor, event)


class SheetTable(QTableWidget):
    """QTableWidget + click-to-reference while typing a formula + fill handle."""

    def __init__(self, dialog):
        super().__init__()
        self.dialog = dialog
        self.editor = None
        self._fill = None                   # (r0, c0, r1, c1, target row, target col) while dragging

    def picking(self) -> bool:
        ed = self.editor
        if ed is None:                      # no cell being edited
            return False
        try:
            t = ed.text()
            pos = ed.cursorPosition()
        except (RuntimeError, AttributeError):   # editor already closed / deleted
            self.editor = None
            return False
        return t.startswith("=") and pos > 0 and t[pos - 1] in REF_AFTER

    def _sel(self):
        rs = self.selectedRanges()
        if not rs:
            return None
        r = rs[-1]
        return r.topRow(), r.leftColumn(), r.bottomRow(), r.rightColumn()

    def handle_rect(self):
        sel = self._sel()
        if sel is None or self.dialog.readonly:
            return None
        rect = self.visualRect(self.model().index(sel[2], sel[3]))
        from PySide6.QtCore import QRect
        return QRect(rect.right() - HANDLE // 2, rect.bottom() - HANDLE // 2, HANDLE, HANDLE)

    def mousePressEvent(self, event):
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        idx = self.indexAt(pos)
        if self.picking() and idx.isValid():                 # formula: click = insert that cell
            self.editor.insert(ref_name(idx.row(), idx.column()))
            self.editor.setFocus()
            return
        h = self.handle_rect()
        if h is not None and h.adjusted(-3, -3, 3, 3).contains(pos) and event.button() == Qt.LeftButton:
            r0, c0, r1, c1 = self._sel()
            self._fill = [r0, c0, r1, c1, r1, c1]
            self.setCursor(Qt.CrossCursor)
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        if self._fill is not None:
            idx = self.indexAt(pos)
            if idx.isValid():
                self._fill[4], self._fill[5] = idx.row(), idx.column()
                self.viewport().update()
            return
        h = self.handle_rect()
        self.setCursor(Qt.CrossCursor if (h is not None and h.adjusted(-3, -3, 3, 3).contains(pos))
                       else Qt.ArrowCursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._fill is not None:
            r0, c0, r1, c1, tr, tc = self._fill
            self._fill = None
            self.setCursor(Qt.ArrowCursor)
            self.dialog.fill((r0, c0, r1, c1), tr, tc)
            self.viewport().update()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        h = self.handle_rect()
        if h is not None and h.adjusted(-3, -3, 3, 3).contains(pos):
            self.dialog.fill_down_auto()                     # like Excel: fill down along the data
            return
        super().mouseDoubleClickEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        h = self.handle_rect()
        if h is None:
            return
        p = QPainter(self.viewport())
        p.fillRect(h, QColor("#1f6fd1"))
        if self._fill is not None:
            r0, c0, r1, c1, tr, tc = self._fill
            a = self.visualRect(self.model().index(min(r0, tr), min(c0, tc)))
            b = self.visualRect(self.model().index(max(r1, tr), max(c1, tc)))
            pen = QPen(QColor("#1f6fd1"))
            pen.setStyle(Qt.DashLine)
            p.setPen(pen)
            p.drawRect(a.united(b).adjusted(0, 0, -1, -1))
        p.end()


class DataEditorDialog(QDialog):
    def __init__(self, title: str, rows: Sequence[Sequence], headers: Optional[List[str]] = None,
                 name: Optional[str] = None, readonly: bool = False, mark_cols: Optional[Dict[int, str]] = None,
                 header_row: Optional[int] = None, focus_row: Optional[int] = None, note: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 560)
        self.readonly = readonly
        lay = QVBoxLayout(self)
        self.name_edit = None
        if name is not None:
            row = QHBoxLayout()
            row.addWidget(QLabel("이름"))
            self.name_edit = QLineEdit()
            self.name_edit.setText(name)
            self.name_edit.setReadOnly(readonly)
            row.addWidget(self.name_edit, 1)
            lay.addLayout(row)
        if note:
            lab = QLabel(note)
            lab.setWordWrap(True)
            lab.setStyleSheet("color:#555; font-size:8.5pt;")
            lay.addWidget(lab)
        n_cols = max([len(r) for r in rows] + [len(headers or [])] + [1])
        self.headers = list(headers or [])
        self.table = SheetTable(self)
        self.table.setItemDelegate(FormulaDelegate(self.table))
        self.table.setColumnCount(n_cols)
        self.table.setRowCount(len(rows))
        self._label_columns()
        self.table.setSelectionMode(QAbstractItemView.ContiguousSelection)
        if readonly:
            self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        mark_cols = mark_cols or {}
        for r, vals in enumerate(rows):
            for c in range(n_cols):
                it = QTableWidgetItem(fmt_cell(vals[c]) if c < len(vals) else "")
                if c in mark_cols:
                    it.setBackground(QBrush(QColor(mark_cols[c])))
                if header_row is not None and r == header_row:
                    f = it.font()
                    f.setBold(True)
                    it.setFont(f)
                self.table.setItem(r, c, it)
        lay.addWidget(self.table, 1)
        self._busy = False
        self.table.itemChanged.connect(self._on_item_changed)
        btns = QHBoxLayout()
        if not readonly:
            self.add_btn, self.del_btn, self.paste_btn = QPushButton("행 추가"), QPushButton("선택 행 삭제"), \
                QPushButton("붙여넣기 (Ctrl+V)")
            self.add_col_btn = QPushButton("열 추가")
            self.add_col_btn.setToolTip("오른쪽에 빈 열 추가 (계산 결과를 따로 두고 싶을 때)")
            self.add_btn.clicked.connect(self.add_row)
            self.add_col_btn.clicked.connect(self.add_column)
            self.del_btn.clicked.connect(self.delete_rows)
            self.paste_btn.clicked.connect(lambda *_: self.paste())
            for b in (self.add_btn, self.add_col_btn, self.del_btn, self.paste_btn):
                btns.addWidget(b)
            hint = QLabel("계산: '=' 입력 후 셀 클릭 (예: =A3+B3*2) · 선택 오른쪽 아래 ■ 를 끌거나 더블클릭 = 채우기")
            hint.setStyleSheet("color:#1f6fd1; font-size:8.5pt;")
            btns.addWidget(hint)
        btns.addStretch(1)
        lay.addLayout(btns)
        box = QDialogButtonBox(QDialogButtonBox.Close if readonly else (QDialogButtonBox.Ok | QDialogButtonBox.Cancel))
        box.accepted.connect(self._ok)
        box.rejected.connect(self.reject)
        lay.addWidget(box)
        if focus_row is not None and 0 <= focus_row < len(rows):
            self.table.setCurrentCell(focus_row, 0)
            self.table.scrollToItem(self.table.item(focus_row, 0), QAbstractItemView.PositionAtCenter)
            self.table.selectRow(focus_row)

    # ------------------------------------------------------------ columns / formulas
    def _label_columns(self):
        """Column headers 'A', 'B' … (+ the given names) so formulas can refer to them."""
        n = self.table.columnCount()
        self.table.setHorizontalHeaderLabels(
            [f"{col_name(c)}  {self.headers[c]}" if c < len(self.headers) and self.headers[c] else col_name(c)
             for c in range(n)])

    def _on_item_changed(self, it):
        if self._busy or it is None:
            return
        t = it.text()
        if is_formula(t):
            it.setData(FORMULA_ROLE, t.strip())
        elif it.data(FORMULA_ROLE):
            it.setData(FORMULA_ROLE, None)               # a plain value typed over a formula
        self.recalc()

    def _raw(self, r, c):
        it = self.table.item(r, c)
        if it is None:
            return None
        f = it.data(FORMULA_ROLE)
        return f if f else to_cell(it.text())

    def recalc(self):
        """Compute every formula cell and show its value (errors in red, formula as tooltip)."""
        cells, ftab = {}, {}
        for r in range(self.table.rowCount()):
            for c in range(self.table.columnCount()):
                v = self._raw(r, c)
                if v is not None:
                    cells[(r, c)] = v
                    if is_formula(v):
                        ftab[(r, c)] = v
        if not ftab:
            return
        vals = evaluate_all(cells)
        self._busy = True
        try:
            for (r, c), f in ftab.items():
                it = self.table.item(r, c)
                v = vals.get((r, c))
                err = isinstance(v, str) and v.startswith("#ERR")
                it.setText(v if err else fmt_cell(float(v)) if v is not None else "")
                it.setToolTip(f"{f}" + (f"\n{v}" if err else ""))
                it.setForeground(QBrush(QColor("#c62828" if err else "#0b5394")))
        finally:
            self._busy = False

    def _set_cell(self, r, c, value):
        if r >= self.table.rowCount():
            self.table.setRowCount(r + 1)
        if c >= self.table.columnCount():
            self.table.setColumnCount(c + 1)
            self._label_columns()
        it = QTableWidgetItem("" if value is None else (value if isinstance(value, str) else fmt_cell(value)))
        if is_formula(value):
            it.setData(FORMULA_ROLE, value)
        self._busy = True
        try:
            self.table.setItem(r, c, it)
        finally:
            self._busy = False

    def fill(self, src, to_row: int, to_col: int):
        """Fill handle: repeat the selected block down (or right) up to (to_row, to_col);
        formulas get their relative references moved, like Excel."""
        r0, c0, r1, c1 = src
        down = (to_row - r1) >= (to_col - c1)
        if down and to_row > r1:
            h = r1 - r0 + 1
            for r in range(r1 + 1, to_row + 1):
                sr = r0 + (r - r0) % h
                for c in range(c0, c1 + 1):
                    v = self._raw(sr, c)
                    self._set_cell(r, c, shift_formula(v, r - sr, 0) if is_formula(v) else v)
        elif not down and to_col > c1:
            w = c1 - c0 + 1
            for c in range(c1 + 1, to_col + 1):
                sc = c0 + (c - c0) % w
                for r in range(r0, r1 + 1):
                    v = self._raw(r, sc)
                    self._set_cell(r, c, shift_formula(v, 0, c - sc) if is_formula(v) else v)
        else:
            return
        self.recalc()

    def fill_down_auto(self):
        """Double-click on the fill handle: fill down as far as the neighbouring column has data."""
        sel = self.table._sel()
        if sel is None:
            return
        r0, c0, r1, c1 = sel
        last = r1
        for nc in (c0 - 1, c1 + 1):
            if 0 <= nc < self.table.columnCount():
                r = r1 + 1
                while r < self.table.rowCount() and self._raw(r, nc) is not None:
                    r += 1
                last = max(last, r - 1)
        if last > r1:
            self.fill(sel, last, c1)

    def errors(self) -> int:
        return sum(1 for r in range(self.table.rowCount()) for c in range(self.table.columnCount())
                   if self.table.item(r, c) is not None and self.table.item(r, c).data(FORMULA_ROLE)
                   and self.table.item(r, c).text().startswith("#ERR"))

    def _ok(self):
        n = self.errors()
        if n and QMessageBox.question(self, "계산 오류", f"계산 오류가 있는 셀이 {n}개 있습니다 (빨간 글씨).\n"
                                      "그 셀은 빈 값으로 저장됩니다. 그대로 적용할까요?",
                                      QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        self.accept()

    # ------------------------------------------------------------ editing
    def add_row(self):
        r = self.table.currentRow() + 1 if self.table.currentRow() >= 0 else self.table.rowCount()
        self.table.insertRow(r)
        for c in range(self.table.columnCount()):
            self.table.setItem(r, c, QTableWidgetItem(""))

    def add_column(self):
        c = self.table.columnCount()
        self.table.setColumnCount(c + 1)
        self._busy = True
        for r in range(self.table.rowCount()):
            self.table.setItem(r, c, QTableWidgetItem(""))
        self._busy = False
        self._label_columns()

    def delete_rows(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)

    def paste(self, text: Optional[str] = None):
        """Paste a tab-separated block (copied from Excel) starting at the current cell."""
        text = QGuiApplication.clipboard().text() if text is None else text
        if not text:
            return
        r0 = max(self.table.currentRow(), 0)
        c0 = max(self.table.currentColumn(), 0)
        lines = text.replace("\r\n", "\n").rstrip("\n").split("\n")
        for i, line in enumerate(lines):
            cells = line.split("\t")
            r = r0 + i
            if r >= self.table.rowCount():
                self.table.insertRow(r)
            for j, v in enumerate(cells):
                self._set_cell(r, c0 + j, v.strip())
        self._label_columns()
        self.recalc()

    def keyPressEvent(self, event):
        if not self.readonly and event.matches(QKeySequence.Paste):
            self.paste()
            return
        if event.matches(QKeySequence.Copy):
            self.copy()
            return
        if not self.readonly and event.key() == Qt.Key_Delete:
            self._busy = True
            for it in self.table.selectedItems():
                it.setData(FORMULA_ROLE, None)
                it.setText("")
            self._busy = False
            self.recalc()
            return
        super().keyPressEvent(event)

    def copy(self):
        idx = self.table.selectedIndexes()
        if not idx:
            return
        rows = sorted({i.row() for i in idx})
        cols = sorted({i.column() for i in idx})
        out = []
        for r in rows:
            out.append("\t".join((self.table.item(r, c).text() if self.table.item(r, c) else "") for c in cols))
        QGuiApplication.clipboard().setText("\n".join(out))

    # ------------------------------------------------------------ result
    def rows(self) -> List[list]:
        out = []
        for r in range(self.table.rowCount()):
            row = []
            for c in range(self.table.columnCount()):
                it = self.table.item(r, c)
                t = it.text() if it is not None else ""
                row.append(None if t.startswith("#ERR") else to_cell(t))   # formulas: their value
            while row and row[-1] is None:
                row.pop()
            out.append(row)
        while out and not out[-1]:
            out.pop()                                   # trailing empty rows
        return out

    def values(self):
        return (self.name_edit.text().strip() if self.name_edit is not None else None), self.rows()
