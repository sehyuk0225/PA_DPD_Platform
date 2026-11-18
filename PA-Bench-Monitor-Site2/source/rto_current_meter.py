# -*- coding: utf-8 -*-
"""
RTO Current Meter v1.0.9.6
R&S RTO2000 (RTO2044 등) 오실로스코프에서 전류 측정값을 실시간으로 읽어
PC 화면에 디지털 숫자로 크게 띄워주는 프로그램.

- 장비에는 아무것도 설치하지 않음 (LAN, SCPI raw socket 5025 포트 사용)
- 오실로스코프 자체의 자동 측정(MEASurement 슬롯)을 이용:
  채널 / 측정 종류(평균, 최대 ...) / 읽는 시간 구간(Gate)을 PC에서 설정
- IP 칸에 demo 라고 입력하면 장비 없이 화면 동작 확인 가능
"""
import array
import csv
import datetime
import json
import math
import os
import queue
import random
import socket
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

APP_NAME = "RTO Current Meter"
VERSION = "v1.0.9.6"

MEAS_TYPES = [
    ("평균 (MEAN)", "MEAN"),
    ("RMS", "RMS"),
    ("최대 (MAX)", "MAXimum"),
    ("최소 (MIN)", "MINimum"),
    ("High (Top)", "HIGH"),
    ("Low (Base)", "LOW"),
    ("Amplitude", "AMPLitude"),
    ("Peak-Peak", "PDELta"),
]
TIME_UNITS = {"ns": 1e-9, "µs": 1e-6, "ms": 1e-3, "s": 1.0}
CALC_MODES = ["PC 계산 (파형 직접 읽기, 권장)", "장비 측정 (Meas 슬롯)",
              "장비 측정값 그대로 읽기 (스코프 설정 사용)",
              "트리거 레벨 읽기 (트리거 정보창 A: Level 값)"]
STAT_TYPES = [
    ("현재값 (Actual)", "ACTual"),
    ("평균 (Mean/Avg)", "AVG"),
    ("최소 (Min)", "NPEak"),
    ("최대 (Max)", "PPEak"),
    ("RMS", "RMS"),
    ("표준편차 (σ)", "STDDev"),
]
TRIG_MODES = ["Auto", "Normal (권장)", "장비 설정 유지"]


def norm_trig_mode(v):
    v = str(v or "")
    if v.startswith("Auto"):
        return TRIG_MODES[0]
    if v.startswith("장비"):
        return TRIG_MODES[2]
    return TRIG_MODES[1]
DISP_MODES = ["측정값 + T level", "측정값만", "T level만"]
CONV_MODES = ["A 그대로 (전류 프로브)", "션트 저항 (Ω)", "배율 (A/V)"]
DISP_UNITS = ["자동", "A", "mA", "µA"]
INVALID_LIMIT = 1e30  # R&S 장비는 결과 없음 시 9.91E37 반환


def settings_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "RTOCurrentMeter")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "settings.json")


