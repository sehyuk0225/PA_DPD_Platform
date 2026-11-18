"""One program, two tools: PA Analyzer (PAE / Gain vs Pout) | S-Parameter Viewer.

Each tool keeps its own window class, settings and shortcuts; they are embedded
as tab pages. Shortcuts (F / V / H / Del / Ctrl+V ...) only act on the visible tab.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMainWindow, QTabWidget

from version import VERSION_LABEL

SUITE_TITLE = f"RF PA Analyzer {VERSION_LABEL}"
TAB_NAMES = ("PA  (PAE / Gain vs Pout)", "S-Parameter  (S21 / S11 / Smith)")


class SuiteWindow(QMainWindow):
    def __init__(self, pa_window, sparam_window, settings=None):
        super().__init__()
        self.pa, self.sp = pa_window, sparam_window
        self.settings = settings
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        for w, name in zip((pa_window, sparam_window), TAB_NAMES):
            w.setWindowFlags(Qt.Widget)          # a QMainWindow can live inside a tab
            self.tabs.addTab(w, name)
        self.setCentralWidget(self.tabs)
        # ONE project file (.pam) holds both tabs: PA saves / opens, the S-Parameter state rides along
        pa_window.project_extras["sparam"] = (sparam_window.has_data, sparam_window.export_state,
                                              sparam_window.import_state)
        sparam_window.save_project_hook = pa_window.on_save_project
        sparam_window.save_project_quick_hook = pa_window.on_save_project_quick
        sparam_window.open_project_hook = self._open_project
        self.setMinimumSize(720, 480)
        self.resize(1500, 940)
        self.setAcceptDrops(False)               # each tab handles its own drag & drop
        last = int(settings.value("suite/tab", 0) or 0) if settings is not None else 0
        self.tabs.setCurrentIndex(last if last in (0, 1) else 0)
        self.tabs.currentChanged.connect(self._tab_changed)
        for w in (pa_window, sparam_window):
            w.windowTitleChanged.connect(lambda *_: self._update_title())
        self._update_title()

    def _open_project(self):
        from PySide6.QtWidgets import QFileDialog
        path, _f = QFileDialog.getOpenFileName(self, "프로젝트 열기", self.pa.settings.value("last_dir", ""),
                                               "RF PA Analyzer 프로젝트 (*.pam)")
        if path:
            self.pa.open_file(path)

    @property
    def current_tool(self):
        return self.pa if self.tabs.currentIndex() == 0 else self.sp

    def show_tool(self, which: str):
        self.tabs.setCurrentIndex(0 if which == "pa" else 1)

    def _tab_changed(self, i):
        if self.settings is not None:
            self.settings.setValue("suite/tab", i)
        self._update_title()

    def _update_title(self):
        sub = self.current_tool.windowTitle() or ""
        # tool titles start with their own app name; keep the file part ("— file.pam *")
        detail = sub.split("—", 1)[1].strip() if "—" in sub else ""
        self.setWindowTitle(f"{SUITE_TITLE} — {TAB_NAMES[self.tabs.currentIndex()].split('  ')[0]}"
                            + (f" — {detail}" if detail else ""))

    def closeEvent(self, event):
        if self.pa._confirm_discard():           # unsaved pasted data in the PA tab?
            event.accept()
        else:
            event.ignore()
