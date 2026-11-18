"""Datasets list: drag measurement items up/down to reorder, Delete key removes them,
double-click / F2 renames a measurement dataset."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QListWidget


SELECTION_CSS = (
    "QListWidget::item:selected, QListWidget::item:selected:!active {"
    " background: #cfe3ff; color: #000000; border-left: 3px solid #1f6fd1; }"
    "QListWidget::item:hover:!selected { background: #eef4ff; }"
)


class DatasetList(QListWidget):
    def __init__(self, on_reordered=None, on_delete=None, on_rename=None, on_clear=None, parent=None,
                 main_kind: str = "meas", select_kinds=("meas", "sim")):
        """main_kind: rows that can be moved / renamed (PA: measurements, S-Param: files)."""
        super().__init__(parent)
        self.main_kind = main_kind
        self.select_kinds = tuple(select_kinds)
        self._on_clear = on_clear or (lambda: None)
        self.on_open = None             # double-click -> open the data editor (set by the window)
        self.on_eye = None              # click on the row's icon (colour + eye) -> always hidden on/off
        self.on_copy = None             # Ctrl+C -> copy the selected datasets (paste with Ctrl+V)
        self._eye_click = False         # a click that started on an eye: its move / release are ours too
        self.on_rename_many = None      # F2 with several rows selected -> one name for all of them
        self.on_toggle_many = None      # Space with several rows selected -> check / uncheck all of them
        self.ctrl_a_kinds = lambda: self.select_kinds   # which rows Ctrl+A selects (PA: only results while searching)
        self.alias_kinds = ()           # rows that stand for a main_kind dataset elsewhere (PA: '★ 즐겨찾기' rows)
        self.on_drop_section = None     # (names, section, before_name) -> True if handled (PA: drop into 즐겨찾기)
        try:
            from PySide6.QtCore import QSize
            from ui.eye_icon import ICON_H, ICON_W
            self.setIconSize(QSize(ICON_W, ICON_H))
        except Exception:
            pass
        self._on_rename = on_rename or (lambda name: None)
        self._on_reordered = on_reordered or (lambda names: None)
        self._on_delete = on_delete or (lambda: None)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        # selected rows: the same clear blue whether or not the list has the keyboard focus
        # (Qt's default turns them grey after you click in the plot -> looked "not selected")
        self.setStyleSheet(SELECTION_CSS)

    def item_names(self, kind="meas"):
        """Names of the items of `kind` in their current on-screen order."""
        out = []
        for i in range(self.count()):
            it = self.item(i)
            if it is not None and it.data(257) == kind and it.data(256) is not None:
                out.append(it.data(256))
        return out

    def selected_names(self, kind):
        kinds = (kind,) + (tuple(self.alias_kinds) if kind == self.main_kind else ())
        return list(dict.fromkeys(it.data(256) for it in self.selectedItems()
                                  if it is not None and not it.isHidden() and it.data(257) in kinds
                                  and it.data(256) is not None))

    def section_at(self, row: int) -> str:
        """Section of list row `row` (header kind above it: 'meas', 'fav', 'sim' …); row >= count -> last."""
        row = min(row, self.count() - 1)
        for i in range(row, -1, -1):
            k = str(self.item(i).data(257) or "")
            if k.startswith("header:"):
                return k.split(":", 1)[1]
        return ""

    def drop_rows(self, names, kinds, row: int) -> bool:
        """Rows (names, their kinds) dropped at list row `row`. True = handled here (no Qt move)."""
        if self.on_drop_section is None:
            return False
        sec = self.section_at(row - 1) if row > 0 else ""     # inserted before `row` = section of the row above
        before = None
        for i in range(max(row, 0), self.count()):
            it = self.item(i)
            k = str(it.data(257) or "")
            if k.startswith("header:"):
                break
            if it.data(256) is not None:
                before = it.data(256)
                break
        return bool(self.on_drop_section(list(names), list(kinds), sec, before))

    def dropEvent(self, event):
        try:
            pos = event.position().toPoint()
        except AttributeError:
            pos = event.pos()
        idx = self.indexAt(pos)
        row = idx.row() if idx.isValid() else self.count()
        try:                                    # below the middle of a row = after it
            if idx.isValid() and pos.y() > self.visualRect(idx).center().y():
                row += 1
        except Exception:
            pass
        sel = [it for it in self.selectedItems() if it is not None and it.data(256) is not None]
        if sel and self.drop_rows([it.data(256) for it in sel], [it.data(257) for it in sel], row):
            event.ignore()                      # handled (favourites): nothing is moved by Qt
            return
        super().dropEvent(event)
        self._on_reordered(self.item_names(self.main_kind))

    def _rename(self, it) -> bool:
        if it is not None and it.data(257) in (self.main_kind,) + tuple(self.alias_kinds) and it.data(256) is not None:
            self._on_rename(it.data(256))   # pass the NAME: the list may be rebuilt meanwhile
            return True
        return False

    def clear_choice(self):
        """Empty space clicked / Esc: deselect everything -> all curves look the same again."""
        self.clearSelection()
        self.setCurrentRow(-1)                 # invalid row = no current item
        self._on_clear()

    def mousePressEvent(self, event):
        try:
            pos = event.position().toPoint()
        except AttributeError:
            pos = event.pos()
        it = self.itemAt(pos)
        if it is None:
            self.clear_choice()
            event.accept()
            return
        if self._is_eye(it, pos):
            self._eye_click = True                  # move / release of this click must not select rows
            self.on_eye(it.data(256))               # eye icon clicked -> always hidden on / off
            event.accept()
            return
        if self._is_check(it, pos):
            self._eye_click = True                  # wide check-box area: just tick / untick this row
            self.toggle_check(it)
            event.accept()
            return
        self._eye_click = False
        mods = event.modifiers() if hasattr(event, "modifiers") else 0
        if self._has(mods, Qt.ShiftModifier) and self.shift_select(it, keep=self._has(mods, Qt.ControlModifier)):
            event.accept()
            return
        if it.data(257) in self.select_kinds and it.data(256) is not None:
            self._anchor = (it.data(256), it.data(257))   # start of a later Shift+click range
        super().mousePressEvent(event)

    @staticmethod
    def _has(mods, flag) -> bool:
        try:
            v = getattr(mods & flag, "value", mods & flag)
            return isinstance(v, int) and v != 0
        except Exception:
            return False

    # ------------------------------------------------------------ check box: wide click area
    CHECK_ZONE_PX = 28

    def _is_check(self, it, pos) -> bool:
        """Click in the left part of a row (the check box and the space around it, up to the icon)."""
        try:
            checkable = bool(it.flags() & Qt.ItemIsUserCheckable)
        except Exception:
            checkable = False
        if it is None or not checkable or it.data(257) not in self.select_kinds or it.data(256) is None:
            return False
        rect = self.visualItemRect(it)
        right = rect.left() + self.CHECK_ZONE_PX
        try:
            from PySide6.QtWidgets import QStyle, QStyleOptionViewItem
            opt = QStyleOptionViewItem()
            self.initViewItemOption(opt)
            opt.rect = rect
            opt.features |= QStyleOptionViewItem.HasCheckIndicator | QStyleOptionViewItem.HasDecoration
            opt.decorationSize = self.iconSize()
            opt.icon = it.icon()
            dec = self.style().subElementRect(QStyle.SE_ItemViewItemDecoration, opt, self)
            if dec.width() > 0:
                right = min(right, dec.left() - 3) if self.on_eye is not None else max(right, dec.left() - 1)
        except Exception:
            pass
        return rect.left() <= pos.x() <= right

    def toggle_check(self, it):
        it.setCheckState(Qt.Unchecked if it.checkState() == Qt.Checked else Qt.Checked)

    # ------------------------------------------------------------ Shift+click: range inside one section
    def shift_select(self, it, keep: bool = False) -> bool:
        """Select from the last clicked row to `it`, but only rows of `it`'s own section (★ rows never
        pull in Measurement rows and the other way round). True = done here."""
        if it is None or it.data(257) not in self.select_kinds or it.data(256) is None:
            return False
        row = next((i for i in range(self.count()) if self.item(i) is it), None)
        if row is None:
            return False
        sec, kind = self.section_at(row), it.data(257)
        anchor = getattr(self, "_anchor", None)
        a = None
        if anchor is not None:
            for i in range(self.count()):
                x = self.item(i)
                if (x.data(256), x.data(257)) == anchor:
                    a = i
                    break
        if a is None or self.section_at(a) != sec:
            a = next((i for i in range(self.count()) if self.section_at(i) == sec
                      and self.item(i).data(257) == kind and not self.item(i).isHidden()), row)
        lo, hi = sorted((a, row))
        self.blockSignals(True)
        if not keep:
            self.clearSelection()
        for i in range(lo, hi + 1):
            x = self.item(i)
            if x.data(257) == kind and x.data(256) is not None and not x.isHidden() and self.section_at(i) == sec:
                x.setSelected(True)
        try:
            from PySide6.QtCore import QItemSelectionModel
            self.setCurrentItem(it, QItemSelectionModel.NoUpdate)
        except Exception:
            pass
        self.blockSignals(False)
        self.itemSelectionChanged.emit()
        return True

    def _is_eye(self, it, pos) -> bool:
        return (self.on_eye is not None and it is not None and it.data(257) in self.select_kinds
                and self.icon_hit(it, pos))

    def mouseMoveEvent(self, event):
        # A click that started on an eye: Qt would otherwise treat a tiny mouse move while the
        # button is down as a drag-selection from the last normally clicked row (-> many rows selected).
        if self._eye_click:
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._eye_click:
            self._eye_click = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def icon_hit(self, it, pos) -> bool:
        """Is `pos` on the item's icon (colour swatch + eye)?"""
        rect = self.visualItemRect(it)
        try:
            from PySide6.QtWidgets import QStyle, QStyleOptionViewItem
            opt = QStyleOptionViewItem()
            self.initViewItemOption(opt)
            opt.rect = rect
            opt.features |= QStyleOptionViewItem.HasCheckIndicator | QStyleOptionViewItem.HasDecoration
            opt.decorationSize = self.iconSize()
            opt.icon = it.icon()
            opt.checkState = it.checkState()
            r = self.style().subElementRect(QStyle.SE_ItemViewItemDecoration, opt, self)
            if r.width() > 0:
                return r.adjusted(-2, -3, 2, 3).contains(pos)
        except Exception:
            pass
        x0 = rect.left() + 20                       # fallback: check box ~20 px, then the icon
        return x0 <= pos.x() <= x0 + self.iconSize().width() + 4

    def mouseDoubleClickEvent(self, event):
        # The first click of a double-click switches the dataset and rebuilds the list, so the
        # item Qt saw on the first click no longer exists -> look up what is under the mouse now.
        try:
            pos = event.position().toPoint()
        except AttributeError:
            pos = event.pos()
        it = self.itemAt(pos)
        if self._is_eye(it, pos):                   # two quick clicks on an eye = two toggles,
            self._eye_click = True                  # not "open the data editor"
            self.on_eye(it.data(256))
            event.accept()
            return
        if self._is_check(it, pos):                 # two quick clicks on a check box = two toggles
            self._eye_click = True
            self.toggle_check(it)
            event.accept()
            return
        if self.on_open is not None and it is not None and it.data(257) in self.select_kinds \
                and it.data(256) is not None:
            self.on_open(it.data(256))               # double-click = see / edit the data (F2 = rename)
            event.accept()
            return
        if self._rename(it):
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def selectAll(self):
        """Ctrl+A: select only the rows you can see (the search result), not headers or hidden rows."""
        kinds = tuple(self.ctrl_a_kinds())
        self.clearSelection()
        for i in range(self.count()):
            it = self.item(i)
            if it is not None and not it.isHidden() and it.data(257) in kinds:
                it.setSelected(True)

    def keyPressEvent(self, event):
        try:
            ctrl_a = event.key() == Qt.Key_A and bool(event.modifiers() & Qt.ControlModifier)
        except Exception:
            ctrl_a = False
        if ctrl_a:
            self.selectAll()
            event.accept()
            return
        try:
            from PySide6.QtGui import QKeySequence
            is_copy = event.matches(QKeySequence.Copy)
        except Exception:
            is_copy = False
        if is_copy and self.on_copy is not None:
            self.on_copy()
            event.accept()
            return
        if event.key() == Qt.Key_Escape:
            self.clear_choice()
            event.accept()
            return
        if event.key() == Qt.Key_F2:
            sel = self.selected_names(self.main_kind)
            if len(sel) > 1 and self.on_rename_many is not None:
                self.on_rename_many(sel)            # several selected -> rename them all
                event.accept()
                return
            if self._rename(self.currentItem()):    # one -> just that one
                event.accept()
                return
        if event.key() == Qt.Key_Space and self.on_toggle_many is not None:
            items = [it for it in self.selectedItems() if it is not None and not it.isHidden()
                     and it.data(257) in self.select_kinds and it.data(256) is not None]
            if not items:                           # nothing selected: the current row
                cur = self.currentItem()
                if cur is not None and not cur.isHidden() and cur.data(257) in self.select_kinds \
                        and cur.data(256) is not None:
                    items = [cur]
            if items:                               # one or several -> all on, or all off
                on = not all(it.checkState() == Qt.Checked for it in items)
                self.on_toggle_many([it.data(256) for it in items], on)
                event.accept()
                return
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self._on_delete()
            event.accept()
            return
        super().keyPressEvent(event)
