# -*- coding: utf-8 -*-
"""
PA Bench Monitor → RF PA Analyzer 그래프 창 (별도 프로세스)

PA Bench Monitor 가  "<exe> --analyzer-child <port>"  (또는 python pa_bench_monitor.py --analyzer-child <port>)
로 실행. RF PA Analyzer 의 실제 PA 화면(MainWindow: 메뉴 · Datasets · Marker · Back-off · Copy Plot …)을
그대로 띄우고, Bench 가 127.0.0.1:<port> 로 보내는 측정 데이터를 Dataset 으로 넣고 갱신한다.

메시지 (JSON 한 줄씩)
  {"op":"set", "key":k, "name":"S1 3.5GHz", "display":"S1 · 3.5 GHz", "rows":[[...], ...]}
  {"op":"remove", "key":k}      {"op":"reset"}      {"op":"show"}      {"op":"bye"}
"""
import json
import os
import socket
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))


def analyzer_src():
    """RF PA Analyzer 소스 폴더 (개발: pa_analyzer/src, exe: 번들 안 — 모듈은 이미 포함됨)."""
    for cand in (os.path.join(HERE, "pa_analyzer", "src"),
                 os.path.join(getattr(sys, "_MEIPASS", HERE), "pa_analyzer", "src")):
        if os.path.isdir(cand):
            return cand
    return None


def main(port):
    app, win, bridge = build(port)
    return app.exec()


