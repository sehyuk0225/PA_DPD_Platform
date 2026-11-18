"""Main window — thin Qt view over app.controller.AppController."""
from __future__ import annotations

import os
import traceback
from typing import Optional

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import (QListWidget, QListWidgetItem, QMenu, QTableWidget, QTableWidgetItem, QAbstractItemView,
                               QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFrame,
                               QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel,
                               QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
                               QRadioButton, QSizePolicy, QSplitter, QVBoxLayout, QWidget)

# matplotlib must be imported after PySide6 so it binds to the same Qt
import matplotlib

from app.controller import AppController
from core.models import Level
from plotting.export import figure_png_bytes, save_png
from ui.paste_dialog import PasteDialog
from ui.plot_view import PlotView
from ui.clipboard import copy_png_to_clipboard
from ui.column_mapping_dialog import ColumnMappingDialog
from ui.condition_panel import ConditionPanel
from utils.freq import format_freq, parse_frequency
from utils.numeric import fmt_num
from utils.paths import example_measurement_path

# V / H / F / arrows belong to this app: drop matplotlib's single-key shortcuts
for _k in ("keymap.fullscreen", "keymap.home", "keymap.back", "keymap.forward", "keymap.pan", "keymap.zoom",
           "keymap.save", "keymap.grid", "keymap.grid_minor", "keymap.yscale", "keymap.xscale", "keymap.quit",
           "keymap.copy"):
    if _k in matplotlib.rcParams:
        matplotlib.rcParams[_k] = []

from version import VERSION_LABEL
APP_TITLE = f"PA Measurement Analyzer {VERSION_LABEL}"
NAME_ROLE = 256  # Qt.UserRole
FILTER_ROLE = 258  # text the Datasets search matches against (name + FREQ)
LOC_NAMES = {"upper left": "왼쪽 위", "upper right": "오른쪽 위", "lower left": "왼쪽 아래", "lower right": "오른쪽 아래"}
EXCEL_FILTER = ("Excel / CSV / 프로젝트 (*.xlsx *.xlsm *.xls *.csv *.txt *.pam);;"
                "PA Analyzer 프로젝트 (*.pam);;All files (*.*)")
PROJECT_FILTER = "PA Analyzer 프로젝트 (*.pam)"
_LEVEL_PREFIX = {Level.ERROR: "❌", Level.WARNING: "⚠", Level.INFO: "ℹ"}


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
    """Slot decorator: show an error dialog instead of crashing on unexpected exceptions."""
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except Exception as exc:  # pragma: no cover - GUI safety net
            traceback.print_exc()
            QMessageBox.critical(self, APP_TITLE, f"예기치 않은 오류가 발생했습니다:\n{exc}")
    wrapper.__name__ = fn.__name__
    return wrapper


