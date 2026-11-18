# -*- coding: utf-8 -*-
"""
PA Bench Monitor 쪽: RF PA Analyzer 그래프 창(별도 프로세스) 실행 · 데이터 전달.

Tk 화면과 Qt 화면이 서로 멈추게 하지 않도록 Analyzer 는 별도 프로세스로 띄우고
127.0.0.1 소켓으로 JSON 한 줄씩 보낸다. 바뀐 Dataset 만 보냄.
"""
import importlib.util
import json
import os
import itertools
import queue
import select
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def child_command(port):
    if getattr(sys, "frozen", False):
        return [sys.executable, "--analyzer-child", str(port)]
    exe = sys.executable
    if os.name == "nt" and exe.lower().endswith("python.exe"):
        w = exe[:-10] + "pythonw.exe"           # 콘솔 창 없이
        if os.path.isfile(w):
            exe = w
    return [exe, os.path.join(HERE, "pa_bench_monitor.py"), "--analyzer-child", str(port)]


def missing_packages():
    """개발 실행(python) 일 때 필요한 패키지 확인. exe 는 모두 포함."""
    if getattr(sys, "frozen", False):
        return []
    return [p for p in ("PySide6", "matplotlib", "numpy", "openpyxl") if importlib.util.find_spec(p) is None]


