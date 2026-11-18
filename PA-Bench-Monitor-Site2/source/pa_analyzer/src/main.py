"""PA Measurement Analyzer — entry point.

    python src/main.py                     # start the GUI
    python src/main.py file.xlsx           # start and open a measurement file
    python src/main.py --sim <folder>      # use another simulation folder
    python src/main.py --smoke-test        # run the GUI self-test and exit
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback

os.environ.setdefault("QT_API", "pyside6")
# PySide6 wheels ship no fonts; Qt's "offscreen" platform (used by the
# self-test) then warns "QFontDatabase: Cannot find font directory" and lays
# out widgets without fonts. Point it at the Windows font folder.
if os.name == "nt" and not os.environ.get("QT_QPA_FONTDIR"):
    os.environ["QT_QPA_FONTDIR"] = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
SRC = os.path.dirname(os.path.abspath(__file__))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from utils.paths import bundle_root, example_simulation_root, is_frozen, user_root  # noqa: E402

PROJECT_ROOT = bundle_root()

# A windowed .exe has no console: sys.stdout / sys.stderr are None and any
# print() would crash. Send output to a log file next to the program instead.
if sys.stdout is None or sys.stderr is None:
    try:
        _log = open(os.path.join(user_root(), "PA-Measurement-Analyzer.log"), "a", encoding="utf-8", buffering=1)
    except OSError:
        _log = open(os.devnull, "w")
    sys.stdout = sys.stdout or _log
    sys.stderr = sys.stderr or _log

# Korean Windows consoles/pipes use cp949, which cannot encode some characters
# (e.g. '—', '✔'). Never let a print() crash the program.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _parse_args(argv):
    p = argparse.ArgumentParser(description="RF PA Measurement vs Simulation analyzer")
    p.add_argument("file", nargs="?", help="Measurement Excel file to open")
    p.add_argument("--sim", help="Simulation data folder (default: local_data/simulation or bundled example)")
    p.add_argument("--smoke-test", action="store_true", help="run the GUI self-test and exit")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    try:
        from PySide6.QtCore import QSettings
        from PySide6.QtWidgets import QApplication, QMessageBox
    except ImportError:
        print("PySide6가 설치되어 있지 않습니다.  pip install -r requirements.txt  를 먼저 실행해주세요.")
        return 2

    import matplotlib
    matplotlib.use("QtAgg")

    from app.controller import AppController, default_simulation_root
    from ui.main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("PA Measurement Analyzer")
    app.setOrganizationName("PA-Measurement-Analyzer")
    app.setStyle("Fusion")
    icon_path = os.path.join(PROJECT_ROOT, "assets", "app.ico")
    if os.path.isfile(icon_path):
        from PySide6.QtGui import QIcon
        app.setWindowIcon(QIcon(icon_path))

    def excepthook(exc_type, exc, tb):  # never die silently on an unexpected error
        traceback.print_exception(exc_type, exc, tb)
        if not args.smoke_test:
            QMessageBox.critical(None, "PA Measurement Analyzer", f"예기치 않은 오류:\n{exc}")
    sys.excepthook = excepthook

    if args.smoke_test:   # never touch the user's real settings during the self-test
        import tempfile
        settings = QSettings(os.path.join(tempfile.mkdtemp(), "smoke_settings.ini"), QSettings.IniFormat)
    else:
        settings = QSettings("PA-Measurement-Analyzer", "PA-Measurement-Analyzer")
    sim_root = args.sim or (None if args.smoke_test else settings.value("sim_root", "")) or ""
    if not sim_root or not os.path.isdir(sim_root):
        sim_root = default_simulation_root()
    if args.smoke_test:
        sim_root = example_simulation_root()

    controller = AppController(sim_root=sim_root)
    saved_files = [] if (args.smoke_test or args.sim) else \
        [f for f in str(settings.value("sim_files", "") or "").split("|") if f]
    if saved_files and all(os.path.isfile(f) for f in saved_files):
        controller.load_simulation_files(saved_files)       # user's own sim files (e.g. 3B / 3X / 3P / 3K)
    win = MainWindow(controller, settings)
    from sparam.app.controller import Controller as SParamController
    from sparam.ui.main_window import MainWindow as SParamWindow
    from ui.suite_window import SuiteWindow
    sp_settings = (QSettings(os.path.join(os.path.dirname(settings.fileName()), "sparam.ini"), QSettings.IniFormat)
                   if args.smoke_test else QSettings("SParam-Viewer", "SParam-Viewer"))
    sp_win = SParamWindow(SParamController(), sp_settings)
    suite = SuiteWindow(win, sp_win, settings)
    suite.show()

    if args.smoke_test:
        from ui.smoke_test import run_smoke_test
        from sparam.ui.smoke_test import run_smoke_test as run_sparam_smoke
        suite.show_tool("pa")
        app.processEvents()
        rc = run_smoke_test(app, win)
        from ui.smoke_test import LAST_REPORT
        suite.show_tool("sparam")
        app.processEvents()
        rc2, report2 = run_sparam_smoke(app, sp_win)
        suite.show_tool("pa")
        report = LAST_REPORT + "\n" + report2
        if is_frozen():  # no console in the .exe -> also leave a result file
            with open(os.path.join(user_root(), "smoke_test_result.txt"), "w", encoding="utf-8") as f:
                f.write(report)
        return 1 if (rc or rc2) else 0

    if args.file:
        f = os.path.abspath(args.file)
        if f.lower().endswith((".s1p", ".s2p", ".s3p", ".s4p", ".snp")):
            suite.show_tool("sparam")
            sp_win.add_paths([f])
        else:
            suite.show_tool("pa")
            win.open_file(f)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
