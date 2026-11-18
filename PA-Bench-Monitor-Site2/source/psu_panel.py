# -*- coding: utf-8 -*-
"""
psu_panel  -  E36155A (Vdd) / N6705C (게이트 바이어스) 화면 + 제어 + 바이어스 자동 순서
(PA Bench Monitor v2.0 의 '파워서플라이' 화면, PSU GPIB Test 단독 실행 둘 다 이 파일 사용)
원래: PSU GPIB Test v1.0  -  전원 연결 확인용 임시 프로그램
  · Keysight E36155A  (드레인 Vdd, 48 V)
  · Keysight N6705C   (게이트 바이어스, CH1 DRV 노랑 · CH2 CA 초록 · CH3 PK 파랑 · CH4)

GPIB (USB-GPIB 어댑터) · LAN · USB 모두 VISA 로 연결 (pyvisa + Keysight IO Libraries / NI-VISA).
기본은 '읽기만' - 전원 설정 · 출력은 바꾸지 않음. (아래 'SCPI 직접 보내기' 는 사용자가 직접 보낼 때만)
장비 없이 화면 확인: 주소에 DEMO 입력.
"""
import collections
import math
import datetime
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

APP = "PSU GPIB Test"
VERSION = "v1.2.2"
CH_COLORS = {1: ("#f2c200", "DRV"), 2: ("#2e9e3f", "CA"), 3: ("#1f6fd1", "PK"), 4: ("#d32f2f", "CH4")}
CH_DESC = {1: "DRV (노랑)", 2: "CA (초록)", 3: "PK (파랑)", 4: "CH4"}


# ---------------------------------------------------------------- 장비 없이 시험용
class DemoInst:
    def __init__(self, kind):
        self.kind = kind
        self.out = {1: True, 2: True, 3: True, 4: False}
        self.e_out = True
        self.ev, self.ei = 48.0, 3.0
        self.vset = {1: 4.20, 2: 4.35, 3: 5.00, 4: 0.0}
        self.iset = {1: 0.02, 2: 0.02, 3: 0.02, 4: 0.08}
        self.extra, self.vofs = 0.0, {}      # 데모 이상 시험용 (Vdd 추가 전류 A, 게이트 측정 오차 V)

    def query(self, cmd):
        import random
        c = cmd.strip().upper()
        time.sleep(0.01)
        if c == "*IDN?":
            return ("Keysight Technologies,E36155A,MY59000123,1.0.4-1.0.2" if self.kind == "E36155A"
                    else "Keysight Technologies,N6705C,MY56001234,D.03.10.1730")
        if c.startswith("SYST:ERR"):
            return '+0,"No error"'
        if self.kind == "E36155A":
            if c.startswith("MEAS:VOLT"):
                return "%+.6E" % (self.ev + random.uniform(-0.003, 0.003) if self.e_out else 0.0)
            if c.startswith("MEAS:CURR"):
                if not self.e_out:
                    return "+0.000000E+00"
                # 데모: 게이트(장비 표시 +)가 3.15 V 아래로 내려가면 전류가 흐름 (CH1 · CH2), Vdd 가 있어야
                g = getattr(self, "peer", None)
                a = 0.0002 + (self.extra if self.ev > 0.05 else 0.0)
                if g is not None and self.ev > 1:
                    for ch, k in ((1, 5.0), (2, 9.0)):
                        if g.out[ch]:
                            a += k * max(0.0, 3.15 - g.vset[ch]) ** 1.5
                return "%+.6E" % min(self.ei, a * (1 + random.uniform(-0.01, 0.01)))
            if c.startswith("VOLT?"):
                return "%+.6E" % self.ev
            if c.startswith("CURR?"):
                return "%+.6E" % self.ei
            if c.startswith("OUTP?"):
                return "1" if self.e_out else "0"
        else:
            chs = [int(x) for x in c.split("(@")[1].rstrip(")").split(",")] if "(@" in c else [1]
            vset, iset = self.vset, self.iset
            if c.startswith("SYST:CHAN:MOD?"):
                return ",".join({1: "N6781A", 2: "N6781A", 3: "N6784A", 4: "N6731B"}[n] for n in chs)
            if c.startswith("SYST:CHAN:COUN?"):
                return "4"
            if c.startswith("MEAS:VOLT"):
                return ",".join("%+.6E" % ((vset[n] + self.vofs.get(n, 0.0) + random.uniform(-0.0005, 0.0005)) if self.out[n] else 0.0) for n in chs)
            if c.startswith("MEAS:CURR"):
                return ",".join("%+.6E" % ((1.2e-6 + random.uniform(-2e-7, 2e-7)) if self.out[n] else 0.0) for n in chs)
            if c.startswith("VOLT?"):
                return ",".join("%+.6E" % vset[n] for n in chs)
            if c.startswith("CURR?"):
                return ",".join("%+.6E" % iset[n] for n in chs)
            if c.startswith("OUTP?"):
                return ",".join("1" if self.out[n] else "0" for n in chs)
        return "0"

    def write(self, cmd):
        c = cmd.strip().upper()
        chs = [int(x) for x in c.split("(@")[1].rstrip(")").split(",")] if "(@" in c else None
        val = c.split()[1].split(",")[0] if " " in c else ""
        if self.kind == "E36155A":
            if c.startswith("OUTP"):
                self.e_out = val in ("ON", "1")
            elif c.startswith("VOLT "):
                self.ev = float(val)
            elif c.startswith("CURR "):
                self.ei = float(val)
        else:
            for n in chs or [1]:
                if c.startswith("OUTP"):
                    self.out[n] = val in ("ON", "1")
                elif c.startswith("VOLT "):
                    self.vset[n] = float(val)
                elif c.startswith("CURR "):
                    self.iset[n] = float(val)

    def close(self):
        pass


# ---------------------------------------------------------------- 다이얼 (마우스 휠로 값 조정)
class Dial(tk.Canvas):
    """손잡이 모양. 휠 위 = 올림, 휠 아래 = 내림 (Shift = 10배). on_turn(+1/-1, 배수) 호출."""

    def __init__(self, parent, bg, size=54, accent="#cfd3d7", on_turn=None):
        super().__init__(parent, width=size, height=size, bg=bg, highlightthickness=0, cursor="hand2")
        self.size, self.accent, self.on_turn, self.angle = size, accent, on_turn, -90.0
        self.enabled = True
        self.bind("<MouseWheel>", lambda e: self.turn(1 if e.delta > 0 else -1, e))
        self.bind("<Button-4>", lambda e: self.turn(1, e))
        self.bind("<Button-5>", lambda e: self.turn(-1, e))
        self.configure(takefocus=1)
        self.bind("<Enter>", lambda e: self.focus_set())              # 마우스를 올리면 방향키도 이 다이얼로
        self.bind("<Up>", lambda e: self.turn(1, e))
        self.bind("<Down>", lambda e: self.turn(-1, e))
        self.bind("<Right>", lambda e: self.turn(1, e))
        self.bind("<Left>", lambda e: self.turn(-1, e))
        self.bind("<FocusIn>", lambda e: self.configure(highlightthickness=1, highlightbackground="white"))
        self.bind("<FocusOut>", lambda e: self.configure(highlightthickness=0))
        self.draw()

    def turn(self, d, e=None):
        mult = 10 if (e is not None and (e.state & 0x0001)) else 1
        if self.on_turn and self.on_turn(d, mult) is not False:
            self.angle += 18 * d
            self.draw()
        return "break"

    def draw(self):
        import math
        self.delete("all")
        s = self.size
        c = s / 2
        r = s / 2 - 3
        self.create_oval(c - r - 1, c - r + 1, c + r + 1, c + r + 3, fill="#1e2226", outline="")   # 그림자
        self.create_oval(c - r, c - r, c + r, c + r, fill="#8e959c" if self.enabled else "#5a5f64",
                         outline="#2b2f33", width=2)
        r2 = r * 0.72
        self.create_oval(c - r2, c - r2, c + r2, c + r2, fill="#b9bfc5" if self.enabled else "#6c7176",
                         outline="#7b8288")
        for k in range(12):          # 홈
            a = math.radians(k * 30)
            self.create_line(c + r * 0.82 * math.cos(a), c + r * 0.82 * math.sin(a),
                             c + r * 0.98 * math.cos(a), c + r * 0.98 * math.sin(a), fill="#5d646b")
        a = math.radians(self.angle)
        self.create_line(c + r2 * 0.25 * math.cos(a), c + r2 * 0.25 * math.sin(a),
                         c + r2 * 0.95 * math.cos(a), c + r2 * 0.95 * math.sin(a),
                         fill=self.accent, width=3, capstyle="round")

    def set_enabled(self, on):
        self.enabled = on
        self.draw()


# ---------------------------------------------------------------- VISA
class Visa:
    def __init__(self):
        self.rm = None
        self.err = ""
        self.backend = ""

    def open_rm(self, lib=""):
        try:
            import pyvisa
        except ImportError:
            self.err = "pyvisa 가 없습니다.  명령 프롬프트에서:  python -m pip install pyvisa"
            return False
        try:
            self.rm = pyvisa.ResourceManager(lib) if lib else pyvisa.ResourceManager()
            self.backend = str(self.rm.visalib)
            return True
        except Exception as e:   # VISA 라이브러리(Keysight IO Libraries / NI-VISA) 없음
            self.err = ("VISA 라이브러리를 찾지 못했습니다: %s\n\n"
                        "Keysight IO Libraries Suite (또는 NI-VISA) 를 설치하세요.\n"
                        "GPIB 어댑터(82357B 등) 드라이버도 함께 설치됩니다." % e)
            self.rm = None
            return False

    def list(self):
        if self.rm is None:
            return []
        try:
            return list(self.rm.list_resources("?*::INSTR"))
        except Exception:
            try:
                return list(self.rm.list_resources())
            except Exception:
                return []

    def open(self, res, timeout_ms=3000):
        if res.strip().upper().startswith("DEMO"):
            d = DemoInst("N6705C" if "N67" in res.upper() else "E36155A")
            self.demo = getattr(self, "demo", {})
            self.demo[d.kind] = d
            if "E36155A" in self.demo and "N6705C" in self.demo:
                self.demo["E36155A"].peer = self.demo["N6705C"]
            return d
        if self.rm is None:
            raise RuntimeError(self.err or "VISA 없음")
        inst = self.rm.open_resource(res.strip())
        inst.timeout = timeout_ms
        inst.read_termination = "\n"
        inst.write_termination = "\n"
        return inst


def fix_addr(a):
    """흔한 오타 고침: GPIBO(영문 O) → GPIB0, 공백 제거, ::INSTR 빠짐"""
    import re
    a = (a or "").strip().replace(" ", "")
    a = re.sub(r"(?i)^gpib[oO]", "GPIB0", a)
    a = re.sub(r"(?i)^gpib(\d*)::", lambda m: "GPIB%s::" % (m.group(1) or "0"), a)
    if re.match(r"(?i)^gpib\d+::\d+$", a):
        a += "::INSTR"
    return a


def fnum(s):
    try:
        return float(str(s).strip().split(",")[0])
    except ValueError:
        return float("nan")


def flist(s):
    out = []
    for x in str(s).strip().split(","):
        try:
            out.append(float(x))
        except ValueError:
            out.append(float("nan"))
    return out


# ---------------------------------------------------------------- 통신 스레드
class SeqStop(Exception):
    """바이어스 순서 중지"""


class Worker(threading.Thread):
    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.tasks = queue.Queue()
        self.alive = True

    def run(self):
        nxt, last_full = 0.0, 0.0
        while self.alive:
            try:
                fn = self.tasks.get(timeout=0.002)
            except queue.Empty:
                fn = None
            try:
                if fn is not None:
                    fn()
                    continue
                a = self.app
                now = time.time()
                if a.polling and now >= nxt:
                    full = now - last_full > 1.0 or a.force_full     # 설정값 · 출력 상태는 1초마다
                    if full:
                        last_full, a.force_full = now, False
                    a.read_job(full=full)
                    nxt = now + a.poll_dt
            except Exception as e:
                self.app.post("log", "오류: %s" % e, "err")