class AnalyzerLink:
    def __init__(self):
        self.proc = None
        self.srv = None
        self.conn = None
        self.sent = {}            # key -> 보낸 내용 (같으면 다시 안 보냄)
        self.sent_ref = None      # 보낸 색 기준 주파수 목록
        self.q = queue.Queue()
        self.started = 0.0
        self.error = ""
        self.pam = None           # 샘플 종류에 연결된 PA Analyzer 프로젝트 (.pam), None = 빈 화면
        self.stype = ""
        self.replies = {}         # 그래프 창이 보낸 답 (id → dict)
        self._ids = itertools.count(1)

    # ---------------------------------------------------- 상태
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def connected(self):
        return self.running() and self.conn is not None

    # ---------------------------------------------------- 실행
    def open(self):
        """창 열기 (이미 떠 있으면 앞으로). 반환: (ok, 메시지)"""
        if self.running():
            self.send({"op": "show"})
            return True, "PA Analyzer 그래프 창을 앞으로 가져왔습니다."
        miss = missing_packages()
        if miss:
            return False, ("PA Analyzer 그래프 창에 필요한 패키지가 없습니다: %s\n\n"
                           "명령 프롬프트에서:\n  python -m pip install PySide6 matplotlib numpy openpyxl\n\n"
                           "(exe 로 빌드한 프로그램은 설치 없이 동작)" % ", ".join(miss))
        self.close()
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        port = self.srv.getsockname()[1]
        flags = 0x08000000 if os.name == "nt" else 0      # CREATE_NO_WINDOW
        try:
            self.proc = subprocess.Popen(child_command(port), creationflags=flags, close_fds=True)
        except Exception as e:
            self.srv.close()
            self.srv = None
            return False, "PA Analyzer 그래프 창을 실행하지 못했습니다.\n%s" % e
        self.sent = {}
        self.sent_ref = None
        self.q = queue.Queue()
        self.q.put({"op": "load", "path": self.pam, "stype": self.stype})   # 제일 먼저: 연결된 .pam 열기
        self.started = time.time()
        threading.Thread(target=self._serve, args=(self.srv, self.q, self.proc), daemon=True).start()
        return True, "PA Analyzer 그래프 창 여는 중… (처음엔 몇 초 걸림)"

    def _serve(self, srv, q, proc):
        srv.settimeout(1.0)
        conn = None
        t0 = time.time()
        while conn is None and proc.poll() is None and time.time() - t0 < 90:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
        try:
            srv.close()
        except OSError:
            pass
        if conn is None:
            self.error = "PA Analyzer 그래프 창이 시작되지 않았습니다 (종료 코드 %s)" % proc.poll()
            return
        conn.settimeout(None)
        self.conn = conn
        buf = b""
        stop = False
        while proc.poll() is None and not stop:
            # 그래프 창 → Bench (저장 결과 등)
            try:
                rd, _, _ = select.select([conn], [], [], 0.05)
            except (OSError, ValueError):
                break
            if rd:
                try:
                    chunk = conn.recv(65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        m = json.loads(line.decode("utf-8"))
                        if m.get("id") is not None:
                            self.replies[m["id"]] = m
                    except ValueError:
                        pass
            # Bench → 그래프 창
            while True:
                try:
                    msg = q.get_nowait()
                except queue.Empty:
                    break
                if msg is None:
                    stop = True
                    break
                try:
                    conn.sendall((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
                except OSError:
                    stop = True
                    break
        try:
            conn.close()
        except OSError:
            pass
        if self.conn is conn:
            self.conn = None

    def send(self, msg):
        if self.running():
            self.q.put(msg)

    def close(self):
        if self.running():
            self.q.put({"op": "bye"})
            self.q.put(None)
            try:
                self.proc.wait(timeout=1.5)
            except Exception:
                try:
                    self.proc.terminate()
                except Exception:
                    pass
        self.proc = None
        self.conn = None

    # ---------------------------------------------------- 데이터
    def sync(self, datasets, freq_ref=None):
        """datasets: [(key, name, display, rows)] — 지금 Bench 에 있는 전체. 바뀐 것만 보냄.
        freq_ref: 색 기준 주파수 목록 (GHz) - 바뀌었을 때만 보냄"""
        if not self.running():
            return
        if freq_ref is not None and list(freq_ref) != self.sent_ref:
            self.sent_ref = list(freq_ref)
            self.send({"op": "freq_ref", "ghz": list(freq_ref)})
        keys = set()
        for key, name, display, rows in datasets:
            k = json.dumps(key, ensure_ascii=False)
            keys.add(k)
            body = (name, display, rows)
            if self.sent.get(k) != body:
                self.sent[k] = body
                self.send({"op": "set", "key": k, "name": name, "display": display, "rows": rows})
        for k in [k for k in self.sent if k not in keys]:
            # Bench 에서 지워진 데이터 → 그래프 창에서는 지우지 않고 연결만 끊음 (그 창에서 직접 지우기)
            del self.sent[k]
            self.send({"op": "detach", "key": k})

    def set_pam(self, path, stype="", force=False):
        """샘플 종류 변경 → 연결된 .pam 으로 바꿔 열고, 그 종류 기록을 다시 보냄 (그래프 창이 열려 있으면 바로)."""
        if path == self.pam and stype == self.stype:
            return
        self.pam, self.stype = path, stype
        if self.running():
            self.sent = {}
            self.sent_ref = None
            self.send({"op": "load", "path": path, "stype": stype, "force": bool(force)})
            self.send({"op": "show"})

    def rekey(self, old_key, new_key, display):
        """샘플 번호 반영: 그래프 창의 Dataset 을 새 이름으로 (새로 만들지 않고 이름만 바꿈)."""
        ko = json.dumps(old_key, ensure_ascii=False)
        kn = json.dumps(new_key, ensure_ascii=False)
        self.sent.pop(ko, None)
        self.sent.pop(kn, None)
        self.send({"op": "rekey", "old": ko, "new": kn, "display": display})

    def detach_all(self):
        """Bench 기록 전체 지움 → 그래프 창의 Dataset 은 그대로 두고 연결만 끊음."""
        self.sent = {}
        self.sent_ref = None
        self.send({"op": "detach"})

    def request_save(self, path, stype):
        """그래프 창에 '지금 열려 있는 .pam 이 path 이면 그 파일에 저장' 요청 → id"""
        rid = next(self._ids)
        self.replies.pop(rid, None)
        self.send({"op": "save", "path": path, "stype": stype, "id": rid})
        return rid

    def take_reply(self, rid):
        return self.replies.pop(rid, None)

    def wait_reply(self, rid, timeout):
        t0 = time.time()
        while time.time() - t0 < timeout and self.running():
            r = self.take_reply(rid)
            if r is not None:
                return r
            time.sleep(0.05)
        return None

    def reset(self):
        """기록 모두 지움 → Analyzer 쪽 Bench 데이터도 지우고 주파수 색 처음부터."""
        self.sent = {}
        self.sent_ref = None
        self.send({"op": "reset"})
