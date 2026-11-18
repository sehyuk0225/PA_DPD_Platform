"""Main window: shared file list + up to 4 independent plot panes.

The left panel always edits the ACTIVE pane (blue border; click a pane to
activate it). Keyboard shortcuts act on the pane under the mouse.
"""
from __future__ import annotations

import io
import os
import traceback

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QAction, QColor, QCursor, QGuiApplication, QIcon, QKeySequence, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QColorDialog, QComboBox, QFileDialog,
                               QGridLayout, QMenu,
                               QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMainWindow, QMessageBox, QPushButton, QRadioButton,
                               QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

import matplotlib

from sparam.app.controller import COLORS, LINESTYLES, LINEWIDTHS, PANEL_LOCS, Controller, DataStore
from sparam.core.models import PARAM_LABEL, PARAMS
from sparam.loaders import FILE_FILTER, is_supported
from sparam.plotting.export import figure_png_bytes, save_png
from sparam.ui.clipboard import copy_png_to_clipboard
from sparam.ui.plot_pane import PlotPane
from sparam.utils.freq import parse_freq_input
from sparam.utils.numeric import fmt_num
from sparam.utils.paths import sparam_example_dir

# F / V / H / Delete belong to this app: remove matplotlib's single-key shortcuts
for _k in ("keymap.fullscreen", "keymap.home", "keymap.back", "keymap.forward", "keymap.pan", "keymap.zoom",
           "keymap.save", "keymap.grid", "keymap.grid_minor", "keymap.yscale", "keymap.xscale", "keymap.quit",
           "keymap.copy"):
    if _k in matplotlib.rcParams:
        matplotlib.rcParams[_k] = []

APP_TITLE = "S-Parameter Viewer"   # tab of the RF PA Analyzer suite
UID_ROLE = 256  # Qt.UserRole
KIND_ROLE = 257  # "file" (shared Datasets-list widget of the PA tab)
GRID = {1: (1, 1), 2: (1, 2), 3: (2, 2), 4: (2, 2)}
# default content of the 4 panes: magnitude, Smith, return loss, phase
STYLE_NAMES = {"solid": "실선 ━━", "dashed": "파선 ╍╍", "dotted": "점선 ┈┈", "dashdot": "1점 쇄선 ━·"}
WIDTH_NAMES = {"thin": "가는 선", "normal": "보통", "thick": "굵은 선"}
LOC_NAMES = {"upper left": "왼쪽 위", "upper right": "오른쪽 위", "lower left": "왼쪽 아래", "lower right": "오른쪽 아래"}
PRESETS = [("db", {"s21"}), ("smith", {"s11", "s22"}), ("db", {"s11", "s22"}), ("phase", {"s21"})]


def _small_buttons(*buttons):
    """Toolbar buttons next to the plot count: one size, a bit smaller than normal buttons."""
    for b in buttons:
        b.setFixedSize(76, 20)
        b.setStyleSheet("QPushButton { font-size: 8pt; padding: 0px 2px; }")


def _half_buttons(*buttons):
    """'결과만 Plot' / '선택 Plot': half the old width each, side by side next to the search box."""
    for b in buttons:
        b.setFixedSize(62, 22)
        b.setStyleSheet("QPushButton { font-size: 8pt; padding: 0px 1px; }")


def _guard(fn):
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except Exception as exc:  # pragma: no cover - GUI safety net
            traceback.print_exc()
            QMessageBox.critical(self, APP_TITLE, f"예기치 않은 오류가 발생했습니다:\n{exc}")
    wrapper.__name__ = fn.__name__
    return wrapper


def _color_icon(hex_color: str):
    pm = QPixmap(14, 14)
    pm.fill(QColor(hex_color))
    return QIcon(pm)