class MainWindow(QMainWindow):
    def __init__(self, controller: AppController, settings: Optional[QSettings] = None):
        super().__init__()
        self.c = controller
        self.settings = settings or QSettings("PA-Measurement-Analyzer", "PA-Measurement-Analyzer")
        self._updating = False
        self._refresh_pending = False
        self._filter = ""
        self._scroll_to_current = False
        self._sel_gen = 0
        # other tabs saved in the same project: name -> (has_data(), export_state(), import_state(dict))
        self.project_extras = {}
        self._plot_picked = []                     # curves clicked in a plot (Delete there = take them off)
        from app.history import History
        self.history = History()
        self._restoring = False
        self._hist_gen = 0
        self.n_panes = 2
        self.c.set_active_pane(0)
        self.c.highlight_selected = str(self.settings.value("highlight_selected", "true")).lower() not in ("false", "0")
        saved = str(self.settings.value("conditions", "") or "")
        if saved:
            from core.quantities import CONDITION_FIELDS
            keys = [k for k in saved.split(",") if k in CONDITION_FIELDS]
            if keys:
                self.c.condition_keys = keys

        self.setWindowTitle(APP_TITLE)
        self.resize(1500, 920)
        self.setMinimumSize(720, 480)
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        self._base_pt = (app.font().pointSizeF() if app is not None else 9.0) or 9.0
        self._ui_scale = 1.0
        self._font_timer = QTimer(self)
        self._font_timer.setSingleShot(True)
        self._font_timer.setInterval(150)
        self._font_timer.timeout.connect(self.apply_ui_scale)
        self.setAcceptDrops(True)

        self._build_ui()
        self._build_actions()
        self._refresh_sim_combo()
        self.activate_view(self.views[0])
        self.set_layout(getattr(self.c, "n_panes", 2) or 2)
        self.refresh(rerender=True)

    # ------------------------------------------------------------ active plot shortcuts
    @property
    def view(self) -> PlotView:
        return self.views[self.c.active_pane if hasattr(self.c, "active_pane") else 0]

    @property
    def figure(self):
        return self.view.figure

    @property
    def canvas(self):
        return self.view.canvas

    @property
    def renderer(self):
        return self.view.renderer

    @property
    def hover(self):
        return self.view.hover

    @property
    def visible_views(self):
        return self.views[: self.n_panes]

    def target_view(self) -> PlotView:
        return next((v for v in self.visible_views if v.mouse is not None), self.view)

    # ================================================================== UI
    def _build_ui(self):
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 6)
        root.setSpacing(6)

        # ---- top bar: file + frequency + simulation --------------------------
        top = QGridLayout()
        top.setHorizontalSpacing(8)
        top.addWidget(QLabel("Measurement File:"), 0, 0)
        self.file_edit = QLineEdit()
        self.file_edit.setReadOnly(True)
        self.file_edit.setPlaceholderText("Excel 파일을 열거나 창에 끌어다 놓으세요 (Ctrl+O)")
        top.addWidget(self.file_edit, 0, 1, 1, 4)
        self.open_btn = QPushButton("Open…")
        self.open_btn.clicked.connect(self.on_open)
        top.addWidget(self.open_btn, 0, 5)
        self.example_btn = QPushButton("Example")
        self.example_btn.setToolTip("저장소에 포함된 Example Measurement 파일 열기")
        self.example_btn.clicked.connect(self.on_open_example)
        top.addWidget(self.example_btn, 0, 6)

        top.addWidget(QLabel("선택 Dataset:"), 1, 0)
        self.current_label = QLabel("-")
        self.current_label.setStyleSheet("font-weight:bold;")
        self.current_label.setMinimumWidth(240)
        top.addWidget(self.current_label, 1, 1)
        self.freq_btn = QPushButton("FREQ 지정…")
        self.freq_btn.setToolTip("FREQ 자동 인식이 실패했거나 잘못된 경우 직접 입력")
        self.freq_btn.clicked.connect(self.on_set_freq)
        top.addWidget(self.freq_btn, 1, 2)
        self.map_btn = QPushButton("Column Mapping…")
        self.map_btn.clicked.connect(self.on_column_mapping)
        top.addWidget(self.map_btn, 1, 3)

        sim_box = QHBoxLayout()
        sim_box.addWidget(QLabel("Simulation:"))
        self.sim_combo = QComboBox()
        self.sim_combo.setMinimumWidth(170)
        self.sim_combo.currentIndexChanged.connect(self.on_sim_changed)
        sim_box.addWidget(self.sim_combo)
        self.sim_dir_btn = QPushButton("Sim Folder…")
        self.sim_dir_btn.setToolTip("Simulation Data 폴더 선택 (하위 폴더 = Simulation Set)")
        self.sim_dir_btn.clicked.connect(self.on_sim_folder)
        self.sim_files_btn = QPushButton("Sim 파일…")
        self.sim_files_btn.setToolTip("Simulation 메모장 파일(.txt)을 직접 선택 (예: 3B, 3X, 3P, 3K)\n"
                                      "열: 주파수(Freq), Pout, PAE, Gain — 파일 하나 = Simulation Set 하나")
        self.sim_files_btn.clicked.connect(self.on_sim_files)
        sim_box.addWidget(self.sim_dir_btn)
        sim_box.addWidget(self.sim_files_btn)
        top.addLayout(sim_box, 1, 4, 1, 3)

        data_box = QHBoxLayout()
        self.paste_btn = QPushButton("📋 붙여넣기")
        self.paste_btn.setToolTip("Excel에서 데이터 범위를 복사(Ctrl+C)한 뒤 누르면 새 Dataset으로 추가되고 바로 그려집니다.\n"
                                  "조건(FREQ, VGS…) 셀과 Header까지 함께 복사하면 자동 인식됩니다.")
        self.paste_btn.clicked.connect(self.on_paste)
        self.rename_btn = QPushButton("이름…")
        self.rename_btn.clicked.connect(self.on_rename_sheet)
        self.delete_btn = QPushButton("삭제")
        self.delete_btn.clicked.connect(self.on_delete_sheet)
        self.save_proj_btn = QPushButton("💾 프로젝트 저장")
        self.save_proj_btn.setToolTip("Ctrl+S: 지금 열린 프로젝트에 바로 덮어쓰기 (처음이면 저장 위치 선택)\n"
                                      "다른 이름으로 저장: Ctrl+Shift+S · S-Parameter 탭도 같은 프로젝트에 함께 저장")
        self.save_proj_btn.clicked.connect(self.on_save_project_quick)
        for b in (self.paste_btn, self.rename_btn, self.delete_btn):
            data_box.addWidget(b)
        data_box.addStretch(1)
        data_box.addWidget(self.save_proj_btn)
        top.addWidget(QLabel("Data:"), 2, 0)
        top.addLayout(data_box, 2, 1, 1, 6)
        top.setColumnStretch(4, 1)
        root.addLayout(top)

        line = QFrame(); line.setFrameShape(QFrame.HLine); line.setFrameShadow(QFrame.Sunken)
        root.addWidget(line)

        # ---- middle: left controls | right condition + plot --------------------
        splitter = QSplitter(Qt.Horizontal)
        from PySide6.QtWidgets import QScrollArea
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        left_scroll.setFrameShape(QFrame.NoFrame)
        left_scroll.setWidget(self._build_left_panel())
        left_scroll.setMinimumWidth(210)
        left_scroll.setMaximumWidth(1100)
        splitter.addWidget(left_scroll)
        splitter.addWidget(self._build_plot_area())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([270, 1090])
        from ui.splitter_util import wide_handle
        wide_handle(splitter)
        root.addWidget(splitter, 1)

        # ---- bottom: export buttons ---------------------------------------------
        bottom = QHBoxLayout()
        self.copy_btn = QPushButton("📋  Copy Plot (화면 전체)")
        self.copy_btn.setToolTip("화면에 띄운 Plot 전체를 한 장으로 복사 (Ctrl+Shift+C) → PowerPoint 등에 Ctrl+V")
        self.copy_btn.setMinimumHeight(34)
        self.copy_btn.clicked.connect(self.on_copy_plot)
        self.copy_one_btn = QPushButton("선택 Plot만 복사")
        self.copy_one_btn.setToolTip("파란 테두리 Plot 하나만 복사")
        self.copy_one_btn.setMinimumHeight(34)
        self.copy_one_btn.clicked.connect(self.on_copy_one)
        self.save_btn = QPushButton("💾  Save PNG (화면 전체)")
        self.save_btn.setToolTip("화면에 띄운 Plot 전체를 PNG로 저장 (Ctrl+Alt+S)")
        self.save_btn.setMinimumHeight(34)
        self.save_btn.clicked.connect(self.on_save_png)
        bottom.addWidget(self.copy_btn, 1)
        bottom.addWidget(self.copy_one_btn, 0)
        bottom.addStretch(2)
        self.excel_btn = QPushButton("📊  Excel로 내보내기")
        self.excel_btn.setToolTip("화면의 Plot 데이터 + 수정 가능한 Excel 차트로 저장 (Ctrl+E)\n"
                                  "보고서용 그래프를 Excel에서 직접 고칠 수 있습니다")
        self.excel_btn.setMinimumHeight(34)
        self.excel_btn.clicked.connect(self.on_export_excel)
        bottom.addWidget(self.excel_btn, 1)
        bottom.addWidget(self.save_btn, 1)
        root.addLayout(bottom)

        self.setCentralWidget(central)
        self.statusBar().showMessage("Ready")

    def _build_left_panel(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 6, 0)

        data_box = QGroupBox()
        data_box.setToolTip("체크 = 표시 · 눈 아이콘 = 항상 숨기기 · 선택 = Plot에서 강조 (빈 곳 클릭/Esc = 해제) · 더블클릭 = 데이터 보기/수정 · F2 = 이름 변경 · 끌어서 순서 변경 · Delete = 삭제 · Ctrl/Shift+클릭 또는 Ctrl+A(검색 결과 전체) → 우클릭 = 한꺼번에 색/스타일\n"
                            "구역 제목 클릭 = 접기/펼치기")
        dl = QVBoxLayout(data_box)
        from ui.dataset_list import DatasetList
        self.data_list = DatasetList(on_reordered=self.on_data_reordered, on_delete=self.on_delete_datasets,
                                     on_rename=self.on_rename_dataset, on_clear=self.on_data_clear,
                                     select_kinds=("meas", "fav", "sim"))
        self.data_list.alias_kinds = ("fav",)          # ★ rows = the same measurement datasets
        self.data_list.on_drop_section = self.on_drop_section
        self.data_list.on_open = self.on_open_dataset
        self.data_list.on_eye = self.on_eye
        self.data_list.on_copy = self.on_copy_datasets
        self.data_list.on_rename_many = lambda names: self.on_batch_rename([n for n in names if not n.startswith("sim:")])
        self.data_list.on_toggle_many = self.on_toggle_many
        # searching: Ctrl+A = the matching measurements (simulations stay listed but are not search results)
        self.data_list.ctrl_a_kinds = lambda: ("meas",) if self._filter_terms() else ("meas", "sim")
        self.data_list.setMinimumHeight(90)
        self.data_list.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self.data_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.data_list.itemChanged.connect(self.on_data_checked)
        self.data_list.currentItemChanged.connect(self.on_data_current)
        self.data_list.itemSelectionChanged.connect(self.on_data_selection)
        self.data_list.itemClicked.connect(self.on_data_clicked)
        self.data_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.data_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.data_list.customContextMenuRequested.connect(self.on_data_context)
        frow = QHBoxLayout()
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("검색 (이름·주파수)  예) 3.6 · 보드A")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.setToolTip("이름(붙여넣을 때 지정한 이름 포함)·주파수에 글자가 들어간 Dataset만 목록에 보여줍니다 (띄어쓰기 = 모두 포함)\n"
                                    "Enter 또는 [결과만 Plot] = 검색된 것만 체크해서 표시")
        self.filter_edit.textChanged.connect(self.on_filter_changed)
        self.filter_edit.returnPressed.connect(self.on_filter_show_only)
        self.filter_only_btn = QPushButton("결과만 Plot")
        self.filter_only_btn.setToolTip("검색된 Dataset만 체크 (나머지는 체크 해제)")
        self.filter_only_btn.clicked.connect(self.on_filter_show_only)
        self.selected_only_btn = QPushButton("선택 Plot")
        self.selected_only_btn.setToolTip("목록에서 선택한 것만 Plot (우클릭 '이것들만 보기'와 같음)")
        self.selected_only_btn.clicked.connect(self.on_selected_show_only)
        self.filter_only_btn.setToolTip("검색된 것만 Plot (Enter와 같음)")
        _half_buttons(self.filter_only_btn, self.selected_only_btn)
        frow.addWidget(self.filter_edit, 1)
        frow.addWidget(self.filter_only_btn)
        frow.addWidget(self.selected_only_btn)
        dl.addLayout(frow)
        dl.addWidget(self.data_list)
        sim_row = QHBoxLayout()
        self.sim_enable_check = QCheckBox("Simulation 표시")
        self.sim_enable_check.setChecked(self.c.sim_enabled)
        self.sim_enable_check.toggled.connect(self.on_sim_enable)
        self.sim_auto_check = QCheckBox("측정 FREQ 자동 매칭")
        self.sim_auto_check.setToolTip("체크하면 보이는 측정 데이터와 같은 FREQ의 Simulation을 자동으로 표시")
        self.sim_auto_check.setChecked(self.c.auto_sim)
        self.sim_auto_check.toggled.connect(self.on_sim_auto)
        sim_row.addWidget(self.sim_enable_check)
        sim_row.addWidget(self.sim_auto_check)
        dl.addLayout(sim_row)
        row = QHBoxLayout()
        self.check_all_btn = QPushButton("모두 체크")
        self.check_all_btn.clicked.connect(lambda: self.set_all_checked(True))
        self.uncheck_all_btn = QPushButton("모두 해제")
        self.uncheck_all_btn.clicked.connect(lambda: self.set_all_checked(False))
        row.addWidget(self.check_all_btn)
        row.addWidget(self.uncheck_all_btn)
        self.sort_btn = QPushButton("↕ 정렬")
        self.sort_btn.setToolTip("같은 이름끼리 모으고, 같은 이름 안에서는 주파수가 낮은 것부터 (Ctrl+Z로 되돌리기)")
        self.sort_btn.clicked.connect(self.on_sort_datasets)
        row.addWidget(self.sort_btn)
        dl.addLayout(row)
        self.sections = {}
        self._add_section(lay, "datasets", "Datasets", data_box, True, grow=True)

        plot_box = QGroupBox()
        pl = QVBoxLayout(plot_box)
        self.graph_group = QButtonGroup(self)
        self.graph_buttons = {}
        for g in self.c.graphs:  # generated from the graph registry -> extensible
            rb = QRadioButton(g.short_name)
            rb.setProperty("graph_key", g.key)
            self.graph_group.addButton(rb)
            self.graph_buttons[g.key] = rb
            pl.addWidget(rb)
        if self.c.graph_key in self.graph_buttons:
            self.graph_buttons[self.c.graph_key].setChecked(True)
        self.graph_group.buttonToggled.connect(self.on_graph_toggled)
        self._add_section(lay, "plot", "Plot", plot_box, True)

        axis_box = QGroupBox()
        al = QGridLayout(axis_box)
        self.xmin_edit, self.xmax_edit = QLineEdit(), QLineEdit()
        self.ymin_edit, self.ymax_edit = QLineEdit(), QLineEdit()
        for r, (lab, ed) in enumerate([("X Min", self.xmin_edit), ("X Max", self.xmax_edit),
                                       ("Y Min", self.ymin_edit), ("Y Max", self.ymax_edit)]):
            al.addWidget(QLabel(lab), r, 0)
            ed.setAlignment(Qt.AlignRight)
            ed.setMaximumWidth(110)
            ed.returnPressed.connect(self.on_apply_range)
            al.addWidget(ed, r, 1)
        self.apply_btn = QPushButton("Apply")
        self.apply_btn.clicked.connect(self.on_apply_range)
        self.auto_btn = QPushButton("Auto Scale")
        self.auto_btn.clicked.connect(self.on_auto_scale)
        self.reset_btn = QPushButton("Reset")
        self.reset_btn.setToolTip("Graph 기본 Range로 되돌리기 (PAE: 0~70 %, Gain: 0~20 dB)")
        self.reset_btn.clicked.connect(self.on_reset)
        self.auto_btn.setText("⤢ 전체 보기 (F)")
        al.addWidget(self.apply_btn, 4, 0, 1, 2)
        al.addWidget(self.auto_btn, 5, 0, 1, 2)
        al.addWidget(self.reset_btn, 6, 0, 1, 2)
        hint = QLabel("왼쪽 Drag: 이동 · 오른쪽 Drag: 영역 확대 · 휠: Zoom")
        hint.setStyleSheet("color:#777; font-size:8.5pt;")
        al.addWidget(hint, 7, 0, 1, 2)
        self._add_section(lay, "axis", "Axis Range", axis_box, True)

        mk_box = QGroupBox()
        mkl = QGridLayout(mk_box)
        self.values_check = QCheckBox("값 표시")
        self.values_check.setChecked(True)
        self.values_check.toggled.connect(self.on_values_toggle)
        mkl.addWidget(self.values_check, 0, 0, 1, 2)
        self.mk_clear_btn = QPushButton("Marker 지우기")
        self.mk_clear_btn.clicked.connect(self.on_clear_markers)
        mkl.addWidget(self.mk_clear_btn, 0, 2, 1, 2)
        self.step_edits = {}
        for col, (key, lab, val) in enumerate([("x", "Pout", "0.1"), ("gain", "Gain", "0.1"), ("pae", "PAE", "0.5")]):
            ed = QLineEdit(val)
            ed.setMaximumWidth(44)
            ed.editingFinished.connect(self.on_steps)
            self.step_edits[key] = ed
            mkl.addWidget(QLabel(lab), 1, col)
            mkl.addWidget(ed, 2, col)
        mkl.addWidget(QLabel("단위\n(dBm/dB/%)"), 1, 3, 2, 1)
        mhint = QLabel("클릭해 선택 → ←→ (Pout) / ↑↓ (Gain·PAE) · 더블클릭: 값 입력\n끌어서 이동 · Del 삭제 · 값 상자 우클릭: 메뉴")
        mhint.setStyleSheet("color:#777; font-size:8.5pt;")
        mkl.addWidget(mhint, 3, 0, 1, 4)
        self._add_section(lay, "marker", "Marker  (Plot에서 V / H)", mk_box, True)

        opt_box = QGroupBox()
        ol = QVBoxLayout(opt_box)
        self.cond_check = QCheckBox("Plot에 Condition 표시")
        self.cond_check.setToolTip("Copy/Save 이미지에도 선택한 조건(VG_DRV, VG_CA …)이 포함됩니다")
        self.cond_check.setChecked(self.c.show_condition_in_plot)
        self.cond_check.toggled.connect(self.on_condition_toggle)
        ol.addWidget(self.cond_check)
        self.hl_check = QCheckBox("선택한 데이터 강조 표시")
        self.hl_check.setToolTip("Datasets 목록에서 선택한 곡선을 굵게 · 맨 위로 그리고 나머지는 흐리게 표시합니다\n"
                                 "(같은 주파수의 Measurement/Simulation 짝은 그대로 보입니다. Copy/Save 이미지에도 적용)")
        self.hl_check.setChecked(self.c.highlight_selected)
        self.hl_check.toggled.connect(self.on_highlight_toggle)
        ol.addWidget(self.hl_check)
        self.cond_sel_btn = QPushButton("표시할 조건 선택…")
        self.cond_sel_btn.setToolTip("기본: VG_DRV, VG_CA, VG_PK, IQ_DRV, IQ_CA")
        self.cond_sel_btn.clicked.connect(self.on_choose_conditions)
        ol.addWidget(self.cond_sel_btn)
        self._add_section(lay, "options", "Options", opt_box, False)

        msg_box = QGroupBox()
        ml = QVBoxLayout(msg_box)
        self.msg_view = QPlainTextEdit()
        self.msg_view.setReadOnly(True)
        self.msg_view.setMinimumHeight(40)
        self.msg_view.setStyleSheet("font-size:9pt;")
        ml.addWidget(self.msg_view)
        self._add_section(lay, "messages", "Messages", msg_box, False)
        lay.addStretch(0)
        w.setMinimumWidth(200)
        return w

    def _build_plot_area(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 0, 0, 0)
        self.cond_panel = ConditionPanel()
        self.cond_panel.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        lay.addWidget(self.cond_panel)

        top = QHBoxLayout()
        top.addWidget(QLabel("Plot 개수:"))
        self.layout_combo = QComboBox()
        for n in (1, 2, 3, 4):
            self.layout_combo.addItem(str(n), n)
        self.layout_combo.currentIndexChanged.connect(self.on_layout_changed)
        top.addWidget(self.layout_combo)
        self.axis_reset_btn = QPushButton("↺ Axis Reset")
        self.axis_reset_btn.setToolTip("모든 Plot의 X/Y 축 범위를 기본값으로 되돌림\n(Ctrl+R = 파란 테두리 Plot만, F = 데이터에 맞춰 전체 보기)")
        self.axis_reset_btn.clicked.connect(self.on_reset_all_axes)
        self.colors_distinct_btn = QPushButton("🎨 각각 다른 색")
        self.colors_distinct_btn.setToolTip("지금 Plot에 그려진 측정 데이터를 모두 서로 다른 색으로\n"
                                            "(같은 FREQ의 Simulation은 그 측정 색을 따라감)")
        self.colors_distinct_btn.clicked.connect(self.on_colors_distinct)
        self.colors_freq_btn = QPushButton("주파수별 기본색")
        self.colors_freq_btn.setToolTip("지금 Plot에 그려진 데이터의 색을 주파수별 기본색으로 (선 굵기·종류는 그대로)")
        self.colors_freq_btn.clicked.connect(self.on_colors_by_freq)
        self.lines_reset_btn = QPushButton("━ 선 기본")
        self.lines_reset_btn.setToolTip("지금 Plot에 그려진 데이터의 선 종류·굵기·점 모양을 기본으로 (색은 그대로)")
        self.lines_reset_btn.clicked.connect(self.on_lines_default)
        self.axis_reset_btn.setText("↺ Axis")
        self.colors_distinct_btn.setText("🎨 각각 색")
        self.colors_freq_btn.setText("주파수 색")
        self.line_fav_btns = {}
        for i in (1, 2):                               # 선1 / 선2: saved line looks for quick comparisons
            b = QPushButton()
            b.clicked.connect(lambda _=False, i=i: self.on_line_fav(i))
            b.setContextMenuPolicy(Qt.CustomContextMenu)
            b.customContextMenuRequested.connect(lambda _pos=None, i=i: self.on_line_fav_menu(i))
            self.line_fav_btns[i] = b
        self._update_line_fav_buttons()
        _small_buttons(self.axis_reset_btn, self.colors_distinct_btn, self.colors_freq_btn, self.lines_reset_btn,
                       *self.line_fav_btns.values())
        for b in (self.axis_reset_btn, self.colors_distinct_btn, self.colors_freq_btn, self.lines_reset_btn,
                  *self.line_fav_btns.values()):
            top.addWidget(b)
        hint = QLabel("Plot 클릭 = 파란 테두리 → 왼쪽 설정이 그 Plot에 적용")
        hint.setStyleSheet("color:#777; font-size:8.5pt;")
        hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        top.addWidget(hint)
        top.addStretch(1)
        self.copy_all_btn = QPushButton("📋 Copy All Plots")
        self.copy_all_btn.setToolTip("화면의 모든 Plot을 한 장으로 복사 (PowerPoint에 Ctrl+V)")
        self.copy_all_btn.clicked.connect(self.on_copy_all)
        top.addWidget(self.copy_all_btn)
        lay.addLayout(top)

        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(6)
        self.views = [PlotView(self.c, self.on_view_changed, self.on_plot_context, self.on_view_key,
                               idx=i, on_activate=self.activate_view) for i in range(4)]
        for v in self.views:
            v.on_pick = self.on_plot_pick
            v.on_edit_labels = lambda part, v=v: self.on_edit_labels(v, part)
            v.on_drop_datasets = self.on_drop_to_view
            v.on_open_data = lambda key, x: self.on_open_dataset(key, x)
            v.on_marker_value = lambda hit, v=v: self.on_marker_value(v, hit)
        # plots | back-off panel in a vertical splitter: drag the bar to resize
        self.v_split = QSplitter(Qt.Vertical)
        self.v_split.setChildrenCollapsible(False)
        self.v_split.addWidget(self.grid_host)

        bo_box = QGroupBox()
        bl = QVBoxLayout(bo_box)
        bl.setContentsMargins(6, 2, 6, 4)
        row = QHBoxLayout()
        from PySide6.QtWidgets import QToolButton
        self.bo_toggle = QToolButton()
        self.bo_toggle.setCheckable(True)
        self.bo_toggle.setChecked(True)
        self.bo_toggle.setText("Back-off")
        self.bo_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.bo_toggle.setArrowType(Qt.DownArrow)
        self.bo_toggle.setStyleSheet("QToolButton { border: none; font-weight: bold; }")
        self.bo_toggle.setToolTip("표 접기 / 펼치기 (접으면 Plot이 커집니다)")
        self.bo_toggle.toggled.connect(self.on_bo_toggle)
        row.addWidget(self.bo_toggle)
        row.addWidget(QLabel("(dB, Psat = 최대 Pout):"))
        self.bo_edit = QLineEdit()
        self.bo_edit.setPlaceholderText("예: 6, 8, 10  (쉼표로 여러 개)")
        self.bo_edit.setToolTip("여러 값을 쉼표로 넣으면 Plot에 모두 표시됩니다. Enter = 이 값들로 표시")
        self.bo_edit.returnPressed.connect(self.on_backoff)
        row.addWidget(self.bo_edit, 1)
        self.bo_btn = QPushButton("표시")
        self.bo_btn.setToolTip("입력한 값들로 바꿔서 표시")
        self.bo_btn.clicked.connect(self.on_backoff)
        self.bo_add_btn = QPushButton("+ 추가")
        self.bo_add_btn.setToolTip("지금 표시 중인 값에 입력한 값을 더함")
        self.bo_add_btn.clicked.connect(self.on_backoff_add)
        self.bo_clear_btn = QPushButton("지우기")
        self.bo_clear_btn.clicked.connect(self.on_backoff_clear)
        self.bo_copy_btn = QPushButton("표 복사")
        self.bo_copy_btn.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.c.backoff_tsv()))
        for b in (self.bo_btn, self.bo_add_btn, self.bo_clear_btn, self.bo_copy_btn):
            row.addWidget(b)
        bl.addLayout(row)
        self.bo_table = QTableWidget()
        self.bo_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.bo_table.setMinimumHeight(30)
        bl.addWidget(self.bo_table, 1)
        self.bo_box = bo_box
        self.v_split.addWidget(bo_box)
        self.v_split.setStretchFactor(0, 1)
        self.v_split.setStretchFactor(1, 0)
        saved = self.settings.value("bo_split", None)
        try:
            sizes = [int(x) for x in str(saved).split(",")] if saved else [700, 170]
        except ValueError:
            sizes = [700, 170]
        self.v_split.setSizes(sizes)
        self.v_split.splitterMoved.connect(lambda *_: self._save_bo_split())
        lay.addWidget(self.v_split, 1)
        if str(self.settings.value("bo_open", "true")).lower() in ("false", "0"):
            self.bo_toggle.setChecked(False)
        return w

    def _save_bo_split(self):
        if self.bo_table.isVisible():
            self.settings.setValue("bo_split", ",".join(str(x) for x in self.v_split.sizes()))

    @_guard
    def on_bo_toggle(self, on):
        on = bool(on)
        self.bo_toggle.setArrowType(Qt.DownArrow if on else Qt.RightArrow)
        self.bo_table.setVisible(on)
        self.settings.setValue("bo_open", on)
        total = sum(self.v_split.sizes()) or 870
        if on:
            saved = str(self.settings.value("bo_split", "") or "")
            try:
                self.v_split.setSizes([int(x) for x in saved.split(",")] if saved else [total - 170, 170])
            except ValueError:
                self.v_split.setSizes([total - 170, 170])
        else:
            try:
                head = int(self.bo_box.sizeHint().height())
            except (TypeError, ValueError):
                head = 40
            self.v_split.setSizes([max(total - head, 100), head])

    def _build_actions(self):
        acts = [
            ("&Open…", QKeySequence(QKeySequence.Open), self.on_open),
            ("붙여넣기 (Excel에서 복사한 데이터)", QKeySequence(QKeySequence.Paste), self.on_paste),
            ("프로젝트 저장 (덮어쓰기)", QKeySequence(QKeySequence.Save), self.on_save_project_quick),
            ("다른 이름으로 프로젝트 저장…", QKeySequence("Ctrl+Shift+S"), self.on_save_project),
            ("Copy Plot", QKeySequence("Ctrl+Shift+C"), self.on_copy_plot),
            ("Save PNG…", QKeySequence("Ctrl+Alt+S"), self.on_save_png),
            ("Excel로 내보내기…", QKeySequence("Ctrl+E"), self.on_export_excel),
            ("되돌리기 (Undo)", QKeySequence(QKeySequence.Undo), self.on_undo),
            ("다시 실행 (Redo)", QKeySequence("Ctrl+Y"), self.on_redo),
            ("Reset Axis", QKeySequence("Ctrl+R"), self.on_reset),
            ("Auto Scale", QKeySequence("Ctrl+Shift+A"), self.on_auto_scale),
            ("전체 보기", QKeySequence("F"), self.on_auto_scale),
            ("세로 Marker", QKeySequence("V"), lambda: self.target_view().add_v()),
            ("가로 Marker", QKeySequence("H"), lambda: self.target_view().add_h()),
            ("Marker 삭제", QKeySequence("Delete"), self.on_delete_key),
            ("포인트 Marker (말풍선)", QKeySequence("M"), lambda: self._pin_here()),
        ]
        menu = self.menuBar().addMenu("&File")
        for text, key, slot in acts:
            a = QAction(text, self)
            a.setShortcut(key)
            a.triggered.connect(slot)
            self.addAction(a)
            menu.addAction(a)
        menu.addSeparator()
        quit_act = QAction("E&xit", self)
        quit_act.triggered.connect(self.close)
        menu.addAction(quit_act)
        help_menu = self.menuBar().addMenu("&Help")
        about = QAction("About", self)
        about.triggered.connect(lambda: QMessageBox.about(
            self, APP_TITLE, f"<b>{APP_TITLE}</b><br>RF PA Measurement vs. Simulation analysis tool.<br>Version {VERSION_LABEL}"))
        help_menu.addAction(about)

    # ============================================================ refresh logic
    def refresh(self, rerender: bool = True):
        """Sync every widget with the controller state."""
        self._updating = True
        try:
            mf = self.c.measurement
            self.file_edit.setText(mf.path if mf else "")
            self._fill_data_list()
            r = self.c.sheet_result
            self.current_label.setText(self.c.measurement.sheet_label(r.sheet_name) if r else "-")
            has_sheet = self.c.sheet_result is not None
            for b in (self.freq_btn, self.map_btn, self.rename_btn, self.delete_btn):
                b.setEnabled(has_sheet)
            self.save_proj_btn.setEnabled(mf is not None and bool(mf.sheets))
            if mf is not None:
                self.setWindowTitle(f"{APP_TITLE} — {os.path.basename(mf.path)}" + (" *" if mf.dirty else ""))

            cond = self.c.current_condition()
            self.cond_panel.set_rows(self.c.condition_panel_rows(), cond.freq_source or "")
            self._show_messages()
        finally:
            self._updating = False
        if rerender:
            self.render()

    def render(self):
        self._updating = True
        try:
            for v in self.visible_views:
                v.render()
            self._fill_range_fields()
            self._fill_backoff()
        finally:
            self._updating = False
        self._history_note()

    def _fill_range_fields(self):
        ax = self.renderer.ax
        if ax is None:
            return
        (x0, x1), (y0, y1) = ax.get_xlim(), ax.get_ylim()
        for ed, v in ((self.xmin_edit, x0), (self.xmax_edit, x1), (self.ymin_edit, y0), (self.ymax_edit, y1)):
            ed.setText(fmt_num(v, 2))

    # ------------------------------------------------------------ datasets list
    KIND_ROLE = 257

    def _add_section(self, lay, key, title, body, default_open, grow=False):
        from ui.collapsible import Section
        saved = self.settings.value(f"section/{key}", None)
        is_open = default_open if saved is None else str(saved).lower() in ("true", "1")
        sec = Section(title, body, is_open, grow=grow,
                      on_toggle=lambda s, key=key: self.settings.setValue(f"section/{key}", s.expanded))
        self.sections[key] = sec
        lay.addWidget(sec, 1 if grow else 0)
        return sec

    def _list_folded(self, kind: str) -> bool:
        v = self.settings.value(f"fold/{kind}", None)
        return str(v).lower() in ("true", "1") if v is not None else False

    def _header_item(self, kind: str, title: str, count: int, shown: int = 0):
        from PySide6.QtGui import QBrush, QColor, QFont
        # while searching, sections are always open: results must never hide behind a folded header
        folded = self._list_folded(kind) and not self._filter_terms()
        text = f"{'▶' if folded else '▼'}  {title}  ({count})"
        if folded:
            text += f"  — 접힘{f' · Plot에 {shown}개 표시 중' if shown else ''} (클릭해서 펼치기)"
        it = QListWidgetItem(text)
        it.setData(self.KIND_ROLE, "header:" + kind)
        it.setFlags(Qt.ItemIsEnabled)
        f = QFont()
        f.setBold(True)
        it.setFont(f)
        it.setBackground(QBrush(QColor("#eef2f8")))
        it.setToolTip("클릭: 접기 / 펼치기")
        return it, folded

    @_guard
    def on_data_clicked(self, item):
        if item is None:
            return
        kind = item.data(self.KIND_ROLE) or ""
        if kind.startswith("header:"):
            if self._filter_terms():                # searching: sections stay open (no accidental fold)
                self.statusBar().showMessage("검색 중에는 접을 수 없습니다 — 검색칸을 비우면 접기/펼치기 가능", 3000)
                return
            key = kind.split(":", 1)[1]
            self.settings.setValue(f"fold/{key}", not self._list_folded(key))
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, self._fill_data_list)

    def set_list_folded(self, kind: str, folded: bool):
        self.settings.setValue(f"fold/{kind}", bool(folded))
        self._fill_data_list()

    def _eye_icon(self, color, muted=False):
        from ui.eye_icon import swatch_eye_icon
        return swatch_eye_icon(color, muted)

    def _muted_brush(self):
        from PySide6.QtGui import QBrush, QColor
        return QBrush(QColor("#9e9e9e"))

    @_guard
    def on_eye(self, name, on=None):
        """Eye icon: always hidden (never drawn, skipped by Enter / 모두 체크 / 같이 보기) <-> normal."""
        if not name:
            return
        names = [name]                              # ONLY the clicked row (several at once: right-click menu)
        mute = (not self.c.is_muted(name)) if on is None else bool(on)
        self.c.set_muted(names, mute)
        if not mute:
            self.c.show_also(names)                    # eye on again -> back on the plot
        self._refresh_soon()
        self.statusBar().showMessage(
            (f"👁 {len(names)}개 항상 숨김 — 검색 Enter / 모두 체크에도 안 나옵니다 (눈 아이콘 다시 클릭 = 보이기)"
             if mute else f"👁 {len(names)}개 다시 보이기"), 5000)

    def _icon(self, color):
        from PySide6.QtGui import QColor, QIcon, QPixmap
        pm = QPixmap(12, 12)
        pm.fill(QColor(color))
        return QIcon(pm)

    def _fill_data_list(self):
        mf = self.c.measurement
        # (name, kind): a ★ row and the Measurement row of the same dataset are separate rows here
        keep_sel = {(it.data(NAME_ROLE), it.data(self.KIND_ROLE)) for it in self.data_list.selectedItems()
                    if it is not None and it.data(NAME_ROLE)}
        _cur = self.data_list.currentItem()
        cur_kind = _cur.data(self.KIND_ROLE) if _cur is not None else None
        scroll = self._list_scroll()                   # rebuilding resets the scroll bar -> remember where we were
        self.data_list.blockSignals(True)
        self.data_list.clear()
        current = None
        series_color = {}
        for s_ in self.c.build_series():
            series_color[s_.label] = s_.style.get("color")
        shown = {v.sheet_name for v in self.c.visible_sheets()}
        terms = self._filter_terms()
        meas_keys = {r.sheet_name: f"{mf.sheet_label(r.sheet_name)} {r.sheet_name} {format_freq(r.dataset.freq_hz) if (r.dataset is not None and r.dataset.freq_hz) else ''}"
                     for r in mf.sheets} if mf else {}
        n_meas = sum(1 for k in meas_keys.values() if self._match(k, terms))
        header, folded_meas = self._header_item("meas", "Measurement",
                                                f"{n_meas}/{len(meas_keys)}" if terms else len(meas_keys),
                                                shown=len(shown))
        self.data_list.addItem(header)
        if mf:
            for r in mf.sheets:
                it = QListWidgetItem(mf.sheet_label(r.sheet_name))
                it.setData(FILTER_ROLE, meas_keys[r.sheet_name])
                it.setData(NAME_ROLE, r.sheet_name)
                it.setData(self.KIND_ROLE, "meas")
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setCheckState(Qt.Checked if (r.sheet_name in shown and r.sheet_name != self.c.browse_name)
                                 else Qt.Unchecked)       # a previewed row (after 모두 해제) stays unticked
                if r.ok:
                    custom = self.c.style_of_dataset(r.sheet_name).get("color")
                    it.setIcon(self._eye_icon(self.c.color_of(r.sheet_name) if r.sheet_name in shown else "#cccccc",
                                              self.c.is_muted(r.sheet_name)))
                if self.c.is_muted(r.sheet_name):
                    it.setForeground(self._muted_brush())
                    it.setToolTip("항상 숨김 (눈 아이콘 클릭 = 다시 보이기)")
                self.data_list.addItem(it)
                it.setHidden(folded_meas or not self._match(meas_keys[r.sheet_name], terms))
                if r.sheet_name == self.c.sheet_name:
                    current = it
            # ---- ★ 즐겨찾기: always listed (the search does not hide them); a row here = that measurement
            favs = self.c.favorite_names()
            header, folded_fav = self._header_item("fav", "★ 즐겨찾기", len(favs),
                                                   shown=sum(1 for n in favs if n in shown))
            header.setToolTip("Measurement에서 끌어다 놓기 (또는 우클릭 → ★ 즐겨찾기에 추가)\n"
                              "검색해도 항상 보임 · 여기서 Delete = 즐겨찾기에서만 빼기 (데이터는 그대로)\n클릭: 접기 / 펼치기")
            self.data_list.addItem(header)
            if not favs and not folded_fav:
                hint = QListWidgetItem("     (Measurement에서 끌어다 놓거나 우클릭 → ★ 즐겨찾기에 추가)")
                hint.setData(self.KIND_ROLE, "hint")
                hint.setFlags(Qt.ItemIsEnabled)
                hint.setForeground(self._muted_brush())
                self.data_list.addItem(hint)
            for n in favs:
                r = mf.sheet(n)
                it = QListWidgetItem("★ " + mf.sheet_label(n) + (f"  · {format_freq(r.dataset.freq_hz)}"
                                                                  if r.dataset is not None and r.dataset.freq_hz else ""))
                it.setData(NAME_ROLE, n)
                it.setData(self.KIND_ROLE, "fav")
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setCheckState(Qt.Checked if (n in shown and n != self.c.browse_name) else Qt.Unchecked)
                if r.ok:
                    it.setIcon(self._eye_icon(self.c.color_of(n) if n in shown else "#cccccc", self.c.is_muted(n)))
                if self.c.is_muted(n):
                    it.setForeground(self._muted_brush())
                self.data_list.addItem(it)
                it.setHidden(folded_fav)
                if n == self.c.sheet_name and (current is None or current.isHidden() or cur_kind == "fav"):
                    current = it                           # worked in ★ / Measurement row hidden by the search
        from app.controller import ALL_SETS
        sims = self.c.all_sim_datasets()
        vis = {d.name for d in self.c.visible_sims()}
        if self.c.sim_set and self.c.sim_set != ALL_SETS:
            # show the data of the set chosen in the Simulation box (+ anything the user checked elsewhere)
            sims = [d for d in sims if d.group == self.c.sim_set or d.name in vis]
        if sims:
            title = "Simulation" + (f" · {self.c.sim_set}" if self.c.sim_set and self.c.sim_set != ALL_SETS else "")
            sim_key = {d.name: f"{d.group} · {format_freq(d.freq_hz)} {d.name}" for d in sims}
            sim_hit = set(sim_key)                  # simulations are never hidden by the search
            header, folded_sim = self._header_item("sim", title, len(sims), shown=len(vis))
            self.data_list.addItem(header)
            single = len(shown) <= 1 and len(vis) <= 1
            groups = list(dict.fromkeys(d.group for d in sims))
            last_group = None
            for d in sims:
                if len(groups) > 1 and d.group != last_group:
                    last_group = d.group
                    n_on = sum(1 for x in sims if x.group == d.group and x.name in vis)
                    g = QListWidgetItem(f"     {d.group}   ({n_on}/{sum(1 for x in sims if x.group == d.group)} 표시)")
                    g.setData(self.KIND_ROLE, "group")
                    g.setFlags(Qt.ItemIsEnabled)
                    g.setForeground(self._group_brush())
                    self.data_list.addItem(g)
                    g.setHidden(folded_sim or not any(x.name in sim_hit for x in sims if x.group == d.group))
                it = QListWidgetItem(f"{d.group} · {format_freq(d.freq_hz)}")
                it.setData(NAME_ROLE, "sim:" + d.name)
                it.setData(FILTER_ROLE, sim_key[d.name])
                it.setData(self.KIND_ROLE, "sim")
                it.setFlags((it.flags() | Qt.ItemIsUserCheckable) & ~Qt.ItemIsDragEnabled)   # only measurements move
                it.setCheckState(Qt.Checked if d.name in vis else Qt.Unchecked)
                if self.c.highlight_sim == "sim:" + d.name:
                    current = it
                if not self.c.sim_enabled:
                    it.setFlags(it.flags() & ~Qt.ItemIsEnabled)
                lab = f"Simulation ({d.group})" if single else f"Sim {d.group} · {format_freq(d.freq_hz)}"
                col = series_color.get(lab) or ("#1f4e9c" if d.name in vis else "#cccccc")
                it.setIcon(self._eye_icon(self.c.style_of_dataset("sim:" + d.name).get("color") or col,
                                          self.c.is_muted("sim:" + d.name)))
                if self.c.is_muted("sim:" + d.name):
                    it.setForeground(self._muted_brush())
                    it.setToolTip("항상 숨김 (눈 아이콘 클릭 = 다시 보이기)")
                self.data_list.addItem(it)
                it.setHidden(folded_sim or d.name not in sim_hit)
        if self.c.highlight_cleared and len(keep_sel) <= 1:
            current = None                             # nothing chosen -> keep the list unselected too
        if current is not None and not current.isHidden():
            if len(keep_sel) > 1:
                from PySide6.QtCore import QItemSelectionModel
                self.data_list.setCurrentItem(current, QItemSelectionModel.NoUpdate)   # keep multi-selection
            else:
                self.data_list.setCurrentItem(current)
        if len(keep_sel) > 1:                          # rebuilding the list must not drop a multi-selection
            for i in range(self.data_list.count()):
                it = self.data_list.item(i)
                if (it.data(NAME_ROLE), it.data(self.KIND_ROLE)) in keep_sel:
                    it.setSelected(True)
        focus = current.data(NAME_ROLE) if (self._scroll_to_current and current is not None
                                            and not current.isHidden()) else None
        self._set_list_scroll(scroll, focus)           # same view as before (no jump to the top); a curve
        self._scroll_to_current = False                # clicked in the plot -> its row in the middle
        self.data_list.blockSignals(False)

    def _list_scroll(self):
        try:
            return int(self.data_list.verticalScrollBar().value()), int(self.data_list.horizontalScrollBar().value())
        except (TypeError, ValueError):
            return 0, 0

    def _set_list_scroll(self, pos, focus=None):
        """Put the list back where it was; with `focus` (a dataset name) scroll that row into view instead."""
        v, h = pos
        try:
            self.data_list.doItemsLayout()             # scroll range must cover the new items first
        except Exception:
            pass
        self.data_list.verticalScrollBar().setValue(v)
        self.data_list.horizontalScrollBar().setValue(h)
        self._scroll_focus(focus)
        if v or h or focus:                            # Qt may lay the list out again later -> re-apply once
            from PySide6.QtCore import QTimer

            def again():
                if focus:
                    self._scroll_focus(focus)
                else:
                    self.data_list.verticalScrollBar().setValue(v)
                    self.data_list.horizontalScrollBar().setValue(h)
            QTimer.singleShot(0, again)

    def _scroll_focus(self, name):
        if not name:
            return
        from PySide6.QtWidgets import QAbstractItemView
        for i in range(self.data_list.count()):
            it = self.data_list.item(i)
            if it is not None and it.data(NAME_ROLE) == name and not it.isHidden():
                self.data_list.scrollToItem(it, QAbstractItemView.PositionAtCenter)
                return

    def _group_brush(self):
        from PySide6.QtGui import QBrush, QColor
        return QBrush(QColor("#555555"))

    @_guard
    def on_sim_enable(self, on):
        self.c.sim_enabled = bool(on)
        self.refresh(rerender=True)

    @_guard
    def on_sim_auto(self, on):
        self.c.auto_sim = bool(on)
        self.refresh(rerender=True)

    def _refresh_after_pause(self, ms: int = 80):
        """Selection changed (click or arrow key). Holding ↑/↓ fires many changes per second and each
        full redraw takes a while -> redraw once the selection has rested for `ms` ms, so the list
        follows the key instantly and the plot catches up as soon as you stop."""
        from PySide6.QtCore import QTimer
        self._sel_gen += 1
        gen = self._sel_gen

        def run():
            if gen == self._sel_gen:                   # a newer selection came in -> skip this one
                self.refresh(rerender=True)
        QTimer.singleShot(ms, run)

    def _refresh_soon(self):
        """Rebuild the list AFTER Qt has finished handling the current click.
        Rebuilding inside the item's own signal deletes the item Qt is still using,
        and Qt then emits itemClicked(None) -> "'NoneType' object has no attribute 'data'"."""
        from PySide6.QtCore import QTimer
        if self._refresh_pending:
            return
        self._refresh_pending = True

        def run():
            self._refresh_pending = False
            self.refresh(rerender=True)
        QTimer.singleShot(0, run)

    @_guard
    def on_data_checked(self, item):
        if self._updating or item is None:
            return
        name = item.data(NAME_ROLE)
        if not name:
            return
        if item.data(self.KIND_ROLE) == "sim":
            self.c.toggle_sim(name[4:], item.checkState() == Qt.Checked)
            self._refresh_soon()
            return
        self.c.set_checked(name, item.checkState() == Qt.Checked)
        self._refresh_soon()

    @_guard
    def on_data_current(self, item, _prev=None):
        if self._updating or item is None or item.data(self.KIND_ROLE) not in ("meas", "fav", "sim"):
            return
        from PySide6.QtWidgets import QApplication
        mods = QApplication.keyboardModifiers()
        if mods & (Qt.ControlModifier | Qt.ShiftModifier):
            return      # Ctrl/Shift+click = building a multi-selection (for the right-click menu), not switching data
        if item.data(self.KIND_ROLE) == "sim":      # selecting a simulation row only emphasises that curve
            self.c.select_highlight_sim(item.data(NAME_ROLE))
            self._refresh_after_pause()
            return
        self.hover.hide()
        self.c.select_sheet(item.data(NAME_ROLE))
        self._refresh_after_pause()

    # ------------------------------------------------------------ search filter
    def _filter_terms(self):
        return [t for t in self._filter.lower().split() if t]

    @staticmethod
    def _match(text: str, terms) -> bool:
        low = (text or "").lower()
        return all(t in low for t in terms)

    @_guard
    def on_filter_changed(self, text: str = ""):
        self._filter = str(text or "")
        self._fill_data_list()

    @_guard
    def on_filter_show_only(self, *_):
        """Check exactly the Datasets the search finds (measurements and simulations)."""
        terms = self._filter_terms()
        if not terms:
            return
        meas, sims, unmatched_sims = [], [], []
        for i in range(self.data_list.count()):
            it = self.data_list.item(i)
            kind, name = it.data(self.KIND_ROLE), it.data(NAME_ROLE)
            if kind not in ("meas", "sim") or not name:
                continue
            hit = self._match(it.data(FILTER_ROLE), terms) and not self.c.is_muted(name)   # eye off: stays off
            if kind == "meas" and hit:
                r = self.c.measurement.sheet(name) if self.c.measurement else None
                if r is not None and r.ok:
                    meas.append(name)
            elif kind == "sim":
                unmatched_sims.append(name[4:])     # simulations: back to auto (follow the shown FREQs)
        if not meas:
            self.statusBar().showMessage(f"'{self._filter}' 검색 결과가 없습니다", 4000)
            return
        self.c.checked = meas
        if meas and self.c.sheet_name not in meas:
            self.c.sheet_name = meas[0]
        for n in sims:
            self.c.toggle_sim(n, True)
        for n in unmatched_sims:                       # back to automatic (follows the checked measurements)
            self.c.sim_on = [x for x in self.c.sim_on if x != n]
            self.c.sim_off = [x for x in self.c.sim_off if x != n]
        self.c.browse_name, self.c.browsing = None, False
        self.c.clear_highlight()
        self.refresh(rerender=True)
        self.statusBar().showMessage(f"검색 결과 표시: 측정 {len(meas)}개 · Simulation {len(sims)}개", 4000)

    @_guard
    def on_selected_show_only(self, *_):
        """[선택 Plot]: only the rows selected in the list on the plot (= right-click '이것들만 보기')."""
        sel = [it.data(NAME_ROLE) for it in self.data_list.selectedItems()
               if it is not None and not it.isHidden() and it.data(self.KIND_ROLE) in ("meas", "fav", "sim")]
        if not sel:
            self.statusBar().showMessage("Plot할 데이터를 목록에서 선택하세요 (Ctrl/Shift+클릭, Ctrl+A) — "
                                         "Measurement / Simulation 모두 가능", 4000)
            return
        self.c.show_only(sel)
        self.refresh(rerender=True)
        n_sim = sum(1 for n in sel if n.startswith("sim:"))
        self.statusBar().showMessage(f"선택한 것만 Plot: 측정 {len(self.c.checked)}개" + (f" · Simulation {n_sim}개" if n_sim else ""), 4000)

    @_guard
    def on_plot_pick(self, key, add=False):
        """Click in the plot: on a curve -> emphasise it and select its Datasets row; empty -> clear.
        add (Ctrl / Shift + click): add that curve to the emphasised ones (again = take it out)."""
        if add and key:
            return self._plot_pick_add(key)
        if add:                                         # Ctrl / Shift + click on empty space: keep the choice
            return
        self._plot_picked = [key] if key else []
        if not key and self.c.highlight_cleared:
            return
        self.c.pick_dataset(key)
        self.c.highlight_multi = []                     # a curve clicked replaces a multi-selection
        self.data_list.blockSignals(True)               # (otherwise the old selection kept winning)
        self.data_list.clearSelection()
        self.data_list.blockSignals(False)
        self._scroll_to_current = bool(key)
        if key:                                         # its section folded? open it so the row can be shown
            kind = "sim" if key.startswith("sim:") else "meas"
            if self._list_folded(kind):
                self.settings.setValue(f"fold/{kind}", False)
            mf = self.c.measurement
            if kind == "sim":
                d = next((d for d in self.c.all_sim_datasets() if d.name == key[4:]), None)
                desc = f"Simulation {d.group} · {format_freq(d.freq_hz)}" if d else key
            else:
                r = mf.sheet(key) if mf else None
                desc = (f"{r.label} · {format_freq(r.dataset.freq_hz)}" if r and r.dataset is not None
                        and r.dataset.freq_hz else (r.label if r else key))
            hidden = kind == "meas" and self._filter_terms() and mf and \
                not self._match(f"{mf.sheet_label(key)} {key}", self._filter_terms())
            self.statusBar().showMessage(f"▶ 선택: {desc}" + ("  (검색 결과에 없어 목록에서는 가려져 있음)" if hidden else ""),
                                         6000)
        self._refresh_soon()

    @_guard
    def on_drop_to_view(self, view, names=None):
        """Datasets rows dragged onto a plot: shown there (Plot 3 / 4: only in that plot)."""
        if names is None:
            names = list(dict.fromkeys(it.data(NAME_ROLE) for it in self.data_list.selectedItems()
                                       if it is not None and not it.isHidden()
                                       and it.data(self.KIND_ROLE) in ("meas", "fav", "sim")))
        if not names:
            return
        self.activate_view(view)
        with self.c.using_pane(view.idx):
            own = self.c.pane_is_own
            n = self.c.add_to_pane(names)
        self.refresh(rerender=True)
        self.statusBar().showMessage(f"Plot {view.idx + 1}에 {n}개 추가" + (" (이 Plot에만)" if own else ""), 4000)

    @_guard
    def on_plot_delete(self, view):
        """Delete in a plot: the selected marker; else the curves picked in this plot go off this plot
        (the data stays — tick it again / drag it back to show it)."""
        picked = [k for k in getattr(self, "_plot_picked", []) if k]
        with self.c.using_pane(view.idx):
            has_marker = self.c.selected is not None
            shown = {r.sheet_name for r in self.c.visible_sheets()} | {"sim:" + d.name for d in self.c.visible_sims()}
        picked = [k for k in picked if k in shown]
        if has_marker or not picked:
            view.delete_marker()
            return
        with self.c.using_pane(view.idx):
            self.c.hide_datasets(picked)
        self._plot_picked = []
        self.c.clear_highlight()
        self.refresh(rerender=True)
        self.statusBar().showMessage(f"Plot {view.idx + 1}에서 {len(picked)}개 숨김 — 데이터는 그대로 "
                                     "(목록 체크 / 끌어다 놓기로 다시 표시)", 5000)

    def _plot_pick_add(self, key):
        c = self.c
        base = list(c.highlight_multi) or list(c.highlight_keys())
        if key in base:
            base.remove(key)
        else:
            base.append(key)
        self.data_list.blockSignals(True)               # the Datasets list shows the same choice
        self.data_list.clearSelection()
        for i in range(self.data_list.count()):
            it = self.data_list.item(i)
            if it.data(NAME_ROLE) in base and it.data(self.KIND_ROLE) in ("meas", "sim") and not it.isHidden():
                it.setSelected(True)
        self.data_list.blockSignals(False)
        self._plot_picked = list(base)
        if len(base) > 1:
            c.set_highlight_multi(base)
        elif base:
            c.highlight_multi = []
            c.pick_dataset(base[0])
        else:
            c.clear_highlight()
        self.statusBar().showMessage(f"▶ 선택 {len(base)}개 (Ctrl/Shift+클릭 = 추가 / 빼기, 빈 곳 클릭 = 해제)", 4000)
        self._refresh_soon()

    @_guard
    def on_data_selection(self):
        """Several rows selected (Ctrl/Shift+click, Ctrl+A) -> all of them bold on the plot."""
        if self._updating:
            return
        self._plot_picked = []                          # chosen in the list, not in a plot
        names = [it.data(NAME_ROLE) for it in self.data_list.selectedItems()
                 if it is not None and not it.isHidden() and it.data(self.KIND_ROLE) in ("meas", "fav", "sim")]
        before = list(self.c.highlight_multi)
        self.c.set_highlight_multi(names)
        if self.c.highlight_multi != before:
            self._refresh_after_pause()

    @_guard
    def on_data_clear(self):
        """Empty space in the Datasets list clicked (or Esc): no emphasis, all curves equal."""
        self.c.clear_highlight()
        self._refresh_soon()

    def set_all_checked(self, on: bool):
        if not self.c.measurement:
            return
        self.c.check_all(on)                       # [모두 해제] = really nothing on the plot
        self.refresh(rerender=True)

    @_guard
    def on_data_context(self, pos):
        item = self.data_list.itemAt(pos)
        if item is None or not item.data(NAME_ROLE):
            return
        if item.data(self.KIND_ROLE) in ("meas", "fav"):
            self.c.sheet_name = item.data(NAME_ROLE)   # menu acts on this dataset; overlay unchanged
        sel = [it.data(NAME_ROLE) for it in self.data_list.selectedItems()
               if it is not None and it.data(NAME_ROLE) and not it.isHidden()]   # rows hidden by the search don't count
        sel = list(dict.fromkeys(sel))
        names = sel if (item.data(NAME_ROLE) in sel and len(sel) > 1) else [item.data(NAME_ROLE)]
        menu, _ = self.build_data_menu(names, from_fav=item.data(self.KIND_ROLE) == "fav")
        menu.exec(self.data_list.viewport().mapToGlobal(pos))

    def build_data_menu(self, names=None, from_fav: bool = False):
        """names: datasets the menu acts on (several = multi-selection). from_fav: opened on a ★ row."""
        menu, acts = QMenu(self), {}
        names = list(names or [self.c.sheet_name])
        multi = len(names) > 1
        only_sim = all(n.startswith("sim:") for n in names)
        meas_names = [n for n in names if not n.startswith("sim:")]
        if multi or only_sim:
            n_sim = len(names) - len(meas_names)
            title = menu.addAction(f"선택: 측정 {len(meas_names)}개 · Simulation {n_sim}개" if multi
                                   else "Simulation " + names[0][4:])
            title.setEnabled(False)
        else:
            for key, text, slot in (("rename", "이름 변경…", self.on_rename_sheet), ("freq", "FREQ 지정…", self.on_set_freq),
                                    ("map", "Column Mapping…", self.on_column_mapping)):
                a = menu.addAction(text)
                a.triggered.connect(lambda _=False, slot=slot: slot())
                acts[key] = a
        menu.addSeparator()
        self._style_menu(menu, names, acts)
        from ui import line_favs
        for i in (1, 2):
            st = line_favs.load(self.settings, i)
            acts[f"line_fav:{i}"] = menu.addAction(f"{line_favs.glyph(st)} 선{i} 즐겨찾기 ({line_favs.describe(st)})")
            acts[f"line_fav:{i}"].triggered.connect(lambda _=False, i=i: self.on_line_fav(i, names))
        menu.addSeparator()
        if meas_names:
            fav = set(self.c.favorite_names())
            if from_fav or all(n in fav for n in meas_names):
                acts["fav_remove"] = menu.addAction("☆ 즐겨찾기에서 빼기 (데이터는 그대로)"
                                                    + (f"  {len(meas_names)}개" if len(meas_names) > 1 else ""))
                acts["fav_remove"].triggered.connect(lambda _=False: self.on_unfavorite(meas_names))
            else:
                acts["fav_add"] = menu.addAction("★ 즐겨찾기에 추가" + (f"  {len(meas_names)}개" if len(meas_names) > 1 else ""))
                acts["fav_add"].setToolTip("Datasets 목록의 ★ 즐겨찾기 구역에 넣어 두기 — 검색해도 항상 보임")
                acts["fav_add"].triggered.connect(lambda _=False: self.on_favorite(meas_names))
            acts["info"] = menu.addAction("ⓘ 데이터 정보 (언제 추가 / 어디서)…")
            acts["info"].triggered.connect(lambda _=False: self.on_dataset_info(meas_names))
        acts["only"] = menu.addAction("이것들만 보기" if len(names) > 1 else "이것만 보기")
        acts["only"].triggered.connect(lambda _=False: self._show_only(names))
        acts["add"] = menu.addAction(f"선택한 {len(names)}개도 같이 보기 (추가)" if len(names) > 1 else "이것도 같이 보기 (추가)")
        acts["add"].setToolTip("지금 Plot에 있는 것은 그대로 두고 추가로 그립니다")
        acts["add"].triggered.connect(lambda _=False: self._show_also(names))
        if meas_names:
            from measurement import mapping_presets
            presets = mapping_presets.load(self.settings)
            pm = menu.addMenu(f"맵핑 즐겨찾기 적용 ({len(meas_names)}개)" if len(meas_names) > 1 else "맵핑 즐겨찾기 적용")
            for p in presets:
                a = pm.addAction(p.get("name", "?"))
                a.triggered.connect(lambda _=False, nm=p.get("name"): self.apply_mapping_preset(nm, meas_names))
                acts[f"preset:{p.get('name')}"] = a
            if not presets:
                t = pm.addAction("(없음 — Column Mapping 창에서 '현재 맵핑 저장')")
                t.setEnabled(False)
        if meas_names:
            acts["copy"] = menu.addAction(f"복사 (Ctrl+C) — {len(meas_names)}개" if len(meas_names) > 1 else "복사 (Ctrl+C)")
            acts["copy"].triggered.connect(lambda _=False: self._copy_names(meas_names))
        acts["paste"] = menu.addAction("붙여넣기 (Ctrl+V)")
        acts["paste"].triggered.connect(lambda _=False: self.on_paste())
        all_muted = all(self.c.is_muted(n) for n in names)
        acts["eye"] = menu.addAction(("👁 다시 보이기 (항상 숨김 해제)" if all_muted else "👁 항상 숨기기 (검색·모두 체크에도 안 나옴)")
                                     + (f"  {len(names)}개" if len(names) > 1 else ""))
        acts["eye"].triggered.connect(lambda _=False: (self.c.set_muted(names, not all_muted),
                                                       (not all_muted) or [self.c.show_also([n]) for n in names],
                                                       self.refresh(rerender=True)))
        acts["hide"] = menu.addAction(f"선택한 {len(names)}개 숨기기 (체크 해제)" if len(names) > 1 else "숨기기 (체크 해제)")
        acts["hide"].triggered.connect(lambda _=False: self._hide(names))
        if len(meas_names) > 1:
            menu.addSeparator()
            acts["rename_many"] = menu.addAction(f"선택한 {len(meas_names)}개 이름 변경…")
            acts["rename_many"].triggered.connect(lambda _=False: self.on_batch_rename(meas_names))
        if meas_names and from_fav:
            acts["delete"] = menu.addAction("★ 즐겨찾기에서만 삭제 (Measurement에는 그대로)")
            acts["delete"].triggered.connect(lambda _=False: self.on_unfavorite(meas_names))
        elif meas_names:
            acts["delete"] = menu.addAction(f"{len(meas_names)}개 삭제" if len(meas_names) > 1 else "삭제")
            acts["delete"].triggered.connect(lambda _=False: self._delete_many(meas_names))
        return menu, acts

    @_guard
    def on_batch_rename(self, names):
        """Selected datasets -> all get the one name you type (same names are allowed)."""
        mf = self.c.measurement
        if not mf:
            return
        names = [n for n in names if mf.sheet(n)]
        labels = [mf.sheet(n).label for n in names]
        start = labels[0] if len(set(labels)) == 1 else ""
        text, ok = QInputDialog.getText(self, f"이름 변경 ({len(names)}개)",
                                        f"선택한 {len(names)}개 Dataset의 새 이름:", text=start)
        text = str(text).strip() if ok else ""
        if not text:
            return
        for n in names:
            self.c.rename_sheet(n, text)
        self._refresh_soon()
        self.statusBar().showMessage(f"✔ {len(names)}개 이름 → '{text}'", 4000)

    def _show_also(self, names):
        self.c.show_also(names)
        self.refresh(rerender=True)
        self.statusBar().showMessage(f"{len(names)}개 추가 — 지금 보이는 측정 {len(self.c.visible_sheets())}개", 4000)

    # ========================================================== marker value by typing
    @_guard
    def on_marker_value(self, view, hit):
        """Double-click on a vertical / horizontal marker -> type the exact value."""
        import re
        from core.quantities import QUANTITIES
        kind, i = hit
        with self.c.using_pane(view.idx):
            g = self.c.graph
            lst = self.c.v_markers if kind == "v" else self.c.cur_h
            if not (0 <= i < len(lst)):
                return
            q = QUANTITIES[g.x if kind == "v" else g.y]
            cur = lst[i]
        text, ok = QInputDialog.getText(self, f"{'세로' if kind == 'v' else '가로'} Marker {'M' if kind == 'v' else 'H'}{i + 1}",
                                        f"{q.label} ({q.unit}) 값:", text=f"{cur:g}")
        if not ok:
            return
        m = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", str(text))
        if not m:
            QMessageBox.warning(self, APP_TITLE, "숫자를 입력해주세요. 예: 38.5")
            return
        with self.c.using_pane(view.idx):
            self.c.set_marker_value(kind, i, float(m.group(0)))
            view.redraw_markers()
        self.on_view_changed("markers")
        self._fill_backoff()

    # ========================================================== data editor
    MARK = {"pout": "#dbeafe", "pin": "#e0e7ff", "gain": "#dcfce7", "pae": "#ffedd5", "de": "#fef9c3"}

    @_guard
    def on_open_dataset(self, name, x=None):
        """Double-click a dataset (list) or its curve (plot): show the numbers, edit measurements."""
        from ui import data_editor
        if not name:
            return
        if name.startswith("sim:"):
            d = next((d for d in self.c.all_sim_datasets() if d.name == name[4:]), None)
            if d is None:
                return
            qs = [q for q in ("pout", "pin", "gain", "pae", "de") if d.has(q)]
            from core.quantities import QUANTITIES
            n = len(d.columns[qs[0]]) if qs else 0
            rows = [[float(d.columns[q][i]) for q in qs] for i in range(n)]
            dlg = data_editor.DataEditorDialog(
                f"Simulation 데이터 — {d.group} · {format_freq(d.freq_hz)}", rows,
                headers=[f"{QUANTITIES[q].label} ({QUANTITIES[q].unit})" for q in qs], readonly=True,
                note="Simulation 데이터는 보기만 가능합니다 (원본 Simulation 파일에서 수정하세요). "
                     "셀을 선택하고 Ctrl+C로 복사할 수 있습니다.", parent=self)
            dlg.exec()
            return
        mf = self.c.measurement
        r = mf.sheet(name) if mf else None
        if r is None:
            return
        rows = [list(row) for row in r.raw.rows]
        focus = None
        pc = r.mapping.get("pout") if r.mapping else None
        if x is not None and pc is not None:               # opened from the plot: jump to the clicked Pout
            best = None
            for i in range((r.header_row or 0) + 1, len(rows)):
                v = rows[i][pc] if pc < len(rows[i]) else None
                if isinstance(v, (int, float)) and (best is None or abs(v - x) < best[0]):
                    best = (abs(v - x), i)
            focus = best[1] if best else None
        marks = {c: self.MARK.get(q, "#f1f5f9") for q, c in (r.mapping or {}).items() if q in self.MARK}
        dlg = data_editor.DataEditorDialog(
            f"데이터 보기 / 수정 — {r.label}", rows, name=r.label, mark_cols=marks, header_row=r.header_row,
            focus_row=focus, parent=self,
            note="셀을 직접 고치거나 Excel에서 복사한 값을 Ctrl+V로 붙여넣으세요. 행 추가/삭제 가능. "
                 "색칠된 열 = Plot에 쓰는 열 (Pout / Gain / PAE). OK를 눌러야 적용되고, Ctrl+Z로 되돌릴 수 있습니다.")
        if not dlg.exec():
            return
        new_name, new_rows = dlg.values()
        old_rows = [list(row) for row in r.raw.rows]
        norm = [[data_editor.to_cell(data_editor.fmt_cell(v)) for v in row] for row in old_rows]
        while norm and not norm[-1]:
            norm.pop()
        norm = [row[:max([i + 1 for i, v in enumerate(row) if v is not None] + [0])] for row in norm]
        changed = new_rows != norm
        if changed:
            res = mf.replace_rows(name, new_rows)
            if not res.ok:
                QMessageBox.warning(self, APP_TITLE, "수정한 데이터에서 Pout / Gain / PAE를 찾지 못했습니다.\n"
                                    + "\n".join(m.text for m in res.messages[:3]) + "\n\nCtrl+Z로 되돌릴 수 있습니다.")
        if new_name and new_name != r.label:
            self.c.rename_sheet(name, new_name)
        if changed or (new_name and new_name != r.label):
            self.refresh(rerender=True)
            n = len(mf.sheet(name).dataset.columns.get("pout", [])) if mf.sheet(name) and mf.sheet(name).dataset else 0
            self.statusBar().showMessage(f"✔ '{new_name or r.label}' 데이터 수정 적용 (점 {n}개) — 저장하려면 프로젝트 저장", 6000)

    def _hide(self, names):
        self.c.hide_datasets(names)
        self.refresh(rerender=True)
        shown = len(self.c.visible_sheets())
        self.statusBar().showMessage(f"{len(names)}개 숨김 — 다시 보려면 체크하세요 (지금 보이는 측정 {shown}개)", 5000)

    def _show_only(self, names):
        self.c.show_only(names)
        self.refresh(rerender=True)

    def on_delete_key(self):
        """Delete: datasets when the Datasets list has focus, otherwise the plot marker."""
        if self.data_list.hasFocus():
            self.on_delete_datasets()                  # in the Datasets list: delete the data (as before)
        else:
            self.on_plot_delete(self.target_view())    # in a plot: selected marker, else the clicked curves off

    @_guard
    def on_delete_datasets(self, *_, confirm: bool = True):
        fav_rows = [it.data(NAME_ROLE) for it in self.data_list.selectedItems()
                    if it is not None and it.data(self.KIND_ROLE) == "fav" and not it.isHidden()]
        cur = self.data_list.currentItem()
        if not fav_rows and not self.data_list.selectedItems() and cur is not None and cur.data(self.KIND_ROLE) == "fav":
            fav_rows = [cur.data(NAME_ROLE)]
        if fav_rows and not any(it.data(self.KIND_ROLE) == "meas" for it in self.data_list.selectedItems()
                                if it is not None and not it.isHidden()):
            self.on_unfavorite(fav_rows)               # Delete on ★ rows: out of the favourites only
            return
        names = [it.data(NAME_ROLE) for it in self.data_list.selectedItems()
                 if it is not None and it.data(self.KIND_ROLE) == "meas" and it.data(NAME_ROLE) and not it.isHidden()]
        if not names:
            cur = self.data_list.currentItem()
            if cur is not None and cur.data(self.KIND_ROLE) == "meas":
                names = [cur.data(NAME_ROLE)]
        if not names:
            self.statusBar().showMessage("삭제할 Measurement Dataset을 선택해주세요. (Simulation은 체크 해제로 숨기기)", 4000)
            return
        labels = [self.c.measurement.sheet(n).label for n in names if self.c.measurement.sheet(n)]
        text = (f"'{labels[0]}' Dataset을 삭제할까요?" if len(names) == 1
                else f"선택한 {len(names)}개 Dataset을 삭제할까요?\n" + ", ".join(labels[:6]) + (" …" if len(labels) > 6 else ""))
        if confirm and QMessageBox.question(self, APP_TITLE, text + "\n(원본 Excel 파일은 바뀌지 않습니다)",
                                            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes) != QMessageBox.Yes:
            return
        for n in names:
            self.c.remove_sheet(n)
        self._refresh_soon()

    # ------------------------------------------------------------ ★ favourites
    @_guard
    def on_favorite(self, names, before=None):
        n = self.c.add_favorites(names, before)
        self._refresh_soon()
        self.statusBar().showMessage(f"★ 즐겨찾기에 {n}개 — 검색해도 목록의 ★ 구역에 항상 보입니다", 4000)

    @_guard
    def on_unfavorite(self, names):
        self.c.remove_favorites(names)
        self._refresh_soon()
        self.statusBar().showMessage(f"☆ 즐겨찾기에서 {len(set(names))}개 뺌 (Measurement 데이터는 그대로)", 4000)

    def on_drop_section(self, names, kinds, section, before):
        """Rows dropped in the Datasets list. True = handled here (Qt must not move anything)."""
        if section == "fav":                           # Measurement (or ★) rows -> into / within the favourites
            names = [n for n, k in zip(names, kinds) if k in ("meas", "fav")]
            if names:
                self.on_favorite(names, before)
            return True
        if section == "meas" and all(k == "meas" for k in kinds):
            return False                               # ordinary reordering of measurements
        return True                                    # anything else: no move

    @_guard
    def on_dataset_info(self, names):
        """Right-click → 데이터 정보: when / how it was added, FREQ, points, columns used."""
        from core.quantities import QUANTITIES
        mf = self.c.measurement
        rows = [mf.sheet(n) for n in names if mf and mf.sheet(n)]
        if not rows:
            return
        if len(rows) > 1:
            lines = [f"{r.label}  ·  {format_freq(r.freq_hz) if r.freq_hz else 'FREQ 없음'}  ·  "
                     f"추가 {r.added_at or '기록 없음'}" for r in rows]
            text = f"선택한 {len(rows)}개\n\n" + "\n".join(lines[:40]) + ("\n…" if len(lines) > 40 else "")
        else:
            r = rows[0]
            ds = r.dataset
            cols = ", ".join(f"{QUANTITIES[q].label} ← {r.raw.cell(r.header_row, c) if r.header_row is not None else ''}"
                             .rstrip(" ←") for q, c in r.mapping.items() if q in QUANTITIES) if r.mapping else "-"
            pout = ds.columns.get("pout") if ds is not None else None
            import numpy as _np
            rng = (f"{_np.nanmin(pout):.2f} ~ {_np.nanmax(pout):.2f} dBm" if pout is not None and len(pout)
                   and _np.isfinite(pout).any() else "-")
            text = "\n".join([
                f"이름: {r.label}", f"FREQ: {format_freq(r.freq_hz) if r.freq_hz else '없음'}",
                f"추가한 때: {r.added_at or '기록 없음 (v2.14 이전에 추가된 데이터)'}",
                f"추가 방법: {r.added_how or '-'}",
                f"마지막 수정 (Mapping / FREQ / 데이터 표): {r.modified_at or '없음'}",
                f"데이터: {ds.n_points if ds is not None else 0}점 · Pout {rng}",
                f"사용한 열: {cols}" + (" (Column Mapping 지정)" if r.user_mapping else ""),
                f"★ 즐겨찾기: {'예' if r.sheet_name in self.c.favorite_names() else '아니오'}"])
        QMessageBox.information(self, "데이터 정보", text)
        return text

    @_guard
    def on_data_reordered(self, names):
        """Drag & drop in the Datasets list -> new dataset order (legend / plot / project)."""
        if self.c.measurement and names:
            self.c.reorder_sheets(names)
        self._refresh_soon()

    def _delete_many(self, names):
        if len(names) == 1:
            self.c.sheet_name = names[0]
            self.on_delete_sheet()
            return
        if QMessageBox.question(self, APP_TITLE, f"선택한 {len(names)}개 Dataset을 목록에서 삭제할까요?",
                                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        for n in names:
            self.c.remove_sheet(n)
        self.refresh(rerender=True)

    @_guard
    def on_choose_conditions(self, *_):
        from ui.condition_dialog import ConditionDialog
        dlg = ConditionDialog(self.c.condition_keys, self)
        if dlg.exec():
            self.set_condition_keys(dlg.values())

    def set_condition_keys(self, keys):
        self.c.condition_keys = list(keys)
        self.settings.setValue("conditions", ",".join(keys))
        if self.c.measurement:
            self.c.measurement.dirty = True
        self.refresh(rerender=True)

    def _style_menu(self, menu, names, acts):
        from PySide6.QtGui import QColor, QIcon, QPixmap
        from PySide6.QtWidgets import QColorDialog
        from app.controller import DATASET_COLORS

        names = [names] if isinstance(names, str) else list(names)

        def apply(**kw):
            self.c.set_styles(names, **kw)
            self.refresh(rerender=True)

        def icon(col):
            pm = QPixmap(14, 14)
            pm.fill(QColor(col))
            return QIcon(pm)
        cm = menu.addMenu("색상")
        if len(names) > 1:
            a = cm.addAction(f"각각 다른 색으로 ({len(names)}개)")
            a.triggered.connect(lambda _=False: (self.c.set_distinct_colors(names), self.refresh(rerender=True)))
            acts["color:distinct"] = a
            a = cm.addAction("주파수별 기본 색으로")
            a.triggered.connect(lambda _=False: (self.c.reset_colors(names), self.refresh(rerender=True)))
            acts["color:auto"] = a
            cm.addSeparator()
            t = cm.addAction("모두 같은 색으로:")
            t.setEnabled(False)
        for col in DATASET_COLORS[:10] + ["#000000"]:
            a = cm.addAction(icon(col), col)
            a.triggered.connect(lambda _=False, col=col: apply(color=col))
            acts[f"color:{col}"] = a
        more = cm.addAction("다른 색…")

        def pick():
            col = QColorDialog.getColor(QColor(self.c.style_of_dataset(names[0]).get("color") or self.c.color_of(names[0])),
                                        self, "색상 선택")
            if col.isValid():
                apply(color=col.name())
        more.triggered.connect(lambda _=False: pick())
        acts["color:more"] = more
        sm = menu.addMenu("선 종류")
        for key, text, ls in (("solid", "실선 ━━", "-"), ("dashed", "파선 ╍╍", "--"), ("dotted", "점선 ┈┈", ":"),
                              ("dashdot", "1점 쇄선 ━·", "-.")):
            a = sm.addAction(text)
            a.triggered.connect(lambda _=False, ls=ls: apply(linestyle=ls))
            acts[f"style:{key}"] = a
        wm = menu.addMenu("선 굵기")
        for key, text, lw in (("thin", "가는 선", 0.8), ("normal", "보통", 1.2), ("thick", "굵은 선", 2.4)):
            a = wm.addAction(text)
            a.triggered.connect(lambda _=False, lw=lw: apply(linewidth=lw))
            acts[f"width:{key}"] = a
        mm = menu.addMenu("점 모양")
        for key, text, mk in (("o", "● 원", "o"), ("s", "■ 사각", "s"), ("^", "▲ 삼각", "^"), ("none", "없음 (선만)", "")):
            a = mm.addAction(text)
            a.triggered.connect(lambda _=False, mk=mk: apply(marker=mk))
            acts[f"marker:{key}"] = a
        reset = menu.addAction("기본 스타일로")
        reset.triggered.connect(lambda _=False: ([self.c.reset_dataset_style(n) for n in names],
                                                 self.refresh(rerender=True)))
        acts["reset"] = reset

    def _show_only_current(self):
        self.c.checked = [self.c.sheet_name]
        self.refresh(rerender=True)

    # ------------------------------------------------------------ plot callbacks
    def on_view_changed(self, what):
        self._history_note()                       # markers / zoom / pan -> an undo step (after a pause)
        if what == "range":
            self._fill_range_fields()
        elif what == "markers":
            pass

    def on_view_key(self, key):
        v = self.target_view()
        if key == "v":
            v.add_v()
        elif key == "h":
            v.add_h()
        elif key == "f":
            if v is not self.view:
                self.activate_view(v)
            self.on_auto_scale()
        elif key == "m":
            self._pin_here(v)
        elif key in ("delete", "backspace"):
            self.on_plot_delete(v)

    # ------------------------------------------------------------ plot panes
    def set_layout(self, n: int):
        self.n_panes = self.c.n_panes = max(1, min(4, int(n)))
        rows, cols = {1: (1, 1), 2: (1, 2), 3: (2, 2), 4: (2, 2)}[self.n_panes]
        for v in self.views:
            self.grid.removeWidget(v)
            v.setVisible(False)
        for r in range(2):                        # equal stretch -> identical pane sizes
            self.grid.setRowStretch(r, 1 if r < rows else 0)
        for col in range(2):
            self.grid.setColumnStretch(col, 1 if col < cols else 0)
        for i, v in enumerate(self.visible_views):
            self.grid.addWidget(v, i // cols, i % cols)
            v.setVisible(True)
        if self.c.active_pane >= self.n_panes:
            self.activate_view(self.views[0])
        idx = self.layout_combo.findData(self.n_panes)
        if idx >= 0 and self.layout_combo.currentIndex() != idx:
            self.layout_combo.blockSignals(True)
            self.layout_combo.setCurrentIndex(idx)
            self.layout_combo.blockSignals(False)
        self.render()

    @_guard
    def on_layout_changed(self, index):
        if index >= 0:
            self.set_layout(self.layout_combo.itemData(index))

    def activate_view(self, view):
        changed = getattr(self.c, "active_pane", 0) != view.idx
        own_before = self.c.pane_is_own
        self.c.set_active_pane(view.idx)
        for v in self.views:
            v.set_active(v is view)
        self._sync_pane_controls()
        if changed and (own_before or self.c.pane_is_own):
            self._fill_data_list()                     # list ticks = what THIS plot shows
        if changed and self.c.pane_is_own:
            self.statusBar().showMessage(f"Plot {view.idx + 1}: 이 Plot 전용 — Datasets에서 끌어다 놓은 것만 표시 "
                                         "(목록 체크 / 모두 체크·해제도 이 Plot에만, 곡선 클릭 후 Delete = 빼기)", 6000)

    def _sync_pane_controls(self):
        """Left panel shows the active pane's graph / ranges / marker options."""
        self._updating = True
        try:
            btn = self.graph_buttons.get(self.c.graph_key)
            if btn is not None and not btn.isChecked():
                btn.setChecked(True)
            self.values_check.setChecked(self.c.show_marker_values)
            self._fill_range_fields()
        finally:
            self._updating = False

    def compose_all_png(self) -> bytes:
        import io
        from PIL import Image
        from plotting.export import figure_png_bytes
        rows, cols = {1: (1, 1), 2: (1, 2), 3: (2, 2), 4: (2, 2)}[self.n_panes]
        imgs = []
        for v in self.visible_views:
            v.canvas.draw()
            imgs.append(Image.open(io.BytesIO(figure_png_bytes(v.figure))).convert("RGB"))
        w, h = max(i.width for i in imgs), max(i.height for i in imgs)
        sheet = Image.new("RGB", (w * cols, h * rows), "white")
        for k, im in enumerate(imgs):
            sheet.paste(im, ((k % cols) * w, (k // cols) * h))
        buf = io.BytesIO()
        sheet.save(buf, format="PNG")
        return buf.getvalue()

    @_guard
    def on_copy_all(self, *_):
        from ui.clipboard import copy_png_to_clipboard
        if copy_png_to_clipboard(self.compose_all_png()):
            self.statusBar().showMessage(f"✔ Plot {self.n_panes}개를 한 장으로 복사했습니다.", 5000)

    def build_panel_menu(self):
        c = self.c
        menu, acts = QMenu(self), {}
        acts["hide"] = menu.addAction("값 상자 숨기기")
        acts["hide"].triggered.connect(lambda _=False: self._set_values(False))
        key = "expand" if c.panel_collapsed else "collapse"
        acts[key] = menu.addAction("펼치기 (곡선별 값)" if c.panel_collapsed else "접기 (Marker 위치만)")
        acts[key].triggered.connect(lambda _=False: self._set_panel(collapsed=not c.panel_collapsed))
        lm = menu.addMenu("위치")
        for loc, name in LOC_NAMES.items():
            a = lm.addAction(("● " if loc == c.panel_loc else "   ") + name)
            a.triggered.connect(lambda _=False, loc=loc: self._set_panel(loc=loc))
            acts[f"loc:{loc}"] = a
        menu.addSeparator()
        acts["clear"] = menu.addAction("Marker 모두 삭제")
        acts["clear"].triggered.connect(lambda _=False: self.on_clear_markers())
        return menu, acts

    def _pin_here(self, view=None):
        view = view or self.target_view()
        if not view.add_pin():
            self.statusBar().showMessage("M: 곡선 위의 점 가까이에 마우스를 두고 눌러주세요.", 4000)

    def build_pin_menu(self, i):
        c = self.c
        menu, acts = QMenu(self), {}
        for key, text in (("bigger", "글자 크게"), ("smaller", "글자 작게"), ("reset", "글자 기본 크기")):
            a = menu.addAction(text)
            a.triggered.connect(lambda _=False, key=key: (c.resize_pin(i, key), self.view.redraw_markers()))
            acts[key] = a
        menu.addSeparator()
        acts["delete"] = menu.addAction("이 말풍선 삭제")
        acts["delete"].triggered.connect(lambda _=False: (c.remove_marker("p", i), self.view.redraw_markers()))
        acts["clear_pins"] = menu.addAction("말풍선 모두 삭제")
        acts["clear_pins"].triggered.connect(lambda _=False: (c.cur_pins.clear(), c.select_marker(None),
                                                              self.view.redraw_markers()))
        return menu, acts

    def build_marker_menu(self, hit):
        c = self.c
        menu, acts = QMenu(self), {}
        acts["delete"] = menu.addAction("이 Marker 삭제")
        acts["delete"].triggered.connect(lambda _=False: (c.remove_marker(*hit), self.view.redraw_markers()))
        acts["values"] = menu.addAction("값 상자 숨기기" if c.show_marker_values else "값 상자 보이기")
        acts["values"].triggered.connect(lambda _=False: self._set_values(not c.show_marker_values))
        acts["clear"] = menu.addAction("Marker 모두 삭제")
        acts["clear"].triggered.connect(lambda _=False: self.on_clear_markers())
        return menu, acts

    @_guard
    def on_plot_context(self, what):
        kind, arg = what
        menu, _ = (self.build_panel_menu() if kind == "panel" else self.build_pin_menu(arg) if kind == "pin"
                   else self.build_plot_menu(arg) if kind == "plot" else self.build_marker_menu(arg))
        menu.exec(QCursor.pos())

    @_guard
    def on_edit_labels(self, view=None, part="title", values=None):
        """Own plot title / axis titles for this plot (double-click on them or Plot right-click)."""
        view = view or self.target_view()
        with self.c.using_pane(view.idx):
            cur, defaults = self.c.plot_labels(), self.c.default_labels()
        if values is None:
            from ui.labels_dialog import LabelsDialog
            dlg = LabelsDialog(cur, defaults, self, focus=part)
            if not dlg.exec():
                return
            values = dlg.values()
        with self.c.using_pane(view.idx):
            self.c.set_plot_labels(**{k: values.get(k) for k in ("title", "x", "y")})
        view.render()
        self._history_note()
        self.statusBar().showMessage("제목 / 축 이름 변경 (비우면 기본 이름, Ctrl+Z = 되돌리기)", 3000)

    PLOT_MENU_MAX = 40          # datasets listed one by one in the plot menu (more -> use the Datasets list)

    def build_plot_menu(self, key=None):
        """Right-click in the plot (no drag; right-drag = box zoom): what to plot and how."""
        menu, acts = QMenu(self), {}
        c = self.c
        mf = c.measurement
        if key:                                         # on a curve: that dataset first
            if key.startswith("sim:"):
                head = menu.addAction(f"곡선: Simulation {key[4:]}")
            else:
                r = mf.sheet(key) if mf else None
                head = menu.addAction(f"곡선: {r.label if r else key}" + (f" · {format_freq(r.dataset.freq_hz)}"
                                      if r and r.dataset is not None and r.dataset.freq_hz else ""))
            head.setEnabled(False)
            sub, sub_acts = self.build_data_menu([key])
            sub.setTitle("이 데이터")
            menu.addMenu(sub)
            acts["curve"] = sub
            acts.update({"curve:" + k: a for k, a in sub_acts.items()})
            acts["open"] = menu.addAction("데이터 보기/수정…")
            acts["open"].triggered.connect(lambda _=False: self.on_open_dataset(key))
            menu.addSeparator()
        gm = menu.addMenu("그래프 종류")
        for g in c.graphs:
            a = gm.addAction(g.short_name)
            a.setCheckable(True)
            a.setChecked(g.key == c.graph_key)
            a.triggered.connect(lambda _=False, k=g.key: self.graph_buttons[k].setChecked(True))
            acts["graph:" + g.key] = a
        dm = menu.addMenu("Datasets 표시")
        rows = [r for r in (mf.sheets if mf else []) if r.ok]
        for r in rows[:self.PLOT_MENU_MAX]:
            a = dm.addAction(f"{r.label} · {format_freq(r.dataset.freq_hz)}" if r.dataset.freq_hz else r.label)
            a.setCheckable(True)
            a.setChecked(r.sheet_name in c.checked)
            a.setEnabled(not c.is_muted(r.sheet_name))
            a.triggered.connect(lambda on=False, n=r.sheet_name: self.set_dataset_shown(n, bool(on)))
            acts["show:" + r.sheet_name] = a
        if len(rows) > self.PLOT_MENU_MAX:
            more = dm.addAction(f"… 외 {len(rows) - self.PLOT_MENU_MAX}개 (Datasets 목록에서 선택)")
            more.setEnabled(False)
        acts["all"] = menu.addAction("모두 표시")
        acts["all"].triggered.connect(lambda _=False: self.set_all_checked(True))
        acts["none"] = menu.addAction("모두 해제")
        acts["none"].triggered.connect(lambda _=False: self.set_all_checked(False))
        n_sel = sum(1 for it in self.data_list.selectedItems() if it is not None and not it.isHidden()
                    and it.data(self.KIND_ROLE) in ("meas", "fav"))
        acts["selected"] = menu.addAction(f"선택 Plot ({n_sel}개)" if n_sel else "선택 Plot (목록에서 선택 없음)")
        acts["selected"].setEnabled(bool(n_sel))
        acts["selected"].triggered.connect(lambda _=False: self.on_selected_show_only())
        if self._filter_terms():
            acts["filtered"] = menu.addAction("검색 결과만 Plot")
            acts["filtered"].triggered.connect(lambda _=False: self.on_filter_show_only())
        menu.addSeparator()
        mk = menu.addMenu("Marker")
        v = self.target_view()
        for k, label, fn in (("v", "V Marker 여기 (V)", lambda: v.add_v()), ("h", "H Marker 여기 (H)", lambda: v.add_h()),
                             ("m", "M 말풍선 여기 (M)", lambda: self._pin_here(v)),
                             ("clear", "Marker 모두 삭제", lambda: self.on_clear_markers())):
            acts["mk:" + k] = mk.addAction(label)
            acts["mk:" + k].triggered.connect(lambda _=False, f=fn: f())
        acts["labels"] = menu.addAction("제목 / 축 이름 바꾸기… (더블클릭으로도)")
        acts["labels"].triggered.connect(lambda _=False, v=v: self.on_edit_labels(v, "title"))
        ax_m = menu.addMenu("축")
        for k, label, fn in (("fit", "Auto Fit (F)", self.on_auto_scale), ("reset", "Zoom Reset", self.on_reset),
                             ("reset_all", "Axis Reset (모든 Plot)", self.on_reset_all_axes)):
            acts[k] = ax_m.addAction(label)
            acts[k].triggered.connect(lambda _=False, f=fn: f())
        st = menu.addMenu("색 / 선")
        for k, label, fn in (("distinct", "각각 다른 색", self.on_colors_distinct),
                             ("by_freq", "주파수별 기본색", self.on_colors_by_freq),
                             ("lines", "선 기본 스타일", self.on_lines_default)):
            acts[k] = st.addAction(label)
            acts[k].triggered.connect(lambda _=False, f=fn: f())
        menu.addSeparator()
        for k, label, fn in (("copy", "Plot 복사", self.on_copy_plot), ("png", "PNG로 저장… (Ctrl+Alt+S)", self.on_save_png),
                             ("excel", "Excel로 내보내기…", self.on_export_excel)):
            acts[k] = menu.addAction(label)
            acts[k].triggered.connect(lambda _=False, f=fn: f())
        return menu, acts

    @_guard
    def set_dataset_shown(self, name, on: bool):
        """Plot menu → Datasets 표시: show / hide one dataset."""
        if on and name not in self.c.checked and not self.c.is_muted(name):
            self.c.checked = list(self.c.checked) + [name]
        elif not on:
            self.c.checked = [n for n in self.c.checked if n != name]
        self.refresh(rerender=True)

    def _set_values(self, on: bool):
        self.c.show_marker_values = on
        self.values_check.blockSignals(True)
        self.values_check.setChecked(on)
        self.values_check.blockSignals(False)
        self.view.redraw_markers()

    def _set_panel(self, collapsed=None, loc=None):
        if collapsed is not None:
            self.c.panel_collapsed = collapsed
        if loc:
            self.c.panel_loc = loc
            self.c.panel_xy = None                    # a fixed corner again (undo a drag)
        self.view.redraw_markers()

    @_guard
    def on_values_toggle(self, on):
        self._set_values(bool(on))

    @_guard
    def on_clear_markers(self, *_):
        self.c.clear_markers()
        self.view.redraw_markers()

    @_guard
    def on_steps(self, *_):
        try:
            x = float(self.step_edits["x"].text() or 0)
            g = float(self.step_edits["gain"].text() or 0)
            p = float(self.step_edits["pae"].text() or 0)
        except ValueError:
            QMessageBox.warning(self, APP_TITLE, "단위에는 숫자만 입력할 수 있습니다.")
            return
        self.c.snap_x = max(0.0, x)
        self.c.snap_y.update(gain=max(0.0, g), pae=max(0.0, p))

    # ------------------------------------------------------------ back-off
    @_guard
    def on_backoff(self, *_):
        errs = self.c.set_backoffs(self.bo_edit.text())
        if errs:
            QMessageBox.warning(self, APP_TITLE, "\n".join(errs))
            return
        self._backoff_changed()

    @_guard
    def on_backoff_add(self, *_):
        old = list(self.c.backoffs)
        errs = self.c.set_backoffs(self.bo_edit.text())
        if errs:
            QMessageBox.warning(self, APP_TITLE, "\n".join(errs))
            return
        self.c.backoffs = sorted(set(old) | set(self.c.backoffs))
        self._backoff_changed()

    @_guard
    def on_backoff_clear(self, *_):
        self.c.backoffs = []
        self._backoff_changed()

    def _backoff_changed(self):
        self.bo_edit.setText(", ".join(f"{b:g}" for b in self.c.backoffs))
        self._fill_backoff()
        for v in self.visible_views:          # every plot shows the back-off points
            v.redraw_markers()

    def _fill_backoff(self):
        rows = self.c.backoff_rows()
        head = ["Dataset", "Psat (dBm)", "BO (dB)", "Pout (dBm)", "Gain (dB)", "PAE (%)"]
        self.bo_table.setColumnCount(len(head))
        self.bo_table.setHorizontalHeaderLabels(head)
        self.bo_table.setRowCount(len(rows))
        import math
        for i, r in enumerate(rows):
            vals = [r["label"], f"{r['psat']:.2f}", f"{r['bo']:g}", f"{r['pout']:.2f}",
                    "" if math.isnan(r["gain"]) else f"{r['gain']:.2f}", "" if math.isnan(r["pae"]) else f"{r['pae']:.2f}"]
            for j, v in enumerate(vals):
                self.bo_table.setItem(i, j, QTableWidgetItem(v))
        self.bo_table.resizeColumnsToContents()

    def _show_messages(self):
        msgs = list(self.c.status_messages())
        lines = [f"{_LEVEL_PREFIX.get(m.level, '')} {m.text}" for m in msgs]
        if self.c.measurement:
            lines.append("")
            lines += [f"{_LEVEL_PREFIX.get(m.level, '')} {m.text}" for m in self.c.measurement.messages]
        self.msg_view.setPlainText("\n".join(lines))
        errs = [m for m in msgs if m.level == Level.ERROR]
        warns = [m for m in msgs if m.level == Level.WARNING]
        if errs:
            self.statusBar().showMessage("❌ " + errs[0].text)
        elif warns:
            self.statusBar().showMessage("⚠ " + warns[0].text)
        elif self.c.measurement_dataset is not None:
            sim = self.c.simulation_dataset()
            self.statusBar().showMessage(
                f"{self.c.measurement.file_name} / {self.c.sheet_result.label}  —  "
                f"{self.c.measurement_dataset.n_points} pts"
                + (f"  |  Simulation: {sim.name}" if sim else ""))

    def _refresh_sim_combo(self):
        self.sim_combo.blockSignals(True)
        self.sim_combo.clear()
        from app.controller import ALL_SETS
        if len(self.c.simulation.set_names) > 1:
            self.sim_combo.addItem("모든 Set", ALL_SETS)
        for n in self.c.simulation.set_names:
            self.sim_combo.addItem(n, n)
        if not self.c.simulation.set_names:
            self.sim_combo.addItem("(없음)", None)
        idx = self.sim_combo.findData(self.c.sim_set)
        self.sim_combo.setCurrentIndex(max(idx, 0))
        self.sim_combo.setToolTip("자동 매칭에 쓸 Simulation Set\n" + (
            "\n".join(self.c.sim_files) if self.c.sim_files else self.c.sim_root))
        self.sim_combo.blockSignals(False)

    # ================================================================== slots
    @_guard
    def on_open(self, *_):
        start = self.settings.value("last_dir", "")
        path, _f = QFileDialog.getOpenFileName(self, "Measurement Excel 열기", start, EXCEL_FILTER)
        if path:
            self.open_file(path)

    @_guard
    def on_open_example(self, *_):
        self.open_file(example_measurement_path())

    def _confirm_discard(self) -> bool:
        """True if it is OK to replace the current (unsaved) data."""
        if not self.c.unsaved:
            return True
        ans = QMessageBox.question(self, APP_TITLE, "붙여넣은/수정한 데이터가 저장되지 않았습니다. 프로젝트로 저장할까요?",
                                   QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if ans == QMessageBox.Save:
            return self.on_save_project_quick() is not None
        return ans == QMessageBox.Discard

    def open_file(self, path: str):
        if not self._confirm_discard():
            return
        msgs = self.c.load_measurement(path)
        errs = [m for m in msgs if m.level == Level.ERROR]
        if self.c.measurement is None or self.c.measurement.path != path:
            QMessageBox.warning(self, APP_TITLE, "\n".join(m.text for m in errs) or "파일을 열 수 없습니다.")
            return
        self.settings.setValue("last_dir", os.path.dirname(path))
        self.setWindowTitle(f"{APP_TITLE} — {os.path.basename(path)}")
        self.history.reset()                               # Ctrl+Z never jumps back into the previous file
        self._filter = ""                                  # a search typed earlier must not hide
        self.filter_edit.blockSignals(True)                # datasets of the file just opened
        self.filter_edit.setText("")
        self.filter_edit.blockSignals(False)
        for kind in ("meas", "sim"):
            if self._list_folded(kind):
                self.settings.setValue(f"fold/{kind}", False)
        if path.lower().endswith(".pam"):
            self.statusBar().showMessage(f"프로젝트 열기: Dataset {len(self.c.measurement.sheets)}개 — {path}", 10000)
            self.set_layout(getattr(self.c, "n_panes", self.n_panes))
            self._sync_pane_controls()
            extras = (getattr(self.c.measurement, "settings", {}) or {}).get("extras") or {}
            for name, (_has, _exp, imp) in self.project_extras.items():
                if name in extras:
                    imp(extras[name])                  # e.g. the S-Parameter tab of the same project
        if errs:
            QMessageBox.warning(self, APP_TITLE, "\n".join(m.text for m in errs))
        self.refresh(rerender=True)

    @_guard
    def on_sim_changed(self, index: int):
        if index < 0:
            return
        self.c.select_sim_set(self.sim_combo.itemData(index))
        self.refresh(rerender=True)

    @_guard
    def on_sim_folder(self, *_):
        d = QFileDialog.getExistingDirectory(self, "Simulation 폴더 선택", self.c.sim_root)
        if not d:
            return
        self.c.load_simulation(d)
        self.settings.setValue("sim_root", d)
        self.settings.setValue("sim_files", "")
        self._refresh_sim_combo()
        if not self.c.simulation.set_names:
            QMessageBox.warning(self, APP_TITLE, "선택한 폴더에서 Simulation Dataset을 찾지 못했습니다.\n"
                                                 "README의 'Simulation Data Format'을 확인해주세요.")
        self.refresh(rerender=True)

    @_guard
    def on_sim_files(self, *_):
        from utils.paths import user_root
        start = self.settings.value("sim_files_dir", "") or os.path.join(user_root(), "sim_data")
        paths, _f = QFileDialog.getOpenFileNames(self, "Simulation 파일 선택 (여러 개)", start,
                                                 "Simulation (*.txt *.dat *.tsv *.csv *.xlsx);;All files (*.*)")
        if paths:
            self.set_sim_files(paths)

    def set_sim_files(self, paths):
        msgs = self.c.load_simulation_files(paths)
        self.settings.setValue("sim_files", "|".join(self.c.sim_files))
        self.settings.setValue("sim_files_dir", os.path.dirname(paths[0]))
        self._refresh_sim_combo()
        warn = [m.text for m in msgs if m.level != Level.INFO]
        if warn:
            QMessageBox.warning(self, APP_TITLE, "\n".join(warn))
        self.refresh(rerender=True)

    @_guard
    def on_graph_toggled(self, button, checked: bool):
        if not checked or self._updating:
            return
        self.hover.hide()
        self.c.select_graph(button.property("graph_key"))       # active pane only
        self.view.render()
        self._fill_range_fields()

    @_guard
    def on_apply_range(self, *_):
        try:
            x = (float(self.xmin_edit.text()), float(self.xmax_edit.text()))
            y = (float(self.ymin_edit.text()), float(self.ymax_edit.text()))
        except ValueError:
            QMessageBox.warning(self, APP_TITLE, "Axis Range에는 숫자만 입력할 수 있습니다.")
            return
        msgs = self.c.set_ranges(x, y)
        if msgs:
            QMessageBox.warning(self, APP_TITLE, "\n".join(m.text for m in msgs))
        self.render()

    @_guard
    def on_auto_scale(self, *_):
        """F: show every curve completely."""
        self.c.auto_scale()
        self.render()

    @_guard
    def on_reset(self, *_):
        self.c.reset_ranges()
        self.render()

    @_guard
    def on_highlight_toggle(self, checked: bool):
        self.c.highlight_selected = bool(checked)
        self.settings.setValue("highlight_selected", bool(checked))
        self.render()

    @_guard
    def on_condition_toggle(self, checked: bool):
        self.c.show_condition_in_plot = checked
        self.render()

    # ------------------------------------------------------------ paste / project
    @_guard
    def on_paste(self, *_):
        from PySide6.QtGui import QGuiApplication
        from ui import clip_datasets
        payload = clip_datasets.get(clip_datasets.MIME_PA)
        if payload:                                    # datasets copied with Ctrl+C in a Datasets list
            self.paste_datasets(payload)
            return
        self.paste_text(QGuiApplication.clipboard().text())

    @_guard
    def on_toggle_many(self, names, on: bool):
        """Space with several rows selected: plot / un-plot all of them at once."""
        for n in names:
            if n.startswith("sim:"):
                self.c.toggle_sim(n[4:], on)
                if on:
                    self.c.set_muted([n], False)
            else:
                self.c.set_checked(n, on)
        self._refresh_soon()
        self.statusBar().showMessage((f"{len(names)}개 " if len(names) > 1 else "") + ("표시 (고정)" if on else "표시 안 함"), 3000)

    @_guard
    def on_copy_datasets(self, *_):
        """Ctrl+C in the Datasets list: the selected measurements -> clipboard (paste with Ctrl+V,
        also into another running copy of the program; Excel gets the numbers as text)."""
        from ui import clip_datasets
        sel = [it.data(NAME_ROLE) for it in self.data_list.selectedItems()
               if it is not None and not it.isHidden() and it.data(self.KIND_ROLE) in ("meas", "fav", "sim")]
        sel = list(dict.fromkeys(sel))                 # a ★ row and its Measurement row = one dataset
        if not sel and self.c.sheet_name:
            sel = [self.c.sheet_name]
        names = [n for n in sel if not n.startswith("sim:")]
        if not names:
            self.statusBar().showMessage("Simulation은 복사할 수 없습니다 (Measurement Dataset을 선택하세요)", 4000)
            return
        payload = self.c.datasets_payload(names)
        mf = self.c.measurement
        clip_datasets.put(clip_datasets.MIME_PA, payload,
                          clip_datasets.table_text([(mf.sheet(n).label, mf.sheet(n).raw.rows) for n in names]))
        skipped = len(sel) - len(names)
        self.statusBar().showMessage(f"📋 Dataset {len(names)}개 복사 — Ctrl+V로 붙여넣기 (다른 창의 프로그램에도 가능)"
                                     + (f" · Simulation {skipped}개는 제외" if skipped else ""), 6000)

    def _copy_names(self, names):
        for i in range(self.data_list.count()):
            it = self.data_list.item(i)
            it.setSelected(it.data(NAME_ROLE) in names)
        self.on_copy_datasets()

    def paste_datasets(self, payload: dict):
        names = self.c.paste_datasets(payload)
        if not names:
            return
        self._scroll_to_current = True                 # (the search is kept)
        self.refresh(rerender=True)
        self.statusBar().showMessage(f"✔ Dataset {len(names)}개 붙여넣기 (이름·FREQ·스타일 그대로) — 저장하려면 프로젝트 저장", 6000)

    def paste_text(self, text: str, ask_freq: bool = True, name: str = None, freq_hz: float = None):
        """ask_freq=True: show the name/FREQ dialog (interactive). Tests pass name/freq directly."""
        name0, msgs = self.c.paste_text(text)
        auto_preset = None
        if name0 is not None and self.c.sheet_result is not None and not self.c.sheet_result.ok:
            auto_preset = self._try_mapping_presets(name0)     # not recognised -> the saved favourite mapping
            if auto_preset:
                msgs = list(self.c.measurement.sheet(name0).messages)
        if name0 is not None and self.c.sheet_result is not None:
            if ask_freq:
                from measurement import mapping_presets
                last = str(self.settings.value("last_paste_name", "") or "")
                presets = [p.get("name") for p in mapping_presets.load(self.settings)]
                try:
                    dlg = PasteDialog(self.c.sheet_result, self, last_name=last, presets=presets, preset=auto_preset,
                                      on_preset=lambda nm: self._paste_preset(name0, nm))
                except TypeError:                          # (dialog replaced in tests)
                    try:
                        dlg = PasteDialog(self.c.sheet_result, self, last_name=last)
                    except TypeError:
                        dlg = PasteDialog(self.c.sheet_result, self)
                if not dlg.exec():
                    self.c.cancel_paste(name0)
                    self.refresh(rerender=True)
                    return
                name, freq_hz = dlg.values()
                msgs = list(self.c.measurement.sheet(name0).messages) if self.c.measurement.sheet(name0) else msgs
                if name:
                    self.settings.setValue("last_paste_name", name)   # offered again next time
            if freq_hz:
                self.c.measurement.set_frequency(name0, freq_hz)
            if name and name != name0:
                self.c.rename_sheet(name0, name)
            ask_freq = False
        name = self.c.sheet_name if name0 is not None else None
        hidden_by_search = False                       # the search stays; just say so if it hides the new data
        if name and self._filter_terms() and self.c.measurement:
            mf = self.c.measurement
            r = mf.sheet(name)
            text = f"{mf.sheet_label(name)} {name} {format_freq(r.dataset.freq_hz) if (r and r.dataset is not None and r.dataset.freq_hz) else ''}"
            hidden_by_search = not self._match(text, self._filter_terms())
        errs = [m for m in msgs if m.level == Level.ERROR]
        if name is None or (errs and self.c.sheet_result is not None and not self.c.sheet_result.ok):
            QMessageBox.warning(self, APP_TITLE, "\n".join(m.text for m in errs) +
                                "\n\n팁: Header(Pout / PAE / Gain)가 포함되도록 복사해 주세요."
                                "\n Header가 다르면 [Column Mapping…]으로 지정할 수 있습니다.")
            self.refresh(rerender=True)
            return
        self.refresh(rerender=True)
        if ask_freq and self.c.current_freq_hz is None:
            self.on_set_freq()
        label = self.c.measurement.sheet(name).label if self.c.measurement and self.c.measurement.sheet(name) else name
        self.statusBar().showMessage(
            f"✔ '{label}' 추가 — " + (f"지금 검색('{self._filter}')에는 안 맞아 목록에서 가려져 있습니다 (Plot에는 표시)"
                                      if hidden_by_search else "프로젝트로 저장하면 다음에 다시 열 수 있습니다 (Ctrl+S)"), 8000)

    def _paste_preset(self, name, preset):
        """Paste dialog '맵핑': read the pasted data again with this favourite (None = automatic)."""
        from measurement import mapping_presets
        mf = self.c.measurement
        r = mf.sheet(name) if mf else None
        if r is None:
            return None
        if preset is None:
            return mf.reparse(name, keep_mapping=False)
        p = next((x for x in mapping_presets.load(self.settings) if x.get("name") == preset), None)
        if p is None:
            return r
        hr, m = mapping_presets.resolve(p, r)
        new = mf.apply_mapping(name, hr, m)
        self.settings.setValue("mapping_last", preset)
        return new

    def _try_mapping_presets(self, name):
        """Automatic recognition failed: try the last used favourite mapping first, then the others.
        Returns the name of the one that worked (data now read with it), or None (left as it was)."""
        from measurement import mapping_presets
        presets = mapping_presets.load(self.settings)
        if not presets:
            return None
        last = str(self.settings.value("mapping_last", "") or "")
        presets.sort(key=lambda p: p.get("name") != last)
        mf = self.c.measurement
        for p in presets:
            r = mf.sheet(name)
            hr, m = mapping_presets.resolve(p, r)
            if "pout" not in m or ("gain" not in m and "pae" not in m):
                continue
            if mf.apply_mapping(name, hr, m).ok:
                return p.get("name")
            mf.reparse(name, keep_mapping=False)
        return None

    @_guard
    def current_project_path(self):
        mf = self.c.measurement
        p = mf.path if mf is not None else ""
        return p if (p and p.lower().endswith(".pam") and os.path.isdir(os.path.dirname(os.path.abspath(p)))) else None

    def on_save_project_quick(self, *_):
        """Ctrl+S: overwrite the open project right away (no project yet -> ask where, like Save As)."""
        path = self.current_project_path()
        if path is None:
            return self.on_save_project()
        return self.on_save_project(path=path)

    @_guard
    def on_sort_datasets(self, *_):
        if self.c.sort_by_name_and_freq():
            self.refresh(rerender=True)
            self.statusBar().showMessage("정렬: 같은 이름끼리, 주파수 낮은 순 (Ctrl+Z = 되돌리기)", 4000)
        else:
            self.statusBar().showMessage("이미 정렬되어 있습니다", 3000)

    def _extras(self) -> dict:
        return {name: export() for name, (has, export, _imp) in self.project_extras.items() if has()}

    def on_save_project(self, *_, path=None):
        mf = self.c.measurement
        extras = self._extras()
        if (mf is None or not mf.sheets) and not extras:
            self.statusBar().showMessage("저장할 데이터가 없습니다 (PA / S-Parameter 모두 비어 있음)", 4000)
            return None
        if mf is None:
            default = os.path.join(self.settings.value("last_dir", ""), "RF_project.pam")
        elif mf.path.lower().endswith(".pam"):
            default = mf.path
        else:
            base = os.path.splitext(os.path.basename(mf.path))[0] if os.path.isfile(mf.path) else "PA_measurement"
            default = os.path.join(self.settings.value("last_dir", ""), base + ".pam")
        if path is None:                               # Save As (Ctrl+Shift+S) or the first save
            path, _f = QFileDialog.getSaveFileName(self, "프로젝트 저장", default, PROJECT_FILTER)
            if not path:
                return None
        try:
            saved = self.c.save_project(path, extras)
        except Exception as exc:                       # never pretend it was saved
            traceback.print_exc()
            QMessageBox.critical(self, APP_TITLE, f"프로젝트를 저장하지 못했습니다.\n\n{exc}\n\n"
                                                  "다른 이름/위치로 다시 저장해 보세요.")
            return None
        self.settings.setValue("last_dir", os.path.dirname(saved))
        self.settings.setValue("last_project", saved)
        self.refresh(rerender=False)
        n = len(self.c.measurement.sheets) if self.c.measurement else 0
        sp = f" + S-Parameter 파일 {len(extras['sparam'].get('files', []))}개" if extras.get("sparam") else ""
        self.statusBar().showMessage(f"✔ 프로젝트 저장 (확인 완료): Dataset {n}개{sp} → {saved}", 10000)
        return saved

    @_guard
    def on_rename_sheet(self, *_):
        if self.c.sheet_name:
            self.on_rename_dataset(self.c.sheet_name)

    @_guard
    def on_rename_dataset(self, name: str):
        """Rename a measurement dataset (Datasets list: double-click / F2, button, right-click menu)."""
        mf = self.c.measurement
        r = mf.sheet(name) if (mf and name) else None
        if r is None:
            return
        text, ok = QInputDialog.getText(self, "Dataset 이름 변경", "새 이름 (다른 Dataset과 같아도 됩니다):", text=r.label)
        if ok and str(text).strip():
            self.c.rename_sheet(r.sheet_name, str(text).strip())
            self._refresh_soon()

    @_guard
    def on_delete_sheet(self, *_):
        r = self.c.sheet_result
        if r is None:
            return
        if QMessageBox.question(self, APP_TITLE, f"'{r.label}' Dataset을 목록에서 삭제할까요?\n(원본 Excel 파일은 바뀌지 않습니다)",
                                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        self.c.remove_sheet(r.sheet_name)
        self.refresh(rerender=True)

    UI_BASE = (1500, 920)

    def ui_scale_for(self, w: int, h: int) -> float:
        """UI fonts shrink with the window (down to 78 %)."""
        return max(0.78, min(1.0, min(w / self.UI_BASE[0], h / self.UI_BASE[1]) ** 0.6))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        try:
            self._font_timer.start()
        except Exception:
            pass

    def apply_ui_scale(self, w: int = None, h: int = None):
        w = w if w is not None else self.width()
        h = h if h is not None else self.height()
        k = self.ui_scale_for(w, h)
        if abs(k - self._ui_scale) < 0.02:
            return
        self._ui_scale = k
        f = self.centralWidget().font()
        f.setPointSizeF(round(self._base_pt * k, 1))
        self.centralWidget().setFont(f)

    def closeEvent(self, event):
        if self._confirm_discard():
            event.accept()
        else:
            event.ignore()

    @_guard
    def on_set_freq(self, *_):
        r = self.c.sheet_result
        if r is None:
            return
        cur = format_freq(r.freq_hz) if r.freq_hz else ""
        text, ok = QInputDialog.getText(self, "FREQ 지정",
                                        f"'{r.label}'의 FREQ를 입력하세요\n(예: 3.6GHz, 3600MHz, 3.6)",
                                        text=cur)
        if not ok or not text.strip():
            return
        hz = parse_frequency(text)
        if hz is None:
            QMessageBox.warning(self, APP_TITLE, f"FREQ를 인식할 수 없습니다: {text}")
            return
        self.c.measurement.set_frequency(r.sheet_name, hz)
        self.refresh(rerender=True)

    @_guard
    def on_column_mapping(self, *_):
        r = self.c.sheet_result
        if r is None:
            return
        dlg = ColumnMappingDialog(r, self, settings=self.settings)
        if not dlg.exec():
            return
        header_row, mapping = dlg.values()
        new = self.c.measurement.apply_mapping(r.sheet_name, header_row, mapping)
        if not new.ok:
            QMessageBox.warning(self, APP_TITLE, "\n".join(m.text for m in new.errors()))
        self.refresh(rerender=True)

    @_guard
    def apply_mapping_preset(self, preset_name, names):
        """Right-click → 맵핑 즐겨찾기 → 맵핑1: apply that saved Column Mapping to the selected datasets."""
        from measurement import mapping_presets
        p = next((x for x in mapping_presets.load(self.settings) if x.get("name") == preset_name), None)
        mf = self.c.measurement
        if p is None or mf is None:
            return
        bad = []
        for n in names:
            r = mf.sheet(n)
            if r is None:
                continue
            hr, m = mapping_presets.resolve(p, r)
            if "pout" not in m:
                bad.append(r.label)
                continue
            if not mf.apply_mapping(n, hr, m).ok:
                bad.append(r.label)
        self.settings.setValue("mapping_last", preset_name)
        self.refresh(rerender=True)
        self.statusBar().showMessage(f"'{preset_name}' 맵핑 적용: {len(names) - len(bad)}개"
                                     + (f" · 실패 {len(bad)}개 ({', '.join(bad[:3])})" if bad else ""), 6000)

    @_guard
    def on_copy_plot(self, *_):
        """All plots on screen as one image (one plot if only one is shown)."""
        png = self.compose_all_png() if self.n_panes > 1 else self._one_png()
        if png and copy_png_to_clipboard(png):
            self.statusBar().showMessage(f"✔ Plot {self.n_panes}개를 Clipboard에 복사했습니다. PowerPoint 등에 Ctrl+V 하세요.", 6000)
        else:
            QMessageBox.warning(self, APP_TITLE, "Clipboard 복사에 실패했습니다.")

    def _one_png(self):
        if self.renderer.ax is None:
            return None
        self.canvas.draw()
        return figure_png_bytes(self.figure)

    @_guard
    def on_copy_one(self, *_):
        png = self._one_png()
        if png and copy_png_to_clipboard(png):
            self.statusBar().showMessage("✔ 선택한 Plot을 복사했습니다.", 5000)

    def default_export_name(self) -> str:
        if self.n_panes == 1:
            return self.c.default_export_name()
        from utils.freq import freq_tag
        return f"PA_{self.n_panes}plots_{freq_tag(self.c.current_freq_hz) if self.c.current_freq_hz else 'plots'}.png"

    @_guard
    def on_save_png(self, *_):
        if self.renderer.ax is None:
            return
        start_dir = self.settings.value("save_dir", "")
        name = self.default_export_name()
        default = os.path.join(start_dir, name) if start_dir else name
        path, _f = QFileDialog.getSaveFileName(self, "Save PNG (화면 전체)", default, "PNG Image (*.png)")
        if not path:
            return
        saved = self.save_all_png(path)
        self.settings.setValue("save_dir", os.path.dirname(saved))
        self.statusBar().showMessage(f"✔ 저장 완료: {saved}", 6000)

    # ========================================================== undo / redo
    def _history_note(self, delay: int = 350):
        """Record the state as an undo step once things have been quiet for `delay` ms
        (a pan / drag / held arrow key becomes ONE step, not hundreds)."""
        if self._restoring:
            return
        from PySide6.QtCore import QTimer
        self._hist_gen += 1
        gen = self._hist_gen

        def run():
            if gen == self._hist_gen and not self._restoring:
                self.history.note(self.c.snapshot())
        QTimer.singleShot(delay, run)

    def _history_flush(self):
        self._hist_gen += 1                        # cancel the pending timer and record right now
        self.history.note(self.c.snapshot())

    @_guard
    def on_undo(self, *_):
        self._history_flush()
        self._apply_history(self.history.undo(), "되돌리기", "더 되돌릴 작업이 없습니다")

    @_guard
    def on_redo(self, *_):
        self._history_flush()
        self._apply_history(self.history.redo(), "다시 실행", "다시 실행할 작업이 없습니다")

    def _apply_history(self, snap, what, empty_msg):
        if snap is None:
            self.statusBar().showMessage(empty_msg, 3000)
            return
        self._restoring = True
        try:
            self.c.restore(snap)
            n = snap["n_panes"]
            if n != self.n_panes:
                self.layout_combo.blockSignals(True)
                self.layout_combo.setCurrentIndex(max(0, min(3, n - 1)))
                self.layout_combo.blockSignals(False)
                self.set_layout(n)
            for box, val in ((self.sim_enable_check, self.c.sim_enabled), (self.sim_auto_check, self.c.auto_sim)):
                box.blockSignals(True)
                box.setChecked(val)
                box.blockSignals(False)
            self.refresh(rerender=True)
            self._sync_pane_controls()
        finally:
            self._restoring = False
        self.statusBar().showMessage(f"↶ {what} (남은 되돌리기 {len(self.history.undo_stack)}번 · "
                                     f"다시 실행 {len(self.history.redo_stack)}번)", 3000)

    # ========================================================== axis reset (all plots)
    @_guard
    def on_reset_all_axes(self, *_):
        for v in self.visible_views:
            with self.c.using_pane(v.idx):
                self.c.reset_ranges()
        self.render()
        self.statusBar().showMessage("모든 Plot의 축 범위를 기본값으로 되돌렸습니다", 3000)

    # ========================================================== colours of what is plotted
    @_guard
    def on_colors_distinct(self, *_):
        names = [r.sheet_name for r in self.c.visible_sheets()]
        if not names:
            return
        self.c.set_distinct_colors(names)
        self.refresh(rerender=True)
        self.statusBar().showMessage(f"측정 {len(names)}개를 서로 다른 색으로", 3000)

    @_guard
    def on_lines_default(self, *_):
        names = [r.sheet_name for r in self.c.visible_sheets()] + ["sim:" + d.name for d in self.c.visible_sims()]
        self.c.reset_line_styles(names)
        self.refresh(rerender=True)
        self.statusBar().showMessage("선 종류·굵기·점 모양을 기본으로 되돌렸습니다 (색은 그대로)", 3000)

    # ------------------------------------------------------------ 선1 / 선2 (line favourites)
    def _update_line_fav_buttons(self):
        from ui import line_favs
        for i, b in self.line_fav_btns.items():
            st = line_favs.load(self.settings, i)
            b.setText(f"{line_favs.glyph(st)} 선{i}")
            b.setToolTip(f"선{i} 즐겨찾기: {line_favs.describe(st)}\n"
                         "목록에서 고른 데이터(없으면 지금 데이터)에 적용 — 색은 그대로\n우클릭 = 선{i} 모양 바꾸기")

    def _line_fav_targets(self):
        sel = list(dict.fromkeys(it.data(NAME_ROLE) for it in self.data_list.selectedItems()
                                 if it is not None and not it.isHidden()
                                 and it.data(self.KIND_ROLE) in ("meas", "fav", "sim")))
        return sel or ([self.c.highlight_sim] if self.c.highlight_sim else []) or \
            ([self.c.sheet_name] if self.c.sheet_name else [])

    @_guard
    def on_line_fav(self, i, names=None):
        """[선1] / [선2]: that saved line look on the chosen datasets (colour kept)."""
        from ui import line_favs
        names = list(names) if names else self._line_fav_targets()
        if not names:
            self.statusBar().showMessage("선을 바꿀 데이터를 목록에서 선택하세요", 3000)
            return
        st = line_favs.load(self.settings, i)
        self.c.set_styles(names, linestyle=st["linestyle"], linewidth=float(st["linewidth"]), marker=st["marker"])
        self.refresh(rerender=True)
        self.statusBar().showMessage(f"선{i} ({line_favs.describe(st)}) → {len(names)}개", 3000)

    def build_line_fav_menu(self, i):
        from ui import line_favs
        menu, acts = QMenu(self), {}
        st = line_favs.load(self.settings, i)
        t = menu.addAction(f"선{i}: {line_favs.describe(st)}")
        t.setEnabled(False)

        def setv(**kw):
            cur = line_favs.load(self.settings, i)
            cur.update(kw)
            line_favs.save(self.settings, i, cur)
            self._update_line_fav_buttons()
        for title, key, opts in (("선 종류", "linestyle", line_favs.LINESTYLES), ("선 굵기", "linewidth", line_favs.WIDTHS),
                                 ("점 모양", "marker", line_favs.MARKERS)):
            sub = menu.addMenu(title)
            for v, text in opts:
                a = sub.addAction(text)
                a.setCheckable(True)
                a.setChecked(st.get(key) == v)
                a.triggered.connect(lambda _=False, key=key, v=v: setv(**{key: v}))
                acts[f"{key}:{v}"] = a
        acts["reset"] = menu.addAction("처음 모양으로")
        acts["reset"].triggered.connect(lambda _=False: (line_favs.reset(self.settings, i), self._update_line_fav_buttons()))
        return menu, acts

    @_guard
    def on_line_fav_menu(self, i):
        menu, _ = self.build_line_fav_menu(i)
        menu.exec(QCursor.pos())

    @_guard
    def on_colors_by_freq(self, *_):
        names = [r.sheet_name for r in self.c.visible_sheets()] + ["sim:" + d.name for d in self.c.visible_sims()]
        self.c.reset_colors(names)
        self.refresh(rerender=True)
        self.statusBar().showMessage("주파수별 기본색으로 되돌렸습니다", 3000)

    # ========================================================== Excel export
    def excel_plots(self):
        """What is on screen, per plot: curves (normal look, no emphasis) + current axis ranges."""
        from plotting.excel_export import CurveOut, PlotOut
        from plotting.renderer import MEAS_STYLE, SIM_STYLE
        from core.models import Source
        plots = []
        keep = self.c.highlight_selected
        self.c.highlight_selected = False             # the report gets every curve at full strength
        try:
            for v in self.visible_views:
                with self.c.using_pane(v.idx):
                    req = self.c.build_plot_request()
                    if req is None or not req.series:
                        continue
                    freqs = self.c.series_freqs()
                    ax = v.renderer.ax
                    xlim = tuple(ax.get_xlim()) if ax is not None else req.xlim
                    ylim = tuple(ax.get_ylim()) if ax is not None else req.ylim
                    curves = []
                    for s in req.series:
                        sim = s.role == Source.SIMULATION
                        st = dict(SIM_STYLE if sim else MEAS_STYLE)
                        st.update(s.style or {})
                        mk = st.get("marker") or ""
                        hz = freqs.get(st.get("gid"))
                        curves.append(CurveOut(
                            label=s.label, x=list(s.x), y=list(s.y), kind="Simulation" if sim else "Measurement",
                            color=st.get("color") or "#1f77b4", linestyle=st.get("linestyle") or "-",
                            linewidth=float(st.get("linewidth") or 1.2), marker="" if mk in ("None", "none") else mk,
                            markersize=float(st.get("markersize") or 4.5), alpha=float(st.get("alpha") or 1.0),
                            freq=format_freq(hz) if hz else ""))
                    conds = list(req.condition_lines or [])
                    def step(ticks):
                        t = [float(v) for v in ticks]
                        return (t[1] - t[0]) if len(t) > 1 else None
                    plots.append(PlotOut(req.title, req.x_label, req.y_label, curves, xlim, ylim, conds,
                                         x_step=step(ax.get_xticks()) if ax is not None else None,
                                         y_step=step(ax.get_yticks()) if ax is not None else None))
        finally:
            self.c.highlight_selected = keep
        return plots

    def export_excel(self, path: str) -> str:
        from plotting.excel_export import export_xlsx
        plots = self.excel_plots()
        if not plots:
            raise ValueError("내보낼 Plot 데이터가 없습니다.")
        return export_xlsx(path, plots)

    @_guard
    def on_export_excel(self, *_):
        start_dir = self.settings.value("save_dir", "")
        name = os.path.splitext(self.default_export_name())[0] + ".xlsx"
        default = os.path.join(start_dir, name) if start_dir else name
        path, _f = QFileDialog.getSaveFileName(self, "Excel로 내보내기 (데이터 + 차트)", default, "Excel (*.xlsx)")
        if not path:
            return
        try:
            saved = self.export_excel(path)
        except PermissionError:
            QMessageBox.warning(self, APP_TITLE, "파일이 Excel에서 열려 있어 저장할 수 없습니다.\nExcel에서 닫은 뒤 다시 시도해주세요.")
            return
        except ValueError as exc:
            QMessageBox.information(self, APP_TITLE, str(exc))
            return
        self.settings.setValue("save_dir", os.path.dirname(saved))
        self.statusBar().showMessage(f"✔ Excel 저장 완료: {saved}", 6000)
        try:
            os.startfile(saved)                       # Windows: open it right away
        except (AttributeError, OSError):
            pass

    def save_all_png(self, path: str) -> str:
        if not path.lower().endswith(".png"):
            path += ".png"
        data = self.compose_all_png() if self.n_panes > 1 else self._one_png()
        with open(path, "wb") as f:
            f.write(data)
        return path

    # ========================================================== drag & drop
    def dragEnterEvent(self, event):
        urls = event.mimeData().urls() if event.mimeData().hasUrls() else []
        if urls and urls[0].toLocalFile().lower().endswith((".xlsx", ".xlsm", ".xls", ".csv", ".txt", ".pam")):
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls:
            self.open_file(urls[0].toLocalFile())
