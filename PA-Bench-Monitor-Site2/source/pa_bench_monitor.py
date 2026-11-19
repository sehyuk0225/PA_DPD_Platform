# -*- coding: utf-8 -*-
"""
PA Bench Monitor v2.0
R&S SMW200A (신호발생기) + FSW (스펙트럼 분석기) + RTO2044 (오실로스코프 전류)
세 장비의 값을 한 화면에 실시간 표시하고, SMW 파워가 바뀔 때마다 자동으로 표에 기록.

- 장비에는 아무것도 설치하지 않음 (LAN, SCPI raw socket 5025)
- IP 칸에 demo 입력 시 장비 없이 동작 확인 가능 (세 장비 모두 demo 로 두면 서로 연동된 가상 PA)
"""
import collections
import copy
import csv
import datetime
import json
import math
import os
import queue
import random
import socket
import re
import sys
import types
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, messagebox, filedialog

import rto_current_meter as rto
import psu_panel
try:  # RF PA Analyzer 그래프 창 연동 (연동판에만 포함 · 단독판은 이 파일이 없음)
    from analyzer_link import AnalyzerLink
    HAS_ANALYZER = True
except ImportError:
    HAS_ANALYZER = False

    class AnalyzerLink:  # 단독판: 아무것도 안 함
        def running(self):
            return False

        def sync(self, datasets):
            pass

        def reset(self):
            pass

        def detach_all(self):
            pass

        def close(self):
            pass

APP_NAME = "PA Bench Monitor"
VERSION = "v2.5.121"

BG = "#0b0f14"
FG_DIM = "#7d8a99"
C_SMW = "#ffb74d"
C_FSW = "#4fc3f7"
C_CUR = "#39ff88"
C_GAIN = "#e0e0e0"


def settings_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "PABenchMonitor")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "settings.json")


DEFAULTS = {
    "smw_host": "192.168.0.11", "smw_port": 5025, "smw_gtl": True, "smw_poll": 300,
    "smw_step": "1", "smw_max": "10",
    "fsw_host": "192.168.0.12", "fsw_port": 5025, "fsw_gtl": True, "fsw_poll": 300,
    "psu_host": "192.168.0.13", "psu_port": 5025, "psu_poll": 200,
    "fsw_marker": 1,
    "settle": "1.0", "avg": "1.0", "vdd": "",
    "loss_table": [],  # [[GHz, input loss dB, output loss dB], ...]
    "in_loss": "0", "out_loss": "0", "freq_ghz": "",
    "auto_rec": True, "geometry": "1560x900", "tab": 0, "min_id": "250", "sa_q": True,
    "pae_ax": ["20", "80", "10"], "gain_ax": ["20", "40", "2.5"], "loss_dir": "",
    "x_ax": ["26", "54", "5", "1"], "sw_return": True, "sw_idmax": "",
    "ramp_dt": "1.0", "loss_open": True,
    "sw_retry": True, "tol_id": "15", "tol_pae": "5", "sample": "",
}


def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(settings_path(), "r", encoding="utf-8") as f:
            s.update(json.load(f))
    except Exception:
        pass
    return s


def save_settings(s):
    try:
        with open(settings_path(), "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def fnum(text, default=None):
    try:
        return float(str(text).strip())
    except (ValueError, TypeError):
        return default


def fmt_freq(hz):
    if hz is None or not math.isfinite(hz):
        return "---"
    if hz >= 1e9:
        return "%.6g GHz" % (hz / 1e9)
    if hz >= 1e6:
        return "%.6g MHz" % (hz / 1e6)
    return "%.6g kHz" % (hz / 1e3)


def round_sa(v):
    """SA 기록값: 소수 둘째 자리에서 반올림 → 소수 첫째 자리.  예) 12.24 → 12.2, 12.25 → 12.3, -10.75 → -10.8"""
    from decimal import Decimal, ROUND_HALF_UP
    return float(Decimal(repr(v)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def dbm_to_w(dbm):
    return 10 ** ((dbm - 30) / 10.0)


# ============================================================ 가상 장비 (demo)
DEMO = {"pin": -40.0, "freq": 3.5e9, "rf": True,
        "wv": "/var/user/PAM_sig/000_PulsedCW_5ms_100us.wv", "arb": True}
DEMO_FILES = {"/var/user": [("PAM_sig", "DIR"), ("old", "DIR")],
              "/var/user/PAM_sig": [("000_PulsedCW_5ms_100us.wv", "BIN"), ("NR100_signal_par7p6_fs983_BO_0.00dB.wv", "BIN"),
                                    ("NR20_signal_par8p0.wv", "BIN"), ("readme.txt", "ASCII")]}


def demo_pout(pin):
    """가상 PA: 이득 28 dB, Psat 약 44 dBm (부드러운 압축)."""
    df = (DEMO["freq"] - 3.5e9) / 1e9  # 주파수에 따라 이득/Psat 조금씩 다르게
    lin = dbm_to_w(pin + 28.0 - 8 * df)
    psat = dbm_to_w(44.0 - 4 * df)
    p = 2.0  # Rapp 모델
    out = lin / (1 + (lin / psat) ** p) ** (1.0 / p)
    return 10 * math.log10(out) + 30


def _demo_amp(self):
    if not DEMO["rf"]:
        return 0.25
    pw = dbm_to_w(demo_pout(DEMO["pin"]))
    return 0.25 + 1.6 * math.sqrt(pw / dbm_to_w(44.0))


rto.DemoScope._amp = _demo_amp  # 오실로스코프 demo 전류를 가상 PA 와 연동


class DemoSMW:
    def write(self, cmd):
        c = cmd.upper()
        val = cmd.split(" ", 1)[1].strip() if " " in cmd else ""
        if c.startswith("SOUR1:POW ") or c.startswith("SOURCE1:POWER "):
            DEMO["pin"] = float(val)
        elif c.startswith("SOUR1:FREQ "):
            DEMO["freq"] = float(val)
        elif c.startswith("OUTP1:STAT ") or c.startswith("OUTP1 "):
            DEMO["rf"] = val.upper() in ("1", "ON")
        elif c.startswith("SOUR1:BB:ARB:WAV:SEL "):
            DEMO["wv"] = val.strip("'\"")
        elif c.startswith("SOUR1:BB:ARB:STAT "):
            DEMO["arb"] = val.upper() in ("1", "ON")

    def query(self, cmd):
        c = cmd.upper()
        time.sleep(0.005)
        if c == "*IDN?":
            return "Rohde&Schwarz,SMW200A (DEMO),1412.0000K02/000000,5.30"
        if c.startswith("SOUR1:FREQ"):
            return "%.1f" % DEMO["freq"]
        if c.startswith("SOUR1:POW"):
            return "%.2f" % DEMO["pin"]
        if c.startswith("OUTP1"):
            return "1" if DEMO["rf"] else "0"
        if c.startswith("SOUR1:BB:ARB:WAV:SEL?"):
            return '"%s"' % DEMO["wv"]
        if c.startswith("SOUR1:BB:ARB:STAT?"):
            return "1" if DEMO["arb"] else "0"
        if c.startswith("MMEM:CAT?"):
            d = cmd.split(" ", 1)[1].strip().strip("'\"").rstrip("/") if " " in cmd else "/var/user"
            if d not in DEMO_FILES:
                raise ConnectionError("데모: 폴더 없음")   # 실제 장비도 없는 폴더면 오류
            return "1000,99999," + ",".join('"%s,%s,%d"' % (n, t, 1234) for n, t in DEMO_FILES[d])
        if c.startswith("SYST:ERR"):
            return '0,"No error"'
        return "1"

    def close(self):
        pass


class DemoPSU:
    """가상 Keysight E36155A: 출력 28 V, 전류 = 대기 전류 + RF 출력에 따라 증가"""

    def write(self, cmd):
        pass

    def query(self, cmd):
        c = cmd.upper()
        time.sleep(0.003)
        if c == "*IDN?":
            return "Keysight Technologies,E36155A (DEMO),MY00000000,1.0.0"
        if c.startswith("MEAS:CURR") or c.startswith("MEASURE:CURRENT"):
            a = 0.30
            if DEMO["rf"]:
                pw = dbm_to_w(demo_pout(DEMO["pin"]))
                a += pw / (28.0 * 0.50)
            return "%+.6E" % (a * (1 + random.uniform(-0.004, 0.004)))
        if c.startswith("MEAS:VOLT") or c.startswith("MEASURE:VOLTAGE"):
            return "%+.6E" % (28.0 + random.uniform(-0.002, 0.002))
        if c.startswith("SYST:ERR"):
            return '+0,"No error"'
        return "0"

    def close(self):
        pass


def psu_poll(w):
    """Keysight E36155A: 출력 전류 · 전압 측정 (MEASure:CURRent? / VOLTage?)"""
    i = q_float(w.inst, "MEAS:CURR?")
    v = q_float(w.inst, "MEAS:VOLT?")
    w.post("val", i, v)


class DemoFSW:
    def __init__(self):
        self.cent = 3.5e9
        self.span = 10e6
        self.mx = {1: 3.5e9}
        self.mon = {1: True}

    def write(self, cmd):
        c = cmd.upper()
        val = cmd.split(" ", 1)[1].strip() if " " in cmd else ""
        n = int("".join(ch for ch in c.split(":MARK")[1][:2] if ch.isdigit()) or 1) if ":MARK" in c else 1
        if c.startswith("FREQ:CENT ") or c.startswith("SENS:FREQ:CENT "):
            self.cent = float(val)
        elif ":X " in c:
            self.mx[n] = float(val)
            self.mon[n] = True
        elif ":STAT " in c and ":MARK" in c:
            self.mon[n] = val.upper() in ("1", "ON")
        elif c.endswith(":MAX"):
            self.mx[n] = DEMO["freq"]
            self.mon[n] = True

    def query(self, cmd):
        c = cmd.upper()
        time.sleep(0.005)
        n = int("".join(ch for ch in c.split(":MARK")[1][:2] if ch.isdigit()) or 1) if ":MARK" in c else 1
        if c == "*IDN?":
            return "Rohde&Schwarz,FSW-50 (DEMO),1331.5003K50/000000,5.00"
        if "FREQ:CENT?" in c:
            return "%.1f" % self.cent
        if "FREQ:SPAN?" in c:
            return "%.1f" % self.span
        if ":STAT?" in c:
            return "1" if self.mon.get(n) else "0"
        if c.endswith(":X?"):
            return "%.1f" % self.mx.get(n, self.cent)
        if c.endswith(":Y?"):
            off = abs(self.mx.get(n, 0) - DEMO["freq"]) / 1e6
            p = demo_pout(DEMO["pin"]) - 40.0 if DEMO["rf"] else -90.0  # 40 dB 감쇠기 가정
            import random
            return "%.3f" % (p - 3.0 * off ** 2 + random.gauss(0, 0.03))
        if c.startswith("INST?"):
            return "SAN"
        if "POW:SEL?" in c:
            return "ACP"
        if "POW:RES?" in c:
            p = demo_pout(DEMO["pin"]) - 40.0 if DEMO["rf"] else -90.0
            import random
            return "%.3f,-45.21,-46.07" % (p - 0.4 + random.gauss(0, 0.03))
        if c.startswith("SYST:ERR"):
            return '0,"No error"'
        return "1"

    def close(self):
        pass


# ============================================================ 공용 통신 스레드
class TaskQueue(queue.Queue):
    """명령이 들어오면 통신 스레드를 바로 깨움 (읽기 주기만큼 기다리지 않음)."""

    def __init__(self):
        super().__init__()
        self.wake = threading.Event()

    def put(self, item, block=True, timeout=None):
        super().put(item, block, timeout)
        self.wake.set()


# ---------- PA DPD Tester 와 장비 같이 쓰기: DPD Tester 가 측정하는 동안 SMW · FSW 읽기를 잠시 멈춤
DPD_SHARE = {"t": 0.0, "busy": False}


def dpd_busy():
    """PA DPD Tester 가 측정 중인지 (설정 폴더의 dpd_busy.json 이 2초 안에 갱신됐으면). 0.3초 캐시"""
    now = time.time()
    if now - DPD_SHARE["t"] < 0.3:
        return DPD_SHARE["busy"]
    busy = False
    try:
        path = os.path.join(os.path.dirname(settings_path()), "dpd_busy.json")
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        busy = now - float(d.get("t") or 0) < 2.0
    except Exception:
        busy = False
    DPD_SHARE["t"], DPD_SHARE["busy"] = now, busy
    return busy


def bench_ack(name, connected, paused):
    """Bench 의 SMW · FSW 연결 상태 · 멈춤 여부를 0.5초마다 남김 → DPD Tester 가 멈춘 걸 확인하고 측정 시작"""
    now = time.time()
    st = DPD_SHARE.setdefault("ack", {})
    st[name] = (bool(connected), bool(paused))
    if now - DPD_SHARE.get("ack_t", 0) < 0.5 and DPD_SHARE.get("ack_last") == dict(st):
        return
    DPD_SHARE["ack_t"], DPD_SHARE["ack_last"] = now, dict(st)
    try:
        path = os.path.join(os.path.dirname(settings_path()), "bench_ack.json")
        tmp = path + ".%s.tmp" % name
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"t": now, "pid": os.getpid(), "sweep": bool(DPD_SHARE.get("sweep")),
                       "inst": {k: {"connected": v[0], "paused": v[1]} for k, v in st.items()}}, f)
        os.replace(tmp, path)
    except Exception:
        pass


class InstWorker(threading.Thread):
    """장비 하나당 하나의 통신 스레드. 결과는 (name, kind, ...) 로 out_q 에 보냄."""

    def __init__(self, name, out_q, demo_cls):
        super().__init__(daemon=True)
        self.name = name
        self.out = out_q
        self.demo_cls = demo_cls
        self.tasks = TaskQueue()
        self.inst = None
        self.interval = 0.3
        self.gtl = True
        self.alive = True
        self.poll_fn = None
        self.init_fn = None
        self.urgent = queue.Queue()     # 긴급 (RF OFF 등) - 다른 작업보다 먼저
        self.share_dpd = False          # True: PA DPD Tester 측정 중에는 이 장비를 잠시 안 건드림 (SMW · FSW)
        self.paused = False
        self.loading = False            # 화면 (설정 파일) 불러오는 중 → 그동안 읽기 쉼 (바뀌는 중인 화면에 물어보면 응답이 안 옴)

    def post(self, *msg):
        self.out.put((self.name,) + msg)

    def close(self):
        if self.inst:
            try:
                if self.gtl:
                    self.inst.write("&GTL")
            except Exception:
                pass
            self.inst.close()
        self.inst = None

    def resync(self):
        """응답 시간 초과: 바로 끊지 않고 남은 응답을 비우고 *IDN? 으로 살아 있는지 확인 → 살아 있으면 연결 유지
        (화면이 바뀌는 중 · 그 화면에 없는 값을 물어서 응답이 안 온 경우 - 장비는 멀쩡함)"""
        inst = self.inst
        sock = getattr(inst, "sock", None)
        if sock is None:
            return False
        old = sock.gettimeout()
        try:
            sock.settimeout(0.5)
            try:
                while sock.recv(65536):
                    pass
            except (socket.timeout, TimeoutError):
                pass
            inst.buf = b""
            sock.settimeout(4.0)
            inst.write("*CLS")
            ok = bool(inst.query("*IDN?").strip())
            inst.buf = b""
            return ok
        except Exception:
            return False
        finally:
            try:
                sock.settimeout(old)
            except Exception:
                pass

    def run(self):
        while self.alive:
            t0 = time.time()
            try:
                while True:
                    try:
                        fn = self.urgent.get_nowait()
                        if self.inst:
                            fn(self)
                        continue
                    except queue.Empty:
                        pass
                    if self.share_dpd and self.inst and dpd_busy():
                        with self.tasks.mutex:   # 연결 · 연결 해제는 DPD 중에도 바로 (예전: 끝날 때까지 기다려 '안 먹힘')
                            head = self.tasks.queue[0] if self.tasks.queue else None
                        if not (head and head[0] in ("connect", "disconnect")):
                            break                # DPD Tester 측정 중: 다른 작업은 줄에 둔 채 기다림 (끝나면 이어서)
                    try:
                        task = self.tasks.get_nowait()
                    except queue.Empty:
                        break
                    kind = task[0]
                    if kind == "connect":
                        self.close()
                        host, port = task[1].strip(), task[2]
                        self.post("status", "연결 중... %s" % host)
                        if host.lower() == "demo":
                            self.inst = self.demo_cls()
                        else:
                            self.inst = rto.ScpiSocket(host, port, timeout=5.0)
                        idn = self.inst.query("*IDN?")
                        self.inst.write("*CLS")
                        if self.init_fn:
                            self.init_fn(self)
                        self.post("connected", idn)
                    elif kind == "disconnect":
                        self.close()
                        self.post("disconnected")
                    elif kind == "call":
                        if not self.inst:
                            raise ConnectionError("먼저 장비에 연결하세요.")
                        task[1](self)
                pause = bool(self.share_dpd and self.inst and dpd_busy())
                if pause != self.paused:
                    self.paused = pause
                    self.post("dpd_pause", pause)
                if self.share_dpd:
                    bench_ack(self.name, self.inst is not None, pause)
                if self.inst and self.poll_fn and not pause and not self.loading:
                    self.poll_fn(self)
                    if self.gtl:
                        self.inst.write("&GTL")  # 앞 패널을 바로 쓸 수 있도록 Local 로 복귀
            except (socket.timeout, TimeoutError):
                if self.inst is not None and self.resync():
                    self.post("status", "응답이 한 번 안 옴 (화면 바뀌는 중?) - 다시 맞춤 · 연결은 그대로")
                    self.mod_q_ok = False
                    self.cfg_t = 0
                else:
                    self.post("error", "응답 시간 초과 - 연결이 끊어졌습니다. (IP / LAN / 장비 상태 확인)")
                    self.close()
                    self.post("disconnected")
            except (ConnectionError, OSError) as e:
                self.post("error", str(e))
                self.close()
                self.post("disconnected")
            except Exception as e:
                self.post("error", str(e))
            wait = self.interval - (time.time() - t0)
            self.tasks.wake.wait(max(wait, 0.03))
            self.tasks.wake.clear()


def q_float(inst, cmd):
    r = inst.query(cmd)
    try:
        v = float(r.split(",")[0])
    except ValueError:
        return float("nan")
    if abs(v) > 1e30:
        return float("nan")
    return v


# ---------- SMW200A
def smw_poll(w):
    f = q_float(w.inst, "SOUR1:FREQ?")
    p = q_float(w.inst, "SOUR1:POW?")
    rf = w.inst.query("OUTP1:STAT?").strip().upper() in ("1", "ON")
    w.post("val", f, p, rf)


def smw_wv_read(w, note=""):
    """SG 에 지금 올라간 ARB 신호 파일 + ARB ON/OFF → ("wv", 파일, on, 메모)"""
    try:
        f = w.inst.query("SOUR1:BB:ARB:WAV:SEL?").strip().strip("'\"")
        on = w.inst.query("SOUR1:BB:ARB:STAT?").strip().upper() in ("1", "ON")
    except Exception as e:
        w.post("wv", None, False, "신호 파일 읽기 실패: %s" % e)
        return
    w.post("wv", f, on, note)


def smw_init(w):
    smw_wv_read(w)


def mmem_list(w, folder):
    """장비 폴더 안의 (이름, 종류) 목록. 없는 폴더면 None"""
    try:
        r = w.inst.query('MMEM:CAT? "%s"' % folder)
    except Exception:
        try:
            w.inst.query("SYST:ERR?")
        except Exception:
            pass
        return None
    out = []
    for part in r.split('"')[1::2]:          # "name,type,size"
        bits = part.split(",")
        if bits and bits[0] not in (".", ".."):
            out.append((bits[0], bits[1].strip().upper() if len(bits) > 1 else ""))
    return out


def resolve_wv(w, path):
    """입력한 신호 파일 경로를 장비에 실제로 있는 파일로 (오타 · 대소문자 · 확장자 빠짐 자동 인식).
    반환 (실제 경로 또는 None, 설명)"""
    import difflib
    import posixpath
    path = (path or "").strip().strip("'\"").replace("\\", "/")
    if not path:
        return None, "파일 경로가 비어 있습니다"
    if not path.startswith("/"):
        path = "/var/user/" + path
    folder, name = posixpath.split(path)
    notes = []
    files = mmem_list(w, folder)
    if files is None:                         # 폴더 이름 오타 → 위 폴더에서 비슷한 폴더 찾기
        parent, sub = posixpath.split(folder.rstrip("/"))
        dirs = [n for n, t in (mmem_list(w, parent) or []) if t.startswith("DIR")]
        m = difflib.get_close_matches(sub, dirs, 1, 0.6) or \
            [d for d in dirs if d.lower() == sub.lower()]
        if not m:
            return None, "장비에 폴더가 없습니다: %s" % folder
        folder = posixpath.join(parent, m[0])
        notes.append("폴더 %s → %s" % (sub, m[0]))
        files = mmem_list(w, folder) or []
    wvs = [n for n, t in files if n.lower().endswith(".wv")]
    want = name if name.lower().endswith(".wv") else name + ".wv"
    exact = [n for n in wvs if n.lower() == want.lower()]
    if exact:
        hit = exact[0]
    else:
        m = difflib.get_close_matches(want, wvs, 1, 0.6) or \
            difflib.get_close_matches(want.lower(), [n.lower() for n in wvs], 1, 0.6)
        if not m:
            return None, "%s 폴더에 비슷한 .wv 파일이 없습니다 (%s)" % (folder, ", ".join(wvs[:6]) or "없음")
        hit = next(n for n in wvs if n.lower() == m[0].lower())
    if hit != name:
        notes.append("파일 %s → %s" % (name, hit))
    return posixpath.join(folder, hit), " · ".join(notes)


def same_wv(a, b):
    """장비가 돌려주는 이름(경로 · 확장자 유무 다를 수 있음)과 비교"""
    def norm(x):
        x = (x or "").strip().strip("'\"").replace("\\", "/").lower()
        x = x.rsplit("/", 1)[-1]
        return x[:-3] if x.endswith(".wv") else x
    return bool(a) and bool(b) and norm(a) == norm(b)


# ---------- FSW
def fsw_read_cfg(w):
    n = w.marker
    cent = q_float(w.inst, "SENS:FREQ:CENT?")
    span = q_float(w.inst, "SENS:FREQ:SPAN?")
    on = w.inst.query("CALC1:MARK%d:STAT?" % n).strip().upper() in ("1", "ON")
    mx = q_float(w.inst, "CALC1:MARK%d:X?" % n) if on else float("nan")
    w.marker_on = on
    w.post("cfg", cent, span, n, on, mx)


def fsw_init(w):
    try:
        w.inst.write("SYST:DISP:UPD ON")
        w.inst.query("*OPC?")
    except Exception:
        pass
    w.mod_q_ok = False
    if getattr(w, "bench_mode", "pulse") != "mod":   # Mod. 화면(변조 분석)에서는 마커 설정을 읽지 않음
        fsw_read_cfg(w)
    w.cfg_t = time.time()


MOD_SRC = ["자동 (FSW 화면에 맞춰)", "ACLR Tx 채널 파워", "Channel Power", "마커 (Pulse 와 같음)", "직접 SCPI"]


def fsw_mod_query(w):
    """Mod. 측정: FSW 화면의 Power 를 읽는 명령 (None = 마커). 자동이면 지금 화면을 보고 고름."""
    ch = getattr(w, "mod_src", MOD_SRC[0])
    if ch == MOD_SRC[1]:
        return "CALC1:MARK:FUNC:POW:RES? ACP"
    if ch == MOD_SRC[2]:
        return "CALC1:MARK:FUNC:POW:RES? CPOW"
    if ch == MOD_SRC[3]:
        return None
    if ch == MOD_SRC[4]:
        return (getattr(w, "mod_custom", "") or "").strip() or None
    app = w.inst.query("INST?").strip().strip('"').upper()
    if "NR5G" in app or app.startswith("NR"):
        return "FETC:CC1:SUMM:POW?"                  # 5G NR 분석 화면: 채널 파워
    sel = w.inst.query("CALC1:MARK:FUNC:POW:SEL?").strip().upper()
    if sel.startswith("ACP"):
        return "CALC1:MARK:FUNC:POW:RES? ACP"        # ACLR: 첫 값 = Tx 채널 파워 (dBm)
    if sel.startswith("MCAC"):
        return "CALC1:MARK:FUNC:POW:RES? MCAC"
    if sel.startswith("CPOW"):
        return "CALC1:MARK:FUNC:POW:RES? CPOW"
    return None


def fsw_poll(w):
    if getattr(w, "bench_mode", "pulse") == "mod":
        # Mod.: 저장해 둔 변조 측정 화면의 Power (ACLR Tx 채널 등) - 마커 설정은 읽지 않음
        if not getattr(w, "mod_q_ok", False) or time.time() - getattr(w, "cfg_t", 0) > 5.0:
            w.mod_q = fsw_mod_query(w)
            w.mod_q_ok = True
            w.cfg_t = time.time()
            w.post("modq", w.mod_q or "마커 (CALC1:MARK%d:Y?)" % w.marker)
        if w.mod_q:
            w.post("val", q_float(w.inst, w.mod_q))
            return
    if time.time() - getattr(w, "cfg_t", 0) > 2.0:  # 사용자가 장비에서 바꾼 설정 반영
        fsw_read_cfg(w)
        w.cfg_t = time.time()
    if w.marker_on:
        y = q_float(w.inst, "CALC1:MARK%d:Y?" % w.marker)
        w.post("val", y)
    else:
        w.post("val", float("nan"))



# ============================================================ 그래프 (Tk Canvas, 추가 설치 불필요)
# 주파수 색상 · 마커: RF PA Analyzer (app/controller.py) 와 똑같은 표
FREQ_COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#ff7f0e", "#9467bd", "#8c564b", "#e377c2",
               "#17becf", "#bcbd22", "#7f7f7f", "#003f5c", "#a05195"]
PALETTE = FREQ_COLORS
MEAS_MARKERS = ["o", "s", "^", "D", "v", "P"]   # 같은 주파수에 여러 측정(샘플) → 마커 모양


def distinct_colors(n):
    """각각색: Analyzer 의 distinct_colors 와 같음 (FREQ 12색 → 추가 8색 → 고르게 퍼진 색)."""
    base = FREQ_COLORS + ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#42d4f4", "#f032e6", "#9a6324"]
    if n <= len(base):
        return base[:max(n, 1)]
    import colorsys
    extra = []
    for i in range(n - len(base)):
        h = (i * 0.618033988749895) % 1.0
        r, g, b = colorsys.hsv_to_rgb(h, 0.75, 0.80 if i % 2 else 0.60)
        extra.append("#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255)))
    return base + extra


def freq_key(r):
    return round(r["freq"], 6) if r.get("freq") is not None else None


# 주파수 고정 색 (낮은 주파수부터): 파랑 · 초록 · 빨강 · 주황 · 보라 · 갈색 · 분홍 · 청록 · 올리브 · 회색 …
FIXED_FREQ_COLORS = ["#1f77b4", "#2ca02c", "#d62728", "#ff7f0e", "#9467bd", "#8c564b", "#e377c2",
                     "#17becf", "#bcbd22", "#7f7f7f", "#003f5c", "#a05195"]


def freq_colors(rows, ref=None):
    """주파수 → 고정 색. 기준 = 샘플 종류에 넣어 둔 주파수 목록 (낮은 순서로 파랑 · 초록 · 빨강 …).
    목록에 없는 주파수는 그 뒤 색을 낮은 순서로. 보이는 데이터가 바뀌어도 색은 그대로.
    화면 · 엑셀 · PA Analyzer 창 공통."""
    ref = sorted({round(f, 6) for f in (ref or [])})
    extra = sorted({freq_key(r) for r in rows if freq_key(r) is not None} - set(ref))
    out = collections.OrderedDict()
    for i, k in enumerate(ref + extra):
        out[k] = FIXED_FREQ_COLORS[i % len(FIXED_FREQ_COLORS)]
    return out


DASHES = {"실선": None, "점선": (2, 3), "파선": (6, 4), "일점쇄선": (8, 3, 2, 3)}


# 화면 테마 (화이트 / 블랙) - 위쪽 큰 숫자 칸은 두 테마 모두 검정
THEMES = {
    "light": dict(bg="#e9edf1", panel="#ffffff", border="#cfd8dc", fg="#1f2933", dim="#546e7a", unit="#78909c",
                  entry="#ffffff", entry_bd="#b0bec5", accent="#1e88e5", line="#eceff1", sel="#e3f2fd",
                  head="#eceff1", plot_bg="#ffffff", grid="#e6e6e6", plot_fg="#333333", plot_dim="#a0a0a0",
                  btn="#ffffff", btn_act="#eef2f5", bias_bg="#fffde7", bias_fg="#795548", live_bg="#e8f5e9",
                  fold="#1e88e5", fold_bg="#f3f8ff", warn="#c0504d", ok="#2e7d32", info="#1f4f8f"),
    "dark": dict(bg="#0f141a", panel="#18212a", border="#2b3743", fg="#dfe6ec", dim="#9fb0bd", unit="#7d8f9c",
                 entry="#0f151b", entry_bd="#3a4855", accent="#1e88e5", line="#26313c", sel="#173451",
                 head="#1f2a35", plot_bg="#11181f", grid="#22303b", plot_fg="#cfd8dc", plot_dim="#6c7a86",
                 btn="#22303c", btn_act="#2c3b48", bias_bg="#2a2614", bias_fg="#e6c46a", live_bg="#16301b",
                 fold="#64b5f6", fold_bg="#152230", warn="#ef9a9a", ok="#81c784", info="#90caf9"),
}
THEME = dict(THEMES["light"])


def blend(color, k=0.6):
    """색을 배경 쪽으로 k 만큼 흐리게 (선택 안 된 / 측정 중이 아닌 데이터셋 표시용)."""
    c = color.lstrip("#")
    b_ = THEME["plot_bg"].lstrip("#")
    r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    br, bg_, bb = int(b_[0:2], 16), int(b_[2:4], 16), int(b_[4:6], 16)
    return "#%02x%02x%02x" % tuple(int(v + (w - v) * k) for v, w in ((r, br), (g, bg_), (b, bb)))


def ticks_by_step(lo, hi, step):
    if not step or step <= 0 or (hi - lo) / step > 400:
        return []
    return [round(k * step, 10) for k in range(int(math.ceil(lo / step - 1e-9)), int(math.floor(hi / step + 1e-9)) + 1)]


def nice_ticks(lo, hi, n=6):
    if not (math.isfinite(lo) and math.isfinite(hi)):
        return []
    if hi - lo < 1e-12:
        lo, hi = lo - 1, hi + 1
    raw = (hi - lo) / n
    e = 10 ** math.floor(math.log10(raw))
    step = min((m * e for m in (1, 2, 2.5, 5, 10) if m * e >= raw), default=10 * e)
    t = math.ceil(lo / step - 1e-9) * step
    out = []
    while t <= hi + 1e-9 * step:
        out.append(round(t, 10))
        t += step
    return out


def draw_marker(cv, shape, cx, cy, r, color):
    """Analyzer(matplotlib) 마커 모양: o 원 · s 사각 · ^ 삼각 · D 마름모 · v 역삼각 · P 굵은 +"""
    if shape == "s":
        cv.create_rectangle(cx - r * 0.9, cy - r * 0.9, cx + r * 0.9, cy + r * 0.9, fill=color, outline=color)
    elif shape == "^":
        cv.create_polygon(cx, cy - r * 1.2, cx - r * 1.1, cy + r * 0.8, cx + r * 1.1, cy + r * 0.8, fill=color, outline=color)
    elif shape == "v":
        cv.create_polygon(cx, cy + r * 1.2, cx - r * 1.1, cy - r * 0.8, cx + r * 1.1, cy - r * 0.8, fill=color, outline=color)
    elif shape == "D":
        cv.create_polygon(cx, cy - r * 1.25, cx + r * 0.9, cy, cx, cy + r * 1.25, cx - r * 0.9, cy, fill=color, outline=color)
    elif shape == "P":
        a, b = r * 1.15, r * 0.4
        cv.create_polygon(cx - b, cy - a, cx + b, cy - a, cx + b, cy - b, cx + a, cy - b, cx + a, cy + b, cx + b, cy + b,
                          cx + b, cy + a, cx - b, cy + a, cx - b, cy + b, cx - a, cy + b, cx - a, cy - b, cx - b, cy - b,
                          fill=color, outline=color)
    else:
        cv.create_oval(cx - r, cy - r, cx + r, cy + r, fill=color, outline=color)


NO_TYPE = "(선택 안 함)"


def same_path(a, b):
    try:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    except (TypeError, ValueError):
        return False


MOD_AFTER = ["SG 시작 레벨로 바로 내림", "RF OFF", "그대로 유지"]
DEFAULT_WV = {"pulse": "/var/user/PAM_sig/000_PulsedCW_5ms_100us.wv",
              "mod": "/var/user/PAM_sig/NR100_signal_par7p6_fs983_BO_0.00dB.wv"}


def center_window(win, parent):
    """팝업을 메인 창의 정가운데에 (화면 밖으로 나가지 않게)."""
    try:
        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        parent.update_idletasks()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        if pw < 50 or ph < 50 or str(parent.state()) == "iconic":   # 최소화 등 → 화면 가운데
            px, py, pw, ph = 0, 0, win.winfo_screenwidth(), win.winfo_screenheight()
        x = px + (pw - w) // 2
        y = py + (ph - h) // 2
        vx, vy = win.winfo_vrootx(), win.winfo_vrooty()
        sw, sh = win.winfo_vrootwidth() or win.winfo_screenwidth(), win.winfo_vrootheight() or win.winfo_screenheight()
        x = min(max(x, vx), vx + sw - w)
        y = min(max(y, vy), vy + sh - h - 40)
        win.geometry("+%d+%d" % (x, y))
    except tk.TclError:
        pass


def centered_choice(root, title, text, buttons):
    """메인 창 정가운데에 뜨는 선택 창 (끝날 때까지 기다림). buttons: [(글자, 값), ...] → 고른 값, 닫으면 None"""
    win = tk.Toplevel(root)
    win.withdraw()
    win.title(title)
    win.transient(root)
    win.resizable(False, False)
    fr = ttk.Frame(win, padding=16)
    fr.pack(fill="both", expand=True)
    ttk.Label(fr, text=text, font=("맑은 고딕", 10), justify="left", wraplength=760).pack(anchor="w")
    bf = ttk.Frame(fr)
    bf.pack(fill="x", pady=(14, 0))
    res = {"v": None}

    def pick(v):
        if res.get("done"):
            return
        res["done"] = True
        res["v"] = v
        try:
            win.grab_release()
        except tk.TclError:
            pass
        win.destroy()
    first = None
    vals = {}
    for lab, val in buttons:
        b = ttk.Button(bf, text=lab, command=lambda v=val: pick(v))
        b.pack(side="left", padx=4)
        vals[str(b)] = val
        first = first or b

    def enter(e=None):   # Enter = 지금 선택된(포커스) 버튼, 없으면 첫 버튼
        w = win.focus_get()
        pick(vals.get(str(w), buttons[0][1] if buttons else None))
    win.bind("<Return>", enter)
    win.bind("<KP_Enter>", enter)
    esc = buttons[-1][1] if len(buttons) > 1 else (buttons[0][1] if buttons else None)
    win.protocol("WM_DELETE_WINDOW", lambda: pick(esc))
    win.bind("<Escape>", lambda e: pick(esc))
    center_window(win, root)
    win.deiconify()
    try:
        win.attributes("-topmost", True)
        win.lift()
        win.focus_force()
        win.grab_set()
    except tk.TclError:
        pass
    if first is not None:
        first.focus_set()
    root.wait_window(win)
    return res["v"]


class CenteredMessageBox:
    """tkinter.messagebox 대신: 모든 알림/질문 창을 메인 창 정가운데에 띄움 (같은 사용법)."""
    root = None

    def _ask(self, title, message, buttons):
        root = self.root
        if root is None:
            return None
        return centered_choice(root, title, message, buttons)

    def showinfo(self, title=None, message=None, **kw):
        self._ask(title, message, [("확인", "ok")])
        return "ok"

    def showwarning(self, title=None, message=None, **kw):
        self._ask(title, "※ " + (message or ""), [("확인", "ok")])
        return "ok"

    def showerror(self, title=None, message=None, **kw):
        self._ask(title, "✖ " + (message or ""), [("확인", "ok")])
        return "ok"

    def askyesno(self, title=None, message=None, **kw):
        return self._ask(title, message, [("예", True), ("아니요", False)]) is True

    def askokcancel(self, title=None, message=None, **kw):
        return self._ask(title, message, [("확인", True), ("취소", False)]) is True

    def askyesnocancel(self, title=None, message=None, **kw):
        return self._ask(title, message, [("예", True), ("아니요", False), ("취소", None)])


messagebox = CenteredMessageBox()


PA_MIME = "application/x-rf-pa-analyzer-pa-datasets"   # RF PA Analyzer 의 Datasets 복사 형식


def win_clipboard_set(text, extra, hwnd=None):
    """Windows 클립보드에 글자(엑셀용) + 다른 형식(이름 → bytes)을 함께 넣음. 성공 시 True."""
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    u32.OpenClipboard.argtypes = [wintypes.HWND]
    u32.OpenClipboard.restype = wintypes.BOOL
    u32.EmptyClipboard.restype = wintypes.BOOL
    u32.CloseClipboard.restype = wintypes.BOOL
    u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    u32.SetClipboardData.restype = wintypes.HANDLE
    u32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    u32.RegisterClipboardFormatW.restype = wintypes.UINT
    k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k32.GlobalAlloc.restype = wintypes.HGLOBAL
    k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalFree.argtypes = [wintypes.HGLOBAL]

    def put(fmt, data):
        h = k32.GlobalAlloc(0x0002, len(data))          # GMEM_MOVEABLE
        if not h:
            return False
        p = k32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        k32.GlobalUnlock(h)
        if not u32.SetClipboardData(fmt, h):
            k32.GlobalFree(h)
            return False
        return True

    for _ in range(10):                                  # 다른 프로그램이 잠깐 잡고 있으면 재시도
        if u32.OpenClipboard(hwnd):   # 창 핸들 필요 (None 이면 SetClipboardData 가 실패할 수 있음)
            break
        time.sleep(0.05)
    else:
        return False
    try:
        u32.EmptyClipboard()
        ok = put(13, text.replace("\n", "\r\n").encode("utf-16-le") + b"\x00\x00")   # CF_UNICODETEXT
        for name, data in extra.items():
            fmt = u32.RegisterClipboardFormatW(name)
            if fmt:
                put(fmt, data)
        return ok
    finally:
        u32.CloseClipboard()


class ScrollFrame(ttk.Frame):
    """창이 작아져 내용이 잘리면 스크롤바가 나타나는 틀. 내용은 .body 에 넣음.
    평소 높이 = 내용 높이 (natural_h), 경계선으로 줄이면 스크롤."""

    def __init__(self, parent, horizontal=True, **kw):
        super().__init__(parent, **kw)
        self.cv = tk.Canvas(self, highlightthickness=0, borderwidth=0, height=50, width=50)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.cv.yview)
        self.hsb = ttk.Scrollbar(self, orient="horizontal", command=self.cv.xview) if horizontal else None
        self.cv.configure(yscrollcommand=self.vsb.set, xscrollcommand=self.hsb.set if self.hsb else None)
        self.cv.grid(row=0, column=0, sticky="nsew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.body = ttk.Frame(self.cv)
        try:
            self.cv.configure(background=ttk.Style().lookup("TFrame", "background") or None)
        except tk.TclError:
            pass
        self.win = self.cv.create_window(0, 0, window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda e: self._sync())
        self.cv.bind("<Configure>", lambda e: self._sync())
        self.bind("<Enter>", self._wheel_on)
        self.bind("<Leave>", self._wheel_off)

    def natural_h(self):
        return self.body.winfo_reqheight()

    def natural_w(self):
        return self.body.winfo_reqwidth()

    def _sync(self):
        bw, bh = self.body.winfo_reqwidth(), self.body.winfo_reqheight()
        cw, ch = self.cv.winfo_width(), self.cv.winfo_height()
        # 내용이 넓을 땐 원래 폭, 창이 넓으면 창 폭만큼 늘려서 그룹 틀이 꽉 차게
        # 가로 스크롤 없는 틀은 창 폭에 맞춤 (내용이 창 폭으로 줄어듦)
        self.cv.itemconfigure(self.win, width=max(bw, cw) if self.hsb is not None else max(cw, 50), height=max(bh, ch))
        mh = getattr(self, "max_h", None)
        self.cv.configure(scrollregion=(0, 0, max(bw, cw), max(bh, ch)),
                          height=min(bh, mh) if mh else bh, width=bw)
        need_v = ch > 1 and bh > ch + 1
        need_h = self.hsb is not None and cw > 1 and bw > cw + 1
        if need_v and not self.vsb.winfo_ismapped():
            self.vsb.grid(row=0, column=1, sticky="ns")
        elif not need_v and self.vsb.winfo_ismapped():
            self.vsb.grid_remove()
            self.cv.yview_moveto(0)
        if self.hsb is not None:
            if need_h and not self.hsb.winfo_ismapped():
                self.hsb.grid(row=1, column=0, sticky="ew")
            elif not need_h and self.hsb.winfo_ismapped():
                self.hsb.grid_remove()
                self.cv.xview_moveto(0)

    def _wheel(self, e):
        w = e.widget
        if isinstance(w, (ttk.Combobox, ttk.Treeview, tk.Listbox)):
            return       # 콤보·표 자체 스크롤 우선
        # 마우스 아래에서 가장 안쪽의 스크롤 가능한 틀을 스크롤 (틀 안에 틀이 있어도)
        try:
            x, y = self.winfo_pointerxy()
            t = self.winfo_containing(x, y)
        except (tk.TclError, KeyError):
            t = None
        while t is not None:
            if isinstance(t, ScrollFrame) and t.vsb.winfo_ismapped():
                n = -1 if (getattr(e, "delta", 0) > 0 or getattr(e, "num", 0) == 4) else 1
                t.cv.yview_scroll(n * 2, "units")
                return
            t = t.master

    def _wheel_on(self, e=None):
        self.bind_all("<MouseWheel>", self._wheel, add="")
        self.bind_all("<Button-4>", self._wheel, add="")
        self.bind_all("<Button-5>", self._wheel, add="")

    def _wheel_off(self, e=None):
        # 안쪽 위젯으로 들어갈 때도 Leave 가 오므로 실제로 틀 밖으로 나갔을 때만 해제 (다른 스크롤 틀 안이면 유지)
        try:
            x, y = self.winfo_pointerxy()
            w = self.winfo_containing(x, y)
            while w is not None:
                if w is self or isinstance(w, ScrollFrame):
                    return
                w = w.master
        except (tk.TclError, KeyError):
            pass
        for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.unbind_all(ev)


class XYPlot(tk.Canvas):
    def __init__(self, parent, title, xlabel, ylabel, **kw):
        super().__init__(parent, bg=THEME["plot_bg"], highlightthickness=0, **kw)
        self.title, self.xlabel, self.ylabel = title, xlabel, ylabel
        self.on_axis = None    # 콜백(그래프, "x" / "y" / "y2") - 축 숫자 · 제목 클릭 → 편집 창
        self.margins = [62, 16, 26, 40]
        self.series = []       # [(label, color, [(x, y), ...]), ...]
        self.xrange = None     # 두 그래프 x축 맞춤용
        self.yaxis = None      # (min, max, step) 고정 Y축, None 이면 자동
        self.xaxis = None      # (min, max, 숫자 표시 간격, 눈금 간격)
        self.note = ""
        self.view = None       # 확대/이동 중인 범위 (x0, x1, y0, y1), None = 기본 범위
        self.partner = None    # X축을 같이 움직일 그래프 (PAE ↔ Gain)
        self._map = None
        self._drag = None
        self._pick = []        # [(key, px, py), ...] 클릭 선택용
        self._hover = []       # [(px, py, series, 번호), ...] 마우스를 올리면 측정값 표시
        self._tip_at = None
        self.on_point = None   # 콜백(기록 행) - 점을 클릭하면 기록 표에서 그 행으로
        self.on_pick = None    # 콜백(key 또는 None, 추가선택 여부)
        # 마커: v = 세로선(두 그래프 공통 Pout), h = 가로선, m = 곡선 위 점
        self.vmarks = []       # [x, ...]  (PlotPanel 이 두 그래프에 같은 목록을 넣음)
        self.hmarks = []       # [y, ...]
        self.mmarks = []       # [(key, x, y), ...]
        self.sel_mark = None   # 선택한 마커 (종류, 번호) - 방향키 ← → (↑ ↓) 로 0.1 씩, Shift = 1
        self.leg_pos = None    # 범례 위치 (그래프 영역 안 왼쪽 위 기준 px), None = 기본
        self._legbox = None
        self.bind("<Configure>", lambda e: self.redraw())
        # 마우스: 휠 위 = 확대, 휠 아래 = 축소, 왼쪽 드래그 = 이동, 더블클릭 = 원래 범위
        self.bind("<MouseWheel>", lambda e: self.on_wheel(e, 1 if e.delta > 0 else -1))
        self.bind("<Button-4>", lambda e: self.on_wheel(e, 1))
        self.bind("<Button-5>", lambda e: self.on_wheel(e, -1))
        self.bind("<ButtonPress-1>", self.on_press)
        self.bind("<B1-Motion>", self.on_drag)
        self.bind("<ButtonRelease-1>", self.on_release)
        self.bind("<Double-Button-1>", lambda e: self.reset_view(both=True))
        self.bind("<Motion>", self.on_motion, add="+")
        # 마우스를 올리면 키보드가 그래프로 (입력칸에 커서가 남아 있으면 v/h/m 이 그 칸에 들어가 마커가 안 찍히던 문제)
        self.configure(takefocus=1)
        self.bind("<Enter>", lambda e: self.focus_set(), add="+")
        self.bind("<Leave>", lambda e: self.hide_tip(), add="+")
        for seq, dx, dy in (("<Left>", -1, 0), ("<Right>", 1, 0), ("<Up>", 0, 1), ("<Down>", 0, -1)):
            self.bind(seq, lambda e, dx=dx, dy=dy: self.nudge_marker(dx, dy, 10 if e.state & 1 else 1))
        self.bind("<Escape>", lambda e: self.select_marker(None))

    # ----- 확대 / 이동
    def to_data(self, px, py):
        L, T, pw, ph, x0, x1, y0, y1 = self._map
        return x0 + (px - L) / pw * (x1 - x0), y0 + (T + ph - py) / ph * (y1 - y0)

    def current_range(self):
        m = self._map
        return (m[4], m[5], m[6], m[7]) if m else None

    def on_wheel(self, e, direction):
        if not self._map:
            return
        L, T, pw, ph = self._map[:4]
        if not (L <= e.x <= L + pw and T <= e.y <= T + ph):
            return
        cx, cy = self.to_data(e.x, e.y)
        x0, x1, y0, y1 = self.current_range()
        f = 0.8 if direction > 0 else 1.25
        nv = (cx - (cx - x0) * f, cx + (x1 - cx) * f, cy - (cy - y0) * f, cy + (y1 - cy) * f)
        self.set_view(nv)

    def axis_at(self, px, py):
        """그래프 영역 밖 클릭 위치 → 어느 축인지"""
        L, T, pw, ph = self._map[:4]
        if px < L - 2 and T - 4 <= py <= T + ph + 4:
            return "y"
        if px > L + pw + 2 and T - 4 <= py <= T + ph + 4 and self.margins[1] > 30:
            return "y2"
        if py > T + ph + 2 and L - 10 <= px <= L + pw + 10:
            return "x"
        return None

    def on_press(self, e):
        if not self._map:
            return
        r = getattr(self, "_rst", None)
        if r and r[0] <= e.x <= r[2] and r[1] <= e.y <= r[3]:     # ⟲ 아이콘: 축 원래대로
            self._drag = None
            self.reset_view(both=True)
            return
        ax = self.axis_at(e.x, e.y)
        if ax and self.on_axis:
            self._drag = None
            self.on_axis(self, ax, e)
            return
        lb = self._legbox
        if lb and lb[0] <= e.x <= lb[2] and lb[1] <= e.y <= lb[3]:
            # 범례를 누른 채 끌기 → 위치 이동
            L, T = self._map[:2]
            self._drag = ("legend", e.x, e.y, (lb[0] - L, lb[1] - T))
            self.configure(cursor="fleur")
            return
        hit = self.marker_at(e.x, e.y)
        if hit:
            self.select_marker(hit)
            self._drag = ("marker", e.x, e.y, hit)
            self.configure(cursor="sb_h_double_arrow" if hit[0] == "v" else
                           "sb_v_double_arrow" if hit[0] == "h" else "hand2")
            return
        if self.sel_mark is not None:
            self.select_marker(None)
        self._drag = (e.x, e.y, self.current_range())

    def point_at(self, px, py, tol=9):
        best, bd = None, tol ** 2
        for cx, cy, sd, ip in self._hover:
            dd = (cx - px) ** 2 + (cy - py) ** 2
            if dd <= bd:
                best, bd = (cx, cy, sd, ip), dd
        return best

    def on_motion(self, e):
        if self._drag:
            return
        h = self.point_at(e.x, e.y)
        if h is None:
            self.hide_tip()
            return
        cx, cy, sd, ip = h
        if self._tip_at == (id(sd), ip):
            return
        self.hide_tip()
        self._tip_at = (id(sd), ip)
        rows = sd.get("rows")
        r = rows[ip] if rows and ip < len(rows) else None
        f = lambda v, nd: ("%%.%df" % nd) % v if v is not None else "-"
        if r is not None:
            lines = [sd.get("label", ""),
                     "SG %s dBm → DUT Pout %s dBm" % (f(r.get("sg"), 2), f(r.get("pout"), 2)),
                     "Gain %s dB · PAE %s %% · Id %s mA" % (f(r.get("gain"), 2), f(r.get("pae"), 1), f(r.get("id"), 0)),
                     "기록 #%s  (클릭 = 표에서 보기)" % r.get("no", "?")]
        else:
            x, y = sd["pts"][ip]
            y = sd["yinv"](y) if sd.get("yinv") else y
            lines = [sd.get("label", ""), "%.2f, %.2f" % (x, y)]
        W = self.winfo_width()
        t = self.create_text(cx + 12, cy - 12, text="\n".join(lines), anchor="sw", fill=THEME["plot_fg"],
                             font=("맑은 고딕", 9), tags=("tip",))
        bb = self.bbox(t)
        if bb and bb[2] > W - 4:                       # 오른쪽 끝이면 왼쪽에 표시
            self.move(t, -(bb[2] - bb[0]) - 24, 0)
            bb = self.bbox(t)
        if bb and bb[1] < 2:
            self.move(t, 0, 2 - bb[1] + 20)
            bb = self.bbox(t)
        if bb:
            rc = self.create_rectangle(bb[0] - 5, bb[1] - 3, bb[2] + 5, bb[3] + 3, fill=THEME["plot_bg"],
                                       outline=sd.get("color", "#888"), width=2, tags=("tip",))
            self.tag_lower(rc, t)
        self.create_oval(cx - 7, cy - 7, cx + 7, cy + 7, outline=sd.get("color", "#888"), width=2, tags=("tip",))
        self.configure(cursor="hand2")

    def hide_tip(self):
        if self._tip_at is not None:
            self.delete("tip")
            self._tip_at = None
            self.configure(cursor="")

    def on_release(self, e):
        d = self._drag
        self._drag = None
        if d is None:
            return
        if d and d[0] in ("legend", "marker"):
            self.configure(cursor="")
            return
        if not d or abs(e.x - d[0]) > 3 or abs(e.y - d[1]) > 3:
            return  # 드래그(이동)였음
        h = self.point_at(e.x, e.y)               # 점 클릭 → 기록 표에서 그 행으로
        if h is not None and self.on_point:
            rows = h[2].get("rows")
            if rows and h[3] < len(rows):
                self.on_point(rows[h[3]])
        # 클릭: 가장 가까운 점(10 px 이내)의 데이터셋 선택. Ctrl/Shift 누르면 추가 선택
        best, bd = None, 10.0 ** 2
        for key, px, py in self._pick:
            dd = (px - e.x) ** 2 + (py - e.y) ** 2
            if dd <= bd:
                best, bd = key, dd
        additive = bool(e.state & 0x0001 or e.state & 0x0004)
        if self.on_pick:
            self.on_pick(best, additive)

    def on_drag(self, e):
        if not self._drag or not self._map:
            return
        if self._drag[0] == "legend":
            _, sx, sy, (ox, oy) = self._drag
            self.leg_pos = (ox + e.x - sx, oy + e.y - sy)
            self.redraw()
            return
        if self._drag[0] == "marker":
            self.move_marker(self._drag[3], e.x, e.y)
            return
        sx, sy, (x0, x1, y0, y1) = self._drag
        L, T, pw, ph = self._map[:4]
        dx = (e.x - sx) / pw * (x1 - x0)
        dy = (e.y - sy) / ph * (y1 - y0)
        self.set_view((x0 - dx, x1 - dx, y0 + dy, y1 + dy))

    # ----- 마커 (v: 세로선, h: 가로선, m: 곡선 위 점)
    def _px(self, x, y):
        L, T, pw, ph, x0, x1, y0, y1 = self._map
        return L + (x - x0) / (x1 - x0) * pw, T + ph - (y - y0) / (y1 - y0) * ph

    def nearest_point(self, px, py, limit=None):
        """화면 좌표에서 가장 가까운 데이터 점 (key, x, y)."""
        best, bd = None, float("inf")
        for sd in self.series:
            for x, y in sd["pts"]:
                cx, cy = self._px(x, y)
                dd = (cx - px) ** 2 + (cy - py) ** 2
                if dd < bd:
                    best, bd = (sd["key"], x, y), dd
        if limit is not None and bd > limit ** 2:
            return None
        return best

    def add_marker(self, kind, px, py):
        if not self._map:
            return False
        L, T, pw, ph = self._map[:4]
        if not (L <= px <= L + pw and T <= py <= T + ph):
            return False
        x, y = self.to_data(px, py)
        x, y = self.snap(x), self.snap(y)
        if kind == "v":
            self.vmarks.append(x)
            self.redraw()
            if self.partner is not None:
                self.partner.redraw()
        elif kind == "h":
            self.hmarks.append(y)
            self.redraw()
        elif kind == "m":
            pt = self.nearest_point(px, py)
            if pt is None:
                return False
            self.mmarks.append(self.on_curve(pt[0], self.snap(pt[1])) or pt)
            self.redraw()
        lst = {"v": self.vmarks, "h": self.hmarks, "m": self.mmarks}[kind]
        self.select_marker((kind, len(lst) - 1))
        return True

    def marker_legend(self, d):
        """범례 뒤에 붙일 마커 값: 세로선 x 에서의 값 '│ @41.2 14.10', 가로선 y 를 지나는 x '│ 34.1→43.20'"""
        out = []
        for x in self.vmarks:
            y = self._interp(d["pts"], x)
            if y is not None:
                out.append("@%.1f %.2f" % (x, d["yinv"](y) if d.get("yinv") else y))
        for y in self.hmarks:
            x = self._cross(d["pts"], y)
            if x is not None:
                yv = d["yinv"](y) if d.get("yinv") else y
                out.append("%.1f→%.2f" % (yv, x))
        return ("  │ " + "  ".join(out)) if out else ""

    @staticmethod
    def snap(v, st=0.1):
        return round(round(v / st) * st, 3)

    def on_curve(self, key, x):
        """곡선 key 위 x 의 점 (보간). 곡선 범위 밖이면 끝 점으로."""
        sd = next((sd for sd in self.series if sd["key"] == key), None)
        if not sd or not sd["pts"]:
            return None
        p = sorted(sd["pts"])
        x = min(max(x, p[0][0]), p[-1][0])
        y = self._interp(p, x)
        if y is None:
            y = min(p, key=lambda q: abs(q[0] - x))[1]
        return (key, x, y)

    def select_marker(self, hit):
        self.sel_mark = hit
        if hit is not None:
            self.focus_set()                # 방향키가 이 그래프로
        self.redraw()

    def nudge_marker(self, dx, dy, mult=1):
        """선택한 마커를 방향키로 0.1 씩 (Shift = 1): 세로선 · 곡선 점 = ← →, 가로선 = ↑ ↓"""
        h = self.sel_mark
        if h is None:
            return
        kind, i = h
        st = 0.1 * mult
        if kind == "v" and i < len(self.vmarks) and dx:
            self.vmarks[i] = self.snap(self.vmarks[i] + dx * st)
            if self.partner is not None:
                self.partner.redraw()
        elif kind == "h" and i < len(self.hmarks) and (dy or dx):
            self.hmarks[i] = self.snap(self.hmarks[i] + (dy or dx) * st)
        elif kind == "m" and i < len(self.mmarks) and dx:
            key, x, y = self.mmarks[i]
            pt = self.on_curve(key, self.snap(x + dx * st))
            if pt:
                self.mmarks[i] = pt
        else:
            return "break"
        self.redraw()
        return "break"

    def marker_at(self, px, py, tol=6):
        """마우스 근처 마커 → (종류, 번호)"""
        if not self._map:
            return None
        L, T, pw, ph = self._map[:4]
        if not (L - tol <= px <= L + pw + tol and T - tol <= py <= T + ph + tol):
            return None
        for i, (k, x, y) in enumerate(self.mmarks):
            cx, cy = self._px(x, y)
            if abs(cx - px) <= tol + 2 and abs(cy - py) <= tol + 2:
                return ("m", i)
        for i, x in enumerate(self.vmarks):
            if abs(self._px(x, 0)[0] - px) <= tol:
                return ("v", i)
        for i, y in enumerate(self.hmarks):
            if abs(self._px(0, y)[1] - py) <= tol:
                return ("h", i)
        return None

    def move_marker(self, hit, px, py):
        kind, i = hit
        L, T, pw, ph = self._map[:4]
        px, py = min(max(px, L), L + pw), min(max(py, T), T + ph)
        x, y = self.to_data(px, py)
        if kind == "v" and i < len(self.vmarks):
            self.vmarks[i] = self.snap(x)
            if self.partner is not None:
                self.partner.redraw()
        elif kind == "h" and i < len(self.hmarks):
            self.hmarks[i] = self.snap(y)
        elif kind == "m" and i < len(self.mmarks):
            key = self.mmarks[i][0]          # 같은 곡선 위에서만, x(Pout) 0.1 단위로
            pt = self.on_curve(key, self.snap(x))
            if pt:
                self.mmarks[i] = pt
        self.redraw()

    def delete_marker(self, px=None, py=None, everything=False):
        if everything:
            del self.vmarks[:]
            self.hmarks, self.mmarks = [], []
        else:
            hit = self.sel_mark or (self.marker_at(px, py, tol=15) if px is not None else None)
            if hit is None:   # 근처에 없으면 마지막에 넣은 것
                for lst, kind in ((self.mmarks, "m"), (self.hmarks, "h"), (self.vmarks, "v")):
                    if lst:
                        hit = (kind, len(lst) - 1)
                        break
            if hit is None:
                return
            lst = {"v": self.vmarks, "h": self.hmarks, "m": self.mmarks}[hit[0]]
            if hit[1] < len(lst):
                lst.pop(hit[1])
        self.sel_mark = None
        self.redraw()
        if self.partner is not None:
            self.partner.redraw()

    @staticmethod
    def _interp(pts, x):
        """곡선(Pout 순)에서 x 의 y 값 (범위 밖이면 None)"""
        p = sorted(pts)
        for (xa, ya), (xb, yb) in zip(p, p[1:]):
            if xa <= x <= xb and xb > xa:
                return ya + (yb - ya) * (x - xa) / (xb - xa)
        if len(p) == 1 and abs(p[0][0] - x) < 1e-9:
            return p[0][1]
        return None

    @staticmethod
    def _cross(pts, y):
        """곡선이 y 를 처음 지나는 x (Pout 순)"""
        p = sorted(pts)
        for (xa, ya), (xb, yb) in zip(p, p[1:]):
            if (ya - y) * (yb - y) <= 0 and yb != ya:
                return xa + (xb - xa) * (y - ya) / (yb - ya)
        return None

    def draw_markers(self):
        L, T, pw, ph, x0, x1, y0, y1 = self._map
        F = ("Consolas", 8)
        vis = [sd for sd in self.series if not sd.get("dim")] or self.series

        boxes = []

        def tag(x, y, text, color, anchor="w", dy=14):
            """값 상자: 다른 상자와 겹치면 위/아래로 비켜서 놓음"""
            t = self.create_text(x, y, text=text, anchor=anchor, fill=color, font=F)
            for k in range(12):
                bb = self.bbox(t)
                if not bb or not any(bb[0] < o[2] + 2 and bb[2] > o[0] - 2 and bb[1] < o[3] + 1 and bb[3] > o[1] - 1
                                     for o in boxes):
                    break
                step = dy * ((k // 2) + 1) * (1 if k % 2 == 0 else -1)
                self.coords(t, x, y + step)
            bb = self.bbox(t)
            if bb:
                boxes.append(bb)
                r = self.create_rectangle(bb[0] - 2, bb[1] - 1, bb[2] + 2, bb[3] + 1, fill=THEME["plot_bg"], outline=color)
                self.tag_lower(r, t)
        sel = self.sel_mark
        SC = THEME["accent"]
        for iv, x in enumerate(self.vmarks):
            if not (x0 <= x <= x1):
                continue
            px = self._px(x, y0)[0]
            on = sel == ("v", iv)
            self.create_line(px, T, px, T + ph, fill=SC if on else THEME["plot_fg"], dash=(5, 3), width=2 if on else 1)
            tag(px, T + 2, (" ◀ %.2f ▶ " if on else " %.2f ") % x, SC if on else THEME["plot_fg"], "n")
            used = []
            for sd in vis:
                y = self._interp(sd["pts"], x)
                if y is None or not (y0 <= y <= y1):
                    continue
                cx, cy = self._px(x, y)
                self.create_oval(cx - 4, cy - 4, cx + 4, cy + 4, outline=sd["color"], width=2)
                tag(cx + 7, cy, "%.2f" % (sd["yinv"](y) if sd.get("yinv") else y), sd["color"])
        for ih, y in enumerate(self.hmarks):
            if not (y0 <= y <= y1):
                continue
            py = self._px(x0, y)[1]
            on = sel == ("h", ih)
            self.create_line(L, py, L + pw, py, fill=SC if on else THEME["plot_fg"], dash=(5, 3), width=2 if on else 1)
            tag(L + pw - 2, py - 8, (" ▲▼ %.2f " if on else " %.2f ") % y, SC if on else THEME["plot_fg"], "e")
            used = []
            for sd in vis:
                x = self._cross(sd["pts"], y)
                if x is None or not (x0 <= x <= x1):
                    continue
                cx, cy = self._px(x, y)
                self.create_oval(cx - 4, cy - 4, cx + 4, cy + 4, outline=sd["color"], width=2)
                tag(cx, cy + 8, "%.2f" % x, sd["color"], "n")
        for im, (key, x, y) in enumerate(self.mmarks):
            if not (x0 <= x <= x1 and y0 <= y <= y1):
                continue
            sd0 = next((sd for sd in self.series if sd["key"] == key), {})
            col = sd0.get("color", "#000")
            cx, cy = self._px(x, y)
            on = sel == ("m", im)
            if on:
                self.create_oval(cx - 9, cy - 9, cx + 9, cy + 9, outline=SC, width=2)
            self.create_polygon(cx, cy - 6, cx - 5, cy - 14, cx + 5, cy - 14, fill=col, outline=THEME["plot_fg"])
            self.create_oval(cx - 5, cy - 5, cx + 5, cy + 5, outline=THEME["plot_fg"], width=2)
            tag(cx + 8, cy - 14, "%.2f, %.2f" % (x, sd0["yinv"](y) if sd0.get("yinv") else y), col)

    def set_view(self, v, sync=True):
        self.view = v
        self.redraw()
        if sync and self.partner is not None:   # 같은 Pout 범위로 다른 그래프도 맞춤
            pr = self.partner.current_range()
            if pr:
                self.partner.set_view((v[0], v[1], pr[2], pr[3]), sync=False)

    def reset_view(self, both=False):
        self.view = None
        self.redraw()
        if both and self.partner is not None:
            self.partner.reset_view()

    def set_data(self, series, xrange=None, note="", yaxis=None, xaxis=None):
        self.series, self.xrange, self.note, self.yaxis, self.xaxis = series, xrange, note, yaxis, xaxis
        self.redraw()

    def redraw(self):
        self.delete("all")
        self.configure(bg=THEME["plot_bg"])
        W, H = self.winfo_width(), self.winfo_height()
        if W < 50 or H < 50:
            return
        L, R, T, B = self.margins
        pw, ph = W - L - R, H - T - B
        pts = [pt for d in self.series for pt in d["pts"]]
        xminor = []
        if self.xaxis:
            x0, x1, xlab, xmin_step = self.xaxis
            xlabels = [round(k * xlab, 10) for k in range(int(math.ceil(x0 / xlab - 1e-9)),
                                                          int(math.floor(x1 / xlab + 1e-9)) + 1)]
            if xmin_step and xmin_step > 0 and (x1 - x0) / xmin_step <= 400:
                xminor = [round(k * xmin_step, 10) for k in range(int(math.ceil(x0 / xmin_step - 1e-9)),
                                                                  int(math.floor(x1 / xmin_step + 1e-9)) + 1)]
        else:
            if self.xrange:
                x0, x1 = self.xrange
            elif pts:
                x0, x1 = min(p[0] for p in pts), max(p[0] for p in pts)
            else:
                x0, x1 = 0, 1
            if x1 - x0 < 1e-9:
                x0, x1 = x0 - 1, x1 + 1
            mx = (x1 - x0) * 0.04
            x0, x1 = x0 - mx, x1 + mx
            xlabels = nice_ticks(x0, x1, max(3, pw // 70))
        yminor = []
        if self.yaxis:
            y0, y1, ystep = self.yaxis[:3]
            if len(self.yaxis) > 3 and self.yaxis[3]:
                yminor = ticks_by_step(y0, y1, self.yaxis[3])
            yticks = []
            v = y0
            while v <= y1 + 1e-9 * max(1, abs(ystep)) and len(yticks) < 200:
                yticks.append(round(v, 10))
                v += ystep
        else:
            if pts:
                y0, y1 = min(p[1] for p in pts), max(p[1] for p in pts)
            else:
                y0, y1 = 0, 1
            if y1 - y0 < 1e-9:
                y0, y1 = y0 - 1, y1 + 1
            my = (y1 - y0) * 0.08
            y0, y1 = y0 - my, y1 + my
            yticks = nice_ticks(y0, y1, max(3, ph // 40))
        if self.view:
            # 확대/이동 중: 범위만 바꾸고 눈금 간격은 기본 설정 유지 (너무 촘촘하면 자동)
            x0, x1, y0, y1 = self.view
            xs_ = self.xaxis[2] if self.xaxis else None
            xlabels = ticks_by_step(x0, x1, xs_) if xs_ and (x1 - x0) / xs_ <= 14 else nice_ticks(x0, x1, max(3, pw // 70))
            xminor = ticks_by_step(x0, x1, self.xaxis[3]) if (self.xaxis and self.xaxis[3]) else []
            ys_ = self.yaxis[2] if self.yaxis else None
            yticks = ticks_by_step(y0, y1, ys_) if ys_ and (y1 - y0) / ys_ <= 16 else nice_ticks(y0, y1, max(3, ph // 40))
            yminor = ticks_by_step(y0, y1, self.yaxis[3]) if (self.yaxis and len(self.yaxis) > 3 and self.yaxis[3]) else []
        self._map = (L, T, pw, ph, x0, x1, y0, y1)

        def X(v):
            return L + (v - x0) / (x1 - x0) * pw

        def Y(v):
            return T + ph - (v - y0) / (y1 - y0) * ph

        for tx in xlabels:
            self.create_line(X(tx), T, X(tx), T + ph, fill=THEME["grid"])
        for ty in yticks:
            self.create_line(L, Y(ty), L + pw, Y(ty), fill=THEME["grid"])
        # 흐린 것 → 보통 → 강조(선택·측정 중) 순서로 그려서 강조된 선이 위에 오게
        self._pick = []
        self._hover = []
        self._tip_at = None
        for sd in sorted(self.series, key=lambda d: d.get("emph", 0) - (1 if d.get("dim") else 0)):
            color = blend(sd["color"]) if sd.get("dim") else sd["color"]
            w = sd.get("width", 1.6) + (1.6 if sd.get("emph") else 0)
            mr = sd.get("marker", 3) + (1.2 if sd.get("emph") else 0)
            xy = [(X(a), Y(b)) for a, b in sd["pts"]]
            if len(xy) > 1:
                kw = {"dash": sd["dash"]} if sd.get("dash") else {}
                self.create_line(*[c for pt in xy for c in pt], fill=color, width=w, **kw)
            shape = sd.get("shape", "o")
            if sd.get("emph") and sd.get("live") and len(xy) > 1:      # 측정 중인 곡선: 뒤에 은은한 테두리
                kw = {"dash": sd["dash"]} if sd.get("dash") else {}
                gl = self.create_line(*[c for pt in xy for c in pt], fill=blend(color, 0.72), width=w + 7, **kw)
                self.tag_lower(gl)
            for ip, (cx, cy) in enumerate(xy):
                if mr > 0:
                    draw_marker(self, shape, cx, cy, mr, color)
                if L <= cx <= L + pw and T <= cy <= T + ph:
                    self._pick.append((sd["key"], cx, cy))
                    self._hover.append((cx, cy, sd, ip))
            if sd.get("live") and sd.get("emph") and xy:            # 마지막 점: 동그라미 + 값
                cx, cy = xy[-1]
                if L <= cx <= L + pw and T <= cy <= T + ph:
                    self.create_oval(cx - 9, cy - 9, cx + 9, cy + 9, outline=color, width=2.5)
                    yv = sd["pts"][-1][1]
                    yv = sd["yinv"](yv) if sd.get("yinv") else yv
                    t = self.create_text(cx + 16, cy - 20, text=" ● 측정 중 %.1f " % yv, anchor="w", fill="white",
                                         font=("맑은 고딕", 9, "bold"))
                    bb = self.bbox(t)
                    if bb:
                        r = self.create_rectangle(bb[0] - 2, bb[1] - 2, bb[2] + 2, bb[3] + 2, fill=color, outline=color)
                        self.tag_lower(r, t)
        self.draw_markers()
        # 그래프 영역 밖으로 나간 선/점 가리기
        PB, PF = THEME["plot_bg"], THEME["plot_fg"]
        for box in ((0, 0, W, T), (0, T + ph, W, H), (0, 0, L, H), (L + pw, 0, W, H)):
            self.create_rectangle(*box, fill=PB, outline=PB)
        self.create_text(L + pw / 2, 12, text=self.title, font=("맑은 고딕", 10, "bold"), fill=PF, tags=("ttl",))
        self.create_text(L + pw / 2, H - 10, text=self.xlabel, font=("맑은 고딕", 9, "underline"), fill=PF)
        self.create_text(14, T + ph / 2, text=self.ylabel, angle=90, font=("맑은 고딕", 9, "underline"), fill=PF)
        for tx in xminor:  # 1 단위 눈금
            big = any(abs(tx - lb) < 1e-9 for lb in xlabels)
            self.create_line(X(tx), T + ph, X(tx), T + ph - (7 if big else 4), fill="#888")
            self.create_line(X(tx), T, X(tx), T + (7 if big else 4), fill="#888")
        for ty in yminor:
            big = any(abs(ty - lb) < 1e-9 for lb in yticks)
            self.create_line(L, Y(ty), L + (7 if big else 4), Y(ty), fill="#888")
        for tx in xlabels:
            self.create_text(X(tx), T + ph + 12, text="%g" % tx, font=("Consolas", 8), fill=PF)
        for ty in yticks:
            self.create_text(L - 6, Y(ty), text="%g" % ty, anchor="e", font=("Consolas", 8), fill=PF)
        self.create_rectangle(L, T, L + pw, T + ph, outline="#888")
        self.draw_extra_axis(L, T, pw, ph, W, H)
        # 범례 (흰 배경 상자, 최대 10개)
        # (마우스로 범례를 끌어 옮길 수 있음, 그래프 안에서만)
        leg = self.series[:10]
        self._legbox = None
        labs = [d["label"] + self.marker_legend(d) for d in leg]
        if leg:
            wmax = max(len(t) for t in labs) * 7 + 45
            extra = len(self.series) - len(leg)
            hgt = 3 + 16 * (len(leg) + (1 if extra else 0))
            ox, oy = self.leg_pos if self.leg_pos else (4, 1)
            ox = min(max(ox, 0), max(0, pw - wmax))
            oy = min(max(oy, 0), max(0, ph - hgt))
            bx, by = L + ox, T + oy
            self._legbox = (bx, by, bx + wmax, by + hgt)
            self.create_rectangle(bx, by, bx + wmax, by + hgt, fill=PB, outline=THEME["border"])
            ly = by + 7
            for d, lab in zip(leg, labs):
                color = blend(d["color"]) if d.get("dim") else d["color"]
                kw = {"dash": d["dash"]} if d.get("dash") else {}
                self.create_line(bx + 6, ly, bx + 26, ly, fill=color, width=3 if d.get("emph") else 2, **kw)
                if d.get("marker", 3) > 0:
                    draw_marker(self, d.get("shape", "o"), bx + 16, ly, 3.5, color)
                self.create_text(bx + 31, ly, text=lab, anchor="w", fill=PF,
                                 font=("맑은 고딕", 9, "bold" if d.get("emph") else "normal"))
                ly += 16
            if extra:
                self.create_text(bx + 31, ly, text="… 외 %d개" % extra, anchor="w", fill="#888", font=("맑은 고딕", 8))
        if self.note:
            self.create_text(L + pw / 2, T + ph / 2, text=self.note, fill="#999", font=("맑은 고딕", 11))
        # 도움말: 제목과 겹치지 않는 가장 긴 문구 (창이 좁으면 짧게)
        pre = "확대 중 · " if self.view else ""
        bt = self.bbox("ttl")
        for hint in ("휠: 확대/축소 · 드래그: 이동 · 축 클릭: 편집 · v/h/m: 마커 (마커 클릭 → ←→↑↓ 0.1, Shift 1)",
                     "휠: 확대 · 드래그: 이동 · v/h/m: 마커 (클릭 → ←→ 0.1)", "v/h/m: 마커 · Del: 지우기", ""):
            if not (pre + hint):
                break
            t = self.create_text(W - 4, 3, text=pre + hint, anchor="ne", fill="#a0a0a0", font=("맑은 고딕", 8))
            bb = self.bbox(t)
            if not bt or not bb or bb[0] > bt[2] + 10:
                break
            self.delete(t)
        # ⟲ 축 원래대로 아이콘 (그래프 왼쪽 위, 그래프 영역 바로 위) - 확대 중이면 파란색으로 강조
        on = bool(self.view)
        bx0, by0 = max(2, L - 2), max(1, T - 24)
        bx1, by1 = bx0 + 22, by0 + 21
        fg = "white" if on else THEME["plot_fg"]
        self.create_rectangle(bx0, by0, bx1, by1, fill=THEME["accent"] if on else THEME["plot_bg"],
                              outline=THEME["accent"] if on else THEME["plot_dim"], width=1)
        cx, cy, rr = (bx0 + bx1) / 2, (by0 + by1) / 2, 6
        self.create_arc(cx - rr, cy - rr, cx + rr, cy + rr, start=60, extent=280, style="arc", outline=fg, width=2)
        ax_, ay_ = cx + rr * math.cos(math.radians(60)), cy - rr * math.sin(math.radians(60))
        self.create_polygon(ax_ - 4, ay_ - 4, ax_ + 4, ay_ - 1, ax_ - 1, ay_ + 4, fill=fg, outline=fg)
        self.create_text(bx1 + 4, (by0 + by1) / 2, text="원래 범위" if on else "", anchor="w",
                         fill=THEME["accent"], font=("맑은 고딕", 8, "bold"))
        self._rst = (bx0, by0, bx1 + (52 if on else 0), by1)

    def draw_extra_axis(self, L, T, pw, ph, W, H):
        """오른쪽 두 번째 축 (DualPlot 에서 그림)"""
        return


class DualPlot(XYPlot):
    """한 그래프에 Gain(왼쪽 축, 실선) + PAE(오른쪽 축, 점선). 확대 · 이동하면 오른쪽 축도 같은 비율로."""

    def __init__(self, parent, **kw):
        super().__init__(parent, "Gain · PAE vs. Pout", "DUT Pout (dBm)", "Gain (dB)", **kw)
        self.margins = [62, 54, 26, 40]
        self.ylabel2 = "PAE (%)"
        self.y2axis = None
        self._tf = None

    def set_dual(self, s_gain, s_pae, xr, note, yaxis, y2axis, xaxis):
        g0, g1 = (yaxis[0], yaxis[1]) if yaxis else self._auto([p for d in s_gain for p in d["pts"]], 20, 40)
        p0, p1 = (y2axis[0], y2axis[1]) if y2axis else self._auto([p for d in s_pae for p in d["pts"]], 0, 80)
        k = (g1 - g0) / (p1 - p0) if p1 != p0 else 1.0
        self._tf = (g0, p0, k)
        self.y2axis = y2axis
        fwd = lambda v: g0 + (v - p0) * k
        inv = lambda y: p0 + (y - g0) / k
        series = [dict(d) for d in s_gain]
        for d in s_pae:
            d2 = dict(d, pts=[(x, fwd(y)) for x, y in d["pts"]], dash=(6, 4), yinv=inv, key=d["key"],
                      label=d["label"] + " PAE")
            series.append(d2)
        ya = yaxis if yaxis else (g0, g1, (nice_ticks(g0, g1, 6)[1] - nice_ticks(g0, g1, 6)[0]) if len(nice_ticks(g0, g1, 6)) > 1 else (g1 - g0))
        self.set_data(series, xr, note, ya, xaxis)

    @staticmethod
    def _auto(pts, lo, hi):
        if not pts:
            return lo, hi
        a, b = min(p[1] for p in pts), max(p[1] for p in pts)
        if b - a < 1e-9:
            a, b = a - 1, b + 1
        m = (b - a) * 0.08
        return a - m, b + m

    def draw_extra_axis(self, L, T, pw, ph, W, H):
        if not self._tf or not self._map:
            return
        g0, p0, k = self._tf
        y0, y1 = self._map[6], self._map[7]
        q0, q1 = p0 + (y0 - g0) / k, p0 + (y1 - g0) / k
        st = self.y2axis[2] if (self.y2axis and len(self.y2axis) > 2 and self.y2axis[2]) else None
        ticks = ticks_by_step(q0, q1, st) if st and (q1 - q0) / st <= 16 else nice_ticks(q0, q1, max(3, ph // 40))
        PF = THEME["plot_fg"]
        for q in ticks:
            y = T + ph - (q - q0) / (q1 - q0) * ph
            self.create_line(L + pw, y, L + pw - 5, y, fill="#888")
            self.create_text(L + pw + 6, y, text="%g" % q, anchor="w", font=("Consolas", 8), fill=PF)
        mn = self.y2axis[3] if (self.y2axis and len(self.y2axis) > 3 and self.y2axis[3]) else None
        for q in (ticks_by_step(q0, q1, mn) if mn else []):
            y = T + ph - (q - q0) / (q1 - q0) * ph
            self.create_line(L + pw, y, L + pw - 3, y, fill="#888")
        self.create_text(W - 12, T + ph / 2, text=self.ylabel2, angle=270, font=("맑은 고딕", 9, "underline"), fill=PF)
        # 선 종류 안내
        bx, by = L + pw - 132, T + 6
        self.create_rectangle(bx, by, bx + 126, by + 36, fill=THEME["plot_bg"], outline=THEME["border"])
        self.create_line(bx + 8, by + 11, bx + 34, by + 11, fill=PF, width=2)
        self.create_text(bx + 40, by + 11, text="Gain (실선)", anchor="w", fill=PF, font=("맑은 고딕", 8))
        self.create_line(bx + 8, by + 26, bx + 34, by + 26, fill=PF, width=2, dash=(6, 4))
        self.create_text(bx + 40, by + 26, text="PAE (점선)", anchor="w", fill=PF, font=("맑은 고딕", 8))


class PlotPanel(ttk.Frame):
    """Gain · PAE vs Pout. 합쳐 보기(한 그래프, 축 2개) / 나눠 보기(왼쪽 Gain | 오른쪽 PAE). 주파수별 색상."""

    def __init__(self, parent, mode="combined"):
        super().__init__(parent)
        self.split = ttk.PanedWindow(self, orient="horizontal")
        self.p2 = XYPlot(self.split, "Gain vs. Pout", "DUT Pout (dBm)", "Gain (dB)", height=260)
        self.p1 = XYPlot(self.split, "PAE vs. Pout", "DUT Pout (dBm)", "PAE (%)", height=260)
        self.split.add(self.p2, weight=1)
        self.split.add(self.p1, weight=1)
        self.p1.partner, self.p2.partner = self.p2, self.p1
        self.p2.vmarks = self.p1.vmarks          # 세로 마커는 두 그래프 같은 Pout 에
        self.pd = DualPlot(self, height=260)
        self.mode = None
        self.set_mode(mode)

    def plots(self):
        return (self.p1, self.p2, self.pd)

    def set_mode(self, mode):
        if mode == self.mode:
            return
        self.mode = mode
        if mode == "split":
            self.pd.pack_forget()
            self.split.pack(fill="both", expand=True)
        else:
            self.split.pack_forget()
            self.pd.pack(fill="both", expand=True)

    def show(self, s1, s2, xr, notes, axes, titles=None):
        xax = axes[2] if len(axes) > 2 else None
        t = titles or {}
        self.p1.xlabel = self.p2.xlabel = self.pd.xlabel = t.get("x", "DUT Pout (dBm)")
        self.p1.ylabel = self.pd.ylabel2 = t.get("pae", "PAE (%)")
        self.p2.ylabel = self.pd.ylabel = t.get("gain", "Gain (dB)")
        self.p1.set_data(s1, xr, notes[0], axes[0], xax)
        self.p2.set_data(s2, xr, notes[1], axes[1], xax)
        self.pd.set_dual(s2, s1, xr, notes[1], axes[1], axes[0], xax)

    def reset_view(self):
        self.p1.reset_view(both=True)
        self.pd.reset_view()


def set_app_icon(root):
    """창 · 작업 표시줄 아이콘: 측정 장비 화면 모양 (Gain · PAE 곡선)"""
    here = os.path.dirname(os.path.abspath(__file__))
    bases = [getattr(sys, "_MEIPASS", None), here, os.path.dirname(sys.executable)]
    cands = []
    for b in bases:
        if b:
            cands += [os.path.join(b, "bench_assets"), os.path.join(b, "assets")]
    if sys.platform.startswith("win"):
        try:   # 스크립트로 실행해도 작업 표시줄에 python 아이콘 대신 이 아이콘
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PABenchMonitor")
        except Exception:
            pass
    for d in cands:
        ico, png = os.path.join(d, "bench.ico"), os.path.join(d, "bench.png")
        try:
            if sys.platform.startswith("win") and os.path.isfile(ico):
                root.iconbitmap(default=ico)
                return True
            if os.path.isfile(png):
                img = tk.PhotoImage(file=png)
                root.iconphoto(True, img)
                root._app_icon = img
                return True
        except tk.TclError:
            continue
    return False


def make_icon(root, kind):
    """16x16 아이콘: save(디스켓) / open(폴더)."""
    img = tk.PhotoImage(master=root, width=16, height=16)

    def box(x0, y0, x1, y1, color):
        img.put(color, to=(x0, y0, x1, y1))

    if kind == "save":
        box(1, 1, 15, 15, "#2f5d8a")      # 몸체
        box(4, 1, 12, 6, "#dfe8f1")       # 셔터
        box(9, 2, 11, 5, "#2f5d8a")
        box(3, 9, 13, 15, "#ffffff")      # 라벨
        box(4, 11, 12, 12, "#9fb3c8")
    else:
        box(1, 3, 7, 5, "#c99a2e")        # 탭
        box(1, 5, 15, 14, "#e8b84a")      # 폴더
        box(1, 7, 15, 8, "#c99a2e")
    return img


# ============================================================ 메인 화면
class BenchApp:
    COLS = [("no", "No", 36), ("sample", "샘플", 56), ("time", "시간", 66), ("freq", "주파수(GHz)", 80),
            ("sg", "SG(dBm)", 68), ("sa", "SA(dBm)", 68), ("inl", "In loss(dB)", 74), ("outl", "Out loss(dB)", 80), ("lossf", "Loss 기준(GHz)", 92),
            ("pin", "DUT Pin(dBm)", 88), ("pout", "DUT Pout(dBm)", 92), ("gain", "Gain(dB)", 66),
            ("id", "Id(mA)", 68), ("vdd_used", "Vdd(V)", 58), ("pdc", "Pdc(W)", 64), ("de", "DE(%)", 58), ("pae", "PAE(%)", 62)]

    def __init__(self, root):
        self.root = root
        if isinstance(messagebox, CenteredMessageBox):
            messagebox.root = root
        self.s = load_settings()
        self.q = queue.Queue()
        self.mode = "mod" if self.s.get("mode") == "mod" else "pulse"
        self.rows_by_mode = {"pulse": [], "mod": []}   # Pulse / Mod 측정 기록은 따로
        self.rows = self.rows_by_mode[self.mode]
        self.mod_win = None
        # 실시간 값
        self.smw = {"conn": False, "f": None, "p": None, "rf": False, "t": 0}
        self.fsw = {"conn": False, "y": None, "mx": None, "t": 0}
        self.psu = {"conn": False, "i": None, "v": None, "t": 0}
        self.psu_samples = collections.deque(maxlen=2000)  # (t, A)
        self.cur_samples = collections.deque(maxlen=2000)  # (t, A)
        self.fsw_samples = collections.deque(maxlen=2000)  # (t, dBm)
        # 자동 기록 상태
        self.rec_on = False
        self.last_pin = None
        self.change_t = None
        self.pending = False

        root.title("%s %s%s" % (APP_NAME, VERSION, "  (PA Analyzer 연동판)" if HAS_ANALYZER else ""))
        geo = self.s.get("geometry") or "1560x900"
        try:
            gw = int(geo.split("x")[0])
            if gw < 1400:
                geo = "1560x900"
        except ValueError:
            geo = "1560x900"
        # 모니터보다 크면 아래쪽(바이어스 칸 등)이 화면 밖으로 잘림 → 화면에 맞춤 (작은 모니터)
        try:
            sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
            gw_, gh_ = (int(v) for v in geo.split("+")[0].split("x"))
            if gw_ > sw or gh_ > sh - 60:
                geo = "%dx%d+0+0" % (min(gw_, sw), min(gh_, sh - 80))
                if sys.platform.startswith("win"):
                    root.after(50, lambda: root.state("zoomed"))
        except (ValueError, tk.TclError):
            pass
        root.geometry(geo)
        root.minsize(900, 560)
        root.option_add("*Font", ("맑은 고딕", 9))
        set_app_icon(root)
        self.apply_theme(redraw=False)

        self.smw_w = InstWorker("smw", self.q, DemoSMW)
        self.smw_w.poll_fn = smw_poll
        self.smw_w.init_fn = smw_init
        self.smw_w.share_dpd = True
        self.fsw_w = InstWorker("fsw", self.q, DemoFSW)
        self.fsw_w.poll_fn = fsw_poll
        self.fsw_w.init_fn = fsw_init
        self.fsw_w.share_dpd = True
        self.fsw_w.marker = int(self.s.get("fsw_marker", 1))
        self.fsw_w.marker_on = False
        for w in (self.smw_w, self.fsw_w):
            w.start()

        self.var_smw_step = tk.StringVar(value=self.s["smw_step"])
        self.var_smw_max = tk.StringVar(value=self.s["smw_max"])
        self.ds_style, self.ds_hidden, self.ds_selected = {}, set(), set()
        self.color_mode = "freq"
        self.fcolor_cache = collections.OrderedDict()
        self.analyzer = AnalyzerLink()
        self.build_topbar()
        self.view = self.mode                     # 화면: pulse / mod / dpd (self.mode 는 Pulse / Mod. 측정 그대로)
        self.dpd = None
        self.build_modetabs()
        self.build_dashboard()
        self.build_statusbar()
        self.build_main()
        self.build_smw_tab()
        self.build_fsw_tab()
        self.fsw_mod_opts()
        self.build_scope_tab()
        for k in self.TRIG_KEYS:                   # 전류 트리거 설정도 샘플 종류마다 기억
            if hasattr(self.scope, k):
                getattr(self.scope, k).trace_add("write", lambda *a: self.cond_changed())
        for k in self.COND_KEYS:                   # 나중에 만들어진 측정 조건 칸 (끝나면 복귀 · RF OFF · 바이어스 OFF 등)
            if k not in getattr(self, "_cond_traced", set()) and hasattr(self, k):
                getattr(self, k).trace_add("write", lambda *a: self.cond_changed())
                self._cond_traced.add(k)
        self.load_stype_cond()
        self.root.after(300, self.refresh_latest_pams)  # 예전 설정을 불러와도 .pam 은 항상 가장 최근 파일로
        self.nb.add(self.psu_extra, text="  파워서플라이 연결 · SCPI  ")
        try:
            self.nb.select(int(self.s.get("tab", 0)))
        except Exception:
            pass
        self.apply_theme(redraw=False)
        self.apply_worker_opts()
        menubar = tk.Menu(root)
        mfile = tk.Menu(menubar, tearoff=0)
        mfile.add_command(label="케이블 손실 표 불러오기 (txt)...", command=self.loss_load)
        mfile.add_command(label="케이블 손실 표 내보내기 (txt)...", command=self.loss_save)
        mfile.add_separator()
        mfile.add_command(label="측정 기록 엑셀 저장 (데이터 + 그래프)...", command=self.export_excel)
        mfile.add_command(label="측정 기록 CSV 저장...", command=self.save_csv)
        mfile.add_command(label="선택 행 복사 (엑셀 · PA Analyzer 붙여넣기용)   Ctrl+C", command=self.copy_rows)
        mfile.add_command(label="선택 행 PA Analyzer 프로젝트(.pam)로 저장...", command=self.save_pam)
        mfile.add_command(label="측정 기록 전체 지우기...", command=self.clear_rows)
        mfile.add_separator()
        mfile.add_command(label="설정 저장 (파일로)...", command=self.export_settings)
        mfile.add_command(label="설정 불러오기...", command=self.import_settings)
        mfile.add_separator()
        mfile.add_command(label="종료", command=self.on_close)
        menubar.add_cascade(label="파일", menu=mfile)
        mgr = tk.Menu(menubar, tearoff=0)
        if HAS_ANALYZER:
            mgr.add_command(label="PA Analyzer 그래프 창 열기 (실시간 연동)", command=self.open_analyzer)
        mgr.add_command(label="그래프 새 창", command=self.open_plot_window)
        menubar.add_cascade(label="그래프", menu=mgr)
        root.config(menu=menubar)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.tick)
        root.after(300, self.refresh_plots)
        root.after(300, self.apply_mode_ui)
        if self.s.get("view") == "dpd":
            root.after(800, lambda: self.set_view("dpd"))      # 지난번에 DPD 화면이었으면 그대로
        root.after(350, self.fit_ctrl_height)
        # 단축키: 4 = 파워 올리기, 5 = 기록, 6 = 파워 내리기 (숫자 키패드도 동일)
        self.p_change_t = 0.0
        self.rec_wait = None
        self.last_key_t = {}
        self.sweep = None
        self.ramp = None
        self.undo_stack = []       # Ctrl+Z: 기록 표 되돌리기 (최근 50단계)
        self.redo_stack = []       # Ctrl+Y: 되돌린 것 다시
        self.ds_done = {}          # 자동 측정이 끝난 데이터셋 → "측정 완료" / "중지됨" (그래프 범례)
        self.freq_ok_pair = None
        self.pam_dirty = set()      # 연결된 .pam 에 저장 안 된 측정이 있는 샘플 종류
        self.pam_renames = {}       # 샘플 번호 반영으로 바뀐 Dataset 이름: 새 이름 → {예전 이름}
        self.pam_pending = {}       # PA Analyzer 창에 맡긴 저장 요청 id → (종류, .pam)
        self.cur_mem = {}
        self.mem_freq = None
        self._live_key = None
        root.bind_all("<Escape>", self.on_escape)
        for keys, fn in ((("4", "KP_4"), lambda: self.smw_step(+1)),
                         (("5", "KP_5"), self.record_request),
                         (("6", "KP_6"), lambda: self.smw_step(-1))):
            for k in keys:
                root.bind_all("<KeyPress-%s>" % k, lambda e, fn=fn: self.on_hotkey(e, fn))
        # 그래프 위에 마우스를 두고 v / h / m = 마커, Delete = 근처 마커 지우기, Shift+Delete = 모두
        for k in ("v", "h", "m", "V", "H", "M"):
            root.bind_all("<KeyPress-%s>" % k, lambda e, k=k.lower(): self.on_marker_key(e, k))
        root.bind_all("<KeyPress-Delete>", lambda e: self.on_marker_key(e, "del"))
        root.bind_all("<Control-z>", self.on_undo_key)
        root.bind_all("<Control-Z>", self.on_undo_key)
        root.bind_all("<Control-y>", lambda e: self.on_undo_key(e, redo=True))
        root.bind_all("<Control-Y>", lambda e: self.on_undo_key(e, redo=True))
        root.bind_all("<Shift-KeyPress-Delete>", lambda e: self.on_marker_key(e, "delall"))

    # ------------------------------------------------ 테마 (화이트 / 블랙)
    # ------------------------------------------------ 체크박스 → 켜기/끄기 스위치 (켜짐 = 파랑)
    @staticmethod
    def draw_switch(img, bg, track, knob, on, W=30, H=16):
        """작은 토글 스위치 그림 (가장자리 부드럽게, 추가 설치 없이 Tk 이미지에 직접)"""
        def hx(c):
            c = c.lstrip("#")
            return [int(c[i:i + 2], 16) for i in (0, 2, 4)]
        B, TR, KN = hx(bg), hx(track), hx(knob)
        r = H / 2.0
        kr = r - 2.0
        kx = (W - r) if on else r
        rows = []
        SS = 3
        for y in range(H):
            line = []
            for x in range(W):
                ct = ck = 0
                for sy in range(SS):
                    for sx in range(SS):
                        px, py = x + (sx + 0.5) / SS, y + (sy + 0.5) / SS
                        cx = min(max(px, r), W - r)
                        if (px - cx) ** 2 + (py - r) ** 2 <= r * r:
                            ct += 1
                            if (px - kx) ** 2 + (py - r) ** 2 <= kr * kr:
                                ck += 1
                n = SS * SS
                a, k = ct / n, ck / n
                col = [B[i] + (TR[i] - B[i]) * a for i in range(3)]
                col = [col[i] + (KN[i] - col[i]) * k for i in range(3)]
                line.append("#%02x%02x%02x" % tuple(int(round(v)) for v in col))
            rows.append("{" + " ".join(line) + "}")
        img.put(" ".join(rows))

    def apply_switch_style(self, st):
        T = THEME
        off_t = "#455a64" if self.s.get("theme") == "dark" else "#b0bec5"
        if not hasattr(self, "_sw_imgs"):
            self._sw_imgs = {k: tk.PhotoImage(width=30, height=16) for k in
                             ("p_on", "p_off", "p_don", "p_doff", "b_on", "b_off", "b_don", "b_doff")}
            for pre in ("p", "b"):
                im = self._sw_imgs
                name = "Switch%s.indicator" % pre
                try:
                    st.element_create(name, "image", im[pre + "_off"],
                                      ("disabled", "selected", im[pre + "_don"]), ("disabled", im[pre + "_doff"]),
                                      ("selected", im[pre + "_on"]), border=0, padding=(0, 0, 6, 0), sticky="w")
                except tk.TclError:
                    pass
        im = self._sw_imgs
        for pre, bgk in (("p", "panel"), ("b", "bg")):
            bg = T[bgk]
            self.draw_switch(im[pre + "_on"], bg, T["accent"], "#ffffff", True)
            self.draw_switch(im[pre + "_off"], bg, off_t, "#ffffff", False)
            self.draw_switch(im[pre + "_don"], bg, self.mix(T["accent"], bg, 0.55), self.mix("#ffffff", bg, 0.3), True)
            self.draw_switch(im[pre + "_doff"], bg, self.mix(off_t, bg, 0.55), self.mix("#ffffff", bg, 0.3), False)
        for sty, pre in (("Card.TCheckbutton", "p"), ("TCheckbutton", "b")):
            try:
                st.layout(sty, [("Checkbutton.padding", {"sticky": "nswe", "children": [
                    ("Switch%s.indicator" % pre, {"side": "left", "sticky": ""}),
                    ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
                        ("Checkbutton.label", {"sticky": "nswe"})]})]})])
                st.configure(sty, padding=(1, 2, 4, 2))
            except tk.TclError:
                pass

    def apply_theme(self, name=None, redraw=True):
        name = name or self.s.get("theme", "light")
        if name not in THEMES:
            name = "light"
        self.s["theme"] = name
        THEME.clear()
        THEME.update(THEMES[name])
        T = THEME
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        F = ("맑은 고딕", 9)
        st.configure(".", background=T["bg"], foreground=T["fg"], fieldbackground=T["entry"], font=F,
                     bordercolor=T["border"], lightcolor=T["panel"], darkcolor=T["border"],
                     troughcolor=T["bg"], selectbackground=T["accent"], selectforeground="white",
                     insertcolor=T["fg"])
        st.map(".", foreground=[("disabled", T["unit"])])
        for sty in ("TFrame", "TLabel", "TCheckbutton", "TRadiobutton", "TLabelframe", "TLabelframe.Label"):
            st.configure(sty, background=T["bg"], foreground=T["fg"])
        # 카드(패널) 안
        st.configure("Card.TFrame", background=T["panel"])
        st.configure("Card.TLabel", background=T["panel"], foreground=T["fg"])
        st.configure("Lab.TLabel", background=T["panel"], foreground=T["dim"])
        st.configure("Unit.TLabel", background=T["panel"], foreground=T["unit"])
        st.configure("Hint.TLabel", background=T["panel"], foreground=T["unit"], font=("맑은 고딕", 8))
        st.configure("Info.TLabel", background=T["panel"], foreground=T["info"])
        st.configure("Head.TLabel", background=T["panel"], foreground=T["fg"], font=("맑은 고딕", 10, "bold"))
        st.configure("Num.TLabel", background=T["accent"], foreground="white", font=("맑은 고딕", 8, "bold"),
                     anchor="center", width=2)
        st.configure("Fold.TLabel", background=T["panel"], foreground=T["fold"], font=("맑은 고딕", 9))
        st.configure("Card.TCheckbutton", background=T["panel"], foreground=T["fg"])
        st.map("Card.TCheckbutton", background=[("active", T["panel"])], indicatorcolor=[("selected", T["accent"]),
                                                                                       ("!selected", T["entry"])])
        st.map("TCheckbutton", background=[("active", T["bg"])])
        self.apply_switch_style(st)
        st.configure("Card.TRadiobutton", background=T["panel"], foreground=T["fg"])
        st.map("Card.TRadiobutton", background=[("active", T["panel"])])
        st.configure("TEntry", fieldbackground=T["entry"], foreground=T["fg"], bordercolor=T["entry_bd"],
                     lightcolor=T["entry"], darkcolor=T["entry"], padding=(4, 2))
        st.map("TEntry", bordercolor=[("focus", T["accent"])], lightcolor=[("focus", T["accent"])])
        st.configure("Num.TEntry", fieldbackground=T["entry"], foreground=T["fg"], bordercolor=T["entry_bd"],
                     lightcolor=T["entry"], darkcolor=T["entry"], padding=(4, 2))
        st.configure("TCombobox", fieldbackground=T["entry"], foreground=T["fg"], background=T["btn"],
                     arrowcolor=T["fg"], bordercolor=T["entry_bd"])
        st.map("TCombobox", fieldbackground=[("readonly", T["entry"])], foreground=[("readonly", T["fg"])],
               selectbackground=[("readonly", T["entry"])], selectforeground=[("readonly", T["fg"])])
        self.root.option_add("*TCombobox*Listbox.background", T["entry"])
        self.root.option_add("*TCombobox*Listbox.foreground", T["fg"])
        # 맨 위 막대 [샘플] 칸: 막대 (늘 어두움) 의 장비 칩과 같은 모양 - 어두운 칸 · 밝은 글자 (화이트 / 블랙 둘 다)
        st.configure("Top.TCombobox", fieldbackground="#263340", foreground="#e0f2f1", background="#263340",
                     arrowcolor="#90a4ae", bordercolor="#33475a", lightcolor="#263340", darkcolor="#263340", padding=(6, 1))
        st.map("Top.TCombobox",
               fieldbackground=[("readonly", "focus", "#2f4255"), ("readonly", "hover", "#33475a"), ("readonly", "#263340")],
               foreground=[("readonly", "#e0f2f1")], background=[("active", "#33475a"), ("readonly", "#263340")],
               selectbackground=[("readonly", "#263340"), ("readonly", "focus", "#2f4255")],
               selectforeground=[("readonly", "#e0f2f1")], arrowcolor=[("active", "#e0f2f1")],
               bordercolor=[("focus", "#4fc3f7"), ("hover", "#4fc3f7")])
        st.configure("TButton", background=T["btn"], foreground=T["fg"], bordercolor=T["entry_bd"],
                     lightcolor=T["btn"], darkcolor=T["btn"], padding=(8, 3), focuscolor=T["btn"])
        st.map("TButton", background=[("pressed", T["btn_act"]), ("active", T["btn_act"])])
        st.configure("Pri.TButton", background=T["accent"], foreground="white", bordercolor="#1565c0",
                     lightcolor=T["accent"], darkcolor=T["accent"], font=("맑은 고딕", 9, "bold"))
        st.map("Pri.TButton", background=[("pressed", "#1565c0"), ("active", "#1976d2")])
        st.configure("Small.TButton", padding=(6, 1), font=("맑은 고딕", 8))
        st.configure("Seg.Toolbutton", background=T["panel"], foreground=T["accent"], padding=(10, 3),
                     borderwidth=1, relief="solid", bordercolor=T["accent"], lightcolor=T["panel"],
                     darkcolor=T["panel"], font=("맑은 고딕", 9), anchor="center")
        st.map("Seg.Toolbutton", background=[("selected", T["accent"]), ("active", T["sel"])],
               foreground=[("selected", "white")], lightcolor=[("selected", T["accent"])],
               darkcolor=[("selected", T["accent"])], relief=[("selected", "solid")])
        st.configure("Tab.Toolbutton", background=T["head"], foreground=T["dim"], padding=(16, 6),
                     font=("맑은 고딕", 10), borderwidth=0, relief="flat", lightcolor=T["head"], darkcolor=T["head"])
        st.map("Tab.Toolbutton", background=[("selected", T["accent"]), ("active", T["sel"])],
               foreground=[("selected", "white")], lightcolor=[("selected", T["accent"])],
               darkcolor=[("selected", T["accent"])], relief=[("selected", "flat")])
        st.configure("Treeview", background=T["panel"], fieldbackground=T["panel"], foreground=T["fg"],
                     bordercolor=T["border"], rowheight=22)
        st.map("Treeview", background=[("selected", T["sel"])], foreground=[("selected", T["fg"])])
        st.configure("Treeview.Heading", background=T["head"], foreground=T["dim"], bordercolor=T["border"],
                     font=("맑은 고딕", 9, "bold"), relief="flat")
        st.map("Treeview.Heading", background=[("active", T["sel"])])
        st.configure("TNotebook", background=T["bg"], bordercolor=T["border"])
        st.configure("TNotebook.Tab", background=T["head"], foreground=T["dim"], padding=(12, 4))
        st.map("TNotebook.Tab", background=[("selected", T["panel"])], foreground=[("selected", T["fg"])])
        for sb_ in ("TScrollbar", "Vertical.TScrollbar", "Horizontal.TScrollbar"):
            st.configure(sb_, background=T["border"], troughcolor=T["bg"], bordercolor=T["bg"],
                         arrowcolor=T["dim"], lightcolor=T["border"], darkcolor=T["border"], gripcount=0)
            st.map(sb_, background=[("active", T["unit"])])
        st.configure("TPanedwindow", background=T["bg"])
        st.configure("Sash", sashthickness=6, gripcount=0, background=T["bg"])
        st.configure("TLabelframe", background=T["bg"], bordercolor=T["border"])
        self.root.configure(bg=T["bg"])
        # tk 위젯 (테마 등록된 것)
        for w, opts in list(getattr(self, "_themed", [])):
            try:
                w.configure(**{k: THEME[v] for k, v in opts.items()})
            except tk.TclError:
                pass
        for sf in list(getattr(self, "_scrollframes", [])):
            try:
                sf.cv.configure(background=THEME[sf._bgkey])
            except tk.TclError:
                pass
        if hasattr(self, "theme_btns"):
            for k, b in self.theme_btns.items():
                b.configure(bg="#1e88e5" if k == name else "#263340", fg="white")
        if redraw and hasattr(self, "plots"):
            self.refresh_plots()
            try:
                self.tree.tag_configure("check", background="#5a2626" if name == "dark" else "#ffd6d6")
            except (AttributeError, tk.TclError):
                pass

    def reg(self, w, **opts):
        """tk 위젯 색을 테마에 맞춤 (opts: 옵션 = THEME 키)"""
        self.__dict__.setdefault("_themed", []).append((w, opts))
        try:
            w.configure(**{k: THEME[v] for k, v in opts.items()})
        except tk.TclError:
            pass
        return w

    def scrollframe(self, parent, bgkey="panel", **kw):
        sf = ScrollFrame(parent, **kw)
        sf._bgkey = bgkey
        sf.body.configure(style="Card.TFrame" if bgkey == "panel" else "TFrame")
        sf.cv.configure(background=THEME[bgkey])
        self.__dict__.setdefault("_scrollframes", []).append(sf)
        return sf

    def set_theme(self, name):
        self.apply_theme(name)
        if getattr(self, "dpd", None) is not None:
            self.dpd.apply_theme(name, styles=False)
        self.status("화면 테마: %s" % ("블랙" if name == "dark" else "화이트"), "#39ff88")

    # ------------------------------------------------ 맨 위 줄: 연결 상태 · 모두 연결 · 테마 · 긴급 정지
    def build_topbar(self):
        TB = "#1c2530"
        bar = tk.Frame(self.root, bg=TB)
        bar.pack(side="top", fill="x")
        tk.Label(bar, text=APP_NAME, bg=TB, fg="white", font=("맑은 고딕", 12, "bold")).pack(side="left", padx=(12, 4), pady=6)
        tk.Label(bar, text=VERSION + ("  (PA Analyzer 연동판)" if HAS_ANALYZER else ""), bg=TB, fg="#90a4ae",
                 font=("맑은 고딕", 8)).pack(side="left", padx=(0, 14))
        sf_ = tk.Frame(bar, bg=TB)
        sf_.pack(side="left", padx=(0, 8))
        tk.Label(sf_, text="샘플", bg=TB, fg="#90a4ae", font=("맑은 고딕", 9)).pack(side="left", padx=(0, 4))
        for k_, v_ in (("background", "#263340"), ("foreground", "#e0f2f1"), ("selectBackground", "#1e88e5"),
                       ("selectForeground", "white")):          # 펼친 목록도 막대와 같은 어두운 색 (이 칸만)
            self.root.option_add("*topsample*Listbox.%s" % k_, v_, 100)
        self.cb_stype_top = ttk.Combobox(sf_, name="topsample", state="readonly", width=12, font=("맑은 고딕", 9, "bold"),
                                         style="Top.TCombobox")
        self.cb_stype_top.pack(side="left")
        self.cb_stype_top.bind("<<ComboboxSelected>>", lambda e: self.top_stype_pick())

        self.chips = {}
        for key, name in (("smw", "SMW200A"), ("fsw", "FSW"), ("rto", "RTO2044"), ("vdd", "E36155A Vdd"),
                          ("gate", "N6705C Gate")) + ((("ana", "PA Analyzer"),) if HAS_ANALYZER else ()):
            c = tk.Label(bar, text="● " + name, bg="#263340", fg="#78909c", font=("맑은 고딕", 8), padx=8, pady=2,
                         cursor="hand2")
            c.pack(side="left", padx=2)
            c.bind("<Button-1>", lambda e, k=key: self.goto_equip(k))     # 누르면 그 장비 설정으로
            c.bind("<Enter>", lambda e, w=c: w.configure(bg="#33475a"))
            c.bind("<Leave>", lambda e, w=c: w.configure(bg="#263340"))
            self.chips[key] = c
        mk = dict(relief="flat", padx=10, pady=2, activeforeground="#ffffff", font=("맑은 고딕", 9), bd=0)
        tk.Button(bar, text="모두 연결", command=self.connect_all, bg="#2f3d4a", fg="#e0e0e0",
                  activebackground="#3c4d5c", **mk).pack(side="left", padx=(12, 3))
        self.btn_cur = tk.Button(bar, text="▶ 전류 측정 시작", command=self.toggle_current, bg="#7d6a26", fg="#ffffff",
                                 activebackground="#93802f", **mk)
        self.btn_cur.pack(side="left", padx=3)
        tk.Button(bar, text="⚙ 장비 설정", command=lambda: self.show_right("equip"), bg="#2f3d4a", fg="#e0e0e0",
                  activebackground="#3c4d5c", **mk).pack(side="left", padx=3)
        mb = tk.Menubutton(bar, text="설정 저장/불러오기 ▾", bg="#2f3d4a", fg="#e0e0e0", activebackground="#3c4d5c",
                           activeforeground="white", relief="flat", padx=10, pady=2, font=("맑은 고딕", 9))
        m = tk.Menu(mb, tearoff=0)
        m.add_command(label="설정 저장 (파일로)...", command=self.export_settings)
        m.add_command(label="설정 불러오기...", command=self.import_settings)
        m.add_separator()
        m.add_command(label="케이블 손실 표 불러오기 (txt)...", command=self.loss_load)
        m.add_command(label="케이블 손실 표 내보내기 (txt)...", command=self.loss_save)
        mb.configure(menu=m)
        mb.pack(side="left", padx=3)
        tf = tk.Frame(bar, bg="#455a64")
        tf.pack(side="left", padx=(10, 3))
        self.theme_btns = {}
        for key, txt in (("light", "☀ 화이트"), ("dark", "🌙 블랙")):
            b = tk.Button(tf, text=txt, command=lambda k=key: self.set_theme(k), relief="flat", bd=0, padx=10, pady=1,
                          font=("맑은 고딕", 9), bg="#263340", fg="white", activebackground="#1976d2",
                          activeforeground="white")
            b.pack(side="left", padx=1, pady=1)
            self.theme_btns[key] = b
        tk.Button(bar, text="■ 긴급 정지 (Esc)", command=self.emergency_stop, bg="#c62828", fg="white",
                  activebackground="#e53935", activeforeground="white", relief="flat", bd=0, padx=16, pady=3,
                  font=("맑은 고딕", 10, "bold")).pack(side="right", padx=10, pady=5)

    # ------------------------------------------------ 큰 숫자 4칸 (SG · SA · Id · Gain·PAE)
    def build_dashboard(self):
        d = tk.Frame(self.root, bg="#141b22")
        d.pack(side="top", fill="x")
        self.dash = d
        cols = tk.Frame(d, bg="#141b22")
        cols.pack(side="top", fill="x", padx=8, pady=6)
        self.big = {}
        self.big_title = {}
        spec = [("smw", "SG · SMW200A", C_SMW), ("fsw", "SA · FSW", C_FSW),
                ("cur", "Id · RTO (Pulse)", C_CUR)]
        for i, (key, title, color) in enumerate(spec):
            cols.columnconfigure(i, weight=10, uniform="c")
            box = tk.Frame(cols, bg=BG, highlightthickness=1, highlightbackground="#2b3540")
            box.grid(row=0, column=i, sticky="nsew", padx=4)
            self.__dict__.setdefault("tile_boxes", []).append(box)
            tl = tk.Label(box, text=title, bg=BG, fg="#b0bec5", font=("맑은 고딕", 12, "bold"))
            tl.pack(anchor="w", padx=12, pady=(5, 0))
            self.big_title[key] = tl
            v = tk.Label(box, text="---", bg=BG, fg=color, font=("Consolas", 40, "bold"), anchor="w", width=1)
            v.pack(fill="x", padx=10)
            sub = tk.Label(box, text="", bg=BG, fg="#90a4ae", font=("Consolas", 11), anchor="w")
            sub.pack(fill="x", padx=12, pady=(0, 6))
            self.big[key] = (v, sub)
            if key == "smw":                         # SG 칸 오른쪽 위: RF ON / OFF 토글 (어느 화면에서든)
                self.btn_rf_top = tk.Button(box, text="RF OFF", command=self.toggle_rf, bg="#5a5a5a", fg="white",
                                            activebackground="#6a6a6a", activeforeground="white", relief="flat", bd=0,
                                            font=("맑은 고딕", 10, "bold"), padx=10, pady=2, cursor="hand2")
                self.btn_rf_top.place(relx=1.0, x=-8, y=6, anchor="ne")
        # 맨 오른쪽: ⚡ 바이어스 미세 조정 (측정 중에도 항상 보이게 여기 고정)
        cols.columnconfigure(3, weight=18, uniform="c")
        bbox = tk.Frame(cols, bg=BG, highlightthickness=1, highlightbackground="#2b3540")
        bbox.grid(row=0, column=3, sticky="nsew", padx=4)
        self.build_bias_tuner(bbox)
        # DPD 화면의 큰 숫자 3칸 (DPD 화면일 때만 Bench 3칸 자리에)
        self.dpd_tiles = tk.Frame(cols, bg="#141b22")
        for i in range(3):
            self.dpd_tiles.columnconfigure(i, weight=10, uniform="d")
        self.dpd_tiles.rowconfigure(0, weight=1)
        self.dash_cols = cols
        self.build_progress_strip(d)
        d.bind("<Configure>", self.on_dash_resize)

    # ------------------------------------------------ 측정 종류 탭 (Pulse · Mod. · DPD)
    VIEW_COL = {"pulse": "#ff9800", "mod": "#26a69a", "dpd": "#1e88e5", "equip": "#607d8b", "psu": "#8d6e63"}
    AUX_VIEWS = ("equip", "psu")        # 측정 종류가 아닌 공통 화면 (장비 · 파워서플라이)

    def build_modetabs(self):
        bar = tk.Frame(self.root, bg="#141b22")
        bar.pack(side="top", fill="x")
        self.view_btns = {}
        # 측정 3개 (같은 크기) | 간격 | 장비 · 파워서플라이 (조금 작게, 모아 둠)
        tabs = (("pulse", "Pulse 측정", 0), ("mod", "Mod. 측정", 0), ("dpd", "DPD 측정", 0),
                ("equip", "🔧 장비", 1), ("psu", "⚡ 파워서플라이", 1))
        for i, (key, txt, small) in enumerate(tabs):
            b = tk.Label(bar, text=txt, font=("맑은 고딕", 10 if small else 12, "bold"), padx=14 if small else 26,
                         pady=3 if small else 5, cursor="hand2",
                         highlightthickness=1, highlightbackground=self.VIEW_COL[key])
            padl = 12 if i == 0 else (36 if key == "equip" else 2)
            b.pack(side="left", padx=(padl, 0), pady=(6, 2), anchor="s" if small else "center")
            b.bind("<Button-1>", lambda e, k=key: self.set_view(k))
            self.view_btns[key] = b
        tk.Label(bar, text="측정 중에는 바꿀 수 없음 · 측정 종류마다 조건 · 그래프 · 기록 따로 (샘플 종류별 기억)", bg="#141b22",
                 fg="#607d8b", font=("맑은 고딕", 8)).pack(side="left", padx=14, pady=(6, 2))
        self.lbl_view_info = tk.Label(bar, text="", bg="#141b22", fg="#cfd8dc", font=("맑은 고딕", 10))
        self.lbl_view_info.pack(side="right", padx=14, pady=(6, 2))
        self.paint_view_tabs()

    def paint_view_tabs(self):
        v = getattr(self, "view", "pulse")
        for k, b in getattr(self, "view_btns", {}).items():
            on = k == v
            b.configure(bg=self.VIEW_COL[k] if on else "#1f2b36", fg="white" if on else "#90a4ae")

    def meas_view(self):
        """지금 보고 있는 측정 종류 (장비 탭이면 그 전에 보던 측정)"""
        v = getattr(self, "view", "pulse")
        return getattr(self, "equip_prev", "pulse") if v in self.AUX_VIEWS else v

    def dpd_running(self):
        return bool(self.dpd is not None and self.dpd.worker)

    def dpd_stop_only(self):
        """DPD 측정만 바로 멈춤: 장비 통신 끊고 SG RF OFF · FSW 원래 화면. 바이어스 · 파워서플라이 · 자동 측정은 건드리지 않음"""
        if not self.dpd_running():
            return
        try:
            self.dpd.stop()
        except Exception as e:
            self.status("DPD 중지 실패: %s" % e, "#ff6b6b")
            return
        self.status("■ DPD 측정 중지 (SG RF OFF · 바이어스 그대로)", "#d4a017")
        try:
            self.update_progress_strip()
        except Exception:
            pass

    def set_view(self, key):
        """탭: Pulse / Mod. = 지금까지 화면 (self.mode 전환), DPD = DPD 측정 화면 (큰 숫자 3칸 · 아래 화면 통째로),
        장비 = SMW · FSW · RTO · 전원 연결 (모든 측정 공통, 측정 중에도 볼 수 있음 · 큰 숫자는 보던 측정 것 그대로)"""
        cur = self.view
        if key == cur:
            return
        back = getattr(self, "equip_prev", "pulse") if cur in self.AUX_VIEWS else cur
        busy = self.dpd_running() or self.sweep or self.ramp
        if busy and key not in self.AUX_VIEWS and key != back:
            messagebox.showwarning(APP_NAME, "DPD 측정 중입니다. 끝나거나 중지 (Esc) 한 뒤 바꾸세요." if self.dpd_running() else
                                   "자동 측정 / 천천히 내리기 중에는 측정 종류를 바꿀 수 없습니다.")
            return
        if key == "dpd" and not self.ensure_dpd():
            return
        had_keep = False
        if key in ("pulse", "mod"):
            had_keep = os.path.exists(os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "PADPDTester",
                                                   "fsw_acp_keep.json"))
            if self.fsw.get("conn") and self.var_fsw_state_auto.get() and (self.s.get("fsw_state_saved") or {}).get(key):
                # 저장해 둔 그 모드 화면을 통째로 불러올 거라 DPD 쪽 '원래대로 돌리기' 는 안 함 (같이 돌면 불러온 화면을 덮어씀)
                try:
                    os.remove(os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "PADPDTester", "fsw_acp_keep.json"))
                except OSError:
                    pass
            else:
                self.dpd_fsw_back()                      # DPD 가 신호 가운데로 둔 FSW Mod. 화면 → 원래 (Bench 측정 설정)
        # 지금 화면 내리기
        aux_host = {"equip": self.equip_host, "psu": self.psu_host}
        if cur in aux_host:
            aux_host[cur].pack_forget()
        elif cur == "dpd":
            self.dpd_main.pack_forget()
        else:
            self.hpan.pack_forget()
        if key in aux_host:
            self.equip_prev = back
            aux_host[key].pack(side="top", fill="both", expand=True, padx=8, pady=8)
            self.view = key
            if key == "psu":
                self.root.after(80, self.fit_psu_sash)
        else:
            dpd_tiles_on = bool(self.dpd_tiles.winfo_ismapped()) if hasattr(self, "dpd_tiles") else False
            if key == "dpd":
                if not dpd_tiles_on:
                    for b in self.tile_boxes:
                        b.grid_remove()
                    self.dpd_tiles.grid(row=0, column=0, columnspan=3, sticky="nsew")
                self.dpd_main.pack(side="top", fill="both", expand=True, padx=8, pady=8)
                self.view = "dpd"
                self.dpd.follow_sample(self.cur_stype())
                done_ = False
                try:
                    done_ = self.dpd.fsw_dfl_auto("DPD 화면")       # 최근 [설정 불러오기…] 한 FSW 설정 (.dfl) 이 있으면 그것으로
                except Exception:
                    done_ = False
                if not done_:
                    self.fsw_dpd_auto()                   # 없으면: 저장해 둔 이 샘플의 DPD FSW 화면
            else:
                if dpd_tiles_on:
                    self.dpd_tiles.grid_remove()
                    for b in self.tile_boxes:
                        b.grid()
                self.hpan.pack(side="top", fill="both", expand=True, padx=8, pady=8)
                self.view = key
                if key != self.mode:
                    self.set_mode(key)
                    if self.mode != key:              # 바꾸지 못함 (측정 중 등)
                        self.view = self.mode
                elif (cur == "dpd" or had_keep) and self.fsw.get("conn") and self.var_fsw_state_auto.get() \
                        and (self.s.get("fsw_state_saved") or {}).get(key):
                    self.fsw_state(key, "load", auto=True)  # DPD → 같은 Pulse / Mod. 로 돌아옴: 그 모드 화면
            self._big_grow = True
            self.root.after_idle(self.fit_big)
        self.s["view"] = self.view
        self.paint_view_tabs()
        self.update_progress_strip()

    def ensure_dpd(self):
        """DPD 측정 화면은 처음 누를 때 만듦 (numpy · DPD 계산 모듈)"""
        if self.dpd is not None:
            return True
        try:
            import pa_dpd_tester as dpdui
        except Exception as e:
            messagebox.showerror(APP_NAME, "DPD 측정 화면을 열지 못했습니다:\n%s" % e)
            return False
        self.dpd_main = ttk.Frame(self.root)
        try:
            self.dpd = dpdui.App(self.root, host=self, dash_parent=self.dpd_tiles, main_parent=self.dpd_main)
        except Exception as e:
            import traceback
            traceback.print_exc()
            messagebox.showerror(APP_NAME, "DPD 측정 화면을 만들지 못했습니다:\n%s" % e)
            self.dpd = None
            return False
        self.dpd_mod = dpdui
        return True

    # ------------------------------------------------ 큰 숫자 아래 진행 줄 (세 측정 종류 공통)
    def build_progress_strip(self, parent):
        BGs = "#0b1117"
        st = tk.Frame(parent, bg=BGs, highlightthickness=1, highlightbackground="#2b3540")
        st.pack(side="top", fill="x", padx=12, pady=(0, 6))
        self.ps_state = tk.Label(st, text="● 대기", bg="#37474f", fg="white", font=("맑은 고딕", 10, "bold"), padx=8)
        self.ps_state.pack(side="left", padx=(6, 8), pady=4)
        self.ps_bar = tk.Canvas(st, width=200, height=8, bg="#263340", highlightthickness=0, bd=0)
        self.ps_bar.pack(side="left")
        self.ps_left = tk.Label(st, text="", bg=BGs, fg="#ffd54f", font=("Consolas", 13, "bold"), width=6, anchor="w")
        self.ps_left.pack(side="left", padx=(8, 0))
        # DPD 측정만 멈추는 버튼 (바이어스 · 전원은 그대로, SG RF 만 끔) - 오른쪽 위 [긴급 정지] 와 따로
        self.ps_stop = tk.Button(st, text="■ DPD 측정 중지", command=self.dpd_stop_only, bg="#c62828", fg="white",
                                 activebackground="#b71c1c", activeforeground="white", relief="flat", bd=0,
                                 font=("맑은 고딕", 10, "bold"), padx=12, cursor="hand2")
        self._ps_stop_on = False
        self.ps_items = tk.Frame(st, bg=BGs)
        self.ps_items.pack(side="left", fill="x", expand=True)
        self.ps_labels = []
        self._ps_sw = None
        self.root.after(500, self.tick_progress_strip)

    def tick_progress_strip(self):
        try:
            self.update_progress_strip()
        except Exception:
            pass
        self.root.after(400, self.tick_progress_strip)

    def bench_status_info(self):
        sw = self.sweep
        mod = self.mode == "mod"
        if sw:
            if self._ps_sw is not sw:
                self._ps_sw, self._ps_t0 = sw, time.time()
            nl = max(len(sw.get("levels") or [1]), 1)
            fl = sw.get("freqs") or [None]
            nf = max(len(fl), 1)
            fi, li = min(sw.get("fi", 0), nf - 1), min(sw.get("i", 0), nl - 1)
            frac = (fi * nl + li) / float(nf * nl)
            el = time.time() - self._ps_t0
            left = el / frac * (1 - frac) if frac > 0.03 and el > 5 else None
            lt = ("%d:%02d" % (left // 60, left % 60)) if left is not None else ""
            f_now = fl[fi] if fl[fi] is not None else (self.smw.get("f") or 0) / 1e9
            items = [("주파수", ("%d / %d · %.4g GHz" % (fi + 1, nf, f_now)) if f_now else "-"),
                     ("레벨", "%d / %d" % (li + 1, nl)),
                     ("SG", ("%.2f dBm" % self.smw["p"]) if isinstance(self.smw.get("p"), float) and math.isfinite(self.smw["p"]) else "-")]
            if mod and sw.get("target") is not None:
                items.append(("목표 Pout", "%.1f dBm (+%.1f)" % (sw["target"], sw.get("margin", 0.3))))
            items.append(("점", str(len(self.rows))))
            return ("● %s 자동 측정 중" % ("Mod." if mod else "Pulse"), "#2e7d32", frac, lt, items)
        self._ps_sw = None
        if self.ramp:
            return ("● 천천히 내리는 중", "#ef6c00", None, "", [("SG", ("%.2f dBm" % self.smw["p"]) if isinstance(self.smw.get("p"), float) else "-")])
        return ("● 대기", "#37474f", None, "", [("측정", "Mod." if mod else "Pulse"), ("점", str(len(self.rows))),
                                               ("SG", ("%.2f dBm" % self.smw["p"]) if isinstance(self.smw.get("p"), float) and math.isfinite(self.smw["p"]) else "-")])

    def update_progress_strip(self):
        if not hasattr(self, "ps_state"):
            return
        if self.meas_view() == "dpd" and self.dpd is not None:
            st, col, frac, lt, items = self.dpd.status_info()
            if self.cur_stype() and not self.dpd.worker:
                self.dpd.follow_sample(self.cur_stype())     # 샘플 종류 · 번호가 바뀌면 그 샘플 설정 · 기록으로
        else:
            st, col, frac, lt, items = self.bench_status_info()
        self.ps_state.configure(text=st, bg=col)
        self.ps_left.configure(text=lt)
        want = self.meas_view() == "dpd" and self.dpd_running()
        if want != self._ps_stop_on:                       # DPD 측정 중일 때만 보임
            self._ps_stop_on = want
            if want:
                self.ps_stop.pack(side="right", padx=6, pady=3)
            else:
                self.ps_stop.pack_forget()
        cv = self.ps_bar
        cv.delete("all")
        if frac is not None:
            cv.create_rectangle(0, 0, 200 * frac, 8, fill="#ffb300" if col == "#2e7d32" else "#43a047", outline="")
        while len(self.ps_labels) < len(items):
            a = tk.Label(self.ps_items, bg="#0b1117", fg="#78909c", font=("맑은 고딕", 9))
            b = tk.Label(self.ps_items, bg="#0b1117", fg="#cfd8dc", font=("Consolas", 10))
            a.pack(side="left", padx=(14, 4))
            b.pack(side="left")
            self.ps_labels.append((a, b))
        for i, (a, b) in enumerate(self.ps_labels):
            if i < len(items):
                a.configure(text=items[i][0])
                b.configure(text=items[i][1])
            else:
                a.configure(text="")
                b.configure(text="")
        # 탭 오른쪽: 지금 샘플 · 주파수
        st_ = self.cur_stype()
        f = self.smw.get("f")
        info = "샘플 %s" % (self.cur_sample() or "-")
        if self.meas_view() == "dpd" and self.dpd is not None:
            d_ = self.dpd.v
            info += " · %s MHz DPD · %s · %s GHz" % (d_["bw_mode"].get(), "NR 1C" if d_["sig_src"].get() == "nr" else
                                                    (os.path.basename(d_["wv_path"].get()) or "내 .wv"), d_["freq_ghz"].get())
        elif isinstance(f, float) and math.isfinite(f):
            info += " · %s · %.4g GHz" % ("Mod." if self.mode == "mod" else "Pulse", f / 1e9)
        self.lbl_view_info.configure(text=info)

    def on_dash_resize(self, event=None):
        w = max(self.dash.winfo_width(), 400)
        size = int(max(20, min(56, w / 4.5 / 10.5 * 1.45)))
        self._big_base = size
        self._big_fit = {}
        self._big_grow = True                        # 창 크기가 바뀌면 다시 키울 수 있음
        self.root.after_idle(self.fit_big)

    def fit_big(self):
        """큰 숫자: 모든 칸이 같은 글자 크기 (가장 긴 값이 칸에 들어가는 크기) - 값이 바뀌어도 크기 · 칸 높이가 흔들리지 않게
        작아지는 건 바로, 커지는 건 창 크기를 바꿀 때만 (전류가 흔들려 자릿수가 바뀌어도 위아래로 들썩이지 않음)"""
        base = getattr(self, "_big_base", 40)
        fonts = self.__dict__.setdefault("_big_fonts", {})
        labels = [vv[0] for vv in self.big.values()]
        if self.meas_view() == "dpd" and getattr(self, "dpd", None) is not None:
            labels = [vv[0] for vv in self.dpd.big.values()]
        need = base
        for v in labels:
            try:
                avail = v.winfo_width() - 6
                txt = v.cget("text")
            except tk.TclError:
                continue
            if avail < 40:
                continue
            size = need
            while size > 12:
                f = fonts.get(size)
                if f is None:
                    f = fonts[size] = tkfont.Font(family="Consolas", size=size, weight="bold")
                if f.measure(txt) <= avail:
                    break
                size -= 1
            need = min(need, size)
        cur = getattr(self, "_big_size", None)
        if cur is None or need < cur or getattr(self, "_big_grow", False):
            self._big_size = need
        self._big_grow = False
        size = self._big_size
        fnt = ("Consolas", size, "bold")
        allv = [vv[0] for vv in self.big.values()] + ([vv[0] for vv in self.dpd.big.values()]
                                                      if getattr(self, "dpd", None) is not None else [])
        for v in allv:
            try:
                if getattr(v, "_sz", None) != size:
                    v.configure(font=fnt)
                    v._sz = size
            except tk.TclError:
                pass

    # ------------------------------------------------ 맨 아래 상태 줄
    def build_statusbar(self):
        SB = "#1c2530"
        bar = tk.Frame(self.root, bg=SB)
        bar.pack(side="bottom", fill="x")
        self.lbl_rec = tk.Label(bar, text="", bg=SB, fg="#ffd54f", font=("맑은 고딕", 9))
        self.lbl_rec.pack(side="left", padx=10, pady=3)
        self.lbl_lossbar = tk.Label(bar, text="", bg=SB, fg="#9fc5e8", font=("맑은 고딕", 9))
        self.lbl_lossbar.pack(side="left", padx=(12, 0))
        tk.Label(bar, text="단축키: [4] 올리기 · [5] 기록 · [6] 내리기 · Esc 긴급 정지", bg=SB, fg="#78909c",
                 font=("맑은 고딕", 8)).pack(side="right", padx=10)
        self.lbl_status = tk.Label(bar, text="", bg=SB, fg=FG_DIM, anchor="e", font=("맑은 고딕", 9))
        self.lbl_status.pack(side="right", fill="x", expand=True, padx=6)

    # ------------------------------------------------ 카드 (번호 · 제목 · 내용)
    def card(self, parent, title, num=None, right_text=None):
        outer = tk.Frame(parent, highlightthickness=1, bd=0)
        self.reg(outer, bg="panel", highlightbackground="border", highlightcolor="border")
        hd = ttk.Frame(outer, style="Card.TFrame")
        hd.pack(fill="x", padx=10, pady=(7, 5))
        if num is not None:
            ttk.Label(hd, text=str(num), style="Num.TLabel").pack(side="left", padx=(0, 7))
        lab = ttk.Label(hd, text=title, style="Head.TLabel")
        lab.pack(side="left")
        rt = None
        if right_text is not None:
            rt = ttk.Label(hd, text=right_text, style="Hint.TLabel")
            rt.pack(side="right")
        sep = tk.Frame(outer, height=1)
        self.reg(sep, bg="line")
        sep.pack(fill="x")
        body = ttk.Frame(outer, style="Card.TFrame", padding=(10, 6, 10, 8))
        body.pack(fill="both", expand=True)
        outer.head_label, outer.right_label, outer.body = lab, rt, body
        return outer

    def card_scroll(self, card, key):
        """카드 내용을 스크롤 틀에 넣고 아래 경계(═)를 끌어 높이 조절 → 내용을 넣을 틀을 돌려줌"""
        sf = self.scrollframe(card.body, horizontal=False)
        sf.pack(fill="both", expand=True)
        self.add_grip(card, sf, key)
        return sf.body

    def add_grip(self, card, sf, key):
        """카드 아래 경계: 위아래로 끌면 이 칸 높이 (넘치는 내용은 칸 안에서 스크롤), 더블클릭 = 내용 높이로"""
        g = tk.Frame(card, height=8, cursor="sb_v_double_arrow")
        self.reg(g, bg="panel")
        ln = tk.Frame(g, height=2, width=40, cursor="sb_v_double_arrow")
        self.reg(ln, bg="border")
        ln.place(relx=0.5, rely=0.5, anchor="center")
        g.pack(side="bottom", fill="x", before=card.body)
        card.grip, card.sf, card.gkey = g, sf, key
        hs = self.s.setdefault("card_h", {})
        if hs.get(key):
            sf.max_h = int(hs[key])
        st = {}

        def press(e):
            st["y"], st["h"] = e.y_root, sf.cv.winfo_height()

        def drag(e):
            if "y" not in st:
                return
            h = max(40, st["h"] + e.y_root - st["y"])
            nat = sf.natural_h()
            sf.max_h = None if h >= nat else h
            hs[key] = sf.max_h
            sf._sync()
            self.sf_left._sync()

        def reset(e=None):
            hs.pop(key, None)
            sf.max_h = None
            if key == "c2":
                self.fit_ctrl_height()
            sf._sync()
            self.sf_left._sync()
        for w in (g, ln):
            w.bind("<ButtonPress-1>", press)
            w.bind("<B1-Motion>", drag)
            w.bind("<ButtonRelease-1>", lambda e: st.clear())
            w.bind("<Double-Button-1>", reset)

    def form(self, parent):
        """이름(오른쪽 맞춤) | 값(같은 폭) | 단위 - 한 줄에 두 쌍"""
        g = ttk.Frame(parent, style="Card.TFrame")
        for c, (w, mn) in enumerate(((0, 88), (0, 72), (0, 34), (0, 88), (0, 72), (0, 34))):
            g.columnconfigure(c, minsize=mn, weight=0)
        g._row = 0
        g._col = 0
        return g

    def fpair(self, g, label, var, unit="", width=8, col=None, entry_kw=None, newrow=False):
        """form 에 '이름 [값] 단위' 한 쌍"""
        if newrow and g._col:
            g._row += 1
            g._col = 0
        c = g._col if col is None else col
        r = g._row
        ttk.Label(g, text=label, style="Lab.TLabel", anchor="e").grid(row=r, column=c * 3, sticky="e", padx=(0, 6), pady=3)
        e = ttk.Entry(g, textvariable=var, width=width, justify="right", **(entry_kw or {}))
        e.grid(row=r, column=c * 3 + 1, sticky="we", pady=3)
        ttk.Label(g, text=unit, style="Unit.TLabel").grid(row=r, column=c * 3 + 2, sticky="w", padx=(4, 0))
        if c == 0:
            g._col = 1
        else:
            g._col = 0
            g._row += 1
        return e

    def fold(self, parent, title, key, summary=None, default=False, on_toggle=None):
        """▶ 눌러서 펼치기 / ▼ 접기"""
        fr = ttk.Frame(parent, style="Card.TFrame")
        hd = ttk.Label(fr, text="", style="Fold.TLabel", cursor="hand2")
        hd.pack(fill="x", pady=(4, 2))
        inner = ttk.Frame(fr, style="Card.TFrame", padding=(10, 2, 0, 4))
        state = {"open": bool(self.s.get("fold_" + key, default))}

        def apply():
            sm = summary() if callable(summary) else (summary or "")
            if state["open"]:
                inner.pack(fill="x")
                hd.configure(text="▼ %s" % title)
            else:
                inner.pack_forget()
                hd.configure(text="▶ %s%s" % (title, ("   " + sm) if sm else ""))

        def toggle(e=None):
            state["open"] = not state["open"]
            self.s["fold_" + key] = state["open"]
            apply()
            if on_toggle:
                on_toggle()
            self.root.after(30, self.fit_ctrl_height)

        hd.bind("<Button-1>", toggle)
        fr.refresh = apply
        apply()
        self.__dict__.setdefault("folds", {})[key] = fr
        return fr, inner

    # ------------------------------------------------ 본 화면: 왼쪽 ① ② ③ ⚡ | 오른쪽 탭 (그래프 · 파워서플라이 · 장비)
    def build_main(self):
        s = self.s
        hpan = ttk.PanedWindow(self.root, orient="horizontal")
        hpan.pack(side="top", fill="both", expand=True, padx=8, pady=8)
        self.hpan = hpan
        # 왼쪽: 창이 작으면 왼쪽 칸 전체가 스크롤 (② 측정 조건은 먼저 줄어들고 그래도 모자라면)
        self.sf_left = self.scrollframe(hpan, bgkey="bg", horizontal=False)
        left = self.sf_left.body
        right = ttk.Frame(hpan)
        hpan.add(self.sf_left, weight=0)
        hpan.add(right, weight=1)
        self.leftcol = left
        self.build_left(left)

        def on_left_size(e=None):
            if getattr(self, "_fit_job", None):
                self.root.after_cancel(self._fit_job)
            self._fit_job = self.root.after(120, self.fit_ctrl_height)
        self.sf_left.cv.bind("<Configure>", lambda e: (self.sf_left._sync(), on_left_size()), add="+")
        self.build_right(right)

    def build_left(self, left):
        s = self.s
        # ① 샘플 · 신호
        c1 = self.card(left, "샘플 · 신호", 1, right_text="")
        c1.pack(fill="x", pady=(0, 8))
        self.lbl_stype = c1.right_label
        b = self.card_scroll(c1, "c1")
        g = self.form(b)
        g.pack(fill="x")
        ttk.Label(g, text="측정 모드", style="Lab.TLabel").grid(row=0, column=0, sticky="e", padx=(0, 6), pady=3)
        seg = ttk.Frame(g, style="Card.TFrame")
        seg.grid(row=0, column=1, columnspan=2, sticky="w")
        self.var_mode = tk.StringVar(value=self.mode)
        for val, txt in (("pulse", "Pulse"), ("mod", "Mod.")):
            ttk.Radiobutton(seg, text=txt, value=val, variable=self.var_mode, style="Seg.Toolbutton",
                            command=lambda: self.set_mode(self.var_mode.get())).pack(side="left")
        ttk.Button(seg, text="📷 FSW", style="Small.TButton", width=6,
                   command=lambda: self.fsw_state(self.mode, "save")).pack(side="left", padx=(4, 0))
        # 샘플 종류 / 번호
        self.sample_types = [dict(t) for t in s.get("sample_types", []) if t.get("name")]
        ttk.Label(g, text="샘플 종류", style="Lab.TLabel").grid(row=0, column=3, sticky="e", padx=(0, 6))
        stf = ttk.Frame(g, style="Card.TFrame")
        stf.grid(row=0, column=4, columnspan=2, sticky="we")
        self.var_stype = tk.StringVar(value=s.get("stype", NO_TYPE))
        self.cb_stype = ttk.Combobox(stf, textvariable=self.var_stype, state="readonly", width=10)
        self.cb_stype.pack(side="left", fill="x", expand=True)
        self.cb_stype.bind("<<ComboboxSelected>>", lambda e: self.on_stype_select())
        self.prev_stype = self.var_stype.get()
        if hasattr(self, "cb_stype_top"):              # 맨 위 [샘플] 칸도 같이
            self.var_stype.trace_add("write", lambda *a: self.cb_stype_top.set(self.var_stype.get()))
            self.cb_stype_top.set(self.var_stype.get())
        ttk.Button(stf, text="설정", style="Small.TButton", width=4, command=self.edit_sample_types).pack(side="left", padx=(3, 0))
        ttk.Label(g, text="샘플 번호", style="Lab.TLabel").grid(row=1, column=0, sticky="e", padx=(0, 6), pady=3)
        snf = ttk.Frame(g, style="Card.TFrame")
        snf.grid(row=1, column=1, columnspan=2, sticky="we")
        self.var_sample = tk.StringVar(value=s.get("sample", ""))
        ttk.Entry(snf, textvariable=self.var_sample, width=9, font=("맑은 고딕", 10, "bold")).pack(side="left", fill="x", expand=True)
        ttk.Button(snf, text="반영", style="Small.TButton", width=4, command=self.apply_sample_to_rows).pack(side="left", padx=(3, 0))
        self.var_sample.trace_add("write", lambda *a: self.on_sample_change())
        ttk.Label(g, text="주파수", style="Lab.TLabel").grid(row=1, column=3, sticky="e", padx=(0, 6))
        frf = ttk.Frame(g, style="Card.TFrame")
        frf.grid(row=1, column=4, columnspan=2, sticky="we")
        self.var_freq = tk.StringVar(value=s.get("freq_ghz", ""))
        e = ttk.Entry(frf, textvariable=self.var_freq, width=7, justify="right")
        e.pack(side="left", fill="x", expand=True)
        e.bind("<Return>", lambda ev: self.apply_freq())
        ttk.Label(frf, text="GHz", style="Unit.TLabel").pack(side="left", padx=(3, 3))
        ttk.Button(frf, text="SG+SA", style="Small.TButton", width=6, command=self.apply_freq).pack(side="left")
        # SG 신호 파일 (한 줄)
        self.wv_paths = {"pulse": s.get("wv_pulse") or DEFAULT_WV["pulse"], "mod": s.get("wv_mod") or DEFAULT_WV["mod"]}
        ttk.Label(g, text="SG 신호", style="Lab.TLabel").grid(row=2, column=0, sticky="e", padx=(0, 6), pady=3)
        wvf = ttk.Frame(g, style="Card.TFrame")
        wvf.grid(row=2, column=1, columnspan=5, sticky="we")
        self.var_wv = tk.StringVar(value=self.wv_paths[self.mode])
        e = ttk.Entry(wvf, textvariable=self.var_wv, width=30)
        e.pack(side="left", fill="x", expand=True)
        e.bind("<Return>", lambda ev: self.apply_waveform())
        self.var_wv.trace_add("write", lambda *a: self.wv_paths.__setitem__(self.mode, self.var_wv.get().strip()))
        ttk.Button(wvf, text="적용", style="Small.TButton", width=4, command=self.apply_waveform).pack(side="left", padx=(3, 0))
        ttk.Button(wvf, text="기본", style="Small.TButton", width=4,
                   command=lambda: self.var_wv.set(DEFAULT_WV[self.mode])).pack(side="left", padx=(2, 0))
        self.lbl_wv = ttk.Label(g, text="SG 연결 후 표시", style="Hint.TLabel")
        self.lbl_wv.grid(row=3, column=1, columnspan=5, sticky="w")
        self.smw_wv = None          # 장비에서 읽은 지금 신호 파일
        # 케이블 손실 (요약 + 편집 창)
        ttk.Label(g, text="케이블 손실", style="Lab.TLabel").grid(row=4, column=0, sticky="e", padx=(0, 6), pady=3)
        lsf = ttk.Frame(g, style="Card.TFrame")
        lsf.grid(row=4, column=1, columnspan=5, sticky="we")
        self.lbl_loss = ttk.Label(lsf, text="", style="Card.TLabel")
        self.lbl_loss.pack(side="left", fill="x", expand=True)
        ttk.Button(lsf, text="편집", style="Small.TButton", width=4, command=self.open_loss_editor).pack(side="left")
        self.lbl_sample = ttk.Label(g, text="", style="Hint.TLabel")
        self.lbl_sample.grid(row=5, column=1, columnspan=5, sticky="w")
        self.build_loss_editor()
        self.fill_stype_combo()

        # 여러 주파수 자동 측정 (항상 보이는 칸 - 머리를 눌러 펼치기 / 접기, 쓰는 사람만 펼쳐서 사용)
        cf = self.card(left, "▶ 여러 주파수 자동 측정", None, right_text="")
        cf.pack(fill="x", pady=(0, 8))
        self.card_fq = cf
        fqb = self.card_scroll(cf, "fq")
        self.fq_frames = {m: ttk.Frame(fqb, style="Card.TFrame") for m in ("pulse", "mod")}
        hd = cf.head_label.master
        self.chk_fq_head = ttk.Checkbutton(hd, text="사용", style="Card.TCheckbutton")
        self.chk_fq_head.pack(side="left", padx=(10, 0), after=cf.head_label)
        self.fq_mini = tk.Canvas(hd, width=110, height=7, highlightthickness=0, bd=0)   # 제목줄 작은 진행 바
        self.reg(self.fq_mini, bg="panel")
        for w in (hd, cf.head_label, cf.right_label):
            w.configure(cursor="hand2")
            w.bind("<Button-1>", lambda e: self.toggle_fq_card())

        # ② 측정 조건 (이 칸 안에서 스크롤)
        c2 = self.card(left, "측정 조건 (Pulse)", 2, right_text="")
        c2.pack(fill="x", pady=(0, 8))
        self.card2 = c2
        self.sf_cond = self.scrollframe(c2.body, horizontal=False)
        self.sf_cond.pack(fill="both", expand=True)
        self.add_grip(c2, self.sf_cond, "c2")
        self.cond_pulse = ttk.Frame(self.sf_cond.body, style="Card.TFrame")
        self.cond_mod = ttk.Frame(self.sf_cond.body, style="Card.TFrame")
        self.build_cond_pulse(self.cond_pulse)
        self.build_cond_mod(self.cond_mod)
        self._cond_loading = False
        self._cond_traced = set()
        for k in self.COND_KEYS:                    # ② 측정 조건: 샘플 종류마다 기억
            if hasattr(self, k):
                getattr(self, k).trace_add("write", lambda *a: self.cond_changed())
                self._cond_traced.add(k)
        self.load_stype_cond()
        for w in (c2.head_label.master, c2.head_label, c2.right_label):     # 머리 누르면 접기 / 펼치기
            w.configure(cursor="hand2")
            w.bind("<Button-1>", lambda e: self.toggle_card2())

        # ③ 실행
        c3 = self.card(left, "실행", 3)
        c3.pack(fill="x", pady=(0, 8))
        self.card3 = c3
        b = self.card_scroll(c3, "c3")
        self.btn_sweep = tk.Button(b, text="● 자동 측정 시작", command=self.toggle_sweep, bg="#2e7d32", fg="white",
                                   activebackground="#388e3c", activeforeground="white", relief="flat", bd=0,
                                   font=("맑은 고딕", 11, "bold"), pady=6)
        self.btn_sweep.pack(fill="x")
        r1 = ttk.Frame(b, style="Card.TFrame")
        r1.pack(fill="x", pady=(6, 0))
        for c in range(3):
            r1.columnconfigure(c, weight=1, uniform="b")
        ttk.Button(r1, text="▲ 올리기 [4]", command=lambda: self.smw_step(+1)).grid(row=0, column=0, sticky="we", padx=(0, 3))
        ttk.Button(r1, text="● 기록 [5]", style="Pri.TButton", command=self.record_request).grid(row=0, column=1, sticky="we", padx=3)
        ttk.Button(r1, text="▼ 내리기 [6]", command=lambda: self.smw_step(-1)).grid(row=0, column=2, sticky="we", padx=(3, 0))
        self.btn_ramp = ttk.Button(r1, text="▼▼ 천천히 내리기", command=self.toggle_ramp)
        self.btn_ramp.grid(row=1, column=0, sticky="we", padx=(0, 3), pady=(5, 0))
        self.btn_rf = tk.Button(r1, text="RF", relief="flat", bd=0, font=("맑은 고딕", 9, "bold"), command=self.toggle_rf)
        self.btn_rf.grid(row=1, column=1, sticky="nswe", padx=3, pady=(5, 0))
        ttk.Button(r1, text="SG 레벨 적용", command=lambda: self.smw_set_level(fnum(self.var_lvl2.get()))).grid(
            row=1, column=2, sticky="we", padx=(3, 0), pady=(5, 0))
        rpf = tk.Frame(b, highlightthickness=1, bd=0)
        self.reg(rpf, bg="head", highlightbackground="border")
        rpf.pack(fill="x", pady=(7, 0))
        lb = tk.Label(rpf, text="▼▼ 천천히 내리기 속도", font=("맑은 고딕", 9, "bold"))
        self.reg(lb, bg="head", fg="dim")
        lb.pack(side="left", padx=6, pady=4)
        self.var_ramp_dt = tk.StringVar(value=s.get("ramp_dt", "1.0"))
        self.var_ramp_db = tk.StringVar(value=s.get("ramp_db", s.get("smw_step", "1.0")))
        for var, unit in ((self.var_ramp_dt, "초 간격"), (self.var_ramp_db, "dB 씩")):
            lu = tk.Label(rpf, text=unit, font=("맑은 고딕", 9))
            self.reg(lu, bg="head", fg="unit")
            lu.pack(side="right", padx=(2, 6))
            ttk.Entry(rpf, textvariable=var, width=5, justify="right").pack(side="right")
        ck = ttk.Frame(b, style="Card.TFrame")
        ck.pack(fill="x", pady=(5, 0))
        self.var_sw_ret = tk.BooleanVar(value=bool(s.get("sw_return", True)))
        self.chk_ret = ttk.Checkbutton(ck, text="끝나면 시작 레벨로 천천히 복귀", variable=self.var_sw_ret, style="Card.TCheckbutton")
        self.chk_ret.pack(anchor="w")
        self.var_rf_ramp_off = tk.BooleanVar(value=bool(s.get("rf_ramp_off", True)))
        ttk.Checkbutton(ck, text="천천히 내리기 끝나면 RF OFF", variable=self.var_rf_ramp_off,
                        style="Card.TCheckbutton").pack(anchor="w")
        self.var_auto_bias_off = tk.BooleanVar(value=bool(s.get("auto_bias_off", False)))
        ttk.Checkbutton(ck, text="측정이 끝나면 바이어스도 자동 OFF (SG 내림 → RF OFF → 바이어스 OFF)",
                        variable=self.var_auto_bias_off, style="Card.TCheckbutton").pack(anchor="w")
        # 자동 측정은 항상 RF 를 직접 켬 (RF OFF 로 둬도 시작 SG 레벨에서 RF ON) - 예전 옵션은 없앰
        self.var_rf_sweep_on = tk.BooleanVar(value=True)
        self.lbl_rf = ttk.Label(ck, text="", style="Hint.TLabel")
        self.lbl_rf.pack(anchor="w")
        self.rf_cmd = None
        # 예전 버튼 이름 (다른 곳에서 글자 바꾸기만 함)
        self.btn_rec = tk.Button(self.root)
        self.btn_mod = tk.Button(self.root)

        for k, v in self.fqv[self.mode].items():
            if k in ("on", "how", "start", "stop", "step", "list", "wait", "auto"):
                setattr(self, "var_fq_" + k, v)
        self.lbl_fq = self.fqv[self.mode]["lbl"]

    def build_cond_pulse(self, f):
        s = self.s
        g = self.form(f)
        g.pack(fill="x")
        self.var_lvl2 = tk.StringVar(value=s.get("lvl2") or "-40")
        e = self.fpair(g, "시작 SG 파워", self.var_lvl2, "dBm")
        e.bind("<Return>", lambda ev: self.smw_set_level(fnum(self.var_lvl2.get())))
        self.fpair(g, "SG 최대 허용", self.var_smw_max, "dBm")
        self.fpair(g, "스텝", self.var_smw_step, "dB")
        self.var_sw_idmax = tk.StringVar(value=s.get("sw_idmax", ""))
        self.fpair(g, "Id 상한", self.var_sw_idmax, "mA")
        self.var_settle = tk.StringVar(value=s["settle"])
        self.var_avg = tk.StringVar(value=s["avg"])
        self.fpair(g, "기록 전 대기", self.var_settle, "초")
        self.fpair(g, "평균 시간", self.var_avg, "초")
        self.var_sat_on = tk.BooleanVar(value=bool(s.get("sat_on", True)))
        self.var_sat_db = tk.StringVar(value=s.get("sat_db", "0.1"))
        self.fpair(g, "SA 포화 ≤", self.var_sat_db, "dB")
        self.var_vdd = tk.StringVar(value=s["vdd"])
        self._vdd_entries = [self.fpair(g, "Vdd", self.var_vdd, "V (전원 따라감)")]
        ttk.Label(f, text="기록 전 대기: SG 파워를 바꾼 뒤 기다리는 시간 → 평균 시간 동안 읽은 값을 평균 내서 기록",
                  style="Hint.TLabel").pack(anchor="w", pady=(2, 0))
        self.lbl_vdd = ttk.Label(f, text="", style="Card.TLabel", foreground="#c0504d")
        self.lbl_vdd.pack(anchor="w")
        adv, a = self.fold(f, "고급", "adv_pulse", summary="(튀는 값 재측정 · Id/PAE 허용 오차 · 최소 Id · 반올림 · 손 조작 자동 기록)")
        adv.pack(fill="x")
        ttk.Checkbutton(a, text="SA 증가가 위 값 이하면 포화로 보고 확인", variable=self.var_sat_on,
                        style="Card.TCheckbutton").pack(anchor="w")
        self.var_sw_retry = tk.BooleanVar(value=bool(s.get("sw_retry", True)))
        ttk.Checkbutton(a, text="튀는 값 자동 재측정 (한 단계 아래부터 다시)", variable=self.var_sw_retry,
                        style="Card.TCheckbutton").pack(anchor="w")
        g2 = self.form(a)
        g2.pack(fill="x")
        self.var_tol_id = tk.StringVar(value=s.get("tol_id", "15"))
        self.var_tol_pae = tk.StringVar(value=s.get("tol_pae", "5"))
        self.var_retry_minid = tk.StringVar(value=s.get("retry_minid", s.get("min_id", "250")))
        self.var_minid = tk.StringVar(value=s.get("min_id", "250"))
        self.fpair(g2, "Id 허용 ±", self.var_tol_id, "%")
        self.fpair(g2, "PAE 허용 ±", self.var_tol_pae, "%p")
        self.fpair(g2, "무시 Id <", self.var_retry_minid, "mA")
        self.fpair(g2, "효율 최소 Id", self.var_minid, "mA")
        self.var_gain_drop = tk.StringVar(value=s.get("gain_drop", "3"))
        self.fpair(g2, "Gain 급하락 ≥", self.var_gain_drop, "dB")
        ttk.Label(a, text="Gain 급하락: 직전 점보다 Gain 이 이만큼 떨어지면 더 올리지 않고 확인 (커넥터 빠짐 등 조립 문제) · 빈칸 = 끔",
                  style="Hint.TLabel").pack(anchor="w")
        self.var_autochg = tk.BooleanVar(value=False)
        ttk.Checkbutton(a, text="손으로 SG 파워를 바꿀 때도 자동 기록", variable=self.var_autochg, command=self.toggle_rec,
                        style="Card.TCheckbutton").pack(anchor="w")
        self.var_saq = tk.BooleanVar(value=bool(s.get("sa_q", True)))
        ttk.Checkbutton(a, text="SA 값 소수 둘째 자리에서 반올림 (12.24 → 12.2)", variable=self.var_saq,
                        style="Card.TCheckbutton").pack(anchor="w")
        mmf = ttk.Frame(a, style="Card.TFrame")
        mmf.pack(fill="x", pady=(3, 0))
        ttk.Button(mmf, text="전류 기억 초기화 (샘플 바꿀 때)", style="Small.TButton", command=self.reset_memory).pack(side="left")
        self.lbl_mem = ttk.Label(mmf, text="", style="Info.TLabel")
        self.lbl_mem.pack(side="left", padx=6)
        ttk.Label(a, text="Id 가 최소 Id 미만이면 PAE·DE 빈칸 (Gain 만)\nDUT Pin = SG − In loss,  DUT Pout = SA + Out loss\n"
                          "PAE = (Pout − Pin)[W] / (Vdd·Id) × 100", style="Hint.TLabel", justify="left").pack(anchor="w", pady=(3, 0))
        self.build_fq_box(self.fq_frames["pulse"], "pulse")

    def build_cond_mod(self, f):
        s = self.s
        g = self.form(f)
        g.pack(fill="x")
        self.var_mod_target = tk.StringVar(value=s.get("mod_target", "43"))
        self.var_mod_margin = tk.StringVar(value=s.get("mod_margin", "0.3"))
        self.var_mod_start = tk.StringVar(value=s.get("mod_start", "-40"))
        self.var_mod_step = tk.StringVar(value=s.get("mod_step", "0.5"))
        self.var_mod_max = tk.StringVar(value=s.get("mod_max", "10"))
        self.var_mod_after = tk.StringVar(value=s.get("mod_after", MOD_AFTER[0]))
        self.fpair(g, "목표 Pout", self.var_mod_target, "dBm", entry_kw={"font": ("맑은 고딕", 9, "bold")})
        self.fpair(g, "초과 허용 +", self.var_mod_margin, "dB")
        self.fpair(g, "시작 SG 파워", self.var_mod_start, "dBm")
        self.fpair(g, "스텝", self.var_mod_step, "dB")
        self.fpair(g, "SG 파워 최대", self.var_mod_max, "dBm")
        self._vdd_entries.append(self.fpair(g, "Vdd", self.var_vdd, "V (전원 따라감)"))
        self.fpair(g, "기록 전 대기", self.var_settle, "초")
        self.fpair(g, "평균 시간", self.var_avg, "초")
        ttk.Label(g, text="종료 후", style="Lab.TLabel").grid(row=g._row, column=0, sticky="e", padx=(0, 6), pady=3)
        ttk.Combobox(g, textvariable=self.var_mod_after, values=MOD_AFTER, state="readonly", width=24).grid(
            row=g._row, column=1, columnspan=5, sticky="w")
        # 목표 가까이에서 스텝 줄이기 (샘플 보호)
        fr = ttk.Frame(f, style="Card.TFrame")
        fr.pack(fill="x", pady=(4, 0))
        self.var_mod_fine_on = tk.BooleanVar(value=bool(s.get("mod_fine_on", True)))
        ttk.Checkbutton(fr, text="목표", variable=self.var_mod_fine_on, style="Card.TCheckbutton").pack(side="left")
        self.var_mod_fine_from = tk.StringVar(value=s.get("mod_fine_from", "2"))
        ttk.Entry(fr, textvariable=self.var_mod_fine_from, width=5, justify="right").pack(side="left", padx=2)
        ttk.Label(fr, text="dB 전부터 스텝", style="Card.TLabel").pack(side="left")
        self.var_mod_fine_step = tk.StringVar(value=s.get("mod_fine_step", "0.1"))
        ttk.Entry(fr, textvariable=self.var_mod_fine_step, width=5, justify="right").pack(side="left", padx=2)
        ttk.Label(fr, text="dB 로 줄이기", style="Card.TLabel").pack(side="left")
        ttk.Label(f, text="목표 Pout 에 닿으면 멈춤 · 다음 스텝이 '목표 + 초과 허용' 을 넘을 것 같으면 그 전에 멈춤\n"
                          "(예: 목표 43, 초과 허용 0.3 → 43.3 dBm 까지는 넘어가도 됨) · SG 는 SG 최대를 절대 넘지 않음",
                  style="Hint.TLabel", justify="left").pack(anchor="w", pady=(2, 0))
        ttk.Label(f, text="Mod. Id = E36155A 가 읽은 Vdd 전류 · 효율 = DUT Pout(SA 파워 + Out loss) / (Vdd × Id)",
                  style="Hint.TLabel").pack(anchor="w", pady=(2, 0))
        adv, a = self.fold(f, "고급", "adv_mod", summary="(E36155A 전압 사용 · 최소 Id · 반올림)")
        adv.pack(fill="x")
        # Mod. 측정 중 안내 창은 없앰 (긴급 정지는 Esc · 위쪽 [긴급 정지] 버튼)
        self.var_mod_popup = tk.BooleanVar(value=False)
        self.var_psu_vmeas = tk.BooleanVar(value=bool(s.get("psu_vmeas", False)))
        # (예전 'E36155A 잰 전압 사용' 스위치는 없앰: 이제 효율은 늘 파워서플라이 Vdd - Mod. = 잰 전압, Pulse = 설정 전압)
        g2 = self.form(a)
        g2.pack(fill="x")
        self.fpair(g2, "효율 최소 Id", self.var_minid, "mA")
        self.fpair(g2, "Gain 급하락 ≥", self.var_gain_drop, "dB")
        ttk.Checkbutton(a, text="SA 값 소수 둘째 자리에서 반올림", variable=self.var_saq,
                        style="Card.TCheckbutton").pack(anchor="w")
        self.build_fq_box(self.fq_frames["mod"], "mod")

    def fold_card_body(self, c, op):
        sep = c.winfo_children()[1]
        body, grip = c.body, getattr(c, "grip", None)
        if op:
            if not body.winfo_ismapped():
                if grip is not None:
                    grip.pack(side="bottom", fill="x")
                sep.pack(fill="x")
                body.pack(fill="both", expand=True)
        else:
            body.pack_forget()
            sep.pack_forget()
            if grip is not None:
                grip.pack_forget()

    COND_KEYS = ("var_lvl2", "var_smw_max", "var_smw_step", "var_sw_idmax", "var_settle", "var_avg", "var_sat_db", "var_vdd",
                 "var_tol_id", "var_tol_pae", "var_retry_minid", "var_minid", "var_gain_drop",
                 "var_mod_target", "var_mod_margin", "var_mod_start", "var_mod_step", "var_mod_max", "var_mod_after",
                 "var_mod_fine_on", "var_mod_fine_from", "var_mod_fine_step",
                 "var_sw_ret", "var_rf_ramp_off", "var_auto_bias_off", "var_sat_on", "var_sw_retry")

    # 전류(RTO) 트리거 자동 추적 설정 - 샘플 종류마다 기억
    TRIG_KEYS = ("var_trk", "var_trk_start", "var_trk_ratio", "var_trk_floor", "var_trk_set", "var_trk_mode", "var_asc",
                 "var_trk_lowp", "var_gate", "var_t1", "var_t2", "var_tu")      # + 전류 읽는 구간 (Gate)
    GATE_KEYS = ("var_gate", "var_t1", "var_t2", "var_tu")

    def cond_changed(self):
        """② 측정 조건 · 전류 트리거 설정을 고치면 지금 샘플 종류에 저장 (종류 없음 = 따로)"""
        if self._cond_loading or not hasattr(self, "var_stype"):
            return
        cond = {k[4:]: getattr(self, k).get() for k in self.COND_KEYS if hasattr(self, k)}
        sc = getattr(self, "scope", None)
        trig = {k[4:]: getattr(sc, k).get() for k in self.TRIG_KEYS if hasattr(sc, k)} if sc is not None else {}
        t = self.stype_rec()
        md = "mod" if self.mode == "mod" else "pulse"   # 같은 샘플 종류 안에서도 Pulse · Mod. 따로
        if t is not None:
            t["cond_" + md] = cond
            if trig:
                trig["ratio_v2"] = True             # 레벨 비율 기본 100% 로 바꾼 뒤 저장한 값
                t["trig_" + md] = trig
            self.s["sample_types"] = [dict(x) for x in self.sample_types]
        else:
            self.s["cond_free_" + md] = cond
            if trig:
                self.s["trig_free_" + md] = trig

    def load_stype_cond(self):
        """샘플 종류를 바꾸면 그 종류에 저장된 ② 측정 조건으로 (저장된 것이 없으면 지금 값 그대로 시작)"""
        t = self.stype_rec()
        md = "mod" if self.mode == "mod" else "pulse"
        if t is not None:                           # 이 모드 값 → 없으면 예전 (모드 구분 없던) 값
            cond = t.get("cond_" + md) or t.get("cond") or {}
            trig = t.get("trig_" + md) or t.get("trig") or {}
        else:
            cond = self.s.get("cond_free_" + md) or self.s.get("cond_free") or {}
            trig = self.s.get("trig_free_" + md) or self.s.get("trig_free") or {}
        fresh = not ((t.get("cond_" + md) if t is not None else self.s.get("cond_free_" + md)))
        sc = getattr(self, "scope", None)
        changed_trig = gate_changed = False
        self._cond_loading = True
        try:
            for k in self.COND_KEYS:
                v = cond.get(k[4:])
                if v is not None and hasattr(self, k) and getattr(self, k).get() != v:
                    getattr(self, k).set(v)
            if sc is not None:
                gate0 = tuple(str(getattr(sc, k).get()) for k in self.GATE_KEYS if hasattr(sc, k))
                for k in self.TRIG_KEYS:
                    v = trig.get(k[4:])
                    if k == "var_trk_ratio" and str(v).strip() in ("90", "90.0") and not trig.get("ratio_v2"):
                        v = "100"                   # v2.3.11: 레벨 = 측정값 평균 (예전 기본 90% → 100%)
                    if v is not None and hasattr(sc, k) and getattr(sc, k).get() != v:
                        getattr(sc, k).set(v)
                        changed_trig = True
                gate1 = tuple(str(getattr(sc, k).get()) for k in self.GATE_KEYS if hasattr(sc, k))
                gate_changed = gate1 != gate0
                if gate_changed:
                    try:
                        sc.update_gate_state()
                    except Exception:
                        pass
        finally:
            self._cond_loading = False
        if fresh or not cond or (sc is not None and not trig):
            self.cond_changed()                     # 처음 고른 종류 · 모드: 지금 값을 이 종류 · 모드 값으로
        self.card2_refresh()
        if changed_trig and sc is not None:
            self.scope_reapply(gate_changed=gate_changed)

    def scope_reapply(self, gate_changed=False):
        """샘플 종류 · 모드가 바뀌어 전류 설정(읽는 구간 · 트리거)이 달라졌을 때:
        읽는 중이면 [읽기 중단] → 설정 적용 → [전류 읽기] 다시 (연결은 그대로)"""
        sc = getattr(self, "scope", None)
        if sc is None or not getattr(sc, "connected", False):
            return
        try:
            sc.read_cfg()                           # 구간 값이 잘못됐으면 그대로 둠
        except ValueError as e:
            self.status("전류 읽는 구간 확인: %s" % e, "#d4a017")
            return
        if sc.running:
            sc.on_run()                             # 읽기 중단
            def restart():
                if not sc.running and getattr(sc, "connected", False):
                    sc.reset_stats()
                    sc.on_run()                     # 새 설정 적용 + 다시 읽기 (트리거 · 스케일 추적도 새 설정으로)
                    self.status("전류: 이 샘플 종류의 읽는 구간 · 트리거 설정으로 다시 읽는 중", "#5aa9ff")
            self.root.after(400, restart)
        elif gate_changed:
            try:
                sc.update_info()
                sc.worker.tasks.put(("apply", sc.read_cfg()))   # 읽고 있지 않으면 설정만 적용
            except Exception:
                pass

    def toggle_card2(self):
        self.s["fold_card2"] = not self.s.get("fold_card2", True)
        self.card2_refresh()
        self.root.after(30, self.fit_ctrl_height)

    def card2_refresh(self):
        """② 측정 조건: 접혀 있으면 한 줄 요약만"""
        c2 = getattr(self, "card2", None)
        if c2 is None:
            return
        op = bool(self.s.get("fold_card2", True))
        self.fold_card_body(c2, op)
        mod = self.mode == "mod"
        c2.head_label.configure(text="%s 측정 조건 (%s)" % ("▼" if op else "▶", "Mod." if mod else "Pulse"))
        if op:
            txt = "접기"
        elif mod:
            txt = "목표 Pout %s dBm · 시작 SG 파워 %s · 스텝 %s dB · SG 최대 %s dBm" % (
                self.var_mod_target.get(), self.var_mod_start.get(), self.var_mod_step.get(), self.var_mod_max.get())
        else:
            txt = "SG %s → %s dBm · 스텝 %s dB · 대기 %s / 평균 %s 초" % (
                self.var_lvl2.get(), self.var_smw_max.get(), self.var_smw_step.get(), self.var_settle.get(), self.var_avg.get())
        if c2.right_label.cget("text") != txt:
            c2.right_label.configure(text=txt)

    def toggle_fq_card(self):
        self.s["fold_fqcard"] = not self.s.get("fold_fqcard", False)
        self.fq_card_refresh()
        self.root.after(30, self.fit_ctrl_height)

    def fq_card_refresh(self):
        """여러 주파수 칸: 펼침 상태 · 지금 모드 내용 · 요약 (접혀 있어도 켜져 있는지 보이게)"""
        cf = getattr(self, "card_fq", None)
        if cf is None or not hasattr(self, "fqv"):
            return
        op = bool(self.s.get("fold_fqcard", False))
        self.fold_card_body(cf, op)
        for m, fr in self.fq_frames.items():
            if m == self.mode:
                if not fr.winfo_ismapped():
                    fr.pack(fill="x")
            else:
                fr.pack_forget()
        v = self.fqv.get(self.mode)
        cf.head_label.configure(text="%s 여러 주파수 자동 측정%s" % ("▼" if op else "▶", " (Mod.)" if self.mode == "mod" else ""))
        if v is None:
            return
        if str(self.chk_fq_head.cget("variable")) != str(v["on"]):
            self.chk_fq_head.configure(variable=v["on"])
        fl = self.fq_list(self.mode)
        if not v["on"].get():
            txt, col = "꺼짐 (지금 주파수 하나만)", THEME["dim"]
        elif fl:
            txt = "● %d개: %s GHz" % (len(fl), ", ".join("%.6g" % x for x in fl[:4]) + (" …" if len(fl) > 4 else ""))
            col = THEME["ok"] if "ok" in THEME else "#2e9e3f"
        else:
            txt, col = "● 사용: 주파수 확인", THEME["warn"]
        sw0 = getattr(self, "sweep", None)
        sw = sw0 if (sw0 and sw0.get("mode") == self.mode and sw0.get("freqs")) else None
        if sw:                              # 접혀 있어도 진행 현황이 보이게 (지금 주파수 색으로)
            fl_, fi = sw["freqs"], sw.get("fi", 0)
            have = {freq_key(r) for r in self.rows if freq_key(r) is not None}
            allf = sorted(have | {round(f, 6) for f in fl_})
            if 0 <= fi < len(fl_):
                f = round(fl_[fi], 6)
                col = THEME["info"]
                txt = "%.6g GHz · %d/%d" % (f, fi + 1, len(fl_))
        cf.right_label.configure(text=txt, foreground=col)

    def build_fq_box(self, parent, mode):
        """여러 주파수 자동 측정 (Pulse / Mod. 따로). 주파수는 직접 입력(개수 제한 없음) 또는 시작 · 끝 · 간격,
        샘플 종류마다 Pulse · Mod. 따로 기억"""
        s = self.s
        pre = "fq_" if mode == "pulse" else "mfq_"
        v = dict(on=tk.BooleanVar(value=bool(s.get(pre + "on", False))),
                 how=tk.StringVar(value="range"),
                 start=tk.StringVar(value=s.get(pre + "start", "3.4")),
                 stop=tk.StringVar(value=s.get(pre + "stop", "3.6")),
                 step=tk.StringVar(value=s.get(pre + "step", "0.1")),
                 list=tk.StringVar(value=""),
                 wait=tk.StringVar(value=s.get(pre + "wait", "3")),
                 auto=tk.BooleanVar(value=bool(s.get(pre + "auto", True))))
        self.__dict__.setdefault("fqv", {})[mode] = v
        self._fx_loading = getattr(self, "_fx_loading", False)
        a = parent
        box = types.SimpleNamespace(refresh=self.fq_card_refresh)
        lst = ttk.Frame(a, style="Card.TFrame")
        rng = ttk.Frame(a, style="Card.TFrame")
        hint = ttk.Label(a, text="", style="Hint.TLabel")

        def link(par, txt, how):
            lb = tk.Label(par, text=txt, cursor="hand2", font=("맑은 고딕", 8, "underline"), bd=0, padx=0)
            self.reg(lb, bg="panel", fg="accent")
            lb.bind("<Button-1>", lambda e: v["how"].set(how))
            lb.pack(side="right")
            return lb

        def show_how():
            who = "Mod." if mode == "mod" else "Pulse"
            if v["how"].get() == "list":
                rng.pack_forget()
                lst.pack(fill="x", pady=(3, 0), before=hint)
                hint.configure(text="쉼표로 구분 · 개수 제한 없음 · 샘플 종류마다 %s 따로 기억" % who)
            else:
                lst.pack_forget()
                rng.pack(fill="x", pady=(3, 0), before=hint)
                hint.configure(text="최대 200개 · 샘플 종류마다 %s 따로 기억" % who)
            self.root.after(30, self.fit_ctrl_height)
        for k, (nm_, u_) in enumerate((("start", "시작"), ("stop", "끝"), ("step", "간격"))):
            ttk.Label(rng, text=u_, style="Lab.TLabel").pack(side="left", padx=(6 if k else 0, 3))
            ttk.Entry(rng, textvariable=v[nm_], width=6, justify="right").pack(side="left")
        ttk.Label(rng, text="GHz", style="Unit.TLabel").pack(side="left", padx=(4, 0))
        v["cnt_r"] = ttk.Label(rng, text="", style="Info.TLabel")
        v["cnt_r"].pack(side="left", padx=(6, 0))
        link(rng, "직접 입력…", "list")
        link(lst, "시작 · 끝 · 간격…", "range")
        ttk.Label(lst, text="주파수", style="Lab.TLabel").pack(side="left", padx=(0, 4))
        e = ttk.Entry(lst, textvariable=v["list"], width=22)
        e.pack(side="left", fill="x", expand=True)
        ttk.Label(lst, text="GHz", style="Unit.TLabel").pack(side="left", padx=(4, 0))
        v["cnt_l"] = ttk.Label(lst, text="", style="Info.TLabel")
        v["cnt_l"].pack(side="left", padx=(6, 8))
        hint.pack(anchor="w")
        v["hint"] = hint
        # 진행 바: 시작 ~ 끝 GHz 막대 · 눈금 = 측정할 주파수 · 지금 위치 ▼
        bar = tk.Canvas(a, height=70, highlightthickness=0, bd=0)
        self.reg(bar, bg="panel")
        bar.pack(fill="x", pady=(6, 0))
        bar.bind("<Configure>", lambda e, mode=mode: (v.__setitem__("bar_key", None), self.update_fq_chips()))
        v["bar"] = bar
        v["bar_key"] = None
        g3 = self.form(a)
        g3.pack(fill="x")
        self.fpair(g3, "RF OFF 대기" if mode == "mod" else "사이 대기", v["wait"], "초")
        lab = ttk.Label(a, text="", style="Info.TLabel")
        lab.pack(anchor="w")
        v["lbl"] = lab
        if mode == "pulse":
            ttk.Checkbutton(a, text="묻지 않고 자동 진행 (최대 허용 · SA 포화 · Id 상한 → 내리고 다음 주파수)", variable=v["auto"],
                            style="Card.TCheckbutton").pack(anchor="w")
            ttk.Label(a, text="주파수마다: 시작 SG → 최대 → 천천히 내림 → 대기 → 다음", style="Hint.TLabel").pack(anchor="w")
        else:
            ttk.Label(a, text="주파수마다: 시작 SG → 목표 Pout → RF OFF → 대기 → 다음 주파수 시작 SG 에서 RF ON",
                      style="Hint.TLabel").pack(anchor="w")
        for k in ("how", "list", "start", "stop", "step"):
            v[k].trace_add("write", lambda *a_, mode=mode: (self.fx_changed(mode), self.update_fq_label(), box.refresh()))
        v["on"].trace_add("write", lambda *a_, mode=mode: (self.fx_changed(mode), self.update_fq_label(), box.refresh()))
        for k in ("wait", "auto"):
            v[k].trace_add("write", lambda *a_, mode=mode: self.fx_changed(mode))
        v["box"] = box
        v["show_how"] = show_how
        v["how"].trace_add("write", lambda *a_: show_how())
        show_how()
        if mode == "mod":                  # 두 모드 다 만든 뒤 한 번: 지금 샘플 종류 값 불러오기
            self.load_stype_freqs()

    def stype_rec(self, name=None):
        name = self.cur_stype() if name is None else name
        for t in self.sample_types:
            if t["name"] == name:
                return t
        return None

    @staticmethod
    def fq_from_text(txt):
        """설정 창 칸 글자 → 주파수 설정. '3.4~3.6/0.1' = 시작 · 끝 · 간격, 그 밖 = 직접 입력"""
        txt = (txt or "").strip()
        if "~" in txt:
            a_, rest = txt.split("~", 1)
            b_, st_ = (rest.split("/", 1) + ["0.1"])[:2]
            return {"how": "range", "start": a_.strip(), "stop": b_.strip(), "step": st_.strip(), "list": ""}
        return {"how": "list", "list": txt}

    @staticmethod
    def fq_to_text(d):
        d = d or {}
        if d.get("how") == "range":
            return "%s~%s/%s" % (d.get("start", ""), d.get("stop", ""), d.get("step", ""))
        return d.get("list", "")

    def stype_fq(self, t, mode):
        """샘플 종류의 Pulse / Mod. 주파수 설정 (예전 'freqs' 3개 → 두 모드 모두 직접 입력으로)"""
        if t is None:
            d = (self.s.get("fq_free2") or {}).get(mode)
            if d is None and self.s.get("fq_free"):
                d = {"how": "list", "list": ", ".join(x for x in self.s["fq_free"] if str(x).strip())}
            return d
        d = (t.get("fq") or {}).get(mode)
        if d is None and t.get("freqs"):
            d = {"how": "list", "list": ", ".join(x for x in t["freqs"] if str(x).strip())}
        return d

    def fq_ref(self, stype=None):
        """색 기준 주파수 목록 (GHz) = 샘플 종류의 Pulse ∪ Mod. 주파수 (종류가 없으면 지금 입력값)"""
        out = set()
        t = self.stype_rec(stype) if (stype or self.cur_stype()) else None
        for md in ("pulse", "mod"):
            try:
                d = self.stype_fq(t, md) if t is not None else None
                if d:
                    fl = self.fq_list(md, d=d)
                else:
                    fl = self.fq_list(md) if hasattr(self, "fqv") and md in self.fqv else []
                out.update(round(f, 6) for f in (fl or []))
            except Exception:
                pass
        return sorted(out)

    def load_stype_freqs(self):
        """샘플 종류를 바꾸면 그 종류의 Pulse · Mod. 주파수 설정을 칸에 넣음 (없으면 지금 값 그대로)"""
        t = self.stype_rec()
        self._fx_loading = True
        try:
            for mode, v in self.fqv.items():
                d = self.stype_fq(t, mode)
                if not d:
                    continue
                for k in ("how", "list", "start", "stop", "step", "wait"):
                    if k in d and v[k].get() != str(d[k]):
                        v[k].set(str(d[k]))
                for k in ("on", "auto"):                # 여러 주파수 자동 측정 사용 · 자동 진행
                    if k in d and bool(v[k].get()) != bool(d[k]):
                        v[k].set(bool(d[k]))
                if "show_how" in v:
                    v["show_how"]()
        finally:
            self._fx_loading = False
        for mode in self.fqv:
            self.fx_changed(mode)
        self.update_fq_label()

    def fx_changed(self, mode, save=True):
        if not save or self._fx_loading or not hasattr(self, "fqv") or mode not in self.fqv:
            return
        v = self.fqv[mode]
        d = {k: v[k].get() for k in ("how", "list", "start", "stop", "step", "wait", "on", "auto")}
        t = self.stype_rec()
        if t is not None:
            t.setdefault("fq", {})[mode] = d
            self.s["sample_types"] = [dict(x) for x in self.sample_types]
        else:
            self.s.setdefault("fq_free2", {})[mode] = d

    def fq_progress(self, mode):
        """여러 주파수 진행 상태 → (주파수 목록, 지금 번호(-1 = 측정 전), 측정 중?, 남은 시간 글자)"""
        v = self.fqv[mode]
        sw0 = getattr(self, "sweep", None)
        sw = sw0 if (sw0 and sw0.get("mode") == mode and sw0.get("freqs")) else None
        if sw:
            fl, fi = sw["freqs"], min(sw.get("fi", 0), len(sw["freqs"]) - 1)
        elif v["on"].get():
            fl, fi = (self.fq_list(mode) or []), -1
        else:
            fl, fi = [], -1
        rem = ""
        if sw:
            now = time.time()
            tm = v.get("fi_t")
            if not tm or tm.get("id") != id(sw):
                tm = v["fi_t"] = {"id": id(sw)}
            tm.setdefault(fi, now)
            if fi > 0 and 0 in tm:
                avg = (tm[fi] - tm[0]) / fi                     # 끝난 주파수 평균 시간
                left = max(0.0, avg * (len(fl) - fi) - (now - tm[fi]))
                rem = "남은 약 %d분" % max(1, round(left / 60)) if left >= 90 else "남은 약 %d초" % (max(5, round(left / 5) * 5))
        return fl, fi, bool(sw), rem

    def update_fq_chips(self):
        """여러 주파수 진행 바 (Pulse · Mod. 따로): 시작 ~ 끝 GHz 막대, 눈금 = 측정할 주파수,
        끝난 구간은 파랗게 채우고 지금 주파수에 ▼ 말풍선. 측정 전에는 계획한 눈금만."""
        for mode, v in getattr(self, "fqv", {}).items():
            cv = v.get("bar")
            if cv is None:
                continue
            fl, fi, run, rem = self.fq_progress(mode)
            W = cv.winfo_width()
            key = (tuple(fl), fi, run, rem, W, THEME["panel"])
            if key == v.get("bar_key"):
                continue
            v["bar_key"] = key
            cv.delete("all")
            if not fl:
                if cv.winfo_ismapped():
                    cv.pack_forget()
                    self.root.after_idle(self.sync_left_cards)
                continue
            if not cv.winfo_ismapped():
                cv.pack(fill="x", pady=(6, 0), after=v["hint"])
                self.root.after_idle(self.sync_left_cards)
                continue                                    # 크기가 정해지면 <Configure> 로 다시 그림
            if W < 60:
                continue
            P, acc, dark = THEME["panel"], THEME["accent"], "#1565c0"
            L, R = 12, W - 12
            lo, hi = min(fl), max(fl)
            xf = (lambda f: (L + R) / 2) if hi - lo < 1e-9 else (lambda f: L + (f - lo) / (hi - lo) * (R - L))
            ty0, ty1 = 24, 34
            cv.create_rectangle(L - 5, ty0, R + 5, ty1, fill=self.mix(acc, P, 0.85), outline="")
            if run:
                xc = xf(fl[fi])
                cv.create_rectangle(L - 5, ty0, xc, ty1, fill=acc, outline="")
            tick_dim = self.mix(THEME["dim"], P, 0.45)
            for i, f in enumerate(fl):
                x = xf(f)
                on_fill = run and i <= fi
                cv.create_line(x, ty0 + 2, x, ty1 - 2, fill="white" if on_fill else tick_dim, width=2)
            fnt = ("맑은 고딕", 8)
            cv.create_text(L - 4, ty1 + 3, text="%.6g" % lo, anchor="nw", fill=THEME["unit"], font=fnt)
            cv.create_text(R + 4, ty1 + 3, text="%.6g GHz" % hi, anchor="ne", fill=THEME["unit"], font=fnt)
            if len(fl) > 2 and hi - lo > 1e-9:
                cv.create_text((L + R) / 2, ty1 + 3, text="%.6g" % round((lo + hi) / 2, 4), anchor="n",
                               fill=THEME["unit"], font=fnt)
            if run:
                xc = xf(fl[fi])
                cv.create_oval(xc - 7, (ty0 + ty1) / 2 - 7, xc + 7, (ty0 + ty1) / 2 + 7, fill="white", outline=dark, width=3)
                t = cv.create_text(0, 0, text="%.6g GHz" % fl[fi], fill="white", font=("맑은 고딕", 8, "bold"))
                x0, y0, x1, y1 = cv.bbox(t)
                bw, bh = x1 - x0 + 10, y1 - y0 + 2
                bx = min(max(xc - bw / 2, 1), W - bw - 1)
                cv.create_rectangle(bx, 1, bx + bw, 1 + bh, fill=dark, outline="")
                cv.create_polygon(xc - 4, 1 + bh, xc + 4, 1 + bh, xc, 6 + bh, fill=dark, outline="")
                cv.coords(t, bx + bw / 2, 1 + bh / 2)
                cv.tag_raise(t)
                nxt = (" · 다음 %.6g" % fl[fi + 1]) if fi + 1 < len(fl) else " · 마지막"
                cv.create_text(L - 4, 62, text="● %.6g GHz 측정 중%s" % (fl[fi], nxt), anchor="w",
                               fill=THEME["info"], font=("맑은 고딕", 9, "bold"))
                cv.create_text(R + 4, 62, text="%d / %d%s" % (fi + 1, len(fl), (" · " + rem) if rem else ""),
                               anchor="e", fill=THEME["fg"], font=("맑은 고딕", 9))
            else:
                cv.create_text(L - 4, 62, text="%d개 측정 예정" % len(fl), anchor="w", fill=THEME["dim"],
                               font=("맑은 고딕", 9))
        self.fq_head_bar()

    def fq_head_bar(self):
        """카드 제목줄 작은 진행 바 (접혀 있어도 보이게, 측정 중일 때만)"""
        cf = getattr(self, "card_fq", None)
        cv = getattr(self, "fq_mini", None)
        if cf is None or cv is None or not hasattr(self, "fqv") or self.mode not in self.fqv:
            return
        fl, fi, run, rem = self.fq_progress(self.mode)
        key = (tuple(fl), fi, run, THEME["panel"])
        if key == getattr(self, "_fq_mini_key", None):
            return
        self._fq_mini_key = key
        cv.delete("all")
        if not run:
            cv.pack_forget()
            return
        if not cv.winfo_ismapped():
            cv.pack(side="right", padx=(0, 6))
        w, h = int(cv.cget("width")), int(cv.cget("height"))
        cv.create_rectangle(0, 0, w, h, fill=self.mix(THEME["accent"], THEME["panel"], 0.8), outline="")
        cv.create_rectangle(0, 0, w * (fi + 0.5) / len(fl), h, fill=THEME["accent"], outline="")

    def sync_left_cards(self):
        """왼쪽 칸 내용 높이가 바뀌면 (칩 · 접기 등) 칸 높이를 다시 맞춤"""
        for c in (getattr(self, "card_fq", None), getattr(self, "card3", None)):
            sf = getattr(c, "sf", None)
            if sf is not None:
                try:
                    if sf.cv.winfo_height() != min(sf.natural_h(), getattr(sf, "max_h", None) or 10 ** 6):
                        sf._sync()
                except tk.TclError:
                    pass
        c1 = self.leftcol.winfo_children()[0] if hasattr(self, "leftcol") else None
        if getattr(c1, "sf", None) is not None:
            c1.sf._sync()

    @staticmethod
    def mix(c1, c2, k):
        """c1 을 c2 쪽으로 k 만큼 (옅은 색 만들기)"""
        a = [int(c1.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
        b = [int(c2.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
        return "#%02x%02x%02x" % tuple(int(x + (y - x) * k) for x, y in zip(a, b))

    # ------------------------------------------------ 케이블 손실 편집 창
    def build_loss_editor(self):
        s = self.s
        win = tk.Toplevel(self.root)
        win.withdraw()
        win.title("케이블 손실 (주파수별, dB)")
        win.transient(self.root)
        win.protocol("WM_DELETE_WINDOW", win.withdraw)
        self.loss_win = win
        g3 = ttk.Frame(win, padding=10)
        g3.pack(fill="both", expand=True)
        g3.rowconfigure(0, weight=1)
        for c in range(6):
            g3.columnconfigure(c, weight=1)
        self.loss_tree = ttk.Treeview(g3, columns=("f", "i", "o"), show="headings", height=10)
        for k, t, wd in (("f", "주파수(GHz)", 90), ("i", "In loss", 70), ("o", "Out loss", 70)):
            self.loss_tree.heading(k, text=t)
            self.loss_tree.column(k, width=wd, anchor="center")
        self.loss_tree.grid(row=0, column=0, columnspan=6, sticky="nsew")
        self.loss_tree.bind("<<TreeviewSelect>>", self.loss_selected)
        self.loss_tree.tag_configure("used", background="#fff3a8", foreground="#000")
        self.loss_hl = None
        self.var_lf = tk.StringVar()
        self.var_li = tk.StringVar(value=s.get("in_loss", "0"))
        self.var_lo = tk.StringVar(value=s.get("out_loss", "0"))
        for col, (lab, var, wd) in enumerate((("GHz", self.var_lf, 7), ("In", self.var_li, 5), ("Out", self.var_lo, 5))):
            ttk.Label(g3, text=lab).grid(row=1, column=col * 2, sticky="e", padx=2, pady=4)
            ttk.Entry(g3, textvariable=var, width=wd).grid(row=1, column=col * 2 + 1, sticky="w", padx=2, pady=4)
        bf3 = ttk.Frame(g3)
        bf3.grid(row=2, column=0, columnspan=6, sticky="w")
        ttk.Button(bf3, text="추가/수정", command=self.loss_add).pack(side="left", padx=2)
        ttk.Button(bf3, text="삭제", command=self.loss_del).pack(side="left", padx=2)
        self.icon_save = make_icon(self.root, "save")
        self.icon_open = make_icon(self.root, "open")
        ttk.Button(bf3, text="내보내기", image=self.icon_save, compound="left", command=self.loss_save).pack(side="left", padx=2)
        ttk.Button(bf3, text="불러오기", image=self.icon_open, compound="left", command=self.loss_load).pack(side="left", padx=2)
        self.lbl_loss2 = ttk.Label(g3, text="", foreground="#1f4f8f")
        self.lbl_loss2.grid(row=3, column=0, columnspan=6, sticky="w", pady=(4, 0))
        ttk.Label(g3, text="SG 주파수에 가장 가까운 행 사용 (노란색 = 사용 중). 표가 비어 있으면 In / Out 칸 값 사용",
                  foreground="#666").grid(row=4, column=0, columnspan=6, sticky="w")
        ttk.Button(g3, text="닫기", command=win.withdraw).grid(row=5, column=5, sticky="e", pady=(6, 0))
        self.loss_table = [list(map(float, r)) for r in s.get("loss_table", [])]
        self.refresh_loss_tree()

    def open_loss_editor(self):
        self.loss_win.deiconify()
        center_window(self.loss_win, self.root)
        self.loss_win.lift()

    # ------------------------------------------------ ⚡ 바이어스 미세 조정 (Vdd · DRV · CA · PK)
    BT = dict(bg=BG, head="#1f2a35", dim="#9fb0bd", unit="#7d8f9c", ok="#81c784", fg="#dfe6ec", entry="#0f151b")

    def build_bias_tuner(self, parent):
        """⚡ 바이어스 미세 조정 - 맨 위 오른쪽 (어두운 칸 고정): Vdd · DRV · CA · PK + ON / ON+측정 / OFF"""
        c = self.BT
        self.bias_rows = {}
        top = tk.Frame(parent, bg=c["bg"])
        top.pack(fill="x", padx=10, pady=(4, 1))
        self.lbl_power_lamp = tk.Label(top, text=" ○ POWER OFF ", bg="#455a64", fg="white", font=("맑은 고딕", 11, "bold"),
                                       padx=6)
        self.lbl_power_lamp.pack(side="left", padx=(0, 8))
        tk.Label(top, text="⚡ 바이어스", bg=c["bg"], fg="#b0bec5", font=("맑은 고딕", 12, "bold")).pack(side="left")
        self.lbl_bias_state = tk.Label(top, text="○ 전원 연결 안 됨", bg=c["bg"], fg=c["unit"], font=("맑은 고딕", 8))
        self.lbl_bias_state.pack(side="right")
        body = tk.Frame(parent, bg=c["bg"])
        body.pack(fill="both", expand=True, padx=(10, 6), pady=(0, 2))
        rowsf = tk.Frame(body, bg=c["bg"])
        rowsf.pack(side="left", fill="y")
        rows = (("e", None, "Vdd", "#455a64", "white"), ("n", 1, "DRV", "#f2c200", "black"),
                ("n", 2, "CA", "#2e9e3f", "white"), ("n", 3, "PK", "#1f6fd1", "white"))
        for k, ch, name, col, fg in rows:
            fr = tk.Frame(rowsf, bg=c["bg"])
            fr.pack(fill="x", pady=1)
            tk.Label(fr, text=name, bg=col, fg=fg, width=4, font=("맑은 고딕", 8, "bold")).pack(side="left", ipady=1)
            var = tk.StringVar(value="---")
            ent = tk.Entry(fr, textvariable=var, width=8, justify="right", font=("Consolas", 11, "bold"),
                           relief="flat", bd=1, highlightthickness=1, bg=c["entry"], fg=c["fg"],
                           insertbackground=c["fg"], highlightbackground="#2b3540", highlightcolor="#1e88e5")
            ent.pack(side="left", padx=(5, 3))
            dl = psu_panel.Dial(fr, c["bg"], size=22, accent="#ff9800",
                                on_turn=lambda d, m, k=k, ch=ch: self.bias_nudge(k, ch, d, m))
            dl.pack(side="left")
            on = tk.Button(fr, text="Off", width=3, relief="flat", bd=0, font=("맑은 고딕", 7, "bold"),
                           command=lambda k=k, ch=ch: self.bias_toggle(k, ch))
            on.pack(side="left", padx=(4, 4))
            cur = tk.Label(fr, text="", bg=c["bg"], fg=c["ok"], font=("Consolas", 9), anchor="w", width=15)
            cur.pack(side="left")
            for seq, d in (("<Up>", 1), ("<Down>", -1)):
                ent.bind(seq, lambda e, k=k, ch=ch, d=d: (self.bias_nudge(k, ch, d, 10 if e.state & 1 else 1), "break")[1])
            ent.bind("<MouseWheel>", lambda e, k=k, ch=ch: (self.bias_nudge(k, ch, 1 if e.delta > 0 else -1,
                                                                            10 if e.state & 1 else 1), "break")[1])
            ent.bind("<Button-4>", lambda e, k=k, ch=ch: (self.bias_nudge(k, ch, 1, 1), "break")[1])
            ent.bind("<Button-5>", lambda e, k=k, ch=ch: (self.bias_nudge(k, ch, -1, 1), "break")[1])
            ent.bind("<Enter>", lambda e, w=ent: w.focus_set())
            ent.bind("<Return>", lambda e, k=k, ch=ch, v=var: self.bias_set(k, ch, v.get()))
            self.bias_rows[(k, ch)] = dict(var=var, ent=ent, dial=dl, on=on, cur=cur)
        bf = tk.Frame(body, bg=c["bg"])
        bf.pack(side="right", fill="y", padx=(6, 0))
        # 가운데 빈 곳: Vdd 전류 추이 (최근 60 초) 작은 그래프
        self.mini_trend = tk.Canvas(body, bg="#0a0f14", highlightthickness=1, highlightbackground="#2b3540",
                                    width=120, height=90)
        self.mini_trend.pack(side="left", fill="both", expand=True, padx=(4, 0))
        self._mini_t = 0.0
        for txt, cmd, bg_, act in (("▶ 바이어스 ON", lambda: self.psu_panel.power_on(), "#2e7d32", "#388e3c"),
                                   ("▶▶ ON + 측정", lambda: self.psu_panel.power_on(auto=True), "#1565c0", "#1e88e5"),
                                   ("■ 바이어스 OFF", self.bias_off_click, "#455a64", "#607d8b")):
            b = tk.Button(bf, text=txt, command=cmd, bg=bg_, fg="white", activebackground=act, activeforeground="white",
                          relief="flat", bd=0, font=("맑은 고딕", 8, "bold"), width=12)
            b.pack(fill="both", expand=True, pady=1)
            if txt.startswith("■"):
                self.btn_bias_off = b
        self.lbl_bias_hint = tk.Label(parent, text="휠 / ↑↓ (마우스 올린 칸) · Shift ×10 · Vdd ±0.1 V · 게이트 ±0.001 V · 입력 후 Enter",
                                      bg=c["bg"], fg=c["unit"], font=("맑은 고딕", 7), anchor="w")
        self.lbl_bias_hint.pack(fill="x", padx=10, pady=(0, 3))

    def bias_off_click(self):
        if self.sweep or self.ramp:
            messagebox.showwarning(APP_NAME, "자동 측정 / 천천히 내리기 중입니다. 끝나거나 중지한 뒤 끄세요.")
            return
        self.psu_panel.power_off()

    def auto_bias_off_step(self):
        """측정이 끝나면 (완료) SG 내림 → RF OFF → 바이어스 OFF 를 이어서 (③ 실행 체크)"""
        pend = getattr(self, "pending_bias_off", None)
        if not pend or self.sweep or self.ramp:
            return
        pp = self.psu_panel
        if pp.seq_running:
            return
        now = time.time()
        if self.smw.get("conn") and self.smw.get("rf"):
            if not pend.get("rf_t"):
                pend["rf_t"] = now
                self.set_rf(False, why="측정 끝 → 바이어스 OFF 전: ")
            elif now - pend["rf_t"] > 8:
                self.pending_bias_off = None
                self.status("RF OFF 확인 실패 → 바이어스 자동 OFF 안 함 (직접 확인하세요)", "#ff6b6b")
            return
        self.pending_bias_off = None
        on = pp.vdd_on() or any(pp.sets.get(("n", c), {}).get("o") for c in (1, 2, 3))
        if not on:
            return
        if pp.power_off(auto=True):
            self.status("측정 끝 → 바이어스 자동 OFF 시작 (CA · DRV 게이트 %s V → Vdd 0 V → OFF)" % pp.sq["off_g"].get(), "#39ff88")

    def bias_nudge(self, k, ch, d, mult=1):
        pp = getattr(self, "psu_panel", None)
        if pp is None:
            return False
        r = pp.nudge(k, ch, d, mult)
        if r is False:
            if pp.lock_reason():
                self.lbl_bias_hint.configure(text="🔒 이 샘플 종류의 바이어스 기본값이 없어 원격 제어 잠금 - ① 샘플 종류 [설정] 에서 지정")
            elif not pp.var_ctrl.get():
                self.lbl_bias_hint.configure(text="제어 잠금 상태 - [⚡ 파워서플라이] 화면에서 '제어 사용' 체크 (POWER SUPPLY ON 하면 자동 해제)")
            elif pp.inst[k] is None:
                self.lbl_bias_hint.configure(text="%s 가 연결되지 않았습니다." % ("E36155A" if k == "e" else "N6705C"))
            else:
                self.lbl_bias_hint.configure(text="안전 한계 밖이거나 설정값을 아직 못 읽었습니다.")
        else:
            self.lbl_bias_hint.configure(text="")
            self.bias_update(force=True)
        return r

    def bias_set(self, k, ch, txt):
        pp = getattr(self, "psu_panel", None)
        if pp is None:
            return
        try:
            val = float(txt.replace("V", "").strip())
        except ValueError:
            self.lbl_bias_hint.configure(text="숫자를 입력하세요")
            return
        pp.set_value(k, ch, val)
        self.root.focus_set()

    def bias_toggle(self, k, ch):
        pp = getattr(self, "psu_panel", None)
        if pp is not None:
            pp.toggle_out(k, ch)

    def bias_update(self, force=False):
        pp = getattr(self, "psu_panel", None)
        if pp is None or not hasattr(self, "bias_rows"):
            return
        if time.time() - getattr(self, "_mini_t", 0) > 0.5 and hasattr(self, "mini_trend"):
            self._mini_t = time.time()
            try:
                pp.draw_trend_canvas(self.mini_trend, compact=True)
            except tk.TclError:
                pass
        snap = pp.bias_snapshot()
        i_, v_, t_ = pp.e_last
        fresh = time.time() - t_ < 3
        if pp.inst["e"] is None and pp.inst["n"] is None:
            st_, col_ = "○ 전원 연결 안 됨", self.BT["unit"]
        elif pp.seq_running:
            st_, col_ = "● 전원 순서 진행 중", "#f9a825"
        elif pp.polling and fresh:
            st_, col_ = "● 전원 읽는 중 (%.2g 초마다)" % pp.poll_dt, self.BT["ok"]
        else:
            st_, col_ = "○ 전원 읽기 멈춤", "#e65100"
        rl = self.lbl_bias_state
        if rl.cget("text") != st_:
            rl.configure(text=st_, fg=col_)
        # 크게: 파워서플라이 ON / OFF 상태 (Vdd 출력 · 게이트 출력 기준)
        outs = [bool(pp.sets.get(key, {}).get("o")) for key in (("e", None), ("n", 1), ("n", 2), ("n", 3))]
        if pp.inst["e"] is None and pp.inst["n"] is None:
            lt, lb = " ○ 연결 안 됨 ", "#37474f"
        elif pp.bias_state == "fault":
            lt, lb = " ⚠ 이상 - 확인 ", "#c62828"
        elif pp.seq_running:
            lt, lb = " ◐ ON/OFF 진행 중 ", "#ef6c00"
        elif all(outs):
            lt, lb = " ● POWER ON ", "#2e7d32"
        elif not any(outs):
            lt, lb = " ○ POWER OFF ", "#455a64"
        else:
            lt, lb = " ◐ 일부만 ON (%s) " % " ".join(n for n, o in zip(("Vdd", "DRV", "CA", "PK"), outs) if o), "#f9a825"
        if self.lbl_power_lamp.cget("text") != lt:
            self.lbl_power_lamp.configure(text=lt, bg=lb)
        for (k, ch), r in self.bias_rows.items():
            cur = pp.sets.get((k, ch), {})
            val = cur.get("v")
            if force or self.root.focus_get() is not r["ent"] or not r.get("typing"):
                txt = ("%.1f V" % val if k == "e" else "%.3f V" % val) if val is not None and val == val else "---"
                if r["var"].get() != txt and (force or self.root.focus_get() is not r["ent"] or r.get("last") == r["var"].get()):
                    r["var"].set(txt)
                    r["last"] = txt
            o = cur.get("o")
            B = self.BT
            if pp.inst[k] is None:
                r["on"].configure(text="-", bg=B["head"], fg=B["unit"])
            else:
                r["on"].configure(text="On" if o else "Off", bg="#2e7d32" if o else B["head"],
                                  fg="white" if o else B["dim"])
            if k == "e":
                r["cur"].configure(text=("Id %.1f mA" % (i_ * 1e3)) if (fresh and i_ == i_) else "", fg=B["ok"])
            elif ch == 1 and snap.get("i_drv") is not None:
                r["cur"].configure(text="I_DRV %.1f mA" % (snap["i_drv"] * 1e3), fg=B["ok"])
            elif ch == 2 and snap.get("i_ca") is not None:
                r["cur"].configure(text="I_CA %.1f mA" % (snap["i_ca"] * 1e3), fg=B["ok"])
            else:
                vm = pp.last_vm.get(ch)
                r["cur"].configure(text=("읽음 %.3f V" % vm) if (vm is not None and pp.inst["n"] is not None) else "",
                                   fg=B["dim"])
            r["dial"].set_enabled(pp.inst[k] is not None and pp.var_ctrl.get())

    # ------------------------------------------------ 오른쪽: 탭 (그래프 · 파워서플라이 · 장비)
    def build_right(self, right):
        s = self.s
        bar = ttk.Frame(right)
        bar.pack(fill="x")
        self.var_right = tk.StringVar(value="plot")
        for val, txt in (("plot", "📈 그래프 (Gain · PAE)"),):   # 파워서플라이는 맨 위 [⚡ 파워서플라이] 탭
            ttk.Radiobutton(bar, text=txt, value=val, variable=self.var_right, style="Tab.Toolbutton",
                            command=lambda v=val: self.show_right(v)).pack(side="left", padx=(0, 2))
        self.plot_tools = ttk.Frame(bar)
        self.plot_tools.pack(side="right")
        self.var_plotmode = tk.StringVar(value=s.get("plot_mode", "combined"))
        seg = ttk.Frame(self.plot_tools)
        seg.pack(side="left", padx=(0, 6))
        for val, txt in (("combined", "합쳐 보기"), ("split", "나눠 보기 (Gain | PAE)")):
            ttk.Radiobutton(seg, text=txt, value=val, variable=self.var_plotmode, style="Seg.Toolbutton",
                            command=self.on_plot_mode).pack(side="left")
        if HAS_ANALYZER:
            ttk.Button(self.plot_tools, text="PA Analyzer 창", style="Small.TButton", command=self.open_analyzer).pack(side="left", padx=2)
        ttk.Button(self.plot_tools, text="새 창", style="Small.TButton", command=self.open_plot_window).pack(side="left", padx=2)
        ttk.Button(self.plot_tools, text="원래 범위", style="Small.TButton", command=self.plots_reset_view).pack(side="left", padx=2)
        mb = ttk.Menubutton(self.plot_tools, text="⋯", width=2)
        m = tk.Menu(mb, tearoff=0)
        m.add_command(label="주파수 색 (같은 주파수 = 같은 색)", command=lambda: self.set_color_mode("freq"))
        m.add_command(label="각각 다른 색", command=lambda: self.set_color_mode("each"))
        m.add_command(label="선 · 마커 기본으로", command=self.reset_line_styles)
        m.add_separator()
        m.add_command(label="X 축 편집...", command=lambda: self.edit_axis(None, "x"))
        m.add_command(label="Gain 축 편집...", command=lambda: self.edit_axis(None, "gain"))
        m.add_command(label="PAE 축 편집...", command=lambda: self.edit_axis(None, "pae"))
        mb.configure(menu=m)
        mb.pack(side="left", padx=(2, 0))
        line = tk.Frame(right, height=2)
        self.reg(line, bg="accent")
        line.pack(fill="x")
        body = ttk.Frame(right)
        body.pack(fill="both", expand=True, pady=(6, 0))
        self.view_plot = ttk.Frame(body)
        # 파워서플라이 화면도 맨 위 탭 (장비 옆) → 본 화면 밖 자기 자리에
        self.psu_host = ttk.Frame(self.root)
        self.view_psu = ttk.Frame(self.psu_host)
        self.view_psu.pack(fill="both", expand=True)
        # 장비 화면은 맨 위 측정 탭 옆 [🔧 장비] 탭 (Pulse · Mod. · DPD 공통) → 본 화면 밖 자기 자리에
        self.equip_host = ttk.Frame(self.root)
        self.view_equip = ttk.Frame(self.equip_host)
        self.view_equip.pack(fill="both", expand=True)
        # 축 설정 (편집 창에서 바꿈)
        def axv(key, default):
            vals = list(s.get(key, default))
            vals += [""] * (len(default) - len(vals))
            return [tk.StringVar(value=str(v)) for v in vals[:len(default)]]
        self.var_pae_ax = axv("pae_ax", ["20", "80", "10", ""])
        self.var_gain_ax = axv("gain_ax", ["20", "40", "2.5", ""])
        self.var_x_ax = axv("x_ax", ["26", "54", "5", "1"])
        self.ax_titles = dict({"x": "DUT Pout (dBm)", "gain": "Gain (dB)", "pae": "PAE (%)"}, **s.get("ax_titles", {}))
        # 그래프 화면: [그래프 | Datasets] / [측정 기록]
        vp = ttk.PanedWindow(self.view_plot, orient="vertical")
        vp.pack(fill="both", expand=True)
        self.vpan = vp
        top = ttk.Frame(vp)
        vp.add(top, weight=3)
        dsp = ttk.PanedWindow(top, orient="horizontal")
        dsp.pack(fill="both", expand=True)
        pf = tk.Frame(dsp, highlightthickness=1, bd=0)
        self.reg(pf, bg="plot_bg", highlightbackground="border")
        self.plots = [PlotPanel(pf, mode=self.var_plotmode.get())]
        self.plots[0].pack(fill="both", expand=True, padx=2, pady=2)
        dsp.add(pf, weight=1)
        dsf = tk.Frame(dsp, highlightthickness=1, bd=0)
        self.reg(dsf, bg="panel", highlightbackground="border")
        dsp.add(dsf, weight=0)
        dsi = ttk.Frame(dsf, style="Card.TFrame", padding=6)
        dsi.pack(fill="both", expand=True)
        self.build_datasets(dsi)
        self.hook_plot(self.plots[0])
        self.dsp = dsp
        self.build_records(vp)
        # 장비 화면: SMW · FSW · RTO · 전원 연결(VISA · SCPI) 탭
        self.nb = ttk.Notebook(self.view_equip)
        self.nb.pack(fill="both", expand=True)
        self.psu_extra = ttk.Frame(self.nb, padding=4)
        # 파워서플라이 화면
        self.build_psu_view(self.view_psu)
        self.show_right("plot")
        self.root.after(400, self.init_sashes)

    def build_records(self, vp):
        rc = tk.Frame(vp, highlightthickness=1, bd=0)
        self.reg(rc, bg="panel", highlightbackground="border")
        vp.add(rc, weight=2)
        hd = ttk.Frame(rc, style="Card.TFrame", padding=(10, 6, 8, 6))
        hd.pack(fill="x")
        ttk.Label(hd, text="측정 기록", style="Head.TLabel").pack(side="left")
        seg = ttk.Frame(hd, style="Card.TFrame")
        seg.pack(side="left", padx=(12, 0))
        self.rec_mode_btns = {}
        for val in ("pulse", "mod"):
            b = ttk.Radiobutton(seg, text=val, value=val, variable=self.var_mode, style="Seg.Toolbutton",
                                command=lambda: self.set_mode(self.var_mode.get()))
            b.pack(side="left")
            self.rec_mode_btns[val] = b
        for txt, fn, sty in (("전체 지우기", self.clear_rows, "Small.TButton"),
                             ("선택 삭제", self.delete_selected, "Small.TButton"),
                             ("다시 계산", self.recalc_rows, "Small.TButton"),
                             ("CSV", self.save_csv, "Small.TButton"),
                             (".pam 저장", self.save_pam, "Small.TButton"),
                             ("엑셀 저장", self.export_excel, "Small.TButton"),
                             ("복사", self.copy_rows, "Small.TButton")):
            ttk.Button(hd, text=txt, style=sty, command=fn).pack(side="right", padx=2)
            if txt == "다시 계산":
                self.build_whatif_vdd(hd)
        self.lbl_bias = tk.Label(rc, text="", anchor="w", font=("Consolas", 9), padx=10, pady=3)
        self.reg(self.lbl_bias, bg="bias_bg", fg="bias_fg")
        self.lbl_bias.pack(fill="x")
        tf = ttk.Frame(rc, style="Card.TFrame")
        tf.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tf, columns=[c[0] for c in self.COLS], show="headings", height=8)
        for key, title, wd in self.COLS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=wd, minwidth=40, anchor="center", stretch=False)
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        sbx = ttk.Scrollbar(tf, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=sb.set, xscrollcommand=sbx.set)
        sbx.pack(side="bottom", fill="x")
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Control-c>", lambda e: self.copy_rows())
        self.tree.bind("<Delete>", lambda e: (self.delete_selected(), "break")[1])
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.update_bias_bar(), add="+")
        self.tree.tag_configure("check", background="#ffd6d6")   # 재측정 후에도 이상한 점

    def on_plot_mode(self):
        self.s["plot_mode"] = self.var_plotmode.get()
        for pnl in self.plots:
            try:
                pnl.set_mode(self.var_plotmode.get())
            except tk.TclError:
                pass
        self.refresh_plots(datasets=False)

    # ------------------------------------------------ 축 편집 창 (축 숫자 · 제목 클릭)
    def edit_axis(self, plot, which, event=None):
        if plot is not None:
            if isinstance(plot, DualPlot):
                which = {"y": "gain", "y2": "pae"}.get(which, which)
            elif which in ("y", "y2"):
                which = "pae" if plot.title.startswith("PAE") else "gain"
        vars_ = {"x": self.var_x_ax, "gain": self.var_gain_ax, "pae": self.var_pae_ax}[which]
        name = {"x": "X 축 (Pout)", "gain": "Gain 축", "pae": "PAE 축"}[which]
        unit = {"x": "dBm", "gain": "dB", "pae": "%"}[which]
        old = getattr(self, "axis_win", None)
        if old is not None:
            try:
                old.destroy()
            except tk.TclError:
                pass
        w = tk.Toplevel(self.root)
        self.axis_win = w
        w.withdraw()
        w.title(name + " 편집")
        w.transient(self.root)
        w.resizable(False, False)
        w.configure(bg=THEME["panel"])
        hd = tk.Label(w, text="  %s (클릭해서 편집)" % name, bg=THEME["accent"], fg="white", anchor="w",
                      font=("맑은 고딕", 10, "bold"), pady=5)
        hd.pack(fill="x")
        f = ttk.Frame(w, style="Card.TFrame", padding=12)
        f.pack(fill="both", expand=True)
        tv = tk.StringVar(value=self.ax_titles.get(which, ""))
        lo, hi, st = tk.StringVar(value=vars_[0].get()), tk.StringVar(value=vars_[1].get()), tk.StringVar(value=vars_[2].get())
        mn = tk.StringVar(value=vars_[3].get() if len(vars_) > 3 else "")
        rows = (("축 제목", tv, ""), ("최소", lo, unit), ("최대", hi, unit), ("숫자 간격", st, ""), ("작은 눈금", mn, ""))
        first = None
        for r, (lab, var, u) in enumerate(rows):
            ttk.Label(f, text=lab, style="Lab.TLabel").grid(row=r, column=0, sticky="e", padx=(0, 8), pady=3)
            e = ttk.Entry(f, textvariable=var, width=22 if r == 0 else 10, justify="left" if r == 0 else "right")
            e.grid(row=r, column=1, sticky="w", pady=3)
            ttk.Label(f, text=u, style="Unit.TLabel").grid(row=r, column=2, sticky="w", padx=(4, 0))
            first = first or e
        ttk.Label(f, text="빈 칸 = 자동 · 작은 눈금 빈 칸 = 없음", style="Hint.TLabel").grid(row=5, column=0, columnspan=3, sticky="w")
        bf = ttk.Frame(f, style="Card.TFrame")
        bf.grid(row=6, column=0, columnspan=3, sticky="e", pady=(8, 0))

        def apply(e=None):
            vars_[0].set(lo.get().strip())
            vars_[1].set(hi.get().strip())
            vars_[2].set(st.get().strip())
            if len(vars_) > 3:
                vars_[3].set(mn.get().strip())
            self.ax_titles[which] = tv.get().strip() or {"x": "DUT Pout (dBm)", "gain": "Gain (dB)", "pae": "PAE (%)"}[which]
            self.s["ax_titles"] = dict(self.ax_titles)
            w.destroy()
            self.plots_reset_view()
            self.refresh_plots(datasets=False)

        def auto():
            lo.set("")
            hi.set("")
            st.set("")
        ttk.Button(bf, text="자동", command=auto).pack(side="left", padx=2)
        ttk.Button(bf, text="취소", command=w.destroy).pack(side="left", padx=2)
        ttk.Button(bf, text="적용 (Enter)", style="Pri.TButton", command=apply).pack(side="left", padx=2)
        w.bind("<Return>", apply)
        w.bind("<Escape>", lambda e: w.destroy())
        w.update_idletasks()
        if event is not None:
            x = min(event.x_root + 10, w.winfo_screenwidth() - w.winfo_reqwidth() - 10)
            y = min(event.y_root - w.winfo_reqheight() // 2, w.winfo_screenheight() - w.winfo_reqheight() - 40)
            w.geometry("+%d+%d" % (max(0, x), max(0, y)))
        else:
            center_window(w, self.root)
        w.deiconify()
        w.lift()
        if first is not None:
            first.focus_set()
            first.select_range(0, "end")

    # ------------------------------------------------ 바이어스 기록 (측정 시작 때 한 번)
    def bias_now(self):
        pp = getattr(self, "psu_panel", None)
        if pp is None or (pp.inst["e"] is None and pp.inst["n"] is None):
            return None
        try:
            b = pp.bias_snapshot()
        except Exception:
            return None
        return dict(vdd=b.get("vdd"), vg_drv=b["vg"].get(1), vg_ca=b["vg"].get(2), vg_pk=b["vg"].get(3),
                    i_drv=b.get("i_drv"), i_ca=b.get("i_ca"), i_leak=b.get("i_leak"),
                    idq=b.get("idq") if b.get("idq") is not None else b.get("idq_now"))

    def bias_of_ds(self, key):
        """데이터셋(샘플 × 주파수)의 첫 행 바이어스"""
        for r in self.rows:
            if self.ds_key(r) == key and r.get("bias"):
                return r["bias"]
        return None

    @staticmethod
    def bias_text(b):
        def f(v, d, k=1.0):
            return ("%.*f" % (d, v * k)) if v is not None and v == v else "-"
        return "Vdd %s V   VG_DRV %s   VG_CA %s   VG_PK %s V   I_DRV %s mA   I_CA %s mA   Idq %s mA   I_leak %s mA" % (
            f(b.get("vdd"), 1), f(b.get("vg_drv"), 3), f(b.get("vg_ca"), 3), f(b.get("vg_pk"), 3),
            f(b.get("i_drv"), 1, 1e3), f(b.get("i_ca"), 1, 1e3), f(b.get("idq"), 1, 1e3), f(b.get("i_leak"), 2, 1e3))

    def update_bias_bar(self):
        if not hasattr(self, "lbl_bias"):
            return
        key = None
        sel = self.tree.selection()
        if sel:
            r = next((r for r in self.rows if str(r["no"]) == sel[0]), None)
            if r is not None:
                key = self.ds_key(r)
        if key is None and self.rows:
            key = self.ds_key(self.rows[-1])
        b = self.bias_of_ds(key) if key else None
        if b:
            self.lbl_bias.configure(text="바이어스 (측정 시작 시 1회 기록 · %s)   %s" % (self.ds_label(key), self.bias_text(b)))
        else:
            self.lbl_bias.configure(text="바이어스 (측정 시작 시 1회 기록)   - 전원(E36155A · N6705C) 연결 후 측정하면 표시됩니다")

    # ------------------------------------------------ 탭: SMW
    def build_smw_tab(self):
        s = self.s
        f = ttk.Frame(self.nb, padding=10)
        self.nb.add(f, text="  SMW200A (신호발생기)  ")
        pad = dict(padx=4, pady=4)
        g = ttk.LabelFrame(f, text=" 연결 (LAN) ", padding=8)
        g.pack(fill="x")
        ttk.Label(g, text="IP").grid(row=0, column=0, **pad)
        self.var_smw_host = tk.StringVar(value=s["smw_host"])
        ttk.Entry(g, textvariable=self.var_smw_host, width=18).grid(row=0, column=1, **pad)
        ttk.Label(g, text="Port").grid(row=0, column=2, **pad)
        self.var_smw_port = tk.StringVar(value=str(s["smw_port"]))
        ttk.Entry(g, textvariable=self.var_smw_port, width=6).grid(row=0, column=3, **pad)
        self.btn_smw = ttk.Button(g, text="연결", command=self.smw_connect, width=10)
        self.btn_smw.grid(row=0, column=4, **pad)
        self.lbl_smw_idn = ttk.Label(g, text="(IP에 demo 입력 시 장비 없이 테스트)", foreground="#666")
        self.lbl_smw_idn.grid(row=1, column=0, columnspan=6, sticky="w", **pad)
        self.var_smw_gtl = tk.BooleanVar(value=bool(s["smw_gtl"]))
        ttk.Checkbutton(g, text="읽은 뒤 바로 Local 복귀 (&GTL) - 장비 앞 패널로 파워 조절 가능하게",
                        variable=self.var_smw_gtl, command=self.apply_worker_opts).grid(
            row=2, column=0, columnspan=6, sticky="w", **pad)
        ttk.Label(g, text="읽기 주기(ms)").grid(row=3, column=0, **pad)
        self.var_smw_poll = tk.StringVar(value=str(s["smw_poll"]))
        ttk.Spinbox(g, from_=100, to=5000, increment=100, textvariable=self.var_smw_poll, width=7,
                    command=self.apply_worker_opts).grid(row=3, column=1, sticky="w", **pad)

        g2 = ttk.LabelFrame(f, text=" 파워 조절 (선택 사항 - 장비 앞 패널에서 직접 올려도 됨) ", padding=8)
        g2.pack(fill="x", pady=8)
        ttk.Label(g2, text="레벨(dBm)").grid(row=0, column=0, **pad)
        self.var_smw_lvl = tk.StringVar(value="")
        ttk.Entry(g2, textvariable=self.var_smw_lvl, width=8).grid(row=0, column=1, **pad)
        ttk.Button(g2, text="설정", command=self.smw_set_level).grid(row=0, column=2, **pad)
        ttk.Label(g2, text="스텝(dB)").grid(row=0, column=3, **pad)
        ttk.Entry(g2, textvariable=self.var_smw_step, width=5).grid(row=0, column=4, **pad)
        ttk.Button(g2, text="▲ 올리기", command=lambda: self.smw_step(+1)).grid(row=0, column=5, **pad)
        ttk.Button(g2, text="▼ 내리기", command=lambda: self.smw_step(-1)).grid(row=0, column=6, **pad)
        ttk.Label(g2, text="최대 허용(dBm)").grid(row=1, column=0, **pad)
        ttk.Entry(g2, textvariable=self.var_smw_max, width=8).grid(row=1, column=1, **pad)
        ttk.Label(g2, text="※ 이 값보다 높게는 프로그램에서 설정하지 않음 (DUT 보호)", foreground="#666").grid(
            row=1, column=2, columnspan=5, sticky="w", **pad)
        self.lbl_smw_read = ttk.Label(f, text="", foreground="#1f4f8f")
        self.lbl_smw_read.pack(anchor="w", pady=4)

    # ------------------------------------------------ 탭: FSW
    def build_fsw_tab(self):
        s = self.s
        f = ttk.Frame(self.nb, padding=10)
        self.nb.add(f, text="  FSW (스펙트럼 분석기)  ")
        pad = dict(padx=4, pady=4)
        g = ttk.LabelFrame(f, text=" 연결 (LAN) ", padding=8)
        g.pack(fill="x")
        ttk.Label(g, text="IP").grid(row=0, column=0, **pad)
        self.var_fsw_host = tk.StringVar(value=s["fsw_host"])
        ttk.Entry(g, textvariable=self.var_fsw_host, width=18).grid(row=0, column=1, **pad)
        ttk.Label(g, text="Port").grid(row=0, column=2, **pad)
        self.var_fsw_port = tk.StringVar(value=str(s["fsw_port"]))
        ttk.Entry(g, textvariable=self.var_fsw_port, width=6).grid(row=0, column=3, **pad)
        self.btn_fsw = ttk.Button(g, text="연결", command=self.fsw_connect, width=10)
        self.btn_fsw.grid(row=0, column=4, **pad)
        self.lbl_fsw_idn = ttk.Label(g, text="(IP에 demo 입력 시 장비 없이 테스트)", foreground="#666")
        self.lbl_fsw_idn.grid(row=1, column=0, columnspan=6, sticky="w", **pad)
        self.var_fsw_gtl = tk.BooleanVar(value=bool(s["fsw_gtl"]))
        ttk.Checkbutton(g, text="읽은 뒤 바로 Local 복귀 (&GTL) - 장비 앞 패널도 계속 조작 가능하게",
                        variable=self.var_fsw_gtl, command=self.apply_worker_opts).grid(
            row=2, column=0, columnspan=6, sticky="w", **pad)
        ttk.Label(g, text="읽기 주기(ms)").grid(row=3, column=0, **pad)
        self.var_fsw_poll = tk.StringVar(value=str(s["fsw_poll"]))
        ttk.Spinbox(g, from_=100, to=5000, increment=100, textvariable=self.var_fsw_poll, width=7,
                    command=self.apply_worker_opts).grid(row=3, column=1, sticky="w", **pad)

        g2 = ttk.LabelFrame(f, text=" 주파수 / 마커  (연결하면 장비에 지금 설정된 값을 불러옴) ", padding=8)
        g2.pack(fill="x", pady=8)
        ttk.Label(g2, text="Center(GHz)").grid(row=0, column=0, **pad)
        self.var_fsw_cent = tk.StringVar(value="")
        ttk.Entry(g2, textvariable=self.var_fsw_cent, width=12).grid(row=0, column=1, **pad)
        ttk.Label(g2, text="Span(MHz)").grid(row=0, column=2, **pad)
        self.var_fsw_span = tk.StringVar(value="")
        ttk.Entry(g2, textvariable=self.var_fsw_span, width=10).grid(row=0, column=3, **pad)
        ttk.Label(g2, text="마커 번호").grid(row=1, column=0, **pad)
        self.var_fsw_mk = tk.StringVar(value=str(s["fsw_marker"]))
        cb = ttk.Combobox(g2, textvariable=self.var_fsw_mk, values=[str(i) for i in range(1, 17)],
                          width=4, state="readonly")
        cb.grid(row=1, column=1, sticky="w", **pad)
        cb.bind("<<ComboboxSelected>>", lambda e: self.fsw_marker_changed())
        ttk.Label(g2, text="마커 주파수(GHz)").grid(row=1, column=2, **pad)
        self.var_fsw_mx = tk.StringVar(value="")
        ttk.Entry(g2, textvariable=self.var_fsw_mx, width=12).grid(row=1, column=3, **pad)
        bf = ttk.Frame(g2)
        bf.grid(row=2, column=0, columnspan=6, sticky="w", pady=(6, 0))
        ttk.Button(bf, text="장비에 적용", command=self.fsw_apply).pack(side="left", padx=3)
        ttk.Button(bf, text="마커 피크 찾기", command=self.fsw_peak).pack(side="left", padx=3)
        ttk.Button(bf, text="장비 값 다시 불러오기", command=self.fsw_reload).pack(side="left", padx=3)
        ttk.Label(g2, text="※ 빈 칸은 장비에 보내지 않음. 마커는 Window 1 기준.", foreground="#666").grid(
            row=3, column=0, columnspan=6, sticky="w", **pad)
        g3 = ttk.LabelFrame(f, text=" 화면 Amplitude (Pulse 측정) ", padding=8)
        g3.pack(fill="x", pady=(0, 8))
        ttk.Label(g3, text="Ref Level").pack(side="left")
        self.var_fsw_rlev = tk.StringVar(value=str(s.get("fsw_rlev", "20")))
        ttk.Entry(g3, textvariable=self.var_fsw_rlev, width=6, justify="right").pack(side="left", padx=4)
        ttk.Label(g3, text="dBm").pack(side="left")
        self.var_fsw_rlev_auto = tk.BooleanVar(value=bool(s.get("fsw_rlev_auto", True)))
        ttk.Checkbutton(g3, text="Pulse 측정 때 자동 적용 (연결 · Pulse 로 바꿀 때 · 자동 측정 시작)",
                        variable=self.var_fsw_rlev_auto).pack(side="left", padx=(12, 6))
        ttk.Button(g3, text="지금 적용", command=lambda: self.fsw_set_rlev(force=True)).pack(side="left")
        g4 = ttk.LabelFrame(f, text=" Pulse / Mod. / DPD 화면 기억 (FSW 설정 파일 - 채널 · 측정 · 트레이스 · 마커 등 화면 전체) ", padding=8)
        g4.pack(fill="x", pady=(0, 8))
        r0 = ttk.Frame(g4)
        r0.pack(fill="x")
        ttk.Label(r0, text="저장 폴더 (FSW 안)").pack(side="left")
        self.var_fsw_sdir = tk.StringVar(value=s.get("fsw_sdir", r"C:\R_S\INSTR\USER"))
        ttk.Entry(r0, textvariable=self.var_fsw_sdir, width=32).pack(side="left", padx=4)
        ttk.Label(r0, text="파일: PABench_Pulse.dfl · PABench_Mod.dfl · PABench_DPD_샘플종류.dfl", foreground="#666").pack(side="left", padx=6)
        r1 = ttk.Frame(g4)
        r1.pack(fill="x", pady=(6, 0))
        for md, nm in (("pulse", "Pulse"), ("mod", "Mod.")):
            ttk.Button(r1, text="📷 지금 화면 → %s 로 저장" % nm, command=lambda md=md: self.fsw_state(md, "save")).pack(side="left", padx=(0, 4))
            ttk.Button(r1, text="%s 화면 불러오기" % nm, command=lambda md=md: self.fsw_state(md, "load")).pack(side="left", padx=(0, 12))
        r2 = ttk.Frame(g4)
        r2.pack(fill="x", pady=(6, 0))
        ttk.Label(r2, text="DPD").pack(side="left", padx=(0, 4))
        self.var_dpd_bw_pick = tk.StringVar(value="now")
        for val, txt in (("now", "지금 대역"), ("100", "100 MHz"), ("400", "400 MHz")):
            ttk.Radiobutton(r2, text=txt, value=val, variable=self.var_dpd_bw_pick).pack(side="left")
        pick_bw = lambda: None if self.var_dpd_bw_pick.get() == "now" else self.var_dpd_bw_pick.get()
        ttk.Button(r2, text="📷 지금 화면 → DPD 로 저장 (지금 샘플 종류)",
                   command=lambda: self.fsw_state(self.dpd_key(bw=pick_bw()), "save")).pack(side="left", padx=(8, 4))
        ttk.Button(r2, text="DPD 화면 불러오기",
                   command=lambda: self.fsw_state(self.dpd_key(bw=pick_bw()) if pick_bw() else (self.dpd_state_key() or self.dpd_key()),
                                                  "load")).pack(side="left", padx=(0, 8))
        ttk.Button(r2, text="📷 → 공통 DPD 로 저장 (종류 상관없이)",
                   command=lambda: self.fsw_state(self.dpd_key(common=True, bw=pick_bw()), "save")).pack(side="left")
        self.var_fsw_state_auto = tk.BooleanVar(value=bool(s.get("fsw_state_auto", True)))
        ttk.Checkbutton(g4, text="Pulse / Mod. / DPD 를 바꿀 때 (DPD 는 샘플 종류 · 100 ↔ 400 MHz 를 바꿀 때도) 저장해 둔 그 화면을 자동으로 불러오기",
                        variable=self.var_fsw_state_auto).pack(anchor="w", pady=(6, 0))
        self.lbl_fsw_state = ttk.Label(g4, text="", foreground="#1f4f8f")
        self.lbl_fsw_state.pack(anchor="w")
        self.fsw_state_label()
        g5 = ttk.LabelFrame(f, text=" Mod. 측정: SA 파워 읽기 (DUT Pout = 이 값 + Out loss) ", padding=8)
        g5.pack(fill="x", pady=(0, 8))
        r5 = ttk.Frame(g5)
        r5.pack(fill="x")
        ttk.Label(r5, text="읽을 값").pack(side="left")
        self.var_fsw_modsrc = tk.StringVar(value=s.get("fsw_modsrc", MOD_SRC[0]) if s.get("fsw_modsrc") in MOD_SRC else MOD_SRC[0])
        cb5 = ttk.Combobox(r5, textvariable=self.var_fsw_modsrc, values=MOD_SRC, state="readonly", width=22)
        cb5.pack(side="left", padx=4)
        cb5.bind("<<ComboboxSelected>>", lambda e: self.fsw_mod_opts())
        ttk.Label(r5, text="직접 SCPI").pack(side="left", padx=(10, 2))
        self.var_fsw_modq = tk.StringVar(value=s.get("fsw_modq", "CALC1:MARK:FUNC:POW:RES? ACP"))
        e5 = ttk.Entry(r5, textvariable=self.var_fsw_modq, width=34)
        e5.pack(side="left")
        e5.bind("<Return>", lambda e: self.fsw_mod_opts())
        ttk.Button(r5, text="적용", command=self.fsw_mod_opts).pack(side="left", padx=4)
        self.lbl_fsw_modq = ttk.Label(g5, text="지금 읽는 명령: (Mod. 모드에서 FSW 연결되면 표시)", foreground="#1f4f8f")
        self.lbl_fsw_modq.pack(anchor="w", pady=(4, 0))
        ttk.Label(g5, text="자동: 5G NR 화면 → 채널 파워 · ACLR 화면 → Tx 채널 파워 (첫 값) · Channel Power 화면 → 그 값 · 그 밖 → 마커\n"
                           "※ 맨 위 SA 칸 · 기록 표 SA 칸에 이 값이 나오는지 확인하세요 (FSW 화면의 Power 와 같아야 함)",
                  foreground="#666", justify="left").pack(anchor="w")
        self.lbl_fsw_read = ttk.Label(f, text="", foreground="#1f4f8f")
        self.lbl_fsw_read.pack(anchor="w", pady=4)

    # ------------------------------------------------ 탭: 오실로스코프 (기존 전류계 그대로)
    def open_plot_window(self):
        win = tk.Toplevel(self.root)
        win.title("%s - 그래프" % APP_NAME)
        win.geometry("640x760")
        panel = PlotPanel(win)
        panel.pack(fill="both", expand=True)
        self.plots.append(panel)
        self.hook_plot(panel)

        def closed():
            if panel in self.plots:
                self.plots.remove(panel)
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", closed)
        win.after(100, self.refresh_plots)

    # ------------------------------------------------ Datasets (샘플 × 주파수)
    @staticmethod
    def ds_key(r):
        return (r.get("sample") or "", freq_key(r))

    @staticmethod
    def ds_label(key):
        smp, fk = key
        f = ("%.6g GHz" % fk) if fk is not None else "주파수 없음"
        return ("%s · %s" % (smp, f)) if smp else f

    def ds_list(self):
        out = []
        for r in self.rows:
            k = self.ds_key(r)
            if k not in out:
                out.append(k)
        return out

    def ds_live(self):
        f = self.smw.get("f") if self.smw.get("conn") else None
        if not f:
            return None
        return (self.cur_sample(), round(f / 1e9, 6))

    def ds_visible(self, key):
        if key in self.ds_hidden:
            return False
        flt = self.var_dsfilter.get().strip().lower() if hasattr(self, "var_dsfilter") else ""
        if flt:
            return all(w in self.ds_label(key).lower() for w in flt.split())
        return True

    def ds_color(self, key):
        st = self.ds_style.get(key, {})
        if st.get("color"):
            return st["color"]
        if self.color_mode == "each":
            vis = [k for k in self.ds_list() if self.ds_visible(k)]
            idx = vis.index(key) if key in vis else self.ds_list().index(key) if key in self.ds_list() else 0
            return distinct_colors(max(len(vis), idx + 1))[idx]
        fc = freq_colors([r for md in ("pulse", "mod") for r in self.rows_by_mode.get(md, [])] + list(self.rows),
                         self.fq_ref())
        return fc.get(key[1], FIXED_FREQ_COLORS[0])

    def ds_shape(self, key):
        """Analyzer 와 같음: 같은 주파수의 여러 측정(샘플)은 기록 순서대로 o · s · ^ · D · v · P"""
        st = self.ds_style.get(key, {})
        if st.get("shape"):
            return st["shape"]
        same = [k for k in self.ds_list() if k[1] == key[1]]
        if len(same) < 2 or key not in same:
            return "o"
        return MEAS_MARKERS[same.index(key) % len(MEAS_MARKERS)]

    def build_datasets(self, parent):
        top = ttk.Frame(parent)
        top.pack(fill="x")
        ttk.Label(top, text="Datasets", font=("맑은 고딕", 9, "bold")).pack(side="left")
        ttk.Label(top, text=" 필터").pack(side="left", padx=(6, 1))
        self.var_dsfilter = tk.StringVar()
        ef = ttk.Entry(top, textvariable=self.var_dsfilter, width=6)
        ef.pack(side="left", fill="x", expand=True)
        self.var_dsfilter.trace_add("write", lambda *a: self.refresh_plots())
        bt = ttk.Frame(parent)
        bt.pack(fill="x", pady=2)
        ttk.Button(bt, text="전체 표시", width=7, command=lambda: self.ds_show_all(True)).pack(side="left")
        ttk.Button(bt, text="숨김", width=4, command=lambda: self.ds_show_all(False)).pack(side="left", padx=1)
        ttk.Button(bt, text="선택 해제", width=7, command=self.ds_clear_sel).pack(side="left")
        tf = ttk.Frame(parent)
        tf.pack(fill="both", expand=True)
        self.dstree = ttk.Treeview(tf, columns=("v", "s", "f", "n"), show="headings", selectmode="extended", height=8)
        for k, t, w in (("v", "표시", 30), ("s", "샘플", 44), ("f", "GHz", 52), ("n", "점", 28)):
            self.dstree.heading(k, text=t)
            self.dstree.column(k, width=w, anchor="center", stretch=(k == "f"))
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.dstree.yview)
        self.dstree.configure(yscrollcommand=sb.set)
        self.dstree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.dstree.bind("<<TreeviewSelect>>", self.on_ds_select)
        self.dstree.bind("<Button-1>", self.on_ds_click, add="+")
        self.dstree.bind("<Button-3>", self.on_ds_menu)
        ttk.Label(parent, text="클릭: 선택(진하게)\nCtrl/Shift: 여러 개\n'표시' 칸: 보이기/숨기기\n"
                               "우클릭: 색·굵기·선·마커\n그래프: Ctrl/Shift+클릭",
                  foreground="#777", font=("맑은 고딕", 8), justify="left").pack(fill="x", pady=(3, 0))
        self.ds_iid = {}   # iid → key
        self._ds_sync = False

    def refresh_datasets(self):
        if not hasattr(self, "dstree"):
            return
        keys = self.ds_list()
        counts = collections.Counter(self.ds_key(r) for r in self.rows)
        live = self.ds_live()
        self._ds_sync = True
        want = {}
        for k in keys:
            iid = "ds_%s_%s" % (k[0], k[1])
            want[iid] = k
        for iid in list(self.dstree.get_children()):
            if iid not in want:
                self.dstree.delete(iid)
        for i, (iid, k) in enumerate(want.items()):
            vals = ("☑" if k not in self.ds_hidden else "☐", k[0] or "-",
                    ("%.6g" % k[1]) if k[1] is not None else "-", counts.get(k, 0))
            tag = "c_%s" % self.ds_color(k).lstrip("#")
            self.dstree.tag_configure(tag, foreground=self.ds_color(k))
            tags = [tag] + (["live"] if k == live else []) + (["filtered"] if not self.ds_visible(k) and k not in self.ds_hidden else [])
            if self.dstree.exists(iid):
                self.dstree.item(iid, values=vals, tags=tags)
                self.dstree.move(iid, "", i)
            else:
                self.dstree.insert("", i, iid=iid, values=vals, tags=tags)
        self.dstree.tag_configure("live", font=("맑은 고딕", 9, "bold"))
        self.dstree.tag_configure("filtered", foreground="#bbbbbb")
        self.ds_iid = want
        sel = [iid for iid, k in want.items() if k in self.ds_selected]
        if tuple(sorted(sel)) != tuple(sorted(self.dstree.selection())):
            self.dstree.selection_set(sel)
        self._ds_sync = False

    def on_ds_select(self, event=None):
        if self._ds_sync:
            return
        self.ds_selected = {self.ds_iid[i] for i in self.dstree.selection() if i in self.ds_iid}
        self.refresh_plots(datasets=False)

    def on_ds_click(self, event):
        if self.dstree.identify_region(event.x, event.y) == "cell" and self.dstree.identify_column(event.x) == "#1":
            iid = self.dstree.identify_row(event.y)
            k = self.ds_iid.get(iid)
            if k is not None:
                if k in self.ds_hidden:
                    self.ds_hidden.discard(k)
                else:
                    self.ds_hidden.add(k)
                self.refresh_plots()
                return "break"

    def ds_show_all(self, show):
        self.ds_hidden = set() if show else set(self.ds_list())
        self.refresh_plots()

    def ds_clear_sel(self):
        self.ds_selected = set()
        self.refresh_plots()

    def on_plot_pick(self, key, additive):
        if key is None:
            if not additive:
                self.ds_selected = set()
        elif additive:
            self.ds_selected ^= {key}
        else:
            self.ds_selected = {key}
        self.refresh_plots()
        if key is not None:   # 선택한 데이터셋으로 목록 스크롤
            for iid, k in self.ds_iid.items():
                if k == key:
                    self.dstree.see(iid)
                    break

    def hook_plot(self, panel):
        for pl in panel.plots():
            pl.on_pick = self.on_plot_pick
            pl.on_point = self.on_plot_point
            pl.on_axis = lambda plot, which, e: self.edit_axis(plot, which, e)

    def on_plot_point(self, row):
        """그래프 점 클릭 → 기록 표에서 그 행으로 스크롤 + 잠깐 노랗게 강조"""
        if not any(r is row for r in self.rows):
            return
        iid = str(row.get("no"))
        try:
            if not self.tree.exists(iid):
                return
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            self.tree.see(iid)
            self.auto_sel = None                 # 사용자가 고른 행
            old = self.tree.item(iid, "tags")
            self.tree.tag_configure("flash", background="#ffe082", foreground="#000")
            self.tree.item(iid, tags=("flash",))
            self.root.after(1500, lambda: self.tree.exists(iid) and self.tree.item(iid, tags=old))
        except tk.TclError:
            return
        self.status("기록 #%s: SG %.2f dBm · DUT Pout %s dBm" % (iid, row.get("sg") or 0, self.fmt(row.get("pout"), 2)), "#39ff88")

    def on_ds_menu(self, event):
        iid = self.dstree.identify_row(event.y)
        if not iid:
            return
        if iid not in self.dstree.selection():
            self.dstree.selection_set(iid)
            self.on_ds_select()
        keys = [self.ds_iid[i] for i in self.dstree.selection() if i in self.ds_iid]
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="색상 바꾸기...", command=lambda: self.ds_pick_color(keys))
        mw = tk.Menu(m, tearoff=0)
        for w in (1, 1.6, 2.5, 3.5, 5):
            mw.add_command(label="%g" % w, command=lambda w=w: self.ds_set_style(keys, width=w))
        m.add_cascade(label="선 굵기", menu=mw)
        md = tk.Menu(m, tearoff=0)
        for name in DASHES:
            md.add_command(label=name, command=lambda n=name: self.ds_set_style(keys, dash=n))
        m.add_cascade(label="선 종류", menu=md)
        mm = tk.Menu(m, tearoff=0)
        for name, r in (("없음", 0), ("작게", 2), ("보통", 3), ("크게", 4.5)):
            mm.add_command(label=name, command=lambda r=r: self.ds_set_style(keys, marker=r))
        m.add_cascade(label="마커 크기", menu=mm)
        ms = tk.Menu(m, tearoff=0)
        for name, sh in (("● 원", "o"), ("■ 사각", "s"), ("▲ 삼각", "^"), ("◆ 마름모", "D"), ("▼ 역삼각", "v"), ("✚ 십자", "P")):
            ms.add_command(label=name, command=lambda sh=sh: self.ds_set_style(keys, shape=sh))
        m.add_cascade(label="마커 모양", menu=ms)
        m.add_separator()
        m.add_command(label="이것만 보기", command=lambda: self.ds_only(keys))
        m.add_command(label="숨기기", command=lambda: (self.ds_hidden.update(keys), self.refresh_plots()))
        m.add_command(label="스타일 기본으로", command=lambda: ([self.ds_style.pop(k, None) for k in keys],
                                                        self.refresh_plots()))
        m.tk_popup(event.x_root, event.y_root)

    def ds_pick_color(self, keys):
        from tkinter import colorchooser
        c = colorchooser.askcolor(color=self.ds_color(keys[0]) if keys else None, title="데이터셋 색상")
        if c and c[1]:
            self.ds_set_style(keys, color=c[1])

    def ds_set_style(self, keys, **kw):
        for k in keys:
            self.ds_style.setdefault(k, {}).update(kw)
        self.refresh_plots()

    def ds_only(self, keys):
        self.ds_hidden = set(self.ds_list()) - set(keys)
        self.refresh_plots()

    def set_color_mode(self, mode):
        """각각색: 지금 보이는 데이터셋마다 다른 색 / 주파수 색: 주파수마다 같은 색. (직접 고른 색은 초기화)"""
        self.color_mode = mode
        for st in self.ds_style.values():
            st.pop("color", None)
        self.refresh_plots()

    def reset_line_styles(self):
        for st in self.ds_style.values():
            for k in ("width", "dash", "marker", "shape"):
                st.pop(k, None)
        self.refresh_plots()

    def init_sashes(self):
        """처음: 왼쪽 칸 470 px, Datasets 목록 230 px (오른쪽), 그래프 : 기록 표 = 약 62 : 38"""
        try:
            self.root.update_idletasks()
            if not getattr(self, "_sash_done", False):
                self.hpan.sashpos(0, int(self.s.get("left_w", 470)))
                self._sash_done = True
            W = self.dsp.winfo_width()
            if W > 300 and (self.dsp.sashpos(0) > W - 60 or self.dsp.sashpos(0) < 100):
                self.dsp.sashpos(0, W - 230)
            H = self.vpan.winfo_height()
            if H > 200 and not getattr(self, "_vsash_done", False):
                self.vpan.sashpos(0, int(H * 0.6))
                self._vsash_done = True
            Wp = self.psu_pw.winfo_width()
            if Wp > 300 and not getattr(self, "_psash_done", False):
                self.psu_pw.sashpos(0, min(560, int(Wp * 0.45)))
                self._psash_done = True
        except (tk.TclError, AttributeError):
            pass

    def plots_reset_view(self):
        for pnl in self.plots:
            try:
                pnl.reset_view()
            except tk.TclError:
                pass

    def build_series(self):
        """그래프용 시리즈 (PAE, Gain). 표시 체크된 데이터셋만, 선택·측정 중은 진하게."""
        groups = collections.OrderedDict()
        for r in self.rows:
            if r.get("pout") is None:
                continue
            groups.setdefault(self.ds_key(r), []).append(r)
        live = self.ds_live()
        sel = self.ds_selected
        pp = getattr(self, "psu_panel", None)
        reading = bool(self.scope.running) if self.mode != "mod" else bool(pp is not None and pp.inst.get("e") is not None and pp.polling)
        live_on = bool(reading or self.sweep) and live in groups and (bool(self.sweep) or live not in self.ds_done)
        s1, s2, xs = [], [], []
        for k, rs in groups.items():
            if not self.ds_visible(k):
                continue
            rs = sorted(rs, key=lambda r: r["sg"] if r.get("sg") is not None else r["pout"])
            st = self.ds_style.get(k, {})
            is_live = live_on and k == live
            base = dict(key=k, label=self.ds_label(k), color=self.ds_color(k),
                        width=st.get("width", 1.6) + (1.2 if is_live else 0), dash=DASHES.get(st.get("dash", "실선")),
                        marker=st.get("marker", 3), shape=self.ds_shape(k),
                        emph=1 if (k in sel or is_live) else 0, live=is_live,
                        # 선택한 것이 있으면 나머지 흐리게, 없고 측정 중이면 측정 중인 곡선만 진하게
                        dim=(bool(sel) and k not in sel) or (not sel and live_on and not is_live))
            if is_live and k not in sel:
                base["label"] += " (측정 중)"
            elif k in self.ds_done:
                base["label"] += " (%s)" % self.ds_done[k]
            ok_ = lambda *v: all(x is not None and math.isfinite(x) for x in v)   # NaN · inf 점은 그리지 않음
            rp = [r for r in rs if ok_(r.get("pout"), r.get("pae"))]
            rg = [r for r in rs if ok_(r.get("pout"), r.get("gain"))]
            pae = [(r["pout"], r["pae"]) for r in rp]
            gain = [(r["pout"], r["gain"]) for r in rg]
            xs += [r["pout"] for r in rs if ok_(r.get("pout"))]
            if pae:
                s1.append(dict(base, pts=pae, rows=rp, what="PAE"))
            if gain:
                s2.append(dict(base, pts=gain, rows=rg, what="Gain"))
        xr = (min(xs), max(xs)) if xs else None
        notes = ("" if s1 else ("Vdd 를 입력하면 PAE 가 표시됩니다" if self.rows else "기록된 데이터가 없습니다"),
                 "" if s2 else "기록된 데이터가 없습니다")
        return s1, s2, xr, notes

    # ------------------------------------------------ RF PA Analyzer 그래프 창 (실시간 연동)
    def analyzer_datasets(self):
        if not self.analyzer.running():
            return []
        # Pulse · Mod. 기록 모두 (지금 모드 것만 보내면, 모드를 바꿀 때 다른 모드 데이터 연결이 끊겼다가
        #  돌아올 때 같은 데이터가 새 Dataset 으로 한 번 더 그려졌음)
        self.rows_by_mode[self.mode] = self.rows
        rows = [r for md in ("pulse", "mod") for r in self.rows_by_mode.get(md, [])]
        t = self.cur_stype()
        if t:   # 샘플 종류를 고르면 그 종류의 기록만 (연결된 .pam 과 함께 표시)
            return self.pa_datasets([r for r in rows if r.get("stype", "") == t])
        return self.pa_datasets(rows)

    def pa_datasets(self, rows_in):
        """Bench 기록 → Analyzer Dataset (샘플 × 주파수). 조건 + 측정 표 (Analyzer 가 열 이름을 자동 인식).
        반환: [(key, 내부 이름, 표시 이름, 셀 표), ...]"""
        groups = collections.OrderedDict()
        for r in rows_in:
            if r.get("pout") is not None:
                groups.setdefault(self.ds_key(r) + (r.get("mode") or "pulse",), []).append(r)
        vdd = fnum(self.var_vdd.get())

        def v(x, nd):
            return "" if x is None else round(x, nd)
        out = []
        for k, rs in groups.items():
            rs = sorted(rs, key=lambda r: r["sg"] if r.get("sg") is not None else r["pout"])
            smp, fk = k[0], k[1]
            cond = []
            if fk is not None:
                cond.append(["FREQ", "%.6g GHz" % fk])
            b = next((r["bias"] for r in rs if r.get("bias")), None) or {}
            vu = next((r["vdd_used"] for r in rs if r.get("vdd_used") is not None), None)
            vd = vu if vu is not None else (b.get("vdd") if (b.get("vdd") is not None and b.get("vdd") == b.get("vdd")) else vdd)
            if vd is not None:
                cond.append(["VDD", round(vd, 2)])
            # 바이어스 (엑셀 맨 위 바이어스 줄과 같은 값) → PA Analyzer 조건 칸 · 그래프 제목 아래에 VG / IQ 로 표시
            for lab, kk, mul, nd in (("VGS,DRV", "vg_drv", 1.0, 3), ("VGS,CA", "vg_ca", 1.0, 3), ("VGS,PK", "vg_pk", 1.0, 3),
                                     ("IQ_DRV", "i_drv", 1e3, 1), ("IQ_CA", "i_ca", 1e3, 1)):
                x = b.get(kk)
                if x is not None and x == x:
                    cond.append([lab, round(x * mul, nd)])
            if smp:
                cond.append(["DUT", smp])
            if rs[0].get("inl") is not None:
                cond.append(["Input Loss", round(rs[0]["inl"], 3)])
            if rs[0].get("outl") is not None:
                cond.append(["Output Loss", round(rs[0]["outl"], 3)])
            rows = cond + [[], ["SG Pout (dBm)", "SA Pin (dBm)", "DUT_Pin (dBm)", "DUT_Pout (dBm)", "Gain",
                                "Id (mA)", "DC_Power(mW)", "DE(%)", "PAE(%)"]]
            for r in rs:
                rows.append([v(r.get("sg"), 3), v(r.get("sa"), 3), v(r.get("pin"), 3), v(r.get("pout"), 3),
                             v(r.get("gain"), 3), v(r.get("id"), 2),
                             "" if r.get("pdc") is None else round(r["pdc"] * 1e3, 2),
                             v(r.get("de"), 3), v(r.get("pae"), 3)])
            # 표시 이름 = 사용자가 정한 샘플 번호만 (종류 · 날짜 없이), Analyzer 가 뒤에 FREQ 를 붙임 "S1 · 3.50 GHz"
            # 내부 이름 = 번호 + 주파수 + 처음 잰 시각 → .pam 에 저장할 때 이번 측정분만 갱신 (예전 측정은 그대로)
            snum = rs[0].get("snum")
            if snum is None:
                st = rs[0].get("stype") or ""
                snum = smp[len(st):].strip() if st and smp.startswith(st) else smp
            # 이 Dataset 을 처음 잰 날짜-시각 → 다른 때 잰 같은 샘플·주파수는 절대 덮어쓰지 않음
            day = min((r.get("date") or "") for r in rs) or datetime.datetime.now().strftime("%Y%m%d-%H%M")
            md = rs[0].get("mode") or "pulse"
            tag = " Mod" if md == "mod" else ""          # Pulse / Mod. 데이터 구분 (같은 .pam 에 같이 저장될 때)
            name = ("%s%s %sGHz %s" % (snum or "Bench", tag, "%.6g" % fk if fk is not None else "?", day)).strip()
            out.append((list(k), name, (snum or "Bench") + tag, rows))
        return out

    # ------------------------------------------------ 연결된 .pam 저장
    def save_type_pam(self, stype, pam, wait=False):
        """그 샘플 종류의 측정을 연결된 .pam 에 저장 (덮어쓰기, 이전 파일은 .pam.bak).
        PA Analyzer 창이 바로 그 .pam 을 열고 있으면 그 창의 내용(실시간 그래프 포함)을 그 파일에 저장,
        아니면 Bench 기록을 .pam 에 직접 합침 (같은 날 같은 샘플·주파수만 교체, 나머지는 그대로)."""
        if not pam or self.stype_pam(stype) != pam:
            return False                       # 종류-파일 연결이 바뀌었으면 저장 안 함 (다른 파일 덮어쓰기 방지)
        a = self.analyzer
        if hasattr(a, "request_save") and a.connected() and a.pam and same_path(a.pam, pam) and a.stype == stype:
            rid = a.request_save(pam, stype)
            if wait:
                rep_ = a.wait_reply(rid, 15.0)
                return self.on_pam_saved(rep_, stype, pam)
            self.pam_pending[rid] = (stype, pam)
            self.status("%s → %s 저장 중..." % (stype, os.path.basename(pam)), "#d4a017")
            return True
        return self.merge_into_pam(stype, pam)

    def on_pam_saved(self, rep_, stype, pam):
        if rep_ and rep_.get("ok"):
            self.pam_dirty.discard(stype)
            self.status("✔ %s 측정 → %s 저장 (이전 파일은 .bak)" % (stype, pam), "#39ff88")
            return True
        # 그래프 창이 저장을 못 했으면(.pam 이 바뀌었거나 오류) Bench 기록으로 직접 저장
        return self.merge_into_pam(stype, pam)

    def poll_pam_replies(self):
        a = self.analyzer
        if not self.pam_pending or not hasattr(a, "take_reply"):
            return
        for rid in list(self.pam_pending):
            rep_ = a.take_reply(rid)
            if rep_ is None and a.running():
                continue
            stype, pam = self.pam_pending.pop(rid)
            self.on_pam_saved(rep_, stype, pam)

    def merge_into_pam(self, stype, pam):
        rows = [r for md in ("pulse", "mod") for r in self.rows_by_mode[md] if r.get("stype", "") == stype]
        sets = self.pa_datasets(rows)
        if not sets:
            self.pam_dirty.discard(stype)
            return True
        try:
            with open(pam, "r", encoding="utf-8") as f:
                doc = json.load(f)
            if doc.get("format") != "PA-Measurement-Analyzer project":
                raise ValueError("PA Analyzer 프로젝트 파일이 아닙니다")
            sheets = doc.setdefault("sheets", [])
            now = datetime.datetime.now().isoformat(timespec="seconds")
            names = []
            for _k, name, disp, cells in sets:
                new = {"name": name, "display": disp, "rows": cells, "freq_override_hz": None,
                       "user_mapping": False, "header_row": None, "mapping": None,
                       "added_at": now, "added_how": "PA Bench Monitor %s" % VERSION, "modified_at": now}
                cands = {name} | set(self.pam_renames.get(name, ()))    # 샘플 번호를 바꾼 경우 예전 이름도
                idx = next((i for i, sh in enumerate(sheets) if sh.get("name") in cands
                            and str(sh.get("added_how", "")).startswith("PA Bench")), None)
                if idx is None:
                    if any(sh.get("name") == name for sh in sheets):   # 이름이 같은 다른 데이터 → 새 이름
                        n = 2
                        while any(sh.get("name") == "%s (%d)" % (name, n) for sh in sheets):
                            n += 1
                        new["name"] = "%s (%d)" % (name, n)
                    sheets.append(new)
                else:
                    new["added_at"] = sheets[idx].get("added_at") or now
                    sheets[idx] = new
                names.append(new["name"])
            st = doc.setdefault("settings", {})
            chk = list(st.get("checked") or [])
            st["checked"] = chk + [n for n in names if n not in chk]
            doc["saved"] = now
            data = json.dumps(doc, ensure_ascii=False)
            tmp = pam + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            try:
                import shutil
                shutil.copy2(pam, pam + ".bak")
            except OSError:
                pass
            os.replace(tmp, pam)
            with open(pam, "r", encoding="utf-8") as f:     # 다시 읽어서 확인
                chk_names = {sh.get("name") for sh in json.load(f).get("sheets", [])}
            if not set(names) <= chk_names:
                raise ValueError("저장 확인 실패")
        except Exception as e:
            messagebox.showerror(APP_NAME, "%s 측정을 연결된 .pam 에 저장하지 못했습니다.\n%s\n\n%s" % (stype, pam, e))
            return False
        self.pam_dirty.discard(stype)
        self.status("✔ %s 측정 %d개 Dataset → %s 저장 (이전 파일은 .bak)" % (stype, len(names), pam), "#39ff88")
        return True

    def ask_save_pams(self):
        """프로그램을 닫을 때: 연결된 .pam 에 저장 안 된 측정이 있으면 저장할지 물어봄. 계속 닫으면 True."""
        job = getattr(self, "_pam_job", None)
        if job:
            try:
                self.root.after_cancel(job)
            except tk.TclError:
                pass
            self._pam_job = None
        todo = [(t, self.stype_pam(t)) for t in sorted(self.pam_dirty) if self.stype_pam(t)]
        a = self.analyzer
        if hasattr(a, "pam") and a.running() and a.pam and a.stype and (a.stype, a.pam) not in todo \
                and self.stype_pam(a.stype) == a.pam:
            todo.append((a.stype, a.pam))       # 그래프 창에서 직접 고친 것도 저장 대상
        if not todo:
            return True
        for t, p in todo:                       # 묻지 않고 자동 저장 (실패할 때만 물어봄)
            if not self.save_type_pam(t, p, wait=True):
                if not messagebox.askyesno(APP_NAME, "%s → %s 저장에 실패했습니다. 그래도 닫을까요?" % (t or "샘플종류없음", p)):
                    return False
        return True

    def dpd_fsw_back(self):
        """DPD 끝에 FSW Mod. 화면을 DPD 주파수 · 반송파 설정 그대로 뒀으면 원래대로 (DPD 쪽이 기억한 값)"""
        d = getattr(self, "dpd", None)
        if d is not None and hasattr(d, "fsw_view_back"):
            try:
                d.fsw_view_back()
            except Exception:
                pass

    def goto_equip(self, key):
        """맨 위 장비 표시를 누르면 그 장비 설정 화면으로 (SMW · FSW · RTO = 장비 탭의 그 장비 · 전원 = 파워서플라이 탭)"""
        if key in ("vdd", "gate"):
            self.set_view("psu")
            return
        if key == "ana":
            try:
                self.open_analyzer()
            except Exception:
                pass
            return
        tabs = {"smw": "SMW200A", "fsw": "FSW", "rto": "RTO2044"}
        self.set_view("equip")
        try:
            for tid in self.nb.tabs():
                if tabs.get(key, "~") in self.nb.tab(tid, "text"):
                    self.nb.select(tid)
                    break
        except Exception:
            pass

    def open_analyzer(self):
        self.analyzer_set_target()
        ok, msg = self.analyzer.open()
        if not ok:
            messagebox.showwarning(APP_NAME, msg)
            return
        self.status(msg, "#39ff88")
        self.analyzer_watch(time.time())
        self.refresh_plots(datasets=False)

    def analyzer_watch(self, t0):
        """창이 뜨는 동안 상태 확인 (연결되면 데이터 전체 전송, 실패하면 알림)."""
        a = self.analyzer
        if a.connected():
            a.sent = {}
            self.refresh_plots(datasets=False)
            self.status("PA Analyzer 그래프 창 연결됨 - 기록할 때마다 자동으로 갱신", "#39ff88")
            return
        if not a.running() or a.error:
            code = a.proc.poll() if a.proc is not None else None
            messagebox.showwarning(APP_NAME, (a.error or "PA Analyzer 그래프 창이 종료되었습니다 (코드 %s)." % code)
                                   + "\n\n로그: PA-Analyzer-graph.log (프로그램 폴더)")
            a.error = ""
            return
        if time.time() - t0 > 90:
            return
        self.root.after(300, lambda: self.analyzer_watch(t0))

    def plot_axes(self):
        out = []
        for vars_ in (self.var_pae_ax, self.var_gain_ax):
            lo, hi, st = (fnum(v.get()) for v in vars_[:3])
            mn = fnum(vars_[3].get()) if len(vars_) > 3 else None
            if lo is None or hi is None or hi <= lo:
                out.append(None)
                continue
            if st is None or st <= 0 or (hi - lo) / st > 100:
                st = (nice_ticks(lo, hi, 6)[1] - nice_ticks(lo, hi, 6)[0]) if len(nice_ticks(lo, hi, 6)) > 1 else (hi - lo)
            out.append((lo, hi, st, mn if (mn and mn > 0 and (hi - lo) / mn <= 400) else None))
        lo, hi, lab, mn = (fnum(v.get()) for v in self.var_x_ax)
        if lo is None or hi is None or hi <= lo:
            out.append(None)
        else:
            if lab is None or lab <= 0 or (hi - lo) / lab > 100:
                lab = 5.0
            out.append((lo, hi, lab, mn if (mn and mn > 0) else None))
        return tuple(out)

    # ------------------------------------------------ 케이블 손실 표 파일
    def loss_save(self):
        if not self.loss_table:
            messagebox.showinfo(APP_NAME, "저장할 손실 데이터가 없습니다.")
            return
        path = filedialog.asksaveasfilename(
            title="케이블 손실 표 내보내기 (txt)", defaultextension=".txt",
            initialdir=self.s.get("loss_dir") or None,
            initialfile="cable_loss_%s.txt" % datetime.datetime.now().strftime("%Y%m%d"),
            filetypes=[("텍스트 파일", "*.txt"), ("모든 파일", "*.*")])
        if not path:
            return
        # 탭 구분 텍스트 → 메모장으로 열기 / 엑셀에 그대로 붙여넣기 가능
        with open(path, "w", encoding="utf-8-sig", newline="\r\n") as f:
            f.write("Freq(GHz)\tIn loss(dB)\tOut loss(dB)\n")
            for fg, li, lo in sorted(self.loss_table):
                f.write("%.6g\t%.3f\t%.3f\n" % (fg, li, lo))
        self.s["loss_dir"] = os.path.dirname(path)
        self.status("케이블 손실 표 저장됨: " + path, "#39ff88")

    def loss_load(self):
        path = filedialog.askopenfilename(
            title="케이블 손실 표 불러오기 (txt)", initialdir=self.s.get("loss_dir") or None,
            filetypes=[("텍스트 파일", "*.txt"), ("CSV", "*.csv"), ("모든 파일", "*.*")])
        if not path:
            return
        rows = []
        try:
            try:
                with open(path, "r", encoding="utf-8-sig") as f:
                    lines = f.read().splitlines()
            except UnicodeDecodeError:  # 메모장 ANSI(cp949) 저장 파일
                with open(path, "r", encoding="cp949", errors="replace") as f:
                    lines = f.read().splitlines()
            for line in lines:
                # 탭 / 공백 / 쉼표 / 세미콜론 구분 모두 허용, 숫자 3개 있는 줄만 사용
                parts = line.replace(",", " ").replace(";", " ").split()
                vals = [fnum(x) for x in parts]
                if len(vals) >= 3 and None not in vals[:3]:
                    rows.append(vals[:3])
        except Exception as e:
            messagebox.showerror(APP_NAME, "파일을 읽지 못했습니다.\n%s" % e)
            return
        if not rows:
            messagebox.showwarning(APP_NAME, "읽을 수 있는 행이 없습니다.\n형식: 주파수(GHz), In loss(dB), Out loss(dB)")
            return
        self.loss_table = rows
        self.refresh_loss_tree()
        self.s["loss_dir"] = os.path.dirname(path)
        self.status("케이블 손실 표 불러옴: %d개 주파수 (%s)" % (len(rows), os.path.basename(path)), "#39ff88")

    def refresh_plots(self, datasets=True):
        if datasets:
            self.refresh_datasets()
        try:
            self.analyzer.sync(self.analyzer_datasets(), self.fq_ref())
        except Exception:
            pass
        s1, s2, xr, notes = self.build_series()
        axes = self.plot_axes()
        for pnl in list(self.plots):
            try:
                pnl.show(s1, s2, xr, notes, axes, getattr(self, "ax_titles", None))
            except Exception as e:
                # 창이 닫힌 그래프만 목록에서 뺌. 그리기 중 오류(값 이상 등)로 메인 그래프가 영영 멈추지 않게
                try:
                    alive = bool(pnl.winfo_exists())
                except tk.TclError:
                    alive = False
                if not alive:
                    self.plots.remove(pnl)
                else:
                    import traceback
                    traceback.print_exc()
                    self.status("그래프 그리기 오류 (다음 기록 때 다시 그림): %s" % e, "#d4a017")

    def psu_connect(self):
        self.psu_panel.connect_all()

    # ------------------------------------------------ 파워서플라이 (E36155A Vdd / N6705C 게이트) 화면
    def build_psu_view(self, parent):
        """오른쪽 '파워서플라이' 화면: 왼쪽 POWER SUPPLY ON/OFF · 설정 | 오른쪽 E36155A · N6705C · 전류 추이"""
        # 왼쪽(가운데) ON/OFF · 설정 (세로로 길게) | 오른쪽 장비 화면 + 그 아래 Vdd 전류 추이
        pw = ttk.PanedWindow(parent, orient="horizontal")
        pw.pack(fill="both", expand=True)
        sfl = self.scrollframe(pw, bgkey="bg", horizontal=False)
        sfr = self.scrollframe(pw, bgkey="bg", horizontal=False)
        pw.add(sfl, weight=1)
        pw.add(sfr, weight=1)
        self.psu_pw = pw
        self._psu_sash_user = False
        pw.bind("<ButtonRelease-1>", lambda e: setattr(self, "_psu_sash_user", True), add="+")
        pw.bind("<Configure>", lambda e: self.root.after(60, self.fit_psu_sash), add="+")
        sfx = self.scrollframe(self.psu_extra, bgkey="bg", horizontal=False)
        sfx.pack(fill="both", expand=True)

        def on_vdd(i, v, t):
            self.psu.update(conn=True, i=i, v=v, t=t)
            if i == i:
                self.psu_samples.append((t, i))
            self.share_live(i, v, t)

        def rf_guard():
            if self.sweep or self.ramp:
                messagebox.showwarning(APP_NAME, "자동 측정 / 천천히 내리기 중에는 바이어스를 바꿀 수 없습니다.")
                return False
            if self.smw.get("conn") and self.smw.get("rf"):
                self.set_rf(False, why="바이어스 켜고 끄기 전 (자동): ")     # 묻지 않고 RF 먼저 OFF
            return True

        def bias_done(msg, ok):
            if ok and getattr(self.psu_panel, "auto_measure", False) and getattr(self, "_asking_xlsx", False):
                self._start_after_xlsx = True               # 엑셀 저장 창을 닫으면 바로 측정 시작
                self.status(msg.replace("\n", " ") + " → 엑셀 저장 위치를 고르면 자동 측정 시작", "#39ff88")
                return
            if ok and getattr(self.psu_panel, "auto_measure", False) and not self.sweep:
                # 바이어스 끝 → 묻지 않고 바로 자동 측정 (Pulse: SG 레벨 → 최대 / Mod.: → 목표 Pout)
                self.status(msg.replace("\n", " ") + " → 자동 측정 시작", "#39ff88")
                self.show_right("plot")
                self.psu_panel.auto_measure = False
                self.toggle_sweep(auto=True)
                return
            # 바이어스만 (또는 실패 · 중지): 창 없이 상태줄. 미리 고른 엑셀 위치는 버림 → 다음 측정 때 다시 물어봄
            self.pre_xlsx = None
            self._xlsx_tok = getattr(self, "_xlsx_tok", 0) + 1
            self._start_after_xlsx = False
            self.psu_panel.auto_measure = False
            self.status(msg.replace("\n", " ") + ("  · RF 는 켜지 않음 - ③ 실행에서 측정 시작" if ok else ""),
                        "#39ff88" if ok else "#d4a017")
        def on_fault(why):
            # 전원 이상: 자동 측정 중지 + SG RF OFF (게이트 기본값 → Vdd 0 V 는 파워서플라이 쪽에서 진행)
            self.emergency_stop()
            self.status("⚠ 전원 이상 → 측정 중지 · RF OFF: " + why.split("\n")[0], "#ff6b6b")
            self.show_right("psu")
        self.psu_panel = psu_panel.App(self.root, container=sfl.body, settings=self.s, on_vdd=on_vdd, mb=messagebox,
                                       rf_guard=rf_guard, on_bias_done=bias_done, on_fault=on_fault,
                                       left=sfl.body, right=sfr.body, extra=sfx.body)
        self.psu_sfr = sfr
        self.psu_panel.on_seq_start = self.on_bias_seq_start
        # 측정 중 Vdd 전류 감시 한계: 지금 화면 (Pulse / Mod. / DPD) 의 값
        self.psu_panel.lim_mode_fn = lambda: "dpd" if self.meas_view() == "dpd" else self.mode
        self.pre_xlsx = None

    def share_live(self, i, v, t):
        """PA DPD Tester 등 다른 프로그램이 Vdd 전류를 쓸 수 있게 0.5초마다 작은 파일로 남김 (읽기 전용 공유)
        %APPDATA%/PABenchMonitor/live.json = {t, vdd, id, bias_state, stype, vg{DRV,CA,PK}}"""
        now = time.time()
        if now - getattr(self, "_live_t", 0) < 0.5:
            return
        self._live_t = now
        pp = getattr(self, "psu_panel", None)
        d = dict(t=t, vdd=v if v == v else None, id=i if i == i else None, pid=os.getpid(), app="PA Bench Monitor " + VERSION,
                 bias_state=getattr(pp, "bias_state", None), stype=self.cur_stype() if hasattr(self, "cur_stype") else "")
        try:
            d["vg"] = {nm: (pp.sets.get(("n", ch), {}).get("v")) for ch, nm in ((1, "DRV"), (2, "CA"), (3, "PK"))}
        except Exception:
            pass
        try:
            path = os.path.join(os.path.dirname(settings_path()), "live.json")
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f)
            os.replace(tmp, path)
        except Exception:
            pass

    def fit_psu_sash(self):
        """파워서플라이 화면: 오른쪽 장비 화면이 잘리지 않게 나눔 (사용자가 직접 옮기면 그대로)"""
        if getattr(self, "_psu_sash_user", False):
            return
        try:
            pw = self.psu_pw
            W = pw.winfo_width()
            if W < 300 or not pw.winfo_ismapped():
                return
            need = self.psu_sfr.body.winfo_reqwidth() + 24
            right = int(min(max(need, W * 0.45), W - 280))
            pw.sashpos(0, max(min(340, W // 2), W - right))     # 왼쪽(ON/OFF · 설정)도 최소 340 px
        except (tk.TclError, AttributeError):
            pass
        self.root.after(200, lambda: self.apply_stype_bias(quiet=True))

    def show_right(self, which):
        """오른쪽 화면: 그래프. 장비 · 파워서플라이는 맨 위 탭 → 그 탭으로 / 'plot' 이면 보던 측정 화면으로 돌아감"""
        if which in self.AUX_VIEWS:
            self.set_view(which)
            return
        which = "plot"
        if getattr(self, "view", None) in self.AUX_VIEWS:
            self.set_view(self.meas_view())
        self.s["right_view"] = which
        self.view_plot.pack(fill="both", expand=True)
        self.var_right.set(which)
        if which == "plot":
            self.plot_tools.pack(side="right")
            self.root.after(80, self.init_sashes)
        else:
            self.plot_tools.pack_forget()
        if which == "psu":
            self.root.after(80, self.fit_psu_sash)

    def build_scope_tab(self):
        f = ttk.Frame(self.nb)
        self.nb.add(f, text="  RTO2044 (전류)  ")
        self.scope = rto.App(self.root, container=f)
        orig = self.scope.show_value
        bench = self

        def hooked(raw, t):
            amps = raw * bench.scope.conv_factor() if math.isfinite(raw) else float("nan")
            # 새로 들어온 파형의 값만 평균·기록·기억에 사용 (트리거가 안 걸려 예전 파형이 반복되면 제외)
            if math.isfinite(amps) and getattr(bench.scope, "value_fresh", True) \
                    and not getattr(bench.scope, "stale", False):
                bench.cur_samples.append((t, amps))
                bench.cur_fresh_t = t
                p_now = bench.smw.get("p")
                if p_now is not None and math.isfinite(p_now) and t - bench.p_change_t > 0.3:
                    bench.cur_fresh_ref = (amps, p_now)   # 새 파형으로 읽힌 전류와 그때의 SG 파워
                bench.learn_current(amps, t)
            bench.cur_last = (t, amps)
            orig(raw, t)

        self.scope.show_value = hooked
        self.cur_last = (0, float("nan"))
        self.cur_fresh_t = 0.0
        self.cur_fresh_ref = None
        # 트리거 모드: Normal 권장 (이 버전에서 한 번만 바꿔 줌, 이후엔 사용자가 고른 값 유지)
        if not self.s.get("v106_normal"):
            self.scope.var_trk_mode.set(rto.TRIG_MODES[1])
            self.s["v106_normal"] = True

    # ------------------------------------------------ 동작
    def apply_worker_opts(self):
        self.smw_w.gtl = self.var_smw_gtl.get()
        self.fsw_w.gtl = self.var_fsw_gtl.get()
        self.smw_w.interval = max(100, fnum(self.var_smw_poll.get(), 300)) / 1000.0
        self.fsw_w.interval = max(100, fnum(self.var_fsw_poll.get(), 300)) / 1000.0

    def connect_all(self):
        if not self.smw["conn"]:
            self.smw_connect()
        if not self.fsw["conn"]:
            self.fsw_connect()
        if not self.scope.connected:
            self.scope.on_connect()
        if hasattr(self, "psu_panel"):
            self.psu_panel.connect_all()

    def toggle_current(self):
        if not self.scope.connected:
            self.status("오실로스코프가 연결되지 않았습니다 - 'RTO2044 (전류)' 탭 확인", "#ff6b6b")
            return
        self.scope.on_run()

    def smw_connect(self):
        if self.smw["conn"]:
            self.smw_w.tasks.put(("disconnect",))
            return
        self.apply_worker_opts()
        self.smw_w.tasks.put(("connect", self.var_smw_host.get(), int(fnum(self.var_smw_port.get(), 5025))))

    def fsw_connect(self):
        if self.fsw["conn"]:
            self.fsw_w.tasks.put(("disconnect",))
            return
        self.apply_worker_opts()
        self.fsw_w.marker = int(self.var_fsw_mk.get())
        self.fsw_w.tasks.put(("connect", self.var_fsw_host.get(), int(fnum(self.var_fsw_port.get(), 5025))))
        self.fsw_set_rlev()                          # Pulse 측정이면 연결하자마자 Ref Level

    def smw_set_level(self, lvl=None):
        if not self.smw["conn"]:
            messagebox.showinfo(APP_NAME, "SMW200A 가 연결되지 않았습니다.")
            return
        if lvl is None:
            lvl = fnum(self.var_smw_lvl.get())
            if lvl is None:
                messagebox.showwarning(APP_NAME, "레벨(dBm)에 숫자를 입력하세요.")
                return
        mx = fnum(self.var_smw_max.get(), 10.0)
        if lvl > mx + 1e-9:
            messagebox.showwarning(APP_NAME, "최대 허용 레벨(%.2f dBm)을 넘습니다." % mx)
            return
        self.var_smw_lvl.set("%.2f" % lvl)
        self.p_change_t = time.time()  # 읽어오기 전에 바로 기록 눌러도 안정화 대기
        self.smw_w.tasks.put(("call", lambda w: w.inst.write("SOUR1:POW %.2f" % lvl)))

    def plot_under_pointer(self):
        try:
            x, y = self.root.winfo_pointerxy()
            w = self.root.winfo_containing(x, y)
        except (tk.TclError, KeyError):
            return None, 0, 0
        if isinstance(w, XYPlot):
            return w, x - w.winfo_rootx(), y - w.winfo_rooty()
        return None, 0, 0

    def on_marker_key(self, event, kind):
        w = event.widget
        plot, px, py = self.plot_under_pointer()
        if plot is None:
            return
        if isinstance(w, (tk.Entry, ttk.Entry, tk.Spinbox, ttk.Spinbox, ttk.Combobox, tk.Text)) and w is not plot:
            plot.focus_set()                         # 마우스가 그래프 위면 그래프 마커가 우선
        if kind == "del":
            plot.delete_marker(px, py)
        elif kind == "delall":
            plot.delete_marker(everything=True)
        elif not plot.add_marker(kind, px, py):
            return
        return "break"

    def on_hotkey(self, event, fn):
        # 입력칸에 숫자를 치는 중이면 단축키로 쓰지 않음
        w = event.widget
        if isinstance(w, (tk.Entry, ttk.Entry, tk.Spinbox, ttk.Spinbox, ttk.Combobox, tk.Text)):
            return
        if self.sweep or self.ramp or self.meas_view() == "dpd":
            return "break"                           # DPD 화면에서는 SG 올리기 · 기록 단축키 안 씀
        now = time.time()
        k = event.keysym.replace("KP_", "")
        if now - self.last_key_t.get(k, 0) < 0.25:  # 같은 키를 누르고 있을 때 연속 입력 방지
            return "break"
        self.last_key_t[k] = now
        fn()
        return "break"

    def record_request(self):
        """기록: 방금 파워를 바꿨으면 안정화+평균 시간이 지난 뒤 기록."""
        g_in, g_sg, diff = self.freq_check()
        p = self.smw.get("p")
        if diff and p is not None and self.rows_at(g_sg, [p]) and self.freq_ok_pair != (g_in, g_sg):
            smp = self.cur_sample()
            ans = self.choice("주파수 확인", "주파수 칸은 %.6g GHz 인데 SG 는 아직 %.6g GHz 입니다.\n"
                                         "([SG + SA 주파수 함께 변경] 을 안 누른 것 같습니다)\n\n"
                                         "%s%.6g GHz · SG %.2f dBm 에 이미 기록이 있어 덮어씁니다.\n주파수가 맞나요?"
                              % (g_in, g_sg, (smp + " · ") if smp else "", g_sg, p),
                              [("SG %.6g GHz 그대로 기록 (덮어쓰기)" % g_sg, "rec"), ("취소", None)])
            if ans != "rec":
                self.status("기록 취소 - 주파수를 확인하세요 ([SG + SA 주파수 함께 변경])", "#d4a017")
                return
            self.freq_ok_pair = (g_in, g_sg)    # 같은 상황에서는 다시 묻지 않음
        settle = max(0.0, fnum(self.var_settle.get(), 1.0))
        avg = max(0.2, fnum(self.var_avg.get(), 1.0))
        ready_t = self.p_change_t + settle + avg
        now = time.time()
        if self.rec_wait is not None:
            self.root.after_cancel(self.rec_wait)
            self.rec_wait = None
        if now >= ready_t:
            self.record(manual=True)
            return
        self.lbl_rec.configure(text="기록 대기... %.1f초 (파워 안정화)" % (ready_t - now))

        def go():
            self.rec_wait = None
            self.lbl_rec.configure(text="")
            if time.time() < self.p_change_t + settle + avg - 0.05:
                self.record_request()  # 그 사이 파워가 또 바뀌었으면 다시 대기
                return
            self.record(manual=True, t1=self.p_change_t + settle, t2=time.time())

        self.rec_wait = self.root.after(int((ready_t - now) * 1000) + 50, go)

    def smw_step(self, sign):
        p = self.smw["p"]
        if p is None or not math.isfinite(p):
            messagebox.showinfo(APP_NAME, "SMW 현재 레벨을 아직 못 읽었습니다.")
            return
        st = fnum(self.var_smw_step.get(), 1.0)
        self.smw_set_level(round(p + sign * st, 2))

    def fsw_mod_opts(self):
        """Mod. 파워 읽기 방법 → FSW 통신 스레드에 알림 (다음 읽기부터)"""
        w = self.fsw_w
        w.mod_src = self.var_fsw_modsrc.get()
        w.mod_custom = self.var_fsw_modq.get()
        w.bench_mode = self.mode
        w.mod_q_ok = False
        self.fsw_samples.clear()

    @staticmethod
    def safe_name(t):
        """FSW 파일 이름 (SCPI 는 영문만): 영문 · 숫자는 그대로, 한글 등이 섞이면 뒤에 이름 고유번호"""
        import zlib
        asc = re.sub(r"[^A-Za-z0-9_-]+", "_", t).strip("_")[:30]
        if asc != t:
            asc = (asc + "_" if asc else "S") + "%06x" % (zlib.crc32(t.encode("utf-8")) & 0xffffff)
        return asc

    def dpd_bw(self):
        """DPD 대역 (100 · 400) - DPD 화면의 대역 선택 (아직 안 열었으면 마지막 저장값)"""
        d = getattr(self, "dpd", None)
        try:
            if d is not None and "bw_mode" in d.v:
                return str(d.v["bw_mode"].get() or "100")
        except Exception:
            pass
        return "100"

    def dpd_key(self, stype=None, bw=None, common=False):
        """DPD 화면 기억 키: 샘플 종류 × 대역 (100 · 400 MHz) 마다 · common = 종류 상관없이 (대역만)"""
        t = "" if common else (self.cur_stype() if stype is None else stype)
        b_ = self.dpd_bw() if bw is None else bw
        return "dpd" + (":" + t if t else "") + "|" + b_

    def fsw_state_path(self, mode):
        d = (self.var_fsw_sdir.get().strip() or r"C:\R_S\INSTR\USER").rstrip("\\/")
        if mode.startswith("dpd"):
            body, _, bw = mode.partition("|")
            t = body[4:] if body.startswith("dpd:") else ""
            return "%s\\PABench_DPD%s%s.dfl" % (d, ("_" + self.safe_name(t)) if t else "", ("_%sM" % bw) if bw else "")
        return "%s\\PABench_%s.dfl" % (d, "Pulse" if mode == "pulse" else "Mod")

    def mode_name(self, mode):
        if mode.startswith("dpd"):
            body, _, bw = mode.partition("|")
            return "DPD%s%s" % ((" %s MHz" % bw) if bw else "", (" (%s)" % body[4:]) if body.startswith("dpd:") else " (공통)")
        return "Pulse" if mode == "pulse" else "Mod."

    def dpd_state_key(self, stype=None):
        """불러올 DPD 화면: 이 샘플 종류로 저장한 것 → 없으면 공통 DPD 화면 → 없으면 None"""
        sv = self.s.get("fsw_state_saved") or {}
        t = self.cur_stype() if stype is None else stype
        bw = self.dpd_bw()
        for k in (self.dpd_key(t, bw), ("dpd:" + t) if t else None, self.dpd_key(common=True, bw=bw), "dpd"):
            if k and sv.get(k):                          # 이 샘플 · 이 대역 → 이 샘플 (예전 저장) → 공통 · 이 대역 → 공통
                return k
        return None

    def fsw_dpd_auto(self):
        """DPD 화면으로 올 때 · DPD 화면에서 샘플 종류를 바꿀 때: 저장해 둔 그 샘플의 DPD FSW 화면 자동으로"""
        self.fsw_state_label()
        if getattr(self, "view", None) != "dpd":
            return
        if not (self.fsw.get("conn") and self.var_fsw_state_auto.get()) or self.dpd_running():
            return
        k = self.dpd_state_key()
        if k:
            self.fsw_state(k, "load", auto=True)

    def fsw_state_label(self):
        sv = self.s.get("fsw_state_saved") or {}
        txt = "  ·  ".join("%s: %s" % (nm, ("저장됨 " + sv[md]) if sv.get(md) else "저장 안 됨")
                           for md, nm in (("pulse", "Pulse"), ("mod", "Mod.")))
        if hasattr(self, "var_stype"):
            t = self.cur_stype()
            parts = []
            for bw in ("100", "400"):
                k = self.dpd_key(t, bw)
                if sv.get(k):
                    parts.append("%s MHz 저장됨 %s" % (bw, sv[k]))
                else:
                    use = None
                    for k2 in (("dpd:" + t) if t else None, self.dpd_key(common=True, bw=bw), "dpd"):
                        if k2 and sv.get(k2):
                            use = k2
                            break
                    parts.append("%s MHz 저장 안 됨%s" % (bw, (" → %s 사용" % self.mode_name(use)) if use else ""))
            txt += "\nDPD (%s): %s" % (t or "샘플 종류 없음", "  ·  ".join(parts))
        if hasattr(self, "lbl_fsw_state"):
            self.lbl_fsw_state.configure(text=txt)

    def fsw_state(self, mode, what, auto=False):
        """FSW 화면 전체를 모드별 설정 파일로 저장 / 불러오기 (MMEM:STOR:STAT · MMEM:LOAD:STAT)"""
        nm = self.mode_name(mode)
        if not self.fsw["conn"]:
            if not auto:
                messagebox.showinfo(APP_NAME, "FSW 가 연결되지 않았습니다.")
            return
        if self.sweep or self.ramp or self.dpd_running():
            if not auto:
                messagebox.showwarning(APP_NAME, "측정 중 (자동 측정 · 천천히 내리기 · DPD) 에는 FSW 화면을 바꾸지 않습니다.")
            return
        path = self.fsw_state_path(mode)
        if what == "save" and not auto:
            if not messagebox.askokcancel(APP_NAME, "지금 FSW 화면 전체를 %s 측정 화면으로 저장할까요?\n\n%s\n(이미 있으면 덮어씀)" % (nm, path)):
                return
        bench = self

        if what == "load":
            self.fsw_w.loading = True                    # 불러오는 동안 · 그 전에 줄 선 읽기도 쉼

        def job(w):
            if getattr(w, "inst", None) is None:
                w.loading = False
                return
            sock = getattr(w.inst, "sock", None)
            old_to = sock.gettimeout() if sock is not None else None
            errs = []
            try:
                if sock is not None:
                    sock.settimeout(40.0)                  # 설정 파일 불러오기는 몇 초 걸림
                w.inst.write("*CLS")
                if what == "save":
                    w.inst.write("MMEM:STOR1:TYPE INST")
                    w.inst.write("MMEM:STOR1:STAT 1,'%s'" % path)
                else:
                    w.inst.write("MMEM:LOAD:STAT 1,'%s'" % path)
                w.inst.query("*OPC?")
                errs = []
                for _ in range(10):
                    e = w.inst.query("SYST:ERR?")
                    if e.startswith("0") or e.startswith("+0"):
                        break
                    errs.append(e)
            finally:
                if sock is not None:
                    sock.settimeout(old_to)
                if what == "load":
                    w.mod_q_ok = False                 # 화면이 바뀜 → Mod. 읽기 명령 다시 고름
                    w.cfg_t = 0
                    w.loading = False
            w.post("call", lambda: bench.fsw_state_done(mode, what, path, errs, auto))
            if what == "load" and mode == "pulse":
                try:
                    fsw_read_cfg(w)
                except Exception:
                    pass
        self.fsw_w.tasks.put(("call", job))
        self.status("FSW %s 화면 %s 중... (%s)" % (nm, "저장" if what == "save" else "불러오기", path), "#ffd54f")

    def fsw_state_done(self, mode, what, path, errs, auto):
        nm = self.mode_name(mode)
        if errs:
            msg = "FSW %s 화면 %s 실패:\n%s\n\n%s" % (nm, "저장" if what == "save" else "불러오기", path, "\n".join(errs))
            self.status(msg.replace("\n", " "), "#ff6b6b")
            if not auto or what == "save":
                messagebox.showwarning(APP_NAME, msg + ("\n\n저장 폴더가 FSW 에 있는지 확인하세요 (🔧 장비 → FSW 탭)." if what == "save" else ""))
            return
        if what == "save":
            self.s.setdefault("fsw_state_saved", {})[mode] = datetime.datetime.now().strftime("%m-%d %H:%M")
            self.fsw_state_label()
        self.fsw_samples.clear()
        if what == "load":
            self.dpd_fsw_reset()
        self.status("FSW %s 화면 %s 완료 (%s)" % (nm, "저장" if what == "save" else "불러오기", path), "#39ff88")

    def dpd_fsw_reset(self):
        """FSW 화면을 통째로 바꿈 → DPD 쪽이 기억하던 FSW 상태 (I/Q 채널 · 원래 ACP 값) 는 버림 → 다음 DPD 실행 때 새로 연결"""
        d = getattr(self, "dpd", None)
        if d is None or self.dpd_running():
            return
        try:
            os.remove(os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "PADPDTester", "fsw_acp_keep.json"))
        except OSError:
            pass
        eng = getattr(d, "engine", None)
        if eng is not None and getattr(eng, "fsw", None) is not None:
            threading.Thread(target=lambda: eng.go_local(close=True), daemon=True).start()

    def fsw_set_rlev(self, force=False):
        """Pulse 측정: FSW 화면 Ref Level (기본 20 dBm)"""
        if not hasattr(self, "var_fsw_rlev"):
            return
        if not force and (self.mode != "pulse" or not self.var_fsw_rlev_auto.get()):
            return
        if not force and self.var_fsw_state_auto.get() and (self.s.get("fsw_state_saved") or {}).get("pulse"):
            return                                   # 저장해 둔 Pulse 화면을 쓰면 그 화면의 Ref Level 그대로
        rl = fnum(self.var_fsw_rlev.get())
        if rl is None or not (-130 <= rl <= 60):
            if force:
                messagebox.showwarning(APP_NAME, "Ref Level 은 -130 ~ 60 dBm 숫자로 입력하세요.")
            return

        def job(w, rl=rl):
            if getattr(w, "inst", None) is None:
                return
            w.inst.write("DISP:WIND1:TRAC:Y:SCAL:RLEV %.2f" % rl)
        self.fsw_w.tasks.put(("call", job))
        self.status("FSW Ref Level %.1f dBm 적용" % rl, "#39ff88")

    def fsw_marker_changed(self):
        n = int(self.var_fsw_mk.get())
        self.fsw_w.tasks.put(("call", lambda w: (setattr(w, "marker", n), fsw_read_cfg(w))))
        self.fsw_samples.clear()

    def fsw_apply(self):
        if not self.fsw["conn"]:
            messagebox.showinfo(APP_NAME, "FSW 가 연결되지 않았습니다.")
            return
        n = int(self.var_fsw_mk.get())
        cent = fnum(self.var_fsw_cent.get())
        span = fnum(self.var_fsw_span.get())
        mx = fnum(self.var_fsw_mx.get())

        def job(w):
            w.marker = n
            if cent is not None:
                w.inst.write("SENS:FREQ:CENT %.6f" % (cent * 1e9))
            if span is not None:
                w.inst.write("SENS:FREQ:SPAN %.6f" % (span * 1e6))
            w.inst.write("CALC1:MARK%d:STAT ON" % n)
            if mx is not None:
                w.inst.write("CALC1:MARK%d:X %.6f" % (n, mx * 1e9))
            w.inst.query("*OPC?")
            errs = []
            for _ in range(10):
                e = w.inst.query("SYST:ERR?")
                if e.startswith("0") or e.startswith("+0"):
                    break
                errs.append(e)
            if errs:
                w.post("error", "FSW 오류: " + " / ".join(errs))
            fsw_read_cfg(w)
            w.post("filled")

        self.fsw_samples.clear()
        self.fsw_w.tasks.put(("call", job))

    def fsw_peak(self):
        if not self.fsw["conn"]:
            return
        n = int(self.var_fsw_mk.get())

        def job(w):
            w.marker = n
            w.inst.write("CALC1:MARK%d:STAT ON" % n)
            w.inst.write("CALC1:MARK%d:MAX" % n)
            w.inst.query("*OPC?")
            fsw_read_cfg(w)
            w.post("fill_fields")

        self.fsw_samples.clear()
        self.fsw_w.tasks.put(("call", job))

    def fsw_reload(self):
        if not self.fsw["conn"]:
            return
        self.fsw_w.tasks.put(("call", lambda w: (fsw_read_cfg(w), w.post("fill_fields"))))

    # ------------------------------------------------ 주파수 / 케이블 손실
    def apply_freq(self):
        g = fnum(self.var_freq.get())
        if g is None or g <= 0:
            messagebox.showwarning(APP_NAME, "주파수(GHz)를 입력하세요.")
            return
        hz = g * 1e9
        done = []
        if self.smw["conn"]:
            self.smw_w.tasks.put(("call", lambda w: w.inst.write("SOUR1:FREQ %.3f" % hz)))
            done.append("SG")
        if self.fsw["conn"]:
            n = int(self.var_fsw_mk.get())

            def job(w):
                w.marker = n
                w.inst.write("SENS:FREQ:CENT %.3f" % hz)
                w.inst.write("CALC1:MARK%d:STAT ON" % n)
                w.inst.write("CALC1:MARK%d:X %.3f" % (n, hz))
                w.inst.query("*OPC?")
                fsw_read_cfg(w)
                w.post("fill_fields")

            self.fsw_w.tasks.put(("call", job))
            done.append("SA")
        self.fsw_samples.clear()
        self.cur_samples.clear()
        self.cur_mem = {}
        self.mem_freq = round(g, 4)
        if done:
            self.status("%s 주파수 → %.6g GHz" % (" + ".join(done), g), "#39ff88")
        else:
            self.status("SG / SA 가 연결되지 않았습니다.", "#ff6b6b")

    def refresh_loss_tree(self):
        self.loss_hl = -1  # 다음 tick 에 하이라이트 다시 그림
        self.loss_table.sort(key=lambda r: r[0])
        self.loss_tree.delete(*self.loss_tree.get_children())
        for i, (fg, li, lo) in enumerate(self.loss_table):
            self.loss_tree.insert("", "end", iid=str(i), values=("%.6g" % fg, "%.2f" % li, "%.2f" % lo))

    def loss_selected(self, event=None):
        sel = self.loss_tree.selection()
        if sel:
            fg, li, lo = self.loss_table[int(sel[0])]
            self.var_lf.set("%.6g" % fg)
            self.var_li.set("%.2f" % li)
            self.var_lo.set("%.2f" % lo)

    def loss_add(self):
        fg, li, lo = fnum(self.var_lf.get()), fnum(self.var_li.get()), fnum(self.var_lo.get())
        if fg is None:
            f_now = self.smw.get("f")
            if f_now:
                fg = f_now / 1e9
                self.var_lf.set("%.6g" % fg)
        if fg is None or li is None or lo is None:
            messagebox.showwarning(APP_NAME, "주파수(GHz), In loss, Out loss 를 숫자로 입력하세요.")
            return
        self.loss_table = [r for r in self.loss_table if abs(r[0] - fg) > 1e-9]
        self.loss_table.append([fg, li, lo])
        self.refresh_loss_tree()

    def loss_del(self):
        sel = self.loss_tree.selection()
        if not sel:
            return
        idx = {int(i) for i in sel}
        self.loss_table = [r for i, r in enumerate(self.loss_table) if i not in idx]
        self.refresh_loss_tree()

    def loss_index(self, hz):
        """가장 가까운 주파수 행 번호 (표가 비었거나 주파수 없음 → None)."""
        if not self.loss_table or hz is None or not math.isfinite(hz):
            return None
        g = hz / 1e9
        return min(range(len(self.loss_table)), key=lambda i: abs(self.loss_table[i][0] - g))

    def losses_at(self, hz):
        """주파수(Hz)의 (In loss, Out loss): 표에서 가장 가까운 주파수 값. 표가 없으면 입력칸 값."""
        i = self.loss_index(hz)
        if i is None:
            return fnum(self.var_li.get(), 0.0), fnum(self.var_lo.get(), 0.0)
        return self.loss_table[i][1], self.loss_table[i][2]

    def update_loss_highlight(self):
        hz = self.smw.get("f") if self.smw.get("conn") else None
        if not hz:
            g = fnum(self.var_freq.get())
            hz = g * 1e9 if (g and g > 0) else None
        i = self.loss_index(hz)
        if i != self.loss_hl:
            for iid in self.loss_tree.get_children():
                self.loss_tree.item(iid, tags=("used",) if (i is not None and iid == str(i)) else ())
            if i is not None:
                self.loss_tree.see(str(i))
            self.loss_hl = i

    def eff_vdd(self, mode=None):
        """효율 (DE · PAE) 계산에 쓸 Vdd → (V, 출처). 파워서플라이 (E36155A) 가 연결돼 있으면 그 값:
        Mod. = 잰 전압 (5초 안), 없으면 설정 전압 · Pulse = 설정 전압 (펄스라 평균으로 잰 전압보다 맞음).
        연결 안 됐을 때만 측정 조건 'Vdd' 칸"""
        mode = mode or self.mode
        pp = getattr(self, "psu_panel", None)
        try:
            if pp is not None and pp.inst.get("e") is not None:
                if mode == "mod":
                    v, t = self.psu.get("v"), self.psu.get("t") or 0
                    if v is not None and v == v and v > 0.5 and time.time() - t < 5:
                        return float(v), "전원 잰 전압"
                sv = (pp.sets.get(("e", None)) or {}).get("v")
                if sv is not None and sv == sv and sv > 0.5:
                    return float(sv), "전원 설정 전압"
                _i, ve, te = pp.e_last                       # 설정값을 아직 못 읽었으면 전원이 잰 전압
                if ve == ve and ve > 0.5 and time.time() - te < 10:
                    return float(ve), "전원 잰 전압"
                v, t = self.psu.get("v"), self.psu.get("t") or 0
                if v is not None and v == v and v > 0.5 and time.time() - t < 10:
                    return float(v), "전원 잰 전압"
        except Exception:
            pass
        return fnum(self.var_vdd.get()), "Vdd 칸"

    def follow_vdd(self):
        """측정 조건 'Vdd' 칸이 파워서플라이 Vdd 를 따라감 (칸을 고치는 중이 아닐 때)"""
        v, src = self.eff_vdd()
        if src == "Vdd 칸" or v is None:
            return
        try:
            if self.root.focus_get() in getattr(self, "_vdd_entries", ()):
                return
        except (KeyError, tk.TclError):
            pass
        cur = fnum(self.var_vdd.get())
        if cur is None or abs(cur - v) > 0.005:
            self.var_vdd.set("%.2f" % v)

    def dut_calc(self, sg, sa, amps, hz, vdd=None):
        """DUT Pin/Pout/Gain/Pdc/DE/PAE 계산. 값이 없으면 None. (vdd: 전원이 잰 전압을 쓸 때)"""
        li, lo = self.losses_at(hz)
        pin = sg - li if (sg is not None and math.isfinite(sg)) else None
        pout = sa + lo if (sa is not None and math.isfinite(sa)) else None
        gain = pout - pin if (pin is not None and pout is not None) else None
        if vdd is None:
            vdd = fnum(self.var_vdd.get())
        pdc = vdd * amps if (vdd is not None and amps is not None and math.isfinite(amps)) else None
        de = pae = None
        min_a = fnum(self.var_minid.get(), 0.0) / 1e3 if hasattr(self, "var_minid") else 0.0
        low_id = amps is not None and math.isfinite(amps) and amps < min_a
        if pdc is not None and pdc > 0 and pout is not None and not low_id:
            de = dbm_to_w(pout) / pdc * 100.0
            if pin is not None:
                pae = (dbm_to_w(pout) - dbm_to_w(pin)) / pdc * 100.0
        ix = self.loss_index(hz)
        lossf = self.loss_table[ix][0] if ix is not None else None
        return dict(inl=li, outl=lo, lossf=lossf, pin=pin, pout=pout, gain=gain, pdc=pdc, de=de, pae=pae)

    # ------------------------------------------------ 기록
    def toggle_rec(self):
        """체크박스: 손으로 파워를 바꿀 때마다 자동 기록."""
        self.rec_on = self.var_autochg.get()
        if self.rec_on:
            self.last_pin = None
            self.change_t = time.time()
            self.pending = True
        else:
            self.pending = False
            self.lbl_rec.configure(text="")

    # ------------------------------------------------ 자동 측정 (SG 레벨 → 최대 허용)
    # ------------------------------------------------ Pulse / Mod. 측정 종류
    def set_mode(self, mode):
        """Pulse / Mod. 전환: 기록 표 · 그래프 · 자동 측정 설정 · SG 신호 파일이 그 모드 것으로 바뀜 (하나만 선택)."""
        if mode == self.mode:
            self.var_mode.set(mode)
            return
        if self.sweep or self.ramp:
            messagebox.showwarning(APP_NAME, "자동 측정 / 천천히 내리기 중에는 측정 종류를 바꿀 수 없습니다.")
            self.var_mode.set(self.mode)
            return
        self.rows_by_mode[self.mode] = self.rows
        self.mode = mode
        self.s["mode"] = mode
        if getattr(self, "view", None) in ("pulse", "mod"):
            self.view = mode
            self.paint_view_tabs()
        self.var_mode.set(mode)
        self.rows = self.rows_by_mode[mode]
        self.var_wv.set(self.wv_paths[mode])
        self.apply_mode_ui()
        self.load_stype_cond()                       # 같은 샘플 종류의 이 모드 ② 조건 · 트리거 설정으로
        self.cur_mem = {}
        self.cur_fresh_ref = None
        self.renumber()
        self.status("측정 종류: %s" % ("Mod. (변조 신호)" if mode == "mod" else "Pulse"), "#39ff88")
        self.root.after(100, self.check_waveform)
        self.fsw_mod_opts()                          # FSW: Mod. 이면 변조 화면 Power, Pulse 면 마커
        if self.fsw.get("conn"):
            if self.var_fsw_state_auto.get() and (self.s.get("fsw_state_saved") or {}).get(mode):
                self.fsw_state(mode, "load", auto=True)   # 저장해 둔 이 모드의 FSW 화면
            elif mode == "pulse":
                self.fsw_set_rlev()

    def apply_mode_ui(self):
        mod = self.mode == "mod"
        if mod:
            self.cond_pulse.pack_forget()
            self.cond_mod.pack(fill="x")
            self.chk_ret.pack_forget()
        else:
            self.cond_mod.pack_forget()
            self.cond_pulse.pack(fill="x")
            if not self.chk_ret.winfo_ismapped():
                self.chk_ret.pack(anchor="w", before=self.chk_ret.master.winfo_children()[1])
        self.card2_refresh()
        v = self.fqv[self.mode]
        for k in ("on", "how", "start", "stop", "step", "list", "wait", "auto"):
            setattr(self, "var_fq_" + k, v[k])
        self.lbl_fq = v["lbl"]
        self.fq_card_refresh()
        if not self.sweep:
            self.btn_sweep.configure(text=("● Mod 자동 측정 시작 (→ 목표 Pout %s dBm)" % self.var_mod_target.get()) if mod else
                                     "● 자동 측정 시작 (SG %s → %s dBm)" % (self.var_lvl2.get() or "레벨", self.var_smw_max.get()),
                                     bg="#2e7d32", activebackground="#388e3c")
        self.update_mode_counts()
        self.update_fq_label()
        self.update_wv_label()
        self.root.after(30, self.fit_ctrl_height)

    def update_mode_counts(self):
        try:
            self.rows_by_mode[self.mode] = self.rows
            for m_, b in self.rec_mode_btns.items():
                b.configure(text="%s %d" % ("Pulse" if m_ == "pulse" else "Mod.", len(self.rows_by_mode[m_])))
        except (AttributeError, tk.TclError):
            pass

    def on_wv_read(self, f, on, note):
        self.smw_wv = (f, on)
        if note:
            self.status("SG 신호: " + note, "#39ff88" if f else "#ff6b6b")
        self.update_wv_label()

    def update_wv_label(self):
        if not hasattr(self, "lbl_wv"):
            return
        if not self.smw.get("conn") or not self.smw_wv:
            self.lbl_wv.configure(text="SG 연결 후 지금 신호 표시", foreground="#666")
            return
        f, on = self.smw_wv
        if not f:
            self.lbl_wv.configure(text="SG 지금 신호를 못 읽음", foreground="#c0504d")
            return
        ok = same_wv(f, self.wv_paths[self.mode])
        self.lbl_wv.configure(text="SG 지금: %s%s%s" % (f.rsplit("/", 1)[-1], "" if on else "  (ARB OFF)",
                                                        "  ✔" if (ok and on) else "  ← 이 모드 신호와 다름"),
                              foreground="#2e7d46" if (ok and on) else "#c0504d")

    def wv_ok(self):
        return bool(self.smw_wv and self.smw_wv[1] and same_wv(self.smw_wv[0], self.wv_paths[self.mode]))

    def apply_waveform(self, path=None, quiet=False):
        """SG 에 신호 파일 적용 (RF 가 켜져 있으면 먼저 RF OFF). 오타는 장비 파일 목록에서 자동 인식."""
        if not self.smw["conn"]:
            if not quiet:
                messagebox.showwarning(APP_NAME, "SG(SMW200A)를 먼저 연결하세요.")
            return False
        path = (path or self.var_wv.get()).strip()
        mode = self.mode
        rf_was = bool(self.smw["rf"])

        def job(w):
            real, note = resolve_wv(w, path)
            if not real:
                w.post("wv_fail", path, note)
                smw_wv_read(w)
                return
            if rf_was:
                w.inst.write("OUTP1:STAT OFF")       # 신호 바꾸는 순간 출력이 튀지 않게
            w.inst.write('SOUR1:BB:ARB:WAV:SEL "%s"' % real)
            w.inst.write("SOUR1:BB:ARB:STAT ON")
            try:
                w.inst.query("*OPC?")
            except Exception:
                pass
            smw_poll(w)
            smw_wv_read(w, ("신호 적용 %s%s%s" % (real.rsplit("/", 1)[-1], (" (자동 인식: %s)" % note) if note else "",
                                                 " · RF OFF" if rf_was else "")))
            w.post("wv_real", mode, real, note)
        self.smw_w.tasks.put(("call", job))
        self.status("SG 신호 적용 중... %s" % path, "#d4a017")
        return True

    def check_waveform(self):
        """모드를 바꿨을 때 SG 신호가 그 모드 파일이 아니면 묻지 않고 바로 바꿈 (RF 가 켜져 있으면 먼저 RF OFF)."""
        if not self.smw["conn"] or not self.smw_wv or self.wv_ok():
            self.update_wv_label()
            return
        if self.sweep or self.ramp:
            return
        self.apply_waveform(quiet=True)
        self.status("%s 측정 → SG 신호를 이 모드 파일로 바꿈: %s" % (
            "Mod." if self.mode == "mod" else "Pulse", self.wv_paths[self.mode].rsplit("/", 1)[-1]), "#39ff88")

    def on_escape(self, event=None):
        if self.meas_view() == "dpd" and self.dpd_running():
            self.dpd_stop_only()                          # DPD 화면: Esc = DPD 측정만 중지 (바이어스 그대로)
            return
        if self.sweep and self.sweep.get("mode") == "mod":
            self.emergency_stop()
        elif self.sweep:
            self.stop_sweep("Esc")

    def emergency_stop(self):
        """긴급 정지: 자동 측정 · 천천히 내리기 즉시 중지 + SG RF OFF (다른 작업보다 먼저)."""
        if self.dpd_running():
            try:
                self.dpd.stop()                          # DPD 측정: 바로 끊고 뒤에서 RF OFF · FSW 원래 화면
            except Exception:
                pass
        if self.sweep:
            self.stop_sweep("긴급 정지", ramp=False, emergency=True)
        if self.ramp:
            self.stop_ramp("긴급 정지")
        if self.smw["conn"]:
            def off(w):
                w.inst.write("OUTP1:STAT OFF")
                smw_poll(w)
            self.smw_w.urgent.put(off)
            self.smw_w.tasks.wake.set()
            self.rf_cmd = (False, time.time())
        self.close_mod_popup()
        self.status("■ 긴급 정지 - SG RF OFF", "#ff6b6b")
        try:
            self.root.bell()
        except tk.TclError:
            pass

    # ------------------------------------------------ Mod. 측정 중 안내 창 (긴급 정지 버튼)
    def open_mod_popup(self):
        self.close_mod_popup()
        if hasattr(self, "var_mod_popup") and not self.var_mod_popup.get():
            return
        win = tk.Toplevel(self.root)
        win.withdraw()
        win.title("Mod. 측정 중")
        win.transient(self.root)
        win.resizable(False, False)
        win.configure(bg="#1b1b1b")
        # 작은 안내 창 (화면 오른쪽 아래): 진행 상황 + 긴급 정지 · 중지
        tk.Label(win, text="● Mod. 측정 중", bg="#1b1b1b", fg="#ffcc33",
                 font=("맑은 고딕", 10, "bold")).pack(padx=10, pady=(6, 0), anchor="w")
        self.mod_lbl = tk.Label(win, text="", bg="#1b1b1b", fg="#e0e0e0", font=("Consolas", 9), justify="left")
        self.mod_lbl.pack(padx=10, pady=2, anchor="w")
        bf = tk.Frame(win, bg="#1b1b1b")
        bf.pack(fill="x", padx=10, pady=(2, 8))
        tk.Button(bf, text="■ 긴급 정지 (RF OFF)", command=self.emergency_stop, bg="#d32f2f", fg="white",
                  activebackground="#f44336", activeforeground="white", font=("맑은 고딕", 10, "bold"),
                  relief="flat", bd=0, padx=8, pady=3).pack(side="left", fill="x", expand=True)
        tk.Button(bf, text="측정 중지", command=lambda: self.stop_sweep("사용자가 중지함"), bg="#455a64", fg="white",
                  relief="flat", bd=0, font=("맑은 고딕", 9), padx=8, pady=3).pack(side="left", padx=(4, 0))
        win.protocol("WM_DELETE_WINDOW", lambda: None)      # 측정 중에는 닫히지 않게
        win.bind("<Escape>", lambda e: self.emergency_stop())
        win.update_idletasks()
        center_window(win, self.root)                # 안내 창은 항상 메인 창 정가운데
        win.deiconify()
        try:
            win.attributes("-topmost", True)
        except tk.TclError:
            pass
        self.mod_win = win
        self.update_mod_popup()

    def close_mod_popup(self):
        w = self.mod_win
        self.mod_win = None
        if w is not None:
            try:
                w.destroy()
            except tk.TclError:
                pass

    def update_mod_popup(self):
        w, sw = self.mod_win, self.sweep
        if w is None:
            return
        if not sw:
            self.close_mod_popup()
            return
        last = self.find_row(self.smw["f"], sw["levels"][sw["i"] - 1]) if sw["i"] > 0 else None
        f = self.fmt
        txt = ("목표 DUT Pout : %s dBm\n"
               "SG 지금      : %s dBm   (%d / %d)\n"
               "마지막 기록  : Pout %s dBm · Gain %s dB\n"
               "               Id %s mA · PAE %s %%" % (
                   self.var_mod_target.get(), f(self.smw.get("p"), 2), min(sw["i"] + 1, len(sw["levels"])),
                   len(sw["levels"]), f(last.get("pout") if last else None, 2), f(last.get("gain") if last else None, 2),
                   f(last.get("id") if last else None, 0), f(last.get("pae") if last else None, 1)))
        try:
            self.mod_lbl.configure(text=txt)
        except tk.TclError:
            pass

    def toggle_sweep(self, auto=False):
        """auto=True: POWER SUPPLY ON 직후 자동 시작 - '시작할까요?' 확인은 건너뜀 (덮어쓰기 · 주파수 문제는 그대로 물어봄)"""
        if self.sweep:
            self.stop_sweep("사용자가 중지함")
            return
        if not self.smw["conn"] or not self.fsw["conn"]:
            messagebox.showwarning(APP_NAME, "SG(SMW200A)와 SA(FSW)를 먼저 연결하세요.")
            return
        if self.ramp:
            messagebox.showinfo(APP_NAME, "천천히 내리는 중입니다. 끝난 뒤 시작하세요.")
            return
        self.dpd_fsw_back()
        mod = self.mode == "mod"
        if auto and not mod and not self.scope.running and self.scope.connected:
            self.toggle_current()                 # 자동 시작: 전류 측정(RTO)도 같이 켬
        if mod:
            start, end, step = fnum(self.var_mod_start.get()), fnum(self.var_mod_max.get()), fnum(self.var_mod_step.get())
            target = fnum(self.var_mod_target.get())
            if start is None or end is None or step is None or step <= 0 or start > end or target is None:
                messagebox.showwarning(APP_NAME, "Mod. 측정: 목표 Pout, 시작 SG ≤ SG 최대(안전), 스텝 > 0 을 입력하세요.")
                return
        else:
            start = fnum(self.var_lvl2.get())
            end = fnum(self.var_smw_max.get())
            step = fnum(self.var_smw_step.get())
            if start is None or end is None or step is None or step <= 0 or start > end:
                messagebox.showwarning(APP_NAME, "SG 레벨(시작) ≤ 최대 허용(끝), 스텝 > 0 으로 입력하세요.")
                return
        # SG 신호 파일이 이 모드(Pulse / Mod.) 것인지 확인
        want_wv = False
        if self.smw_wv is not None and not self.wv_ok():
            cur = (self.smw_wv[0] or "?").rsplit("/", 1)[-1]
            ans = "set" if auto else self.choice("SG 신호 확인", "※ SG 지금 신호가 %s 측정 신호와 다릅니다.\n\n"
                                           "SG 지금 신호: %s%s\n%s 신호: %s\n\n어떻게 할까요?" % (
                                               "Mod." if mod else "Pulse", cur, "" if self.smw_wv[1] else " (ARB OFF)",
                                               "Mod." if mod else "Pulse", self.wv_paths[self.mode].rsplit("/", 1)[-1]),
                              [("신호 바꾸고 시작", "set"), ("지금 신호로 시작", "keep"), ("취소", None)])
            if ans is None:
                return
            if ans == "set":
                if not self.apply_waveform():
                    return
                want_wv = True
        levels = []
        v = start
        while v <= end + 1e-9 and len(levels) < 500:
            levels.append(round(v, 2))
            v += step
        settle = max(0.0, fnum(self.var_settle.get(), 1.0))
        avg = max(0.2, fnum(self.var_avg.get(), 1.0))
        if self.mode == "mod":
            warn = "" if self.psu_panel.inst["e"] is not None else "\n\n※ E36155A 전원이 연결되지 않았습니다 (Id·PAE 빈칸으로 기록)"
        else:
            warn = "" if self.scope.running else "\n\n※ 전류 측정(RTO)이 꺼져 있습니다 (Id·PAE 빈칸으로 기록)"
        if self.var_rf_sweep_on.get():
            warn = ("\n\nRF OFF 상태 → SG %.2f dBm 으로 맞춘 뒤 RF ON 하고 시작" % levels[0]
                    if not self.smw["rf"] else "\n\nRF ON 확인됨") + warn
        info = "SG %.2f → %.2f dBm, %.2g dB 간격, %d점을 자동 측정합니다.\n점당 약 %.1f초 (안정화 %.1f + 평균 %.1f)\n%s%s" % (
            levels[0], levels[-1], step, len(levels), settle + avg + 0.4, settle, avg,
            ("Mod.: DUT Pout %s dBm 에 닿으면 종료 (초과 허용 +%s dB, 넘을 것 같으면 그 전에 멈춤)%s → %s" % (
                self.var_mod_target.get(), self.var_mod_margin.get(),
                (" · 목표 %s dB 전부터 %s dB 스텝" % (self.var_mod_fine_from.get(), self.var_mod_fine_step.get()))
                if self.var_mod_fine_on.get() else "", self.var_mod_after.get())) if mod else
            ("끝나면 %.2f dBm 으로 복귀" % levels[0] if self.var_sw_ret.get() else "끝나도 마지막 레벨 유지"), warn)
        fqv = self.fqv[self.mode]
        freqs = self.fq_list() if fqv["on"].get() else None
        if fqv["on"].get() and not freqs:
            messagebox.showwarning(APP_NAME, "여러 주파수 자동 측정: 주파수를 확인하세요 (시작 · 끝 · 스텝 또는 직접 입력).")
            return
        if freqs:
            # 여러 주파수: 첫 주파수로 SG + SA 를 맞추고 시작, 이미 있는 기록 수 확인
            smp = self.cur_sample()
            n_old = sum(len(self.rows_at(f, levels)) for f in freqs)
            fl = ", ".join("%.6g" % f for f in freqs)
            if mod:
                txt = "주파수 %d개: %s GHz\n주파수마다 SG %.2f dBm → 목표 Pout, 다음 주파수 전 RF OFF 후 %s초 대기 → 시작 SG 에서 RF ON\n\n" % (
                    len(freqs), fl, levels[0], fqv["wait"].get())
            else:
                txt = "주파수 %d개: %s GHz\n주파수마다 SG %.2f → %.2f dBm, 다음 주파수 전 천천히 내린 뒤 %s초 대기\n\n" % (
                    len(freqs), fl, levels[0], levels[-1], fqv["wait"].get())
            if n_old:
                txt = ("※ %s 이 주파수들에 이미 기록 %d점 → 같은 SG 레벨은 새 값으로 덮어씁니다.\n\n" % (smp or "(샘플 없음)", n_old)) + txt
            ans = "go" if (auto and not n_old) else self.choice("여러 주파수 자동 측정", txt + info + "\n\n진행할까요?",
                                                                [("덮어쓰고 시작" if n_old else "시작", "go"), ("취소", None)])
            if ans is None:
                return
            want_g = freqs[0]
            g_sg = self.smw["f"] / 1e9 if self.smw.get("f") else None
            if g_sg is None or abs(g_sg - want_g) > 1e-6:
                self.var_freq.set("%.6g" % want_g)
                self.apply_freq()
        else:
            # 주파수 확인: 주파수 칸만 바꾸고 [SG + SA 주파수 함께 변경] 을 안 눌렀으면 기존 데이터를 덮어쓸 수 있음
            g_in, g_sg, diff = self.freq_check()
            smp = self.cur_sample()
            tag = (smp + " · ") if smp else ""
            want_g = g_sg
            if diff:
                n_old = len(self.rows_at(g_sg, levels))
                n_new = len(self.rows_at(g_in, levels))
                txt = ("※ 주파수 칸은 %.6g GHz 인데 SG 는 아직 %.6g GHz 입니다.\n"
                       "   ([SG + SA 주파수 함께 변경] 을 안 누른 것 같습니다)\n" % (g_in, g_sg))
                if n_old:
                    txt += "   SG 주파수 그대로 하면 %s%.6g GHz 기존 데이터 %d점을 덮어씁니다.\n" % (tag, g_sg, n_old)
                if n_new:
                    txt += "   %s%.6g GHz 에도 이미 %d점 있음 (같은 SG 레벨은 덮어씀)\n" % (tag, g_in, n_new)
                ans = self.choice("자동 측정 - 주파수 확인", txt + "\n" + info + "\n\n어느 주파수로 측정할까요?",
                                  [("%.6g GHz 로 바꾸고 시작 (SG + SA)" % g_in, "new"),
                                   ("SG 지금 %.6g GHz 로 시작%s" % (g_sg, " (덮어쓰기)" if n_old else ""), "cur"),
                                   ("취소", None)])
                if ans is None:
                    return
                if ans == "new":
                    self.apply_freq()
                    want_g = g_in
            else:
                g = g_sg if g_sg is not None else g_in
                n_old = len(self.rows_at(g, levels)) if g is not None else 0
                if n_old:
                    txt = ("※ %s%.6g GHz 에 이미 기록이 %d점 있습니다 → 같은 SG 레벨은 새 값으로 덮어씁니다.\n"
                           "   주파수가 맞나요? (다른 주파수면 주파수 칸 입력 후 [SG + SA 주파수 함께 변경])\n\n" % (tag, g, n_old))
                    ans = self.choice("자동 측정 - 덮어쓰기 확인", txt + info,
                                      [("덮어쓰고 시작", "go"), ("취소", None)])
                else:
                    ans = "go" if auto else self.choice("자동 측정", info + "\n\n진행할까요?", [("시작", "go"), ("취소", None)])
                if ans is None:
                    return
        xlsx = None
        if freqs:
            # 여러 주파수: 시작할 때 엑셀 저장 위치 · 이름 (ON + 측정이면 바이어스 올리는 동안 이미 물어봄)
            if auto and getattr(self, "pre_xlsx", None) is not None:     # ON + 측정 으로 바로 시작할 때만
                xlsx = self.pre_xlsx or None
            else:                                    # 직접 [자동 측정 시작] 은 항상 저장 창
                xlsx = self.ask_sweep_xlsx()
        self.pre_xlsx = None
        self.push_undo("자동 측정 전체")
        self.fsw_set_rlev()                          # Pulse: FSW Ref Level (설정값, 기본 20 dBm)
        self.sweep = {"levels": levels, "i": 0, "phase": "set", "t": 0.0, "confirm": None, "start": levels[0], "xlsx": xlsx,
                      "stype": self.cur_stype(), "pam": self.ensure_today_pam(self.cur_stype()),
                      "need_rf": bool(self.var_rf_sweep_on.get()),
                      "want_f": want_g * 1e9 if want_g is not None else None,
                      "mode": self.mode, "want_wv": want_wv,
                      "target": target if mod else None, "margin": abs(fnum(self.var_mod_margin.get(), 0.3)),
                      "fine": ((abs(fnum(self.var_mod_fine_from.get(), 2.0)), abs(fnum(self.var_mod_fine_step.get(), 0.1)))
                               if (mod and self.var_mod_fine_on.get() and (fnum(self.var_mod_fine_step.get()) or 0) > 0) else None),
                      "after": self.var_mod_after.get(),
                      "freqs": freqs, "fi": 0, "base_levels": list(levels), "bias": self.bias_now()}
        if (self.var_rf_sweep_on.get() and (not self.smw["rf"] or want_wv)) or (want_wv and self.smw["rf"]):
            # RF 꺼져 있음 (또는 신호 파일을 바꾸면서 RF 를 껐음) → SG 시작 레벨로 먼저 맞춘 뒤 RF ON
            self.set_rf(True, level=levels[0], why="자동 측정 시작: ")
            self.sweep["need_rf"] = True
        self.btn_rec.configure(text="■ 자동 측정 중지 (Esc)" if not mod else "■ Mod 측정 중지", bg="#a52a2a")
        self.btn_sweep.configure(text="■ 자동 측정 중지 (Esc)" if not mod else "■ Mod 측정 중지 (Esc)", bg="#c62828",
                                 activebackground="#e53935")
        self.btn_mod.configure(text="■ Mod 측정 중지")
        self.status("%s자동 측정 시작: 최대 %d점" % ("Mod. " if mod else "", len(levels)), "#39ff88")
        if mod:
            self.open_mod_popup()

    def stop_sweep(self, reason, ok=False, ramp=None, emergency=False):
        sw = self.sweep
        self.sweep = None
        if sw:                                     # 그래프: '측정 중' → '측정 완료' / '중지됨'
            for k in sw.get("keys", ()):
                self.ds_done[k] = "측정 완료" if ok else "중지됨"
            self.refresh_plots()
        self.close_mod_popup()
        dlg = getattr(self, "sweep_dialog", None)
        if dlg is not None:
            try:
                dlg.destroy()
            except tk.TclError:
                pass
            self.sweep_dialog = None
        self.btn_rec.configure(bg="#7a1f1f")
        self.btn_mod.configure(text="● Mod 자동 측정 시작")
        self.apply_mode_ui()
        self.lbl_rec.configure(text="")
        self.status(("자동 측정 완료" if ok else "자동 측정 중지") + ": " + reason, "#39ff88" if ok else "#d4a017")
        if sw and ok and not emergency and self.var_auto_bias_off.get():
            self.pending_bias_off = {"t": time.time()}       # SG 다 내리고 RF OFF 뒤 바이어스 OFF
        if sw and sw.get("xlsx") and self.rows:
            if self.export_excel(path=sw["xlsx"]):
                self.status(self.lbl_status.cget("text") + " · 엑셀 저장: " + os.path.basename(sw["xlsx"]), "#39ff88")
        if sw and sw.get("stype") in self.pam_dirty and self.stype_pam(sw.get("stype")):
            # 자동 측정이 끝나면(완료 · 중지 모두) 그 샘플 종류의 오늘 .pam 에 자동 저장 (묻지 않음)
            self.save_type_pam(sw["stype"], self.stype_pam(sw["stype"]))
        if sw and sw.get("mode") == "mod":
            # Mod.: 천천히 내리지 않음. 긴급 정지면 RF OFF 만, 아니면 '종료 후' 설정대로
            if not emergency and self.smw["conn"]:
                after = sw.get("after", MOD_AFTER[0])
                if after == MOD_AFTER[0]:
                    self.smw_w.tasks.put(("call", lambda w, l=sw["start"]: (w.inst.write("SOUR1:POW %.2f" % l), smw_poll(w))))
                    self.p_change_t = time.time()
                    self.status(self.lbl_status.cget("text") + " → SG %.2f dBm" % sw["start"], "#39ff88" if ok else "#d4a017")
                elif after == MOD_AFTER[1]:
                    self.set_rf(False, why="Mod. 측정 종료: ")
            return
        if sw and (self.var_sw_ret.get() if ramp is None else ramp) and self.smw["conn"]:
            # 바로 내리지 않고 현재 파워에서 한 칸씩 천천히 내림
            cur = sw.get("cur", sw["start"])
            self.start_ramp(sw["start"], from_level=cur, why="자동 측정 %s (%s) → " % ("완료" if ok else "중지", reason))

    # ------------------------------------------------ 전류 기억 → 트리거 예상 위치
    def mem_key(self, hz=None):
        hz = self.smw.get("f") if hz is None else hz
        return round(hz / 1e9, 4) if hz else None

    def learn_current(self, amps, t):
        """안정된 상태(파워 바뀐 뒤 안정화 시간 지남)의 전류를 'SG 파워 → 전류'로 기억 (지금 샘플·주파수만)."""
        p = self.smw.get("p")
        if p is None or not math.isfinite(p) or not self.smw.get("rf"):
            return
        settle = max(0.0, fnum(self.var_settle.get(), 1.0))
        if t - self.p_change_t < settle:
            return
        key = self.mem_key()
        if key != self.mem_freq:
            self.reset_memory(quiet=True)
            self.mem_freq = key
        self.cur_mem[round(p, 2)] = amps

    def reset_memory(self, quiet=False):
        self.cur_mem = {}
        self.mem_freq = self.mem_key()
        if not quiet:
            self.status("전류 기억 초기화 (새 샘플 / 주파수)", "#39ff88")

    def predict_current(self, old, new):
        """SG 파워 new 에서의 전류 예상 (A). 기억한 값 → 보간 / 가까운 점들의 기울기 → 마지막 값 기준."""
        m = self.cur_mem if self.mem_freq == self.mem_key() else {}
        k = round(new, 2)
        if k in m:
            return m[k], "기억값"
        lower = sorted(x for x in m if x < k)
        upper = sorted(x for x in m if x > k)
        if lower and upper:
            a, b = lower[-1], upper[0]
            return m[a] + (m[b] - m[a]) * (k - a) / (b - a), "보간"
        side = lower[-2:] if lower else upper[:2]
        near = side[-1] if lower else (side[0] if side else None)
        if near is not None and abs(k - near) > 6.0:
            side = []   # 기억한 점에서 너무 멀면 기울기로 예상하지 않고 직전값 기준으로 대략 예상
        if len(side) == 2 and abs(side[1] - side[0]) > 1e-9:
            a, b = side
            slope = (m[b] - m[a]) / (b - a)
            ref = b if lower else a
            est = m[ref] + slope * (k - ref)
            return max(est, 0.0), "기울기"
        if len(side) == 1 and abs(k - side[0]) <= 6.0:
            ref = side[0]
            d = k - ref
            return m[ref] * 10 ** (d / (20.0 if d < 0 else 40.0)), "1점 기준"
        if self.cur_fresh_ref:
            a, p_ref = self.cur_fresh_ref
            d = max(-20.0, min(20.0, new - p_ref))
            # 내릴 때는 조금 낮게, 올릴 때도 보수적으로 예상 (레벨이 전류보다 높으면 트리거가 안 걸림)
            mult = 10 ** (d / (20.0 if d < 0 else 40.0))
            return a * min(mult, 1.5), "직전값 기준" if abs(d) <= 6 else "대략"   # 크게 올릴 때도 최대 1.5배까지만
        return None, ""

    def scope_idq_raw(self, sc):
        """파워서플라이에서 읽은 지금 Vdd 전류 → 스코프 단위 (모르면 None)"""
        k = sc.conv_factor() or 1.0
        pp = getattr(self, "psu_panel", None)
        try:
            if pp is not None and time.time() - pp.e_last[2] < 10 and pp.e_last[0] == pp.e_last[0] and pp.e_last[0] > 0:
                return pp.e_last[0] / k
        except Exception:
            pass
        return None

    def scope_sg_level(self, p):
        """SG 파워가 저전력 기준(기본 -37 dBm) 아래로 내려오면 스코프 트리거 · 스케일을 잘 잡혔던 낮은 파워 상태로"""
        sc = getattr(self, "scope", None)
        if sc is None or not sc.running:
            self._sg_low = None                         # 전류 측정을 다시 시작하면 처음부터 다시 알림
            return
        if p is None or not math.isfinite(p):
            return
        try:
            thr = float(sc.var_trk_lowp.get())
        except (ValueError, AttributeError, tk.TclError):
            return
        low = p <= thr + 1e-6
        if low == getattr(self, "_sg_low", None):
            return
        self._sg_low = low
        if low:
            sc.worker.tasks.put(("sg_low", self.scope_idq_raw(sc)))
        else:
            sc.worker.tasks.put(("sg_high",))

    def scope_rf_changed(self, rf):
        """SG RF OFF → 전류 화면을 대기 전류(Idq)가 크게 보이는 mA 스케일로 / RF ON → RF 켜져 있던 때 스케일로"""
        sc = getattr(self, "scope", None)
        if sc is None or not sc.running:
            return                                      # (세로 스케일 자동이 꺼져 있으면 스코프 쪽에서 스케일은 안 건드림)
        k = sc.conv_factor() or 1.0
        if rf:
            sc.worker.tasks.put(("rf_on", self.scope_idq_raw(sc)))
            return
        idq = float("nan")
        pp = getattr(self, "psu_panel", None)
        try:
            if pp is not None and time.time() - pp.e_last[2] < 10:
                idq = pp.e_last[0]                      # 파워서플라이에서 읽은 지금 Vdd 전류
        except Exception:
            pass
        if not (idq == idq) or idq <= 0:
            idq = 0.05                                  # 모르면 50 mA 기준
        sc.worker.tasks.put(("rf_idle", max(idq, 0.005) * 1.3 / k))

    def on_sg_change(self, old, new, hz):
        sc = self.scope
        if self.mode == "mod" or not (sc.running and sc.var_trk.get()):
            return
        if self.mem_key(hz) != self.mem_freq:
            self.reset_memory(quiet=True)
        pred, how = self.predict_current(old, new)
        if pred is None:
            return
        start_a = fnum(sc.var_trk_start.get(), 200.0) / 1e3
        if pred < start_a:
            pred, how = start_a, "시작 전류 대기"
        k = sc.conv_factor() or 1.0
        direction = 1 if new > old else -1
        why = "SG %+.2g dB → 예상 %.0f mA (%s)" % (new - old, pred * 1e3, how)
        sc.worker.tasks.put(("trk_hint", pred / k, direction, why))

    # ------------------------------------------------ 자동 측정: 튀는 값 판단
    def find_row(self, hz, sg):
        fk = round(hz / 1e9, 6) if hz else None
        smp = self.cur_sample()
        for r in self.rows:
            if r.get("sg") is not None and abs(r["sg"] - sg) < 0.006 and (r.get("freq") and fk) \
                    and abs(r["freq"] - fk) < 1e-6 and r.get("sample", "") == smp:
                return r
        return None

    def on_sample_change(self):
        """샘플이 바뀌면 전류 기억 새로 시작, 그래프의 '측정 중' 데이터셋도 바뀜."""
        self.cur_mem = {}
        self.cur_fresh_ref = None
        self.s["sample"] = self.var_sample.get()
        if hasattr(self, "var_stype") and not getattr(self, "_snum_loading", False):
            # 샘플 종류마다 마지막 샘플 번호 기억 (종류를 다시 고르면 그 번호로)
            self.s.setdefault("snum_by_type", {})[self.var_stype.get()] = self.var_sample.get()
        if hasattr(self, "lbl_sample"):
            self.lbl_sample.configure(text="기록 이름: %s" % (self.cur_sample() or "(없음)"))

    # ------------------------------------------------ 샘플 종류 (PA Analyzer .pam 연결)
    def cur_stype(self):
        t = (self.var_stype.get() if hasattr(self, "var_stype") else "") or ""
        return "" if t == NO_TYPE else t.strip()

    def cur_sample(self):
        """기록 · 그래프에 쓰는 샘플 이름 = '종류 번호' (예: 'A타입 S1'), 종류 안 고르면 번호만."""
        num = (self.var_sample.get() or "").strip()
        t = self.cur_stype()
        return ("%s %s" % (t, num)).strip() if t else num

    def stype_pam(self, name=None):
        name = self.cur_stype() if name is None else name
        if not name:
            return self.s.get("pam_free") or None          # 샘플 종류 없음
        for t in self.sample_types:
            if t["name"] == name:
                return t.get("pam") or None
        return None

    def set_stype_pam(self, name, path):
        if not name:
            self.s["pam_free"] = path
            return
        t = self.stype_rec(name)
        if t is not None:
            t["pam"] = path
            self.s["sample_types"] = [dict(x) for x in self.sample_types]

    def latest_pam(self, stype):
        """그 샘플 종류의 가장 최근 .pam (연결된 파일 · 같은 폴더의 날짜_종류.pam 중 가장 최근에 저장된 것).
        예전 설정을 불러와도 데이터는 항상 최신 파일에서."""
        import glob
        import re
        cur = self.stype_pam(stype)
        folder = os.path.dirname(cur) if cur else (self.s.get("pam_dir") or
                                                     os.path.join(os.path.expanduser("~"), "Documents", "PA-Bench-pam"))
        safe = "".join(ch if ch not in '\\/:*?"<>|' else "_" for ch in (stype or "샘플종류없음")).strip() or "샘플"
        cands = [cur] if (cur and os.path.isfile(cur)) else []
        try:
            pat = re.compile(r"^\d{8}_%s\.pam$" % re.escape(safe), re.IGNORECASE)
            cands += [f for f in glob.glob(os.path.join(glob.escape(folder), "*.pam")) if pat.match(os.path.basename(f))]
        except (OSError, re.error):
            pass
        if not cands:
            return cur
        best = max(cands, key=lambda f: (os.path.getmtime(f), os.path.basename(f)))
        if not cur or not same_path(best, cur):
            self.set_stype_pam(stype, best)
        return best

    def refresh_latest_pams(self):
        """시작할 때 · 설정을 불러온 뒤: 샘플 종류마다 가장 최근 .pam 으로 연결"""
        names = [t["name"] for t in self.sample_types] + [""]
        changed = []
        for n in names:
            old = self.stype_pam(n)
            new = self.latest_pam(n)
            if new and (not old or not same_path(old, new)):
                changed.append("%s → %s" % (n or "샘플종류없음", os.path.basename(new)))
        if changed:
            self.status("가장 최근 .pam 으로 연결: " + " · ".join(changed), "#39ff88")
        return changed

    def ensure_today_pam(self, stype):
        """오늘 날짜 .pam (날짜_샘플종류.pam) 을 그 샘플 종류에 연결. 날짜가 바뀌어 오늘 처음 재면
        이전 파일은 그대로 두고, 이전 파일 내용을 복사한 새 파일로 이어서 저장."""
        today = datetime.date.today().strftime("%Y%m%d")
        cur = self.latest_pam(stype)
        folder = os.path.dirname(cur) if cur else (self.s.get("pam_dir") or
                                                     os.path.join(os.path.expanduser("~"), "Documents", "PA-Bench-pam"))
        safe = "".join(ch if ch not in '\\/:*?"<>|' else "_" for ch in (stype or "샘플종류없음")).strip() or "샘플"
        want = os.path.join(folder, "%s_%s.pam" % (today, safe))
        if cur and same_path(cur, want) and os.path.isfile(want):
            return want
        try:
            os.makedirs(folder, exist_ok=True)
            if not os.path.isfile(want):
                if cur and os.path.isfile(cur):
                    import shutil
                    shutil.copy2(cur, want)                     # 이전 파일은 그대로, 데이터는 이어서
                    how = "%s 내용을 이어서" % os.path.basename(cur)
                else:
                    doc = {"format": "PA-Measurement-Analyzer project", "version": 1,
                           "saved": datetime.datetime.now().isoformat(timespec="seconds"),
                           "source_file": "", "sheets": [], "settings": {}}
                    with open(want, "w", encoding="utf-8") as f:
                        json.dump(doc, f, ensure_ascii=False)
                    how = "새 파일"
                self.status("오늘 %s .pam 시작: %s (%s)" % (stype or "샘플종류없음", os.path.basename(want), how), "#39ff88")
        except OSError as e:
            self.status("⚠ .pam 파일을 만들 수 없음: %s (%s)" % (want, e), "#ff6b6b")
            return cur
        self.set_stype_pam(stype, want)
        if stype == self.cur_stype() and getattr(self, "analyzer", None) is not None and self.analyzer.running():
            self.analyzer_set_target()                          # 그래프 창도 오늘 파일로
        if hasattr(self, "on_stype_change_label"):
            self.on_stype_change_label()
        return want

    def schedule_pam_save(self, delay=3000):
        """기록 뒤 잠깐 기다렸다가 (연속 기록이면 마지막 뒤) .pam 에 자동 저장 - 묻지 않음"""
        job = getattr(self, "_pam_job", None)
        if job:
            try:
                self.root.after_cancel(job)
            except tk.TclError:
                pass
        self._pam_job = self.root.after(delay, self.flush_pams)

    def flush_pams(self, wait=False):
        self._pam_job = None
        if self.sweep and not wait:
            return True                                         # 자동 측정은 끝날 때 저장
        ok = True
        for t in sorted(self.pam_dirty):
            p = self.stype_pam(t)
            if p:
                ok = self.save_type_pam(t, p, wait=wait) and ok
        return ok

    def fill_stype_combo(self):
        names = [t["name"] for t in self.sample_types]
        self.cb_stype.configure(values=[NO_TYPE] + names)
        if hasattr(self, "cb_stype_top"):
            self.cb_stype_top.configure(values=[NO_TYPE] + names)
            self.cb_stype_top.set(self.var_stype.get())
        if self.var_stype.get() not in names:
            self.var_stype.set(NO_TYPE)
        self.prev_stype = self.var_stype.get()
        self.on_stype_change(quiet=True)

    def on_stype_change(self, quiet=False):
        t = self.cur_stype()
        self.s["stype"] = self.var_stype.get()
        pam = self.stype_pam()
        if not t:
            txt = "종류 없음" + (" · PA Analyzer 창은 전체 기록 연동" if HAS_ANALYZER else "")
        elif pam:
            txt = "연결 .pam: %s%s" % (os.path.basename(pam), "" if os.path.isfile(pam) else "  (파일 없음!)")
        elif HAS_ANALYZER:
            txt = "연결된 .pam 없음 · 이 종류 기록만 연동"
        else:
            txt = "연결된 .pam 없음"
        self.lbl_stype.configure(text=txt, foreground="#c0504d" if (pam and not os.path.isfile(pam)) else "#1f4f8f")
        if hasattr(self, "fqv") and "mod" in self.fqv:
            self.load_stype_freqs()
        if hasattr(self, "COND_KEYS") and hasattr(self, "_cond_loading"):
            self.load_stype_cond()
        self.on_sample_change()
        if not quiet:
            self.status("샘플 종류: %s" % (t or "선택 안 함"), "#39ff88")
        self.apply_stype_bias(quiet=quiet)
        self.analyzer_set_target()
        if not quiet:
            self.refresh_plots()

    # ------------------------------------------------ 샘플 종류별 바이어스 기본값 · 목표 전류
    BIAS_NAMES = {"g1": "DRV", "g2": "CA", "g3": "PK", "vdd": "Vdd", "t_green": "목표 CA", "t_yellow": "목표 DRV", "pk_ext": "외부 PK"}

    def stype_bias(self, name=None):
        name = self.cur_stype() if name is None else name
        for t in self.sample_types:
            if t["name"] == name:
                return t.get("bias") or {}
        return {}

    def apply_stype_bias(self, quiet=False):
        """샘플 종류를 고르면 그 종류의 게이트 기본값 · Vdd · 목표 전류를 파워서플라이 설정에 넣음"""
        pp = getattr(self, "psu_panel", None)
        if pp is None:
            return
        t = self.cur_stype()
        b = self.stype_bias(t)
        changed = pp.set_preset(b, t)
        if not changed or quiet:
            return
        txt = ", ".join("%s %s" % (self.BIAS_NAMES[k], (pp.sq[k].get() + (" mA" if k.startswith("t_") else " V"))
                                   if pp.sq[k].get() else "빈칸") for k in changed)
        if pp.preset_missing():
            self.status("샘플 종류 '%s' 바이어스 기본값 없음 → 파워서플라이 원격 제어 잠금 ([설정] 에서 지정)" % (t or "선택 안 함"),
                        "#ff6b6b")
        else:
            self.status("샘플 종류 '%s' 바이어스 기본값 적용: %s" % (t, txt), "#39ff88")
        if pp.bias_state == "on" or pp.vdd_on():
            messagebox.showinfo(APP_NAME, "샘플 종류 '%s' 바이어스 기본값으로 바꿨습니다.\n%s\n\n"
                                          "지금 켜져 있는 바이어스는 그대로입니다.\n"
                                          "새 값은 다음 POWER SUPPLY OFF → ON 부터 적용됩니다." % (t, txt))

    def save_stype_bias(self):
        """파워서플라이 화면 [💾 샘플 종류 기본값으로 저장]: 지금 설정값 → 지금 샘플 종류"""
        t = self.cur_stype()
        if not t:
            messagebox.showwarning(APP_NAME, "샘플 종류를 먼저 고르세요 (왼쪽 ① 샘플 종류).\n"
                                             "종류가 없으면 [설정] 에서 이름을 추가하세요.")
            return
        pp = self.psu_panel
        d = pp.get_preset()
        for st in self.sample_types:
            if st["name"] == t:
                st["bias"] = d
        self.s["sample_types"] = [dict(x) for x in self.sample_types]
        pp.set_preset(d, t)
        self._autosave_t = 0
        self.autosave_settings()                 # 바로 설정 파일에 저장
        self.status("샘플 종류 '%s' 바이어스 기본값 저장: DRV %s · CA %s · PK %s V, Vdd %s V, 목표 CA %s · DRV %s mA" % (
            t, d["g1"], d["g2"], d["g3"], d["vdd"], d["t_green"], d["t_yellow"]), "#39ff88")

    def analyzer_set_target(self):
        """PA Analyzer 창에 띄울 .pam (종류에 연결된 파일, 없으면 빈 화면)."""
        if not hasattr(self, "analyzer") or not hasattr(self.analyzer, "set_pam"):
            return
        pam = self.latest_pam(self.cur_stype())
        if pam and not os.path.isfile(pam):
            pam = None
        self.analyzer.set_pam(pam, self.cur_stype(), force=True)   # 저장 여부는 Bench 에서 이미 물어봄

    def apply_sample_to_rows(self):
        """[반영]: 샘플 번호 칸의 이름을 기록에 적용 (선택한 행이 있으면 그 행만, 없으면 지금 탭 전체).
        PA Analyzer 그래프 창의 지금 측정 Dataset 이름도 같이 바뀜."""
        new = (self.var_sample.get() or "").strip()
        sel = set(self.tree.selection())
        if sel == {getattr(self, "auto_sel", None)}:
            sel = set()          # 기록할 때 자동으로 선택된 마지막 행 → 사용자가 고른 것 아님
        if sel:
            rows = [r for r in self.rows if str(r["no"]) in sel]
        elif self.rows:
            # 선택 안 함 → 마지막에 기록한 샘플(번호를 안 바꾸고 잰 측정)의 기록 전체
            last = self.rows[-1]
            rows = [r for r in self.rows if (r.get("snum") or "") == (last.get("snum") or "")
                    and (r.get("stype") or "") == (last.get("stype") or "")]
        else:
            rows = []
        if not rows:
            self.status("바꿀 기록이 없습니다.", "#d4a017")
            return
        olds = sorted({r.get("snum") or "" for r in rows})
        if olds == [new]:
            self.status("이미 샘플 번호가 '%s' 입니다." % new, "#39ff88")
            return
        # 바꾼 뒤 같은 샘플 · 주파수 · SG 레벨이 겹치면 안 됨 (하나가 다른 것을 덮어쓰게 됨)
        ids = {id(r) for r in rows}
        others = [r for r in self.rows if id(r) not in ids]
        keyset = set()
        for r in rows + others:
            smp = ("%s %s" % (r.get("stype") or "", new if id(r) in ids else (r.get("snum") or ""))).strip()
            k = (smp, round(r.get("freq") or 0, 6), round(r.get("sg") or 0, 2))
            if k in keyset:
                messagebox.showwarning(APP_NAME, "바꾸면 같은 샘플 · 주파수 · SG 레벨 기록이 겹칩니다 (%s, %.6g GHz, %.2f dBm).\n"
                                                 "겹치는 샘플은 표에서 따로 선택해서 바꾸세요." % k)
                return
            keyset.add(k)
        ans = self.choice("샘플 번호 반영", "%s 기록 %d행의 샘플 번호를 바꿀까요?\n\n   %s  →  %s\n\n"
                                       "(PA Analyzer 그래프 창의 지금 측정 Dataset 이름도 같이 바뀜)" % (
                                           "선택한" if sel else "마지막 샘플", len(rows),
                                           ", ".join(o or "(없음)" for o in olds), new or "(없음)"),
                          [("바꾸기", True), ("취소", None)])
        if not ans:
            return
        self.push_undo("샘플 번호 반영 → '%s'" % new)
        before_disp = {tuple(k): d_ for k, _n, d_, _r in self.pa_datasets(rows)}
        before = {tuple(k): (n, r_) for k, n, _d, r_ in self.pa_datasets(rows)}
        old_keys = {id(r): self.ds_key(r) + (r.get("mode") or "pulse",) for r in rows}
        for r in rows:
            r["snum"] = new
            r["sample"] = ("%s %s" % (r.get("stype") or "", new)).strip()
            if r.get("stype") and self.stype_pam(r["stype"]):
                self.pam_dirty.add(r["stype"])
        after = {tuple(k): (n, d) for k, n, d, _r in self.pa_datasets(rows)}
        moved = {}
        for r in rows:
            moved[old_keys[id(r)]] = self.ds_key(r) + (r.get("mode") or "pulse",)
        remain = {self.ds_key(r) + (r.get("mode") or "pulse",) for r in self.rows if id(r) not in ids}
        for ko, kn in moved.items():
            if ko in remain:
                continue                    # 예전 이름 데이터가 아직 남아 있으면 그쪽은 그대로, 새 이름은 새 Dataset
            if ko in before and kn in after:
                old_name, new_name = before[ko][0], after[kn][0]
                if old_name != new_name:
                    self.pam_renames.setdefault(new_name, set()).add(old_name)
                    self.pam_renames[new_name] |= self.pam_renames.pop(old_name, set())
                if hasattr(self.analyzer, "rekey"):
                    self.analyzer.rekey(list(ko), list(kn), after[kn][1])
                    self.undo_stack[-1]["rekeys"].append((ko, kn, before_disp.get(ko), after[kn][1]))
        # 데이터셋 스타일(색 · 선)도 새 이름으로
        for ko, kn in moved.items():
            ko2, kn2 = ko[:2], kn[:2]
            if ko2 in self.ds_style and kn2 not in self.ds_style:
                self.ds_style[kn2] = self.ds_style.pop(ko2)
        self.renumber()
        self.status("샘플 번호 반영: %d행 → '%s'" % (len(rows), new), "#39ff88")

    def clear_all_rows(self):
        """기록 모두 지움 (Pulse · Mod. 둘 다). PA Analyzer 창 그래프는 그대로."""
        self.push_undo("기록 모두 지움")
        for md in self.rows_by_mode:
            self.rows_by_mode[md] = []
        self.rows = self.rows_by_mode[self.mode]
        self.fcolor_cache.clear()
        self.analyzer.detach_all()
        self.renumber()
        self.reset_memory(quiet=True)

    def top_stype_pick(self):
        """맨 위 [샘플] 칸: ① 카드 샘플 종류와 같은 것 (바꾸면 그 샘플 설정 · DPD 화면 · FSW DPD 화면 으로)"""
        v = self.cb_stype_top.get()
        if v != self.var_stype.get():
            self.var_stype.set(v)
            self.on_stype_select()
        self.cb_stype_top.set(self.var_stype.get())     # 못 바꿨으면 (측정 중) 되돌림

    def on_stype_select(self):
        """샘플 종류 콤보 선택: 묻지 않고 .pam 자동 저장 → 표 비우고 새 종류로."""
        new, old = self.var_stype.get(), getattr(self, "prev_stype", NO_TYPE)
        if new == old:
            return
        if self.sweep or self.ramp:
            messagebox.showwarning(APP_NAME, "자동 측정 / 천천히 내리기 중에는 샘플 종류를 바꿀 수 없습니다.")
            self.var_stype.set(old)
            return
        old_t = "" if old == NO_TYPE else old
        # 묻지 않음: 저장 안 된 측정은 모두 각 종류의 오늘 .pam 에 자동 저장 → 표를 비우고 새 종류로 (Ctrl+Z 로 되돌리기 가능)
        job = getattr(self, "_pam_job", None)
        if job:
            try:
                self.root.after_cancel(job)
            except tk.TclError:
                pass
            self._pam_job = None
        failed = [t or "샘플종류없음" for t in sorted(self.pam_dirty)
                  if self.stype_pam(t) and not self.save_type_pam(t, self.stype_pam(t), wait=True)]
        if failed and not messagebox.askyesno(APP_NAME, ".pam 저장에 실패했습니다: %s\n그래도 표를 비우고 바꿀까요?" % ", ".join(failed)):
            self.var_stype.set(old)
            return
        if self.rows_by_mode["pulse"] or self.rows_by_mode["mod"]:
            self.clear_all_rows()
        self.pam_dirty.discard(old_t)
        self.s.setdefault("snum_by_type", {})[old] = self.var_sample.get()
        self.prev_stype = new
        last = (self.s.get("snum_by_type") or {}).get(new)
        if last is not None and last != self.var_sample.get():
            self._snum_loading = True
            try:
                self.var_sample.set(last)          # 이 종류에서 마지막으로 쓴 샘플 번호
            finally:
                self._snum_loading = False
        self.on_stype_change()
        self.fsw_state_label()
        if getattr(self, "view", None) == "dpd":
            if self.dpd is not None:
                try:
                    self.dpd.follow_sample(self.cur_stype())
                except Exception:
                    pass
            if not (getattr(self, "dpd", None) is not None and self.dpd.v.get("fsw_dfl_auto") is not None
                    and self.dpd.v["fsw_dfl_auto"].get() and self.dpd.s.get("fsw_dfl_last")):
                self.fsw_dpd_auto()                    # DPD 화면에서 샘플 종류 바꿈 → 그 샘플 DPD FSW 화면 (최근 FSW 설정 자동이 켜져 있으면 그대로)

    def edit_sample_types(self):
        """샘플 종류 이름 + 연결할 PA Analyzer .pam 파일 (최대 8개)."""
        win = tk.Toplevel(self.root)
        win.withdraw()
        win.title("샘플 종류 설정")
        win.transient(self.root)
        fr = ttk.Frame(win, padding=12)
        fr.pack(fill="both", expand=True)
        ttk.Label(fr, text="샘플 종류 이름과 PA Analyzer 프로젝트(.pam) 파일을 연결합니다. 기록 이름 = '종류 번호' (예: A타입 S1)\n"
                           "· 자동 측정이 끝나면 그 종류의 측정을 연결된 .pam 에 자동 저장 (이전 파일은 .pam.bak), 프로그램을 닫을 때도 확인\n"
                           + ("· 종류를 고르면 PA Analyzer 그래프 창이 그 .pam 을 열고, 그 종류의 측정을 실시간으로 함께 표시"
                              if HAS_ANALYZER else ""),
                  foreground="#444", justify="left").grid(row=0, column=0, columnspan=5, sticky="w", pady=(0, 8))
        ttk.Label(fr, text="이름", font=("맑은 고딕", 9, "bold")).grid(row=1, column=1, sticky="w")
        if True:
            ttk.Label(fr, text="연결할 .pam 파일", font=("맑은 고딕", 9, "bold")).grid(row=1, column=2, sticky="w")
        BK = ("g1", "g2", "g3", "vdd", "t_green", "t_yellow", "pk_ext")    # pk_ext = 외부 PK 게이트 V (빈칸 = 안 씀)
        for j, (k, t_) in enumerate(zip(BK, ("DRV V", "CA V", "PK V", "Vdd V", "목표 CA mA", "목표 DRV mA", "외부 PK V"))):
            ttk.Label(fr, text=t_, font=("맑은 고딕", 9, "bold"), **({"foreground": "#00897b"} if k == "pk_ext" else {})
                      ).grid(row=1, column=5 + j, sticky="w", padx=2)
        ttk.Label(fr, text="Pulse 주파수 GHz", font=("맑은 고딕", 9, "bold")).grid(row=1, column=13, sticky="w", padx=(8, 2))
        ttk.Label(fr, text="Mod. 주파수 GHz", font=("맑은 고딕", 9, "bold")).grid(row=1, column=14, sticky="w", padx=(4, 2))
        rows = []
        cur = self.sample_types + [{}] * (8 - len(self.sample_types))
        for i, t in enumerate(cur[:8]):
            vn = tk.StringVar(value=t.get("name", ""))
            vp = tk.StringVar(value=t.get("pam", ""))
            vb = {k: tk.StringVar(value=str((t.get("bias") or {}).get(k, ""))) for k in BK}
            for col_, md_ in ((13, "pulse"), (14, "mod")):
                vb["_fq_" + md_] = tk.StringVar(value=self.fq_to_text(self.stype_fq(t, md_)) if t.get("name") else "")
                ttk.Entry(fr, textvariable=vb["_fq_" + md_], width=18).grid(row=2 + i, column=col_, padx=(8 if col_ == 13 else 4, 2))
            ttk.Label(fr, text="%d" % (i + 1)).grid(row=2 + i, column=0, padx=(0, 4))
            ttk.Entry(fr, textvariable=vn, width=14).grid(row=2 + i, column=1, sticky="w", pady=2)
            for j, k in enumerate(BK):
                ttk.Entry(fr, textvariable=vb[k], width=7, justify="right").grid(row=2 + i, column=5 + j, padx=2)

            def now(vb=vb):
                pp = getattr(self, "psu_panel", None)
                if pp is not None:
                    for k, v in pp.get_preset().items():
                        vb[k].set(v)
                    if hasattr(self, "var_pk_ext"):
                        vb["pk_ext"].set(self.var_pk_ext.get().strip())
            ttk.Button(fr, text="지금 값", width=7, command=now).grid(row=2 + i, column=12, padx=(4, 0))
            if True:
                ttk.Entry(fr, textvariable=vp, width=36).grid(row=2 + i, column=2, sticky="ew", padx=4)

                def browse(vp=vp):
                    pth = filedialog.askopenfilename(
                        parent=win, title="PA Analyzer 프로젝트 선택", initialdir=self.s.get("pam_dir") or None,
                        filetypes=[("PA Analyzer 프로젝트", "*.pam"), ("모든 파일", "*.*")])
                    if pth:
                        vp.set(pth)
                        self.s["pam_dir"] = os.path.dirname(pth)
                ttk.Button(fr, text="찾기...", command=browse).grid(row=2 + i, column=3)
                ttk.Button(fr, text="연결 해제", command=lambda vp=vp: vp.set("")).grid(row=2 + i, column=4, padx=(4, 0))
            rows.append((vn, vp, vb))
        ttk.Label(fr, text="바이어스 칸: 이 종류를 고르면 파워서플라이 설정(게이트 기본값 · Vdd · 목표 전류)에 이 값이 그대로 들어감\n"
                           "· 목표 CA · 목표 DRV = 각자 전류 (mA), 둘을 합한 값 = Idq  (예: CA 80 + DRV 40 → Idq 120 mA)\n"
                           "· 외부 PK V = 외부 전원 (GPIB) 피킹 게이트 - 빈칸이면 안 씀, 값이 있는 종류만 켬 (주소: 장비 설정 > 파워서플라이 연결 탭)\n"
                           "· 하나라도 빈칸이면 그 종류는 파워서플라이 원격 제어 잠금 (POWER SUPPLY ON 누르면 '바이어스 값을 지정하세요')\n"
                           "· [지금 값] = 파워서플라이 화면에 지금 들어 있는 값 가져오기 · 설정 파일(설정 저장/불러오기)에도 같이 저장\n"
                           "· 주파수 = 여러 주파수 자동 측정 (Pulse · Mod. 따로) - 직접 입력 3.4, 3.5, 3.6 (개수 제한 없음) 또는 범위 3.4~3.6/0.1",
                  foreground="#444", justify="left").grid(row=11, column=0, columnspan=15, sticky="w", pady=(6, 0))
        bf = ttk.Frame(fr)
        bf.grid(row=12, column=0, columnspan=15, sticky="e", pady=(10, 0))

        def save():
            out, seen = [], set()
            for vn, vp, vb in rows:
                n = vn.get().strip()
                if n and n not in seen and n != NO_TYPE:
                    seen.add(n)
                    bias = {k: v.get().strip() for k, v in vb.items() if v.get().strip() and not k.startswith("_fq_")}
                    fqd = {}
                    for md_ in ("pulse", "mod"):
                        d_ = self.fq_from_text(vb["_fq_" + md_].get())
                        nums = ([d_["start"], d_["stop"], d_["step"]] if d_["how"] == "range" else
                                [x for x in d_["list"].replace(";", ",").replace(" ", ",").split(",") if x.strip()])
                        if any(fnum(x) is None for x in nums):
                            messagebox.showwarning(APP_NAME, "'%s' 의 %s 주파수를 확인하세요: %s\n"
                                                             "(직접 입력: 3.4, 3.5, 3.6 · 범위: 3.4~3.6/0.1)" % (
                                                                 n, "Pulse" if md_ == "pulse" else "Mod.", vb["_fq_" + md_].get()), parent=win)
                            return
                        fqd[md_] = d_
                    for k, v in bias.items():
                        if fnum(v) is None:
                            messagebox.showwarning(APP_NAME, "'%s' 의 %s 값이 숫자가 아닙니다: %s" % (n, self.BIAS_NAMES[k], v), parent=win)
                            return
                    prev = self.stype_rec(n) or {}
                    for md_ in ("pulse", "mod"):            # 주파수 말고 [사용] · 대기 · 자동 진행 기억은 그대로
                        for kk in ("on", "wait", "auto"):
                            if kk in ((prev.get("fq") or {}).get(md_) or {}):
                                fqd[md_][kk] = prev["fq"][md_][kk]
                    rec = {"name": n, "pam": vp.get().strip(), "bias": bias, "fq": fqd}
                    for kk in ("cond", "trig", "cond_pulse", "cond_mod", "trig_pulse", "trig_mod"):   # ② 조건 · 트리거 기억은 그대로
                        if prev.get(kk):
                            rec[kk] = prev[kk]
                    out.append(rec)
            self.sample_types = out
            self.s["sample_types"] = out
            win.destroy()
            self.fill_stype_combo()
            self.apply_stype_bias()
            self.load_stype_freqs()
            self.refresh_plots()
            self.status("샘플 종류 %d개 저장" % len(out), "#39ff88")
        ttk.Button(bf, text="저장", command=save).pack(side="left", padx=4)
        ttk.Button(bf, text="취소", command=win.destroy).pack(side="left", padx=4)
        center_window(win, self.root)
        win.deiconify()
        win.grab_set()

    def check_anomaly(self, row, sw):
        """방금 기록한 점이 이상하면 이유 문자열, 정상이면 "" (직전 두 점 기준)."""
        if row is None:
            return "기록 실패"
        i = sw["i"]
        hz = self.smw["f"]
        p1 = self.find_row(hz, sw["levels"][i - 1]) if i > 0 else None
        p2 = self.find_row(hz, sw["levels"][i - 2]) if i >= 2 else None
        lim = fnum(self.var_retry_minid.get(), 0.0) or 0.0
        if row.get("id") is None:
            # 직전 점이 무시 기준(예: 250 mA) 아래였으면 아직 낮은 전류 구간 → 다시 재지 않음
            if lim and p1 is not None and p1.get("id") is not None and p1["id"] < lim:
                return ""
            return "전류 못 읽음"
        if lim and row["id"] < lim:
            return ""          # 이 전류 아래는 튀어도 무시 (효율 계산도 안 하는 구간)
        if i == 0:
            return ""
        if lim:                # 비교 기준 점도 무시 기준 이상인 것만
            if p1 is not None and (p1.get("id") is None or p1["id"] < lim):
                p1, p2 = None, None
            if p2 is not None and (p2.get("id") is None or p2["id"] < lim):
                p2 = None
        tol_id = max(1.0, fnum(self.var_tol_id.get(), 15.0)) / 100.0
        tol_pae = max(0.5, fnum(self.var_tol_pae.get(), 5.0))
        if p1 is not None and p1.get("id") is not None:
            if row["id"] < p1["id"] * 0.95:
                return "전류 감소 (%.0f → %.0f mA)" % (p1["id"], row["id"])
            if p2 is not None and p2.get("id") is not None:
                exp = p1["id"] + (p1["id"] - p2["id"])
                if exp > 0 and abs(row["id"] - exp) > tol_id * exp:
                    return "전류 튐 (예상 %.0f, 측정 %.0f mA)" % (exp, row["id"])
        if row.get("pae") is not None and p1 is not None and p1.get("pae") is not None:
            if p2 is not None and p2.get("pae") is not None:
                exp = 2 * p1["pae"] - p2["pae"]
                if abs(row["pae"] - exp) > tol_pae:
                    return "PAE 튐 (예상 %.1f, 측정 %.1f %%)" % (exp, row["pae"])
            elif row["pae"] < p1["pae"] - tol_pae:
                return "PAE 감소 (%.1f → %.1f %%)" % (p1["pae"], row["pae"])
        return ""

    # ------------------------------------------------ 천천히 내리기
    # ------------------------------------------------ SG RF 출력
    def set_rf(self, on, level=None, why=""):
        """RF ON/OFF. level 을 주면 먼저 그 레벨로 맞춘 뒤 켬 (같은 작업 순서로 보내 순서 보장)."""
        if not self.smw["conn"]:
            self.status("SG 가 연결되지 않았습니다.", "#ff6b6b")
            return False

        def job(w, on=on, level=level):
            if on and level is not None:
                w.inst.write("SOUR1:POW %.2f" % level)
            w.inst.write("OUTP1:STAT %s" % ("ON" if on else "OFF"))
            smw_poll(w)                    # 바로 다시 읽어서 화면 갱신
        self.smw_w.tasks.put(("call", job))
        if on and level is not None:
            self.p_change_t = time.time()
        self.rf_cmd = (on, time.time())
        self.status(why + "SG RF %s%s" % ("ON" if on else "OFF",
                                          (" (SG %.2f dBm)" % level) if (on and level is not None) else ""),
                    "#39ff88" if on else "#d4a017")
        return True

    def toggle_rf(self):
        if not self.smw["conn"]:
            messagebox.showwarning(APP_NAME, "SG(SMW200A)를 먼저 연결하세요.")
            return
        if self.smw["rf"]:
            if self.sweep:
                if not messagebox.askyesno(APP_NAME, "자동 측정 중입니다. 측정을 중지하고 RF 를 끌까요?"):
                    return
                self.stop_sweep("RF OFF", ramp=False)
            if self.ramp:
                self.stop_ramp("RF OFF")
            self.set_rf(False)
            return
        p = self.smw.get("p")
        lvl = fnum(self.var_lvl2.get())
        msg = "SG RF 를 켭니다.\n\n현재 SG 레벨: %s dBm" % ("%.2f" % p if p is not None else "?")
        if lvl is not None and p is not None and p > lvl + 0.01:
            # 높은 레벨에서 갑자기 켜지지 않게: SG 레벨 칸 값으로 먼저 낮춰서 켜는 것을 기본으로
            ans = messagebox.askyesnocancel(
                APP_NAME, msg + "  (SG 레벨 칸 %.2f dBm 보다 높음)\n\n"
                                "[예] SG 레벨 %.2f dBm 으로 낮춘 뒤 켜기\n[아니요] 현재 레벨 그대로 켜기" % (lvl, lvl))
            if ans is None:
                return
            self.set_rf(True, level=lvl if ans else None)
            return
        self.set_rf(True)

    def update_rf_button(self):
        if not hasattr(self, "btn_rf"):
            return
        rf = bool(self.smw["rf"]) if self.smw["conn"] else None
        cmd = self.rf_cmd
        if cmd and (rf == cmd[0] or time.time() - cmd[1] > 5):
            self.rf_cmd = cmd = None
        top = getattr(self, "btn_rf_top", None)
        if not self.smw["conn"]:
            self.btn_rf.configure(text="RF (연결 안 됨)", bg=THEME["head"], fg=THEME["unit"], activebackground=THEME["head"])
            self.lbl_rf.configure(text="")
            want = ("RF -", "#37474f", "#90a4ae")
        elif cmd:
            self.btn_rf.configure(text="RF %s ..." % ("ON" if cmd[0] else "OFF"), bg="#d4a017", fg="white",
                                  activebackground="#d4a017")
            want = ("RF %s ..." % ("ON" if cmd[0] else "OFF"), "#d4a017", "white")
        elif rf:
            self.btn_rf.configure(text="● RF ON", bg="#2e7d46", fg="white", activebackground="#3a9a58")
            self.lbl_rf.configure(text="SG %.2f dBm 출력 중" % self.smw["p"] if self.smw.get("p") is not None else "")
            want = ("● RF ON", "#2e7d46", "white")
        else:
            self.btn_rf.configure(text="○ RF OFF", bg="#5a5a5a", fg="white", activebackground="#6a6a6a")
            self.lbl_rf.configure(text="")
            want = ("○ RF OFF", "#5a5a5a", "white")
        if top is not None and top.cget("text") != want[0]:
            top.configure(text=want[0], bg=want[1], fg=want[2], activebackground=want[1])

    def toggle_ramp(self):
        if self.ramp:
            self.stop_ramp("사용자가 중지함 (현재 레벨 유지)")
            return
        if self.sweep:
            messagebox.showinfo(APP_NAME, "자동 측정 중입니다. 먼저 자동 측정을 중지하세요 (중지하면 자동으로 천천히 내려감).")
            return
        target = fnum(self.var_lvl2.get())
        if target is None:
            messagebox.showwarning(APP_NAME, "SG 레벨(dBm) 칸에 내려갈 목표 레벨을 입력하세요.")
            return
        self.start_ramp(target, rf_off=self.var_rf_ramp_off.get())

    def start_ramp(self, target, from_level=None, why="", rf_off=None):
        if rf_off is None:
            rf_off = self.var_rf_ramp_off.get()
        if not self.smw["conn"]:
            self.status("SG 가 연결되지 않아 내릴 수 없습니다.", "#ff6b6b")
            return
        cur = from_level if from_level is not None else self.smw.get("p")
        if cur is None or not math.isfinite(cur):
            self.status("SG 현재 레벨을 아직 못 읽었습니다.", "#ff6b6b")
            return
        if cur <= target + 1e-9:
            self.status(why + "이미 SG %.2f dBm (목표 %.2f dBm 이하)" % (cur, target), "#39ff88")
            if rf_off and self.smw["rf"]:
                self.set_rf(False, why=why + "SG %.2f dBm · " % cur)
            return
        self.ramp = {"target": target, "level": cur, "next": time.time() + 0.05, "why": why, "rf_off": rf_off}
        self.btn_ramp.configure(text="■ 천천히 내리기 중지")
        self.status(why + "천천히 내리는 중: %.2f → %.2f dBm" % (cur, target), "#d4a017")

    def stop_ramp(self, reason, ok=False):
        r = self.ramp
        self.ramp = None
        self.btn_ramp.configure(text="▼▼ 천천히 내리기 (현재 → SG 레벨)")
        self.lbl_rec.configure(text="")
        self.status((r["why"] if r else "") + ("천천히 내리기 완료: " if ok else "천천히 내리기 중지: ") + reason,
                    "#39ff88" if ok else "#d4a017")

    def ramp_step(self):
        r = self.ramp
        if not r:
            return
        if not self.smw["conn"]:
            self.stop_ramp("SG 연결 끊김")
            return
        now = time.time()
        if now < r["next"]:
            self.lbl_rec.configure(text="천천히 내리는 중  SG %.2f dBm → %.2f dBm  (%s %.1f초)" % (
                r["level"], r["target"], "RF OFF 까지" if r.get("phase") == "rf_off" else "다음", r["next"] - now))
            return
        if r.get("phase") == "rf_off":
            self.set_rf(False)
            self.stop_ramp("SG %.2f dBm, RF OFF" % r["level"], ok=True)
            return
        step = abs(fnum(self.var_ramp_db.get(), fnum(self.var_smw_step.get(), 1.0))) or 1.0
        new = max(round(r["level"] - step, 2), r["target"])
        self.smw_w.tasks.put(("call", lambda w, l=new: w.inst.write("SOUR1:POW %.2f" % l)))
        self.p_change_t = now
        r["level"] = new
        dt = max(0.2, fnum(self.var_ramp_dt.get(), 1.0))
        r["next"] = now + dt
        if new <= r["target"] + 1e-9:
            if r.get("rf_off"):
                r["phase"] = "rf_off"            # 마지막 레벨이 적용된 뒤(한 간격 후) RF OFF
                return
            self.stop_ramp("SG %.2f dBm" % new, ok=True)

    # ------------------------------------------------ 접기 / 펼치기 그룹
    def fold_group(self, parent, title, key):
        """제목 왼쪽 ▼/▶ 를 눌러 접고 펼치는 그룹. (바깥 틀, 내용 넣을 프레임) 반환."""
        lf = ttk.LabelFrame(parent, padding=(6, 2, 6, 6))
        lab = ttk.Label(lf, text="", cursor="hand2", font=("맑은 고딕", 9, "bold"), foreground="#1f3f66")
        lf.configure(labelwidget=lab)
        inner = ttk.Frame(lf)
        stub = ttk.Frame(lf, height=1)   # 접었을 때 틀이 제목 높이로 줄어들도록 하는 빈 자리
        state = {"open": bool(self.s.get("fold_" + key, True))}

        def apply():
            if state["open"]:
                stub.pack_forget()
                inner.pack(fill="both", expand=True)
                lab.configure(text=" ▼ %s " % title)
            else:
                inner.pack_forget()
                stub.pack(fill="x")
                lab.configure(text=" ▶ %s  (펼치기) " % title)

        def toggle(event=None):
            state["open"] = not state["open"]
            self.s["fold_" + key] = state["open"]
            apply()
            self.root.after(20, self.fit_ctrl_height)

        lab.bind("<Button-1>", toggle)
        self.__dict__.setdefault("folds", {})[key] = toggle
        apply()
        return lf, inner

    def fit_ctrl_height(self):
        """② 측정 조건 칸 높이: 내용에 맞추되, 왼쪽 칸 높이를 넘으면 그 안에서 스크롤"""
        try:
            self.root.update_idletasks()
            H = self.sf_left.winfo_height()
            others = sum(c.winfo_reqheight() for c in (self.card3, self.card_fq)) + 3 * 8
            c1 = self.leftcol.winfo_children()[0].winfo_reqheight()
            head = self.card2.winfo_reqheight() - self.sf_cond.winfo_reqheight()
            avail = H - c1 - others - head - 4
            nat = self.sf_cond.natural_h()
            uh = (self.s.get("card_h") or {}).get("c2")
            if uh:
                self.sf_cond.max_h = int(uh)          # 사용자가 경계를 끌어 정한 높이
            else:
                self.sf_cond.max_h = max(110, min(nat, avail)) if H > 100 else None
            self.sf_cond._sync()
            self.sf_left._sync()
        except (tk.TclError, AttributeError, IndexError):
            pass

    def sweep_step(self):
        sw = self.sweep
        if not sw:
            return
        now = time.time()
        if not self.smw["conn"] or not self.fsw["conn"]:
            self.stop_sweep("장비 연결 끊김")
            return
        if sw.get("phase") in ("ask", "ask_sat", "ask_gain"):
            self.lbl_rec.configure(text="자동 측정 일시 정지 - 팝업에서 다음 동작 선택")
            dlg = getattr(self, "sweep_dialog", None)
            alive = False
            try:
                alive = dlg is not None and bool(dlg.winfo_exists())
            except tk.TclError:
                pass
            if not alive and now - sw.get("ask_t", 0) > 1.0:
                # 팝업이 안 떴거나 닫혀 버림 → 다시 띄움 (멈춘 채로 남지 않게)
                sw["ask_t"] = now
                if sw["phase"] == "ask":
                    self.ask_sweep_end(sw.get("ask_reason"), sw.get("ask_more", True))
                elif sw["phase"] == "ask_gain":
                    self.ask_gain_drop(sw.get("gain_info", ""))
                else:
                    self.ask_saturation(sw.get("sat_info", ""))
            elif alive:
                try:
                    dlg.lift()
                except tk.TclError:
                    pass
            return
        if sw.get("phase") == "fq_ramp":
            self.fq_ramp_step(sw, now)
            return
        lvl = sw["levels"][sw["i"]]
        mx = fnum(self.var_mod_max.get() if sw.get("mode") == "mod" else self.var_smw_max.get(), 10.0)
        if sw["phase"] == "set":
            if lvl > max(mx, sw.get("over_ok", -999)) + 1e-9:
                if sw["i"] == 0:
                    self.stop_sweep("최대 허용 레벨 초과")
                    return
                # 측정 중에 최대 허용을 낮춤 → 여기까지를 끝으로 보고 팝업
                sw["levels"] = sw["levels"][:sw["i"]]
                sw["i"] = len(sw["levels"])
                self.enter_ask(sw, "최대 허용 %.2f dBm 도달" % mx, True)
                return
            self.smw_w.tasks.put(("call", lambda w, l=lvl: w.inst.write("SOUR1:POW %.2f" % l)))
            self.p_change_t = now
            sw.update(phase="wait", t=now, confirm=None, cur=lvl)
            return
        settle = max(0.0, fnum(self.var_settle.get(), 1.0))
        avg = max(0.2, fnum(self.var_avg.get(), 1.0))
        p = self.smw["p"]
        if sw["confirm"] is None:
            rf_ok = self.smw["rf"] or not sw.get("need_rf")
            wf = sw.get("want_f")
            f_ok = not wf or (self.smw.get("f") is not None and abs(self.smw["f"] - wf) < 1e3)
            if sw.get("want_wv") and not self.wv_ok():
                f_ok = False                     # 신호 파일 바뀐 것을 확인할 때까지 대기
            if p is not None and math.isfinite(p) and abs(p - lvl) < 0.011 and self.smw["t"] > sw["t"] and rf_ok and f_ok:
                sw["confirm"] = max(self.smw["t"], sw["t"])
                sw["rf_n"] = 0
            elif not rf_ok and now - max(sw["t"], sw.get("rf_try", 0)) > 2.5 and sw.get("rf_n", 0) < 2:
                # RF 가 아직 꺼져 있음 (사용자가 RF OFF 해 뒀거나 명령이 씹힘) → 지금 레벨에서 RF ON 다시 보냄
                sw["rf_n"] = sw.get("rf_n", 0) + 1
                sw["rf_try"] = now
                sw["need_rf"] = True
                self.set_rf(True, level=lvl, why="자동 측정: RF 꺼져 있음 → ")
                return
            elif now - max(sw["t"], sw.get("rf_try", 0)) > 6:
                self.stop_sweep(("SG RF 가 켜지지 않음 (RF ON 확인 실패)" if not rf_ok else
                                 "SG 주파수 %.6g GHz 변경 확인 실패" % (wf / 1e9) if not f_ok else
                                 "SG 레벨 %.2f dBm 설정 확인 실패" % lvl), ramp=False if not rf_ok else None)
                return
            else:
                self.lbl_rec.configure(text="%s자동 측정 %d/%d  SG %.2f dBm 설정 중..." % (
                    self.fq_prefix(sw), sw["i"] + 1, len(sw["levels"]), lvl))
                return
        ready = sw["confirm"] + settle + avg
        if now < ready:
            self.lbl_rec.configure(text=self.fq_prefix(sw) + "자동 측정 %d/%d  SG %.2f dBm  %s %.1f초" % (
                sw["i"] + 1, len(sw["levels"]), lvl, "안정화" if now < sw["confirm"] + settle else "평균 중", ready - now))
            return
        # 평균 구간에 전류가 하나도 없으면 바로 '전류 못 읽음' 으로 파워를 내렸다 다시 재지 말고 최대 3 초 기다림
        #   Mod. = 파워서플라이 Vdd 전류 (직접 읽기 요청) · Pulse = RTO 새 파형 (트리거가 늦게 걸리거나 파형이 늦게 올 때)
        t1_ = sw["confirm"] + settle
        mod_ = self.mode == "mod"
        smp = self.psu_samples if mod_ else self.cur_samples
        if not any(t_ >= t1_ and math.isfinite(i_) for t_, i_ in list(smp)[-60:]):
            w0 = sw.setdefault("cur_wait", now)
            pp = getattr(self, "psu_panel", None)
            src_ok = (pp is not None and pp.inst.get("e") is not None) if mod_ else bool(getattr(getattr(self, "scope", None), "connected", False))
            if now - w0 < 3.0 and src_ok:
                if mod_ and now - sw.get("cur_req", 0) > 0.4:
                    sw["cur_req"] = now
                    pp.worker.tasks.put(lambda: pp.read_job(full=False))
                self.lbl_rec.configure(text=self.fq_prefix(sw) + "자동 측정 %d/%d  SG %.2f dBm  %s 전류 읽는 중…" % (
                    sw["i"] + 1, len(sw["levels"]), lvl, "Vdd (파워서플라이)" if mod_ else "RTO (새 파형)"))
                return
            if now - w0 >= 3.0:
                self.status("SG %.2f dBm: %s 전류를 3 초 동안 못 읽음 → 다시 재기 (%s)" % (
                    lvl, "Vdd" if mod_ else "RTO", "파워서플라이 연결 · 전원 읽기 확인" if mod_ else
                    "RTO 트리거 · 채널 · 파형 확인 - 화면 'stale' 이면 트리거가 안 걸림"), "#d4a017")
        sw.pop("cur_wait", None)
        self.record(manual=True, t1=sw["confirm"] + settle, t2=now)
        row = self.find_row(self.smw["f"], lvl)
        bad = self.check_anomaly(row, sw) if self.var_sw_retry.get() else ""
        if bad:
            tries = sw.setdefault("retries", {})
            n = tries.get(sw["i"], 0)
            if n < 2:
                tries[sw["i"]] = n + 1
                if sw["i"] > 0:
                    sw["i"] -= 1          # 한 단계 아래 레벨로 돌아가서 그 점부터 다시
                    back = sw["levels"][sw["i"]]
                    self.status("SG %.2f dBm: %s → %.2f dBm 부터 다시 측정 (%d/2)" % (lvl, bad, back, n + 1), "#d4a017")
                else:
                    self.status("SG %.2f dBm: %s → 다시 측정 (%d/2)" % (lvl, bad, n + 1), "#d4a017")
                sw["phase"] = "set"
                sw["redo"] = sw.get("redo", 0) + 1
                return
            if row is not None:   # 두 번 다시 재도 이상 → 표에 빨간색 표시하고 진행
                row["flag"] = bad
                self.tree.item(str(row["no"]), tags=("check",))
                self.status("SG %.2f dBm: 재측정 후에도 %s → 빨간색 표시, 다음 점 진행" % (lvl, bad), "#ff6b6b")
        # Gain 급하락 (직전 점보다 ≥ 3 dB): 커넥터 빠짐 등 조립 문제 → 더 올리지 않고 확인 (자동 진행이어도 물어봄)
        gd = fnum(self.var_gain_drop.get())
        if gd and gd > 0 and not sw.get("gain_off") and sw["i"] > 0 and row is not None and row.get("gain") is not None:
            prev = self.find_row(self.smw["f"], sw["levels"][sw["i"] - 1])
            if prev is not None and prev.get("gain") is not None and prev["gain"] - row["gain"] >= gd - 1e-9:
                sw.update(phase="ask_gain", ask_t=now)
                sw["gain_info"] = ("SG %.2f → %.2f dBm 올렸더니\nGain %.2f → %.2f dB  (−%.2f dB, 기준 ≥ %g dB)\n"
                                   "DUT Pout %s dBm · Id %s mA · PAE %s %%" % (
                                       prev["sg"], lvl, prev["gain"], row["gain"], prev["gain"] - row["gain"], gd,
                                       self.fmt(row.get("pout"), 2), self.fmt(row.get("id"), 0), self.fmt(row.get("pae"), 1)))
                if row is not None:
                    row["flag"] = "Gain 급하락"
                    try:
                        self.tree.item(str(row["no"]), tags=("check",))
                    except tk.TclError:
                        pass
                self.ask_gain_drop(sw["gain_info"])
                return
        idmax = fnum(self.var_sw_idmax.get())
        last = self.rows[-1] if self.rows else None
        for r in self.rows:
            if r.get("sg") is not None and abs(r["sg"] - lvl) < 0.006 and abs((r.get("freq") or 0) - (self.smw["f"] or 0) / 1e9) < 1e-6:
                last = r
        if idmax and last and last.get("id") is not None and last["id"] > idmax:
            sw["levels"] = sw["levels"][:sw["i"] + 1]
            sw["i"] = len(sw["levels"])
            if self.fq_auto(sw) and sw.get("mode") != "mod":
                self.fq_auto_advance(sw, "Id %.0f mA > 상한 %.0f mA" % (last["id"], idmax))
                return
            self.enter_ask(sw, "Id %.0f mA > 상한 %.0f mA" % (last["id"], idmax), False)
            return
        # Mod.: 목표 DUT Pout 근처면 자동 종료 (다음 스텝이 목표를 넘을 것 같아도 종료)
        if sw.get("mode") == "mod":
            tgt, mg = sw["target"], sw["margin"]
            po = row.get("pout") if row is not None else None
            if po is not None:
                if po >= tgt:
                    self.mod_freq_done(sw, "목표 Pout 도달 (DUT Pout %.2f dBm ≥ %.2f%s)" % (
                        po, tgt, "" if po <= tgt + mg + 1e-9 else " · ⚠ 초과 허용 +%.2g dB 를 넘음" % mg))
                    return
                fine = sw.get("fine")
                if fine and not sw.get("fine_on") and po >= tgt - fine[0]:
                    # 목표 가까이 → 남은 스텝을 작은 스텝으로 바꿈 (SG 최대는 그대로)
                    mx_ = fnum(self.var_mod_max.get())
                    mx_ = sw["levels"][-1] if mx_ is None else mx_            # SG 최대 (안전) 는 그대로
                    st_ = fine[1]
                    n_ = int(math.floor((mx_ - lvl) / st_ + 1e-6))
                    sw["levels"] = sw["levels"][:sw["i"] + 1] + [round(lvl + st_ * (k + 1), 3) for k in range(max(0, n_))]
                    sw["fine_on"] = True
                    self.status("목표 %.2f dBm 까지 %.2f dB 남음 → 스텝 %.2g dB 로 줄임" % (tgt, tgt - po, st_), "#d4a017")
                prev = None
                for k in range(sw["i"] - 1, -1, -1):          # 직전에 잰 점 (작은 스텝으로 바뀌어도)
                    prev = self.find_row(self.smw["f"], sw["levels"][k])
                    if prev is not None:
                        break
                nxt = sw["levels"][sw["i"] + 1] if sw["i"] + 1 < len(sw["levels"]) else None
                if prev is not None and prev.get("pout") is not None and nxt is not None and lvl > prev["sg"]:
                    slope = min(max((po - prev["pout"]) / (lvl - prev["sg"]), 0.0), 1.2)
                    est = po + slope * (nxt - lvl)
                    if est > tgt + mg:
                        self.mod_freq_done(sw, "다음 스텝(SG %.2f)이면 목표 + 초과 허용 %.2f dBm 을 넘을 것으로 예상 → 멈춤 "
                                               "(예상 Pout %.2f dBm, 지금 %.2f)" % (nxt, tgt + mg, est, po))
                        return
            sw["i"] += 1
            if sw["i"] >= len(sw["levels"]):
                self.mod_freq_done(sw, "SG 최대(안전) %.2f dBm 까지 측정 - 목표 Pout 미달" % lvl)
                return
            sw["phase"] = "set"
            return
        # FSW(SA) 값이 직전 점보다 거의 안 오름 → 포화 가능, 계속할지 물어봄
        if self.var_sat_on.get() and not sw.get("sat_off") and sw["i"] > 0 and row is not None:
            prev = self.find_row(self.smw["f"], sw["levels"][sw["i"] - 1])
            thr = fnum(self.var_sat_db.get(), 0.1)
            if (prev is not None and prev.get("sa") is not None and row.get("sa") is not None
                    and thr is not None and lvl > prev["sg"] + 1e-6 and row["sa"] - prev["sa"] <= thr + 1e-6):
                if self.fq_auto(sw):
                    # 묻지 않고: 포화 → 천천히 내리고 다음 주파수 (마지막이면 내리고 끝)
                    self.fq_auto_advance(sw, "SA 포화 (SG %.2f → %.2f dBm 에 SA +%.2f dB ≤ %.2g)" % (
                        prev["sg"], lvl, row["sa"] - prev["sa"], thr))
                    return
                sw["phase"] = "ask_sat"
                sw["ask_t"] = now
                sw["sat_info"] = ("SG %.2f → %.2f dBm 올렸는데\nSA(FSW) %.2f → %.2f dBm  (+%.2f dB, 기준 ≤ %.2g dB)\n"
                                  "DUT Pout %s dBm · Gain %s dB · Id %s mA · PAE %s %%" % (
                                      prev["sg"], lvl, prev["sa"], row["sa"], row["sa"] - prev["sa"], thr,
                                      self.fmt(row.get("pout"), 2), self.fmt(row.get("gain"), 2),
                                      self.fmt(row.get("id"), 0), self.fmt(row.get("pae"), 1)))
                self.ask_saturation(sw["sat_info"])
                return
        sw["i"] += 1
        if sw["i"] >= len(sw["levels"]):
            if self.fq_auto(sw):
                self.fq_auto_advance(sw, "최대 허용 도달")
                return
            self.enter_ask(sw, None, True)
            return
        sw["phase"] = "set"

    # ------------------------------------------------ 여러 주파수 자동 측정
    def fq_list(self, mode=None, d=None):
        """여러 주파수 목록 (GHz). 잘못 입력이면 None. 직접 입력은 개수 제한 없음 (쉼표 · 띄어쓰기 · 줄바꿈)
        d = 저장된 설정 dict (how/list/start/stop/step) 를 주면 그것으로 계산"""
        if d is not None:
            v = {k: (lambda x=str(d.get(k, "")): types.SimpleNamespace(get=lambda: x))()
                 for k in ("how", "list", "start", "stop", "step")}
        else:
            v = self.fqv[mode or self.mode]
        try:
            if v["how"].get() == "list":
                txt = v["list"].get()
                for ch_ in ";/\n\t ":
                    txt = txt.replace(ch_, ",")
                out = [round(float(x), 6) for x in txt.split(",") if x.strip()]
            else:
                a, b, st = (float(v[k].get()) for k in ("start", "stop", "step"))
                st = abs(st)
                if st <= 0:
                    return None
                n = int(math.floor(abs(b - a) / st + 1e-6)) + 1
                sgn = 1 if b >= a else -1
                out = [round(a + sgn * st * k, 6) for k in range(min(n, 200))]
        except (ValueError, TypeError):
            return None
        out = [f for f in out if 0.001 < f < 100]
        seen, res = set(), []
        for f in out:
            if f not in seen:
                seen.add(f)
                res.append(f)
        return res or None

    def update_fq_label(self):
        for mode, v in getattr(self, "fqv", {}).items():
            if "lbl" not in v:
                continue
            fl = self.fq_list(mode)
            n = ("= %d개" % len(fl)) if fl else ""
            for k in ("cnt_r", "cnt_l"):
                if k in v:
                    v[k].configure(text=n)
            if not fl:
                v["lbl"].configure(text="주파수를 확인하세요", foreground=THEME["warn"])
            elif not v["on"].get():
                v["lbl"].configure(text="(꺼짐 - 지금 주파수 하나만 측정)", foreground=THEME["unit"])
            else:
                v["lbl"].configure(text="")
            v["bar_key"] = None
        self.update_fq_chips()

    def fq_auto(self, sw):
        """여러 주파수 자동 측정 + '묻지 않고 자동 진행' 체크 → 팝업 없이 진행"""
        return bool(sw.get("freqs")) and (sw.get("mode") == "mod" or bool(self.fqv["pulse"]["auto"].get()))

    def fq_auto_advance(self, sw, why):
        """묻지 않고: 다음 주파수가 있으면 천천히 내리고 대기 후 다음 주파수, 마지막이면 천천히 내리고 끝."""
        if self.fq_has_next(sw):
            self.next_freq(sw, why)
        else:
            self.stop_sweep("%s%s → 마지막 주파수, 천천히 내리고 끝 (%s)" % (self.fq_prefix(sw), why, self.finish_sweep_msg()),
                            ok=True, ramp=True)

    def mod_freq_done(self, sw, why):
        """Mod.: 이 주파수 끝 → 다음 주파수가 있으면 RF 완전히 끄고 대기 후 다음 주파수, 없으면 종료"""
        if self.fq_has_next(sw):
            nf = sw["freqs"][sw["fi"] + 1]
            self.set_rf(False, why="%s%s → " % (self.fq_prefix(sw), why))
            sw.update(phase="fq_ramp", fq_t=None, fq_why=why)
            self.status("%s%s → RF OFF, 대기 후 다음 주파수 %.6g GHz" % (self.fq_prefix(sw), why, nf), "#d4a017")
        else:
            self.stop_sweep(self.fq_prefix(sw) + why, ok=True)

    def fq_has_next(self, sw):
        return bool(sw.get("freqs")) and sw.get("fi", 0) + 1 < len(sw["freqs"])

    def fq_prefix(self, sw):
        fr = sw.get("freqs")
        if not fr:
            return ""
        return "[%d/%d %.6g GHz] " % (sw["fi"] + 1, len(fr), fr[sw["fi"]])

    def next_freq(self, sw, why):
        """지금 주파수 끝 → SG 시작 레벨까지 천천히 내리고, 대기 후 다음 주파수"""
        if self.sweep is not sw or not self.fq_has_next(sw):
            return
        nf = sw["freqs"][sw["fi"] + 1]
        sw.update(phase="fq_ramp", fq_t=None, fq_why=why)
        cur = sw.get("cur", sw["start"])
        self.status("%s%s → 천천히 내린 뒤 다음 주파수 %.6g GHz" % (self.fq_prefix(sw), why, nf), "#d4a017")
        self.start_ramp(sw["start"], from_level=cur, why="%s다음 주파수 준비: " % self.fq_prefix(sw), rf_off=False)

    def fq_ramp_step(self, sw, now):
        nf = sw["freqs"][sw["fi"] + 1]
        if self.ramp:
            return                                   # 천천히 내리는 중 (ramp_step 이 표시)
        wait = max(0.0, fnum(self.fqv[sw.get("mode", "pulse")]["wait"].get(), 3.0))
        if sw.get("fq_t") is None:
            sw["fq_t"] = now + wait
        if now < sw["fq_t"]:
            self.lbl_rec.configure(text="%s다음 주파수 %.6g GHz 까지 %.1f초 대기" % (self.fq_prefix(sw), nf, sw["fq_t"] - now))
            return
        sw["fi"] += 1
        self.var_freq.set("%.6g" % nf)
        self.apply_freq()
        sw.update(levels=list(sw["base_levels"]), i=0, phase="set", fine_on=False, want_f=nf * 1e9, sat_off=False,
                  over_ok=-999, retries={}, t=0.0, confirm=None)
        if sw.get("mode") == "mod" or not self.smw["rf"]:
            sw["need_rf"] = True
            # Mod.: RF 를 끈 채로 주파수를 바꿨음 (Pulse 도 RF 가 꺼져 있으면) → 시작 SG 레벨에서 다시 RF ON
            self.set_rf(True, level=sw["base_levels"][0], why="%s다음 주파수: " % self.fq_prefix(sw))
            sw["rf_try"] = time.time()
        self.status("%s측정 시작" % self.fq_prefix(sw), "#39ff88")

    def enter_ask(self, sw, reason, more):
        sw.update(phase="ask", ask_reason=reason, ask_more=more, ask_t=time.time())
        self.ask_sweep_end(reason, more)

    def popup(self, title, text, buttons, default_esc=None):
        """자동 측정용 선택 팝업 (항상 맨 위, 가운데). buttons: [(글자, 함수), ...]"""
        old = getattr(self, "sweep_dialog", None)
        if old is not None:
            try:
                old.destroy()
            except tk.TclError:
                pass
        win = tk.Toplevel(self.root)
        win.withdraw()                     # 다 만든 뒤 가운데에 놓고 보여줌 (엉뚱한 곳에 잠깐 뜨지 않게)
        win.title(title)
        win.transient(self.root)
        win.resizable(False, False)
        if title.startswith("⚠"):
            tk.Label(win, text="  " + title, bg="#c62828", fg="white", anchor="w", font=("맑은 고딕", 12, "bold"),
                     pady=6).pack(fill="x")
        fr = ttk.Frame(win, padding=16)
        fr.pack(fill="both", expand=True)
        ttk.Label(fr, text=text, font=("맑은 고딕", 10), justify="left").pack(anchor="w")
        bf = ttk.Frame(fr)
        bf.pack(fill="x", pady=(14, 0))
        done = {"v": False}

        def run(fn):
            if done["v"]:
                return
            done["v"] = True
            try:
                win.destroy()
            except tk.TclError:
                pass
            if self.sweep_dialog is win:
                self.sweep_dialog = None
            fn()
        first = None
        for lab, fn in buttons:
            b = ttk.Button(bf, text=lab, command=lambda fn=fn: run(fn))
            b.pack(side="left", padx=4)
            first = first or b
        last_fn = buttons[-1][1]
        win.protocol("WM_DELETE_WINDOW", lambda: run(last_fn))
        if default_esc is not None:
            win.bind("<Escape>", lambda e: run(default_esc))
        center_window(win, self.root)
        win.deiconify()
        try:   # 다른 창(전류 큰 화면 '항상 위' 등)에 가려지지 않게
            win.attributes("-topmost", True)
            win.lift()
            win.focus_force()
        except tk.TclError:
            pass
        try:
            win.grab_set()
        except tk.TclError:
            win.after(200, lambda: win.winfo_exists() and win.grab_set())
        if first is not None:
            first.focus_set()
        try:
            self.root.bell()
        except tk.TclError:
            pass
        self.sweep_dialog = win
        return win

    def choice(self, title, text, buttons):
        return centered_choice(self.root, title, text, buttons)

    def freq_check(self):
        """입력한 주파수 칸 vs SG 실제 주파수. (입력 GHz, SG GHz, 다른지)"""
        g_in = fnum(self.var_freq.get())
        f = self.smw.get("f") if self.smw.get("conn") else None
        g_sg = f / 1e9 if f else None
        diff = g_in is not None and g_sg is not None and abs(g_in - g_sg) > 1e-6
        return g_in, g_sg, diff

    def rows_at(self, g, levels=None):
        """지금 샘플 · 주파수 g(GHz) 에 이미 기록된 행 (levels 를 주면 그 SG 레벨만)"""
        smp = self.cur_sample()
        out = []
        for r in self.rows:
            if r.get("freq") is None or abs(r["freq"] - g) > 1e-6 or r.get("sample", "") != smp:
                continue
            if levels is not None and not any(r.get("sg") is not None and abs(r["sg"] - l) < 0.006 for l in levels):
                continue
            out.append(r)
        return out

    def ask_saturation(self, info):
        """SA 가 거의 안 오름 → 계속 / 계속(다시 묻지 않음) / 천천히 내리기 / 그대로 멈춤"""
        sw = self.sweep
        if not sw:
            return
        lvl = sw["levels"][sw["i"]]

        def go(no_more=False):
            if self.sweep is not sw:
                return
            if no_more:
                sw["sat_off"] = True
            sw["i"] += 1
            if sw["i"] >= len(sw["levels"]):
                if self.fq_has_next(sw) and self.var_fq_auto.get():
                    self.next_freq(sw, "최대 허용 도달")
                else:
                    self.enter_ask(sw, None, True)
            else:
                sw["phase"] = "set"
                self.status("SA 포화 확인 → 계속 측정", "#d4a017")

        def down():
            if self.sweep is sw:
                self.stop_sweep("SA 포화 (SG %.2f dBm) → 천천히 내림" % lvl, ok=True, ramp=True)

        def keep():
            if self.sweep is sw:
                self.stop_sweep("SA 포화 (SG %.2f dBm) · 레벨 유지" % lvl, ok=True, ramp=False)

        buttons = [("▲ 계속 진행", go), ("▲ 계속 (다시 묻지 않음)", lambda: go(True)),
                   ("▼▼ 천천히 내리기 (→ %.2f dBm)" % sw["start"], down), ("멈추고 그대로 두기", keep)]
        if self.fq_has_next(sw):
            nf = sw["freqs"][sw["fi"] + 1]
            buttons.insert(2, ("▶ 다음 주파수로 (→ %.6g GHz)" % nf, lambda: self.next_freq(sw, "SA 포화")))
            buttons[3] = ("▼▼ 천천히 내리고 끝", down)
        self.popup("SA 증가 작음 (포화?)",
                   "%s※ FSW 값이 직전 점보다 거의 오르지 않았습니다.\n\n%s\n\n계속 올릴까요?" % (self.fq_prefix(sw), info),
                   buttons, default_esc=down)

    def ask_gain_drop(self, info):
        """Gain 이 갑자기 떨어짐 (조립 문제 의심) → 계속 / 천천히 내리기 / (Mod.) RF OFF / 그대로 멈춤"""
        sw = self.sweep
        if not sw:
            return
        lvl = sw["levels"][sw["i"]]
        mod = sw.get("mode") == "mod"
        try:
            self.root.bell()
        except tk.TclError:
            pass

        def go(no_more=False):
            if self.sweep is not sw:
                return
            if no_more:
                sw["gain_off"] = True
            sw["i"] += 1
            if sw["i"] >= len(sw["levels"]):
                if mod:
                    self.mod_freq_done(sw, "SG 최대 도달")
                elif self.fq_has_next(sw) and self.var_fq_auto.get():
                    self.next_freq(sw, "최대 허용 도달")
                else:
                    self.enter_ask(sw, None, True)
            else:
                sw["phase"] = "set"
                self.status("Gain 급하락 확인 → 계속 측정", "#d4a017")

        def down():
            if self.sweep is not sw:
                return
            cur = sw.get("cur", sw["start"])
            if mod:
                self.stop_sweep("Gain 급하락 (SG %.2f dBm) → 천천히 내림" % lvl, ramp=False, emergency=True)
                self.start_ramp(sw["start"], from_level=cur, why="Gain 급하락 → ")
            else:
                self.stop_sweep("Gain 급하락 (SG %.2f dBm) → 천천히 내림" % lvl, ramp=True)

        def rf_off():
            if self.sweep is sw:
                self.stop_sweep("Gain 급하락 (SG %.2f dBm) → RF OFF" % lvl, ramp=False, emergency=True)
            self.set_rf(False, why="Gain 급하락: ")

        def keep():
            if self.sweep is sw:
                self.stop_sweep("Gain 급하락 (SG %.2f dBm) · 레벨 유지" % lvl, ramp=False, emergency=mod)
        buttons = [("▼▼ 천천히 내리기 (→ %.2f dBm)" % sw["start"], down), ("■ RF 바로 OFF", rf_off),
                   ("▲ 계속 진행", go), ("▲ 계속 (다시 묻지 않음)", lambda: go(True)), ("멈추고 그대로 두기", keep)]
        self.popup("⚠ Gain 급하락 - 조립 확인",
                   "%s※ Gain 이 직전 점보다 갑자기 떨어졌습니다. 더 올리지 않고 멈췄습니다.\n\n%s\n\n"
                   "효율이 튀는 것과 달리 조립 문제일 수 있습니다:\n"
                   "입력 / 출력 커넥터 빠짐 · 케이블 · 지그 접촉 · 소자 손상 등을 확인하세요." % (self.fq_prefix(sw), info),
                   buttons, default_esc=down)

    def finish_sweep_msg(self):
        sw = self.sweep
        if True:
            msg = "%d점 기록" % len(sw["levels"])
            if sw.get("redo"):
                msg += " (재측정 %d번)" % sw["redo"]
            nflag = sum(1 for r in self.rows if r.get("flag"))
            if nflag:
                msg += ", 확인 필요 %d점(빨간색)" % nflag
            return msg

    def ask_sweep_end(self, reason=None, more=True):
        """최대 허용 레벨까지 끝남(또는 Id 상한) → 한 칸 더 올리기 / 천천히 내리기 / 그대로 두기 선택."""
        sw = self.sweep
        if not sw:
            return
        last = sw["levels"][-1]
        step = fnum(self.var_smw_step.get(), 1.0) or 1.0
        nxt = round(last + step, 2)
        msg = self.finish_sweep_msg()
        r = self.find_row(self.smw["f"], last)
        info = ""
        if r is not None:
            info = "\n마지막 점: DUT Pout %s dBm · Gain %s dB · Id %s mA · PAE %s %%" % (
                self.fmt(r.get("pout"), 2), self.fmt(r.get("gain"), 2), self.fmt(r.get("id"), 0), self.fmt(r.get("pae"), 1))
        head = ("SG %.2f dBm 에서 멈춤: %s" % (last, reason)) if reason else ("SG %.2f dBm 까지 자동 측정 완료" % last)

        def up():
            if self.sweep is not sw:
                return
            sw["levels"].append(nxt)
            sw["over_ok"] = max(sw.get("over_ok", -999), nxt)   # 최대 허용을 넘는 한 칸 (사용자 선택)
            sw["phase"] = "set"
            self.status("한 칸 더: SG %.2f dBm" % nxt, "#d4a017")

        def down():
            if self.sweep is sw:
                self.stop_sweep(msg, ok=True, ramp=True)

        def keep():
            if self.sweep is sw:
                self.stop_sweep(msg + " · 레벨 유지", ok=True, ramp=False)

        buttons = ([("▲ 한 칸 더 올리기 (→ %.2f dBm)" % nxt, up)] if more else []) + [
            ("▼▼ 천천히 내리기 (→ %.2f dBm)" % sw["start"], down), ("그대로 두기", keep)]
        if self.fq_has_next(sw):
            nf = sw["freqs"][sw["fi"] + 1]
            buttons.insert(0, ("▶ 다음 주파수로 (→ %.6g GHz)" % nf, lambda: self.next_freq(sw, reason or "최대 허용 도달")))
            down_txt = "▼▼ 천천히 내리고 끝 (남은 주파수 측정 안 함)"
            buttons = [(down_txt if lab.startswith("▼▼") else lab, fn) for lab, fn in buttons]
            head = self.fq_prefix(sw) + head
        self.popup("자동 측정 완료", "%s (%s)%s\n\n다음 동작을 고르세요." % (head, msg, info), buttons, default_esc=down)

    def window_avg(self, samples, t1, t2, db=False):
        vals = [v for (t, v) in samples if t1 <= t <= t2 and math.isfinite(v)]
        if not vals:
            return None, 0
        if db:  # dBm → 선형 평균
            lin = sum(10 ** (v / 10.0) for v in vals) / len(vals)
            return 10 * math.log10(lin), len(vals)
        return sum(vals) / len(vals), len(vals)

    def record(self, manual=False, t1=None, t2=None):
        now = time.time()
        if not self.sweep:
            self.push_undo("기록 1점")       # 자동 측정은 시작할 때 한 번만 (전체를 한 번에 되돌림)
        avg_s = max(0.0, fnum(self.var_avg.get(), 1.0))
        if t2 is None:
            t2 = now
        if t1 is None:
            t1 = t2 - max(avg_s, 0.3)
        pin_raw = self.smw["p"]
        freq = self.smw["f"]
        pout_raw, nf = self.window_avg(self.fsw_samples, t1, t2, db=True)
        if pout_raw is None and self.fsw["y"] is not None and math.isfinite(self.fsw["y"]):
            pout_raw = self.fsw["y"]
        vmeas = None
        if self.mode == "mod":
            # Mod.: 전류는 E36155A 전원에서 (평균 구간 값, 없으면 평균 시작 뒤 · 3 초 안의 마지막 값)
            cur, nc = self.window_avg(self.psu_samples, t1, t2)
            if cur is None and self.psu.get("conn") and self.psu.get("i") is not None \
                    and math.isfinite(self.psu["i"]) and now - self.psu["t"] < 3.0 and self.psu["t"] >= t1 - 0.5:
                cur = self.psu["i"]
            pass
        else:
            cur, nc = self.window_avg(self.cur_samples, t1, t2)
        if cur is None and self.mode != "mod":
            # 평균 구간에 새 파형이 없으면, 1초 안에 들어온 새 파형 값만 사용 (오래된 값은 쓰지 않음)
            t, a = self.cur_last
            if now - self.cur_fresh_t < 1.0 and abs(t - self.cur_fresh_t) < 1e-6 and math.isfinite(a) \
                    and self.cur_fresh_t >= self.p_change_t:
                cur = a
        if pout_raw is not None and math.isfinite(pout_raw) and self.var_saq.get():
            pout_raw = round_sa(pout_raw)
        vmeas, vsrc = self.eff_vdd()                    # 효율에 쓴 Vdd (파워서플라이 값, 없으면 Vdd 칸)
        c = self.dut_calc(pin_raw, pout_raw, cur, freq, vdd=vmeas)
        c["vdd_used"], c["vdd_src"] = vmeas, vsrc
        row = {
            "no": len(self.rows) + 1,
            "time": datetime.datetime.now().strftime("%H:%M:%S"),
            "freq": freq / 1e9 if freq else None,
            "sg": pin_raw if (pin_raw is not None and math.isfinite(pin_raw)) else None,
            "sa": pout_raw,
            "id": cur * 1e3 if cur is not None else None,
            "sample": self.cur_sample(),
            "stype": self.cur_stype(),
            "snum": (self.var_sample.get() or "").strip(),
            "mode": self.mode,
            "date": datetime.datetime.now().strftime("%Y%m%d-%H%M"),
        }
        row.update(c)
        # 바이어스: 자동 측정이면 시작할 때 잰 값, 아니면 이 데이터셋 첫 기록 때 값 (표 위 · 엑셀 맨 위에 1회 표시)
        dk = self.ds_key(row)
        self.ds_done.pop(dk, None)                 # 다시 기록하면 '측정 완료' 표시 해제
        if self.sweep:
            self.sweep.setdefault("keys", set()).add(dk)
        prev_b = self.bias_of_ds(dk)
        if self.sweep and self.sweep.get("bias") and not prev_b:
            row["bias"] = self.sweep["bias"]
        elif not prev_b:
            row["bias"] = self.bias_now()
        if self.ensure_today_pam(row["stype"]):          # 오늘 날짜 .pam (없으면 만들고, 날짜 바뀌면 이어서 새 파일)
            self.pam_dirty.add(row["stype"])          # .pam 에 아직 저장 안 된 측정이 있음
            if not self.sweep:
                self.schedule_pam_save()
        if nc > 0 and row["sg"] is not None and self.mem_key(freq) == self.mem_freq:
            self.cur_mem[round(row["sg"], 2)] = cur
        pin, pout = c["pin"], c["pout"]
        replaced = None
        if row["sg"] is not None:
            key = (row["sample"], round(row["freq"] or 0, 6), round(row["sg"], 2))
            for i, r in enumerate(self.rows):
                if r.get("sg") is not None and \
                        (r.get("sample", ""), round(r["freq"] or 0, 6), round(r["sg"], 2)) == key:
                    replaced = i
                    break
        if replaced is not None:
            row["no"] = self.rows[replaced]["no"]
            row["date"] = self.rows[replaced].get("date") or row["date"]   # 같은 Dataset 이름 유지
            if not row.get("bias") and self.rows[replaced].get("bias"):
                row["bias"] = self.rows[replaced]["bias"]
            self.rows[replaced] = row
        else:
            self.rows.append(row)
        try:
            if replaced is not None:
                self.tree.item(str(row["no"]), values=self.row_values(row), tags=())
            else:
                self.tree.insert("", "end", iid=str(row["no"]), values=self.row_values(row))
        except tk.TclError:
            self.renumber()             # 표 번호가 어긋났으면 표를 새로 그림 (그래프 갱신이 멈추지 않게)
        try:
            self.tree.see(str(row["no"]))
            self.tree.selection_set(str(row["no"]))
        except tk.TclError:
            pass
        self.auto_sel = str(row["no"])
        self.refresh_plots()
        miss = []
        if pin is None:
            miss.append("SMW")
        if pout is None:
            miss.append("FSW")
        if cur is None:
            miss.append("전류(새 파형 없음 - 트리거 확인)")
        if fnum(self.var_vdd.get()) is None:
            miss.append("Vdd 미입력 → PAE")
        msg = "%s #%d  DUT Pin %s dBm" % ("덮어씀" if replaced is not None else "기록", row["no"], self.fmt(pin, 2))
        if cur is not None and row["pae"] is None and fnum(self.var_vdd.get()) is not None:
            msg += "  · Id < %s mA → PAE 제외" % self.var_minid.get()
        if miss:
            msg += "  (값 없음: %s)" % ", ".join(miss)
        self.status(msg, "#d4a017" if miss else "#39ff88")

    @staticmethod
    def fmt(v, d):
        return "" if v is None else ("%.*f" % (d, v))

    def row_values(self, r):
        f = self.fmt
        return (r["no"], r.get("sample", ""), r["time"], f(r["freq"], 4), f(r["sg"], 2), f(r["sa"], 1 if self.var_saq.get() else 2),
                f(r["inl"], 2), f(r["outl"], 2), f(r.get("lossf"), 4),
                f(r["pin"], 2), f(r["pout"], 2), f(r["gain"], 2), f(r["id"], 1), f(r.get("vdd_used"), 2), f(r["pdc"], 3),
                f(r["de"], 2), f(r["pae"], 2))

    def selected_rows(self):
        """기록 표에서 선택한 행 (Ctrl/Shift 다중 선택), 선택이 없으면 전체."""
        sel = set(self.tree.selection())
        rows = [r for r in self.rows if str(r["no"]) in sel]
        return (rows, True) if rows else (list(self.rows), False)

    def table_text(self, rows=None):
        lines = ["\t".join(c[1] for c in self.COLS)]
        for r in (self.rows if rows is None else rows):
            lines.append("\t".join(str(x) for x in self.row_values(r)))
        return "\n".join(lines)

    def pa_payload(self, rows):
        """RF PA Analyzer 의 Datasets 복사 형식 (Ctrl+V 하면 이름·FREQ 그대로 바로 그래프)"""
        return {"version": 1, "datasets": [
            {"name": name, "display": disp, "rows": cells, "freq_override_hz": None,
             "user_mapping": False, "header_row": None, "mapping": None, "style": {}}
            for _k, name, disp, cells in self.pa_datasets(rows)]}

    def copy_rows(self):
        """선택한 행(없으면 전체) 복사 → 엑셀에 Ctrl+V = 표, RF PA Analyzer 에 Ctrl+V = 바로 그래프."""
        rows, part = self.selected_rows()
        if not rows:
            self.status("기록된 행이 없습니다.", "#d4a017")
            return
        text = self.table_text(rows)
        payload = self.pa_payload(rows)
        ok = False
        if os.name == "nt" and payload["datasets"]:
            try:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                ok = win_clipboard_set(text, {PA_MIME: data,
                                              'application/x-qt-windows-mime;value="%s"' % PA_MIME: data},
                                       hwnd=self.root.winfo_id())
            except Exception:
                ok = False
        if not ok:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
        what = "%s%d행" % ("선택 " if part else "전체 ", len(rows))
        if ok:
            self.status("%s 복사됨 - 엑셀: 표 / PA Analyzer: Ctrl+V 하면 Dataset %d개 바로 그래프"
                        % (what, len(payload["datasets"])), "#39ff88")
        else:
            self.status("%s 복사됨 - 엑셀에서 Ctrl+V" % what, "#39ff88")

    def save_pam(self):
        """선택한 행(없으면 전체)을 RF PA Analyzer 프로젝트(.pam)로 저장 → Analyzer 에서 열면 바로 그래프."""
        rows, part = self.selected_rows()
        sets = self.pa_datasets(rows)
        if not sets:
            self.status("저장할 기록이 없습니다.", "#d4a017")
            return
        path = filedialog.asksaveasfilename(
            title="PA Analyzer 프로젝트로 저장 (.pam)", defaultextension=".pam",
            initialdir=self.s.get("pam_dir") or self.s.get("save_dir") or None,
            initialfile="PA_bench_%s.pam" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"),
            filetypes=[("PA Analyzer 프로젝트", "*.pam"), ("모든 파일", "*.*")])
        if not path:
            return
        now = datetime.datetime.now().isoformat(timespec="seconds")
        used, sheets = set(), []
        for _k, name, disp, cells in sets:
            nm, i = name, 2
            while nm in used:                 # Analyzer 는 내부 이름이 겹치면 안 됨
                nm, i = "%s (%d)" % (name, i), i + 1
            used.add(nm)
            sheets.append({"name": nm, "display": disp, "rows": cells, "freq_override_hz": None,
                           "user_mapping": False, "header_row": None, "mapping": None,
                           "added_at": now, "added_how": "PA Bench Monitor %s" % VERSION, "modified_at": None})
        names = [sh["name"] for sh in sheets]
        doc = {"format": "PA-Measurement-Analyzer project", "version": 1, "saved": now, "source_file": "",
               "sheets": sheets, "settings": {"checked": names, "sheet": names[-1]}}
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False)
        except OSError as e:
            messagebox.showerror(APP_NAME, "저장 실패:\n%s" % e)
            return
        self.s["pam_dir"] = os.path.dirname(path)
        self.status("PA Analyzer 프로젝트 저장: %s (Dataset %d개, %s%d행)" % (
            os.path.basename(path), len(sheets), "선택 " if part else "", len(rows)), "#39ff88")

    def export_excel(self, path=None):
        """path 를 주면 묻지 않고 그 파일로 저장 (여러 주파수 자동 측정이 끝났을 때)"""
        if not self.rows:
            self.status("기록된 행이 없습니다.", "#d4a017")
            return False
        try:
            import excel_export
        except ImportError:
            messagebox.showerror(APP_NAME, "엑셀 내보내기에 openpyxl 이 필요합니다.\n"
                                           "명령 프롬프트에서:  python -m pip install openpyxl\n"
                                           "(build_exe.bat 로 만든 exe 에는 포함되어 있음)")
            return
        auto = path is not None
        if not auto:
            name = self.meas_filename(".xlsx")
            dmap = self.s.setdefault("xlsx_dir_by_type", {})
            t_ = self.cur_stype() or ""
            d0 = dmap.get(t_) or self.s.get("xlsx_dir") or None
            if d0 and not os.path.isdir(d0):
                d0 = None
            path = filedialog.asksaveasfilename(title="엑셀 내보내기", defaultextension=".xlsx", initialfile=name,
                                                initialdir=d0, filetypes=[("Excel", "*.xlsx")])
            if not path:
                return False
            dmap[t_] = os.path.dirname(path)              # 샘플 종류마다 저장 폴더 기억 (자동 저장 창과 같이 씀)
        ax = self.plot_axes()
        ax = tuple(a[:3] if (a is not None and i < 2) else a for i, a in enumerate(ax))
        for k in range(20):
            p_ = path if k == 0 else "%s_%d%s" % (os.path.splitext(path)[0], k + 1, os.path.splitext(path)[1])
            try:
                excel_export.export_xlsx(p_, self.COLS, self.rows, self.row_values, self.ds_key, self.ds_label,
                                         self.ds_color, ax, vdd=fnum(self.var_vdd.get()),
                                         ds_shape=self.ds_shape, bias_of=self.bias_of_ds)
                path = p_
                break
            except PermissionError:
                if not auto:
                    messagebox.showerror(APP_NAME, "저장 실패: 같은 이름의 파일이 엑셀에서 열려 있으면 닫고 다시 시도하세요.")
                    return False
                continue                      # 자동 저장: 엑셀에서 열려 있으면 _2, _3 … 이름으로
            except Exception as e:
                messagebox.showerror(APP_NAME, "엑셀 저장 실패\n%s" % e)
                return False
        else:
            messagebox.showerror(APP_NAME, "엑셀 자동 저장 실패: %s" % path)
            return False
        if not auto:
            self.s["xlsx_dir"] = os.path.dirname(path)
        self.status("엑셀 저장됨: " + path, "#39ff88")
        return True

    def ask_sweep_xlsx(self):
        """여러 주파수 측정이 끝나면 자동 저장할 엑셀 위치 · 이름 (취소 = 저장 안 함 → None)"""
        t_ = self.cur_stype()
        nm = self.meas_filename(".xlsx")
        dmap = self.s.setdefault("xlsx_dir_by_type", {})
        d0 = dmap.get(t_ or "") or self.s.get("xlsx_auto_dir") or self.s.get("xlsx_dir") or None
        if d0 and not os.path.isdir(d0):
            d0 = None
        xlsx = filedialog.asksaveasfilename(
            title="여러 주파수 측정이 끝나면 엑셀 자동 저장 - 폴더 · 이름 (취소 = 저장 안 함)%s" % (
                "  [%s 종류: 지난번 폴더]" % t_ if dmap.get(t_ or "") else ""), defaultextension=".xlsx",
            initialfile=nm, initialdir=d0, filetypes=[("Excel", "*.xlsx")]) or None
        if xlsx:
            self.s["xlsx_auto_dir"] = os.path.dirname(xlsx)
            dmap[t_ or ""] = os.path.dirname(xlsx)       # 샘플 종류마다 저장 폴더 기억
        return xlsx

    def on_bias_seq_start(self, auto):
        """POWER SUPPLY 순서 시작: ON + 측정이고 여러 주파수면, 바이어스 올리는 동안 엑셀 저장 위치를 바로 물어봄"""
        self.pre_xlsx = None
        if not auto:
            return
        v = self.fqv.get(self.mode)
        if not v or not v["on"].get() or not self.fq_list(self.mode):
            return

        self._xlsx_tok = tok = getattr(self, "_xlsx_tok", 0) + 1

        def ask():
            self._asking_xlsx = True
            try:
                got = self.ask_sweep_xlsx() or ""
            finally:
                self._asking_xlsx = False
            # 그사이 바이어스가 실패 · 중지됐으면 (ON + 측정이 아니게 됐으면) 쓰지 않음 → 다음 측정은 다시 물어봄
            self.pre_xlsx = got if self._xlsx_tok == tok else None
            if getattr(self, "_start_after_xlsx", False):      # 저장 창을 고르는 동안 바이어스가 끝났으면 이제 측정 시작
                self._start_after_xlsx = False
                self.show_right("plot")
                self.toggle_sweep(auto=True)
        self.root.after(250, ask)

    def meas_filename(self, ext):
        """측정 결과 파일 이름: Pulse_Meas_20261103_0915_A타입_S1 / Mod_Meas_... (종류 · 번호가 없으면 뺌)"""
        t_, n_ = self.cur_stype(), (self.var_sample.get() or "").strip()
        parts = ["Mod_Meas" if self.mode == "mod" else "Pulse_Meas", datetime.datetime.now().strftime("%Y%m%d_%H%M"), t_, n_]
        nm = "_".join(x for x in parts if x)
        return "".join(ch if ch not in '\\/:*?"<>|' else "_" for ch in nm) + ext

    def save_csv(self):
        if not self.rows:
            return
        name = self.meas_filename(".csv")
        path = filedialog.asksaveasfilename(defaultextension=".csv", initialfile=name,
                                            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow([c[1] for c in self.COLS])
            for r in self.rows:
                w.writerow(self.row_values(r))
        self.status("저장됨: " + path, "#39ff88")

    def build_whatif_vdd(self, parent):
        """[다시 계산] 옆 '가정 Vdd' 칸: 계산에만 쓰는 Vdd (파워서플라이는 안 건드림).
        휠 ±0.1 V · Shift+휠 ±1 V · ↑↓ · 직접 입력. 체크하면 다시 계산 때 이 Vdd 로 계산"""
        fr = ttk.Frame(parent, style="Card.TFrame")
        fr.pack(side="right", padx=(6, 2))
        self.var_wi_on = tk.BooleanVar(value=False)
        self.var_wi_vdd = tk.StringVar(value="")
        ttk.Checkbutton(fr, text="가정 Vdd", variable=self.var_wi_on,
                        command=self.whatif_toggle).pack(side="left")
        ent = tk.Entry(fr, textvariable=self.var_wi_vdd, width=6, justify="right", font=("Consolas", 10, "bold"))
        ent.pack(side="left", padx=(2, 1))
        ttk.Label(fr, text="V", style="Card.TLabel").pack(side="left")
        self.ent_wi_vdd = ent

        def nudge(d, big):
            v = fnum(self.var_wi_vdd.get())
            if v is None:
                v = self.eff_vdd()[0] or 0.0
            v = max(0.0, round(v + d * (1.0 if big else 0.1), 2))
            self.var_wi_vdd.set("%.2f" % v)
            return "break"
        ent.bind("<MouseWheel>", lambda e: nudge(1 if e.delta > 0 else -1, e.state & 1))
        ent.bind("<Button-4>", lambda e: nudge(1, e.state & 1))
        ent.bind("<Button-5>", lambda e: nudge(-1, e.state & 1))
        ent.bind("<Up>", lambda e: nudge(1, e.state & 1))
        ent.bind("<Down>", lambda e: nudge(-1, e.state & 1))
        ent.bind("<Return>", lambda e: (self.var_wi_on.set(True), self.recalc_rows(), "break")[2])
        ent.bind("<FocusIn>", lambda e: self.whatif_fill())

    def whatif_fill(self):
        if fnum(self.var_wi_vdd.get()) is None:
            v = self.eff_vdd()[0]
            if v is not None:
                self.var_wi_vdd.set("%.2f" % v)

    def whatif_toggle(self):
        if self.var_wi_on.get():
            self.whatif_fill()

    def recalc_rows(self):
        """기록된 SG·SA·Id 원본값으로 DUT Pin/Pout/Gain/PAE 를 현재 손실로 다시 계산.
        Vdd = 측정 때 Vdd 그대로, '가정 Vdd' 체크 시 그 칸 값 (원래 값은 vdd_meas 에 보관 → 끄면 복원)"""
        wi = None
        if getattr(self, "var_wi_on", None) is not None and self.var_wi_on.get():
            wi = fnum(self.var_wi_vdd.get())
            if wi is None or wi <= 0:
                self.status("가정 Vdd 값이 올바르지 않음", "#ff6b6b")
                return
        for r in self.rows:
            if "vdd_meas" not in r:
                r["vdd_meas"] = r.get("vdd_used")
            r["vdd_used"] = wi if wi is not None else r.get("vdd_meas")
            amps = r["id"] / 1e3 if r.get("id") is not None else None
            hz = r["freq"] * 1e9 if r.get("freq") is not None else None
            r.update(self.dut_calc(r.get("sg"), r.get("sa"), amps, hz, vdd=r.get("vdd_used")))
        self.renumber()
        if wi is not None:
            self.status("%d행 다시 계산됨 — 가정 Vdd %.2f V 로 (전원은 안 바뀜 · 체크 끄고 다시 계산 = 원래대로)" % (len(self.rows), wi), "#ffb300")
        else:
            self.status("%d행 다시 계산됨 (손실 · Vdd = 측정마다 그때 Vdd, 없으면 Vdd 칸 %s V)" % (len(self.rows), self.var_vdd.get() or "미입력"), "#39ff88")

    # ------------------------------------------------ Ctrl+Z (기록 표 되돌리기)
    def snapshot(self, what, rekeys=None):
        return dict(what=what, mode=self.mode,
                    rows={md: copy.deepcopy(rs) for md, rs in self.rows_by_mode.items()},
                    ds_style=copy.deepcopy(self.ds_style), pam_renames=copy.deepcopy(self.pam_renames),
                    rekeys=list(rekeys or []))

    def push_undo(self, what):
        self.undo_stack.append(self.snapshot(what))
        del self.undo_stack[:-50]
        self.redo_stack = []             # 새로 바꾸면 다시 실행(Ctrl+Y) 목록은 비움

    def on_undo_key(self, e=None, redo=False):
        w = e.widget if e is not None else None
        if self.meas_view() == "dpd" and self.dpd is not None:
            return None if redo else self.dpd.undo()  # DPD 화면: 4 DPD 카드 값 되돌리기
        if isinstance(w, (tk.Entry, ttk.Entry, tk.Spinbox, ttk.Spinbox, ttk.Combobox, tk.Text)):
            return               # 입력칸에서는 그 칸 기본 동작
        self.redo() if redo else self.undo()
        return "break"

    def restore_snapshot(self, u, rekey_back):
        self.rows_by_mode = u["rows"]
        self.rows = self.rows_by_mode[self.mode]
        self.ds_style = u["ds_style"]
        self.pam_renames = u["pam_renames"]
        try:
            if rekey_back:       # 되돌리기: 그래프 창 이름도 원래대로
                for ko, kn, d_old, _d_new in reversed(u["rekeys"]):
                    self.analyzer.rekey(list(kn), list(ko), d_old)
            else:                # 다시 실행: 다시 새 이름으로
                for ko, kn, _d_old, d_new in u["rekeys"]:
                    self.analyzer.rekey(list(ko), list(kn), d_new)
            self.analyzer.send({"op": "reattach"})           # 되살린 기록 → 그래프 창의 같은 Dataset 에 다시 연결
            self.analyzer.sent = {}
        except Exception:
            pass
        for r in self.rows_by_mode.get("pulse", []) + self.rows_by_mode.get("mod", []):
            if r.get("stype") and self.stype_pam(r["stype"]):
                self.pam_dirty.add(r["stype"])
        self.renumber()
        self.update_mode_counts()

    def undo(self):
        if self.sweep or self.ramp:
            self.status("자동 측정 / 천천히 내리기 중에는 되돌릴 수 없습니다.", "#d4a017")
            return
        if not self.undo_stack:
            self.status("되돌릴 것이 없습니다 (Ctrl+Z).", "#d4a017")
            return
        u = self.undo_stack.pop()
        self.redo_stack.append(self.snapshot(u["what"], u["rekeys"]))
        self.restore_snapshot(u, rekey_back=True)
        self.status("되돌림 (Ctrl+Z): %s  · 되돌릴 수 있음 %d · 다시 실행(Ctrl+Y) %d" % (
            u["what"], len(self.undo_stack), len(self.redo_stack)), "#39ff88")

    def redo(self):
        if self.sweep or self.ramp:
            self.status("자동 측정 / 천천히 내리기 중에는 다시 실행할 수 없습니다.", "#d4a017")
            return
        if not self.redo_stack:
            self.status("다시 실행할 것이 없습니다 (Ctrl+Y).", "#d4a017")
            return
        r = self.redo_stack.pop()
        self.undo_stack.append(self.snapshot(r["what"], r["rekeys"]))
        self.restore_snapshot(r, rekey_back=False)
        self.status("다시 실행 (Ctrl+Y): %s  · 되돌릴 수 있음 %d · 다시 실행 %d" % (
            r["what"], len(self.undo_stack), len(self.redo_stack)), "#39ff88")

    def delete_selected(self):
        sel = set(self.tree.selection())
        if not sel:
            return
        ok = self.choice("삭제 확인", "선택한 %d행을 삭제하시겠습니까?\n\n(Enter = 예, Esc = 아니요)" % len(sel),
                         [("예 (Enter)", True), ("아니요 (Esc)", False)])
        if not ok:
            return
        self.push_undo("%d행 삭제" % len(sel))
        self.rows = [r for r in self.rows if str(r["no"]) not in sel]
        self.renumber()

    def clear_rows(self):
        if self.rows and messagebox.askyesno(APP_NAME, "기록을 모두 지울까요?" + (
                "\n\n(PA Analyzer 그래프 창에 그려진 Dataset 은 그대로 남습니다 - 그 창에서 직접 지우기)"
                if self.analyzer.running() else "")):
            self.push_undo("기록 모두 지움")
            self.rows = []
            self.fcolor_cache.clear()
            self.analyzer.detach_all()     # PA Analyzer 창의 그래프는 그대로 둠 (거기서 직접 지우기)
            self.renumber()
            self.reset_memory()

    def renumber(self):
        self.rows_by_mode[self.mode] = self.rows
        self.tree.delete(*self.tree.get_children())
        for i, r in enumerate(self.rows, 1):
            r["no"] = i
            self.tree.insert("", "end", iid=str(i), values=self.row_values(r),
                             tags=("check",) if r.get("flag") else ())
        self.refresh_plots()

    def status(self, text, color=FG_DIM):
        self.lbl_status.configure(text=text, fg=color)

    # ------------------------------------------------ 주기 처리
    def tick(self):
        # 어떤 단계에서 오류가 나도 다음 주기는 반드시 돌도록 (장비 읽기·자동 측정·천천히 내리기가 멈추지 않게)
        try:
            for step in (self.drain_queue, self.update_dashboard, self.follow_vdd, self.auto_record_step,
                         self.sweep_step, self.ramp_step, self.auto_bias_off_step, self.poll_pam_replies, self.autosave_settings,
                         self.update_mod_popup):
                try:
                    step()
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    self.status("내부 오류 (%s): %s" % (step.__name__, e), "#ff6b6b")
        finally:
            self.root.after(100, self.tick)

    def drain_queue(self):
        DPD_SHARE["sweep"] = bool(self.sweep)          # DPD Tester 가 자동 측정 중엔 시작하지 않게 알려 줌
        try:
            while True:
                msg = self.q.get_nowait()
                self.handle(msg)
        except queue.Empty:
            pass

    def handle(self, msg):
        name, kind = msg[0], msg[1]
        now = time.time()
        if kind == "call":                 # 장비 쓰레드 → UI 에서 실행할 함수
            msg[2]()
            return
        if name == "smw":
            if kind == "val":
                _, _, f, p, rf = msg
                old = self.smw.get("p")
                if old is None or not math.isfinite(old) or (math.isfinite(p) and abs(p - old) > 0.004):
                    self.p_change_t = now
                    if old is not None and math.isfinite(old) and math.isfinite(p):
                        self.on_sg_change(old, p, f)
                rf_prev = self.smw.get("rf")
                self.smw.update(f=f, p=p, rf=rf, t=now)
                self.scope_sg_level(p)
                if rf_prev is not None and bool(rf_prev) != bool(rf):
                    self.scope_rf_changed(bool(rf))
                self.lbl_smw_read.configure(text="현재: %s · %s dBm · RF %s" % (
                    fmt_freq(f), self.fmt(p, 2) if math.isfinite(p) else "---", "ON" if rf else "OFF"))
            elif kind == "connected":
                self.smw["conn"] = True
                self.btn_smw.configure(text="연결 해제")
                self.lbl_smw_idn.configure(text=msg[2], foreground="#1f4f8f")
                self.status("SMW200A 연결됨", "#39ff88")
            elif kind == "disconnected":
                self.smw.update(conn=False, p=None)
                self.btn_smw.configure(text="연결")
            elif kind == "dpd_pause":
                self.status(("SMW200A: PA DPD Tester 측정 중 - 잠시 읽기 멈춤 (끝나면 자동으로 다시 읽음)" if msg[2] else
                             "SMW200A: PA DPD Tester 끝 - 다시 읽는 중"), "#d4a017" if msg[2] else "#39ff88")
            elif kind == "error":
                self.status("SMW: " + msg[2], "#ff6b6b")
            elif kind == "status":
                self.status("SMW: " + msg[2])
            elif kind == "wv":
                self.on_wv_read(msg[2], msg[3], msg[4])
            elif kind == "wv_fail":
                if self.sweep and self.sweep.get("want_wv"):
                    self.stop_sweep("SG 신호 적용 실패", ramp=False, emergency=True)
                messagebox.showerror(APP_NAME, "SG 신호 파일을 적용하지 못했습니다.\n\n입력: %s\n%s" % (msg[2], msg[3]))
            elif kind == "wv_real":
                md, real, note = msg[2], msg[3], msg[4]
                if note and real and md in self.wv_paths:
                    self.wv_paths[md] = real            # 오타를 고친 실제 경로로 칸도 바꿈
                    if md == self.mode:
                        self.var_wv.set(real)
        elif name == "fsw":
            if kind == "val":
                y = msg[2]
                self.fsw.update(y=y, t=now)
                if math.isfinite(y):
                    self.fsw_samples.append((now, y))
            elif kind == "modq":
                self.fsw["modq"] = msg[2]
                if hasattr(self, "lbl_fsw_modq"):
                    self.lbl_fsw_modq.configure(text="지금 읽는 명령: %s" % msg[2])
            elif kind == "cfg":
                _, _, cent, span, n, on, mx = msg
                self.fsw.update(mx=mx, cent=cent, span=span, mon=on)
                first = not self.fsw.get("filled_once")
                self.lbl_fsw_read.configure(text="장비 현재: Center %s · Span %s · M%d %s" % (
                    fmt_freq(cent), fmt_freq(span), n, fmt_freq(mx) if on else "꺼짐"))
                if first:
                    self.fill_fsw_fields()
                    self.fsw["filled_once"] = True
            elif kind == "fill_fields":
                self.fill_fsw_fields()
            elif kind == "filled":
                self.fill_fsw_fields()
                self.status("FSW 설정 적용됨", "#39ff88")
            elif kind == "connected":
                self.fsw["conn"] = True
                self.fsw["filled_once"] = False
                self.btn_fsw.configure(text="연결 해제")
                self.lbl_fsw_idn.configure(text=msg[2], foreground="#1f4f8f")
                self.status("FSW 연결됨 - 장비 설정값을 불러왔습니다", "#39ff88")
            elif kind == "disconnected":
                self.fsw.update(conn=False, y=None)
                self.btn_fsw.configure(text="연결")
            elif kind == "dpd_pause":
                self.status(("FSW: PA DPD Tester 측정 중 - 잠시 읽기 멈춤 (끝나면 자동으로 다시 읽음)" if msg[2] else
                             "FSW: PA DPD Tester 끝 - 다시 읽는 중"), "#d4a017" if msg[2] else "#39ff88")
            elif kind == "error":
                self.status("FSW: " + msg[2], "#ff6b6b")
            elif kind == "status":
                self.status("FSW: " + msg[2])

    def fill_fsw_fields(self):
        cent, span, mx = self.fsw.get("cent"), self.fsw.get("span"), self.fsw.get("mx")
        if cent is not None and math.isfinite(cent):
            self.var_fsw_cent.set("%.6g" % (cent / 1e9))
        if span is not None and math.isfinite(span):
            self.var_fsw_span.set("%.6g" % (span / 1e6))
        if mx is not None and math.isfinite(mx):
            self.var_fsw_mx.set("%.6g" % (mx / 1e9))

    def update_dashboard(self):
        now = time.time()
        # SMW
        v, sub = self.big["smw"]
        p = self.smw["p"]
        if self.smw["conn"] and p is not None and math.isfinite(p):
            v.configure(text="%.2f dBm" % p, fg=C_SMW if self.smw["rf"] else FG_DIM)
            sub.configure(text="%s · RF %s" % (fmt_freq(self.smw["f"]), "ON" if self.smw["rf"] else "OFF"))
        else:
            v.configure(text="---", fg=FG_DIM)
            sub.configure(text="연결 안 됨" if not self.smw["conn"] else "")
        self.update_rf_button()
        # FSW
        v, sub = self.big["fsw"]
        y = self.fsw["y"]
        if self.fsw["conn"] and y is not None and math.isfinite(y):
            v.configure(text="%.2f dBm" % y, fg=C_FSW)
            sub.configure(text="M%s @ %s" % (self.var_fsw_mk.get(), fmt_freq(self.fsw["mx"])))
        else:
            v.configure(text="---", fg=FG_DIM)
            sub.configure(text="연결 안 됨" if not self.fsw["conn"] else "마커 꺼짐 / 값 없음")
        # 전류
        v, sub = self.big["cur"]
        vm = None
        if self.mode == "mod":
            self.big_title["cur"].configure(text="Id · E36155A (Mod.)")
            self.psu["conn"] = bool(getattr(self, "psu_panel", None) and self.psu_panel.inst["e"] is not None)
            pi = self.psu.get("i")
            amps = pi if (self.psu.get("conn") and pi is not None and math.isfinite(pi) and now - self.psu["t"] < 3) else None
            if amps is not None:
                v.configure(text="%.3f A" % amps if amps >= 1 else "%.1f mA" % (amps * 1e3), fg=C_CUR)
                pv = self.psu.get("v")
                sub.configure(text="E36155A · %.2f V" % pv if pv is not None and math.isfinite(pv) else "E36155A")
                if self.var_psu_vmeas.get() and pv is not None and math.isfinite(pv):
                    vm = pv
            else:
                v.configure(text="---", fg=FG_DIM)
                sub.configure(text="E36155A 연결 안 됨" if not self.psu.get("conn") else "값 없음")
        else:
            self.big_title["cur"].configure(text="Id · RTO (Pulse)")
            t, a = self.cur_last
            if self.scope.running and math.isfinite(a) and now - t < 3:
                v.configure(text=self.scope.format_value(a), fg=C_CUR if not getattr(self.scope, "stale", False) else FG_DIM)
                sub.configure(text=self.scope.lbl_info.cget("text")[:40])
            else:
                v.configure(text="---", fg=FG_DIM)
                sub.configure(text="측정 정지" if self.scope.connected else "연결 안 됨")
            amps = a if (math.isfinite(a) and now - t < 3 and self.scope.running) else None
        # 맨 위 연결 표시
        try:
            pp = self.psu_panel
            st = {"smw": self.smw["conn"], "fsw": self.fsw["conn"], "rto": bool(self.scope.connected),
                  "vdd": pp.inst["e"] is not None, "gate": pp.inst["n"] is not None,
                  "ana": bool(self.analyzer.running()) if HAS_ANALYZER else False}
            for k, c in self.chips.items():
                want = "#69f0ae" if st.get(k) else "#78909c"
                if c.cget("fg") != want:
                    c.configure(fg=want)
        except (AttributeError, tk.TclError):
            pass
        # DUT Gain · PAE (실시간, 케이블 손실 포함)
        v, sub = self.big.get("gain", (None, None))
        if v is None:
            pass
        elif (p is not None and y is not None and math.isfinite(p) and math.isfinite(y)
                and self.smw["conn"] and self.fsw["conn"]):
            _vu = self.eff_vdd()
            c = self.dut_calc(p, y, amps, self.smw["f"], vdd=_vu[0])
            v.configure(text="%.1f dB · %s" % (c["gain"], ("%.1f %%" % c["pae"]) if c["pae"] is not None else "- %"),
                        fg="#ffab40")
            txt = "Pin %.2f · Pout %.2f dBm" % (c["pin"], c["pout"])
            if c.get("de") is not None:
                txt += " · DE %.1f%%" % c["de"]
            if _vu[0]:
                txt += " · Vdd %.1f V" % _vu[0]               # 효율에 쓴 Vdd (파워서플라이 값)
            sub.configure(text=txt)
        else:
            v.configure(text="---", fg=FG_DIM)
            sub.configure(text="")
        self.fit_big()
        # 케이블 손실 / Vdd 안내
        li, lo = self.losses_at(self.smw["f"])
        ix = self.loss_index(self.smw["f"])
        lt_ = "In %.2f dB · Out %.2f dB%s" % (li, lo, ("  (%.6g GHz 행 사용)" % self.loss_table[ix][0]) if ix is not None else "  (고정값)")
        if self.lbl_loss.cget("text") != lt_:
            self.lbl_loss.configure(text=lt_)
            self.lbl_loss2.configure(text="적용 중: " + lt_)
        if ix is not None:
            lt = "케이블 손실 (표 %.4g GHz)  In %.1f · Out %.1f dB" % (self.loss_table[ix][0], li, lo)
        else:
            lt = "케이블 손실 (고정값)  In %.1f · Out %.1f dB" % (li, lo)
        if self.smw.get("f"):
            lt = "SG %.4g GHz → " % (self.smw["f"] / 1e9) + lt
        self.lbl_lossbar.configure(text=lt)
        run = self.scope.running
        want = ("■ 전류 측정 중 (정지)", "#2e7d46", "#3a9656") if run else ("▶ 전류 측정 시작", "#7d6a26", "#93802f")
        if self.btn_cur.cget("text") != want[0]:
            self.btn_cur.configure(text=want[0], bg=want[1], activebackground=want[2])
        self.lbl_mem.configure(text="기억 %d점" % len(self.cur_mem) if self.cur_mem else "")
        self.bias_update()
        self.update_fq_chips()
        if now - getattr(self, "_bias_bar_t", 0) > 1.0:
            self._bias_bar_t = now
            self.update_bias_bar()
            self.update_mode_counts()
            self.fq_card_refresh()
            self.card2_refresh()
            self.sync_left_cards()
        self.update_loss_highlight()
        pp_ = getattr(self, "psu_panel", None)
        lk = (self.ds_live(), bool(self.scope.running or self.sweep), self.mode,
              bool(pp_ is not None and pp_.inst.get("e") is not None and pp_.polling))
        if lk != self._live_key:
            self._live_key = lk
            self.refresh_plots()
        _v, _src = self.eff_vdd()
        self.lbl_vdd.configure(text=("효율 Vdd = %s %.2f V" % (_src, _v)) if (_v and _src != "Vdd 칸") else
                               ("" if fnum(self.var_vdd.get()) else "← Vdd 를 입력해야 Pdc · DE · PAE 가 계산됩니다 (파워서플라이 연결하면 자동)"),
                               foreground="#2e7d32" if (_v and _src != "Vdd 칸") else "#c0504d")
        if not self.var_lvl2.get().strip():
            self.var_lvl2.set("-40")                 # 시작 SG 파워 기본 -40 dBm (SG 지금 레벨을 가져오지 않음)
        if not self.sweep:
            mod = self.mode == "mod"
            t_ = (("● Mod 자동 측정 시작 (→ 목표 Pout %s dBm)" % self.var_mod_target.get()) if mod else
                  "● 자동 측정 시작 (SG %s → %s dBm)" % (self.var_lvl2.get() or "레벨", self.var_smw_max.get()))
            if self.btn_sweep.cget("text") != t_:
                self.btn_sweep.configure(text=t_, bg="#2e7d32", activebackground="#388e3c")

    def auto_record_step(self):
        if not self.rec_on:
            return
        p = self.smw["p"]
        now = time.time()
        if p is None or not math.isfinite(p):
            self.lbl_rec.configure(text="SMW 값 대기 중...")
            return
        if self.sweep or self.ramp or getattr(self.psu_panel, "seq_running", False):
            # 자동 측정 · 천천히 내리기 · 바이어스 켜고 끄는 중에는 이 기록을 쉼 (자동 측정이 직접 기록 → 두 번 기록 방지)
            self.last_pin = p
            self.pending = False
            return
        if self.last_pin is None:
            self.last_pin = p
        elif abs(p - self.last_pin) > 0.004:
            self.last_pin = p
            self.change_t = now
            self.pending = True
        if self.pending:
            settle = max(0.0, fnum(self.var_settle.get(), 1.0))
            avg = max(0.2, fnum(self.var_avg.get(), 1.0))
            left = self.change_t + settle + avg - now
            if left > 0:
                stage = "안정화" if now < self.change_t + settle else "평균 중"
                self.lbl_rec.configure(text="Pin %.2f dBm  %s... %.1f초" % (p, stage, left))
            else:
                self.record(t1=self.change_t + settle, t2=now)
                self.pending = False
                self.lbl_rec.configure(text="다음 파워 변경 대기 중 (Pin %.2f dBm 기록됨)" % p)

    # ------------------------------------------------ 종료
    def collect_settings(self):
        """화면의 모든 설정 → self.s (파일 저장 · 내보내기용)"""
        s = self.s
        if getattr(self, "dpd", None) is not None:
            try:
                self.dpd.save_settings()                 # DPD 측정 설정 (샘플 종류별 포함) → s["dpd"]
            except Exception:
                pass
        s["view"] = self.meas_view()
        s.update(
            smw_host=self.var_smw_host.get(), smw_port=int(fnum(self.var_smw_port.get(), 5025)),
            smw_gtl=self.var_smw_gtl.get(), smw_poll=int(fnum(self.var_smw_poll.get(), 300)),
            smw_step=self.var_smw_step.get(), smw_max=self.var_smw_max.get(),
            fsw_host=self.var_fsw_host.get(), fsw_port=int(fnum(self.var_fsw_port.get(), 5025)),
            fsw_rlev=self.var_fsw_rlev.get(), fsw_rlev_auto=self.var_fsw_rlev_auto.get(),
            fsw_sdir=self.var_fsw_sdir.get(), fsw_state_auto=self.var_fsw_state_auto.get(),
            fsw_modsrc=self.var_fsw_modsrc.get(), fsw_modq=self.var_fsw_modq.get(), mod_popup=self.var_mod_popup.get(),
            fsw_gtl=self.var_fsw_gtl.get(), fsw_poll=int(fnum(self.var_fsw_poll.get(), 300)),
            psu_vmeas=self.var_psu_vmeas.get(), **self.psu_panel.get_settings(),
            fsw_marker=int(self.var_fsw_mk.get()),
            settle=self.var_settle.get(), avg=self.var_avg.get(), vdd=self.var_vdd.get(),
            loss_table=self.loss_table, in_loss=self.var_li.get(), out_loss=self.var_lo.get(),
            freq_ghz=self.var_freq.get(), min_id=self.var_minid.get(), sa_q=self.var_saq.get(),
            pae_ax=[v.get() for v in self.var_pae_ax], gain_ax=[v.get() for v in self.var_gain_ax],
            x_ax=[v.get() for v in self.var_x_ax], sw_return=self.var_sw_ret.get(), sw_idmax=self.var_sw_idmax.get(),
            ramp_dt=self.var_ramp_dt.get(), sw_retry=self.var_sw_retry.get(), sample=self.var_sample.get(),
            stype=self.var_stype.get(), sample_types=self.sample_types,
            sat_on=self.var_sat_on.get(), sat_db=self.var_sat_db.get(),
            tol_id=self.var_tol_id.get(), tol_pae=self.var_tol_pae.get(), retry_minid=self.var_retry_minid.get(),
            rf_ramp_off=self.var_rf_ramp_off.get(), rf_sweep_on=self.var_rf_sweep_on.get(),
auto_bias_off=self.var_auto_bias_off.get(), gain_drop=self.var_gain_drop.get(),
            mode=self.mode, wv_pulse=self.wv_paths["pulse"], wv_mod=self.wv_paths["mod"],
            **{pre + k: self.fqv[md][k].get() for md, pre in (("pulse", "fq_"), ("mod", "mfq_"))
               for k in ("on", "how", "start", "stop", "step", "list", "wait", "auto")},
            ramp_db=self.var_ramp_db.get(), theme=self.s.get("theme", "light"), plot_mode=self.var_plotmode.get(),
            ax_titles=dict(self.ax_titles), left_w=self.hpan.sashpos(0) if self.hpan.winfo_ismapped() else self.s.get("left_w", 470),
            lvl2=self.var_lvl2.get(),
            mod_target=self.var_mod_target.get(), mod_margin=self.var_mod_margin.get(), mod_start=self.var_mod_start.get(),
            mod_step=self.var_mod_step.get(), mod_max=self.var_mod_max.get(), mod_after=self.var_mod_after.get(),
            mod_fine_on=self.var_mod_fine_on.get(), mod_fine_from=self.var_mod_fine_from.get(),
            mod_fine_step=self.var_mod_fine_step.get(),
            geometry=self.root.geometry(), tab=self.nb.index("current"))
        return s

    def on_close(self, keep_file=False):
        if self.dpd_running():
            if not messagebox.askyesno(APP_NAME, "DPD 측정 중입니다. 멈추고 닫을까요?"):
                return False
        if not keep_file and hasattr(self, "psu_panel"):
            if self.psu_panel.seq_running:
                messagebox.showwarning(APP_NAME, "바이어스 자동 순서가 진행 중입니다. 끝나거나 중지한 뒤 닫으세요.")
                return False
            if self.psu_panel.vdd_on() and not messagebox.askyesno(
                    APP_NAME, "Vdd(E36155A) 출력이 켜져 있습니다.\n전원은 그대로 켜 둔 채 프로그램만 닫을까요?\n"
                              "(끄려면 [아니요] → 파워서플라이 화면의 [바이어스 끄기])"):
                return False
        if not self.ask_save_pams():
            return False
        s = self.s
        if not keep_file:
            self.collect_settings()
        if not keep_file:
            save_settings(s)
        all_rows = self.rows_by_mode["pulse"] + self.rows_by_mode["mod"]
        if all_rows:
            # 실수로 닫아도 기록이 남도록 자동 백업
            try:
                d = os.path.join(os.path.dirname(settings_path()), "backup")
                os.makedirs(d, exist_ok=True)
                path = os.path.join(d, "PA_bench_%s.csv" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
                with open(path, "w", newline="", encoding="utf-8-sig") as f:
                    w = csv.writer(f)
                    w.writerow(["측정"] + [c[1] for c in self.COLS])
                    for r in all_rows:
                        w.writerow(["Mod" if r.get("mode") == "mod" else "Pulse"] + list(self.row_values(r)))
            except Exception:
                pass
        try:
            if not keep_file:
                self.root.withdraw()                     # 창은 바로 닫힌 것처럼 (장비 정리는 뒤에서 · 최대 몇 초)
                self.root.update_idletasks()
        except tk.TclError:
            pass
        if getattr(self, "dpd", None) is not None:
            try:
                self.dpd.shutdown()                      # DPD: 측정 멈춤 · 장비 연결 끊기 (응답 없어도 4 초면 넘어감)
            except Exception:
                pass
        for w in (self.smw_w, self.fsw_w):
            w.alive = False
            w.close()
        try:
            self.psu_panel.shutdown()     # 전원 출력은 그대로 둠 (연결만 끊음)
        except Exception:
            pass
        if keep_file:          # 불러온 설정 파일이 덮이지 않게 (전류 화면 설정도)
            self.scope.collect_settings = lambda: None
            self.scope.s = rto.load_settings()
        self.scope.shutdown()
        try:
            self.analyzer.close()
        except Exception:
            pass
        try:   # 남은 예약 작업 취소 (설정 불러오기로 화면을 다시 열 때 오류 방지)
            for aid in self.root.tk.splitlist(self.root.tk.call("after", "info")):
                try:
                    self.root.tk.call("after", "cancel", aid)   # (after_cancel 은 다른 칸이 등록한 명령까지 지워 창 닫기가 멈춤)
                except tk.TclError:
                    pass
        except tk.TclError:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            try:
                self.root.tk.call("destroy", ".")             # 그래도 남은 게 있으면 Tcl 쪽에서 바로 닫음
            except tk.TclError:
                pass
            try:
                self.root.quit()
            except tk.TclError:
                pass
        return True

    # ------------------------------------------------ 설정 파일 (저장 / 불러오기)
    def autosave_settings(self):
        """30초마다 지금 설정을 저장 (갑자기 꺼져도 다음에 그대로 열리게)."""
        now = time.time()
        if now - getattr(self, "_autosave_t", 0) < 30:
            return
        self._autosave_t = now
        try:
            save_settings(self.collect_settings())
            self.scope.collect_settings()
            rto.save_settings(self.scope.s)
        except Exception:
            pass

    def export_settings(self):
        path = filedialog.asksaveasfilename(
            title="설정 저장", defaultextension=".json", initialdir=self.s.get("cfg_dir") or None,
            initialfile="PA_bench_설정_%s.json" % datetime.datetime.now().strftime("%Y%m%d"),
            filetypes=[("PA Bench 설정", "*.json"), ("모든 파일", "*.*")])
        if not path:
            return
        self.s["cfg_dir"] = os.path.dirname(path)
        try:
            self.scope.collect_settings()
        except Exception:
            pass
        doc = {"format": "PA Bench Monitor settings", "version": VERSION,
               "saved": datetime.datetime.now().isoformat(timespec="seconds"),
               "bench": dict(self.collect_settings()), "rto": dict(getattr(self.scope, "s", {}))}
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=2)
        except OSError as e:
            messagebox.showerror(APP_NAME, "설정을 저장하지 못했습니다.\n%s" % e)
            return
        save_settings(self.s)
        self.status("설정 저장: %s" % path, "#39ff88")

    def import_settings(self):
        if self.sweep or self.ramp:
            messagebox.showwarning(APP_NAME, "자동 측정 / 천천히 내리기 중에는 설정을 불러올 수 없습니다.")
            return
        path = filedialog.askopenfilename(
            title="설정 불러오기", initialdir=self.s.get("cfg_dir") or None,
            filetypes=[("PA Bench 설정", "*.json"), ("모든 파일", "*.*")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                doc = json.load(f)
            if doc.get("format") != "PA Bench Monitor settings" or not isinstance(doc.get("bench"), dict):
                raise ValueError("PA Bench Monitor 설정 파일이 아닙니다")
        except Exception as e:
            messagebox.showerror(APP_NAME, "설정을 불러오지 못했습니다.\n%s" % e)
            return
        if not messagebox.askyesno(APP_NAME, "설정을 불러옵니다:\n%s\n(저장: %s, %s)\n\n"
                                             "장비 IP · 주파수 · 손실 표 · 축 · 샘플 종류/.pam 연결 · 전류 측정 설정 등이 바뀌고,\n"
                                             "화면을 다시 엽니다 (장비 연결은 다시 눌러야 함, 지금 기록은 그대로 유지).\n\n진행할까요?"
                                   % (os.path.basename(path), doc.get("saved", "?"), doc.get("version", "?"))):
            return
        b = dict(doc["bench"])
        b["cfg_dir"] = os.path.dirname(path)
        b["geometry"] = self.root.geometry()      # 창 위치 · 크기는 지금 그대로
        save_settings(b)
        if isinstance(doc.get("rto"), dict):
            r = dict(doc["rto"])
            r.pop("geometry", None)
            rto.save_settings(r)
        self.restart = True
        if not self.on_close(keep_file=True):
            self.restart = False


_bench_init = BenchApp.__init__


def _bench_init_ext(self, *a, **k):
    _bench_init(self, *a, **k)
    def _cb_err(et, ev, tb_):                            # 화면 (버튼 · 칸) 오류: 상태 줄에 종류 · 파일 · 줄 번호
        import traceback
        fr = [f for f in traceback.extract_tb(tb_) if str(f.filename).endswith(".py")][-3:]
        where = " ← ".join("%s %d줄 %s" % (os.path.basename(f.filename), f.lineno, f.name) for f in reversed(fr))
        msg = "✖ 오류 %s: %s%s" % (et.__name__, ev, ("  (위치: %s)" % where) if where else "")
        print(msg)
        try:
            self.status(msg, "#ff6b6b")
        except Exception:
            pass
    try:
        self.root.report_callback_exception = _cb_err
    except Exception:
        pass
    try:                                                 # 외부 전원 (드라이버 앰프 · PK 게이트, GPIB) - Site2 판에서는 안 붙음
        import ext_psu
        ext_psu.attach(self)
    except Exception as e:
        print("ext_psu:", e)


BenchApp.__init__ = _bench_init_ext


def main():
    import sys
    if HAS_ANALYZER and "--analyzer-child" in sys.argv:          # PA Analyzer 그래프 창 (별도 프로세스, 같은 exe)
        import analyzer_child
        port = int(sys.argv[sys.argv.index("--analyzer-child") + 1])
        sys.exit(analyzer_child.main(port))
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    keep = None
    while True:   # 설정 불러오기 → 같은 프로그램 안에서 화면을 새 설정으로 다시 엶 (기록은 그대로)
        root = tk.Tk()
        app = BenchApp(root)
        if keep:
            app.rows_by_mode, app.pam_dirty = keep
            app.rows = app.rows_by_mode[app.mode]
            root.after(200, app.renumber)
            root.after(250, lambda: app.status("설정을 불러왔습니다 - 장비는 [모두 연결] 을 다시 눌러 주세요", "#39ff88"))
        root.mainloop()
        if not getattr(app, "restart", False):
            os._exit(0)                                  # 장비 연결 · 스레드가 남아 있어도 프로그램은 확실히 끝냄 (작업 관리자 안 써도)
        app.rows_by_mode[app.mode] = app.rows
        keep = (app.rows_by_mode, app.pam_dirty)


if __name__ == "__main__":
    main()