DEFAULTS = {
    "host": "192.168.0.10",
    "port": 5025,
    "channel": 1,
    "meas_label": MEAS_TYPES[0][0],
    "slot": 8,
    "calc_mode": CALC_MODES[0],
    "stat_label": STAT_TYPES[1][0],
    "trk_on": True,
    "trk_start": "200",
    "trk_ratio": "100",
    "trk_floor": "100",
    "trk_settrig": True,
    "trk_mode": TRIG_MODES[1],
    "gate_cursor": True,
    "asc_on": True,
    "show_trig": True,
    "disp_mode": DISP_MODES[0],
    "gate_on": True,
    "gate_start": "10",
    "gate_stop": "50",
    "gate_unit": "µs",
    "conv_mode": CONV_MODES[0],
    "conv_value": "1",
    "interval_ms": 200,
    "disp_unit": "자동",
    "digits": 2,
    "topmost": False,
    "show_settings": True,
    "geometry": "",
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


# ----------------------------------------------------------------- 통신
class ScpiSocket:
    """SCPI raw socket (TCP 5025). VISA 설치 필요 없음."""

    def __init__(self, host, port=5025, timeout=10.0):
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        self.sock.settimeout(timeout)
        self.timeout = timeout
        self.buf = b""

    def write(self, cmd):
        self.sock.sendall((cmd + "\n").encode("ascii"))

    def query(self, cmd):
        self.write(cmd)
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("장비와의 연결이 끊어졌습니다.")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return line.decode("ascii", "replace").strip()

    def _read_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(1 << 20)
            if not chunk:
                raise ConnectionError("장비와의 연결이 끊어졌습니다.")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def query_block(self, cmd):
        """IEEE488.2 binary block (#<n><len><data>) 수신."""
        self.write(cmd)
        while True:
            c = self._read_exact(1)
            if c == b"#":
                break
        nd = int(self._read_exact(1))
        if nd == 0:  # 길이 미정 블록: 줄바꿈까지
            data = b""
            while b"\n" not in self.buf:
                chunk = self.sock.recv(1 << 20)
                if not chunk:
                    raise ConnectionError("장비와의 연결이 끊어졌습니다.")
                self.buf += chunk
            data, self.buf = self.buf.split(b"\n", 1)
            return data
        ln = int(self._read_exact(nd))
        data = self._read_exact(ln)
        if self.buf[:1] == b"\n":
            self.buf = self.buf[1:]
        else:
            try:
                self.sock.settimeout(0.2)
                self._read_exact(1)
            except Exception:
                pass
            finally:
                self.sock.settimeout(self.timeout)
        return data

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


class DemoScope:
    """장비 없이 화면 확인용 가상 오실로스코프 (IP = demo)."""

    def __init__(self):
        self.main = "MEAN"
        self.start = -1e-6
        self.stop = 1e-6
        self.trig = 0.45
        self.t0 = time.time()
        self.last = None

    def _amp(self):
        # 시간에 따라 0.2 A → 1.2 A → 0.2 A 로 천천히 변하는 펄스 전류
        return 0.7 + 0.5 * math.sin((time.time() - self.t0) / 8)

    def write(self, cmd):
        c = cmd.upper()
        val = cmd.split(" ", 1)[1] if " " in cmd else ""
        if ":MAIN " in c:
            self.main = val.strip().upper()
        elif ":ABSOLUTE:START " in c:
            self.start = float(val)
        elif ":ABSOLUTE:STOP " in c:
            self.stop = float(val)
        elif c.startswith("TRIGGER1:LEVEL") and ":VALUE " in c:
            self.trig = float(val)
        elif ":SCALE " in c and c.startswith("CHANNEL"):
            self.scale = float(val)
        elif c.startswith("TRIGGER1:MODE "):
            self.mode = "AUTO" if val.strip().upper().startswith("AUTO") else "NORM"

    def query(self, cmd):
        c = cmd.upper()
        time.sleep(0.01)
        if c == "*IDN?":
            return "Rohde&Schwarz,RTO2044 (DEMO),1332.2531K44/000000,5.50"
        if c.startswith("SYST") and "ERR" in c:
            return '0,"No error"'
        if c == "*OPC?":
            return "1"
        if c.startswith("TRIGGER1:LEVEL") and c.endswith("?"):
            return "%.6E" % self.trig
        if c == "TRIGGER1:SOURCE?":
            return "CHAN1"
        if c == "TRIGGER1:TYPE?":
            return "EDGE"
        if c.endswith(":MAIN?"):
            return "MEAN"
        if c.endswith(":SOURCE?"):
            return "C1W1"
        if c.endswith(":ENABLE?"):
            return "1"
        if ":SCALE?" in c:
            return "%.6E" % getattr(self, "scale", 0.05)
        if c == "TRIGGER1:MODE?":
            return getattr(self, "mode", "AUTO")
        if ":DATA:HEADER?" in c:
            return "-5.0E-05,5.0E-05,20000,1"
        if ":ABSOLUTE:START?" in c:
            return "%.6E" % self.start
        if ":ABSOLUTE:STOP?" in c:
            return "%.6E" % self.stop
        if ":RESULT" in c:
            base = 0.850 + 0.02 * math.sin((time.time() - self.t0) / 3)
            noise = random.gauss(0, 0.002)
            factor = {"MAXIMUM": 1.05, "MINIMUM": 0.95, "RMS": 1.01,
                      "AMPLITUDE": 0.84, "PDELTA": 0.9, "LOW": 0.01}.get(self.main, 1.0)
            return "%.6E" % ((base + noise) * factor)
        return ""

    def query_block(self, cmd):
        # 가상 펄스 파형: -50 µs ~ 50 µs, 펄스 ON 0 ~ 40 µs
        n = 20000
        xs, xe = -50e-6, 50e-6
        dt = (xe - xs) / n
        base = self._amp()
        if self.trig > base and self.last is not None and getattr(self, "mode", "AUTO") == "NORM":
            return self.last  # 트리거 레벨이 펄스보다 높으면 새 파형 없음 (Normal 모드)
        vals = array.array("f")
        for i in range(n):
            t = xs + i * dt
            v = base if 0 <= t < 40e-6 else 0.02
            v += random.gauss(0, 0.004)
            vals.append(min(v, 8 * getattr(self, "scale", 0.05)))  # 화면 위 클리핑 흉내
        self.last = vals.tobytes()
        return self.last

    def close(self):
        pass


def nice_ceil(x):
    """x 이상인 가장 작은 1-2-5 단계 값."""
    if x <= 0:
        return 1e-3
    e = math.floor(math.log10(x))
    for m in (1, 2, 5, 10):
        v = m * 10 ** e
        if v >= x * (1 - 1e-9):
            return v
    return 10 ** (e + 1)


def calc_stat(seg, meas):
    n = len(seg)
    if n == 0:
        return float("nan")
    if meas == "MEAN":
        return math.fsum(seg) / n
    if meas == "RMS":
        return math.sqrt(math.fsum(x * x for x in seg) / n)
    mx, mn = max(seg), min(seg)
    if meas == "MAXimum":
        return mx
    if meas == "MINimum":
        return mn
    if meas == "PDELta":
        return mx - mn
    # HIGH / LOW / AMPLitude: 중간값 기준 위/아래 그룹의 중앙값
    mid = (mx + mn) / 2
    hi = sorted(x for x in seg if x >= mid)
    lo = sorted(x for x in seg if x < mid) or hi
    high = hi[len(hi) // 2]
    low = lo[len(lo) // 2]
    if meas == "HIGH":
        return high
    if meas == "LOW":
        return low
    return high - low


class Worker(threading.Thread):
    """모든 장비 통신을 하나의 스레드에서 처리 (명령이 섞이지 않도록)."""

    def __init__(self, out_q):
        super().__init__(daemon=True)
        self.out = out_q
        self.tasks = queue.Queue()
        self.inst = None
        self.polling = False
        self.interval = 0.2
        self.slot = 8
        self.cfg = None
        self.trk = None
        self.asc = None
        self.idle_manual = None
        self.sg_low = False
        self.low_snap = None                  # (트리거 레벨, 스케일) - 저전력에서 잘 잡혔던 상태
        self.last_peak = None
        self.show_trig = False
        self.trig_src_t = 0.0
        self.alive = True

    def post(self, *msg):
        self.out.put(msg)

    def _close(self):
        if self.inst:
            self.inst.close()
        self.inst = None
        self.polling = False

    def _errors(self):
        errs = []
        for _ in range(20):
            e = self.inst.query("SYSTem:ERRor?")
            if e.startswith("0") or e.startswith("+0"):
                break
            errs.append(e)
        return errs

    def _do(self, task):
        kind = task[0]
        if kind == "connect":
            _, host, port = task
            self._close()
            self.post("status", "연결 중... %s" % host, "#b8860b")
            if host.strip().lower() == "demo":
                self.inst = DemoScope()
            else:
                self.inst = ScpiSocket(host.strip(), port)
            idn = self.inst.query("*IDN?")
            self.inst.write("*CLS")
            # 원격 제어 중에도 스코프 화면이 계속 갱신되도록 (기본값은 화면 멈춤)
            self.inst.write("SYSTem:DISPlay:UPDate ON")
            self.inst.write("SYSTem:KLOCk OFF")
            self.inst.query("*OPC?")
            self._errors()
            self.post("connected", idn)
        elif kind == "disconnect":
            self._close()
            self.post("disconnected")
        elif kind == "apply":
            cfg = task[1]
            if not self.inst:
                raise ConnectionError("먼저 장비에 연결하세요.")
            self.cfg = cfg
            if cfg["pc_calc"]:
                self._apply_pc(cfg)
                return
            if cfg.get("trig_read"):
                src = self.inst.query("TRIGger1:SOURce?").strip().upper()
                try:
                    typ = self.inst.query("TRIGger1:TYPE?").strip()
                except Exception:
                    typ = ""
                digits = "".join(ch for ch in src if ch.isdigit())
                if src.startswith("CHAN") and digits:
                    n = int(digits[0])
                elif src.startswith("EXT"):
                    n = 5
                else:
                    n = 1
                self.trig_n = n
                errs = self._errors()
                self.post("applied_read", errs, "A: %s %s · Level 값 읽는 중" % (typ.title(), src.title()))
                return
            m = cfg["slot"]
            self.slot = m
            if cfg.get("scope_read"):
                # 스코프 설정은 그대로 두고, 결과만 읽음
                if cfg["stat"] != "ACTual":
                    self.inst.write("MEASurement%d:STATistics:ENABle ON" % m)
                self.inst.query("*OPC?")
                errs = self._errors()
                try:
                    info = "Meas%d · %s · 소스 %s" % (
                        m, self.inst.query("MEASurement%d:MAIN?" % m),
                        self.inst.query("MEASurement%d:SOURce?" % m))
                except Exception:
                    info = "Meas%d" % m
                try:
                    en = self.inst.query("MEASurement%d:ENABle?" % m).strip()
                    if en in ("0", "OFF"):
                        info += "  ⚠ 스코프에서 이 Meas가 꺼져 있음"
                except Exception:
                    pass
                self.post("applied_read", errs, info)
                return
            cmds = [
                "MEASurement%d:ENABle ON" % m,
                "MEASurement%d:SOURce C%dW1" % (m, cfg["channel"]),
                "MEASurement%d:MAIN %s" % (m, cfg["meas"]),
            ]
            if cfg["gate_on"]:
                cmds += [
                    "MEASurement%d:GATE:MODE ABS" % m,
                    "MEASurement%d:GATE:ABSolute:STARt %.9E" % (m, cfg["t_start"]),
                    "MEASurement%d:GATE:ABSolute:STOP %.9E" % (m, cfg["t_stop"]),
                    # 시작/끝 순서에 따른 장비 자동 보정 방지용으로 한 번 더 설정
                    "MEASurement%d:GATE:ABSolute:STARt %.9E" % (m, cfg["t_start"]),
                    "MEASurement%d:GATE:STATe ON" % m,
                ]
            else:
                cmds += ["MEASurement%d:GATE:STATe OFF" % m]
            for c in cmds:
                self.inst.write(c)
            self.inst.query("*OPC?")
            errs = self._errors()
            actual = None
            if cfg["gate_on"]:
                try:
                    a = float(self.inst.query("MEASurement%d:GATE:ABSolute:STARt?" % m))
                    b = float(self.inst.query("MEASurement%d:GATE:ABSolute:STOP?" % m))
                    actual = (a, b)
                except Exception:
                    actual = None
            self.slot = m
            self.post("applied", errs, actual)
        elif kind == "track_start":
            t = dict(task[1])
            if not self.inst:
                raise ConnectionError("먼저 장비에 연결하세요.")
            ch = t["channel"]
            self.inst.write("RUN")  # 스코프가 STOP 상태면 연속 측정으로
            if t["settrig"]:
                for c in ["TRIGger1:SOURce CHAN%d" % ch, "TRIGger1:TYPE EDGE",
                          "TRIGger1:EDGE:SLOPe POSitive"]:
                    self.inst.write(c)
            if t["mode"] == "AUTO":
                self.inst.write("TRIGger1:MODE AUTO")
            elif t["mode"] == "NORM":
                self.inst.write("TRIGger1:MODE NORMal")
            try:
                t["mode_actual"] = self.inst.query("TRIGger1:MODE?").upper()
            except Exception:
                t["mode_actual"] = ""
            t["level"] = max(t["start"] * t["ratio"], t["floor"])
            t["last_set"] = 0.0
            t["last_sig"] = None
            t["stale_since"] = None
            t["last_good"] = t["start"]
            t["search"] = None
            t["floor_since"] = None
            t["avg"] = []
            self.trk = t
            self._set_level(t["level"], "측정 시작 레벨")
            errs = self._errors()
            if errs:
                self.post("error", "트리거 설정 오류: " + " / ".join(errs))
        elif kind == "show_trig":
            self.show_trig = task[1]
            self.trig_src_t = 0.0
        elif kind == "stat_reset":
            self.inst.write("MEASurement%d:STATistics:RESet" % task[1])
        elif kind == "track_stop":
            self.trk = None
        elif kind == "trk_hint":
            # SG 파워가 바뀜 → 예상 전류 위치로 트리거를 바로 옮기고, 방향 우선 탐색 준비
            _, pred, direction, why = task
            t = self.trk
            if t and direction < 0:
                t["down_ok"] = time.time() + 4.0          # SG 내림 뒤 4초 동안은 측정값 따라 내려가도 됨
            if t and t.get("paused"):
                pass  # SG RF OFF 중
            elif t and t.get("search") and t.get("stale_since") is not None:
                pass  # 이미 트리거를 찾는 중이면 끊지 않고 계속 탐색
            elif t and pred is not None and math.isfinite(pred) and pred > 0:
                new_lv = max(pred * t["ratio"], t["floor"])
                if direction > 0:
                    # 파워 올림: 펄스가 커지므로 지금 레벨(이전 평균)에서도 트리거는 계속 걸림
                    # → 미리 올리지 않고, 새 파형의 평균이 나오면 그 값으로 따라감 (미리 올렸다가 놓치는 일 없음)
                    t["avg"] = []
                else:
                    # 파워 내림: 레벨이 새 펄스보다 높으면 놓치므로 예상값으로 미리 내림
                    new_lv = min(new_lv, t["level"])
                    t["search"] = self._new_search(pred, -1)
                    t["stale_since"] = None
                    t["floor_since"] = None
                    t["hint_t"] = time.time()
                    t["avg"] = []
                    self._set_level(new_lv, why)
        elif kind == "scale_start":
            ch = task[1]
            pos = -3.0  # 0 A 선을 아래쪽(아래에서 2칸)에 두어 위로 8칸 사용
            self.inst.write("CHANnel%d:POSition %.2f" % (ch, pos))
            self.inst.write("CHANnel%d:OFFSet 0" % ch)
            try:
                sc = float(self.inst.query("CHANnel%d:SCALe?" % ch))
            except Exception:
                sc = 0.1
            self._errors()
            self.asc = {"channel": ch, "pos": pos, "scale": sc, "last": 0.0}
            self.post("scale", sc, "현재값")
        elif kind == "scale_stop":
            self.asc = None
        elif kind == "sg_low":
            # SG 파워가 저전력 기준 아래로 내려옴 → 잘 잡혔던 낮은 파워 상태로 되돌림 (없으면 Idq 기준)
            self.sg_low = True
            self._low_restore(task[1], "SG 저전력")
        elif kind == "sg_high":
            self.sg_low = False
        elif kind == "rf_idle":
            # SG RF OFF: 트리거 추적 멈춤 + 대기 전류(peak)가 화면 75% 안에 크게 보이는 가장 작은 스케일 (mA 화면)
            if self.trk:
                self.trk["paused"] = True
                self.trk["search"] = None
            a = self.asc
            if a:
                if not a.get("idle"):
                    a["rf_scale"] = a["scale"]
                a["idle"] = True
                self._scale_now(nice_ceil(task[1] / ((5.0 - a["pos"]) * 0.75)), "SG RF OFF → 대기 전류 크게")
            elif self.inst and self.cfg:
                # 세로 스케일 자동이 꺼져 있어도 RF OFF 때는 대기 전류를 크게 (RF ON 하면 원래 스케일로)
                ch = self.cfg["channel"]
                try:
                    pos = float(self.inst.query("CHANnel%d:POSition?" % ch))
                    old = float(self.inst.query("CHANnel%d:SCALe?" % ch))
                except Exception:
                    pos, old = 0.0, None
                if old is not None:
                    if self.idle_manual is None:
                        self.idle_manual = (ch, old)
                    new = nice_ceil(task[1] / (max(5.0 - pos, 1.0) * 0.75))
                    self.inst.write("CHANnel%d:SCALe %.6E" % (ch, new))
                    self.post("scale", new, "SG RF OFF → 대기 전류 크게")
        elif kind == "rf_on":
            # SG RF ON: 트리거 추적 다시 + RF 켜져 있던 때 스케일로 바로 (작은 스케일에서 한 칸씩 키우면 느림)
            if self.trk:
                self.trk.update(paused=False, stale_since=None, floor_since=None, avg=[], down_ok=time.time() + 4.0)
            a = self.asc
            if self.sg_low:
                # 저전력에서 RF ON (Mod. 다음 주파수 등): 높은 파워 때 스케일이 아니라 잘 잡혔던 낮은 파워 상태로
                if a:
                    a["idle"] = False
                self.idle_manual = None
                self._low_restore(task[1] if len(task) > 1 else None, "SG RF ON (저전력)")
                return
            if a and a.get("idle"):
                a["idle"] = False
                if a.get("rf_scale"):
                    self._scale_now(a["rf_scale"], "SG RF ON → 이전 스케일")
            elif self.idle_manual is not None and self.inst:
                ch, old = self.idle_manual
                self.inst.write("CHANnel%d:SCALe %.6E" % (ch, old))
                self.post("scale", old, "SG RF ON → 이전 스케일")
            self.idle_manual = None
        elif kind == "poll":
            self.polling = task[1]
            self.interval = task[2]

    # ---------- 트리거 레벨 자동 추적 ----------
    def _set_level(self, level, why):
        t = self.trk
        self.inst.write("TRIGger1:LEVel%d:VALue %.6E" % (t["channel"], level))
        t["level"] = level
        t["last_set"] = time.time()
        actual = None
        try:
            actual = float(self.inst.query("TRIGger1:LEVel%d:VALue?" % t["channel"]))
        except Exception:
            pass
        self.post("trig", level, why, actual)

    def _new_search(self, center, direction):
        """예상 전류(center) 주변을 방향 우선으로 탐색할 레벨 목록 (트리거 레벨 = 전류 × 비율)."""
        t = self.trk
        # 예상 위치 바로 근처부터 위·아래를 번갈아 넓혀 가되, 파워가 움직인 방향을 먼저 봄
        # (레벨이 펄스 꼭대기보다 높아도, 바닥(대기 전류)보다 낮아도 트리거가 안 걸리므로 양쪽 다 확인)
        # 가까운 곳부터 점점 넓게 (약 0.2배 ~ 2.5배까지)
        # 놓치는 건 거의 항상 레벨이 펄스보다 높을 때 → 아래쪽을 먼저, 위쪽은 나중에 조금만
        if direction < 0:      # 파워 내림: 아래 먼저
            mults = [0.85, 0.72, 0.6, 0.5, 1.1, 0.42, 0.35, 0.28, 1.25, 0.22, 0.17, 1.45]
        else:
            mults = [0.88, 0.76, 0.65, 1.1, 0.55, 0.46, 1.25, 0.38, 0.3, 1.45, 0.24, 0.18]
        levels = []
        for m in mults:
            lv = max(center * m * t["ratio"], t["floor"])
            if all(abs(lv - x) > 1e-9 for x in levels):
                levels.append(lv)
        return {"levels": levels, "n": len(levels), "dir": direction, "center": center}

    def _track(self, v, sig, top=None):
        t = self.trk
        if not t or t.get("paused"):
            return                     # SG RF OFF 동안은 트리거를 찾지 않음 (펄스가 없음)
        now = time.time()
        dwell = max(0.6, 2.5 * self.interval)   # 한 레벨에서 트리거를 기다리는 시간
        sr = t.get("search")
        if sig is not None and sig == t["last_sig"]:
            # 파형이 그대로 = 새 트리거 없음
            if t["stale_since"] is None:
                t["stale_since"] = now
            if now - t["stale_since"] < (dwell if sr else 1.5):
                return  # 스코프 갱신 주기가 느린 경우 오판 방지
            auto = t.get("mode_actual", "").startswith("AUTO")
            self.post("stale", "auto" if auto else True)
            if auto:
                return  # Auto 모드에서 파형이 안 바뀌면 스코프 STOP 상태 → 레벨은 건드리지 않음
            if now - t["last_set"] < dwell:
                return
            if sr is None or not sr["levels"]:
                # 처음 놓쳤거나 한 바퀴 다 돌았음 → 마지막으로 잘 잡힌 전류 주변을 다시 탐색
                if t.get("floor_since") is None:
                    t["floor_since"] = now          # 탐색 시작 시각 (오래 못 찾으면 Auto 전환용)
                if now - t["floor_since"] > 20.0 and t.get("mode_actual", "").startswith("NORM"):
                    self.inst.write("TRIGger1:MODE AUTO")
                    t["mode_actual"] = "AUTO"
                    t["search"] = None
                    self.post("error", "20초 동안 트리거를 못 찾아 Auto 모드로 전환했습니다 "
                                       "(RF OFF / 트리거 소스·극성 확인, 측정 시작을 다시 누르면 Normal 로 복귀)")
                    return
                sr = t["search"] = self._new_search(t.get("last_good", t["start"]), 0)
            lv = sr["levels"].pop(0)
            k = sr["n"] - len(sr["levels"])
            arrow = "↓" if sr["dir"] < 0 else ("↑" if sr["dir"] > 0 else "↕")
            self._set_level(lv, "트리거 찾는 중 %s %d/%d" % (arrow, k, sr["n"]))
            return
        # 새 파형 = 트리거 걸림
        t["last_sig"] = sig
        t["stale_since"] = None
        t["floor_since"] = None
        self.post("stale", False)
        if not math.isfinite(v) or v <= 0:
            return
        if sr is not None:
            t["search"] = None
            self.post("trig", t["level"], "트리거 찾음 (%.4g)" % v, None)
        t["last_good"] = v
        if t.get("search") is None:
            self._low_snap()
        # 레벨 = 평균과 펄스 꼭대기 사이: 평균 + (꼭대기 - 평균) × 30%  (최근 새 파형 3개 평균값으로)
        #   꼭대기를 모르면 (스코프 측정 읽기 모드) 평균 × 비율
        avg = t.setdefault("avg", [])
        avg.append((v, top if (top is not None and math.isfinite(top)) else None))
        del avg[:-3]
        m = sum(x[0] for x in avg) / len(avg)
        tops = [x[1] for x in avg if x[1] is not None]
        target = m * t["ratio"]
        how = "평균"
        if tops:
            tp = sum(tops) / len(tops)
            if tp > m:
                target = max(target, m + 0.3 * (tp - m))
                how = "평균~꼭대기 30%"
        target = max(target, t["floor"])
        lv = t["level"]
        if now - t["last_set"] < 0.3:
            return
        if target > lv * 1.04:
            self._set_level(target, "측정값 따라 ↑ (%s)" % how)
        elif target < lv * 0.96 and now < t.get("down_ok", 0):
            # 내려가는 건 SG 파워를 내린 직후 다시 측정할 때만 (그 밖에는 잡은 레벨을 지킴)
            self._set_level(target, "SG 내림 → 측정값 따라 ↓ (%s)" % how)

    # ---------- 트리거 레벨 표시 ----------
    def _read_trig_display(self):
        now = time.time()
        if now - self.trig_src_t > 2.0:  # 소스/종류는 2초마다 확인
            src = self.inst.query("TRIGger1:SOURce?").strip().upper()
            try:
                typ = self.inst.query("TRIGger1:TYPE?").strip().title()
            except Exception:
                typ = ""
            digits = "".join(ch for ch in src if ch.isdigit())
            if src.startswith("CHAN") and digits:
                self.disp_n, name = int(digits[0]), "Ch" + digits[0]
            elif src.startswith("EXT"):
                self.disp_n, name = 5, "Ext"
            else:
                self.disp_n, name = 1, src.title()
            self.disp_label = "A: %s %s" % (typ, name)
            self.trig_src_t = now
        r = self.inst.query("TRIGger1:LEVel%d:VALue?" % self.disp_n)
        try:
            v = float(r)
        except ValueError:
            v = float("nan")
        self.post("trigval", v, self.disp_label)

    # ---------- 세로 스케일 자동 ----------
    def _cur_scale(self):
        a = self.asc
        if a:
            return a["channel"], a["scale"]
        if self.inst and self.cfg:
            ch = self.cfg["channel"]
            try:
                return ch, float(self.inst.query("CHANnel%d:SCALe?" % ch))
            except Exception:
                pass
        return None, None

    def _set_scale_any(self, new, why):
        if self.asc:
            self._scale_now(new, why)
        elif self.inst and self.cfg:
            self.inst.write("CHANnel%d:SCALe %.6E" % (self.cfg["channel"], new))
            self.post("scale", new, why)

    def _low_snap(self):
        """저전력 (SG ≤ 기준) 에서 새 파형으로 잘 잡혔을 때 그 레벨 · 스케일을 기억"""
        t = self.trk
        if not t or t.get("paused") or not self.sg_low:
            return
        now = time.time()
        if now - getattr(self, "_snap_t", 0) < 1.0:
            return
        self._snap_t = now
        ch, sc = self._cur_scale()
        self.low_snap = (t["level"], sc)

    def _low_restore(self, idq_raw, why):
        """잘 잡혔던 낮은 파워 상태로 (C: 기억한 값 → 없으면 A: 스케일 = Idq 크게, 레벨 = Idq × 1.1)"""
        t = self.trk
        if self.low_snap:
            lv, sc = self.low_snap
            how = "잡혔던 상태로"
        else:
            if idq_raw is None or not (idq_raw == idq_raw) or idq_raw <= 0:
                return
            lv, sc = idq_raw * 1.1, None
            a = self.asc
            pos = a["pos"] if a else -3.0
            sc = nice_ceil(idq_raw * 1.3 / ((5.0 - pos) * 0.75))
            how = "Idq 기준"
        if sc:
            self._set_scale_any(sc, "%s → %s" % (why, how))
        if t and lv:
            t.update(search=None, stale_since=None, floor_since=None, avg=[], down_ok=time.time() + 4.0)
            self._set_level(max(lv, t["floor"]), "%s → %s" % (why, how))

    def _scale_now(self, new, why):
        a = self.asc
        self.inst.write("CHANnel%d:SCALe %.6E" % (a["channel"], new))
        try:
            new = float(self.inst.query("CHANnel%d:SCALe?" % a["channel"]))
        except Exception:
            pass
        a["scale"] = new
        a["last"] = time.time()
        a["down_cnt"] = 0
        self.post("scale", new, why)

    def _autoscale(self, v, peak):
        a = self.asc
        if not a or a.get("idle"):
            return                     # RF OFF 동안은 대기 전류 화면 유지 (화면에 남은 옛 펄스로 다시 키우지 않음)
        top = peak if (peak is not None and math.isfinite(peak)) else v
        if not math.isfinite(top) or top <= 0:
            return
        now = time.time()
        if now - a["last"] < 0.5:
            return
        divs = 5.0 - a["pos"]  # 0 A 선부터 화면 맨 위까지 칸 수
        ratio = top / (divs * a["scale"])
        # 최대값이 화면 높이의 75% 이하로 들어가는 가장 작은 1-2-5 스케일
        ideal = nice_ceil(top / (divs * 0.75))
        if ratio > 0.9:
            new = max(ideal, a["scale"] * 1.5)  # 올라갈 때: 클리핑 방지 (즉시)
            a["down_cnt"] = 0
        elif ideal < a["scale"] * 0.99:
            # 내려갈 때: 더 작은 스케일에 들어가면 바로 줄임 (노이즈 대비 2회 연속 확인)
            a["down_cnt"] = a.get("down_cnt", 0) + 1
            if a["down_cnt"] < 2:
                return
            new = ideal
            a["down_cnt"] = 0
        else:
            a["down_cnt"] = 0
            return
        new = nice_ceil(new)
        self.inst.write("CHANnel%d:SCALe %.6E" % (a["channel"], new))
        try:
            new = float(self.inst.query("CHANnel%d:SCALe?" % a["channel"]))
        except Exception:
            pass
        a["scale"] = new
        a["last"] = now
        self.post("scale", new, "전류 증가 ↑" if ratio > 0.9 else "전류 감소 ↓")

    # ---------- PC 계산 모드 ----------
    def _apply_pc(self, cfg):
        ch = cfg["channel"]
        self.inst.write("FORMat:DATA REAL,32")
        self.inst.write("FORMat:BORDer LSBFirst")
        errs = self._errors()
        self.export_part = False
        if cfg["gate_on"]:
            # 전송량 줄이기: 구간 부분만 받아오도록 시도 (안 되면 전체 파형을 받아 PC에서 자름)
            self.inst.write("EXPort:WAVeform:SCOPe MANual")
            self.inst.write("EXPort:WAVeform:STARt %.9E" % (cfg["t_start"] - 1e-9))
            self.inst.write("EXPort:WAVeform:STOP %.9E" % (cfg["t_stop"] + 1e-9))
            self.inst.query("*OPC?")
            if self._errors():
                self.inst.write("EXPort:WAVeform:SCOPe WFM")
                self.inst.write("*CLS")
            else:
                self.export_part = True
        else:
            self.inst.write("EXPort:WAVeform:SCOPe WFM")
            self.inst.query("*OPC?")
            self._errors()
        if cfg["gate_on"] and cfg.get("cursor"):
            c = cfg.get("cursor_set", 2)
            for cmd in ("CURSor%d:SOURce C%dW1" % (c, ch), "CURSor%d:FUNCtion VERTical" % c,
                        "CURSor%d:X1Position %.9E" % (c, cfg["t_start"]),
                        "CURSor%d:X2Position %.9E" % (c, cfg["t_stop"]), "CURSor%d:STATe ON" % c):
                self.inst.write(cmd)
            self.inst.query("*OPC?")
            cerr = self._errors()
            if cerr:
                self.post("error", "구간 커서 표시 실패 (측정은 정상): " + " / ".join(cerr))
        hdr = self.inst.query("CHANnel%d:DATA:HEADer?" % ch)
        info = None
        try:
            xs, xe, n = [float(x) for x in hdr.split(",")[:3]]
            info = (xs, xe, int(n))
        except Exception:
            errs.append("파형 헤더 읽기 실패: " + hdr)
        self.post("applied_pc", errs, info, self.export_part)

    def _read_pc(self, cfg):
        ch = cfg["channel"]
        hdr = self.inst.query("CHANnel%d:DATA:HEADer?" % ch)
        parts = hdr.split(",")
        xs, xe, n = float(parts[0]), float(parts[1]), int(float(parts[2]))
        vps = int(float(parts[3])) if len(parts) > 3 else 1
        raw = self.inst.query_block("CHANnel%d:DATA?" % ch)
        self.last_sig = hash(raw)
        vals = array.array("f")
        vals.frombytes(raw[: len(raw) // 4 * 4])
        if n <= 0 or len(vals) == 0:
            return float("nan"), "파형 데이터 없음 (채널이 꺼져 있거나 트리거 전)"
        self.last_peak = max(vals)
        if vps > 1:  # 피크검출/엔벨로프: 값 쌍
            n = len(vals) // vps
        dt = (xe - xs) / n
        if cfg["gate_on"]:
            t1, t2 = cfg["t_start"], cfg["t_stop"]
            if t2 < xs or t1 > xe:
                return float("nan"), "구간이 파형 범위(%s ~ %s) 밖입니다 - 스코프 시간축/Position 확인" % (
                    fmt_time(xs), fmt_time(xe))
            i1 = max(0, int(math.ceil((t1 - xs) / dt - 1e-9)))
            i2 = min(n, int(math.floor((t2 - xs) / dt + 1e-9)) + 1)
            if i2 <= i1:
                return float("nan"), "구간 안에 샘플이 없습니다 (구간을 넓히세요)"
            seg = vals[i1 * vps: i2 * vps]
            clipped = t1 < xs or t2 > xe
        else:
            seg = vals
            clipped = False
        v = calc_stat(seg, cfg["meas"])
        # 펄스 윗부분 대표값 (구간 안 · 중간값 위쪽 샘플의 90% 지점 - 튀는 스파이크 1~2개는 무시)
        try:
            mx, mn = max(seg), min(seg)
            hi = sorted(x for x in seg if x >= (mx + mn) / 2)
            self.last_top = hi[int(0.9 * (len(hi) - 1))] if hi else float("nan")
        except ValueError:
            self.last_top = float("nan")
        diag = "파형 %s ~ %s · 구간 내 %s pt" % (fmt_time(xs), fmt_time(xe), format(len(seg) // vps, ","))
        if clipped:
            diag += " · ⚠ 구간 일부가 파형 범위 밖"
        return v, diag

    def run(self):
        while self.alive:
            t0 = time.time()
            try:
                while True:
                    try:
                        task = self.tasks.get_nowait()
                    except queue.Empty:
                        break
                    self._do(task)
                if self.polling and self.inst and self.cfg and self.cfg["pc_calc"]:
                    v, diag = self._read_pc(self.cfg)
                    fresh = self.last_sig != getattr(self, "_prev_sig", None)  # 새로 들어온 파형인지
                    self._prev_sig = self.last_sig
                    a = self.asc
                    if a and self.last_peak is not None and math.isfinite(self.last_peak) \
                            and self.last_peak >= 0.97 * (5.0 - a["pos"]) * a["scale"]:
                        fresh = False   # 화면 위로 잘린(클리핑) 파형 → 값이 실제보다 작으므로 사용 안 함
                    self.post("value", v, time.time(), fresh)
                    self._track(v, self.last_sig, getattr(self, "last_top", None))
                    self._autoscale(v, self.last_peak)
                    if diag:
                        self.post("diag", diag)
                elif self.polling and self.inst and self.cfg and self.cfg.get("trig_read"):
                    r = self.inst.query("TRIGger1:LEVel%d:VALue?" % getattr(self, "trig_n", 1))
                    try:
                        v = float(r)
                    except ValueError:
                        v = float("nan")
                    if not math.isfinite(v) or abs(v) > INVALID_LIMIT:
                        v = float("nan")
                    self.post("value", v, time.time())
                    self._autoscale(v, None)
                elif self.polling and self.inst:
                    stat = self.cfg.get("stat", "ACTual") if (self.cfg and self.cfg.get("scope_read")) else "ACTual"
                    r = self.inst.query("MEASurement%d:RESult:%s?" % (self.slot, stat))
                    try:
                        v = float(r)
                    except ValueError:
                        v = float("nan")
                    if not math.isfinite(v) or abs(v) > INVALID_LIMIT:
                        v = float("nan")
                    fresh = r != getattr(self, "_prev_r", None)
                    self._prev_r = r
                    self.post("value", v, time.time(), fresh)
                    self._track(v, r)
                    self._autoscale(v, None)
            except (socket.timeout, TimeoutError):
                self.post("error", "응답 시간 초과 - 연결을 다시 하세요. (IP / LAN 확인)")
                self._close()
                self.post("disconnected")
            except Exception as e:
                self.post("error", str(e))
                if isinstance(e, (ConnectionError, OSError)):
                    self._close()
                    self.post("disconnected")
            try:
                if self.polling and self.inst and self.show_trig:
                    self._read_trig_display()
            except (socket.timeout, TimeoutError, ConnectionError, OSError):
                pass
            except Exception:
                pass
            wait = (self.interval if self.polling else 0.05) - (time.time() - t0)
            if wait > 0:
                time.sleep(wait)


# ----------------------------------------------------------------- 화면
def fmt_time(sec):
    a = abs(sec)
    if a >= 1:
        return "%g s" % sec
    if a >= 1e-3:
        return "%g ms" % round(sec * 1e3, 6)
    if a >= 1e-6:
        return "%g µs" % round(sec * 1e6, 6)
    return "%g ns" % round(sec * 1e9, 6)


class App:
    def __init__(self, root, container=None):
        self.root = root
        self.embedded = container is not None
        self.c = container if container is not None else root
        self.s = load_settings()
        self.q = queue.Queue()
        self.worker = Worker(self.q)
        self.worker.start()
        self.connected = False
        self.running = False
        self.log_file = None
        self.log_writer = None
        self.reset_stats()

        if not self.embedded:
            root.title("%s %s" % (APP_NAME, VERSION))
            root.minsize(380, 200)
        if self.embedded:
            pass
        elif self.s.get("geometry"):
            try:
                root.geometry(self.s["geometry"])
            except Exception:
                pass
        else:
            root.geometry("760x700")

        style = ttk.Style()
        try:
            style.theme_use("vista")
        except Exception:
            pass
        ui_font = ("맑은 고딕", 9)
        root.option_add("*Font", ui_font)

        self.build_display()
        self.build_settings()
        if not self.s.get("normal_once"):
            # v1.0.9.3: 트리거 모드 기본값을 Normal 로 한 번 바꿔 줌 (이후엔 사용자가 고른 값 유지)
            self.var_trk_mode.set(TRIG_MODES[1])
            self.s["normal_once"] = True
        self.root.after(10, self.apply_visibility)
        if not self.embedded:
            root.attributes("-topmost", bool(self.s["topmost"]))
            root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(50, self.pump)

    # --------------------------------------------- 큰 숫자 표시부
    def build_display(self):
        self.disp = tk.Frame(self.c, bg="#0b0f14")
        self.disp.pack(side="top", fill="both", expand=True)
        self.lbl_info = tk.Label(self.disp, text="", bg="#0b0f14", fg="#7d8a99",
                                 font=("맑은 고딕", 10), anchor="w")
        self.lbl_info.pack(side="top", fill="x", padx=10, pady=(6, 0))
        self.lbl_value = tk.Label(self.disp, text="---", bg="#0b0f14", fg="#39ff88",
                                  font=("Consolas", 60, "bold"))
        self.lbl_value.pack(side="top", fill="both", expand=True)
        self.lbl_trigval = tk.Label(self.disp, text="", bg="#0b0f14", fg="#4fc3f7",
                                    font=("Consolas", 24, "bold"))
        self.lbl_trigval.pack(side="top", fill="x")
        self.lbl_stats = tk.Label(self.disp, text="", bg="#0b0f14", fg="#9aa7b4",
                                  font=("Consolas", 10), anchor="w")
        self.lbl_stats.pack(side="top", fill="x", padx=10)
        bar = tk.Frame(self.disp, bg="#0b0f14")
        bar.pack(side="bottom", fill="x", padx=6, pady=4)
        self.lbl_status = tk.Label(bar, text="연결 안 됨", bg="#0b0f14", fg="#c0504d",
                                   font=("맑은 고딕", 9), anchor="w")
        self.lbl_status.pack(side="left", fill="x", expand=True)
        self.btn_toggle_set = tk.Button(bar, text="설정 숨기기", command=self.toggle_settings,
                                        bg="#1c2530", fg="#c8d3de", relief="flat", padx=8)
        self.btn_toggle_set.pack(side="right")
        self.btn_run2 = tk.Button(bar, text="▶ 측정 시작", command=self.on_run,
                                  bg="#1f6f3f", fg="#ffffff", relief="flat", padx=10,
                                  activebackground="#2a8f52", activeforeground="#ffffff")
        self.btn_run2.pack(side="right", padx=6)
        dm = self.s.get("disp_mode", DISP_MODES[0])
        if dm not in DISP_MODES:
            dm = DISP_MODES[0]
        self.var_dmode = tk.StringVar(value=dm)
        om = tk.OptionMenu(bar, self.var_dmode, *DISP_MODES, command=lambda _v: self.on_dmode())
        om.configure(bg="#1c2530", fg="#c8d3de", activebackground="#2a3644", activeforeground="#ffffff",
                     relief="flat", highlightthickness=0, padx=6)
        om["menu"].configure(bg="#1c2530", fg="#c8d3de")
        om.pack(side="right", padx=6)
        tk.Label(bar, text="표시", bg="#0b0f14", fg="#7d8a99").pack(side="right")
        self.var_top = tk.BooleanVar(value=bool(self.s["topmost"]))
        tk.Checkbutton(bar, text="항상 위", variable=self.var_top, command=self.on_topmost,
                       bg="#0b0f14", fg="#c8d3de", selectcolor="#1c2530",
                       activebackground="#0b0f14", activeforeground="#ffffff").pack(side="right", padx=6)
        self.disp.bind("<Configure>", self.on_resize)
        self.lbl_value.bind("<Double-Button-1>", lambda e: self.toggle_settings())

    def on_resize(self, event=None):
        w = max(self.disp.winfo_width(), 100)
        h = max(self.disp.winfo_height() - 80, 40)
        show_t = (self.var_dmode.get() == DISP_MODES[0]) if hasattr(self, "var_dmode") else False
        if show_t:
            h = h * 0.72
        n = max(len(self.lbl_value.cget("text")), 8)
        size = int(min(h * 0.55, w / n * 1.45))
        size = max(14, min(size, 260))
        self.lbl_value.configure(font=("Consolas", size, "bold"))
        if hasattr(self, "lbl_trigval"):
            nt = max(len(self.lbl_trigval.cget("text")), 16)
            tsize = int(min(size * 0.38, w / nt * 1.5))
            self.lbl_trigval.configure(font=("Consolas", max(10, tsize), "bold"))

    # --------------------------------------------- 설정부
    def build_settings(self):
        s = self.s
        f = ttk.Frame(self.c, padding=8)
        self.setf = f
        pad = dict(padx=4, pady=3)

        # 연결
        g1 = ttk.LabelFrame(f, text=" 장비 연결 (LAN) ", padding=6)
        g1.pack(fill="x", pady=(0, 4))
        ttk.Label(g1, text="IP").grid(row=0, column=0, sticky="e", **pad)
        self.var_host = tk.StringVar(value=s["host"])
        ttk.Entry(g1, textvariable=self.var_host, width=18).grid(row=0, column=1, **pad)
        ttk.Label(g1, text="Port").grid(row=0, column=2, sticky="e", **pad)
        self.var_port = tk.StringVar(value=str(s["port"]))
        ttk.Entry(g1, textvariable=self.var_port, width=6).grid(row=0, column=3, **pad)
        self.btn_conn = ttk.Button(g1, text="연결", command=self.on_connect, width=8)
        self.btn_conn.grid(row=0, column=4, **pad)
        self.lbl_idn = ttk.Label(g1, text="(IP에 demo 입력 시 장비 없이 테스트)", foreground="#666")
        self.lbl_idn.grid(row=1, column=0, columnspan=6, sticky="w", **pad)

        # 측정 설정
        g2 = ttk.LabelFrame(f, text=" 전류 읽는 위치 / 방식 ", padding=6)
        g2.pack(fill="x", pady=4)
        ttk.Label(g2, text="채널").grid(row=0, column=0, sticky="e", **pad)
        self.var_ch = tk.StringVar(value="CH%d" % s["channel"])
        ttk.Combobox(g2, textvariable=self.var_ch, values=["CH1", "CH2", "CH3", "CH4"],
                     width=6, state="readonly").grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(g2, text="측정").grid(row=0, column=2, sticky="e", **pad)
        self.var_meas = tk.StringVar(value=s["meas_label"])
        ttk.Combobox(g2, textvariable=self.var_meas, values=[m[0] for m in MEAS_TYPES],
                     width=14, state="readonly").grid(row=0, column=3, sticky="w", **pad)
        ttk.Label(g2, text="장비 측정 슬롯").grid(row=0, column=4, sticky="e", **pad)
        self.var_slot = tk.StringVar(value=str(s["slot"]))
        ttk.Spinbox(g2, from_=1, to=8, textvariable=self.var_slot, width=4,
                    state="readonly").grid(row=0, column=5, sticky="w", **pad)

        self.var_gate = tk.BooleanVar(value=bool(s["gate_on"]))
        ttk.Checkbutton(g2, text="구간 지정 (Gate)", variable=self.var_gate,
                        command=self.update_gate_state).grid(row=1, column=0, columnspan=2, sticky="w", **pad)
        ttk.Label(g2, text="시작").grid(row=1, column=2, sticky="e", **pad)
        self.var_t1 = tk.StringVar(value=s["gate_start"])
        self.ent_t1 = ttk.Entry(g2, textvariable=self.var_t1, width=10)
        self.ent_t1.grid(row=1, column=3, sticky="w", **pad)
        ttk.Label(g2, text="끝").grid(row=1, column=4, sticky="e", **pad)
        self.var_t2 = tk.StringVar(value=s["gate_stop"])
        self.ent_t2 = ttk.Entry(g2, textvariable=self.var_t2, width=10)
        self.ent_t2.grid(row=1, column=5, sticky="w", **pad)
        self.var_tu = tk.StringVar(value=s["gate_unit"])
        self.cmb_tu = ttk.Combobox(g2, textvariable=self.var_tu, values=list(TIME_UNITS),
                                   width=4, state="readonly")
        self.cmb_tu.grid(row=1, column=6, sticky="w", **pad)
        ttk.Label(g2, text="※ 시간 기준 = 오실로스코프 화면의 트리거 지점(0 s). 음수 입력 가능.",
                  foreground="#666").grid(row=2, column=0, columnspan=7, sticky="w", **pad)
        self.var_cursor = tk.BooleanVar(value=bool(s.get("gate_cursor", True)))
        ttk.Checkbutton(g2, text="읽는 구간을 스코프 화면에 커서로 표시 (커서 2번, PC 계산 방식)",
                        variable=self.var_cursor).grid(row=6, column=0, columnspan=7, sticky="w", **pad)
        ttk.Label(g2, text="계산 방식").grid(row=3, column=0, sticky="e", **pad)
        self.var_calc = tk.StringVar(value=s.get("calc_mode", CALC_MODES[0]))
        ttk.Combobox(g2, textvariable=self.var_calc, values=CALC_MODES, width=36,
                     state="readonly").grid(row=3, column=1, columnspan=4, sticky="w", **pad)
        ttk.Label(g2, text="읽을 값").grid(row=3, column=5, sticky="e", **pad)
        self.var_stat = tk.StringVar(value=s.get("stat_label", STAT_TYPES[1][0]))
        self.cmb_stat = ttk.Combobox(g2, textvariable=self.var_stat, values=[x[0] for x in STAT_TYPES],
                                     width=14, state="readonly")
        self.cmb_stat.grid(row=3, column=6, sticky="w", **pad)
        ttk.Label(g2, text="※ '그대로 읽기' : 스코프에서 직접 설정한 Meas(슬롯 번호)의 결과 표 값을 읽음 "
                           "(채널·측정·구간 설정은 사용 안 함)",
                  foreground="#666").grid(row=5, column=0, columnspan=7, sticky="w", **pad)
        self.lbl_applied = ttk.Label(g2, text="", foreground="#1f6f3f")
        self.lbl_applied.grid(row=4, column=0, columnspan=7, sticky="w", **pad)

        # 트리거 자동 추적
        gt = ttk.LabelFrame(f, text=" 트리거 레벨 자동 추적 ", padding=6)
        gt.pack(fill="x", pady=4)
        self.var_trk = tk.BooleanVar(value=bool(s.get("trk_on", True)))
        ttk.Checkbutton(gt, text="사용", variable=self.var_trk).grid(row=0, column=0, sticky="w", **pad)
        ttk.Label(gt, text="시작 전류").grid(row=0, column=1, sticky="e", **pad)
        self.var_trk_start = tk.StringVar(value=s.get("trk_start", "200"))
        ttk.Entry(gt, textvariable=self.var_trk_start, width=7).grid(row=0, column=2, sticky="w", **pad)
        ttk.Label(gt, text="mA").grid(row=0, column=3, sticky="w")
        ttk.Label(gt, text="레벨 = 측정값 ×").grid(row=0, column=4, sticky="e", **pad)
        r0 = s.get("trk_ratio", "100")
        if str(r0).strip() in ("90", "90.0") and not s.get("trk_ratio100"):
            r0 = "100"                                    # 기본값 90% → 100% (레벨 = 측정값 평균) 로 한 번만 바꿈
        s["trk_ratio100"] = True
        self.var_trk_ratio = tk.StringVar(value=r0)
        ttk.Entry(gt, textvariable=self.var_trk_ratio, width=5).grid(row=0, column=5, sticky="w", **pad)
        ttk.Label(gt, text="%").grid(row=0, column=6, sticky="w")
        ttk.Label(gt, text="최저").grid(row=0, column=7, sticky="e", **pad)
        self.var_trk_floor = tk.StringVar(value=s.get("trk_floor", "100"))
        ttk.Entry(gt, textvariable=self.var_trk_floor, width=7).grid(row=0, column=8, sticky="w", **pad)
        ttk.Label(gt, text="mA").grid(row=0, column=9, sticky="w")
        self.var_trk_set = tk.BooleanVar(value=bool(s.get("trk_settrig", True)))
        ttk.Checkbutton(gt, text="트리거 소스를 측정 채널 · 상승 에지로 설정",
                        variable=self.var_trk_set).grid(row=1, column=0, columnspan=5, sticky="w", **pad)
        ttk.Label(gt, text="트리거 모드").grid(row=1, column=5, columnspan=2, sticky="e", **pad)
        self.var_trk_mode = tk.StringVar(value=norm_trig_mode(s.get("trk_mode", TRIG_MODES[1])))
        ttk.Combobox(gt, textvariable=self.var_trk_mode, values=TRIG_MODES, width=14,
                     state="readonly").grid(row=1, column=7, columnspan=3, sticky="w", **pad)
        self.var_asc = tk.BooleanVar(value=bool(s.get("asc_on", True)))
        ttk.Checkbutton(gt, text="세로 스케일 자동 (전류에 맞춰 스코프 A/div 조절, 클리핑 방지)",
                        variable=self.var_asc).grid(row=2, column=0, columnspan=7, sticky="w", **pad)
        self.lbl_scale = ttk.Label(gt, text="", foreground="#1f4f8f")
        self.lbl_scale.grid(row=2, column=7, columnspan=3, sticky="w", **pad)
        lw = ttk.Frame(gt)
        lw.grid(row=3, column=0, columnspan=10, sticky="w", **pad)
        ttk.Label(lw, text="SG ≤").pack(side="left")
        self.var_trk_lowp = tk.StringVar(value=s.get("trk_lowp", "-37"))
        ttk.Entry(lw, textvariable=self.var_trk_lowp, width=5, justify="right").pack(side="left", padx=3)
        ttk.Label(lw, text="dBm 로 내려오면 잘 잡혔던 낮은 파워 트리거 · 스케일로 되돌림 (없으면 Idq 기준) - Bench 연동").pack(side="left")
        self.lbl_trig = ttk.Label(gt, text="현재 트리거 레벨: ---", foreground="#1f4f8f")
        self.lbl_trig.grid(row=4, column=0, columnspan=10, sticky="w", **pad)

        # 환산 / 표시
        g3 = ttk.LabelFrame(f, text=" 환산 / 표시 ", padding=6)
        g3.pack(fill="x", pady=4)
        ttk.Label(g3, text="환산").grid(row=0, column=0, sticky="e", **pad)
        self.var_conv = tk.StringVar(value=s["conv_mode"])
        ttk.Combobox(g3, textvariable=self.var_conv, values=CONV_MODES, width=20,
                     state="readonly").grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(g3, text="값").grid(row=0, column=2, sticky="e", **pad)
        self.var_convv = tk.StringVar(value=s["conv_value"])
        ttk.Entry(g3, textvariable=self.var_convv, width=8).grid(row=0, column=3, sticky="w", **pad)
        ttk.Label(g3, text="갱신(ms)").grid(row=0, column=4, sticky="e", **pad)
        self.var_int = tk.StringVar(value=str(s["interval_ms"]))
        ttk.Spinbox(g3, from_=50, to=10000, increment=50, textvariable=self.var_int,
                    width=6, command=self.on_interval).grid(row=0, column=5, sticky="w", **pad)
        ttk.Label(g3, text="표시 단위").grid(row=1, column=0, sticky="e", **pad)
        self.var_du = tk.StringVar(value=s["disp_unit"])
        ttk.Combobox(g3, textvariable=self.var_du, values=DISP_UNITS, width=6,
                     state="readonly").grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(g3, text="소수 자리").grid(row=1, column=2, sticky="e", **pad)
        self.var_dig = tk.StringVar(value=str(s["digits"]))
        ttk.Spinbox(g3, from_=0, to=6, textvariable=self.var_dig, width=4,
                    state="readonly").grid(row=1, column=3, sticky="w", **pad)

        # 버튼
        g4 = ttk.Frame(f)
        g4.pack(fill="x", pady=(6, 0))
        self.btn_apply = ttk.Button(g4, text="장비에 적용", command=self.on_apply)
        self.btn_apply.pack(side="left", padx=3)
        self.btn_run = ttk.Button(g4, text="▶ 측정 시작", command=self.on_run)
        self.btn_run.pack(side="left", padx=3)
        ttk.Button(g4, text="통계 리셋", command=self.on_reset).pack(side="left", padx=3)
        self.btn_log = ttk.Button(g4, text="CSV 기록 시작", command=self.on_log)
        self.btn_log.pack(side="left", padx=3)
        ttk.Label(g4, text=VERSION, foreground="#999").pack(side="right")

        self.update_gate_state()
        self.update_info()

    def update_gate_state(self):
        st = "normal" if self.var_gate.get() else "disabled"
        self.ent_t1.configure(state=st)
        self.ent_t2.configure(state=st)
        self.cmb_tu.configure(state="readonly" if self.var_gate.get() else "disabled")

    def apply_visibility(self):
        if self.s.get("show_settings", True):
            self.setf.pack(side="bottom", fill="x", before=self.disp)
            self.btn_toggle_set.configure(text="설정 숨기기")
            self.fit_window()
        else:
            self.setf.pack_forget()
            self.btn_toggle_set.configure(text="설정 보기")

    def fit_window(self):
        """저장된 창 크기가 작아도 설정/버튼이 잘리지 않도록 높이를 늘림."""
        if self.embedded:
            return
        self.root.update_idletasks()
        need_h = self.setf.winfo_reqheight() + 220
        need_w = self.setf.winfo_reqwidth()
        try:
            size = self.root.geometry().split("+")[0]
            w, h = [int(x) for x in size.split("x")]
        except Exception:
            return
        if w > 1 and (h < need_h or w < need_w):
            self.root.geometry("%dx%d" % (max(w, need_w), max(h, need_h)))

    def set_run_text(self, txt):
        self.btn_run.configure(text=txt)
        self.btn_run2.configure(text=txt)

    def toggle_settings(self):
        self.s["show_settings"] = not self.s.get("show_settings", True)
        self.apply_visibility()

    def trig_only(self):
        return self.var_dmode.get() == DISP_MODES[2]

    def on_dmode(self):
        mode = self.var_dmode.get()
        self.s["disp_mode"] = mode
        self.lbl_trigval.configure(text="")
        if mode == DISP_MODES[2]:
            self.lbl_value.configure(text="---", fg="#4fc3f7")
            self.lbl_stats.configure(text="")
        else:
            self.lbl_value.configure(text="---", fg="#39ff88")
        self.update_info()
        self.worker.tasks.put(("show_trig", mode != DISP_MODES[1]))
        self.on_resize()

    def on_topmost(self):
        self.s["topmost"] = self.var_top.get()
        self.root.attributes("-topmost", bool(self.s["topmost"]))

    # --------------------------------------------- 설정 읽기
    def read_cfg(self):
        meas = dict(MEAS_TYPES)[self.var_meas.get()]
        ch = int(self.var_ch.get().replace("CH", ""))
        cfg = {"channel": ch, "meas": meas, "slot": int(self.var_slot.get()),
               "gate_on": self.var_gate.get(),
               "pc_calc": self.var_calc.get() == CALC_MODES[0],
               "cursor": self.var_cursor.get(),
               "scope_read": self.var_calc.get() == CALC_MODES[2],
               "trig_read": self.var_calc.get() == CALC_MODES[3],
               "stat": dict(STAT_TYPES).get(self.var_stat.get(), "AVG")}
        if cfg["scope_read"] or cfg["trig_read"]:
            cfg["gate_on"] = False
            return cfg
        if cfg["gate_on"]:
            k = TIME_UNITS[self.var_tu.get()]
            try:
                a = float(self.var_t1.get()) * k
                b = float(self.var_t2.get()) * k
            except ValueError:
                raise ValueError("구간 시작/끝에 숫자를 입력하세요.")
            if b <= a:
                raise ValueError("구간의 끝이 시작보다 커야 합니다.")
            cfg["t_start"], cfg["t_stop"] = a, b
        return cfg

    def conv_factor(self):
        mode = self.var_conv.get()
        try:
            v = float(self.var_convv.get())
        except ValueError:
            v = 1.0
        if mode == CONV_MODES[1]:
            return 1.0 / v if v != 0 else float("nan")
        if mode == CONV_MODES[2]:
            return v
        return 1.0

    def interval_s(self):
        try:
            return max(50, int(float(self.var_int.get()))) / 1000.0
        except ValueError:
            return 0.2

    def update_info(self):
        try:
            cfg = self.read_cfg()
            if self.trig_only():
                self.lbl_info.configure(text="T level (스코프 트리거 레벨)")
                return
            if cfg["trig_read"]:
                self.lbl_info.configure(text="스코프 트리거 A: Level")
                return
            if cfg["scope_read"]:
                self.lbl_info.configure(text="스코프 Meas%d · %s" % (cfg["slot"], self.var_stat.get()))
                return
            txt = "%s · %s" % (self.var_ch.get(), self.var_meas.get())
            txt += " · PC계산" if cfg["pc_calc"] else " · 장비측정"
            if cfg["gate_on"]:
                txt += " · 구간 %s ~ %s" % (fmt_time(cfg["t_start"]), fmt_time(cfg["t_stop"]))
            else:
                txt += " · 화면 전체"
        except Exception:
            txt = "%s · %s" % (self.var_ch.get(), self.var_meas.get())
        self.lbl_info.configure(text=txt)

    # --------------------------------------------- 버튼 동작
    def on_connect(self):
        if self.connected:
            self.running = False
            self.set_run_text("▶ 측정 시작")
            self.worker.tasks.put(("disconnect",))
            return
        try:
            port = int(self.var_port.get())
        except ValueError:
            port = 5025
        self.worker.tasks.put(("connect", self.var_host.get(), port))

    def on_apply(self):
        if not self.connected:
            messagebox.showinfo(APP_NAME, "먼저 장비에 연결하세요.")
            return
        try:
            cfg = self.read_cfg()
        except ValueError as e:
            messagebox.showwarning(APP_NAME, str(e))
            return
        self.update_info()
        self.reset_stats()
        self.worker.tasks.put(("apply", cfg))

    def on_run(self):
        if not self.connected:
            messagebox.showinfo(APP_NAME, "먼저 장비에 연결하세요.")
            return
        if not self.running:
            # 처음 시작할 때는 현재 설정을 장비에 먼저 적용
            try:
                cfg = self.read_cfg()
            except ValueError as e:
                messagebox.showwarning(APP_NAME, str(e))
                return
            trk = None
            if self.var_trk.get() and cfg.get("trig_read"):
                messagebox.showinfo(APP_NAME, "트리거 레벨을 읽는 모드에서는 '트리거 레벨 자동 추적'을 쓰지 않습니다.\n"
                                              "(프로그램이 레벨을 바꾸면 그 값을 다시 읽게 되므로) 추적 없이 시작합니다.")
            elif self.var_trk.get():
                try:
                    k = self.conv_factor()  # 장비값 × k = A
                    trk = {"channel": cfg["channel"],
                           "start": float(self.var_trk_start.get()) * 1e-3 / k,
                           "ratio": float(self.var_trk_ratio.get()) / 100.0,
                           "floor": float(self.var_trk_floor.get()) * 1e-3 / k,
                           "settrig": self.var_trk_set.get(),
                           "mode": {TRIG_MODES[0]: "AUTO", TRIG_MODES[1]: "NORM"}.get(
                               self.var_trk_mode.get(), "KEEP")}
                    if not (0 < trk["ratio"] <= 1.5) or trk["start"] <= 0:
                        raise ValueError
                except (ValueError, ZeroDivisionError):
                    messagebox.showwarning(APP_NAME, "트리거 추적의 시작 전류 / 비율 / 최저 값을 확인하세요.")
                    return
            self.update_info()
            self.worker.tasks.put(("apply", cfg))
            if trk:
                self.worker.tasks.put(("track_start", trk))
            else:
                self.worker.tasks.put(("track_stop",))
            if self.var_asc.get():
                self.worker.tasks.put(("scale_start", cfg["channel"]))
            else:
                self.worker.tasks.put(("scale_stop",))
            self.worker.tasks.put(("show_trig", self.var_dmode.get() != DISP_MODES[1]))
            self.stale = False
            self.running = True
            self.set_run_text("■ 정지")
        else:
            self.worker.tasks.put(("track_stop",))
            self.worker.tasks.put(("scale_stop",))
            self.running = False
            self.set_run_text("▶ 측정 시작")
        self.worker.tasks.put(("poll", self.running, self.interval_s()))

    def on_interval(self):
        if self.running:
            self.worker.tasks.put(("poll", True, self.interval_s()))

    def reset_stats(self):
        self.n = 0
        self.vsum = 0.0
        self.vmin = float("inf")
        self.vmax = float("-inf")

    def on_reset(self):
        self.reset_stats()
        self.lbl_stats.configure(text="")
        if self.connected and self.var_calc.get() == CALC_MODES[2]:
            try:
                self.worker.tasks.put(("stat_reset", int(self.var_slot.get())))
            except ValueError:
                pass

    def on_log(self):
        if self.log_file:
            self.log_file.close()
            self.log_file = None
            self.log_writer = None
            self.btn_log.configure(text="CSV 기록 시작")
            return
        name = "current_log_%s.csv" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = filedialog.asksaveasfilename(defaultextension=".csv", initialfile=name,
                                            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        self.log_file = open(path, "w", newline="", encoding="utf-8-sig")
        self.log_writer = csv.writer(self.log_file)
        self.log_writer.writerow(["time", "current_A", "channel", "measure", "gate"])
        self.btn_log.configure(text="CSV 기록 중지")

    # --------------------------------------------- 값 표시
    def format_value(self, amps):
        if not math.isfinite(amps):
            return "---"
        unit = self.var_du.get()
        try:
            dig = int(self.var_dig.get())
        except ValueError:
            dig = 2
        if unit == "자동":
            a = abs(amps)
            unit = "A" if a >= 1 else ("mA" if a >= 1e-3 else "µA")
        k = {"A": 1, "mA": 1e3, "µA": 1e6}[unit]
        return "%.*f %s" % (dig, amps * k, unit)

    def show_value(self, raw, t):
        amps = raw * self.conv_factor() if math.isfinite(raw) else float("nan")
        if self.trig_only():
            if self.log_writer:
                self.log_writer.writerow([
                    datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                    ("%.9g" % amps) if math.isfinite(amps) else "",
                    self.var_ch.get(), self.var_meas.get(), self.lbl_info.cget("text")])
            return
        txt = self.format_value(amps)
        old_len = len(self.lbl_value.cget("text"))
        color = "#39ff88" if math.isfinite(amps) else "#c0504d"
        if getattr(self, "stale", False) and math.isfinite(amps):
            color = "#7d8a99"  # 새 트리거 없음 (마지막 값 유지)
        self.lbl_value.configure(text=txt, fg=color)
        if len(txt) != old_len:
            self.on_resize()
        if math.isfinite(amps):
            self.n += 1
            self.vsum += amps
            self.vmin = min(self.vmin, amps)
            self.vmax = max(self.vmax, amps)
            self.lbl_stats.configure(text="평균 %s   최소 %s   최대 %s   (n=%d)" % (
                self.format_value(self.vsum / self.n), self.format_value(self.vmin),
                self.format_value(self.vmax), self.n))
        if self.log_writer:
            self.log_writer.writerow([
                datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                ("%.9g" % amps) if math.isfinite(amps) else "",
                self.var_ch.get(), self.var_meas.get(), self.lbl_info.cget("text")])

    def set_status(self, text, color):
        self.lbl_status.configure(text=text, fg=color)

    def pump(self):
        fresh_any = False
        last_value = None
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "value":
                    if self.running:
                        last_value = msg
                        fr = msg[3] if len(msg) > 3 else True
                        fresh_any = fresh_any or fr
                        if self.log_writer:
                            self.value_fresh = fr
                            self.show_value(msg[1], msg[2])
                            last_value = None
                elif kind == "status":
                    self.set_status(msg[1], msg[2])
                elif kind == "connected":
                    self.connected = True
                    self.btn_conn.configure(text="연결 해제")
                    self.lbl_idn.configure(text=msg[1], foreground="#1f4f8f")
                    self.set_status("연결됨  %s" % self.var_host.get(), "#39ff88")
                elif kind == "disconnected":
                    self.connected = False
                    self.running = False
                    self.btn_conn.configure(text="연결")
                    self.set_run_text("▶ 측정 시작")
                    if not self.lbl_status.cget("text").startswith("오류"):
                        self.set_status("연결 안 됨", "#c0504d")
                elif kind == "applied":
                    errs, actual = msg[1], msg[2]
                    if errs:
                        self.lbl_applied.configure(text="장비 오류: " + " / ".join(errs),
                                                   foreground="#c0504d")
                    elif actual:
                        self.lbl_applied.configure(
                            text="장비에 적용됨: 구간 %s ~ %s" % (fmt_time(actual[0]), fmt_time(actual[1])),
                            foreground="#1f6f3f")
                    else:
                        self.lbl_applied.configure(text="장비에 적용됨", foreground="#1f6f3f")
                elif kind == "applied_pc":
                    errs, info, part = msg[1], msg[2], msg[3]
                    if errs:
                        self.lbl_applied.configure(text="장비 오류: " + " / ".join(errs),
                                                   foreground="#c0504d")
                    elif info:
                        self.lbl_applied.configure(
                            text="PC 계산 준비됨 · 파형 %s ~ %s, %s pt%s" % (
                                fmt_time(info[0]), fmt_time(info[1]), format(info[2], ","),
                                " (구간 부분만 전송)" if part else ""),
                            foreground="#1f6f3f")
                elif kind == "trig":
                    k = self.conv_factor()
                    txt = "현재 트리거 레벨: %s   (%s)" % (self.format_value(msg[1] * k), msg[2])
                    if msg[3] is not None:
                        txt += "   · 장비 확인값 %s" % self.format_value(msg[3] * k)
                        if abs(msg[3] - msg[1]) > 0.05 * max(abs(msg[1]), 1e-9):
                            txt += "  ⚠ 설정값과 다름 (채널 범위/단위 확인)"
                    self.lbl_trig.configure(text=txt)
                elif kind == "trigval":
                    mode = self.var_dmode.get()
                    if self.running and mode != DISP_MODES[1]:
                        v = msg[1]
                        val = self.format_value(v * self.conv_factor()) if math.isfinite(v) else "---"
                        if mode == DISP_MODES[2]:
                            # T level만: 큰 숫자 자리에 트리거 레벨 표시
                            old = len(self.lbl_value.cget("text"))
                            self.lbl_value.configure(text=val, fg="#4fc3f7")
                            if self.lbl_trigval.cget("text"):
                                self.lbl_trigval.configure(text="")
                            if len(val) != old:
                                self.on_resize()
                        else:
                            txt = "T level: %s" % val
                            old = len(self.lbl_trigval.cget("text"))
                            self.lbl_trigval.configure(text=txt)
                            if len(txt) != old:
                                self.on_resize()
                elif kind == "scale":
                    k = self.conv_factor()
                    self.lbl_scale.configure(text="스케일 %s/div (%s)" % (
                        self.format_value(msg[1] * k).replace(" ", " "), msg[2]))
                elif kind == "stale":
                    if msg[1] != getattr(self, "stale", False):
                        self.stale = msg[1]
                        if self.running:
                            self.set_status(("파형 갱신 없음 - 스코프 RUN 상태 확인" if msg[1] == "auto"
                                             else "트리거 대기 중 (새 파형 없음)") if msg[1] else
                                            "측정 중  %s" % self.var_host.get(),
                                            "#d4a017" if msg[1] else "#39ff88")
                elif kind == "applied_read":
                    errs, info = msg[1], msg[2]
                    if errs:
                        self.lbl_applied.configure(text="장비 오류: " + " / ".join(errs) + "  ·  " + info,
                                                   foreground="#c0504d")
                    else:
                        self.lbl_applied.configure(text="읽는 대상: " + info,
                                                   foreground="#c0504d" if "⚠" in info else "#1f6f3f")
                elif kind == "diag":
                    if self.running:
                        warn = "밖" in msg[1] or "없" in msg[1]
                        self.lbl_applied.configure(text=msg[1],
                                                   foreground="#c0504d" if warn else "#1f6f3f")
                elif kind == "error":
                    self.set_status("오류: " + msg[1], "#ff6b6b")
        except queue.Empty:
            pass
        if last_value:
            self.value_fresh = fresh_any  # 새 파형 값인지 (PA Bench Monitor 가 평균·기록에 사용)
            self.show_value(last_value[1], last_value[2])
        self.root.after(50, self.pump)

    # --------------------------------------------- 종료
    def collect_settings(self):
        s = self.s
        s["host"] = self.var_host.get()
        try:
            s["port"] = int(self.var_port.get())
        except ValueError:
            pass
        s["channel"] = int(self.var_ch.get().replace("CH", ""))
        s["meas_label"] = self.var_meas.get()
        s["slot"] = int(self.var_slot.get())
        s["gate_on"] = self.var_gate.get()
        s["calc_mode"] = self.var_calc.get()
        s["gate_cursor"] = self.var_cursor.get()
        s["stat_label"] = self.var_stat.get()
        s["trk_on"] = self.var_trk.get()
        s["trk_start"] = self.var_trk_start.get()
        s["trk_ratio"] = self.var_trk_ratio.get()
        s["trk_floor"] = self.var_trk_floor.get()
        s["trk_settrig"] = self.var_trk_set.get()
        s["trk_mode"] = self.var_trk_mode.get()
        s["trk_lowp"] = self.var_trk_lowp.get()
        s["asc_on"] = self.var_asc.get()
        s["disp_mode"] = self.var_dmode.get()
        s["gate_start"] = self.var_t1.get()
        s["gate_stop"] = self.var_t2.get()
        s["gate_unit"] = self.var_tu.get()
        s["conv_mode"] = self.var_conv.get()
        s["conv_value"] = self.var_convv.get()
        s["interval_ms"] = int(self.interval_s() * 1000)
        s["disp_unit"] = self.var_du.get()
        s["digits"] = int(self.var_dig.get())
        if not self.embedded:
            s["geometry"] = self.root.geometry()

    def shutdown(self):
        try:
            self.collect_settings()
            save_settings(self.s)
        except Exception:
            pass
        if self.log_file:
            self.log_file.close()
        self.worker.alive = False
        self.worker._close()

    def on_close(self):
        self.shutdown()
        self.root.destroy()


def main():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # 고해상도 모니터에서 글자 선명하게
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