class App:
    def __init__(self, root, container=None, settings=None, on_vdd=None, mb=None, rf_guard=None, on_bias_done=None,
                 on_fault=None, left=None, right=None, extra=None, on_preset=None, trend=None):
        """container 를 주면 그 안에 그림 (PA Bench Monitor 안), 없으면 단독 창.
        on_vdd(A, V, t): Vdd 측정값이 들어올 때마다, rf_guard(): 바이어스 켜고 끄기 전 RF 확인 (False = 중단),
        on_bias_done(msg): 바이어스 자동 순서 완료"""
        global messagebox
        self.root = root
        self.embedded = container is not None
        if container is None:     # 단독 창: 세로 스크롤
            outer = ttk.Frame(root)
            outer.pack(fill="both", expand=True)
            cv = tk.Canvas(outer, highlightthickness=0)
            sb = ttk.Scrollbar(outer, orient="vertical", command=cv.yview)
            cv.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y")
            cv.pack(side="left", fill="both", expand=True)
            inner = ttk.Frame(cv)
            win = cv.create_window(0, 0, window=inner, anchor="nw")
            inner.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
            cv.bind("<Configure>", lambda e: cv.itemconfigure(win, width=e.width))
            container = inner
        self.box = container
        # 넓은 화면 (PA Bench Monitor): 왼쪽 = ON/OFF · 설정, 오른쪽 = 장비 화면, extra = VISA · SCPI · 기록 (장비 탭)
        self.box_left = left or container
        self.box_right = right or container
        self.box_extra = extra or container
        self.box_trend = trend           # (PA Bench Monitor) Vdd 전류 추이를 아래 칸에 따로
        self.wide = right is not None
        self.trend = collections.deque(maxlen=1200)    # (t, A) Vdd 전류 추이
        self.s = settings if settings is not None else {}
        self.on_vdd, self.rf_guard, self.on_bias_done, self.on_fault = on_vdd, rf_guard, on_bias_done, on_fault
        self.bias_state = "off"
        self.on_preset = on_preset         # (PA Bench Monitor) 지금 값을 샘플 종류 기본값으로 저장
        if mb is not None:
            messagebox = mb
        if container is not None:
            global APP
            APP = "파워서플라이 (Vdd / 게이트)"
        if not self.embedded:
            root.title("%s %s - E36155A / N6705C 연결 확인 · 제어" % (APP, VERSION))
            root.geometry("1020x1000")
            root.option_add("*Font", ("맑은 고딕", 9))
        self.seq_running = False
        self.auto_measure = False        # ON + 측정 으로 눌렀는지 (끝나면 자동 측정)
        self.on_seq_start = None         # (Bench) 순서를 시작하면 부름 (ON + 측정: 엑셀 저장 위치를 바로 물어봄)
        self._busy = None                # 작은 진행 창
        self._busy_job = None
        self._busy_kind = None
        self.seq_abort = False
        self.q = queue.Queue()
        self.visa = Visa()
        self.inst = {"e": None, "n": None}
        self.worker = Worker(self)
        self.worker.start()
        self.polling = False
        self.poll_dt = 1.0
        self.force_full = False
        self.read_chs = [1, 2, 3]
        self.rate_n, self.rate_t = 0, time.time()
        self.last_vm = {}
        self.e_last = (float("nan"), float("nan"), 0.0)
        self.sets = {}            # (k, ch) → {"v": 설정 V, "i": 전류 제한, "o": 출력}
        self.pending = {}         # (k, ch) → {"v"/"i": 보낼 값} (휠 돌리는 중)
        self.edit_t = {}          # (k, ch) → 마지막으로 돌린 시각 (그동안 읽은 값으로 덮지 않음)
        self.build()
        self.on_ctrl_toggle()
        root.after(100, self.pump)
        root.after(300, self.init_visa)
        if not self.embedded:
            root.protocol("WM_DELETE_WINDOW", self.on_close)

    def post(self, *msg):
        self.q.put(msg)

    # ------------------------------------------------ 화면
    def build(self):
        pad = dict(padx=4, pady=3)
        self.build_power_menu()
        top = ttk.LabelFrame(self.box_extra, text=" 장비 찾기 (VISA) ", padding=6)
        top.pack(fill="x", padx=8, pady=(8, 4))
        self.lbl_visa = ttk.Label(top, text="VISA 확인 중...", foreground="#666")
        self.lbl_visa.grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Button(top, text="장비 찾기 (주소 목록 + *IDN?)", command=self.scan).grid(row=1, column=0, sticky="w", **pad)
        ttk.Label(top, text="예: GPIB0::5::INSTR · TCPIP0::192.168.0.13::inst0::INSTR · 장비 없이: DEMO",
                  foreground="#666").grid(row=1, column=1, sticky="w", **pad)
        top.columnconfigure(1, weight=1)
        self.found = ttk.Treeview(top, columns=("r", "i"), show="headings", height=2 if self.embedded else 3)
        self.found.heading("r", text="VISA 주소")
        self.found.heading("i", text="*IDN? (더블클릭 = 아래 칸에 넣기)")
        self.found.column("r", width=170 if self.embedded else 200, stretch=False)
        self.found.column("i", width=300 if self.embedded else 560)
        self.found.grid(row=2, column=0, columnspan=2, sticky="we", pady=(4, 0))
        self.found.bind("<Double-1>", self.pick_found)

        mid = ttk.Frame(self.box_right)
        mid.pack(fill="x", padx=8, pady=4)
        BEZEL, LCD, DIM = "#41464d", "#0a0f14", "#5a6470"

        # ---- E36155A: 실제 앞면처럼 (회색 베젤 + 검은 화면, 큰 V / A 숫자)
        ew = ttk.Frame(mid)
        ew.pack(side="top", fill="x", pady=(0, 6))
        cf = ttk.Frame(ew)
        cf.pack(fill="x")
        ttk.Label(cf, text="E36155A 주소", font=("맑은 고딕", 9, "bold")).pack(side="left")
        self.var_e = tk.StringVar(value=self.s.get("psu_e_addr", "GPIB0::6::INSTR"))
        ttk.Entry(cf, textvariable=self.var_e, width=24).pack(side="left", padx=4)
        self.btn_e = ttk.Button(cf, text="연결", command=lambda: self.connect("e"))
        self.btn_e.pack(side="left")
        ep = tk.Frame(ew, bg=BEZEL, bd=3, relief="raised")
        ep.pack(fill="both", expand=True, pady=(4, 0))
        hd = tk.Frame(ep, bg=BEZEL)
        hd.pack(fill="x", padx=8, pady=(6, 2))
        tk.Label(hd, text="E36155A", bg=BEZEL, fg="#e8e8e8", font=("Arial", 11, "bold")).pack(side="left")
        tk.Label(hd, text="  DC Power Supply", bg=BEZEL, fg="#c9cdd2", font=("Arial", 8)).pack(side="left")
        tk.Label(hd, text="드레인 Vdd", bg="#ffb300", fg="black", font=("맑은 고딕", 8, "bold")).pack(side="right")
        body = tk.Frame(ep, bg=BEZEL)
        body.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        scr = tk.Frame(body, bg=LCD, bd=2, relief="sunken")
        scr.pack(side="left", fill="both", expand=True)
        self.e_vals = {}
        self.lbl_e_idn = tk.Label(scr, text="연결 안 됨", bg=LCD, fg=DIM, font=("Consolas", 8), anchor="w")
        self.lbl_e_idn.pack(fill="x", padx=6, pady=(4, 0))
        for k, col, big in (("vm", "#ffd54a", 30), ("im", "#4fc3f7", 30)):
            lb = tk.Label(scr, text="---", bg=LCD, fg=col, font=("Consolas", big, "bold"), anchor="e")
            lb.pack(fill="x", padx=10)
            self.e_vals[k] = lb
        st = tk.Frame(scr, bg=LCD)
        st.pack(fill="x", padx=6, pady=(2, 6))
        for k, t in (("vs", "Set"), ("is", "Limit")):
            tk.Label(st, text=t, bg=LCD, fg=DIM, font=("Consolas", 9)).pack(side="left", padx=(4, 2))
            lb = tk.Label(st, text="---", bg=LCD, fg="#e0e0e0", font=("Consolas", 10))
            lb.pack(side="left", padx=(0, 8))
            self.e_vals[k] = lb
        self.e_vals["o"] = tk.Label(st, text=" OFF ", bg="#333", fg="#999", font=("Consolas", 10, "bold"))
        self.e_vals["o"].pack(side="right", padx=4)
        kp = tk.Frame(body, bg=BEZEL)           # 오른쪽: 출력 On/Off 버튼 + 다이얼 (휠로 조정)
        kp.pack(side="left", fill="y", padx=(10, 0))
        self.e_led = tk.Button(kp, text=" On/Off ", bg="#555", fg="white", font=("Arial", 9, "bold"), bd=2,
                               relief="raised", activebackground="#777", command=lambda: self.toggle_out("e", None))
        self.e_led.pack(pady=(2, 4), fill="x")
        self.var_e_val = tk.StringVar(value="")
        ee = tk.Entry(kp, textvariable=self.var_e_val, width=9, justify="center", font=("Consolas", 12, "bold"),
                      bg="#10161c", fg="#ffd54a", insertbackground="white", relief="sunken")
        ee.pack(pady=(0, 4))
        ee.bind("<Return>", lambda e: self.entry_set("e", None))
        ee.bind("<Up>", lambda e: self.key_turn("e", None, 1, e))
        ee.bind("<Down>", lambda e: self.key_turn("e", None, -1, e))
        self.e_entry = ee
        self.var_e_dmode = tk.StringVar(value="V")
        self.e_dial = Dial(kp, BEZEL, size=60, accent="#ffd54a",
                           on_turn=lambda d, m: self.dial_turn("e", None, d, m))
        self.e_dial.pack()
        mf = tk.Frame(kp, bg=BEZEL)
        mf.pack()
        for t in ("V", "I"):
            tk.Radiobutton(mf, text={"V": "전압", "I": "전류"}[t], value=t, variable=self.var_e_dmode, bg=BEZEL,
                           fg="#e0e0e0", selectcolor="#2b2f33", activebackground=BEZEL, font=("맑은 고딕", 8),
                           command=lambda: self.dial_mode("e", None)).pack(side="left")
        self.lbl_e_step = tk.Label(kp, text="휠 ±0.1 V", bg=BEZEL, fg="#c9cdd2", font=("맑은 고딕", 8))
        self.lbl_e_step.pack()

        # ---- N6705C: 4채널 화면 (채널 색: 1 노랑 · 2 초록 · 3 파랑 · 4 빨강)
        nw = ttk.Frame(mid)
        nw.pack(side="top", fill="x")
        cf = ttk.Frame(nw)
        cf.pack(fill="x")
        ttk.Label(cf, text="N6705C 주소", font=("맑은 고딕", 9, "bold")).pack(side="left")
        self.var_n = tk.StringVar(value=self.s.get("psu_n_addr", "GPIB0::5::INSTR"))
        ttk.Entry(cf, textvariable=self.var_n, width=24).pack(side="left", padx=4)
        self.btn_n = ttk.Button(cf, text="연결", command=lambda: self.connect("n"))
        self.btn_n.pack(side="left")
        ttk.Label(cf, text="  읽을 채널:").pack(side="left")
        self.ch_on = {}
        for ch in range(1, 5):
            v = tk.BooleanVar(value=ch <= 3)
            self.ch_on[ch] = v
            ttk.Checkbutton(cf, text=str(ch), variable=v, command=self.dim_channels).pack(side="left")
        gb = ttk.Frame(nw)
        gb.pack(fill="x", pady=(3, 0))
        tk.Button(gb, text="▶ 게이트 전체 ON", bg="#2e7d32", fg="white", activebackground="#43a047",
                  font=("맑은 고딕", 9, "bold"), command=lambda: self.gate_all(True)).pack(side="left", padx=(0, 4))
        tk.Button(gb, text="■ 게이트 전체 OFF", bg="#5d4037", fg="white", activebackground="#795548",
                  font=("맑은 고딕", 9, "bold"), command=lambda: self.gate_all(False)).pack(side="left")
        ttk.Label(gb, text="  (읽을 채널로 체크한 채널만 한 번에)", foreground="#666").pack(side="left")

        npnl = tk.Frame(nw, bg=BEZEL, bd=3, relief="raised")
        npnl.pack(fill="both", expand=True, pady=(4, 0))
        hd = tk.Frame(npnl, bg=BEZEL)
        hd.pack(fill="x", padx=8, pady=(6, 2))
        tk.Label(hd, text="N6705C", bg=BEZEL, fg="#e8e8e8", font=("Arial", 11, "bold")).pack(side="left")
        tk.Label(hd, text="  DC Power Analyzer", bg=BEZEL, fg="#c9cdd2", font=("Arial", 8)).pack(side="left")
        tk.Label(hd, text="게이트 바이어스", bg="#7e57c2", fg="white", font=("맑은 고딕", 8, "bold")).pack(side="right")
        scr = tk.Frame(npnl, bg=LCD, bd=2, relief="sunken")
        scr.pack(fill="both", expand=True, padx=8)
        self.lbl_n_idn = tk.Label(scr, text="연결 안 됨", bg=LCD, fg=DIM, font=("Consolas", 8), anchor="w")
        self.lbl_n_idn.grid(row=0, column=0, columnspan=4, sticky="we", padx=6, pady=(4, 2))
        self.n_vals = {}
        self.n_cells = {}
        for ch in range(1, 5):
            col, name = CH_COLORS[ch]
            scr.columnconfigure(ch - 1, weight=1, uniform="ch")
            cell = tk.Frame(scr, bg=LCD, highlightthickness=2, highlightbackground=col)
            cell.grid(row=1, column=ch - 1, sticky="nsew", padx=3, pady=(0, 6))
            top = tk.Frame(cell, bg=col)
            top.pack(fill="x")
            tk.Label(top, text=" %d %s " % (ch, name), bg=col, fg="black" if ch == 1 else "white",
                     font=("Arial", 11, "bold")).pack(side="left")
            row = {"mod": tk.Label(top, text="---", bg=col, fg="black" if ch == 1 else "white", font=("Arial", 8))}
            row["mod"].pack(side="left")
            row["o"] = tk.Label(top, text="Off", bg=col, fg="black" if ch == 1 else "white", font=("Arial", 8, "bold"))
            row["o"].pack(side="right", padx=3)
            row["vm"] = tk.Label(cell, text="---", bg=LCD, fg=col, font=("Consolas", 15, "bold"), anchor="e")
            row["vm"].pack(fill="x", padx=4, pady=(4, 0))
            row["im"] = tk.Label(cell, text="---", bg=LCD, fg="#e0e0e0", font=("Consolas", 12, "bold"), anchor="e")
            row["im"].pack(fill="x", padx=4)
            row["vs"] = tk.Label(cell, text="Set ---", bg=LCD, fg=DIM, font=("Consolas", 8), anchor="e")
            row["vs"].pack(fill="x", padx=4)
            row["is"] = tk.Label(cell, text="Lim ---", bg=LCD, fg=DIM, font=("Consolas", 8), anchor="e")
            row["is"].pack(fill="x", padx=4)

            self.n_vals[ch] = row
            self.n_cells[ch] = cell
        keys = tk.Frame(npnl, bg=BEZEL)       # 채널 On/Off 버튼 + 다이얼 (휠로 조정)
        keys.pack(fill="x", padx=8, pady=(4, 8))
        self.n_leds, self.n_dials, self.var_n_dmode, self.lbl_n_step = {}, {}, {}, {}
        for ch in range(1, 5):
            col, _ = CH_COLORS[ch]
            keys.columnconfigure(ch - 1, weight=1)
            led = tk.Button(keys, text="%d  On/Off" % ch, bg="#555", fg="white", font=("Arial", 8, "bold"),
                            bd=2, relief="raised", highlightthickness=2, highlightbackground=col,
                            activebackground="#777", command=lambda c=ch: self.toggle_out("n", c))
            led.grid(row=0, column=ch - 1, padx=6, sticky="we")
            self.n_leds[ch] = led
            vv = tk.StringVar(value="")
            ne = tk.Entry(keys, textvariable=vv, width=9, justify="center", font=("Consolas", 11, "bold"),
                          bg="#10161c", fg=col, insertbackground="white", relief="sunken")
            ne.grid(row=1, column=ch - 1, pady=(4, 0))
            ne.bind("<Return>", lambda e, c=ch: self.entry_set("n", c))
            ne.bind("<Up>", lambda e, c=ch: self.key_turn("n", c, 1, e))
            ne.bind("<Down>", lambda e, c=ch: self.key_turn("n", c, -1, e))
            self.n_entry_var = getattr(self, "n_entry_var", {})
            self.n_entry = getattr(self, "n_entry", {})
            self.n_entry_var[ch], self.n_entry[ch] = vv, ne
            dl = Dial(keys, BEZEL, size=46, accent=col, on_turn=lambda d, m, c=ch: self.dial_turn("n", c, d, m))
            dl.grid(row=2, column=ch - 1, pady=(4, 0))
            self.n_dials[ch] = dl
            v = tk.StringVar(value="V")
            self.var_n_dmode[ch] = v
            mf = tk.Frame(keys, bg=BEZEL)
            mf.grid(row=3, column=ch - 1)
            for t in ("V", "I"):
                tk.Radiobutton(mf, text={"V": "전압", "I": "전류"}[t], value=t, variable=v, bg=BEZEL, fg="#e0e0e0",
                               selectcolor="#2b2f33", activebackground=BEZEL, font=("맑은 고딕", 8),
                               command=lambda c=ch: self.dial_mode("n", c)).pack(side="left")
            lb = tk.Label(keys, text="휠 ±0.001 V", bg=BEZEL, fg="#c9cdd2", font=("맑은 고딕", 8))
            lb.grid(row=4, column=ch - 1)
            self.lbl_n_step[ch] = lb
        self.dim_channels()

        # 읽기 / 직접 보내기
        cf = ttk.LabelFrame(self.box_left, text=" 제어 (출력 On/Off · 다이얼) - 잠금을 풀어야 동작 ", padding=6)
        cf.pack(fill="x", padx=8, pady=(4, 0))
        self.var_ctrl = tk.BooleanVar(value=False)
        self.chk_ctrl = tk.Checkbutton(cf, text="🔓 제어 사용 (잠금 해제)", variable=self.var_ctrl, command=self.on_ctrl_toggle,
                                       font=("맑은 고딕", 10, "bold"), fg="#c62828")
        self.chk_ctrl.grid(row=0, column=0, columnspan=9, sticky="w")
        self.var_seq = tk.BooleanVar(value=True)
        ttk.Checkbutton(cf, text="순서 경고 (게이트 먼저 ON · Vdd 먼저 OFF)", variable=self.var_seq).grid(
            row=1, column=0, columnspan=9, sticky="w", pady=(2, 0))
        # 안전 한계: 좁은 창에서도 다 보이게 두 줄로
        #   Vdd     최대 [50] V      전류제한 [2] A
        #   게이트  [2] ~ [6] V      전류제한 [0.08] A
        ttk.Label(cf, text="안전 한계", foreground="#666").grid(row=2, column=0, columnspan=9, sticky="w", pady=(6, 0))
        self.lim = {}

        def lim_entry(key, val, r, c):
            sv = self.s.get("psu_" + key, val)
            if key == "g_min" and str(sv).strip() in ("3", "3.0") and not self.s.get("psu_gmin2"):
                sv = "2"                              # 기본값 3 → 2 V 로 변경 (한 번만)
                self.s["psu_gmin2"] = True
            v = tk.StringVar(value=str(sv))
            ttk.Entry(cf, textvariable=v, width=5, justify="right").grid(row=r, column=c, sticky="w", pady=(3, 0))
            self.lim[key] = v

        def lab(t, r, c, **kw):
            ttk.Label(cf, text=t, **kw).grid(row=r, column=c, sticky="w", padx=(0, 3), pady=(3, 0))
        lab("Vdd", 3, 0, width=6)
        lab("최대", 3, 1)
        lim_entry("vdd_max", "50", 3, 2)
        lab("V", 3, 3, foreground="#666")
        lab("   전류제한", 3, 5)
        lim_entry("vdd_imax", "2", 3, 6)
        lab("A", 3, 7, foreground="#666")
        lab("게이트", 4, 0, width=6)
        gf = ttk.Frame(cf)
        gf.grid(row=4, column=1, sticky="w", pady=(3, 0))
        sv = self.s.get("psu_g_min", "2")
        if str(sv).strip() in ("3", "3.0") and not self.s.get("psu_gmin2"):
            sv = "2"                                  # 기본값 3 → 2 V 로 변경 (한 번만)
            self.s["psu_gmin2"] = True
        self.lim["g_min"] = tk.StringVar(value=str(sv))
        ttk.Entry(gf, textvariable=self.lim["g_min"], width=5, justify="right").pack(side="left")
        ttk.Label(gf, text=" ~").pack(side="left", padx=(0, 3))
        lim_entry("g_max", "6", 4, 2)
        lab("V", 4, 3, foreground="#666")
        lab("   전류제한", 4, 5)
        lim_entry("g_imax", "0.08", 4, 6)
        lab("A", 4, 7, foreground="#666")
        if self.wide:
            self.build_trend(self.box_trend or self.box_right)
        ttk.Label(self.box_right, text="   다이얼: 마우스를 올리고 휠 위 = 올림, 아래 = 내림 (Shift + 휠 = 10배).  "
                                  "게이트 0.001 V · 0.001 A,  Vdd 0.1 V · 0.1 A 단위.  [전압]/[전류] 로 조정할 값 선택.",
                  foreground="#666").pack(anchor="w", padx=8)
        bf = ttk.LabelFrame(self.box_extra, text=" 읽기 ", padding=6)
        bf.pack(fill="x", padx=8, pady=4)
        ttk.Button(bf, text="한 번 읽기", command=self.read_once).pack(side="left", padx=4)
        self.btn_poll = ttk.Button(bf, text="▶ 계속 읽기", command=self.toggle_poll)
        self.btn_poll.pack(side="left", padx=4)
        ttk.Label(bf, text="간격(초, 최소 0.01)").pack(side="left", padx=(10, 2))
        self.var_dt = tk.StringVar(value=str(self.s.get("psu_dt", "0.2" if self.embedded else "0.1")))
        ttk.Entry(bf, textvariable=self.var_dt, width=5).pack(side="left")
        self.var_dt.trace_add("write", self.update_dt)
        self.lbl_rate = ttk.Label(bf, text="", foreground="#1f4f8f")
        self.lbl_rate.pack(side="left", padx=10)
        ttk.Label(bf, text="(측정값은 매번, 설정값 · 출력 상태는 1초마다 읽음)", foreground="#666").pack(side="left")
        ttk.Label(bf, text="   타임아웃(ms)").pack(side="left", padx=(10, 2))
        self.var_to = tk.StringVar(value="3000")
        ttk.Entry(bf, textvariable=self.var_to, width=6).pack(side="left")

        sf = ttk.LabelFrame(self.box_extra, text=" SCPI 직접 보내기 (주의: 쓰기 명령은 전원 설정을 바꿈) ", padding=6)
        sf.pack(fill="x", padx=8, pady=4)
        self.var_tgt = tk.StringVar(value="N6705C")
        ttk.Combobox(sf, textvariable=self.var_tgt, values=["E36155A", "N6705C"], state="readonly", width=9).pack(side="left")
        self.var_cmd = tk.StringVar(value="MEAS:VOLT? (@1,2,3)")
        e = ttk.Entry(sf, textvariable=self.var_cmd, width=50)
        e.pack(side="left", padx=4)
        e.bind("<Return>", lambda ev: self.send_cmd())
        ttk.Button(sf, text="보내기", command=self.send_cmd).pack(side="left", padx=4)
        ttk.Label(sf, text="'?' 로 끝나면 응답을 읽음", foreground="#666").pack(side="left", padx=6)

        lf = ttk.LabelFrame(self.box_extra, text=" 기록 ", padding=4)
        lf.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        self.log = tk.Text(lf, height=6 if self.embedded else 10, font=("Consolas", 9))
        self.log.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(lf, command=self.log.yview)
        sb.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=sb.set)
        self.log.tag_configure("err", foreground="#c62828")
        self.log.tag_configure("ok", foreground="#2e7d32")

    # ------------------------------------------------ POWER SUPPLY ON / OFF (맨 위 메뉴)
    def build_power_menu(self):
        s = self.s
        BG = "#263238"
        top = tk.Frame(self.box_left, bg=BG, bd=2, relief="ridge")
        top.pack(fill="x", padx=8, pady=(8, 4))
        row = tk.Frame(top, bg=BG)
        row.pack(fill="x", padx=8, pady=(6, 2))
        tk.Label(row, text="POWER SUPPLY", bg=BG, fg="white", font=("Arial", 14, "bold")).pack(side="left")
        self.lamp = tk.Label(row, text="  OFF  ", bg="#616161", fg="white", font=("맑은 고딕", 10, "bold"), padx=6)
        self.lamp.pack(side="left", padx=10)
        tk.Button(row, text="■ 긴급 정지", bg="#c62828", fg="white", activebackground="#e53935",
                  font=("맑은 고딕", 10, "bold"), bd=3, relief="raised", command=self.seq_emergency).pack(side="right")
        row2 = tk.Frame(top, bg=BG)
        row2.pack(fill="x", padx=8, pady=4)
        btns = [("ON", "#2e7d32", "#43a047", ("Arial", 13, "bold"), self.power_on)]
        if self.on_bias_done:            # PA Bench Monitor 안: 바이어스 → RF → 자동 측정까지 한 번에
            btns.append(("ON + 측정", "#1565c0", "#1e88e5", ("맑은 고딕", 11, "bold"), lambda: self.power_on(auto=True)))
        btns.append(("OFF", "#455a64", "#607d8b", ("Arial", 13, "bold"), self.power_off))
        for k, (txt, bg_, act, fnt, cmd) in enumerate(btns):     # 같은 폭으로 나눔 (칸이 좁아도 세 버튼 모두 보이게)
            row2.columnconfigure(k, weight=1, uniform="pw")
            tk.Button(row2, text=txt, bg=bg_, fg="white", activebackground=act, font=fnt, bd=3, relief="raised",
                      width=1, command=cmd).grid(row=0, column=k, sticky="we", padx=(0 if k == 0 else 3, 0))
        self.lbl_seq = tk.Label(top, text="대기 - ON: 게이트 ON → Vdd 0 → 48 V (쇼트 확인) → CA → DRV 게이트로 목표 Id",
                                bg=BG, fg="#cfd8dc", font=("맑은 고딕", 10, "bold"), anchor="w", justify="left", wraplength=420)
        self.lbl_seq.pack(fill="x", padx=10, pady=(0, 4))
        self.lbl_seq.bind("<Configure>", lambda e: self.lbl_seq.configure(wraplength=max(200, e.width - 10)))
        # 지금 장비 상태 (0 ~ 4) - [ON] 을 누르면 이 상태에서 이어서 진행
        det = tk.Frame(top, bg="#263340")
        det.pack(fill="x", padx=8, pady=(0, 4))
        hd = tk.Frame(det, bg="#263340")
        hd.pack(fill="x", padx=6, pady=(5, 2))
        tk.Label(hd, text="지금 장비 상태 (자동 감지)", bg="#263340", fg="white", font=("맑은 고딕", 9, "bold")).pack(side="left")
        self.lbl_det = tk.Label(hd, text="", bg="#263340", fg="#90a4ae", font=("맑은 고딕", 8))
        self.lbl_det.pack(side="right")
        self.det_rows = {}
        for k in range(5):
            r = tk.Label(det, text="  %d   %s" % (k, self.CASES[k]), bg="#263340", fg="#b0bec5", anchor="w",
                         font=("맑은 고딕", 9), justify="left")
            r.pack(fill="x", padx=6, pady=1)
            r.bind("<Configure>", lambda e, r=r: r.configure(wraplength=max(200, e.width - 10)))
            self.det_rows[k] = r
        self.lbl_det_prob = tk.Label(det, text="", bg="#263340", fg="#ef9a9a", anchor="w", font=("맑은 고딕", 9, "bold"),
                                     justify="left")
        self.lbl_det_prob.pack(fill="x", padx=6, pady=(0, 5))
        sf_ = tk.Frame(top, bg=BG)
        sf_.pack(fill="x", padx=8, pady=(0, 6))
        self.step_lbls = []
        for k in range(6):
            lb = tk.Label(sf_, text="", bg="#455a64", fg="#eceff1", font=("맑은 고딕", 8), pady=2, justify="center")
            lb.pack(side="left", fill="x", expand=True, padx=1)
            self.step_lbls.append(lb)
        self.show_steps((), None)
        for k_, lb in enumerate(self.step_lbls, 1):
            lb.bind("<Button-1>", lambda e, k_=k_: self.step_click(k_))
        # 한 단계씩 (디버그): 단계마다 멈추고 [▶ 다음] (또는 깜빡이는 칩) 을 눌러야 진행
        dbg = tk.Frame(top, bg="#37474f")
        dbg.pack(fill="x", padx=8, pady=(0, 8))
        self.var_stepmode = tk.BooleanVar(value=False)
        self.var_stepfine = tk.BooleanVar(value=False)
        self.step_mode = self.step_fine = False
        self.var_stepmode.trace_add("write", lambda *a: setattr(self, "step_mode", bool(self.var_stepmode.get())))
        self.var_stepfine.trace_add("write", lambda *a: setattr(self, "step_fine", bool(self.var_stepfine.get())))
        r1 = tk.Frame(dbg, bg="#37474f")
        r1.pack(fill="x", padx=6, pady=(4, 0))
        tk.Checkbutton(r1, text="🔧 한 단계씩 진행 (디버그)", variable=self.var_stepmode, bg="#37474f", fg="white",
                       selectcolor="#263238", activebackground="#37474f", activeforeground="white",
                       font=("맑은 고딕", 9, "bold")).pack(side="left")
        tk.Checkbutton(r1, text="Vdd 올리고 내릴 때 1 스텝마다도 멈춤", variable=self.var_stepfine, bg="#37474f", fg="#cfd8dc",
                       selectcolor="#263238", activebackground="#37474f", activeforeground="white",
                       font=("맑은 고딕", 8)).pack(side="left", padx=(10, 0))
        r2 = tk.Frame(dbg, bg="#37474f")
        r2.pack(fill="x", padx=6, pady=(2, 5))
        self.btn_next = tk.Button(r2, text="▶ 다음 단계", state="disabled", bg="#1e88e5", fg="white",
                                  activebackground="#1976d2", disabledforeground="#78909c", relief="flat", bd=0,
                                  font=("맑은 고딕", 9, "bold"), padx=12, command=self.step_next)
        self.btn_next.pack(side="left")
        self.btn_next.bind("<Return>", lambda e: self.step_next())
        tk.Button(r2, text="■ 여기서 중지", bg="#5d4037", fg="white", activebackground="#795548", relief="flat", bd=0,
                  font=("맑은 고딕", 9), padx=10, command=self.step_stop).pack(side="left", padx=(6, 0))
        self.lbl_step = tk.Label(r2, text="체크하고 [ON] / [OFF] 를 누르면 단계마다 멈춥니다 (멈춰 있는 동안에도 장비 값은 계속 읽음)",
                                 bg="#37474f", fg="#b0bec5", font=("맑은 고딕", 8), anchor="w", justify="left")
        self.lbl_step.pack(side="left", padx=8, fill="x", expand=True)
        self.lbl_step.bind("<Configure>", lambda e: self.lbl_step.configure(wraplength=max(150, e.width - 6)))
        self._step_go = threading.Event()
        self._step_wait = None
        self.root.after(800, self.update_detect)

        # 설정 (접기/펼치기) - 줄마다 짧게 (좁은 화면에서도 보이게)
        T_OPEN, T_CLOSE = "▼ 설정 (기본값 · 목표 전류 · 이상 판정) - 눌러서 접기", "▶ 설정 (기본값 · 목표 전류 · 이상 판정) - 눌러서 펼치기"
        hdr = tk.Label(top, text=T_CLOSE, bg=BG, fg="#90caf9", font=("맑은 고딕", 9, "bold"), cursor="hand2", anchor="w")
        hdr.pack(fill="x", padx=10, pady=(0, 6))
        g = ttk.Frame(top, padding=6)
        self.sq = {}

        def toggle(e=None):
            if g.winfo_ismapped():
                g.pack_forget()
                hdr.configure(text=T_CLOSE)
            else:
                g.pack(fill="x", padx=6, pady=(0, 6), after=hdr)
                hdr.configure(text=T_OPEN)
        hdr.bind("<Button-1>", toggle)
        if s.get("seq_open", True):
            g.pack(fill="x", padx=6, pady=(0, 6))
            hdr.configure(text=T_OPEN)

        def line(title, color=None):
            f = ttk.Frame(g)
            f.pack(fill="x", pady=1)
            if title:
                ttk.Label(f, text=title, width=11, font=("맑은 고딕", 9, "bold"),
                          **({"foreground": color} if color else {})).pack(side="left")
            else:
                ttk.Label(f, text="", width=11).pack(side="left")
            return f

        def ent(f, key, default, w=6):
            v = tk.StringVar(value=str(s.get("seq_" + key, default)))
            ttk.Entry(f, textvariable=v, width=w).pack(side="left", padx=2)
            self.sq[key] = v
            return v

        def txt(f, t):
            ttk.Label(f, text=t).pack(side="left", padx=(2, 4))

        def chip(f, ch, t=None):
            col, name = CH_COLORS[ch]
            tk.Label(f, text=" %s " % (t or name), bg=col, fg="black" if ch == 1 else "white",
                     font=("맑은 고딕", 8, "bold")).pack(side="left", padx=(4, 1))

        f = line("게이트 기본값")
        for ch, dv in ((1, "3.200"), (2, "3.200"), (3, "4.700")):
            chip(f, ch)
            ent(f, "g%d" % ch, dv)
        txt(f, "V")
        f = line("")
        ttk.Button(f, text="게이트에 기본값만 적용", command=self.seq_apply_gate).pack(side="left", padx=2)
        self.btn_preset = ttk.Button(f, text="↺ 샘플 기본값으로", command=self.reset_preset)
        self.lbl_preset = ttk.Label(g, text="", foreground="#1f4f8f", justify="left")
        self.lbl_preset.pack(anchor="w", pady=(0, 2))
        f = line("목표 전류")
        chip(f, 2, "1) CA")
        ent(f, "t_green", s.get("seq_t1", "100"))
        chip(f, 1, "2) DRV")
        ent(f, "t_yellow", s.get("seq_t2", "150"))
        txt(f, "mA")
        self.lbl_idq = ttk.Label(f, text="", foreground="#1f4f8f")
        self.lbl_idq.pack(side="left", padx=(6, 0))
        for k_ in ("t_green", "t_yellow"):
            self.sq[k_].trace_add("write", lambda *a: self.show_idq())
        f = line("")
        txt(f, "(각자 전류, 합 = Idq)  오차 ±")
        ent(f, "tol", "10", 4)
        txt(f, "%")
        f = line("Vdd")
        txt(f, "목표")
        ent(f, "vdd", "48", 5)
        txt(f, "V, 스텝")
        ent(f, "vstep", "1", 4)
        txt(f, "V, 간격")
        ent(f, "vdt", "0.5", 4)
        txt(f, "초")
        f = line("게이트 조정")
        txt(f, "스텝")
        ent(f, "gstep", "0.01", 5)
        txt(f, "V → 각자 목표의")
        ent(f, "fine_pct", "80", 3)
        txt(f, "%부터")
        ent(f, "gfine", "0.001", 5)
        txt(f, "V, 간격")
        ent(f, "gdt", "0.3", 4)
        txt(f, "초")
        f = line("")
        txt(f, "도달 후")
        ent(f, "settle", "3", 4)
        txt(f, "초 대기하며 미세 스텝으로 목표에 맞춤 (±")
        ent(f, "settle_band", "1", 4)
        txt(f, "mA 안이면 그대로 · 0 초 = 바로 다음)")
        f = line("상태 감지")
        txt(f, "Vdd 목표에서 Id ≥")
        ent(f, "biased", "10", 5)
        txt(f, "mA 면 바이어스 걸린 것 (4번), 아니면 3번")
        f = line("OFF 게이트")
        txt(f, "끌 때 CA · DRV 게이트를")
        ent(f, "off_g", "3.2", 6)
        txt(f, "V 까지 올림 (샘플 상관없음)")
        f = line("OFF 대기")
        txt(f, "0 V 에서")
        ent(f, "off_w1", "5", 4)
        txt(f, "초 → Vdd OFF →")
        ent(f, "off_w2", "5", 4)
        txt(f, "초 → 게이트 OFF")
        RED = "#c62828"
        f = line("이상 판정", RED)
        txt(f, "1) 게이트 내리기 전 Vdd ≥")
        ent(f, "short", "20", 5)
        txt(f, "mA,  Vdd 올리는 동안 E36155A 전류 제한")
        ent(f, "ramp_ilim", "50", 4)
        txt(f, "mA (0 = 안 씀)")
        # 2) 측정 중 Vdd 전류 감시: 측정 종류마다 따로 (DPD 는 신호가 바뀔 때마다 전류가 크게 튐)
        sp0, sm0, im0 = s.get("seq_spike_pct", "50"), s.get("seq_spike_ma", "100"), s.get("seq_imax", "1.5")
        f = line("")
        txt(f, "2) 측정 중 Vdd 전류 (측정 종류마다)")
        for pre, name, dflt in (("", "Pulse", (sp0, sm0, im0)), ("mod_", "Mod.", (sp0, sm0, im0)),
                                ("dpd_", "DPD", ("150", "500", im0))):
            f = line("")
            tk.Label(f, text=" %s " % name, bg={"": "#ff9800", "mod_": "#26a69a", "dpd_": "#1e88e5"}[pre], fg="white",
                     font=("맑은 고딕", 8, "bold"), width=6).pack(side="left", padx=(4, 4))
            txt(f, "급상승 ≥")
            ent(f, pre + "spike_pct", dflt[0], 4)
            txt(f, "% 그리고 ≥")
            ent(f, pre + "spike_ma", dflt[1], 5)
            txt(f, "mA,  최대 Id")
            ent(f, pre + "imax", dflt[2], 5)
            txt(f, "A")
        f = line("")
        txt(f, "    게이트 조정 중 목표 ×")
        ent(f, "over", "1.5", 4)
        f = line("")
        txt(f, "3) 게이트 오차 >")
        ent(f, "gtol", "0.05", 5)
        txt(f, "V, 게이트 전류 >")
        ent(f, "gimax", "5", 4)
        txt(f, "mA")

    PRESET_KEYS = ("g1", "g2", "g3", "vdd", "t_green", "t_yellow")

    def get_preset(self):
        """샘플 종류 기본값용: 게이트 기본값 · Vdd 목표 · 목표 전류 (CA, DRV)"""
        return {k: self.sq[k].get().strip() for k in self.PRESET_KEYS}

    PRESET_NAMES = {"g1": "DRV", "g2": "CA", "g3": "PK", "vdd": "Vdd", "t_green": "목표 CA", "t_yellow": "목표 DRV"}

    def set_preset(self, d, name=""):
        """(PA Bench Monitor) 샘플 종류를 바꾸면 그 종류 기본값을 그대로 넣음 - 지정 안 된 칸은 빈칸.
        사용자가 칸을 바꾸면 그 값으로 시험(시뮬레이션) 가능, [↺ 샘플 기본값으로] 로 되돌림. 바뀐 항목 목록 반환"""
        d = d or {}
        self.preset_vals = {k: str(d.get(k, "")).strip() for k in self.PRESET_KEYS}
        self.preset_name = name
        changed = []
        for k in self.PRESET_KEYS:
            v = self.preset_vals[k]
            if v != self.sq[k].get().strip():
                self.sq[k].set(v)
                changed.append(k)
        if not getattr(self, "_preset_traced", False):
            self._preset_traced = True
            for k in self.PRESET_KEYS:
                self.sq[k].trace_add("write", lambda *a: self.show_preset_state())
        self.show_preset_state()
        return changed

    def preset_missing(self):
        pv = getattr(self, "preset_vals", None)
        if pv is None:
            return []
        return [k for k in self.PRESET_KEYS if not pv.get(k)]

    def reset_preset(self):
        for k, v in (getattr(self, "preset_vals", None) or {}).items():
            self.sq[k].set(v)

    def show_preset_state(self):
        pv = getattr(self, "preset_vals", None)
        if pv is None or not hasattr(self, "lbl_preset"):
            return
        name = getattr(self, "preset_name", "") or "(선택 안 함)"
        miss = self.preset_missing()
        diff = [k for k in self.PRESET_KEYS if pv.get(k) and self.sq[k].get().strip() != pv[k]]
        if miss:
            self.lbl_preset.configure(text="🔒 샘플 종류 '%s' 바이어스 기본값이 없습니다 (%s) → 원격 제어 잠금\n"
                                           "   왼쪽 ① 샘플 종류 [설정] 에서 지정하세요" % (name, ", ".join(self.PRESET_NAMES[k] for k in miss)),
                                      foreground="#c62828")
        elif diff:
            self.lbl_preset.configure(text="※ 샘플 '%s' 기본값에서 바꾼 값으로 시험: %s" % (name, ", ".join(
                "%s %s (기본 %s)" % (self.PRESET_NAMES[k], self.sq[k].get().strip() or "빈칸", pv[k]) for k in diff)),
                                      foreground="#e65100")
        else:
            self.lbl_preset.configure(text="✔ 샘플 종류 '%s' 바이어스 기본값 적용 중" % name, foreground="#2e7d32")
        if diff and not miss:
            self.btn_preset.pack(side="left", padx=2)
        else:
            self.btn_preset.pack_forget()
        self.apply_lock()

    def lock_reason(self):
        """기본값이 없으면 원격 제어 잠금 (읽기 · 감시 · 긴급 정지는 그대로)"""
        miss = self.preset_missing()
        if miss:
            return "샘플 종류 '%s' 의 바이어스 기본값이 없습니다 (%s).\n\n바이어스 값을 지정하세요:\n" \
                   "PA Bench Monitor 왼쪽 ① 샘플 종류 옆 [설정] → 이 종류의 DRV · CA · PK · Vdd · 목표 전류 입력" % (
                       getattr(self, "preset_name", "") or "(선택 안 함)", ", ".join(self.PRESET_NAMES[k] for k in miss))
        return None

    def locked(self, show=True):
        why = self.lock_reason()
        if why and show:
            messagebox.showwarning(APP, why)
        return bool(why)

    def apply_lock(self):
        if not hasattr(self, "chk_ctrl"):
            return
        if self.lock_reason():
            if self.var_ctrl.get():
                self.var_ctrl.set(False)
            self.on_ctrl_toggle()

    def show_idq(self):
        a, b = fnum(self.sq["t_green"].get()), fnum(self.sq["t_yellow"].get())
        if hasattr(self, "lbl_idq"):
            self.lbl_idq.configure(text=("→ Idq %.0f mA" % (a + b)) if (a == a and b == b) else "")

    def get_settings(self):
        out = {"psu_e_addr": self.var_e.get().strip(), "psu_n_addr": self.var_n.get().strip()}
        for k, v in self.lim.items():
            out["psu_" + k] = v.get()
        for k, v in self.sq.items():
            out["seq_" + k] = v.get()
        return out

    def sqf(self, k, default):
        if k not in self.sq:
            return default
        v = fnum(self.sq[k].get())
        return v if v == v else default

    def lim_prefix(self):
        """측정 중 전류 감시 한계: "" = Pulse, "mod_" = Mod., "dpd_" = DPD (Bench 가 lim_mode_fn 으로 알려 줌)"""
        fn = getattr(self, "lim_mode_fn", None)
        try:
            m = fn() if fn else "pulse"
        except Exception:
            m = "pulse"
        return {"mod": "mod_", "dpd": "dpd_"}.get(m, "")

    def set_state(self, st):
        """lamp: off / seq / on / fault"""
        self.bias_state = st
        self.post("lamp", st)

    # ------------------------------------------------ 순서 동작 (공통)
    def seq_status(self, txt, color="#cfd8dc"):
        self.post("seq", txt, color)

    # ---- 바이어스 진행 창 (단계 목록형): 진행 중 → 완료 (몇 초 뒤 저절로 닫힘) · 이미 켜짐/꺼짐 알림
    POP_BG, POP_FT, POP_LINE = "#263238", "#21303a", "#37474f"
    CASE_SHORT = {0: "모두 꺼짐 → 처음부터", 1: "게이트만 켜져 있음", 2: "게이트 + Vdd 일부 켜짐",
                  3: "Vdd 켜짐 · 전류 거의 없음", 4: "바이어스 다 걸려 있음"}

    def _pop_new(self, color):
        """창 틀: 머리(아이콘 · 제목 · 걸린 시간) / 부제 / 본문 / 값 / 아래(안내 · 닫기)"""
        self.busy_close()
        BG = self.POP_BG
        w = tk.Toplevel(self.root)
        w.withdraw()
        w.title("POWER SUPPLY")
        w.transient(self.root)
        w.resizable(False, False)
        w.configure(bg=BG)
        hd = tk.Frame(w, bg=color)
        hd.pack(fill="x")
        w._hd = hd
        row = tk.Frame(hd, bg=color)
        row.pack(fill="x", padx=14, pady=(10, 0))
        w._ic = tk.Label(row, text="", bg=color, fg="white", font=("맑은 고딕", 15, "bold"), width=2)
        w._ic.pack(side="left")
        w._title = tk.Label(row, text="", bg=color, fg="white", font=("맑은 고딕", 13, "bold"), anchor="w")
        w._title.pack(side="left", padx=(4, 0))
        w._tm = tk.Label(row, text="", bg=color, fg="white", font=("맑은 고딕", 9))
        w._tm.pack(side="right")
        w._sub = tk.Label(hd, text="", bg=color, fg="#e3f2fd", font=("맑은 고딕", 9), anchor="w")
        w._sub.pack(fill="x", padx=(46, 14), pady=(0, 9))
        w._body = tk.Frame(w, bg=BG)
        w._body.pack(fill="x", padx=16, pady=(8, 0))
        w._vals = tk.Frame(w, bg=BG)
        w._vals.pack(fill="x", padx=16)
        w._gates = tk.Label(w, text="", bg=BG, fg="#90a4ae", font=("맑은 고딕", 9))
        w._gates.pack(pady=(0, 6))
        w._cd = tk.Canvas(w, height=3, bg=self.POP_LINE, highlightthickness=0, bd=0)
        ft = tk.Frame(w, bg=self.POP_FT)
        ft.pack(fill="x", side="bottom")
        w._ft = ft
        w._note = tk.Label(ft, text="", bg=self.POP_FT, fg="#90a4ae", font=("맑은 고딕", 9), anchor="w")
        w._note.pack(side="left", padx=(16, 8), pady=9)
        tk.Button(ft, text="닫기", command=self.busy_close, bg="#455a64", fg="white", activebackground="#546e7a",
                  activeforeground="white", relief="flat", bd=0, padx=16, pady=3,
                  font=("맑은 고딕", 9)).pack(side="right", padx=12, pady=8)
        w.protocol("WM_DELETE_WINDOW", self.busy_close)
        w._t0 = time.time()
        w._val_lbls = {}
        self._busy = w
        self._busy_job = None
        return w

    def _pop_show(self, w, width=420):
        try:
            w.update_idletasks()
            ww = max(width, w.winfo_reqwidth())
            hh = w.winfo_reqheight()
            x = self.root.winfo_rootx() + (self.root.winfo_width() - ww) // 2
            y = self.root.winfo_rooty() + (self.root.winfo_height() - hh) // 2
            w.geometry("%dx%d+%d+%d" % (ww, hh, max(0, x), max(0, y)))
            w.deiconify()
            w.attributes("-topmost", True)
        except tk.TclError:
            pass

    def _pop_refit(self, w):
        """내용이 바뀌어 높이가 달라지면 가운데를 유지한 채 크기만 맞춤"""
        try:
            w.update_idletasks()
            ww, hh = w.winfo_width(), w.winfo_reqheight()
            if abs(hh - w.winfo_height()) > 2:
                x = self.root.winfo_rootx() + (self.root.winfo_width() - ww) // 2
                y = self.root.winfo_rooty() + (self.root.winfo_height() - hh) // 2
                w.geometry("%dx%d+%d+%d" % (ww, hh, max(0, x), max(0, y)))
        except tk.TclError:
            pass

    def _pop_values(self, w, items):
        """값 줄: [(이름, 값 글자, 단위), ...] - 큰 숫자"""
        for c in w._vals.winfo_children():
            c.destroy()
        w._val_lbls = {}
        if not items:
            return
        tk.Frame(w._vals, bg=self.POP_LINE, height=1).pack(fill="x", pady=(2, 4))
        row = tk.Frame(w._vals, bg=self.POP_BG)
        row.pack(fill="x")
        for k, (name, val, unit) in enumerate(items):
            col = tk.Frame(row, bg=self.POP_BG)
            col.pack(side="left", expand=True)
            tk.Label(col, text=name, bg=self.POP_BG, fg="#90a4ae", font=("맑은 고딕", 8)).pack()
            ln = tk.Frame(col, bg=self.POP_BG)
            ln.pack()
            lv = tk.Label(ln, text=val, bg=self.POP_BG, fg="white", font=("Consolas", 16, "bold"))
            lv.pack(side="left")
            tk.Label(ln, text=unit, bg=self.POP_BG, fg="#90a4ae", font=("맑은 고딕", 8)).pack(side="left", anchor="s", pady=(0, 3))
            w._val_lbls[name] = lv

    def _gate_text(self, vg=None):
        vg = vg or {c: self.sets.get(("n", c), {}).get("v") for c in (1, 2, 3)}
        if all(vg.get(c) is None for c in (1, 2, 3)):
            return ""
        return "게이트   " + "  ·  ".join("%s %s" % (CH_COLORS[c][1], ("%.3f" % vg[c]) if vg.get(c) is not None else "-")
                                       for c in (1, 2, 3)) + " V"

    def _badge(self, par, state, k):
        cv = tk.Canvas(par, width=22, height=22, bg=self.POP_BG, highlightthickness=0, bd=0)
        col = {"d": "#2e7d32", "c": "#ffd54f"}.get(state, "#546e7a")
        if state == "d":
            cv.create_oval(2, 2, 20, 20, fill=col, outline=col)
            cv.create_text(11, 11, text="✔", fill="white", font=("맑은 고딕", 8, "bold"))
        else:
            cv.create_oval(2, 2, 20, 20, outline=col, width=2)
            cv.create_text(11, 11, text=str(k), fill=col, font=("맑은 고딕", 8, "bold"))
        return cv

    def pop_open(self, kind, names, right, skip=(), sub="", color="#1565c0"):
        """진행 창 열기 - kind on/off, names: 단계 이름 목록, right: 오른쪽 글자 (목표값), skip: 건너뛰는 단계 번호"""
        w = self._pop_new(color)
        w._kind = kind
        w._names, w._right, w._skip = list(names), list(right), set(skip)
        w._cur, w._detail, w._frac = 0, "", None
        w._ic.configure(text="⚡" if kind == "on" else "⏻")
        w._title.configure(text="Bias ON 진행 중" if kind == "on" else "Bias OFF 진행 중")
        w._sub.configure(text=sub)
        w._note.configure(text="창을 닫아도 진행은 계속됩니다")
        self._pop_values(w, [("Vdd", "-", "V"), ("Id", "-", "mA")] +
                         ([("목표 Idq", "%.0f" % self._pop_idq, "mA")] if kind == "on" and getattr(self, "_pop_idq", 0) else []))
        self._pop_steps(w)
        self._pop_live()
        self._pop_show(w)

    def _pop_steps(self, w):
        for c in w._body.winfo_children():
            c.destroy()
        for k, nm in enumerate(w._names, 1):
            if k in w._skip:
                st = "s"
            elif k < w._cur:
                st = "d"
            elif k == w._cur:
                st = "c"
            else:
                st = "p"
            fg = {"d": "#a5d6a7", "c": "white", "s": "#546e7a"}.get(st, "#78909c")
            row = tk.Frame(w._body, bg=self.POP_BG)
            row.pack(fill="x", pady=2)
            self._badge(row, "d" if st == "d" else ("c" if st == "c" else "p"), k).pack(side="left")
            f = ("맑은 고딕", 10, "bold") if st == "c" else ("맑은 고딕", 10, "overstrike") if st == "s" else ("맑은 고딕", 10)
            tk.Label(row, text=nm, bg=self.POP_BG, fg=fg, font=f, anchor="w").pack(side="left", padx=(8, 0))
            rt = ("건너뜀" if st == "s" else "완료" if st == "d" else
                  (w._detail or w._right[k - 1]) if st == "c" else w._right[k - 1])
            tk.Label(row, text=rt, bg=self.POP_BG, fg="#ffd54f" if st == "c" else ("#546e7a" if st == "s" else "#b0bec5"),
                     font=("맑은 고딕", 9, "bold") if st == "c" else ("맑은 고딕", 9)).pack(side="right")
            if st == "c":
                w._cur_right = row.winfo_children()[-1]
                pb = tk.Canvas(w._body, height=6, bg=self.POP_LINE, highlightthickness=0, bd=0)
                pb.pack(fill="x", padx=(30, 0), pady=(1, 4))
                w._pb = pb
                pb.bind("<Configure>", lambda e: self._pop_bar())
        self._pop_refit(w)

    def _pop_bar(self):
        w = self._busy
        pb = getattr(w, "_pb", None) if w is not None else None
        if pb is None:
            return
        try:
            pb.delete("all")
            W = pb.winfo_width()
            if w._frac is None:                             # 진행 정도를 모르면 움직이는 막대
                x = (time.time() * 120) % (W + 80) - 80
                pb.create_rectangle(x, 0, x + 80, 6, fill="#ffd54f", outline="")
            else:
                pb.create_rectangle(0, 0, W * max(0.0, min(1.0, w._frac)), 6, fill="#ffd54f", outline="")
        except tk.TclError:
            pass

    def pop_seq(self, txt):
        """순서 진행 글자 → 지금 단계 · 진행 막대 갱신"""
        import re
        w = self._busy
        if w is None or not getattr(w, "_names", None):
            return
        m = re.match(r"\s*(\d)/6\s*(.*)", txt)
        if not m:
            return
        n, rest = int(m.group(1)), m.group(2)
        if w._kind == "on":
            k = n
        else:
            k = {1: 1, 2: 1, 3: 1, 4: 2, 5: 3, 6: 4}.get(n, w._cur)
        frac, det = None, ""
        mv = re.search(r"([\d.]+)\s*/\s*([\d.]+)\s*V\b", rest)
        mi = re.search(r"([\d.]+)\s*/\s*([\d.]+)\s*mA", rest)
        if mi:
            a, b = float(mi.group(1)), float(mi.group(2))
            frac, det = (a / b if b else None), "%.0f / %.0f mA" % (a, b)
        elif mv:
            a, b = float(mv.group(1)), float(mv.group(2))
            frac, det = (a / b if b else None), "%.1f / %.1f V" % (a, b)
        elif w._kind == "off" and k == 2:
            md = re.search(r"([\d.]+)\s*V", rest)
            if md:
                lv = float(md.group(1))
                w._v0 = max(getattr(w, "_v0", 0.0), lv)
                frac, det = (1 - lv / w._v0 if w._v0 else None), "%.1f V" % lv
        elif "쇼트" in rest:
            det = "쇼트 확인"
        if k != w._cur:
            w._cur, w._detail, w._frac = k, det, frac
            self._pop_steps(w)
        else:
            w._detail, w._frac = det or w._detail, frac if frac is not None else w._frac
            try:
                w._cur_right.configure(text=w._detail or w._right[k - 1])
            except (tk.TclError, AttributeError, IndexError):
                pass
        self._pop_bar()

    def _pop_live(self):
        """0.3초마다: 걸린 시간 · Vdd · Id · 게이트 값"""
        w = self._busy
        if w is None or not getattr(w, "_names", None) or getattr(w, "_done", False):
            return
        try:
            if not w.winfo_exists():
                return
            w._tm.configure(text="%d초" % (time.time() - w._t0))
            i_, v_, _ = self.e_last
            if "Vdd" in w._val_lbls and v_ == v_:
                w._val_lbls["Vdd"].configure(text="%.1f" % v_)
            if "Id" in w._val_lbls and i_ == i_:
                w._val_lbls["Id"].configure(text="%.1f" % (i_ * 1e3) if i_ < 0.1 else "%.0f" % (i_ * 1e3))
            w._gates.configure(text=self._gate_text())
            if w._frac is None:
                self._pop_bar()
        except tk.TclError:
            return
        self.root.after(300, self._pop_live)

    def _pop_countdown(self, w, ms):
        """아래 막대가 줄어들다가 ms 뒤 닫힘"""
        w._cd.pack(fill="x", side="bottom", before=w._ft)
        t_end = time.time() + ms / 1000.0

        def tick():
            if self._busy is not w:
                return
            left = t_end - time.time()
            if left <= 0:
                self.busy_close()
                return
            try:
                W = w._cd.winfo_width()
                w._cd.delete("all")
                w._cd.create_rectangle(0, 0, W * left / (ms / 1000.0), 3, fill="#90a4ae", outline="")
                w._note.configure(text="%d초 뒤 자동으로 닫힘" % max(1, math.ceil(left)))
            except tk.TclError:
                return
            self._busy_job = self.root.after(100, tick)
        tick()

    def pop_done(self, kind, ok=True, ms=2500):
        """완료 화면: 결과 값만 크게 → ms 뒤 저절로 닫힘"""
        w = self._busy
        if w is None:
            return
        w._done = True
        col = "#2e7d32" if ok else "#ef6c00"
        for wd in [w._hd] + list(w._hd.winfo_children()) + [x for c in w._hd.winfo_children() for x in c.winfo_children()]:
            try:
                wd.configure(bg=col)
            except tk.TclError:
                pass
        w._ic.configure(text="✔" if ok else "※")
        for c in w._body.winfo_children():
            c.destroy()
        w._body.configure(height=1)
        w._pb = None
        i_, v_, _ = self.e_last
        if kind == "on":
            bi = getattr(self, "bias_info", None) or {}
            w._title.configure(text="Bias ON 완료" if ok else "Bias ON 완료 - 전류 확인 필요")
            w._sub.configure(text="RF 는 켜지 않음" if not self.auto_measure else "→ 자동 측정 시작")
            items = [("Vdd", "%.1f" % bi.get("vdd", v_), "V"), ("Idq", "%.0f" % (bi.get("idq", i_) * 1e3), "mA")]
            if bi.get("i_ca") is not None and bi.get("i_drv") is not None and bi["i_ca"] == bi["i_ca"]:
                items.append(("CA / DRV", "%.0f / %.0f" % (bi["i_ca"] * 1e3, bi["i_drv"] * 1e3), "mA"))
            self._pop_values(w, items)
            g = self._gate_text(bi.get("vg"))
            if bi.get("i_leak") is not None:
                g += "   ·   누설 %.1f mA" % (bi["i_leak"] * 1e3)
            w._gates.configure(text=g)
        else:
            w._title.configure(text="Bias OFF 완료")
            w._sub.configure(text="Vdd · 게이트 모두 OFF")
            self._pop_values(w, [])
            w._gates.configure(text="")
        self._pop_refit(w)
        self._pop_countdown(w, ms)

    def pop_info(self, title, sub, items=(), ms=3500):
        """알림 (이미 켜짐 / 이미 꺼짐 등): 값만 보여 주고 ms 뒤 닫힘"""
        w = self._pop_new("#37474f")
        w._ic.configure(text="ℹ")
        w._title.configure(text=title)
        w._sub.configure(text=sub, fg="#cfd8dc")
        w._body.configure(height=1)
        self._pop_values(w, list(items))
        if not items:
            w._vals.pack_forget()
        w._gates.configure(text="")
        self._pop_show(w, 380)
        self._pop_countdown(w, ms)

    # 예전 이름 (다른 곳에서 부름)
    def busy(self, title, text="", color="#1565c0"):
        self.pop_info(title, text, (), ms=10 ** 9)

    def busy_done(self, title, text="", color="#2e7d32", ms=2500):
        self.pop_info(title, text, (), ms)

    def toast(self, title, text="", color="#455a64", ms=3500):
        self.pop_info(title, text, (), ms)

    def busy_close(self):
        w = getattr(self, "_busy", None)
        self._busy = None
        job = getattr(self, "_busy_job", None)
        if job:
            try:
                self.root.after_cancel(job)
            except tk.TclError:
                pass
        self._busy_job = None
        if w is not None:
            try:
                w.destroy()
            except tk.TclError:
                pass

    def all_off(self):
        """Vdd · 게이트 출력이 모두 꺼져 있으면 True"""
        return not any(bool(self.sets.get(k, {}).get("o")) for k in (("e", None), ("n", 1), ("n", 2), ("n", 3)))

    def red(self, msg):
        """이상: 빨간 팝업 (바로 보이게)"""
        self.post("red", msg)

    def ensure_ready(self, need_both=True, lock_ok=False):
        if self.seq_running:
            messagebox.showwarning(APP, "전원 순서가 진행 중입니다.")
            return False
        if not self.var_ctrl.get() and not self.lock_reason():
            self.var_ctrl.set(True)       # POWER SUPPLY ON/OFF 는 잠금과 상관없이 동작 (확인 창에서 한 번 더 확인)
            self.on_ctrl_toggle()
        miss = [n for k, n in (("e", "E36155A (Vdd)"), ("n", "N6705C (게이트)")) if self.inst[k] is None]
        if miss and need_both:
            messagebox.showwarning(APP, "연결되지 않았습니다: %s\n먼저 [연결] 하세요." % ", ".join(miss))
            return False
        return True

    def gate_presets(self):
        out = {}
        lo, hi = self.lim_val("g_min"), self.lim_val("g_max")
        for ch in (1, 2, 3):
            v = fnum(self.sq["g%d" % ch].get())
            if v != v or (lo == lo and v < lo - 1e-9) or (hi == hi and v > hi + 1e-9):
                messagebox.showwarning(APP, "%s 게이트 기본값 %s V 가 안전 한계 %s ~ %s V 밖입니다." % (
                    CH_COLORS[ch][1], self.sq["g%d" % ch].get(), self.lim["g_min"].get(), self.lim["g_max"].get()))
                return None
            out[ch] = round(v, 3)
        return out

    def seq_apply_gate(self):
        if self.locked():
            return
        if self.inst["n"] is None:
            messagebox.showwarning(APP, "N6705C 가 연결되지 않았습니다.")
            return
        if not self.ensure_ready(need_both=False):
            return
        gp = self.gate_presets()
        if gp is None:
            return
        if self.vdd_on() and not messagebox.askyesno(
                APP, "Vdd 가 켜져 있습니다. 게이트를 기본값으로 바꾸면 전류가 크게 바뀔 수 있습니다.\n그래도 적용할까요?"):
            return
        n = self.inst["n"]

        def job():
            for ch, v in gp.items():
                n.write("VOLT %.3f,(@%d)" % (v, ch))
            err = n.query("SYST:ERR?").strip()
            self.post("log", "게이트 기본값 적용 %s  (SYST:ERR? %s)" % (gp, err), "ok" if err.startswith(("+0", "0")) else "err")
            self.read_job(full=True)
        self.worker.tasks.put(job)

    def plan_common(self, gp):
        return dict(gp=gp, vdd=self.sqf("vdd", 48.0), vstep=max(0.1, self.sqf("vstep", 1)), vdt=max(0.01, self.sqf("vdt", 0.5)),
                    short=self.sqf("short", 20) / 1e3, gstep=abs(self.sqf("gstep", 0.01)), gfine=abs(self.sqf("gfine", 0.001)),
                    fine_pct=min(0.99, max(0.3, self.sqf("fine_pct", 80) / 100.0)),
                    gdt=max(0.01, self.sqf("gdt", 0.3)), tol=self.sqf("tol", 10) / 100.0, over=max(1.1, self.sqf("over", 1.5)),
                    gtol=abs(self.sqf("gtol", 0.05)), gmin=self.lim_val("g_min"), gmax=self.lim_val("g_max"),
                    w1=max(0.0, self.sqf("off_w1", 5)), w2=max(0.0, self.sqf("off_w2", 5)),
                    ramp_ilim=max(0.0, self.sqf("ramp_ilim", 50)) / 1e3,
                    settle=max(0.0, self.sqf("settle", 3)), band=max(0.0, self.sqf("settle_band", 1)) / 1e3)

    # ------------------------------------------------ POWER SUPPLY ON
    # ------------------------------------------------ 지금 장비 상태 감지 (0 ~ 4)
    CASES = {0: "아무것도 안 켬 → 처음부터 (게이트 기본값 → Vdd → CA → DRV)",
             1: "게이트만 ON → 지금 게이트 값 그대로, Vdd ON → 0.1 V 쇼트 확인 → 목표 V → CA → DRV",
             2: "게이트 + Vdd ON (목표보다 낮음) → 지금 전압에서 쇼트 확인 → 목표 V → CA → DRV",
             3: "Vdd 목표까지 올림 (전류 거의 없음) → 누설 전류 기록 → 게이트만 조정: CA → DRV",
             4: "바이어스 다 걸림 → 지금 전압 · 총 전류만 기록하고 바로 측정"}
    CASE_SKIP = {0: (), 1: (1,), 2: (1, 2), 3: (1, 2, 3), 4: (1, 2, 3, 4, 5)}

    def detect_state(self):
        """→ (경우 0~4 또는 None, 설명, 문제). 출력 상태 · 설정값은 1초마다 읽는 값 사용"""
        e, n = self.inst["e"], self.inst["n"]
        if e is None or n is None:
            return None, "", "E36155A · N6705C 를 먼저 연결하세요"
        gs = {c: self.sets.get(("n", c), {}) for c in (1, 2, 3)}
        es = self.sets.get(("e", None), {})
        if any("o" not in g for g in gs.values()) or "o" not in es:
            return None, "", "상태를 아직 못 읽었습니다"
        g_on = [c for c in (1, 2, 3) if gs[c].get("o")]
        vdd_on = bool(es.get("o"))
        i_, v_, t_ = self.e_last
        target = self.sqf("vdd", 48.0) if self.sq["vdd"].get().strip() else 48.0
        thr = self.sqf("biased", 10.0) / 1e3
        desc = "게이트 ON %d/3 · Vdd %s%s" % (len(g_on), ("ON %.1f V" % v_) if vdd_on and v_ == v_ else ("ON" if vdd_on else "OFF"),
                                             (" · Id %.1f mA" % (i_ * 1e3)) if (vdd_on and i_ == i_) else "")
        if not g_on and not vdd_on:
            return 0, desc, None
        if not g_on and vdd_on:
            return None, desc, "게이트가 꺼져 있는데 Vdd 가 켜져 있습니다 (순서 이상)"
        if len(g_on) < 3:
            return None, desc, "게이트가 일부만 켜져 있습니다 (%s) - 3채널 모두 켜거나 모두 끄세요" % (
                ", ".join(CH_COLORS[c][1] for c in g_on))
        lo, hi = self.lim_val("g_min"), self.lim_val("g_max")
        bad = [c for c in (1, 2, 3) if gs[c].get("v") is None or (lo == lo and gs[c]["v"] < lo - 1e-9)
               or (hi == hi and gs[c]["v"] > hi + 1e-9)]
        if bad:
            return None, desc, "게이트 값이 안전 한계 %s ~ %s V 밖입니다: %s" % (
                self.lim["g_min"].get(), self.lim["g_max"].get(), ", ".join("%s %.3f V" % (CH_COLORS[c][1], gs[c].get("v") or float("nan")) for c in bad))
        if not vdd_on:
            return 1, desc, None
        if v_ != v_ or time.time() - t_ > 3:
            return None, desc, "Vdd 측정값이 없습니다"
        if v_ > target + 0.5:
            return None, desc, "Vdd %.1f V 가 목표 %.1f V 보다 높습니다" % (v_, target)
        if v_ < target - 0.5:
            return 2, desc, None
        return (4 if i_ >= thr else 3), desc, None

    def update_detect(self):
        """[ON] 아래 '지금 장비 상태' 표시 (0.5 초마다)"""
        try:
            case, desc, prob = self.detect_state()
            if self.seq_running:
                prob = None
            for k, row in self.det_rows.items():
                on = case == k and not self.seq_running and self.bias_state != "on"
                row.configure(bg="#1565c0" if on else "#263340", fg="white" if on else "#b0bec5",
                              font=("맑은 고딕", 9, "bold" if on else "normal"))
            age = time.time() - self.e_last[2]
            if self.inst["e"] is None and self.inst["n"] is None:
                rd = "○ 연결 안 됨"
                col = "#90a4ae"
            elif self.polling and age < 3:
                rd = "● 읽는 중 (%.2g 초마다)" % self.poll_dt
                col = "#69f0ae"
            elif self.seq_running:
                rd = "● 순서 진행 중 (순서가 직접 읽음)"
                col = "#ffd54f"
            else:
                rd = "○ 읽기 멈춤 - 상태가 오래됐을 수 있음"
                col = "#ffab40"
            self.lbl_det.configure(text="%s   %s" % (rd, desc if desc else ""), fg=col)
            self.lbl_det_prob.configure(text=("⚠ " + prob) if prob else "")
            if not self.seq_running and self.bias_state != "on":
                self.show_steps(self.CASE_SKIP.get(case, ()), None)
        except (tk.TclError, AttributeError):
            pass
        self.root.after(500, self.update_detect)

    def blink_step(self, k, n=0):
        if self._step_wait != k or not self.step_lbls:
            return
        lb = self.step_lbls[k - 1]
        lb.configure(bg="#1e88e5" if n % 2 == 0 else "#0d47a1", fg="white", cursor="hand2")
        self.root.after(450, lambda: self.blink_step(k, n + 1))

    def show_steps(self, skip, cur):
        names = ("게이트 ON", "Vdd 0 V", "→ 목표 V", "CA", "DRV", "측정")
        for k, lb in enumerate(self.step_lbls, 1):
            if k in skip:
                lb.configure(text="%d %s\n건너뜀" % (k, names[k - 1]), bg="#37474f", fg="#78909c")
            elif cur is not None and k < cur:
                lb.configure(text="%d %s\n✔" % (k, names[k - 1]), bg="#2e7d32", fg="white")
            elif cur == k:
                lb.configure(text="%d %s\n진행 중" % (k, names[k - 1]), bg="#f9a825", fg="black")
            else:
                lb.configure(text="%d %s\n" % (k, names[k - 1]), bg="#455a64", fg="#eceff1")

    # ------------------------------------------------ POWER SUPPLY ON (지금 상태에서 이어서)
    def power_on(self, auto=False):
        """auto=True: 끝나면 PA Bench Monitor 가 바로 자동 측정 (ON + 측정). 기본은 바이어스만."""
        if self.seq_running:
            messagebox.showwarning(APP, "전원 순서가 진행 중입니다.")
            return
        if self.inst["e"] is None or self.inst["n"] is None:
            messagebox.showwarning(APP, "E36155A · N6705C 를 먼저 연결하세요.")
            return
        self.auto_measure = bool(auto)

        def job():                       # 지금 상태를 새로 읽고 판단
            self.read_job(full=True)
            self.post("call", self.power_on_detected)
        self.worker.tasks.put(job)

    def power_on_detected(self):
        case, desc, prob = self.detect_state()
        if case == 4 and not prob and not self.auto_measure and not self.step_mode:
            i_, v_, _ = self.e_last
            self.pop_info("이미 바이어스가 켜져 있습니다", "그대로 둡니다",
                          [("Vdd", "%.1f" % v_, "V"), ("Idq", "%.0f" % (i_ * 1e3), "mA")])
            return
        if prob or case is None:
            self.red_popup("POWER SUPPLY ON 진행 안 함\n\n지금 상태: %s\n⚠ %s" % (desc, prob or "상태를 알 수 없음"))
            return
        need = {0: self.PRESET_KEYS, 1: ("vdd", "t_green", "t_yellow"), 2: ("vdd", "t_green", "t_yellow"),
                3: ("t_green", "t_yellow"), 4: ()}[case]
        miss = [k for k in need if not self.sq[k].get().strip()]
        if miss:
            messagebox.showwarning(APP, "%d번 (%s)\n\n바이어스 값을 지정하세요: %s\n\n"
                                        "PA Bench Monitor 왼쪽 ① 샘플 종류 옆 [설정] 에서 이 종류의 값을 넣거나,\n"
                                        "파워서플라이 설정 칸에 직접 입력 (시험)" % (
                                            case, self.CASES[case], ", ".join(self.PRESET_NAMES[k] for k in miss)))
            return
        if case == 0:
            gp = self.gate_presets()
            if gp is None:
                return
        else:
            gp = {c: round(self.sets[("n", c)]["v"], 3) for c in (1, 2, 3)}      # 사용자가 켜 둔 지금 게이트 값
        p = self.plan_common(gp)
        p["case"] = case
        # 이상 처리 때 게이트를 올릴 값: 샘플 기본값이 있으면 그것, 없으면 시작할 때 게이트 값
        try:
            p["gp_safe"] = {c: round(float(self.sq["g%d" % c].get()), 3) for c in (1, 2, 3)}
        except ValueError:
            p["gp_safe"] = dict(gp)
        vmax = self.lim_val("vdd_max")
        if case < 4 and vmax == vmax and p["vdd"] > vmax + 1e-9:
            messagebox.showwarning(APP, "Vdd 목표 %.1f V 가 안전 한계 %.1f V 를 넘습니다." % (p["vdd"], vmax))
            return
        tg, ty = (self.sqf("t_green", 0) / 1e3, self.sqf("t_yellow", 0) / 1e3) if case < 4 else (0, 0)
        if case < 4 and (tg <= 0 or ty <= 0):
            messagebox.showwarning(APP, "목표 전류를 확인하세요: CA > 0, DRV > 0 mA (각자 전류, 둘을 합한 값 = Idq).")
            return
        p["tune"] = [(2, tg), (1, ty)]           # 각자 목표 전류: CA(초록) 먼저, 그다음 DRV(노랑)
        if self.rf_guard is not None and self.rf_guard() is False:
            return
        i_, v_, _ = self.e_last
        v_ = abs(v_) if (v_ == v_ and abs(v_) < 0.05) else v_
        lines = {0: "게이트 기본값 (DRV %.3f · CA %.3f · PK %.3f V) → 전체 ON → Vdd 0 V ON → 0.1 V 쇼트 확인 → %g V 씩 %.0f V → "
                    "누설 전류 기록 → CA +%.0f mA → DRV +%.0f mA (Idq %.0f mA) → 측정" % (gp[1], gp[2], gp[3], p["vstep"], p["vdd"], tg * 1e3, ty * 1e3, (tg + ty) * 1e3),
                 1: "지금 게이트 값 그대로 (DRV %.3f · CA %.3f · PK %.3f V) → Vdd 0 V ON → 0.1 V 쇼트 확인 → %.0f V → "
                    "누설 전류 기록 → CA +%.0f mA → DRV +%.0f mA (Idq %.0f mA) → 측정" % (gp[1], gp[2], gp[3], p["vdd"], tg * 1e3, ty * 1e3, (tg + ty) * 1e3),
                 2: "지금 Vdd %.1f V 에서 쇼트 확인 → %.0f V 까지 → 누설 전류 기록 → CA +%.0f mA → DRV +%.0f mA (Idq %.0f mA) → 측정" % (
                     v_, p["vdd"], tg * 1e3, ty * 1e3, (tg + ty) * 1e3),
                 3: "Vdd %.1f V · Id %.1f mA (< %g mA) → 누설 전류로 기록 → CA +%.0f mA → DRV +%.0f mA (Idq %.0f mA) → 측정" % (
                     v_, i_ * 1e3, self.sqf("biased", 10), tg * 1e3, ty * 1e3, (tg + ty) * 1e3),
                 4: "Vdd %.1f V · Id %.1f mA 그대로 → 게이트 · Vdd · 총 전류만 기록하고 바로 측정" % (v_, i_ * 1e3)}
        ty_ = (self.sqf("t_green", 0) + self.sqf("t_yellow", 0)) / 1e3          # Idq = CA + DRV
        tol_ = self.sqf("tol", 10) / 100.0
        if case == 4 and ty_ > 0 and abs(i_ - ty_) > tol_ * ty_:
            lines[4] += ("\n⚠ 샘플 목표 Idq %.0f mA (CA + DRV) 와 ±%g%% 넘게 다릅니다 (지금 %.1f mA) - 맞는지 확인하세요" % (
                ty_ * 1e3, tol_ * 100, i_ * 1e3))
        alt = {4: [(3, "3번처럼 게이트 다시 맞추기 (CA → DRV)")]}.get(case, [])
        # 검증 끝: 확인 창 없이 바로 진행 (한 단계씩 디버그 모드에서만 확인 창)
        ans = self.ask_case(case, desc, lines[case], alt) if self.step_mode else case
        if ans is None:
            return
        if ans != case:
            p["case"] = ans
            if ans == 3:
                tg, ty = self.sqf("t_green", 0) / 1e3, self.sqf("t_yellow", 0) / 1e3
                if tg <= 0 or ty <= 0:
                    messagebox.showwarning(APP, "목표 전류(CA · DRV)를 지정하세요.")
                    return
                p["tune"] = [(2, tg), (1, ty)]
        if not self.var_ctrl.get() and not self.lock_reason():
            self.var_ctrl.set(True)
            self.on_ctrl_toggle()
        self._busy_kind = "on"
        tg_, ty_ = (p["tune"][0][1], p["tune"][1][1]) if len(p.get("tune", [])) == 2 else (0, 0)
        self._pop_idq = (tg_ + ty_) * 1e3
        self.pop_open("on", ["게이트 기본값 → 전체 ON", "Vdd 0 V ON", "쇼트 확인 → Vdd 올리기",
                             "CA 게이트 조정", "DRV 게이트 조정", "값 기록 · 완료"],
                      ["", "", "→ %.0f V" % p["vdd"] if p.get("vdd") else "",
                       "+%.0f mA" % (tg_ * 1e3) if tg_ else "", "+%.0f mA" % (ty_ * 1e3) if ty_ else "", ""],
                      skip=self.CASE_SKIP.get(p["case"], ()),
                      sub="시작 상태: %s" % self.CASE_SHORT.get(p["case"], ""), color="#1565c0")
        self.start_seq(self.job_power_on, p)
        if getattr(self, "on_seq_start", None):
            self.on_seq_start(bool(self.auto_measure))   # (Bench) ON + 측정: 엑셀 저장 위치를 지금 물어봄

    def ask_case(self, case, desc, todo, alt):
        """확인 창: 감지된 단계로 진행 / 다른 단계 / 취소 → 고른 경우 번호 또는 None"""
        w = tk.Toplevel(self.root)
        w.withdraw()
        w.title("POWER SUPPLY ON")
        w.transient(self.root)
        w.resizable(False, False)
        tk.Label(w, text="  POWER SUPPLY ON - 지금 상태에서 이어서 진행", bg="#1e88e5", fg="white", anchor="w",
                 font=("맑은 고딕", 11, "bold"), pady=6).pack(fill="x")
        f = ttk.Frame(w, padding=14)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="감지: %d번 - %s" % (case, self.CASES[case]), font=("맑은 고딕", 10, "bold"),
                  wraplength=640, justify="left").pack(anchor="w")
        ttk.Label(f, text="지금 상태: " + desc, foreground="#455a64").pack(anchor="w", pady=(6, 0))
        ttk.Label(f, text="할 일: " + todo, wraplength=640, justify="left").pack(anchor="w", pady=(4, 0))
        sf = tk.Frame(f)
        sf.pack(fill="x", pady=(10, 4))
        names = ("게이트 ON", "Vdd 0 V", "→ 목표 V", "CA", "DRV", "측정")
        for k in range(1, 7):
            sk = k in self.CASE_SKIP[case]
            tk.Label(sf, text="%d %s" % (k, names[k - 1]), bg="#eceff1" if sk else "#2e7d32", fg="#90a4ae" if sk else "white",
                     font=("맑은 고딕", 8, "overstrike" if sk else "bold"), padx=8, pady=4).pack(side="left", fill="x", expand=True, padx=1)
        ttk.Label(f, text="이상이 생기면 빨간 경고 후 게이트 안전값 → Vdd 0 V 로 내립니다.", foreground="#666").pack(anchor="w")
        res = {"v": None}
        bf = ttk.Frame(f)
        bf.pack(fill="x", pady=(10, 0))

        def done(v):
            res["v"] = v
            w.destroy()
        ttk.Button(bf, text="▶ %d번으로 진행 (Enter)" % case, command=lambda: done(case)).pack(side="right", padx=2)
        for c, t in alt:
            ttk.Button(bf, text=t, command=lambda c=c: done(c)).pack(side="right", padx=2)
        ttk.Button(bf, text="취소", command=lambda: done(None)).pack(side="right", padx=2)
        w.bind("<Return>", lambda e: done(case))
        w.bind("<Escape>", lambda e: done(None))
        w.update_idletasks()
        try:
            x = self.root.winfo_rootx() + (self.root.winfo_width() - w.winfo_reqwidth()) // 2
            y = self.root.winfo_rooty() + (self.root.winfo_height() - w.winfo_reqheight()) // 2
            w.geometry("+%d+%d" % (max(0, x), max(0, y)))
        except tk.TclError:
            pass
        w.deiconify()
        w.lift()
        w.focus_force()
        try:
            w.grab_set()
        except tk.TclError:
            pass
        self.root.wait_window(w)
        return res["v"]

    def start_seq(self, fn, p):
        self.seq_abort = False
        self.seq_running = True
        self.was_polling = self.polling
        self.polling = False          # 순서 진행 중에는 순서가 직접 읽음
        self.worker.tasks.put(lambda: self.run_seq(fn, p))

    def step(self, k, txt, color="#ffd54f"):
        if k not in self._stepped:
            self._stepped.add(k)
            self.pause("%d번 %s" % (k, txt), k)
        self.seq_status("%d/6 %s" % (k, txt), color)
        self.post("call", lambda: self.show_steps(self.CASE_SKIP.get(self.plan.get("case", 0), ()), k))

    def pause(self, what, k=None, fine=False):
        """(한 단계씩 모드) 여기서 멈추고 [▶ 다음] 을 기다림 - 그동안 장비 값은 0.5 초마다 읽음"""
        if not self.step_mode or (fine and not self.step_fine):
            return
        self._step_go.clear()
        self._step_wait = k
        self.post("stepwait", what, k)
        self.post("log", "⏸ 멈춤 - 다음: %s" % what)
        t_read = 0.0
        while not self._step_go.is_set():
            if self.seq_abort:
                self._step_wait = None
                self.post("stepwait", None, None)
                raise SeqStop("사용자 중지")
            if time.time() - t_read > 0.5:
                t_read = time.time()
                try:
                    self.read_job(full=True)
                except Exception:
                    pass
            time.sleep(0.05)
        self._step_wait = None
        self.post("stepwait", None, None)
        self.post("log", "▶ 진행: %s" % what)

    def step_next(self):
        self._step_go.set()

    def step_click(self, k):
        if self._step_wait is not None and self._step_wait == k:
            self._step_go.set()

    def step_stop(self):
        if self.seq_running:
            self.seq_abort = True
            self._step_go.set()

    def _ramp_up(self, p, case, e):
        """2 ~ 3번: Vdd 0 V ON → 0.1 V 쇼트 확인 → 목표 V (OFF 와 같은 속도)"""
        if case <= 1:
            # 2. Vdd 0 V ON
            self.step(2, "Vdd 0 V 로 ON")
            e.write("VOLT 0")
            e.write("OUTP ON")
            self.wait(0.3)
            if e.query("OUTP?").strip() not in ("1", "ON"):
                self.fault("Vdd 출력이 켜지지 않았습니다 - 장비 확인")
            lv = 0.0
        else:
            lv = fnum(e.query("VOLT?"))
            lv = 0.0 if lv != lv else lv
        # 3. 쇼트 확인 (0.1 V 또는 지금 전압) → 스텝씩 목표까지
        if lv < 0.1:
            e.write("VOLT 0.1")
            lv = 0.1
        self.step(3, "Vdd %.1f V 쇼트 확인" % lv)
        self.wait(max(0.5, p["vdt"]))
        i, v = self.meas_vdd()
        if i >= p["short"]:
            self.fault("Vdd 과전류 (게이트를 내리기 전인데 전류가 흐름)\nVdd %.2f V 에서 %.1f mA ≥ %.0f mA" % (v, i * 1e3, p["short"] * 1e3))
        self.post("log", "Vdd %.1f V: %.2f mA - 쇼트 없음" % (lv, i * 1e3), "ok")
        # 목표 V 까지: 끌 때와 같은 속도 (스텝마다 쓰기만) · 전류 · 게이트 확인은 5 스텝마다 + 마지막
        self.step(3, "Vdd 올리는 중 → %.1f V" % p["vdd"])
        nst = 0
        while lv < p["vdd"] - 1e-9:
            lv = min(round(math.floor(lv / p["vstep"] + 1e-9) * p["vstep"] + p["vstep"], 3), p["vdd"])
            self.pause("Vdd → %.1f V" % lv, fine=True)
            e.write("VOLT %.3f" % lv)
            self.seq_status("3/6 Vdd 올리는 중 %.1f / %.1f V" % (lv, p["vdd"]), "#ffd54f")
            self.wait(p["vdt"])
            nst += 1
            if nst % 5 == 0 or lv >= p["vdd"] - 1e-9:
                i = self.meas_i()
                if i >= p["short"]:
                    self.fault("Vdd 과전류 (게이트를 내리기 전인데 전류가 흐름)\nVdd %.2f V 에서 %.1f mA ≥ %.0f mA" % (lv, i * 1e3, p["short"] * 1e3))
                self.check_gate(p)
        self.wait(1.0)
        i, v = self.meas_vdd()
        if i >= p["short"]:
            self.fault("Vdd 과전류 (%.0f V 에서 %.1f mA)" % (v, i * 1e3))

    def job_power_on(self, p):
        e, n = self.inst["e"], self.inst["n"]
        self._stepped = set()
        self.set_state("seq")
        self.plan = p
        case = p.get("case", 0)
        chs = sorted(p["gp"])
        lst = "(@%s)" % ",".join(map(str, chs))
        i_leak = None
        if case == 0:
            # 1. 게이트 기본값 → 전체 ON → 읽은 값 확인
            self.step(1, "게이트 기본값 → 전체 ON")
            for ch in chs:
                n.write("VOLT %.3f,(@%d)" % (p["gp"][ch], ch))
            n.write("OUTP ON," + lst)
            self.wait(0.8)
        st = [x.strip() in ("1", "ON") for x in n.query("OUTP? " + lst).split(",")]
        vg = flist(n.query("MEAS:VOLT? " + lst))
        bad = [(c, x) for c, x, o in zip(chs, vg, st) if (not o) or x != x or abs(x - p["gp"][c]) > p["gtol"]]
        if bad:
            msg = "게이트 바이어스 이상\n\n" + "\n".join(
                "%s: 설정 %.3f V, 읽음 %s" % (CH_COLORS[c][1], p["gp"][c], ("%.3f V" % x) if x == x else "없음") for c, x in bad)
            if case <= 1:
                n.write("OUTP OFF," + lst)            # Vdd 켜기 전이므로 게이트만 바로 OFF
                self.set_state("fault")
                self.seq_status("■ 게이트 바이어스 이상 → 게이트 OFF (Vdd 는 켜지 않음)", "#ef9a9a")
                self.red(msg + "\n\n→ 게이트 바로 OFF (Vdd 는 켜지 않음)")
                raise SeqStop("게이트 이상 → 게이트 OFF")
            self.fault(msg)
        self.post("log", "게이트 확인: " + ", ".join("%s %.3f V" % (CH_COLORS[c][1], x) for c, x in zip(chs, vg)), "ok")
        self.read_job(full=True)
        if case <= 2:
            # Vdd 올리는 동안만 E36155A 전류 제한을 낮게 (쇼트면 장비가 바로 막음) → 게이트 조정 전에 원래대로
            ilim0 = fnum(e.query("CURR?"))
            lim = p.get("ramp_ilim", 0.0)
            if 0 < lim <= p["short"]:
                lim = p["short"] * 2              # 제한이 쇼트 기준보다 낮으면 쇼트를 못 알아챔 → 기준의 2배
            if lim > 0 and ilim0 == ilim0 and ilim0 > lim:
                e.write("CURR %.4f" % lim)
                self.post("log", "Vdd 올리는 동안 전류 제한 %.0f mA (원래 %.3f A)" % (lim * 1e3, ilim0), "ok")
            else:
                ilim0 = None
            try:
                self._ramp_up(p, case, e)
            finally:
                if ilim0 is not None:
                    e.write("CURR %.4f" % ilim0)
                    self.post("log", "전류 제한 원래대로 %.3f A" % ilim0, "ok")
        if case <= 3:
            self.wait(0.3)
            i_leak, v = self.meas_vdd()
            self.post("log", "누설 전류: Vdd %.2f V 에서 %.2f mA (게이트 바이어스 전)" % (v, i_leak * 1e3), "ok")
            self.read_job(full=True)
            # 4~5. CA → DRV 게이트 조정
            step_i = {}
            acc = i_leak or 0.0                   # 각자 목표 → Vdd 전체 전류 목표 = 누설 + CA (+ DRV)
            for k, (ch, own) in enumerate(p["tune"]):
                acc += own
                target = acc
                self.step(4 + k, "%s 게이트 조정 → +%.0f mA (Vdd 전체 %.0f mA)" % (CH_COLORS[ch][1], own * 1e3, target * 1e3))
                self.tune_gate(p, ch, target, "%d/6" % (4 + k))
                self.wait(0.3)
                step_i[ch] = self.meas_vdd()[0]          # 이 게이트까지 맞춘 뒤 Vdd 전체 전류
        # 6. 확인 · 기록
        i, v = self.meas_vdd()
        vg = flist(n.query("VOLT? (@1,2,3)"))
        if case <= 3:
            # I_CA = CA 맞춘 뒤 전류 − 누설, I_DRV = 전체 − CA 맞춘 뒤 전류 (Vdd 에 흐르는 전류)
            i_ca = step_i.get(2, float("nan")) - (i_leak or 0.0)
            i_drv = i - step_i.get(2, float("nan"))
            tgt = sum(t for _, t in p["tune"]) + (i_leak or 0.0)
            ok = abs(i - tgt) <= p["tol"] * tgt
            msg = "%s POWER SUPPLY ON 완료: Vdd %.2f V · Id %.1f mA (목표 %.0f mA ±%g%%)%s" % (
                "✔" if ok else "※", v, i * 1e3, tgt * 1e3, p["tol"] * 100, "" if ok else " - 오차 범위 밖, 직접 확인")
        else:
            i_ca = i_drv = None
            ok = True
            msg = "✔ 사용자가 걸어 둔 바이어스 그대로: Vdd %.2f V · Id %.1f mA (게이트 %s) → 측정" % (
                v, i * 1e3, " · ".join("%s %.3f" % (CH_COLORS[c][1], vg[c - 1]) for c in (1, 2, 3)))
        self.bias_info = dict(vdd=v, vset=fnum(e.query("VOLT?")), vg={1: vg[0], 2: vg[1], 3: vg[2]},
                              idq=i, i_ca=i_ca, i_drv=i_drv, i_leak=i_leak, case=case, t=time.time())
        self.step(6, msg + ("  → RF 측정 시작" if ok else ""), "#a5d6a7" if ok else "#ffd54f")
        self.post("log", msg, "ok" if ok else "err")
        self.mon_ref = i
        self.set_state("on")
        self.post("bias_done", msg, ok)

    def bias_snapshot(self):
        """지금 바이어스 (PA Bench Monitor 기록용). I_DRV / I_CA 는 POWER SUPPLY ON 으로 맞춘 그대로일 때만."""
        e = self.sets.get(("e", None), {})
        vg = {c: self.sets.get(("n", c), {}).get("v") for c in (1, 2, 3)}
        i_, v_, t_ = self.e_last
        out = dict(vdd=e.get("v"), vg=vg, idq=None, i_drv=None, i_ca=None)
        bi = getattr(self, "bias_info", None)
        if bi and all(vg[c] is not None and bi["vg"].get(c) == bi["vg"].get(c) and abs(vg[c] - bi["vg"][c]) < 0.0006
                      for c in (1, 2)):
            out.update(idq=bi["idq"], i_drv=bi["i_drv"], i_ca=bi["i_ca"], i_leak=bi.get("i_leak"))
        if i_ == i_ and time.time() - t_ < 5:
            out["idq_now"] = i_
        return out

    def nudge(self, k, ch, d, mult=1):
        """(PA Bench Monitor 바이어스 미세 조정) 전압을 한 칸 올림/내림"""
        (self.var_e_dmode if k == "e" else self.var_n_dmode[ch]).set("V")
        r = self.dial_turn(k, ch, d, mult)
        if r is not False:
            (self.e_dial if k == "e" else self.n_dials[ch]).angle += 18 * d
            (self.e_dial if k == "e" else self.n_dials[ch]).draw()
        return r

    def set_value(self, k, ch, val):
        (self.var_e_dmode if k == "e" else self.var_n_dmode[ch]).set("V")
        (self.var_e_val if k == "e" else self.n_entry_var[ch]).set("%s" % val)
        self.entry_set(k, ch)

    def check_gate(self, p):
        """게이트가 설정값대로인지 (순서 중)"""
        n = self.inst["n"]
        chs = sorted(p["gp"])
        lst = "(@%s)" % ",".join(map(str, chs))
        vg = flist(n.query("MEAS:VOLT? " + lst))
        vs = flist(n.query("VOLT? " + lst))
        for c, x, s_ in zip(chs, vg, vs):
            if x != x or abs(x - s_) > p["gtol"]:
                self.fault("게이트 바이어스 이상: %s 설정 %.3f V, 읽음 %s" % (CH_COLORS[c][1], s_, ("%.3f V" % x) if x == x else "없음"))

    def tune_gate(self, p, ch, target, tag):
        n = self.inst["n"]
        v = fnum(n.query("VOLT? (@%d)" % ch))
        name = CH_COLORS[ch][1]
        self.seq_status("%s %s 게이트 조정 → Vdd %.0f mA" % (tag, name, target * 1e3), "#ffd54f")
        i, _ = self.meas_vdd()
        base = i                                  # 이 게이트를 만지기 전 Vdd 전류 (누설 + 앞에서 맞춘 CA)
        own = max(target - base, 1e-6)            # 이 게이트가 올려야 할 몫
        fine_at = base + p["fine_pct"] * own      # 예: DRV 250 mA 면 DRV 몫의 80% 부터 미세 (전체 전류 기준이 아님)
        steps = 0
        while i < target:
            # CA · DRV 똑같이: 자기 몫의 fine_pct% 까지는 사용자 스텝, 그다음 미세 스텝 (예전 "30% 미만 2배" 는 없앰)
            step = p["gfine"] if i >= fine_at else p["gstep"]
            nv = round(v - step, 3)
            if p["gmin"] == p["gmin"] and nv < p["gmin"] - 1e-9:
                self.fault("%s 게이트가 최소 한계 %.3f V 에 닿았는데 Vdd %.1f mA (목표 %.0f mA 미달)" % (
                    name, p["gmin"], i * 1e3, target * 1e3))
            n.write("VOLT %.3f,(@%d)" % (nv, ch))
            v = nv
            steps += 1
            self.wait(p["gdt"])
            i = self.meas_i()                     # 빠르게: 전류만 읽음
            if steps % 3 == 0 or i >= fine_at:
                self.seq_status("%s %s 게이트 %.3f V → Vdd %.1f / %.0f mA" % (tag, name, v, i * 1e3, target * 1e3), "#ffd54f")
            if i >= p["over"] * target:
                self.fault("과전류: %s 게이트 조정 중 Vdd %.1f mA ≥ 목표 %.0f mA × %.2g" % (name, i * 1e3, target * 1e3, p["over"]))
            if steps > 5000:
                self.fault("게이트 조정 단계가 너무 많음")
        back = 0
        while i > target * (1 + p["tol"]) and back < 50:   # 넘쳤으면 미세하게 되돌림
            v = round(v + p["gfine"], 3)
            n.write("VOLT %.3f,(@%d)" % (v, ch))
            back += 1
            self.wait(p["gdt"])
            i = self.meas_i()
        # 도달 후 대기: 정해진 시간 동안 전류를 보면서 미세 스텝으로 목표 쪽으로 (±band 안이면 그대로)
        settle = p.get("settle", 0.0)
        fine_n = 0
        if settle > 0:
            t_end = time.time() + settle
            prev = None                           # (게이트, 오차) - 마지막으로 바꾸기 직전
            hold = False                          # 0.001 V 한 칸이 band 보다 커서 왔다갔다 하면 가까운 쪽에 멈춤
            while True:
                left = t_end - time.time()
                if left <= 0:
                    break
                self.wait(min(p["gdt"], max(0.05, left)))
                i = self.meas_i()
                err = i - target
                if i >= p["over"] * target:
                    self.fault("과전류: %s 게이트 미세 조정 중 Vdd %.1f mA ≥ 목표 %.0f mA × %.2g" % (name, i * 1e3, target * 1e3, p["over"]))
                band = p.get("band", 0.001)
                if prev is not None and abs(err) > band and (err > 0) != (prev[1] > 0):
                    # 목표를 사이에 두고 한 칸 넘어감 → 둘 중 목표에 더 가까운 쪽으로 정하고 멈춤
                    if abs(prev[1]) < abs(err):
                        n.write("VOLT %.3f,(@%d)" % (prev[0], ch))
                        v = prev[0]
                        fine_n += 1
                    hold, prev = True, None
                elif abs(err) > (3 * band if hold else band):      # 멈춘 뒤에는 크게 벗어날 때만 다시 맞춤
                    hold = False
                    prev = (v, err)
                    nv = round(v + p["gfine"] if err > 0 else v - p["gfine"], 3)   # 게이트 ↓ = 전류 ↑
                    if (p["gmin"] == p["gmin"] and nv < p["gmin"] - 1e-9) or (p["gmax"] == p["gmax"] and nv > p["gmax"] + 1e-9):
                        break
                    n.write("VOLT %.3f,(@%d)" % (nv, ch))
                    v = nv
                    fine_n += 1
                self.seq_status("%s %s 미세 조정 %.3f V → Vdd %.1f / %.0f mA (%.0f초 남음)" % (
                    tag, name, v, i * 1e3, target * 1e3, max(0, math.ceil(t_end - time.time()))), "#ffd54f")
        i, _ = self.meas_vdd()
        self.post("log", "%s %s 게이트 %.3f V → Vdd %.1f mA (목표 %.0f mA) 도달 (%d 스텝%s)" % (
            tag, name, v, i * 1e3, target * 1e3, steps, (", 대기 중 미세 %d번" % fine_n) if settle > 0 else ""), "ok")
        self.post("note", "%s 게이트 %.3f V → Vdd %.1f mA 도달 (목표 %.0f mA)" % (name, v, i * 1e3, target * 1e3))
        self.read_job(full=True)

    # ------------------------------------------------ POWER SUPPLY OFF
    def power_off(self, auto=False):
        """끄기: CA · DRV 게이트를 'OFF 게이트 전압' (기본 3.2 V, 샘플 상관없음) 까지 올림 → Vdd 0 V → OFF → 게이트 OFF.
        auto=True: 측정이 끝나서 자동으로 끄는 경우 (확인 창 없음, RF 는 먼저 꺼 둠)"""
        if self.seq_running:
            if not auto:
                messagebox.showwarning(APP, "전원 순서가 진행 중입니다.")
            return False
        if self.inst["e"] is None or self.inst["n"] is None:
            if not auto:
                messagebox.showwarning(APP, "E36155A · N6705C 를 먼저 연결하세요.")
            return False
        if not auto and self.all_off():
            self.pop_info("이미 꺼져 있습니다", "Vdd · 게이트 모두 OFF")
            return False
        og = fnum(self.sq["off_g"].get())
        lo, hi = self.lim_val("g_min"), self.lim_val("g_max")
        if og != og or (lo == lo and og < lo - 1e-9) or (hi == hi and og > hi + 1e-9):
            messagebox.showwarning(APP, "OFF 게이트 전압 %s V 를 확인하세요 (안전 한계 %s ~ %s V)." % (
                self.sq["off_g"].get(), self.lim["g_min"].get(), self.lim["g_max"].get()))
            return False
        pk = self.sets.get(("n", 3), {}).get("v")
        gp = {1: round(og, 3), 2: round(og, 3), 3: round(pk, 3) if pk is not None else round(og, 3)}
        if not auto and self.rf_guard is not None and self.rf_guard() is False:
            return False
        p = self.plan_common(gp)
        p["gp_safe"] = dict(gp)
        self._busy_kind = "off"
        self.pop_open("off", ["CA · DRV 게이트 → 기본값", "Vdd 내리는 중", "Vdd 0 V 대기 → Vdd OFF", "게이트 OFF · 완료"],
                      ["%.2f V" % og, "→ 0 V", "", ""], sub="RF OFF 확인 → 순서대로 끄는 중", color="#ef6c00")
        self.start_seq(self.job_power_off, p)
        return True

    def job_power_off(self, p):
        e, n = self.inst["e"], self.inst["n"]
        self.set_state("seq")
        self.plan = p
        for k, ch in enumerate((2, 1)):
            self.pause("OFF %d번 %s 게이트 → %.3f V 까지 올림" % (k + 1, CH_COLORS[ch][1], p["gp"][ch]))
            self.seq_status("%d/6 %s 게이트 → %.3f V" % (k + 1, CH_COLORS[ch][1], p["gp"][ch]), "#ffd54f")
            self.raise_gate(ch, p["gp"][ch], p)
        self.pause("OFF 3번 Vdd 전류 없는지 확인")
        self.wait(1.0)
        i, v = self.meas_vdd()
        if i >= p["short"]:
            self.red("Vdd 전류가 남아 있습니다: CA · DRV 게이트 %.3f V 인데 Vdd %.1f mA (≥ %.0f mA)\n→ 그래도 Vdd 를 내립니다" % (
                p["gp"][2], i * 1e3, p["short"] * 1e3))
        else:
            self.post("log", "3/6 게이트 올림, Vdd %.2f mA - 전류 없음" % (i * 1e3), "ok")
        self.pause("OFF 4번 Vdd %g V 씩 0 V 까지 내림" % p["vstep"])
        self.ramp_vdd_down(p["vstep"], p["vdt"], "4/6")
        self.pause("OFF 5번 0 V 에서 %g 초 대기 → Vdd OFF" % p["w1"])
        self.seq_status("5/6 Vdd 0 V 에서 %g 초 대기" % p["w1"], "#ffd54f")
        self.wait(p["w1"])
        e.write("OUTP OFF")
        self.seq_status("5/6 Vdd OFF → %g 초 뒤 게이트 OFF" % p["w2"], "#ffd54f")
        self.wait(p["w2"])
        self.pause("OFF 6번 게이트 전체 OFF")
        n.write("OUTP OFF,(@1,2,3)")
        self.wait(0.2)
        self.set_state("off")
        self.seq_status("✔ POWER SUPPLY OFF 완료 (CA · DRV 게이트 %.2f V → Vdd 0 V → Vdd OFF → 게이트 OFF)" % p["gp"][2], "#a5d6a7")
        self.post("log", "POWER SUPPLY OFF 완료", "ok")

    def raise_gate(self, ch, target, p, fast=False):
        """게이트를 target 까지 올림 (전류 차단 쪽). 이미 더 높으면 그대로 (내리면 전류가 늘어남). fast=True 면 한 번에"""
        n = self.inst["n"]
        v = fnum(n.query("VOLT? (@%d)" % ch))
        if v == v and v >= target - 1e-9:
            return
        if fast or v != v:
            n.write("VOLT %.3f,(@%d)" % (target, ch))
            return
        while v < target - 1e-9:
            v = min(round(v + max(p["gstep"], 0.01), 3), target)
            n.write("VOLT %.3f,(@%d)" % (v, ch))
            self.wait(min(p["gdt"], 0.1), stoppable=False)

    def ramp_vdd_down(self, step, dt, tag):
        e = self.inst["e"]
        lv = fnum(e.query("VOLT?"))
        while lv == lv and lv > 1e-9:
            lv = max(round(lv - step, 3), 0.0)
            if self.seq_running and not getattr(self, "_fast_down", False):
                self.pause("Vdd → %.1f V (내림)" % lv, fine=True)
            e.write("VOLT %.3f" % lv)
            self.seq_status("%s Vdd 내리는 중 %.1f V" % (tag, lv), "#ffd54f")
            self.wait(dt, stoppable=False)
        self.meas_vdd()

    # ------------------------------------------------ 이상 → 게이트 기본값 → Vdd 0 V (항상 이 순서)
    def fault(self, msg):
        """순서 중 이상: 빨간 팝업 + 안전하게 내림 (호출한 순서는 끝남)"""
        self.red(msg + "\n\n→ 게이트 기본값으로 올리고 Vdd 를 0 V 까지 빠르게 내립니다.")
        self.safe_down(msg)
        raise SeqStop("")

    def safe_down(self, why):
        self._fast_down = True
        try:
            self._safe_down(why)
        finally:
            self._fast_down = False

    def _safe_down(self, why):
        """게이트 기본값으로 빠르게 → 기본값 확인 → Vdd 0 V 까지 빠르게 한 단계씩"""
        self.set_state("fault")
        if self.on_fault:
            self.post("call", lambda: self.on_fault(why))      # (Bench) 자동 측정 중지 · RF OFF
        e, n = self.inst["e"], self.inst["n"]
        pl = getattr(self, "plan", None) or {}
        gp = pl.get("gp_safe") or pl.get("gp") or {}
        if not gp:
            try:
                gp = {c: round(float(self.sq["g%d" % c].get()), 3) for c in (1, 2, 3)}
            except ValueError:
                gp = {}
        try:
            if n is not None and gp:
                self.seq_status("이상 → 게이트 기본값으로", "#ef9a9a")
                cur = flist(n.query("VOLT? (@1,2,3)"))
                for ch in (2, 1, 3):
                    if ch in gp:
                        c_ = cur[ch - 1] if len(cur) >= ch else float("nan")
                        if c_ == c_ and c_ >= gp[ch] - 1e-9:
                            continue                 # 이미 더 높음 (더 차단) → 내리지 않음
                        n.write("VOLT %.3f,(@%d)" % (gp[ch], ch))
                time.sleep(0.3)
                vs = flist(n.query("VOLT? (@1,2,3)"))
                self.post("log", "이상 처리: 게이트 기본값 확인 %s" % ", ".join("%.3f" % x for x in vs), "err")
        except Exception as ex:
            self.post("log", "이상 처리: 게이트 명령 실패 %s" % ex, "err")
        try:
            if e is not None:
                self.ramp_vdd_down(1.0, 0.05, "이상 →")
        except Exception as ex:
            try:
                e.write("VOLT 0")
            except Exception:
                pass
            self.post("log", "이상 처리: Vdd 내리기 실패 %s → VOLT 0" % ex, "err")
        self.seq_status("■ 이상 처리 완료: 게이트 기본값 · Vdd 0 V  (원인 확인 후 [OFF] 또는 [ON])", "#ef9a9a")
        self.post("log", "이상 처리 완료 (게이트 기본값 → Vdd 0 V): " + why.replace("\n", " "), "err")

    def check_order(self):
        """장비 앞면에서 직접 켜도: Vdd 가 켜져 있는데 게이트가 꺼져 있으면 바로 경고 + Vdd OFF"""
        if self.seq_running or self.inst["e"] is None or self.inst["n"] is None:
            return
        vdd_on = self.sets.get(("e", None), {}).get("o")
        g_need = [c for c in (1, 2, 3) if c in self.read_chs and ("n", c) in self.sets]
        g_off = [c for c in g_need if not self.sets[("n", c)].get("o")]
        bad = bool(vdd_on and g_off and len(g_need) == len([c for c in (1, 2, 3) if c in self.read_chs]))
        if bad and not getattr(self, "_order_alarm", False):
            self._order_alarm = True
            self.red_popup("순서 이상: 게이트 %s 가 꺼져 있는데 Vdd 가 켜져 있습니다!\n\n"
                           "→ Vdd 를 바로 0 V · OFF 했습니다. 항상 게이트 먼저 켜고 Vdd 를 켜세요." % (
                               ", ".join("CH%d %s" % (c, CH_COLORS[c][1]) for c in g_off)))
            e = self.inst["e"]

            def job():
                try:
                    e.write("VOLT 0")
                    e.write("OUTP OFF")
                finally:
                    self.read_job(full=True)
            self.worker.tasks.put(job)
            if self.on_fault:
                self.on_fault("순서 이상: 게이트 OFF 인데 Vdd ON")
        elif not bad:
            self._order_alarm = False

    def check_faults(self, kind, vals):
        """POWER SUPPLY ON 상태에서 읽을 때마다 (UI 쓰레드): 급상승 · 최대 전류 · 게이트 이상"""
        if self.bias_state != "on" or self.seq_running or getattr(self, "fault_busy", False):
            return
        p = getattr(self, "plan", None) or {}
        why = None
        if kind == "e":
            i = vals.get("im")
            if i is None or i != i:
                return
            ref = getattr(self, "mon_ref", i)
            pre = self.lim_prefix()                        # 지금 측정 종류 (Pulse / Mod. / DPD) 의 한계값
            pct = self.sqf(pre + "spike_pct", self.sqf("spike_pct", 50)) / 100.0
            dma = self.sqf(pre + "spike_ma", self.sqf("spike_ma", 100)) / 1e3
            imax = self.sqf(pre + "imax", self.sqf("imax", 1.5))
            tag = {"": "", "mod_": " (Mod. 한계)", "dpd_": " (DPD 한계)"}.get(pre, "")
            if i > imax:
                why = "Vdd 전류가 최대 %.2f A 를 넘음 (%.3f A)%s" % (imax, i, tag)
            elif i - ref >= dma and i >= ref * (1 + pct):
                why = "Vdd 전류 급상승: %.1f → %.1f mA%s" % (ref * 1e3, i * 1e3, tag)
            self.mon_ref = i if why is None else ref
        elif kind == "n":
            gtol, gimax = self.sqf("gtol", 0.05), self.sqf("gimax", 5) / 1e3
            for c, v in vals.items():
                cur = self.sets.get(("n", c), {})
                if c not in (p.get("gp") or {}):
                    continue
                if "o" in v and v["o"] not in ("1", "ON"):
                    why = "게이트 바이어스 이상: %s 출력이 꺼짐" % CH_COLORS[c][1]
                elif (v.get("vs", cur.get("v")) is not None) and abs(v["vm"] - v.get("vs", cur.get("v"))) > gtol and time.time() - self.edit_t.get(("n", c), 0) > 2:
                    why = "게이트 바이어스 이상: %s 설정 %.3f V, 읽음 %.3f V" % (CH_COLORS[c][1], v.get("vs", cur.get("v")), v["vm"])
                elif abs(v["im"]) > gimax:
                    why = "게이트 전류 이상: %s %.2f mA (> %.1f mA)" % (CH_COLORS[c][1], v["im"] * 1e3, gimax * 1e3)
                if why:
                    break
        if why:
            self.fault_busy = True
            self.red(why + "\n\n→ 게이트 기본값으로 올리고 Vdd 를 0 V 까지 빠르게 내립니다.")

            def job():
                try:
                    self.safe_down(why)
                finally:
                    self.fault_busy = False
                    self.read_job(full=True)
            self.worker.tasks.put(job)

    # ------------------------------------------------ 긴급 정지
    def seq_emergency(self):
        """진행 중이면 중지 → 게이트 기본값 → Vdd 0 V. 아니면 확인 후 같은 순서."""
        if self.seq_running:
            self.seq_abort = "emergency"
            self.seq_status("■ 긴급 정지 요청 → 게이트 기본값 → Vdd 0 V", "#ef9a9a")
            return
        if self.inst["e"] is None and self.inst["n"] is None:
            return
        if not messagebox.askyesno(APP, "긴급 정지: 게이트 기본값으로 올리고 → Vdd 0 V 까지 빠르게 내립니다.\n\n진행할까요?"):
            return
        self.worker.tasks.put(lambda: self.safe_down("긴급 정지 (사용자)"))

    def run_seq(self, fn, plan):
        try:
            fn(plan)
        except SeqStop as st:
            if self.seq_abort == "emergency":
                self.safe_down("긴급 정지 (사용자)")
            elif self.seq_abort:
                self.seq_status("■ 중지됨 (지금 상태 그대로 - 다시 [ON] 하면 지금 상태에서 이어서)", "#ef9a9a")
                self.set_state("off")
        except Exception as ex:
            self.post("log", "전원 순서 오류: %s" % ex, "err")
            self.red("전원 순서 중 통신 오류: %s" % ex)
            try:
                self.safe_down("통신 오류: %s" % ex)
            except Exception:
                pass
        finally:
            self.seq_running = False
            self.read_job(full=True)
            if getattr(self, "was_polling", False) or self.embedded:
                self.polling = True
            self.post("call", self.seq_finished)

    def seq_finished(self):
        """순서가 끝나면 진행 창: 완료 → 몇 초 뒤 저절로 닫힘 · 이상 · 중지면 바로 닫음 (빨간 경고가 따로 뜸)"""
        kind = getattr(self, "_busy_kind", None)
        self._busy_kind = None
        if getattr(self, "_busy", None) is None or kind is None:
            return
        txt = self.lbl_seq.cget("text")
        if self.bias_state == "fault" or "중지" in txt:
            self.busy_close()
        else:
            self.pop_done(kind, ok="※" not in txt[:10])

    def wait(self, sec, stoppable=True):
        t_end = time.time() + sec
        while time.time() < t_end:
            if stoppable and self.seq_abort:
                raise SeqStop("사용자 중지")
            time.sleep(0.02)

    def meas_i(self):
        """Vdd 전류만 빠르게 (순서 중 반복용)"""
        i = fnum(self.inst["e"].query("MEAS:CURR?"))
        self.post("e", {"vm": self.e_last[1], "im": i})
        return i

    def meas_vdd(self):
        e = self.inst["e"]
        i = fnum(e.query("MEAS:CURR?"))
        v = fnum(e.query("MEAS:VOLT?"))
        self.post("e", {"vm": v, "im": i})
        return i, v

    # ------------------------------------------------ 연결 도우미 (PA Bench Monitor 용)
    def connect_all(self):
        for k, var in (("e", self.var_e), ("n", self.var_n)):
            if self.inst[k] is None and var.get().strip():
                self.connect(k)

    def vdd_on(self):
        return bool(self.sets.get(("e", None), {}).get("o"))

    def shutdown(self):
        self.polling = False
        self.seq_abort = True
        self.worker.alive = False
        for k in ("e", "n"):
            try:
                if self.inst[k] is not None:
                    self.inst[k].close()
            except Exception:
                pass

    # ------------------------------------------------ 동작
    def init_visa(self):
        def job():
            ok = self.visa.open_rm()
            self.post("visa", ok, self.visa.backend if ok else self.visa.err)
        self.worker.tasks.put(job)

    def scan(self):
        def job():
            if self.visa.rm is None and not self.visa.open_rm():
                self.post("log", self.visa.err, "err")
                self.post("found", [("DEMO::E36155A", "(장비 없이 시험)"), ("DEMO::N6705C", "(장비 없이 시험)")])
                return
            res = self.visa.list()
            self.post("log", "찾은 주소 %d개: %s" % (len(res), ", ".join(res) or "없음"), "ok" if res else "err")
            out = []
            for r in res:
                if r.upper().startswith("ASRL"):          # COM 포트는 건너뜀 (응답 없이 오래 걸림)
                    out.append((r, "(시리얼 포트 - 건너뜀)"))
                    continue
                try:
                    inst = self.visa.open(r, 1500)
                    idn = inst.query("*IDN?").strip()
                    inst.close()
                except Exception as e:
                    idn = "응답 없음 (%s)" % str(e)[:60]
                out.append((r, idn))
            out += [("DEMO::E36155A", "(장비 없이 시험)"), ("DEMO::N6705C", "(장비 없이 시험)")]
            self.post("found", out)
        self.post("log", "장비 찾는 중... (GPIB 는 몇 초 걸릴 수 있음)")
        self.worker.tasks.put(job)

    def pick_found(self, event=None):
        it = self.found.focus()
        if not it:
            return
        r, idn = self.found.item(it, "values")
        if "N67" in idn.upper() or "N67" in r.upper():
            self.var_n.set(r)
        elif "E361" in idn.upper() or "E361" in r.upper():
            self.var_e.set(r)
        else:
            self.var_n.set(r) if messagebox.askyesno(APP, "%s\n\nN6705C 칸에 넣을까요? (아니요 = E36155A 칸)" % idn) \
                else self.var_e.set(r)

    def connect(self, k):
        if self.inst[k] is not None:
            def job_close():
                try:
                    self.inst[k].close()
                except Exception:
                    pass
                self.inst[k] = None
                self.post("conn", k, False, "")
            self.worker.tasks.put(job_close)
            return
        var = self.var_e if k == "e" else self.var_n
        res = fix_addr(var.get())
        if res != var.get().strip():
            var.set(res)
            self.post("log", "주소 자동 수정: %s" % res)
        to = int(fnum(self.var_to.get()) or 3000)

        def job():
            try:
                inst = self.visa.open(res, to)
                idn = inst.query("*IDN?").strip()
            except Exception as e:
                self.post("log", "%s 연결 실패 (%s): %s" % ("E36155A" if k == "e" else "N6705C", res, e), "err")
                self.post("conn", k, False, "연결 실패: %s" % str(e)[:120])
                return
            self.inst[k] = inst
            want = "E361" if k == "e" else "N67"
            warn = "" if want in idn.upper() else "   ※ 예상한 장비가 아님 - 주소 확인"
            self.post("conn", k, True, idn + warn)
            if self.embedded and not self.polling:
                self.post("call", self.toggle_poll)          # Bench 안에서는 연결하면 바로 계속 읽기
            self.post("log", "%s 연결: %s → %s" % ("E36155A" if k == "e" else "N6705C", res, idn), "ok")
            if k == "n":
                try:
                    mods = inst.query("SYST:CHAN:MOD? (@1:4)").strip()
                except Exception:
                    try:
                        mods = ",".join(inst.query("SYST:CHAN:MOD? (@%d)" % c).strip() for c in range(1, 5))
                    except Exception as e:
                        mods = ""
                        self.post("log", "N6705C 모듈 읽기 실패: %s" % e, "err")
                self.post("mods", [m.strip().strip('"') for m in mods.split(",")] if mods else [])
            self.read_job()
        self.post("log", "연결 중... %s" % res)
        self.worker.tasks.put(job)

    # ------------------------------------------------ 제어 (출력 / 다이얼)
    def on_ctrl_toggle(self):
        if self.var_ctrl.get() and self.lock_reason():
            self.var_ctrl.set(False)
            self.root.after(10, lambda: self.locked())
        on = self.var_ctrl.get()
        lk = bool(self.lock_reason())
        self.chk_ctrl.configure(text=("🔒 바이어스 기본값 없음 - 원격 제어 잠금" if lk else
                                      "🔓 제어 사용 중 (체크 해제 = 잠금)" if on else "🔒 제어 잠금 (체크하면 출력 · 다이얼 사용)"),
                                fg="#c62828" if (on or lk) else "#555", state="disabled" if lk else "normal")
        for d in [self.e_dial] + list(self.n_dials.values()):
            d.set_enabled(on)

    def store_set(self, key, v):
        o = v["o"] in ("1", "ON")
        cur = self.sets.setdefault(key, {})
        cur["o"] = o
        if time.time() - self.edit_t.get(key, 0) > 1.5 and not self.pending.get(key):
            cur["v"], cur["i"] = v["vs"], v["is_"]

    def lim_val(self, k):
        return fnum(self.lim[k].get())

    def step_of(self, k, mode):
        return 0.1 if k == "e" else 0.001        # Vdd 0.1 V/A, 게이트 0.001 V/A

    def dial_mode(self, k, ch):
        mode = (self.var_e_dmode if k == "e" else self.var_n_dmode[ch]).get()
        st = self.step_of(k, mode)
        txt = "휠 ±%g %s" % (st, "V" if mode == "V" else "A")
        (self.lbl_e_step if k == "e" else self.lbl_n_step[ch]).configure(text=txt)
        self.refresh_entry(k, ch, force=True)

    def dial_turn(self, k, ch, d, mult):
        if not self.var_ctrl.get():
            self.post("log", "제어가 잠겨 있습니다 - 아래 '🔒 제어 잠금' 을 체크하세요.", "err")
            return False
        if self.inst[k] is None:
            self.post("log", "%s 가 연결되지 않았습니다." % ("E36155A" if k == "e" else "N6705C"), "err")
            return False
        key = (k, ch)
        cur = self.sets.get(key)
        mode = (self.var_e_dmode if k == "e" else self.var_n_dmode[ch]).get()
        fld = "v" if mode == "V" else "i"
        if not cur or cur.get(fld) is None or cur[fld] != cur[fld]:
            self.post("log", "먼저 [한 번 읽기] 로 지금 설정값을 읽으세요.", "err")
            return False
        st = self.step_of(k, mode) * mult
        nd = 1 if k == "e" else 3
        new = round(cur[fld] + d * st, nd)
        # 안전 한계
        if k == "e":
            lo, hi = (0.0, self.lim_val("vdd_max")) if fld == "v" else (0.0, self.lim_val("vdd_imax"))
        else:
            lo, hi = (self.lim_val("g_min"), self.lim_val("g_max")) if fld == "v" else (0.0, self.lim_val("g_imax"))
        def outside(x):
            return max((lo - x) if lo == lo else 0.0, (x - hi) if hi == hi else 0.0)
        if outside(new) > 1e-9 and outside(new) >= outside(cur[fld]) - 1e-12:
            # 한계 밖으로 나가는 쪽은 막음 (밖에 있던 값을 한계 쪽으로 되돌리는 건 허용)
            self.post("log", "안전 한계 %s ~ %s 밖이라 멈춤 (%s %+.*f)" % (lo, hi, "전압" if fld == "v" else "전류", nd, new), "err")
            return False
        cur[fld] = new
        self.edit_t[key] = time.time()
        self.pending.setdefault(key, {})[fld] = new
        self.show_set(k, ch)
        if not getattr(self, "_flush_job", None):
            self._flush_job = self.root.after(150, self.flush)   # 휠을 빠르게 돌려도 마지막 값만 보냄
        return True

    def mode_of(self, k, ch):
        return (self.var_e_dmode if k == "e" else self.var_n_dmode[ch]).get()

    def refresh_entry(self, k, ch, force=False):
        """On/Off 아래 칸: 지금 조정 중인 값 (전압 또는 전류 제한)"""
        ent = self.e_entry if k == "e" else self.n_entry[ch]
        var = self.var_e_val if k == "e" else self.n_entry_var[ch]
        if not force and self.root.focus_get() is ent:
            return                      # 직접 입력 중이면 덮지 않음
        cur = self.sets.get((k, ch), {})
        fld = "v" if self.mode_of(k, ch) == "V" else "i"
        val = cur.get(fld)
        if val is None or val != val:
            var.set("")
            return
        nd = 1 if k == "e" else 3
        var.set(("%.*f %s" % (nd, val, "V" if fld == "v" else "A")))

    def key_turn(self, k, ch, d, e):
        self.dial_turn(k, ch, d, 10 if (e.state & 0x0001) else 1)
        self.refresh_entry(k, ch, force=True)
        return "break"

    def entry_set(self, k, ch):
        """칸에 값을 직접 쓰고 Enter → 그 값으로 (안전 한계 확인)"""
        if self.locked():
            return
        var = self.var_e_val if k == "e" else self.n_entry_var[ch]
        txt = var.get().replace("V", "").replace("A", "").replace("v", "").replace("a", "").strip()
        try:
            val = float(txt)
        except ValueError:
            self.post("log", "숫자를 입력하세요: %s" % var.get(), "err")
            self.refresh_entry(k, ch, force=True)
            return
        cur = self.sets.get((k, ch))
        fld = "v" if self.mode_of(k, ch) == "V" else "i"
        if not cur or cur.get(fld) is None:
            self.post("log", "먼저 [한 번 읽기] 로 지금 설정값을 읽으세요.", "err")
            return
        st = self.step_of(k, self.mode_of(k, ch))
        d = round((val - cur[fld]) / st)
        if d == 0:
            self.refresh_entry(k, ch, force=True)
            return
        big = abs(val - cur[fld]) > (2.0 if k == "e" else 0.2) and fld == "v"
        if big and not messagebox.askyesno(APP, "%s 을(를) %.3f → %.3f 로 한 번에 바꿀까요?" % (
                "Vdd" if k == "e" else "게이트 CH%d" % ch, cur[fld], val)):
            self.refresh_entry(k, ch, force=True)
            return
        if self.dial_turn(k, ch, 1 if d > 0 else -1, abs(d)) is not False:
            (self.e_dial if k == "e" else self.n_dials[ch]).focus_set()
            self.refresh_entry(k, ch, force=True)

    def show_set(self, k, ch):
        self.refresh_entry(k, ch, force=True)
        cur = self.sets.get((k, ch), {})
        if k == "e":
            self.e_vals["vs"].configure(text="%.3f V" % cur.get("v", float("nan")), fg="#ffd54a")
            self.e_vals["is"].configure(text=self.fmt_a(cur.get("i", float("nan"))), fg="#ffd54a")
        else:
            row = self.n_vals[ch]
            row["vs"].configure(text="Set %+.3f V" % cur.get("v", float("nan")), fg=CH_COLORS[ch][0])
            row["is"].configure(text="Lim %s" % self.fmt_a(cur.get("i", float("nan"))), fg=CH_COLORS[ch][0])

    def flush(self):
        self._flush_job = None
        items = [(key, dict(p)) for key, p in self.pending.items() if p]
        self.pending.clear()
        if not items:
            return

        def job():
            for (k, ch), p in items:
                inst = self.inst[k]
                if inst is None:
                    continue
                sfx = "" if k == "e" else ",(@%d)" % ch
                for fld, val in p.items():
                    cmd = "%s %.*f%s" % ("VOLT" if fld == "v" else "CURR", 1 if k == "e" else 3, val, sfx)
                    inst.write(cmd)
                    err = inst.query("SYST:ERR?").strip()
                    ok = err.startswith(("+0", "0"))
                    self.post("log", "%s ← %s%s" % ("E36155A" if k == "e" else "N6705C CH%d" % ch, cmd,
                                                    "" if ok else "   ✖ 오류: " + err), "ok" if ok else "err")
            self.read_job()
        self.worker.tasks.put(job)

    def gate_in_range(self, c):
        v = self.sets.get(("n", c), {}).get("v")
        lo, hi = self.lim_val("g_min"), self.lim_val("g_max")
        return v is not None and v == v and (lo != lo or v >= lo - 1e-9) and (hi != hi or v <= hi + 1e-9)

    def gate_all(self, on, _retry=False):
        """게이트 채널(읽을 채널로 체크한 것) 한 번에 ON / OFF"""
        self.post("log", "게이트 전체 %s 버튼 누름" % ("ON" if on else "OFF"))
        if self.locked():
            return
        if not on and self.sets.get(("e", None), {}).get("o"):
            self.red_popup("Vdd 가 켜져 있는데 게이트를 끄려고 했습니다 → 막음\n\n"
                           "끌 때는 항상 Vdd 를 먼저 끄고 (0 V → OFF) 게이트를 끄세요. ([OFF] 버튼이 이 순서로 진행)")
            return
        if not self.var_ctrl.get():
            if not messagebox.askyesno(APP, "제어가 잠겨 있습니다 (🔒).\n\n잠금을 풀고 계속할까요?"):
                return
            self.var_ctrl.set(True)
            self.on_ctrl_toggle()
        n = self.inst["n"]
        if n is None:
            messagebox.showwarning(APP, "N6705C 가 연결되지 않았습니다. 먼저 [연결] 하세요.")
            return
        chs = list(self.read_chs)
        if not chs:
            messagebox.showwarning(APP, "읽을 채널(1~4) 중 하나 이상 체크하세요.")
            return
        if any(("n", c) not in self.sets or "o" not in self.sets[("n", c)] for c in chs) or \
                (self.inst["e"] is not None and ("e", None) not in self.sets):
            if _retry:
                messagebox.showwarning(APP, "지금 상태를 읽지 못했습니다. 기록 창의 오류를 확인하세요.")
                return

            def job():
                self.read_job(full=True)
                self.post("call", lambda: self.gate_all(on, _retry=True))
            self.worker.tasks.put(job)
            return
        lines = []
        for c in chs:
            cur = self.sets[("n", c)]
            lines.append("   CH%d %s : %+.3f V,  제한 %s%s" % (c, CH_COLORS[c][1], cur.get("v", float("nan")),
                                                         self.fmt_a(cur.get("i", float("nan"))),
                                                         "" if self.gate_in_range(c) else "   ⚠ 한계 밖"))
        msg = "게이트 CH %s 를 한 번에 %s 할까요?\n\n%s" % (",".join(map(str, chs)), "ON" if on else "OFF", "\n".join(lines))
        if self.var_seq.get():
            vdd_on = self.sets.get(("e", None), {}).get("o")
            if not on and vdd_on:
                msg += "\n\n⚠ Vdd 가 켜져 있습니다! 게이트를 먼저 끄면 소자가 손상될 수 있습니다.\n   Vdd 를 먼저 끄세요."
            bad = [c for c in chs if not self.gate_in_range(c)]
            if on and bad:
                msg += "\n\n⚠ 안전 한계(%s ~ %s V) 밖인 채널: %s" % (
                    self.lim["g_min"].get(), self.lim["g_max"].get(), ", ".join("CH%d" % c for c in bad))
        warn = "⚠" in msg
        if not (messagebox.askyesno if warn else messagebox.askokcancel)(APP, msg + ("\n\n그래도 진행할까요?" if warn else "")):
            return
        lst = "(@%s)" % ",".join(map(str, chs))
        cmd = "OUTP %s,%s" % ("ON" if on else "OFF", lst)

        def job():
            n.write(cmd)
            err = n.query("SYST:ERR?").strip()
            ok = err.startswith(("+0", "0"))
            self.post("log", "N6705C ← %s%s" % (cmd, "" if ok else "   ✖ 오류: " + err), "ok" if ok else "err")
            time.sleep(0.05)
            st = [x.strip() in ("1", "ON") for x in n.query("OUTP? " + lst).strip().split(",")]
            badc = [c for c, x in zip(chs, st) if x != on]
            if badc:
                self.post("log", "✖ 출력이 바뀌지 않은 채널: %s (장비 화면의 보호 · 인히빗 확인)" %
                          ", ".join("CH%d" % c for c in badc), "err")
            else:
                self.post("log", "✔ 게이트 CH %s 출력 %s 확인" % (",".join(map(str, chs)), "ON" if on else "OFF"), "ok")
            self.read_job(full=True)
        self.worker.tasks.put(job)

    def toggle_out(self, k, ch, _retry=False):
        name0 = "E36155A" if k == "e" else "N6705C CH%d" % ch
        self.post("log", "%s On/Off 버튼 누름" % name0)
        if self.locked():
            return
        if not self.var_ctrl.get():
            if not messagebox.askyesno(APP, "제어가 잠겨 있습니다 (🔒).\n\n잠금을 풀고 계속할까요?"):
                return
            self.var_ctrl.set(True)
            self.on_ctrl_toggle()
        inst = self.inst[k]
        if inst is None:
            messagebox.showwarning(APP, "%s 가 연결되지 않았습니다. 먼저 [연결] 하세요." % ("E36155A" if k == "e" else "N6705C"))
            return
        if k == "n" and ch not in self.read_chs:
            self.ch_on[ch].set(True)
            self.dim_channels()
        cur = self.sets.get((k, ch))
        if not cur or "o" not in cur:
            if _retry:
                messagebox.showwarning(APP, "%s 의 지금 출력 상태를 읽지 못했습니다. 기록 창의 오류를 확인하세요." % name0)
                return

            def job():
                self.read_job(full=True)
                self.post("call", lambda: self.toggle_out(k, ch, _retry=True))
            self.worker.tasks.put(job)      # 지금 상태를 먼저 읽고 다시
            return
        turn_on = not cur.get("o")
        name = "Vdd (E36155A)" if k == "e" else "게이트 CH%d (N6705C %s)" % (ch, CH_COLORS[ch][1])
        # 순서: 켤 때 게이트 먼저 → Vdd, 끌 때 Vdd 먼저 → 게이트 (어기면 막고 경고)
        g_need = [c for c in (1, 2, 3) if c in self.read_chs]
        g_off = [c for c in g_need if not self.sets.get(("n", c), {}).get("o")]
        if k == "e" and turn_on and (self.inst["n"] is None or g_off):
            self.red_popup("게이트 바이어스를 먼저 켜야 합니다 → Vdd ON 막음\n\n꺼져 있는 게이트: %s\n"
                           "켤 때는 항상 게이트 (DRV · CA · PK) 먼저 → Vdd 순서입니다. ([ON] 버튼이 이 순서로 진행)" % (
                               ", ".join("CH%d %s" % (c, CH_COLORS[c][1]) for c in g_off) or "N6705C 연결 안 됨"))
            return
        if k == "n" and not turn_on and self.sets.get(("e", None), {}).get("o"):
            self.red_popup("Vdd 가 켜져 있는데 게이트 CH%d %s 를 끄려고 했습니다 → 막음\n\n"
                           "끌 때는 항상 Vdd 를 먼저 끄세요." % (ch, CH_COLORS[ch][1]))
            return
        msg = "%s 출력을 %s 할까요?\n\n설정: %+.3f V,  전류 제한 %s" % (name, "ON" if turn_on else "OFF",
                                                              cur.get("v", float("nan")), self.fmt_a(cur.get("i", float("nan"))))
        if self.var_seq.get():
            gates = [c for c in range(1, 5) if self.ch_on[c].get()]
            g_off = [c for c in gates if not self.sets.get(("n", c), {}).get("o")]
            vdd_on = self.sets.get(("e", None), {}).get("o")
            if k == "e" and turn_on and (self.inst["n"] is None or g_off):
                msg += "\n\n⚠ 게이트 바이어스가 아직 OFF 인 채널이 있습니다: %s\n   GaN 은 게이트를 먼저 켜고 Vdd 를 켜야 합니다!" % (
                    ", ".join("CH%d" % c for c in g_off) or "N6705C 연결 안 됨")
            if k == "n" and not turn_on and vdd_on:
                msg += "\n\n⚠ Vdd 가 켜져 있습니다! 게이트를 먼저 끄면 소자가 손상될 수 있습니다.\n   Vdd 를 먼저 끄세요."
            if k == "n" and turn_on and not self.gate_in_range(ch):
                msg += "\n\n⚠ 게이트 설정 %+.3f V 가 안전 한계 %s ~ %s V 밖입니다." % (
                    cur.get("v", float("nan")), self.lim["g_min"].get(), self.lim["g_max"].get())
        warn = "⚠" in msg
        if not (messagebox.askokcancel if not warn else messagebox.askyesno)(APP, msg + ("\n\n그래도 진행할까요?" if warn else "")):
            return
        cmd = "OUTP %s%s" % ("ON" if turn_on else "OFF", "" if k == "e" else ",(@%d)" % ch)

        def job():
            inst.write(cmd)
            err = inst.query("SYST:ERR?").strip()
            ok = err.startswith(("+0", "0"))
            self.post("log", "%s ← %s%s" % (name, cmd, "" if ok else "   ✖ 오류: " + err), "ok" if ok else "err")
            time.sleep(0.05)
            q = "OUTP?" if k == "e" else "OUTP? (@%d)" % ch
            st = inst.query(q).strip()
            now_on = st.split(",")[0].strip() in ("1", "ON")
            if now_on != turn_on:
                hint = ""
                try:
                    if k == "n":
                        cond = inst.query("STAT:QUES:COND? (@%d)" % ch).strip()
                        hint = " (보호/인히빗 상태 코드 %s - 장비 화면 확인, 보호 해제 OUTP:PROT:CLE (@%d))" % (cond, ch)
                    else:
                        cond = inst.query("STAT:QUES:COND?").strip()
                        hint = " (상태 코드 %s - 장비 화면의 보호(OVP/OCP) · 인히빗 확인)" % cond
                except Exception:
                    pass
                self.post("log", "✖ %s 출력이 바뀌지 않음: 지금 %s%s" % (name, "ON" if now_on else "OFF", hint), "err")
            else:
                self.post("log", "✔ %s 출력 %s 확인" % (name, "ON" if now_on else "OFF"), "ok")
            self.read_job(full=True)
        self.worker.tasks.put(job)

    def dim_channels(self):
        """체크 안 한 채널은 흐리게"""
        self.read_chs = [c for c in range(1, 5) if self.ch_on[c].get()]
        self.force_full = True
        if not hasattr(self, "n_cells"):
            return
        for ch, cell in self.n_cells.items():
            on = self.ch_on[ch].get()
            col = CH_COLORS[ch][0] if on else "#333"
            cell.configure(highlightbackground=col)
            for k in ("vm", "im"):
                self.n_vals[ch][k].configure(fg=(CH_COLORS[ch][0] if k == "vm" else "#e0e0e0") if on else "#444")

    def read_job(self, full=True):
        """full=False: 측정 전압 · 전류만 (빠르게), full=True: 설정값 · 출력 상태까지"""
        e = self.inst["e"]
        if e is not None:
            try:
                vals = dict(vm=fnum(e.query("MEAS:VOLT?")), im=fnum(e.query("MEAS:CURR?")))
                if full:
                    vals.update(vs=fnum(e.query("VOLT?")), is_=fnum(e.query("CURR?")), o=e.query("OUTP?").strip())
                self.post("e", vals)
            except Exception as ex:
                self.post("log", "E36155A 읽기 오류: %s" % ex, "err")
        n = self.inst["n"]
        chs = list(self.read_chs)
        if n is not None and chs:
            lst = "(@%s)" % ",".join(str(c) for c in chs)
            try:
                vm = flist(n.query("MEAS:VOLT? " + lst))
                im = flist(n.query("MEAS:CURR? " + lst))
                out = {c: dict(vm=vm[i], im=im[i]) for i, c in enumerate(chs)}
                if full:
                    vs = flist(n.query("VOLT? " + lst))
                    is_ = flist(n.query("CURR? " + lst))
                    o = n.query("OUTP? " + lst).strip().split(",")
                    for i, c in enumerate(chs):
                        out[c].update(vs=vs[i], is_=is_[i], o=o[i].strip() if i < len(o) else "?")
                self.post("n", out)
            except Exception as ex:
                self.post("log", "N6705C 읽기 오류: %s" % ex, "err")
        self.post("tick")

    def read_once(self):
        if self.inst["e"] is None and self.inst["n"] is None:
            self.post("log", "먼저 [연결] 하세요.", "err")
            return
        self.worker.tasks.put(self.read_job)

    def toggle_poll(self):
        self.update_dt()
        self.force_full = True
        self.polling = not self.polling
        self.btn_poll.configure(text="■ 계속 읽기 중지" if self.polling else "▶ 계속 읽기")
        self.rate_n, self.rate_t = 0, time.time()

    def update_dt(self, *a):
        v = fnum(self.var_dt.get())
        self.poll_dt = max(0.01, v) if v == v else 1.0     # 최소 0.01 초 (실제 속도는 장비 응답 속도까지)

    def send_cmd(self):
        k = "e" if self.var_tgt.get() == "E36155A" else "n"
        cmd = self.var_cmd.get().strip()
        if not cmd:
            return
        inst = self.inst[k]
        if inst is None:
            self.post("log", "%s 가 연결되지 않았습니다." % self.var_tgt.get(), "err")
            return
        if not cmd.endswith("?") and not cmd.upper().startswith(("*CLS", "SYST:ERR")):
            if not messagebox.askyesno(APP, "쓰기 명령입니다 (전원 설정이 바뀔 수 있음):\n\n%s → %s\n\n보낼까요?"
                                       % (cmd, self.var_tgt.get())):
                return

        def job():
            if cmd.endswith("?") or "?" in cmd.split()[0]:
                r = inst.query(cmd).strip()
                self.post("log", "%s ← %s : %s" % (self.var_tgt.get(), cmd, r), "ok")
            else:
                inst.write(cmd)
                err = inst.query("SYST:ERR?").strip()
                self.post("log", "%s ← %s  (SYST:ERR? %s)" % (self.var_tgt.get(), cmd, err),
                          "ok" if err.startswith(("+0", "0")) else "err")
        self.worker.tasks.put(job)

    # ------------------------------------------------ 결과 표시
    def pump(self):
        try:
            while True:
                m = self.q.get_nowait()
                self.handle(m)
        except queue.Empty:
            pass
        self.root.after(100, self.pump)

    def handle(self, m):
        kind = m[0]
        if kind == "call":
            m[1]()
            return
        if kind == "stepwait":
            what, k = m[1], m[2]
            if what is None:
                self.btn_next.configure(state="disabled", text="▶ 다음 단계")
                self.lbl_step.configure(text="진행 중...", fg="#b0bec5")
            else:
                self.btn_next.configure(state="normal", text=("▶ 다음 단계 (%d번)" % k) if k else ("▶ 다음 (%s)" % what[:22]))
                self.lbl_step.configure(text="⏸ 멈춤 - 다음 할 일: %s   ([▶ 다음] 또는 %s)" % (
                    what, ("깜빡이는 %d번 칩 클릭" % k) if k else "Enter"), fg="#ffd54f")
                self.btn_next.focus_set()
                if k:
                    self.blink_step(k)
            return
        if kind == "seq":
            self.lbl_seq.configure(text=m[1], fg=m[2])
            if getattr(self, "_busy", None) is not None:
                try:
                    self.pop_seq(m[1])
                except (tk.TclError, AttributeError):
                    pass
            return
        if kind == "lamp":
            txt, bg = {"off": ("  OFF  ", "#616161"), "seq": (" 진행 중 ", "#f9a825"), "on": ("  ON  ", "#2e7d32"),
                       "fault": ("  이상  ", "#c62828")}.get(m[1], ("?", "#616161"))
            self.lamp.configure(text=txt, bg=bg)
            return
        if kind == "red":
            self.red_popup(m[1])
            return
        if kind == "alert":
            messagebox.showwarning(APP, m[1])
            return
        if kind == "note":
            self.handle(("log", m[1], "ok"))
            try:
                self.root.bell()
            except tk.TclError:
                pass
            return
        if kind == "bias_done":
            if self.on_bias_done:
                self.on_bias_done(m[1], m[2])
            else:
                messagebox.showinfo(APP, m[1])
            return
        if kind == "log":
            tag = m[2] if len(m) > 2 else ""
            self.log.insert("end", "%s  %s\n" % (datetime.datetime.now().strftime("%H:%M:%S"), m[1]), tag)
            self.log.see("end")
        elif kind == "visa":
            ok, txt = m[1], m[2]
            self.lbl_visa.configure(text=("VISA OK: " + txt) if ok else txt, foreground="#2e7d32" if ok else "#c62828")
            self.handle(("log", "VISA: " + txt, "ok" if ok else "err"))
        elif kind == "found":
            self.found.delete(*self.found.get_children())
            for r, idn in m[1]:
                self.found.insert("", "end", values=(r, idn))
        elif kind == "conn":
            k, ok, txt = m[1], m[2], m[3]
            (self.btn_e if k == "e" else self.btn_n).configure(text="연결 해제" if ok else "연결")
            (self.lbl_e_idn if k == "e" else self.lbl_n_idn).configure(
                text=txt or "연결 안 됨", fg="#8bc34a" if ok else "#ef5350")
        elif kind == "mods":
            for i, md in enumerate(m[1][:4]):
                self.n_vals[i + 1]["mod"].configure(text=md or "-")
        elif kind == "tick":
            self.rate_n += 1
            now = time.time()
            if now - self.rate_t >= 1.0:
                self.lbl_rate.configure(text="실제 %.1f 회/초" % (self.rate_n / (now - self.rate_t)) if self.polling else "")
                self.rate_n, self.rate_t = 0, now
        elif kind == "e":
            v = m[1]
            self.check_faults("e", v)
            self.e_last = (v["im"], v["vm"], time.time())
            self.root.after(5, self.check_order)
            if v["im"] == v["im"]:
                self.trend.append((time.time(), v["im"]))
            self.draw_trend()
            if self.on_vdd:
                try:
                    self.on_vdd(v["im"], v["vm"], time.time())
                except Exception:
                    pass
            self.e_vals["vm"].configure(text="%.4f V" % v["vm"])
            self.e_vals["im"].configure(text=self.fmt_a(v["im"]))
            if "o" not in v:
                return
            self.store_set(("e", None), v)
            self.e_vals["vs"].configure(text="%.3f V" % v["vs"], fg="#e0e0e0")
            self.e_vals["is"].configure(text=self.fmt_a(v["is_"]), fg="#e0e0e0")
            self.refresh_entry("e", None)
            on = v["o"] in ("1", "ON")
            self.e_vals["o"].configure(text=" OUTPUT ON " if on else " OFF ", bg="#2e7d32" if on else "#333",
                                       fg="white" if on else "#999")
            self.e_led.configure(bg="#43a047" if on else "#555")
        elif kind == "n":
            self.check_faults("n", m[1])
            for c, v in m[1].items():
                row = self.n_vals[c]
                row["vm"].configure(text="%+.4f V" % v["vm"])
                row["im"].configure(text=self.fmt_a(v["im"]))
                self.last_vm[c] = v["vm"]
                if "o" not in v:
                    continue
                self.store_set(("n", c), v)
                row["vs"].configure(text="Set %+.3f V" % v["vs"], fg="#5a6470")
                row["is"].configure(text="Lim %s" % self.fmt_a(v["is_"]), fg="#5a6470")
                self.refresh_entry("n", c)
                on = v["o"] in ("1", "ON")
                row["o"].configure(text="On" if on else "Off")
                self.n_leds[c].configure(bg="#43a047" if on else "#555")

    def build_trend(self, parent):
        """Vdd 전류 추이 (최근 60 초) - 급상승 감시가 잘 되는지 눈으로 확인"""
        fr = tk.Frame(parent, bg="#0a0f14", bd=2, relief="sunken")
        fr.pack(fill="both", expand=True, padx=8, pady=(4, 4))
        self.trend_cv = tk.Canvas(fr, bg="#0a0f14", height=(120 if self.box_trend else 140), highlightthickness=0)
        self.trend_cv.pack(fill="both", expand=True)
        self.trend_cv.bind("<Configure>", lambda e: self.draw_trend(force=True))
        self._trend_t = 0.0

    def draw_trend(self, force=False):
        cv = getattr(self, "trend_cv", None)
        if cv is None:
            return
        now = time.time()
        if not force and now - self._trend_t < 0.3:
            return
        self._trend_t = now
        self.draw_trend_canvas(cv)

    def draw_trend_canvas(self, cv, compact=False):
        """Vdd 전류 추이 (최근 60 초) 그리기 - 파워서플라이 화면 / (Bench) 맨 위 바이어스 칸의 작은 그래프"""
        now = time.time()
        cv.delete("all")
        W, H = cv.winfo_width(), cv.winfo_height()
        if W < 60 or H < 40:
            return
        L, R, T, B = (34, 6, 14, 12) if compact else (52, 10, 20, 18)
        pw, ph = W - L - R, H - T - B
        cv.create_text(L if compact else 8, 1 if compact else 4,
                       text="Vdd 전류%s (최근 60 초)%s" % (" mA" if compact else "", " · 급상승 감시 중" if self.bias_state == "on" else ""),
                       anchor="nw", fill="#90a4ae", font=("맑은 고딕", 7 if compact else 8))
        pts = [(t, a) for t, a in self.trend if now - t <= 60 and a == a]
        imax = self.sqf("imax", 1.5) if hasattr(self, "sq") else 1.5
        top = max([a for _, a in pts] + [0.05]) * 1.25
        top = min(max(top, 0.05), max(imax * 1.1, 0.05))
        cv.create_rectangle(L, T, L + pw, T + ph, outline="#37474f")
        nk = 2 if compact else 4
        for k in range(nk + 1):
            y = T + ph - ph * k / nk
            cv.create_line(L, y, L + pw, y, fill="#1c262f")
            cv.create_text(L - 4, y, text="%.0f" % (top * 1e3 * k / nk), anchor="e", fill="#78909c", font=("Consolas", 7 if compact else 8))
        if not compact:
            cv.create_text(L - 4, T + ph + 9, text="mA", anchor="e", fill="#78909c", font=("Consolas", 8))
        if imax <= top:
            y = T + ph - ph * imax / top
            cv.create_line(L, y, L + pw, y, fill="#ef5350", dash=(5, 4))
            cv.create_text(L + pw - 2, y - 2, text="최대 Id %.2f A" % imax, anchor="se", fill="#ef9a9a", font=("맑은 고딕", 8))
        if len(pts) > 1:
            xy = []
            for t, a in pts:
                xy += [L + pw - (now - t) / 60.0 * pw, T + ph - min(a, top) / top * ph]
            cv.create_line(*xy, fill="#69f0ae", width=2)
            cv.create_text(xy[-2] - 4, xy[-1] - 8, text="%.1f mA" % (pts[-1][1] * 1e3), anchor="e", fill="#69f0ae",
                           font=("Consolas", 9, "bold"))
        cv.create_text(L, T + ph + 1, text="-60 s", anchor="nw", fill="#78909c", font=("Consolas", 7 if compact else 8))
        cv.create_text(L + pw, T + ph + 1, text="지금", anchor="ne", fill="#78909c", font=("맑은 고딕", 7 if compact else 8))

    def red_popup(self, msg):
        """이상 경고: 빨간 창 (항상 위, 메인 창 가운데)"""
        w = tk.Toplevel(self.root)
        w.withdraw()
        w.title("⚠ 전원 이상")
        w.configure(bg="#b71c1c")
        w.transient(self.root)
        tk.Label(w, text="⚠  전원 이상", bg="#b71c1c", fg="white", font=("맑은 고딕", 16, "bold")).pack(padx=24, pady=(16, 6))
        tk.Label(w, text=msg, bg="#b71c1c", fg="white", font=("맑은 고딕", 11), justify="left", wraplength=620).pack(padx=24, pady=6)
        tk.Label(w, text=time.strftime("%H:%M:%S"), bg="#b71c1c", fg="#ffcdd2", font=("Consolas", 9)).pack()
        tk.Button(w, text="확인", width=12, font=("맑은 고딕", 11, "bold"), command=w.destroy).pack(pady=(8, 16))
        w.update_idletasks()
        try:
            x = self.root.winfo_rootx() + (self.root.winfo_width() - w.winfo_reqwidth()) // 2
            y = self.root.winfo_rooty() + (self.root.winfo_height() - w.winfo_reqheight()) // 2
            w.geometry("+%d+%d" % (max(0, x), max(0, y)))
        except tk.TclError:
            pass
        w.deiconify()
        try:
            w.attributes("-topmost", True)
            w.lift()
        except tk.TclError:
            pass
        self.handle(("log", "⚠ " + msg.replace("\n", " "), "err"))

    @staticmethod
    def fmt_a(a):
        if a != a:
            return "---"
        if abs(a) >= 1:
            return "%.4f A" % a
        if abs(a) >= 1e-3:
            return "%.3f mA" % (a * 1e3)
        return "%.2f µA" % (a * 1e6)

    def on_close(self):
        self.polling = False
        self.worker.alive = False
        for k in ("e", "n"):
            try:
                if self.inst[k] is not None:
                    self.inst[k].close()
            except Exception:
                pass
        self.root.destroy()


def main():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