def build(port):
    if sys.stdout is None or sys.stderr is None:   # 창 모드 exe: 콘솔 없음 → 로그 파일
        try:
            d = os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) else HERE
            log = open(os.path.join(d, "PA-Analyzer-graph.log"), "a", encoding="utf-8", buffering=1)
        except OSError:
            log = open(os.devnull, "w")
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log
    os.environ.setdefault("QT_API", "pyside6")
    if os.name == "nt" and not os.environ.get("QT_QPA_FONTDIR"):
        os.environ["QT_QPA_FONTDIR"] = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    src = analyzer_src()
    if src and src not in sys.path:
        sys.path.insert(0, src)

    from PySide6.QtCore import QSettings, QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox
    import matplotlib
    matplotlib.use("QtAgg")
    from app.controller import AppController
    from excel.reader import RawSheet
    from measurement.loader import MeasurementFile
    from ui.main_window import MainWindow
    from utils.paths import bundle_root, default_simulation_root, example_simulation_root

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("PA Measurement Analyzer")
    app.setOrganizationName("PA-Measurement-Analyzer")
    app.setStyle("Fusion")
    for ico in (os.path.join(bundle_root(), "assets", "app.ico"),
                os.path.join(src or HERE, "..", "assets", "app.ico")):
        if os.path.isfile(ico):
            from PySide6.QtGui import QIcon
            app.setWindowIcon(QIcon(ico))
            break

    # 설정은 RF PA Analyzer 와 같은 곳 (조건 표시 · 시뮬레이션 파일 등 그대로)
    settings = QSettings("PA-Measurement-Analyzer", "PA-Measurement-Analyzer")
    sim_root = str(settings.value("sim_root", "") or "")
    if not sim_root or not os.path.isdir(sim_root):
        sim_root = default_simulation_root()
    example = os.path.normcase(os.path.abspath(example_simulation_root()))
    if os.path.normcase(os.path.abspath(sim_root)) == example:
        # 예제 시뮬레이션은 실측과 겹쳐 보이면 헷갈리므로 빈 폴더 사용 (메뉴에서 직접 불러올 수 있음)
        import tempfile
        sim_root = tempfile.mkdtemp(prefix="pa_bench_nosim_")
    controller = AppController(sim_root=sim_root)
    saved = [f for f in str(settings.value("sim_files", "") or "").split("|") if f]
    if saved and all(os.path.isfile(f) for f in saved):
        try:
            controller.load_simulation_files(saved)
        except Exception:
            traceback.print_exc()

    class BenchWindow(MainWindow):
        """Analyzer PA 화면 그대로. 닫을 때: Bench 데이터만 있으면 묻지 않고 닫음."""

        def closeEvent(self, event):
            mf = self.c.measurement
            own = [s for s in (mf.sheets if mf else []) if s.sheet_name not in bridge.names.values()]
            if own and not self._confirm_discard():
                event.ignore()
                return
            event.accept()
            app.quit()

    win = BenchWindow(controller, settings)
    base_title = win.windowTitle()

    class Bridge:
        def __init__(self):
            self.names = {}        # Bench key -> 내부 dataset 이름
            self.detached = set()  # Bench 에서 지워져 연결만 끊긴 Dataset (그래프에는 남음)
            self.pam, self.stype = None, ""
            self.buf = b""
            self.scroll_pending = True   # 창이 뜨면 데이터셋 목록을 최근 것(맨 아래)으로
            self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
            self.sock.setblocking(False)
            self.timer = QTimer()
            self.timer.timeout.connect(self.poll)
            self.timer.start(120)

        # ---- Bench → 여기
        def poll(self):
            if app.activeModalWidget() is not None or QApplication.activePopupWidget() is not None:
                return             # 대화상자 / 메뉴가 열려 있는 동안은 나중에 반영
            msgs = []
            while True:
                try:
                    chunk = self.sock.recv(1 << 16)
                except (BlockingIOError, InterruptedError):
                    break
                except OSError:
                    chunk = b""
                if not chunk:      # Bench 종료 → 같이 닫음
                    self.timer.stop()
                    app.quit()
                    return
                self.buf += chunk
            while b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                if line.strip():
                    try:
                        msgs.append(json.loads(line.decode("utf-8")))
                    except ValueError:
                        pass
            if not msgs:
                return
            changed = False
            for m in msgs:
                try:
                    changed |= self.apply(m)
                except Exception:
                    traceback.print_exc()
            if changed:
                try:
                    # Analyzer 를 파일로 열 때와 같게: 낮은 주파수부터 파랑 → 빨강 → 초록 … (Bench 화면과 동일)
                    controller.__dict__.pop("_freq_colors", None)
                    win.refresh(rerender=True)
                    mf = controller.measurement
                    head = ("%s · %s + " % (self.stype, os.path.basename(self.pam)) if self.pam else
                            ("%s · " % self.stype if self.stype else ""))
                    win.setWindowTitle("%s — %sPA Bench 실시간 (%d개)" % (base_title, head, len(self.names)))
                    if mf is not None and not [s for s in mf.sheets if s.sheet_name not in self.names.values()]:
                        mf.dirty = False     # Bench 데이터만 있으면 저장 안 물어봄 (.pam 이 열려 있으면 Analyzer 처럼 물어봄)
                except Exception:
                    traceback.print_exc()
            if self.scroll_pending:
                self.scroll_pending = False
                QTimer.singleShot(60, self.scroll_recent)

        def scroll_recent(self):
            """데이터셋 목록: Measurement 의 마지막(가장 최근) 항목이 보이게 아래로"""
            try:
                from PySide6.QtWidgets import QAbstractItemView
                lst = win.data_list
                last = None
                for i in range(lst.count()):
                    it = lst.item(i)
                    if it.data(win.KIND_ROLE) == "meas" and not it.isHidden():
                        last = it
                if last is not None:
                    lst.scrollToItem(last, QAbstractItemView.PositionAtBottom)
                else:
                    lst.scrollToBottom()
            except Exception:
                traceback.print_exc()

        def load(self, path, stype, force=False):
            """샘플 종류에 연결된 .pam 열기 (없으면 빈 화면). Bench 데이터는 뒤이어 다시 들어옴."""
            mf = controller.measurement
            own = [s for s in (mf.sheets if mf else []) if s.sheet_name not in self.names.values()]
            if own and getattr(controller, "unsaved", False) and not force:
                # 열려 있던 .pam 에 Bench 데이터가 추가됐거나 수정됨 → 저장할지 물어봄 (Analyzer 와 같은 창)
                if not win._confirm_discard():
                    return False
            self.names.clear()
            self.detached.clear()
            self.pam, self.stype = path, stype
            if path:
                msgs = controller.load_measurement(path)
                errs = [x for x in msgs if getattr(x.level, "name", "") == "ERROR"]
                if errs or controller.measurement is None:
                    controller.measurement = None
                    controller.shared_checked = []
                    QMessageBox.warning(win, "PA Analyzer 그래프", "연결된 .pam 을 열 수 없습니다:\n%s\n\n%s"
                                        % (path, "\n".join(x.text for x in errs)))
                    self.pam = None
            else:
                controller.measurement = None
                controller.shared_checked = []
                for p in controller.panes:
                    if p.own_meas is not None:
                        p.own_meas = []
                controller.sheet_name = None
            controller.__dict__.pop("_freq_colors", None)
            self.scroll_pending = True
            return True

        def reply(self, msg):
            try:
                self.sock.sendall((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
            except OSError:
                pass

        def save(self, m):
            """Bench 요청: 지금 열린 .pam 이 요청한 파일(같은 샘플 종류)일 때만 그 파일에 저장. 다르면 거절."""
            path, rid = m.get("path"), m.get("id")
            same = False
            try:
                same = bool(path and self.pam) and os.path.normcase(os.path.abspath(path)) == \
                    os.path.normcase(os.path.abspath(self.pam)) and (m.get("stype") or "") == self.stype
            except (TypeError, ValueError):
                pass
            mf = controller.measurement
            if not same or mf is None or not os.path.normcase(os.path.abspath(mf.path or "")) == \
                    os.path.normcase(os.path.abspath(self.pam)):
                self.reply({"id": rid, "ok": False, "msg": "그래프 창에 다른 .pam 이 열려 있음"})
                return
            try:
                saved = win.on_save_project(path=self.pam)
            except Exception as exc:
                traceback.print_exc()
                saved = None
            self.reply({"id": rid, "ok": bool(saved), "path": saved})

        def measurement(self):
            if controller.measurement is None:
                controller.measurement = MeasurementFile("PA Bench Monitor (실시간)")
            return controller.measurement

        def apply(self, m):
            op = m.get("op")
            if op == "show":
                win.showNormal()
                win.raise_()
                win.activateWindow()
                QTimer.singleShot(60, self.scroll_recent)
                return False
            if op == "bye":
                app.quit()
                return False
            if op == "save":
                self.save(m)
                return True
            if op == "reattach":                 # Bench 에서 Ctrl+Z 로 기록을 되살림 → 같은 Dataset 에 다시 연결
                self.detached.clear()
                return False
            if op == "detach":
                k = m.get("key")
                for key in ([k] if k else list(self.names)):
                    name = self.names.pop(key, None)
                    if name:
                        self.detached.add(name)
                return False
            if op == "load":
                return self.load(m.get("path"), m.get("stype") or "", bool(m.get("force")))
            if op == "rekey":
                name = self.names.pop(m.get("old"), None)
                if not name:
                    return False
                if m.get("new") in self.names:          # 새 이름이 이미 있으면 예전 것은 연결만 끊음
                    self.detached.add(name)
                    return False
                self.names[m.get("new")] = name
                mf = controller.measurement
                sh = mf.sheet(name) if mf else None
                if sh is not None and m.get("display"):
                    sh.display_name = m["display"] if m["display"] != name else None
                    mf.dirty = True
                return True
            if op == "freq_ref":                 # 샘플 종류의 주파수 목록 → 색 고정 (낮은 순서로 파랑 · 초록 · 빨강 …)
                controller.set_freq_ref(m.get("ghz") or [])
                return True
            if op == "reset":
                for name in list(self.names.values()):
                    controller.remove_sheet(name)
                self.names.clear()
                controller.__dict__["_freq_colors"] = {}         # 주파수 색 다시 계산 (기준 목록은 그대로)
                return True
            key = m.get("key")
            if op == "remove":
                name = self.names.pop(key, None)
                if name and controller.measurement and controller.measurement.sheet(name):
                    controller.remove_sheet(name)
                    return True
                return False
            if op != "set":
                return False
            mf = self.measurement()
            rows = [list(r) for r in m.get("rows", [])]
            name = self.names.get(key)
            if name is None and m.get("name"):
                # .pam 에 이미 저장된 오늘 같은 샘플·주파수 (Bench 가 저장한 것) → 그걸 이어서 갱신 (중복 안 생기게)
                ex = mf.sheet(m["name"])
                if ex is not None and m["name"] not in self.detached \
                        and str(getattr(ex, "added_how", "") or "").startswith("PA Bench") \
                        and m["name"] not in self.names.values():
                    self.names[key] = name = m["name"]
            old = mf.sheet(name) if name else None
            if old is not None:
                # 같은 Dataset 을 그 자리에서 새 데이터로 교체 (색 · 스타일 · 체크 · 순서 유지)
                from core.models import Source
                from excel.sheet_parser import parse_sheet
                res = parse_sheet(RawSheet(name, rows, "PA Bench"), Source.MEASUREMENT)
                res.display_name = m.get("display") if m.get("display") != name else None
                res.added_at, res.added_how = getattr(old, "added_at", ""), "PA Bench Monitor 실시간"
                mf.sheets = [res if s is old else s for s in mf.sheets]
                mf.__dict__.pop("_idx", None)
                return True
            raw = RawSheet(m.get("name") or "Bench", rows, "PA Bench")
            res = mf.add_raw_sheet(raw, None, display_name=m.get("display"), how="PA Bench Monitor 실시간")
            self.scroll_pending = True           # 새 Dataset (새 주파수 · 샘플) → 목록을 그쪽으로
            if res.sheet_name not in controller.checked:
                controller.checked.append(res.sheet_name)
            controller.sheet_name = res.sheet_name
            self.names[key] = res.sheet_name
            return True

    bridge = Bridge()
    win.show()
    win.raise_()

    def excepthook(exc_type, exc, tb):
        traceback.print_exception(exc_type, exc, tb)
        try:
            QMessageBox.critical(None, "PA Analyzer 그래프", "예기치 않은 오류:\n%s" % exc)
        except Exception:
            pass
    sys.excepthook = excepthook
    return app, win, bridge


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1])))