class MainWindow(QMainWindow):
    def __init__(self, controller: Controller = None, settings=None, n_panes: int = 2):
        super().__init__()
        first = controller or Controller()
        self.store: DataStore = first.store
        self.controllers = [first] + [Controller(store=self.store) for _ in range(3)]
        for ctl, (fmt, params) in zip(self.controllers, PRESETS):
            ctl.fmt, ctl.params = fmt, set(params)
        self.settings = settings or QSettings("SParam-Viewer", "SParam-Viewer")
        self._syncing = False
        self.save_project_hook = lambda: None       # set by the suite: one project file for both tabs
        self.save_project_quick_hook = lambda: None
        self.open_project_hook = lambda: None
        self._filter = ""
        self._sel_gen = 0
        from app.history import History
        self.history = History()
        self._restoring = False
        self._hist_gen = 0
        self.active = None
        self.n_panes = n_panes
        self.setWindowTitle(APP_TITLE)
        self.resize(1500, 920)
        self.setAcceptDrops(True)
        self._build_ui()
        self._build_actions()
        self.set_active(self.panes[0])
        self.set_layout(n_panes)
        self.refresh_files()

    # ------------------------------------------------------------ helpers
    @property
    def c(self) -> Controller:
        return self.active.c

    @property
    def visible_panes(self):
        return self.panes[: self.n_panes]

    def target_pane(self) -> PlotPane:
        """Pane under the mouse, else the active pane."""
        return next((p for p in self.visible_panes if p.mouse is not None), self.active)

    # ================================================================ UI
    def _build_ui(self):
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 6)
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_left())
        splitter.addWidget(self._build_right())
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([330, 1170])
        from ui.splitter_util import wide_handle
        wide_handle(splitter)
        root.addWidget(splitter, 1)

        bottom = QHBoxLayout()
        self.copy_btn = QPushButton("📋  Copy Plot")
        self.copy_btn.setToolTip("선택된(파란 테두리) Plot을 Clipboard로 복사 (Ctrl+Shift+C)")
        self.copy_all_btn = QPushButton("📋  Copy All Plots")
        self.copy_all_btn.setToolTip("화면의 모든 Plot을 한 장의 이미지로 복사")
        self.save_btn = QPushButton("💾  Save PNG")
        for b in (self.copy_btn, self.copy_all_btn, self.save_btn):
            b.setMinimumHeight(34)
        self.copy_btn.clicked.connect(self.on_copy_plot)
        self.copy_all_btn.clicked.connect(self.on_copy_all)
        self.save_btn.clicked.connect(self.on_save_png)
        bottom.addWidget(self.copy_btn, 1)
        bottom.addWidget(self.copy_all_btn, 1)
        bottom.addStretch(1)
        bottom.addWidget(self.save_btn, 1)
        root.addLayout(bottom)
        self.setCentralWidget(central)
        self.statusBar().showMessage("CSV / Excel / .s2p 파일을 추가하거나 창에 끌어다 놓으세요.")

    def _build_left(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 6, 0)

        files = QGroupBox("Files")
        files.setToolTip("체크 = 표시 · 눈 아이콘 = 항상 숨기기 · 선택 = Plot에서 강조 (Ctrl/Shift/Ctrl+A로 여러 개, 빈 곳 클릭/Esc = 해제)\n"
                         "더블클릭 = 데이터 보기/수정 · F2 = 이름 · 끌어서 순서 변경 · Delete = 삭제 · 우클릭 = 색/선/보기/숨기기")
        fl = QVBoxLayout(files)
        frow = QHBoxLayout()
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("검색 (파일 이름)  예) LotA")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.setToolTip("이름에 글자가 들어간 파일만 목록에 표시 (띄어쓰기 = 모두 포함)\n"
                                    "Enter 또는 [결과만 Plot] = 검색된 것만 체크해서 표시")
        self.filter_edit.textChanged.connect(self.on_filter_changed)
        self.filter_edit.returnPressed.connect(self.on_filter_show_only)
        self.filter_only_btn = QPushButton("결과만 Plot")
        self.filter_only_btn.clicked.connect(self.on_filter_show_only)
        self.selected_only_btn = QPushButton("선택 Plot")
        self.selected_only_btn.setToolTip("목록에서 선택한 것만 Plot (우클릭 '이것들만 보기'와 같음)")
        self.selected_only_btn.clicked.connect(self.on_selected_show_only)
        self.filter_only_btn.setToolTip("검색된 것만 Plot (Enter와 같음)")
        _half_buttons(self.filter_only_btn, self.selected_only_btn)
        frow.addWidget(self.filter_edit, 1)
        frow.addWidget(self.filter_only_btn)
        frow.addWidget(self.selected_only_btn)
        fl.addLayout(frow)
        from ui.dataset_list import DatasetList
        self.file_list = DatasetList(on_reordered=self.on_files_reordered, on_delete=self.on_delete_files,
                                     on_rename=self._rename_uid, on_clear=self.on_list_clear,
                                     main_kind="file", select_kinds=("file",))
        self.file_list.on_open = self.on_open_file_data          # double-click = see / edit the data
        self.file_list.on_eye = self.on_eye                      # icon click = always hidden on / off
        self.file_list.on_copy = self.on_copy_files              # Ctrl+C -> copy files (Ctrl+V pastes)
        self.file_list.on_rename_many = self._rename_many        # F2 with several selected
        self.file_list.on_toggle_many = self.on_toggle_many      # Space with several selected
        self.file_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.file_list.itemChanged.connect(self.on_item_changed)
        self.file_list.itemSelectionChanged.connect(self.on_file_selection)
        self.file_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.file_list.customContextMenuRequested.connect(self.on_file_context)
        fl.addWidget(self.file_list)
        row = QHBoxLayout()
        self.add_btn, self.remove_btn = QPushButton("Add…"), QPushButton("Remove")
        self.clear_btn, self.example_btn = QPushButton("Clear"), QPushButton("Example")
        for b, slot in ((self.add_btn, self.on_add), (self.remove_btn, self.on_remove),
                        (self.clear_btn, self.on_clear), (self.example_btn, self.on_example)):
            b.clicked.connect(slot)
            row.addWidget(b)
        fl.addLayout(row)
        lay.addWidget(files, 1)

        self.pane_label = QLabel()
        self.pane_label.setStyleSheet("font-weight:bold; color:#1f77b4;")
        lay.addWidget(self.pane_label)

        pbox = QGroupBox("Parameters")
        pl = QHBoxLayout(pbox)
        self.param_checks = {}
        for p in PARAMS:
            cb = QCheckBox(PARAM_LABEL[p])
            cb.toggled.connect(lambda on, p=p: self.on_param(p, on))
            self.param_checks[p] = cb
            pl.addWidget(cb)
        lay.addWidget(pbox)

        fbox = QGroupBox("Format")
        fml = QHBoxLayout(fbox)
        self.fmt_group = QButtonGroup(self)
        self.db_radio = QRadioButton("dB")
        self.phase_radio = QRadioButton("Phase")
        self.smith_radio = QRadioButton("Smith")
        self.smith_radio.setToolTip("Smith Chart (크기+위상 데이터: .s2p 등)")
        self.fmt_radios = {"db": self.db_radio, "phase": self.phase_radio, "smith": self.smith_radio}
        for rb in self.fmt_radios.values():
            self.fmt_group.addButton(rb)
            fml.addWidget(rb)
            rb.toggled.connect(self.on_fmt)
        lay.addWidget(fbox)

        self.axis_box = QGroupBox("Axis Range")
        al = QGridLayout(self.axis_box)
        self.xmin_edit, self.xmax_edit, self.ymin_edit, self.ymax_edit = (QLineEdit() for _ in range(4))
        self.xmin_label, self.xmax_label = QLabel("X Min"), QLabel("X Max")
        self.ymin_label, self.ymax_label = QLabel("Y Min"), QLabel("Y Max")
        for i, (lab, ed) in enumerate([(self.xmin_label, self.xmin_edit), (self.xmax_label, self.xmax_edit),
                                       (self.ymin_label, self.ymin_edit), (self.ymax_label, self.ymax_edit)]):
            al.addWidget(lab, i // 2, (i % 2) * 2)
            ed.setMaximumWidth(80)
            ed.returnPressed.connect(self.on_apply_range)
            al.addWidget(ed, i // 2, (i % 2) * 2 + 1)
        self.apply_btn, self.auto_btn = QPushButton("Apply"), QPushButton("Auto Scale")
        self.fit_btn, self.reset_btn = QPushButton("⤢ Fit (F)"), QPushButton("Reset")
        self.fit_btn.setToolTip("Zoom/Pan 후 내가 설정한 범위로 복귀 (F)")
        self.reset_btn.setToolTip("설정한 범위를 지우고 데이터 전체로")
        for b, slot in ((self.apply_btn, self.on_apply_range), (self.auto_btn, self.on_auto),
                        (self.fit_btn, self.on_fit), (self.reset_btn, self.on_reset)):
            b.clicked.connect(slot)
        al.addWidget(self.apply_btn, 2, 0, 1, 2)
        al.addWidget(self.auto_btn, 2, 2, 1, 2)
        al.addWidget(self.fit_btn, 3, 0, 1, 2)
        al.addWidget(self.reset_btn, 3, 2, 1, 2)
        hint = QLabel("왼쪽 Drag: 이동 · 오른쪽 Drag: 영역 Zoom · 휠: Zoom\n"
                      "Legend 더블클릭: 이름 변경 · F: 설정 범위로 복귀")
        hint.setStyleSheet("color:#777; font-size:8.5pt;")
        al.addWidget(hint, 4, 0, 1, 4)
        lay.addWidget(self.axis_box)

        mbox = QGroupBox("Marker")
        ml = QVBoxLayout(mbox)
        row1 = QHBoxLayout()
        self.marker_edit = QLineEdit()
        self.marker_edit.setPlaceholderText("예: 3.6 또는 3600MHz")
        self.marker_edit.returnPressed.connect(self.on_add_marker)
        self.marker_btn, self.marker_clear_btn = QPushButton("Add"), QPushButton("Clear")
        self.marker_btn.clicked.connect(self.on_add_marker)
        self.marker_clear_btn.clicked.connect(self.on_clear_markers)
        row1.addWidget(self.marker_edit, 1)
        row1.addWidget(self.marker_btn)
        row1.addWidget(self.marker_clear_btn)
        ml.addLayout(row1)
        row2 = QHBoxLayout()
        self.values_check = QCheckBox("값 표시")
        self.values_check.toggled.connect(self.on_values_toggle)
        row2.addWidget(self.values_check)
        row2.addStretch(1)
        row2.addWidget(QLabel("단위"))
        self.step_edit = QLineEdit("0.05")
        self.step_edit.setMaximumWidth(48)
        self.step_edit.setToolTip("세로 Marker 이동 단위 (GHz, 0 = 제한 없음)")
        self.db_step_edit = QLineEdit("0.5")
        self.db_step_edit.setMaximumWidth(40)
        self.db_step_edit.setToolTip("가로 Marker 이동 단위 (dB, 0 = 제한 없음)")
        for ed in (self.step_edit, self.db_step_edit):
            ed.returnPressed.connect(self.on_step)
            ed.editingFinished.connect(self.on_step)
        row2.addWidget(self.step_edit)
        row2.addWidget(QLabel("GHz"))
        row2.addWidget(self.db_step_edit)
        row2.addWidget(QLabel("dB"))
        ml.addLayout(row2)
        mhint = QLabel("V / H: 세로 / 가로 Marker · M: 곡선 위 점 말풍선\n"
                       "클릭 후 ←→ / ↑↓ 이동 · 끌어서 이동 · 더블클릭: 값 입력 · Del: 삭제\n값 상자·말풍선: 끌어서 이동, 우클릭 메뉴")
        mhint.setStyleSheet("color:#777; font-size:8.5pt;")
        ml.addWidget(mhint)
        lay.addWidget(mbox)
        w.setMinimumWidth(300)
        w.setMaximumWidth(440)
        return w

    def _build_right(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 0, 0, 0)
        top = QHBoxLayout()
        top.addWidget(QLabel("Plot 개수:"))
        self.layout_combo = QComboBox()
        for n in (1, 2, 3, 4):
            self.layout_combo.addItem(f"{n}", n)
        self.layout_combo.currentIndexChanged.connect(self.on_layout_changed)
        top.addWidget(self.layout_combo)
        self.axis_reset_btn = QPushButton("↺ Axis Reset")
        self.axis_reset_btn.setToolTip("모든 Plot의 축 범위를 기본값(데이터 전체)으로\n(Reset = 파란 테두리 Plot만, F = 설정 범위로 복귀)")
        self.axis_reset_btn.clicked.connect(self.on_reset_all_axes)
        self.colors_distinct_btn = QPushButton("🎨 각각 다른 색")
        self.colors_distinct_btn.setToolTip("지금 Plot에 그려진 파일을 모두 서로 다른 색으로")
        self.colors_distinct_btn.clicked.connect(self.on_colors_distinct)
        self.colors_base_btn = QPushButton("기본색")
        self.colors_base_btn.setToolTip("지금 Plot에 그려진 파일의 색을 처음(불러올 때) 색으로 (선 굵기·종류는 그대로)")
        self.colors_base_btn.clicked.connect(self.on_colors_base)
        self.lines_reset_btn = QPushButton("━ 선 기본")
        self.lines_reset_btn.setToolTip("지금 Plot에 그려진 파일의 선 종류·굵기를 기본으로 (색은 그대로)")
        self.lines_reset_btn.clicked.connect(self.on_lines_default)
        self.axis_reset_btn.setText("↺ Axis")
        self.colors_distinct_btn.setText("🎨 각각 색")
        _small_buttons(self.axis_reset_btn, self.colors_distinct_btn, self.colors_base_btn, self.lines_reset_btn)
        for b in (self.axis_reset_btn, self.colors_distinct_btn, self.colors_base_btn, self.lines_reset_btn):
            top.addWidget(b)
        top.addStretch(1)
        self.fit_top_btn = QPushButton("⤢ Fit (F)")
        self.fit_top_btn.clicked.connect(self.on_fit)
        top.addWidget(self.fit_top_btn)
        lay.addLayout(top)

        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(6)
        self.panes = [PlotPane(i, ctl, self.set_active, self.on_pane_changed, self.on_legend_rename,
                               self.on_plot_context, self.on_pane_key)
                      for i, ctl in enumerate(self.controllers)]
        for p in self.panes:
            p.on_pick = self.on_plot_pick
            p.on_open_data = lambda pane, key, f: self.on_open_file_data(key[0], f)
            p.on_marker_value = self.on_marker_value
        lay.addWidget(self.grid_host, 4)

        self.table_box = QGroupBox("Marker Table (선택된 Plot)")
        tl = QVBoxLayout(self.table_box)
        self.table = QTableWidget()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        tl.addWidget(self.table)
        self.copy_table_btn = QPushButton("Copy Table  (Excel에 붙여넣기)")
        self.copy_table_btn.clicked.connect(self.on_copy_table)
        tl.addWidget(self.copy_table_btn)
        lay.addWidget(self.table_box, 1)
        return w

    def _build_actions(self):
        menu = self.menuBar().addMenu("&File")
        for text, key, slot in [("&Add Files…", QKeySequence(QKeySequence.Open), self.on_add),
                                ("Copy Plot", QKeySequence("Ctrl+Shift+C"), self.on_copy_plot),
                                ("Save PNG…", QKeySequence("Ctrl+Alt+S"), self.on_save_png),
                                ("프로젝트 저장 (덮어쓰기)", QKeySequence(QKeySequence.Save),
                                 lambda: self.save_project_quick_hook()),
                                ("Fit to my range", QKeySequence("F"), self.on_fit),
                                ("Vertical Marker", QKeySequence("V"), self.on_v_key),
                                ("Horizontal Marker", QKeySequence("H"), self.on_h_key),
                                ("Point Marker (말풍선)", QKeySequence("M"), self.on_m_key),
                                ("Delete Marker", QKeySequence("Delete"), self.on_delete_marker),
                                ("다른 이름으로 프로젝트 저장 (PA + S-Parameter)…", QKeySequence("Ctrl+Shift+S"),
                                 lambda: self.save_project_hook()),
                                ("프로젝트 열기…", QKeySequence("Ctrl+Shift+O"), lambda: self.open_project_hook()),
                                ("붙여넣기 (복사한 파일)", QKeySequence(QKeySequence.Paste), self.on_paste_files),
                                ("되돌리기 (Undo)", QKeySequence(QKeySequence.Undo), self.on_undo),
                                ("다시 실행 (Redo)", QKeySequence("Ctrl+Y"), self.on_redo)]:
            a = QAction(text, self)
            a.setShortcut(key)
            a.setShortcutContext(Qt.WindowShortcut)
            a.triggered.connect(slot)
            self.addAction(a)
            menu.addAction(a)

    # ============================================================ panes
    def set_layout(self, n: int):
        self.n_panes = max(1, min(4, int(n)))
        rows, cols = GRID[self.n_panes]
        for p in self.panes:
            self.grid.removeWidget(p)
            p.setVisible(False)
        for r in range(2):                      # equal stretch -> every pane the same size
            self.grid.setRowStretch(r, 1 if r < rows else 0)
        for col in range(2):
            self.grid.setColumnStretch(col, 1 if col < cols else 0)
        for i, p in enumerate(self.visible_panes):
            self.grid.addWidget(p, i // cols, i % cols)
            p.setVisible(True)
            p.render()
        if self.active not in self.visible_panes:
            self.set_active(self.panes[0])
        idx = self.layout_combo.findData(self.n_panes)
        if idx >= 0 and self.layout_combo.currentIndex() != idx:
            self.layout_combo.blockSignals(True)
            self.layout_combo.setCurrentIndex(idx)
            self.layout_combo.blockSignals(False)

    def set_active(self, pane: PlotPane):
        if self.active is pane:
            return
        self.active = pane
        for p in self.panes:
            p.set_active(p is pane)
        self.sync_panel()

    def sync_panel(self):
        """Show the active pane's settings in the left panel."""
        c = self.c
        self._syncing = True
        try:
            self.pane_label.setText(f"▶ 설정 대상: Plot {self.active.index + 1}  (Plot을 클릭해 선택)")
            for p, cb in self.param_checks.items():
                cb.setChecked(p in c.params)
            self.fmt_radios[c.fmt].setChecked(True)
            self.values_check.setChecked(c.show_marker_values)
            self.step_edit.setText(fmt_num(c.snap_ghz, 4) or "0")
            self.db_step_edit.setText(fmt_num(c.snap_db, 3) or "0")
            smith = c.is_smith
            self.xmin_label.setText("F Start (GHz)" if smith else "X Min (GHz)")
            self.xmax_label.setText("F Stop (GHz)" if smith else "X Max (GHz)")
            for wdg in (self.ymin_edit, self.ymax_edit, self.auto_btn):
                wdg.setEnabled(not smith)
            self._fill_ranges()
            self._fill_table()
        finally:
            self._syncing = False

    def render_all(self):
        for p in self.visible_panes:
            p.render()
        self.sync_panel()
        self._status()
        self._history_note()

    def on_pane_changed(self, pane, what: str):
        self._history_note()
        if what == "labels":
            for p in self.visible_panes:
                p.render()
        if pane is self.active and what in ("range", "markers"):
            self._syncing = True
            try:
                self._fill_ranges()
                if what == "markers":
                    self._fill_table()
            finally:
                self._syncing = False

    def _fill_ranges(self):
        c, ax = self.c, self.active.ax
        if ax is None:
            return
        if c.is_smith:
            fr = c.smith_frange or c.data_frange_ghz()
            self.xmin_edit.setText(fmt_num(fr[0], 3) if fr else "")
            self.xmax_edit.setText(fmt_num(fr[1], 3) if fr else "")
            self.ymin_edit.setText("")
            self.ymax_edit.setText("")
            return
        (x0, x1), (y0, y1) = ax.get_xlim(), ax.get_ylim()
        for ed, v in ((self.xmin_edit, x0), (self.xmax_edit, x1), (self.ymin_edit, y0), (self.ymax_edit, y1)):
            ed.setText(fmt_num(v, 3))

    def _fill_table(self):
        header, rows = self.c.marker_table()
        self.table_box.setVisible(bool(self.c.markers_hz))
        self.table.setColumnCount(len(header))
        self.table.setRowCount(len(rows))
        self.table.setHorizontalHeaderLabels(header)
        for r, row in enumerate(rows):
            for col, text in enumerate(row):
                self.table.setItem(r, col, QTableWidgetItem(text))
        self.table.resizeColumnsToContents()

    def _status(self):
        msgs = [f"Plot {p.index + 1}: {m}" for p in self.visible_panes for m in p.c.missing_params_messages()]
        if msgs:
            self.statusBar().showMessage("⚠ " + " | ".join(msgs[:2]) + (" …" if len(msgs) > 2 else ""))
        elif self.store.entries:
            n = sum(e.visible for e in self.store.entries)
            self.statusBar().showMessage(f"{n}/{len(self.store.entries)} files shown")

    # ============================================================ files
    def refresh_files(self):
        keep = {it.data(UID_ROLE) for it in self.file_list.selectedItems() if it is not None}
        try:
            scroll = int(self.file_list.verticalScrollBar().value())
        except (TypeError, ValueError):
            scroll = 0
        terms = self._filter_terms()
        self._syncing = True
        self.file_list.blockSignals(True)
        try:
            self.file_list.clear()
            current = None
            for e in self.store.entries:
                from ui.eye_icon import swatch_eye_icon
                muted = getattr(e, "muted", False)
                it = QListWidgetItem(swatch_eye_icon(e.color, muted), f"{e.data.name}   ·  {e.data.kind}")
                if muted:
                    from PySide6.QtGui import QBrush
                    it.setForeground(QBrush(QColor("#9e9e9e")))
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsDragEnabled)
                it.setCheckState(Qt.Checked if e.visible else Qt.Unchecked)
                it.setData(UID_ROLE, e.uid)
                it.setData(KIND_ROLE, "file")
                it.setToolTip(e.data.path + ("\n" + "\n".join(e.data.notes) if e.data.notes else ""))
                self.file_list.addItem(it)
                it.setHidden(not self._match(self._match_text(e), terms))
                if e.uid in keep:
                    it.setSelected(True)
                if self.store.highlight and e.uid == self.store.highlight[-1] and not it.isHidden():
                    current = it
            if current is not None:
                from PySide6.QtCore import QItemSelectionModel
                self.file_list.setCurrentItem(current, QItemSelectionModel.NoUpdate)
            try:
                self.file_list.doItemsLayout()
            except Exception:
                pass
            self.file_list.verticalScrollBar().setValue(scroll)      # no jump to the top
            if self._scroll_to_current and current is not None:
                self.file_list.scrollToItem(current)
        finally:
            self._scroll_to_current = False
            self.file_list.blockSignals(False)
            self._syncing = False
        self.render_all()

    # ============================================================ search / selection / highlight
    _scroll_to_current = False

    def _filter_terms(self):
        return [t for t in self._filter.lower().split() if t]

    @staticmethod
    def _match(text, terms) -> bool:
        low = (text or "").lower()
        return all(t in low for t in terms)

    @staticmethod
    def _match_text(e) -> str:
        return f"{e.data.name} {e.data.kind} {os.path.basename(e.data.path or '')}"

    def _selected_uids(self):
        return [it.data(UID_ROLE) for it in self.file_list.selectedItems()
                if it is not None and not it.isHidden() and it.data(UID_ROLE) is not None]

    @_guard
    def on_filter_changed(self, text: str = ""):
        self._filter = str(text or "")
        self.refresh_files()

    @_guard
    def on_filter_show_only(self, *_):
        terms = self._filter_terms()
        if not terms:
            return
        uids = [e.uid for e in self.store.entries if self._match(self._match_text(e), terms)]
        if not uids:
            self.statusBar().showMessage(f"'{self._filter}' 검색 결과가 없습니다", 4000)
            return
        self.c.show_only(uids)
        self.c.clear_highlight()
        self.refresh_files()

    @_guard
    def on_selected_show_only(self, *_):
        uids = self._selected_uids()
        if not uids:
            self.statusBar().showMessage("Plot할 파일을 목록에서 선택하세요 (Ctrl/Shift+클릭, Ctrl+A)", 4000)
            return
        if len(uids) == 1:
            self.c.set_muted(uids, False)
        self.c.show_only(uids)
        self.refresh_files()

    @_guard
    def on_file_selection(self):
        """Selection in the list (click, Ctrl/Shift, Ctrl+A, arrow keys) -> those files bold on the plots."""
        if self._syncing:
            return
        uids = self._selected_uids()
        if uids == self.store.highlight:
            return
        self.c.set_highlight(uids)
        from PySide6.QtCore import QTimer
        self._sel_gen += 1
        gen = self._sel_gen

        def run():
            if gen == self._sel_gen:
                self.render_panes()
        QTimer.singleShot(80, run)        # arrow key held down: draw once it rests

    def render_panes(self):
        for p in self.visible_panes:
            p.render()

    @_guard
    def on_list_clear(self):
        self.c.clear_highlight()
        self.render_panes()

    @_guard
    def on_plot_pick(self, pane, key):
        """Click on a curve -> its file is emphasised and selected in the list; empty space -> none."""
        if key is None:
            if not self.store.highlight:
                return
            self.c.clear_highlight()
            self.file_list.blockSignals(True)
            self.file_list.clearSelection()
            self.file_list.blockSignals(False)
        else:
            uid = key[0]
            self.c.set_highlight([uid])
            self.file_list.blockSignals(True)
            self.file_list.clearSelection()
            for i in range(self.file_list.count()):
                it = self.file_list.item(i)
                if it.data(UID_ROLE) == uid:
                    it.setSelected(True)
                    self.file_list.setCurrentItem(it)
                    if not it.isHidden():
                        self.file_list.scrollToItem(it, QAbstractItemView.PositionAtCenter)
                    else:
                        self.statusBar().showMessage("선택한 곡선의 파일은 검색 결과에 없어 목록에서 가려져 있습니다", 4000)
            self.file_list.blockSignals(False)
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self.render_panes)

    @_guard
    def on_files_reordered(self, uids):
        self.c.reorder(uids)
        self.refresh_files()

    @_guard
    def on_delete_files(self, *_, confirm: bool = True):
        uids = self._selected_uids()
        if not uids:
            return
        names = [self.c.entry(u).data.name for u in uids if self.c.entry(u)]
        text = (f"'{names[0]}' 파일을 목록에서 삭제할까요?" if len(names) == 1
                else f"선택한 {len(names)}개 파일을 목록에서 삭제할까요?\n" + ", ".join(names[:6]) + (" …" if len(names) > 6 else ""))
        if confirm and QMessageBox.question(self, APP_TITLE, text + "\n(원본 파일은 바뀌지 않습니다)",
                                            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes) != QMessageBox.Yes:
            return
        self.c.remove(uids)
        self.refresh_files()

    # ============================================================ project (.pam) state
    def has_data(self) -> bool:
        return bool(self.store.entries)

    def export_state(self) -> dict:
        """Everything of this tab for the project file (the curves themselves are embedded,
        so the project opens even if the original files moved or are not reachable)."""
        def plain(v):
            if isinstance(v, set):
                return sorted(v)
            if isinstance(v, tuple):
                return [plain(x) for x in v]
            if isinstance(v, list):
                return [plain(x) for x in v]
            if isinstance(v, dict):
                return {k: plain(x) for k, x in v.items()}
            return v
        files = [self._file_dict(e) for e in self.store.entries]
        return {"files": files,
                "labels": [[list(k), v] for k, v in self.store.labels.items()],
                "styles": [[list(k), v] for k, v in self.store.styles.items()],
                "next_uid": self.store.next_uid, "color_i": self.store.color_i,
                "panes": [plain(c.pane_state()) for c in self.controllers], "n_panes": self.n_panes}

    @staticmethod
    def _file_dict(e) -> dict:
        d = e.data
        return {"uid": e.uid, "name": d.name, "path": d.path, "kind": d.kind, "z0": d.z0,
                "notes": list(d.notes), "freq_hz": [float(x) for x in d.freq_hz],
                "db": {k: [float(x) for x in v] for k, v in d.db.items()},
                "phase": {k: [float(x) for x in v] for k, v in d.phase.items()},
                "color": e.color, "base_color": getattr(e, "base_color", e.color),
                "visible": e.visible, "linestyle": e.linestyle, "linewidth": e.linewidth,
                "muted": bool(getattr(e, "muted", False))}

    @staticmethod
    def _entry_from_dict(f: dict, uid: int):
        import numpy as np
        from sparam.app.controller import Entry
        from sparam.core.models import SParamData
        d = SParamData(f.get("name", "data"), f.get("path", ""), np.asarray(f.get("freq_hz", []), float),
                       {k: np.asarray(v, float) for k, v in (f.get("db") or {}).items()},
                       {k: np.asarray(v, float) for k, v in (f.get("phase") or {}).items()},
                       f.get("kind", "csv"), f.get("z0"), list(f.get("notes") or []))
        e = Entry(d, f.get("color") or "#1f77b4", bool(f.get("visible", True)), uid, f.get("linestyle"), f.get("linewidth"))
        e.base_color = f.get("base_color") or e.color
        e.muted = bool(f.get("muted", False))
        return e

    @_guard
    def on_copy_files(self, *_):
        """Ctrl+C in the Files list -> selected files to the clipboard (Ctrl+V here or in another copy)."""
        from ui import clip_datasets
        uids = self._selected_uids()
        ents = [self.c.entry(u) for u in uids if self.c.entry(u)]
        if not ents:
            return
        text = clip_datasets.table_text(
            [(e.data.name, [["Freq (GHz)"] + [f"{k.upper()} dB" for k in e.data.db] + [f"{k.upper()} deg" for k in e.data.phase]]
              + [[float(e.data.freq_hz[i]) / 1e9] + [float(v[i]) for v in e.data.db.values()]
                 + [float(v[i]) for v in e.data.phase.values()] for i in range(len(e.data.freq_hz))])
             for e in ents])
        clip_datasets.put(clip_datasets.MIME_SP, {"version": 1, "files": [self._file_dict(e) for e in ents]}, text)
        self.statusBar().showMessage(f"📋 파일 {len(ents)}개 복사 — Ctrl+V로 붙여넣기 (다른 창의 프로그램에도 가능)", 6000)

    @_guard
    def on_paste_files(self, *_):
        from ui import clip_datasets
        payload = clip_datasets.get(clip_datasets.MIME_SP)
        if not payload:
            self.statusBar().showMessage("붙여넣을 S-Parameter 파일이 없습니다 (Files 목록에서 Ctrl+C로 복사하세요)", 4000)
            return
        n = 0
        for f in payload.get("files", []):
            e = self._entry_from_dict(f, self.store.next_uid)
            self.store.next_uid += 1
            e.data.name = self.c._unique_name(e.data.name)
            e.visible = True
            self.store.entries.append(e)
            n += 1
        self.refresh_files()
        self.statusBar().showMessage(f"✔ 파일 {n}개 붙여넣기 (데이터·색·선 그대로) — 저장하려면 프로젝트 저장", 6000)

    def import_state(self, st: dict) -> None:
        st = st or {}
        entries = [self._entry_from_dict(f, int(f.get("uid", 0))) for f in st.get("files", [])]
        self.store.entries[:] = entries
        self.store.labels = {tuple(k): v for k, v in st.get("labels", [])}
        self.store.styles = {tuple(k): dict(v) for k, v in st.get("styles", [])}
        self.store.next_uid = max([int(st.get("next_uid", 0))] + [e.uid + 1 for e in entries])
        self.store.color_i = int(st.get("color_i", len(entries)))
        self.store.highlight = []
        for c, ps in zip(self.controllers, st.get("panes", [])):
            ps = dict(ps)
            if "params" in ps:
                ps["params"] = set(ps["params"])
            for k in ("xlim", "ylim", "view_xlim", "view_ylim", "smith_frange", "panel_xy"):
                if ps.get(k) is not None:
                    ps[k] = tuple(ps[k])
            for pin in ps.get("pins", []) or []:
                pin["key"] = tuple(pin["key"])
            c.set_pane_state({k: v for k, v in ps.items() if k in c._PANE_FIELDS})
        if st.get("n_panes"):
            self.set_layout(int(st["n_panes"]))
        self.history.reset()
        self.refresh_files()

    # ============================================================ undo / redo
    def _snapshot(self) -> dict:
        import copy
        st = self.store
        ents = [(e, e.color, e.visible, e.linestyle, e.linewidth, e.data.name, e.data, getattr(e, "muted", False))
                for e in st.entries]
        panes = [c.pane_state() for c in self.controllers]

        def norm(v):
            return sorted(v) if isinstance(v, set) else v
        key = repr(([(id(e), c, v, ls, lw, n, id(d), m) for e, c, v, ls, lw, n, d, m in ents], sorted(st.labels.items()),
                    sorted((k, sorted(v.items())) for k, v in st.styles.items()),
                    [{k: norm(v) for k, v in p.items()} for p in panes], self.n_panes))
        return dict(key=key, entries=ents, labels=copy.deepcopy(st.labels), styles=copy.deepcopy(st.styles),
                    panes=panes, n_panes=self.n_panes)

    def _history_note(self, delay: int = 350):
        if self._restoring:
            return
        from PySide6.QtCore import QTimer
        self._hist_gen += 1
        gen = self._hist_gen

        def run():
            if gen == self._hist_gen and not self._restoring:
                self.history.note(self._snapshot())
        QTimer.singleShot(delay, run)

    def _history_flush(self):
        self._hist_gen += 1
        self.history.note(self._snapshot())

    @_guard
    def on_undo(self, *_):
        self._history_flush()
        self._apply_history(self.history.undo(), "되돌리기", "더 되돌릴 작업이 없습니다")

    @_guard
    def on_redo(self, *_):
        self._history_flush()
        self._apply_history(self.history.redo(), "다시 실행", "다시 실행할 작업이 없습니다")

    def _apply_history(self, snap, what, empty):
        import copy
        if snap is None:
            self.statusBar().showMessage(empty, 3000)
            return
        self._restoring = True
        try:
            st = self.store
            st.entries[:] = [e for e, *_ in snap["entries"]]
            for e, color, vis, ls, lw, name, data, muted in snap["entries"]:
                e.data, e.muted = data, muted
                e.color, e.visible, e.linestyle, e.linewidth, e.data.name = color, vis, ls, lw, name
            st.labels = copy.deepcopy(snap["labels"])
            st.styles = copy.deepcopy(snap["styles"])
            st.highlight = [u for u in st.highlight if any(e.uid == u for e in st.entries)]
            for c, ps in zip(self.controllers, snap["panes"]):
                c.set_pane_state(ps)
            if snap["n_panes"] != self.n_panes:
                self.set_layout(snap["n_panes"])
            self.refresh_files()
        finally:
            self._restoring = False
        self.statusBar().showMessage(f"↶ {what} (남은 되돌리기 {len(self.history.undo_stack)}번 · "
                                     f"다시 실행 {len(self.history.redo_stack)}번)", 3000)

    @_guard
    def on_colors_distinct(self, *_):
        uids = [e.uid for e in self.c.visible_entries]
        if uids:
            self.c.set_distinct_colors(uids)
            self.refresh_files()

    @_guard
    def on_colors_base(self, *_):
        uids = [e.uid for e in self.c.visible_entries]
        if uids:
            self.c.reset_colors(uids)
            self.refresh_files()

    @_guard
    def on_reset_all_axes(self, *_):
        for p in self.visible_panes:
            p.c.reset_ranges()
            p.render()
        self.sync_panel()
        self._history_note()
        self.statusBar().showMessage("모든 Plot의 축 범위를 기본값으로 되돌렸습니다", 3000)

    def add_paths(self, paths):
        paths = [p for p in paths if p]
        if not paths:
            return
        n, errors = self.c.add_files(paths)
        self.settings.setValue("last_dir", os.path.dirname(paths[0]))
        if errors:
            QMessageBox.warning(self, APP_TITLE, "일부 파일을 읽지 못했습니다:\n\n" + "\n\n".join(errors))
        self.refresh_files()

    @_guard
    def on_add(self, *_):
        paths, _f = QFileDialog.getOpenFileNames(self, "파일 추가", self.settings.value("last_dir", ""), FILE_FILTER)
        self.add_paths(paths)

    @_guard
    def on_example(self, *_):
        d = sparam_example_dir()
        self.add_paths([os.path.join(d, f) for f in sorted(os.listdir(d)) if is_supported(f)])

    @_guard
    def on_remove(self, *_):
        uids = self._selected_uids()
        if uids:
            self.c.remove(uids)
            self.refresh_files()

    @_guard
    def on_clear(self, *_):
        self.c.clear()
        self.refresh_files()

    @_guard
    def on_item_changed(self, item):
        if self._syncing:
            return
        self.c.set_visible(item.data(UID_ROLE), item.checkState() == Qt.Checked)
        self.render_all()

    @_guard
    def on_rename_file(self, item):
        self._rename_uid(item.data(UID_ROLE))

    @_guard
    def on_marker_value(self, pane, hit):
        """Double-click on a marker: type the exact frequency (vertical / Smith) or level (horizontal)."""
        import re
        c = pane.c
        kind, i = hit
        if kind == "v" and 0 <= i < len(c.markers_hz):
            text, ok = QInputDialog.getText(self, f"Marker M{i + 1}", "주파수 (예: 3.6 = 3.6 GHz, 3600MHz):",
                                            text=f"{c.markers_hz[i] / 1e9:g}")
            if not ok:
                return
            hz = parse_freq_input(str(text))
            if not hz or hz <= 0:
                QMessageBox.warning(self, APP_TITLE, "주파수를 인식할 수 없습니다. 예: 3.6, 3.6GHz, 3600MHz")
                return
            c.markers_hz[i] = float(hz)
        elif kind == "h" and 0 <= i < len(c.h_markers):
            unit = "°" if c.fmt == "phase" else "dB"
            text, ok = QInputDialog.getText(self, f"Marker H{i + 1}", f"값 ({unit}):", text=f"{c.h_markers[i]:g}")
            if not ok:
                return
            m = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", str(text))
            if not m:
                QMessageBox.warning(self, APP_TITLE, "숫자를 입력해주세요. 예: -10")
                return
            c.h_markers[i] = float(m.group(0))
        else:
            return
        pane.redraw_markers()
        self.on_pane_changed(pane, "markers")

    @_guard
    def on_m_key(self, *_):
        if not self.target_pane().add_pin():
            self.statusBar().showMessage("곡선 가까이에 마우스를 두고 M을 누르세요", 3000)

    @_guard
    def on_legend_rename(self, pane, i):
        text, ok = QInputDialog.getText(self, "Legend 이름 변경",
                                        "새 이름 (비우면 기본 이름으로 복원):", text=pane.current_label(i))
        if ok:
            pane.rename_series(i, text)

    # ============================================================ active-pane settings
    @_guard
    def on_layout_changed(self, index):
        if index >= 0:
            self.set_layout(self.layout_combo.itemData(index))

    @_guard
    def on_param(self, param, on):
        if self._syncing:
            return
        self.c.set_param(param, on)
        self.active.render()
        self._fill_table()
        self._status()

    @_guard
    def on_fmt(self, *_):
        if self._syncing:
            return
        fmt = next((k for k, rb in self.fmt_radios.items() if rb.isChecked()), self.c.fmt)
        if fmt != self.c.fmt:
            self.c.set_format(fmt)
            self.active.render()
            self.sync_panel()
            self._status()

    @_guard
    def on_apply_range(self, *_):
        try:
            x = (float(self.xmin_edit.text()), float(self.xmax_edit.text()))
            if self.c.is_smith:
                errs = self.c.set_smith_frange(*x)
            else:
                y = (float(self.ymin_edit.text()), float(self.ymax_edit.text()))
                errs = self.c.set_ranges(x, y)
        except ValueError:
            QMessageBox.warning(self, APP_TITLE, "숫자만 입력할 수 있습니다.")
            return
        if errs:
            QMessageBox.warning(self, APP_TITLE, "\n".join(errs))
        self.active.render()
        self.sync_panel()

    @_guard
    def on_auto(self, *_):
        self.c.auto_scale()
        self.active.render()
        self.sync_panel()

    @_guard
    def on_fit(self, *_):
        pane = self.target_pane()
        pane.c.fit()
        pane.render()
        if pane is self.active:
            self.sync_panel()

    @_guard
    def on_reset(self, *_):
        self.c.reset_ranges()
        self.active.render()
        self.sync_panel()

    @_guard
    def on_values_toggle(self, on):
        if self._syncing:
            return
        self.c.show_marker_values = bool(on)
        self.active.redraw_markers()

    @_guard
    def on_step(self, *_):
        if self._syncing:
            return
        try:
            v = float(self.step_edit.text() or 0)
            d = float(self.db_step_edit.text() or 0)
        except ValueError:
            self.step_edit.setText(fmt_num(self.c.snap_ghz, 4))
            self.db_step_edit.setText(fmt_num(self.c.snap_db, 3))
            return
        for ctl in self.controllers:      # same steps for every pane
            ctl.snap_ghz, ctl.snap_db = max(0.0, v), max(0.0, d)

    @_guard
    def on_v_key(self, *_):
        self.target_pane().add_v_marker()

    @_guard
    def on_h_key(self, *_):
        self._h_on(self.target_pane())

    @_guard
    def on_delete_marker(self, *_):
        self.target_pane().delete_marker()

    @_guard
    def on_add_marker(self, *_):
        hz = parse_freq_input(self.marker_edit.text())
        if hz is None:
            QMessageBox.warning(self, APP_TITLE, "주파수를 인식할 수 없습니다. 예: 3.6, 3.6GHz, 3600MHz")
            return
        self.c.add_marker(hz)
        self.marker_edit.setText("")
        self.active.redraw_markers()
        self._fill_table()

    @_guard
    def on_clear_markers(self, *_):
        self.c.clear_markers()
        self.active.redraw_markers()
        self._fill_table()

    def on_pane_key(self, pane, key):
        """Keys that reached the plot canvas directly (e.g. Korean IME mode)."""
        self.set_active(pane)
        if key == "v":
            pane.add_v_marker()
        elif key == "h":
            self._h_on(pane)
        elif key == "f":
            pane.c.fit(); pane.render(); self.sync_panel()
        elif key in ("delete", "backspace"):
            pane.delete_marker()
        elif key == "m":
            pane.add_pin()
        elif key == "escape":
            self.on_list_clear()
            self.file_list.clearSelection()

    def _h_on(self, pane):
        if not pane.add_h_marker() and pane.c.is_smith:
            self.statusBar().showMessage("Smith Chart에서는 가로 Marker를 사용할 수 없습니다.", 4000)

    # ============================================================ context menus
    def _style_menu(self, parent: QMenu, apply, current_color: str, acts: dict):
        """Colour / line style / width submenus. apply(color=, linestyle=, linewidth=)."""
        cm = parent.addMenu("색상")
        for col in COLORS[:10] + ["#000000"]:
            a = cm.addAction(_color_icon(col), col)
            a.triggered.connect(lambda _=False, col=col: apply(color=col))
            acts[f"color:{col}"] = a
        cm.addSeparator()
        more = cm.addAction("다른 색…")
        more.triggered.connect(lambda _=False: self._pick_color(current_color, apply))
        acts["color:more"] = more
        sm = parent.addMenu("선 종류")
        for k, name in STYLE_NAMES.items():
            a = sm.addAction(name)
            a.triggered.connect(lambda _=False, k=k: apply(linestyle=LINESTYLES[k]))
            acts[f"style:{k}"] = a
        wm = parent.addMenu("선 굵기")
        for k, name in WIDTH_NAMES.items():
            a = wm.addAction(name)
            a.triggered.connect(lambda _=False, k=k: apply(linewidth=LINEWIDTHS[k]))
            acts[f"width:{k}"] = a

    def _pick_color(self, current: str, apply):
        col = QColorDialog.getColor(QColor(current), self, "색상 선택")
        if col.isValid():
            apply(color=col.name())

    def build_file_menu(self, uid):
        """Right-click menu for one file (uid) or several (list of uids)."""
        uids = list(uid) if isinstance(uid, (list, tuple)) else [uid]
        multi = len(uids) > 1
        e = self.c.entry(uids[0])
        menu, acts = QMenu(self), {}
        if multi:
            t = menu.addAction(f"선택: 파일 {len(uids)}개")
            t.setEnabled(False)
            acts["rename_many"] = menu.addAction(f"선택한 {len(uids)}개 이름 변경…")
            acts["rename_many"].triggered.connect(lambda _=False: self._rename_many(uids))
        else:
            acts["rename"] = menu.addAction("이름 변경…")
            acts["rename"].triggered.connect(lambda _=False: self._rename_uid(uids[0]))
        menu.addSeparator()

        def apply(**kw):
            for u in uids:
                self.c.set_file_style(u, **kw)
            self.refresh_files()
        if multi:
            cm = menu.addMenu("색상 (여러 개)")
            acts["color:distinct"] = cm.addAction(f"각각 다른 색으로 ({len(uids)}개)")
            acts["color:distinct"].triggered.connect(lambda _=False: (self.c.set_distinct_colors(uids), self.refresh_files()))
            acts["color:auto"] = cm.addAction("처음 색으로")
            acts["color:auto"].triggered.connect(lambda _=False: (self.c.reset_colors(uids), self.refresh_files()))
        self._style_menu(menu, apply, e.color if e else "#1f77b4", acts)
        acts["reset"] = menu.addAction("기본 스타일로")
        acts["reset"].triggered.connect(lambda _=False: ([self.c.reset_style(u) for u in uids], self.refresh_files()))
        menu.addSeparator()
        acts["copy"] = menu.addAction(f"복사 (Ctrl+C) — {len(uids)}개" if multi else "복사 (Ctrl+C)")
        acts["copy"].triggered.connect(lambda _=False: self._copy_uids(uids))
        acts["paste"] = menu.addAction("붙여넣기 (Ctrl+V)")
        acts["paste"].triggered.connect(lambda _=False: self.on_paste_files())
        acts["only"] = menu.addAction("이것들만 보기" if multi else "이것만 보기")
        acts["only"].triggered.connect(lambda _=False: (self.c.show_only(uids), self.refresh_files()))
        acts["add"] = menu.addAction(f"선택한 {len(uids)}개도 같이 보기 (추가)" if multi else "이것도 같이 보기 (추가)")
        acts["add"].triggered.connect(lambda _=False: (self.c.show_also(uids), self.refresh_files()))
        all_muted = all(getattr(self.c.entry(u), "muted", False) for u in uids if self.c.entry(u))
        acts["eye"] = menu.addAction(("👁 다시 보이기 (항상 숨김 해제)" if all_muted else "👁 항상 숨기기 (검색·이것들만 보기에도 안 나옴)")
                                     + (f"  {len(uids)}개" if multi else ""))
        acts["eye"].triggered.connect(lambda _=False: (self.c.set_muted(uids, not all_muted), self.refresh_files()))
        acts["hide"] = menu.addAction(f"선택한 {len(uids)}개 숨기기 (체크 해제)" if multi else "숨기기 (체크 해제)")
        acts["hide"].triggered.connect(lambda _=False: (self.c.hide(uids), self.refresh_files()))
        if not multi:
            acts["toggle"] = menu.addAction("숨기기" if e.visible else "표시")
            acts["toggle"].triggered.connect(lambda _=False: (self.c.set_visible(uids[0], not e.visible), self.refresh_files()))
        acts["remove"] = menu.addAction(f"{len(uids)}개 목록에서 삭제" if multi else "목록에서 삭제")
        acts["remove"].triggered.connect(lambda _=False: (self.c.remove(uids), self.refresh_files()))
        return menu, acts

    @_guard
    def on_eye(self, uid, on=None):
        e = self.c.entry(uid)
        if e is None:
            return
        uids = [uid]                                # ONLY the clicked row (several at once: right-click menu)
        mute = (not getattr(e, "muted", False)) if on is None else bool(on)
        self.c.set_muted(uids, mute)
        self.refresh_files()
        self.statusBar().showMessage(f"👁 {len(uids)}개 " + ("항상 숨김 (눈 아이콘 다시 클릭 = 보이기)" if mute else "다시 보이기"), 5000)

    @_guard
    def on_lines_default(self, *_):
        self.c.reset_line_styles([e.uid for e in self.c.visible_entries])
        self.refresh_files()

    @_guard
    def on_open_file_data(self, uid, f_ghz=None):
        """Double-click a file (list) or its curve (plot): Freq + dB / phase columns, editable."""
        import numpy as np
        from ui import data_editor
        from sparam.core.models import SParamData
        e = self.c.entry(uid)
        if e is None:
            return
        d = e.data
        cols = [("db", p) for p in PARAMS if p in d.db] + [("phase", p) for p in PARAMS if p in d.phase]
        headers = ["Freq (GHz)"] + [f"{PARAM_LABEL[p]} ({'dB' if k == 'db' else '°'})" for k, p in cols]
        rows = [[float(d.freq_hz[i]) / 1e9] + [float((d.db if k == "db" else d.phase)[p][i]) for k, p in cols]
                for i in range(len(d.freq_hz))]
        focus = int(np.argmin(np.abs(d.freq_hz / 1e9 - f_ghz))) if (f_ghz is not None and len(d.freq_hz)) else None
        dlg = data_editor.DataEditorDialog(
            f"데이터 보기 / 수정 — {d.name}", rows, headers=headers, name=d.name, focus_row=focus, parent=self,
            mark_cols={0: "#dbeafe"},
            note="셀을 직접 고치거나 Excel에서 복사한 값을 Ctrl+V로 붙여넣으세요. 행 추가/삭제 가능. "
                 "OK를 눌러야 적용되고, Ctrl+Z로 되돌릴 수 있습니다. (원본 파일은 바뀌지 않음 — 프로젝트에 저장)")
        if not dlg.exec():
            return
        name, new_rows = dlg.values()
        norm = [[data_editor.to_cell(data_editor.fmt_cell(v)) for v in row] for row in rows]
        if new_rows != norm:
            good = [r for r in new_rows if r and isinstance(r[0], float)]
            if not good:
                QMessageBox.warning(self, APP_TITLE, "주파수(첫 열)가 있는 행이 없어 적용하지 않았습니다.")
                return

            def col(j):
                return np.array([float(r[j]) if j < len(r) and isinstance(r[j], float) else np.nan for r in good])
            freq = col(0) * 1e9
            db = {p: col(j + 1) for j, (k, p) in enumerate(cols) if k == "db"}
            ph = {p: col(j + 1) for j, (k, p) in enumerate(cols) if k == "phase"}
            e.data = SParamData(name or d.name, d.path, freq, db, ph, d.kind, d.z0,
                                list(d.notes) + ["데이터 수정됨 (프로그램에서)"])
        elif name and name != d.name:
            self.c.rename_file(uid, name)
        if name and name != e.data.name:
            e.data.name = name
        self.refresh_files()
        self.statusBar().showMessage(f"✔ '{e.data.name}' 데이터 적용 ({len(e.data.freq_hz)}점) — 저장하려면 프로젝트 저장", 6000)

    @_guard
    def on_toggle_many(self, uids, on: bool):
        for u in uids:
            self.c.set_visible(u, on)
        self.refresh_files()

    def _copy_uids(self, uids):
        for i in range(self.file_list.count()):
            it = self.file_list.item(i)
            it.setSelected(it.data(UID_ROLE) in uids)
        self.on_copy_files()

    def _rename_many(self, uids):
        names = [self.c.entry(u).data.name for u in uids if self.c.entry(u)]
        start = names[0] if len(set(names)) == 1 else ""
        text, ok = QInputDialog.getText(self, f"이름 변경 ({len(uids)}개)", f"선택한 {len(uids)}개 파일의 새 이름:", text=start)
        text = str(text).strip() if ok else ""
        if text:
            for u in uids:
                self.c.rename_file(u, text)
            self.refresh_files()

    def build_legend_menu(self, pane, i: int):
        key = pane.renderer.series_keys[i]
        menu, acts = QMenu(self), {}
        acts["rename"] = menu.addAction("이 곡선 이름 변경…")
        acts["rename"].triggered.connect(lambda _=False: self.on_legend_rename(pane, i))
        menu.addSeparator()

        def apply(**kw):
            self.c.set_curve_style(key, **kw)
            self.render_all()
        e = self.c.entry(key[0])
        self._style_menu(menu, apply, self.c.style_for(e, key[1])[0] if e else "#1f77b4", acts)
        acts["reset"] = menu.addAction("기본 스타일로")
        acts["reset"].triggered.connect(lambda _=False: (self.c.reset_style(key[0], key[1]), self.render_all()))
        return menu, acts

    def build_panel_menu(self, pane):
        c = pane.c
        menu, acts = QMenu(self), {}
        acts["hide"] = menu.addAction("값 상자 숨기기")
        acts["hide"].triggered.connect(lambda _=False: self._set_values(pane, False))
        if c.panel_collapsed:
            acts["expand"] = menu.addAction("펼치기 (곡선별 값)")
            acts["expand"].triggered.connect(lambda _=False: self._set_panel(pane, collapsed=False))
        else:
            acts["collapse"] = menu.addAction("접기 (Marker 주파수만)")
            acts["collapse"].triggered.connect(lambda _=False: self._set_panel(pane, collapsed=True))
        lm = menu.addMenu("위치")
        for loc in PANEL_LOCS:
            a = lm.addAction(("● " if loc == c.panel_loc else "   ") + LOC_NAMES[loc])
            a.triggered.connect(lambda _=False, loc=loc: self._set_panel(pane, loc=loc))
            acts[f"loc:{loc}"] = a
        menu.addSeparator()
        acts["copy"] = menu.addAction("Marker 값 복사 (Excel)")
        acts["copy"].triggered.connect(lambda _=False: self._copy_table_of(pane))
        acts["clear"] = menu.addAction("Marker 모두 삭제")
        acts["clear"].triggered.connect(lambda _=False: self._clear_markers_of(pane))
        return menu, acts

    def build_marker_menu(self, pane, hit):
        c = pane.c
        menu, acts = QMenu(self), {}
        acts["delete"] = menu.addAction("이 Marker 삭제")
        acts["delete"].triggered.connect(lambda _=False: (c.remove_marker(*hit), pane.redraw_markers(),
                                                          self.on_pane_changed(pane, "markers")))
        acts["values"] = menu.addAction("값 상자 숨기기" if c.show_marker_values else "값 상자 보이기")
        acts["values"].triggered.connect(lambda _=False: self._set_values(pane, not c.show_marker_values))
        if c.show_marker_values:
            acts["fold"] = menu.addAction("펼치기" if c.panel_collapsed else "접기")
            acts["fold"].triggered.connect(lambda _=False: self._set_panel(pane, collapsed=not c.panel_collapsed))
        acts["clear"] = menu.addAction("Marker 모두 삭제")
        acts["clear"].triggered.connect(lambda _=False: self._clear_markers_of(pane))
        return menu, acts

    def build_pin_menu(self, pane, i: int):
        menu, acts = QMenu(self), {}
        acts["delete"] = menu.addAction("이 말풍선 삭제")
        acts["delete"].triggered.connect(lambda _=False: (pane.c.remove_pin(i), pane.redraw_markers(),
                                                          self.on_pane_changed(pane, "markers")))
        acts["clear"] = menu.addAction("말풍선 모두 삭제")
        acts["clear"].triggered.connect(lambda _=False: (pane.c.pins.clear(), pane.redraw_markers(),
                                                         self.on_pane_changed(pane, "markers")))
        return menu, acts

    def _set_values(self, pane, on: bool):
        pane.c.show_marker_values = on
        pane.redraw_markers()
        if pane is self.active:
            self.sync_panel()

    def _set_panel(self, pane, collapsed=None, loc=None):
        if collapsed is not None:
            pane.c.panel_collapsed = collapsed
        if loc:
            pane.c.panel_loc = loc
            pane.c.panel_xy = None
        pane.redraw_markers()

    def _clear_markers_of(self, pane):
        pane.c.clear_markers()
        pane.redraw_markers()
        self.on_pane_changed(pane, "markers")

    def _copy_table_of(self, pane):
        QGuiApplication.clipboard().setText(pane.c.marker_table_tsv())

    def _rename_uid(self, uid):
        e = self.c.entry(uid)
        if e is None:
            return
        text, ok = QInputDialog.getText(self, "파일 이름 변경", "Legend에 쓸 이름:", text=e.data.name)
        if ok and text.strip():
            self.c.rename_file(uid, text)
            self.refresh_files()

    PLOT_MENU_MAX = 40

    def build_plot_menu(self, pane, key=None):
        """Right-click in the plot (no drag; right-drag = box zoom): what to plot and how."""
        self.set_active(pane)
        menu, acts = QMenu(self), {}
        c = self.c
        if key:
            e = c.entry(key[0])
            if e is not None:
                head = menu.addAction(f"곡선: {e.data.name} · {PARAM_LABEL.get(key[1], key[1])}")
                head.setEnabled(False)
                sub, sub_acts = self.build_file_menu(key[0])
                sub.setTitle("이 파일")
                menu.addMenu(sub)
                acts["curve"] = sub
                acts.update({"curve:" + k: a for k, a in sub_acts.items()})
                menu.addSeparator()
        pm = menu.addMenu("Parameters")
        for p in PARAMS:
            a = pm.addAction(PARAM_LABEL[p])
            a.setCheckable(True)
            a.setChecked(p in c.params)
            a.triggered.connect(lambda on=False, p=p: self.param_checks[p].setChecked(bool(on)))
            acts["param:" + p] = a
        fm = menu.addMenu("Format")
        for k, rb in self.fmt_radios.items():
            a = fm.addAction(rb.text())
            a.setCheckable(True)
            a.setChecked(k == c.fmt)
            a.triggered.connect(lambda _=False, rb=rb: rb.setChecked(True))
            acts["fmt:" + k] = a
        dm = menu.addMenu("파일 표시")
        entries = list(self.store.entries)
        for e in entries[:self.PLOT_MENU_MAX]:
            a = dm.addAction(e.data.name)
            a.setCheckable(True)
            a.setChecked(bool(e.visible) and not getattr(e, "muted", False))
            a.setEnabled(not getattr(e, "muted", False))
            a.triggered.connect(lambda on=False, u=e.uid: self.set_file_shown(u, bool(on)))
            acts[f"show:{e.uid}"] = a
        if len(entries) > self.PLOT_MENU_MAX:
            more = dm.addAction(f"… 외 {len(entries) - self.PLOT_MENU_MAX}개 (Files 목록에서 선택)")
            more.setEnabled(False)
        acts["all"] = menu.addAction("모두 표시")
        acts["all"].triggered.connect(lambda _=False: self.set_all_files_shown(True))
        acts["none"] = menu.addAction("모두 해제")
        acts["none"].triggered.connect(lambda _=False: self.set_all_files_shown(False))
        n_sel = len(self._selected_uids())
        acts["selected"] = menu.addAction(f"선택 Plot ({n_sel}개)" if n_sel else "선택 Plot (목록에서 선택 없음)")
        acts["selected"].setEnabled(bool(n_sel))
        acts["selected"].triggered.connect(lambda _=False: self.on_selected_show_only())
        if self._filter_terms():
            acts["filtered"] = menu.addAction("검색 결과만 Plot")
            acts["filtered"].triggered.connect(lambda _=False: self.on_filter_show_only())
        menu.addSeparator()
        mk = menu.addMenu("Marker")
        for k, label, fn in (("v", "V Marker 여기 (V)", lambda: pane.add_v_marker()),
                             ("h", "H Marker 여기 (H)", lambda: self._h_on(pane)),
                             ("m", "M 말풍선 여기 (M)", lambda: pane.add_pin()),
                             ("clear", "Marker 모두 삭제", lambda: self.on_clear_markers())):
            acts["mk:" + k] = mk.addAction(label)
            acts["mk:" + k].triggered.connect(lambda _=False, f=fn: f())
        ax_m = menu.addMenu("축")
        for k, label, fn in (("fit", "Fit (F)", self.on_fit), ("auto", "Auto Scale", self.on_auto),
                             ("reset", "Reset", self.on_reset), ("reset_all", "Axis Reset (모든 Plot)", self.on_reset_all_axes)):
            acts[k] = ax_m.addAction(label)
            acts[k].triggered.connect(lambda _=False, f=fn: f())
        st = menu.addMenu("색 / 선")
        for k, label, fn in (("distinct", "각각 다른 색", self.on_colors_distinct),
                             ("base", "기본 색", self.on_colors_base), ("lines", "선 기본 스타일", self.on_lines_default)):
            acts[k] = st.addAction(label)
            acts[k].triggered.connect(lambda _=False, f=fn: f())
        menu.addSeparator()
        for k, label, fn in (("copy", "Plot 복사", self.on_copy_plot), ("copy_all", "모든 Plot 복사", self.on_copy_all),
                             ("png", "PNG로 저장… (Ctrl+Alt+S)", self.on_save_png)):
            acts[k] = menu.addAction(label)
            acts[k].triggered.connect(lambda _=False, f=fn: f())
        return menu, acts

    @_guard
    def set_file_shown(self, uid, on: bool):
        self.c.set_visible(uid, on)
        self.refresh_files()
        self._history_note()

    @_guard
    def set_all_files_shown(self, on: bool):
        for e in self.store.entries:
            if not getattr(e, "muted", False):
                e.visible = bool(on)
        self.refresh_files()
        self._history_note()

    @_guard
    def on_plot_context(self, pane, what, event=None):
        kind, arg = what
        if kind == "pin":
            menu, _ = self.build_pin_menu(pane, arg)
        elif kind == "panel":
            menu, _ = self.build_panel_menu(pane)
        elif kind == "legend":
            menu, _ = self.build_legend_menu(pane, arg)
        elif kind == "plot":
            menu, _ = self.build_plot_menu(pane, arg)
        else:
            menu, _ = self.build_marker_menu(pane, arg)
        menu.exec(QCursor.pos())

    @_guard
    def on_file_context(self, pos):
        item = self.file_list.itemAt(pos)
        if item is None:
            return
        uid = item.data(UID_ROLE)
        sel = self._selected_uids()
        menu, _ = self.build_file_menu(sel if (uid in sel and len(sel) > 1) else uid)
        menu.exec(self.file_list.viewport().mapToGlobal(pos))

    # ============================================================ export
    @_guard
    def on_copy_table(self, *_):
        QGuiApplication.clipboard().setText(self.c.marker_table_tsv())
        self.statusBar().showMessage("✔ Marker Table을 복사했습니다. Excel에 Ctrl+V 하세요.", 5000)

    @_guard
    def on_copy_plot(self, *_):
        self.active.canvas.draw()
        if copy_png_to_clipboard(figure_png_bytes(self.active.figure)):
            self.statusBar().showMessage(f"✔ Plot {self.active.index + 1}을 복사했습니다. PowerPoint 등에 Ctrl+V", 5000)

    def compose_all_png(self) -> bytes:
        """All visible panes in their on-screen grid, as one PNG."""
        from PIL import Image
        rows, cols = GRID[self.n_panes]
        imgs = []
        for p in self.visible_panes:
            p.canvas.draw()
            imgs.append(Image.open(io.BytesIO(figure_png_bytes(p.figure))).convert("RGB"))
        w, h = max(i.width for i in imgs), max(i.height for i in imgs)
        sheet = Image.new("RGB", (w * cols, h * rows), "white")
        for k, im in enumerate(imgs):
            sheet.paste(im, ((k % cols) * w, (k // cols) * h))
        buf = io.BytesIO()
        sheet.save(buf, format="PNG")
        return buf.getvalue()

    @_guard
    def on_copy_all(self, *_):
        if copy_png_to_clipboard(self.compose_all_png()):
            self.statusBar().showMessage(f"✔ Plot {self.n_panes}개를 한 장으로 복사했습니다.", 5000)

    @_guard
    def on_save_png(self, *_):
        start = self.settings.value("save_dir", "")
        name = self.c.default_export_name()
        default = os.path.join(start, name) if start else name
        path, _f = QFileDialog.getSaveFileName(self, "Save PNG (선택된 Plot)", default, "PNG Image (*.png)")
        if path:
            self.active.canvas.draw()
            saved = save_png(self.active.figure, path)
            self.settings.setValue("save_dir", os.path.dirname(saved))
            self.statusBar().showMessage(f"✔ 저장 완료: {saved}", 5000)

    # ======================================================= drag & drop
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        self.add_paths([u.toLocalFile() for u in event.mimeData().urls()])
