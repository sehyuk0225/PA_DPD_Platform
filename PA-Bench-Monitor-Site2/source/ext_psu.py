"""
ext_psu  -  PA Bench Monitor (원래 Bench) 용: 키사이트 외부 전원 (GPIB) 2 가지

  1. 드라이버 앰프 전원: 별도 전원 1 대 (예: E3631A · E3632A · E36xx · N67xx 채널)
     · POWER SUPPLY ON: 게이트 ON → Vdd → 전류 맞추기가 다 끝난 뒤 맨 마지막에 ON (그 다음 측정 · RF)
     · POWER SUPPLY OFF · 전원 이상 처리: 맨 먼저 OFF → 그 다음 원래 끄는 순서
     · ON / OFF 만 (전압은 장비에 맞춰 둔 값 그대로)
  2. 외부 PK 게이트 (하나 더): N6705C 의 DRV · CA · PK 는 그대로 + 외부 전원에 피킹 게이트 하나 더
     · 샘플 종류마다 '외부 PK V' (빈칸 = 안 씀) - 값이 있는 샘플만 켬
     · N6705C PK 채널 (CH3) 과 같이 켜지고 꺼짐: POWER SUPPLY ON 순서 (게이트 ON = Vdd 보다 먼저) · [게이트 전체 ON/OFF] · CH3 버튼
       켤 때 그 값으로 걸고 읽어서 확인 (안 맞으면 멈춤) · 전원 이상 처리 때는 게이트처럼 그대로

  출력 칸: (단일) = 출력 1 개 장비 (E3632A · E36155A 등) · P6V / P25V / N25V = E3631A · 1 ~ 4 = 채널 장비 (N67xx · E3631xA)
  장비 없이: 주소 DEMO

psu_panel.py (바이어스 자동 순서) 는 고치지 않음: 이 화면의 장비 열기 · post · run_seq · safe_down · read_job 만 바깥에서 감쌈.
Site2 판에서는 안 씀 (Site2 는 site2.py 가 함).
"""
import re
import time

PK_CH = 3               # N6705C 의 PK 채널 - 외부 PK 는 이 채널과 같이 켜지고 꺼짐
OUTS = ["(단일)", "P6V", "P25V", "N25V", "1", "2", "3", "4"]


def _chs(cmd):
    m = re.search(r"\(@([^)]*)\)", cmd)
    if not m:
        return None
    out = []
    for part in m.group(1).split(","):
        part = part.strip()
        if ":" in part:
            a, b = part.split(":")
            out += list(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return out


def norm_out(s):
    s = (s or "").strip().upper()
    if s in ("", "(단일)", "단일", "-"):
        return ""
    if s in ("+6V", "6V"):
        return "P6V"
    if s in ("+25V", "25V"):
        return "P25V"
    if s in ("-25V",):
        return "N25V"
    if s.startswith("CH"):
        s = s[2:]
    return s


class DemoExt:
    """가상 외부 전원 (출력 이름 · 채널 아무거나)"""

    def __init__(self):
        self.sel = ""
        self.v, self.i, self.on = {}, {}, {}

    def _k(self, cmd):
        ch = _chs(cmd)
        return str(ch[0]) if ch else self.sel

    def query(self, cmd):
        import random
        c = cmd.strip().upper()
        if c == "*IDN?":
            return "Keysight,EXT-PSU,0,DEMO"
        if c.startswith("SYST:ERR"):
            return '+0,"No error"'
        k = self._k(c)
        if c.startswith("MEAS") and " " in c and not _chs(c):
            k = c.split()[-1]
        on = self.on.get(k, self.on.get("", False)) if k not in self.on else self.on[k]
        if c.startswith("OUTP?"):
            return "1" if on else "0"
        if c.startswith("MEAS:VOLT"):
            return "%+.6E" % ((self.v.get(k, 0.0) + random.uniform(-5e-4, 5e-4)) if on else 0.0)
        if c.startswith("MEAS:CURR"):
            return "%+.6E" % (random.uniform(0.05, 0.06) if on else 0.0)
        if c.startswith("VOLT?"):
            return "%+.6E" % self.v.get(k, 0.0)
        if c.startswith("CURR?"):
            return "%+.6E" % self.i.get(k, 1.0)
        return "0"

    def write(self, cmd):
        c = cmd.strip().upper()
        val = c.split()[1].split(",")[0] if " " in c else ""
        k = self._k(c)
        if c.startswith("INST:SEL") or c.startswith("INST "):
            self.sel = val
        elif c.startswith("OUTP"):
            if _chs(c):
                self.on[k] = val in ("ON", "1")
            else:                                       # 출력 켜기가 장비 전체 (E3631A 등)
                for k_ in list(self.on) + [self.sel, ""]:
                    self.on[k_] = val in ("ON", "1")
        elif c.startswith("VOLT ") and not c.startswith("VOLT:"):
            self.v[k] = float(val)
        elif c.startswith("CURR ") and not c.startswith("CURR:"):
            self.i[k] = float(val)

    def close(self):
        pass


class Out:
    """외부 전원의 출력 하나: (단일) · P6V/P25V/N25V (E3631A, INST:SEL) · 채널 번호 (@n)"""

    def __init__(self, inst, out=""):
        self.inst = inst
        self.out = norm_out(out)
        self.ch = int(self.out) if self.out.isdigit() else None

    def name(self):
        return ("CH%d" % self.ch) if self.ch else (self.out or "단일")

    def _sel(self):
        if self.out and self.ch is None:
            self.inst.write("INST:SEL %s" % self.out)

    def q(self, head):
        head = head.strip()
        if self.ch:
            return self.inst.query("%s (@%d)" % (head, self.ch)).strip()
        if head.upper().startswith("MEAS:") and self.out:
            return self.inst.query("%s %s" % (head, self.out)).strip()
        if head.upper().startswith("OUTP"):
            return self.inst.query("OUTP?").strip()
        self._sel()
        return self.inst.query(head).strip()

    def w(self, head, val):
        head = head.strip().upper()
        if self.ch:
            self.inst.write("%s %s,(@%d)" % (head, val, self.ch))
        elif head == "OUTP":
            self.inst.write("OUTP %s" % val)
        else:
            self._sel()
            self.inst.write("%s %s" % (head, val))

    def is_on(self):
        try:
            return self.q("OUTP?") in ("1", "ON")
        except Exception:
            return False


class GateHook:
    """N6705C 를 그대로 쓰되, PK 채널 (CH3) 을 켜고 끄는 명령을 보면 외부 PK 게이트도 같이 켜고 끔
    (POWER SUPPLY ON 순서 · [게이트 전체 ON/OFF] · CH3 On/Off 버튼 어디서든)"""

    def __init__(self, n, ext):
        self.n = n
        self.ext = ext

    def __getattr__(self, k):               # 데모 장비 속성 (out · vset · kind · peer …) 은 N6705C 것
        n = self.__dict__.get("n")
        if n is None:
            raise AttributeError(k)
        return getattr(n, k)

    @property
    def timeout(self):
        return getattr(self.n, "timeout", 3000)

    @timeout.setter
    def timeout(self, v):
        try:
            self.n.timeout = v
        except Exception:
            pass

    def query(self, cmd):
        return self.n.query(cmd)

    def write(self, cmd):
        c = cmd.strip().upper()
        chs = _chs(c) or []
        on = off = False
        if c.startswith("OUTP") and not c.startswith("OUTP:") and PK_CH in chs:
            val = c.split()[1].split(",")[0] if " " in c else ""
            on, off = val in ("ON", "1"), val in ("OFF", "0")
        if on:
            self.ext.pk_follow(True)            # 게이트보다 먼저 (Vdd 전에)
        self.n.write(cmd)
        if off:
            self.ext.pk_follow(False)           # 게이트 끈 뒤

    def close(self):
        try:
            self.n.close()
        except Exception:
            pass


class Ext:
    """설정 (app.s['ext_psu']) · 외부 장비 열기 · 드라이버 앰프 ON/OFF"""

    def __init__(self, app):
        self.app = app
        self.P = app.psu_panel
        self.dev = {}                                    # 주소 → 장비
        self.fail_t = {}                                 # 주소 → 마지막 연결 실패 시각
        self.last = {}                                   # 마지막으로 읽은 값 (pk · da)
        self.orig_open = self.P.visa.open

    def cfg(self):
        c = self.app.s.setdefault("ext_psu", {})
        if not c.get("_pk25"):                          # 1 번 사이트: 외부 PK 게이트는 E3631A +25V 출력 (기본값 · 한 번만 · 칸에서 바꿈)
            if not c.get("pk_out"):
                c["pk_out"] = "P25V"
            c["_pk25"] = True
        return c

    def log(self, msg, tag=""):
        try:
            self.P.q.put(("log", msg, tag))
        except Exception:
            pass

    def open_dev(self, addr):
        addr = (addr or "").strip()
        if not addr:
            raise RuntimeError("주소가 비어 있음")
        d = self.dev.get(addr.upper())
        if d is not None:
            return d
        if addr.upper().startswith("DEMO"):
            d = DemoExt()
        else:
            import psu_panel as PM
            if self.P.visa.rm is None:
                self.P.visa.open_rm()
            d = self.orig_open(PM.fix_addr(addr), 1500)
        try:
            idn = d.query("*IDN?").strip()
        except Exception as e:
            idn = "? (%s)" % e
        self.log("외부 전원 연결: %s → %s" % (addr, idn), "ok")
        self.dev[addr.upper()] = d
        if not hasattr(self, "idn"):
            self.idn = {}
        self.idn[addr.upper()] = idn
        return d

    def drop(self, addr):
        d = self.dev.pop((addr or "").strip().upper(), None)
        if d is not None:
            try:
                d.close()
            except Exception:
                pass

    def pk_out(self):
        c = self.cfg()
        return Out(self.open_dev(c.get("pk_addr", "")), c.get("pk_out", ""))

    def da_out(self):
        c = self.cfg()
        return Out(self.open_dev(c.get("da_addr", "")), c.get("da_out", ""))

    def pk_lims(self):
        """외부 PK 게이트 전용 안전 한계 (V) - N6705C 게이트 한계 (2 ~ 6 V) 와 따로. 음수 (예 -3.25 V) 도 됨"""
        c = self.cfg()
        try:
            lo = float(c.get("pk_min", "-6"))
        except (TypeError, ValueError):
            lo = -6.0
        try:
            hi = float(c.get("pk_max", "6"))
        except (TypeError, ValueError):
            hi = 6.0
        return min(lo, hi), max(lo, hi)

    def pk_v(self):
        """지금 화면 '외부 PK' 칸 값 (빈칸 = 안 씀) → float / None"""
        v = getattr(self.app, "var_pk_ext", None)
        t = v.get().strip() if v is not None else ""
        try:
            return float(t) if t else None
        except ValueError:
            raise RuntimeError("외부 PK 칸이 숫자가 아님: %s" % t)

    def pk_v_safe(self):
        try:
            return self.pk_v()
        except Exception:
            return None

    def pk_state(self):
        """외부 PK 상태 한 줄 (이상 메시지에 붙임)"""
        c = self.cfg()
        if not (c.get("pk_addr") or "").strip():
            return ""
        v = self.pk_v_safe()
        try:
            o = self.pk_out()
            on = o.is_on()
            m = float(o.q("MEAS:VOLT?").split(",")[0])
            return "외부 PK 게이트: %s · 읽음 %.3f V (설정 %s)" % ("ON" if on else "OFF", m, ("%.3f V" % v) if v is not None else "빈칸 = 안 씀")
        except Exception as e:
            return "외부 PK 게이트: 읽기 실패 (%s) · 설정 %s" % (e, ("%.3f V" % v) if v is not None else "빈칸")

    def pk_on_for_seq(self):
        """POWER SUPPLY ON 맨 앞: 이 샘플에 외부 PK 값이 있으면 그 값으로 걸고 ON → 읽어서 확인 (안 맞으면 순서 중지)"""
        v = self.pk_v()
        if v is None:
            return
        c = self.cfg()
        if not (c.get("pk_addr") or "").strip():
            raise RuntimeError("외부 PK 값 %.3f V 가 있는데 외부 PK 전원 주소가 없음 (장비 설정 > 파워서플라이 연결)" % v)
        P = self.P
        gmin, gmax = self.pk_lims()
        if not (gmin <= v <= gmax):
            raise RuntimeError("외부 PK %.3f V 가 외부 PK 안전 한계 %g ~ %g V 밖 (장비 설정 > 파워서플라이 연결 '외부 전원')" % (v, gmin, gmax))
        o = self.pk_out()
        o.w("VOLT", "%.3f" % v)
        try:
            o.w("CURR", "%.4f" % float(P.lim["g_imax"].get()))
        except Exception:
            pass
        if not o.is_on():
            o.w("OUTP", "ON")
        time.sleep(0.5)
        try:
            m = float(o.q("MEAS:VOLT?").split(",")[0])
        except Exception:
            m = float("nan")
        try:
            tol = abs(float(P.sq["gtol"].get()))
        except Exception:
            tol = 0.05
        if not o.is_on() or m != m or abs(m - v) > tol:
            raise RuntimeError("외부 PK 게이트 이상: 설정 %.3f V, 읽음 %s" % (v, ("%.3f V" % m) if m == m else "없음"))
        self.log("외부 PK 게이트 ON %.3f V (읽음 %.3f V) - 게이트 · Vdd 보다 먼저 (%s %s)"
                 % (v, m, c.get("pk_addr", ""), o.name()), "ok")

    def pk_follow(self, on):
        """N6705C PK 채널과 같이: ON = 이 샘플 '외부 PK' 값으로 켜고 읽어서 확인 (빈칸이면 안 켬) · OFF = 끔"""
        c = self.cfg()
        if not (c.get("pk_addr") or "").strip():
            return
        if not on:
            self.pk_set(False, "게이트와 같이")
            return
        try:
            v = self.pk_v()
        except RuntimeError as e:
            self.log("외부 PK 게이트 안 켬: %s" % e, "err")
            raise
        if v is None:
            self.log("외부 PK 게이트: 이 샘플 '외부 PK' 칸이 빈칸 → 안 켬 (쓰려면 샘플 종류 설정 · 파워서플라이 화면 '외부 PK' 칸에 V)", "")
            return
        self.pk_on_for_seq()

    def pk_set(self, on, why=""):
        """외부 PK 게이트 ON / OFF (연결돼 있을 때만)"""
        c = self.cfg()
        a_ = (c.get("pk_addr") or "").strip()
        if not a_ or a_.upper() not in self.dev:
            return
        try:
            o = self.pk_out()
            if o.is_on() == bool(on):
                return
            o.w("OUTP", "ON" if on else "OFF")
            self.log("외부 PK 게이트 %s%s" % ("ON" if on else "OFF", " - " + why if why else ""), "ok")
        except Exception as e:
            self.log("외부 PK 게이트 %s 실패: %s" % ("ON" if on else "OFF", e), "err")

    def shared_outp(self):
        """드라이버 앰프와 외부 PK 가 같은 E36xx 장비의 다른 출력 (P6V · P25V · N25V) 이면 True
        → 그 장비는 OUTP ON/OFF 가 세 출력 공통이라 드라이버 앰프만 끄면 PK 게이트도 같이 꺼짐 (GaN: 게이트 0 V = 전류 흐름)"""
        c = self.cfg()
        if not c.get("da_use"):
            return False
        a, b = (c.get("da_addr") or "").strip().upper(), (c.get("pk_addr") or "").strip().upper()
        if not a or not b:
            return False
        try:
            if Out(None, c.get("da_out", "")).ch or Out(None, c.get("pk_out", "")).ch:
                return False                               # N67xx 처럼 채널마다 OUTP 가 따로면 괜찮음
        except Exception:
            pass
        if a == b:
            return True
        ids = getattr(self, "idn", {})
        ia, ib = ids.get(a, ""), ids.get(b, "")
        return bool(ia and ib and not ia.startswith("?") and ia == ib)

    def da_set(self, on, why=""):
        """드라이버 앰프 전원 ON / OFF 만 (전압 · 전류는 장비에 맞춰 둔 값)"""
        c = self.cfg()
        if not c.get("da_use"):
            return
        if self.shared_outp() and (self.pk_v_safe() is not None):
            # 같은 장비 = OUTP 공통 → 드라이버 앰프를 끄면 PK 게이트도 꺼짐 → 위험해서 ON/OFF 안 함
            self.log("✖ 드라이버 앰프 %s 안 함: 드라이버 앰프와 외부 PK 가 같은 장비 (%s) 라 출력 ON/OFF 가 같이 움직임 "
                     "→ 드라이버 앰프만 끄면 PK 게이트도 꺼져 전류가 흐름. 드라이버 앰프는 다른 전원으로 (장비 설정 > 외부 전원)"
                     % ("ON" if on else "OFF", c.get("da_addr", "")), "err")
            return
        try:
            o = self.da_out()                             # 전압 · 전류는 장비에 맞춰 둔 값 그대로 - ON / OFF 만
            if o.is_on() == bool(on):
                return
            o.w("OUTP", "ON" if on else "OFF")
            time.sleep(0.2)
            st = o.is_on()
            ok = st == bool(on)
            v = ""
            if on:
                try:
                    v = " · %.3f V" % float(o.q("MEAS:VOLT?").split(",")[0])
                except Exception:
                    pass
            self.log("드라이버 앰프 전원 %s (%s %s)%s%s%s" % ("ON" if on else "OFF", c.get("da_addr", ""), o.name(), v,
                                                       " - " + why if why else "", "" if ok else "  ✖ 상태가 안 바뀜"),
                     "ok" if ok else "err")
        except Exception as e:
            self.log("드라이버 앰프 전원 %s 실패: %s" % ("ON" if on else "OFF", e), "err")


def attach(app):
    """원래 Bench 에 붙임 (Site2 판에서는 부르지 않음)"""
    import psu_panel as PM
    if getattr(PM.Visa, "_site2", False):
        return
    P = getattr(app, "psu_panel", None)
    if P is None:
        return
    ext = Ext(app)
    app.ext_psu = ext

    # 1. 게이트 장비 (N6705C) 를 열면 GateHook 로 감쌈 (PK 채널과 외부 PK 를 같이)
    def open2(res, timeout_ms=3000):
        inst = ext.orig_open(res, timeout_ms)
        try:
            idn = inst.query("*IDN?").strip().upper()
        except Exception:
            return inst
        if "N67" in idn and "SITE2" not in idn:
            return GateHook(inst, ext)
        return inst
    P.visa.open = open2

    # 2. 드라이버 앰프 전원 순서
    orig_post = P.post

    def post2(*msg):
        if msg and msg[0] == "bias_done" and getattr(P, "bias_state", "") == "on":
            ext.da_set(True, "바이어스 · 전류 맞추기 끝난 뒤 마지막")      # 순서 쓰레드에서 바로 → 측정 (RF) 보다 먼저
        orig_post(*msg)
    P.post = post2

    orig_run = P.run_seq

    def run2(fn, plan):
        nm = getattr(fn, "__name__", "")
        if nm == "job_power_off":
            ext.da_set(False, "끄기 맨 먼저")
        if nm == "job_power_on":
            def fn2(pl, _fn=fn):
                # 어떤 시작 방식이든 (게이트가 이미 켜져 있어 OUTP ON 을 안 보내는 경우 포함) Vdd 전에 외부 PK 게이트 확인
                #   (예전: '게이트만 ON · 지금 값 그대로' 로 시작하면 외부 PK 가 꺼진 채 Vdd → 쇼트 확인에서 과전류)
                if ext.pk_v_safe() is not None:
                    try:
                        ext.pk_on_for_seq()
                    except Exception as e:
                        P.post("log", "외부 PK 게이트 확인 실패 → Vdd 안 켬: %s" % e, "err")
                        P.red("외부 PK 게이트 확인 실패 → Vdd 를 켜지 않고 멈춥니다\n\n%s" % e)
                        raise PM.SeqStop("외부 PK 이상")
                return _fn(pl)
            fn2.__name__ = nm
            orig_run(fn2, plan)
            return
        orig_run(fn, plan)
    P.run_seq = run2

    orig_fault = P.fault

    def fault2(msg):
        # 과전류 등 순서 이상: 외부 PK 상태를 같이 보여 줌 (PK 게이트가 꺼져 있었는지 바로 보이게)
        try:
            st = ext.pk_state()
        except Exception:
            st = ""
        if st:
            P.post("log", st, "err")
            msg = msg + "\n\n" + st
        orig_fault(msg)
    P.fault = fault2

    orig_safe = P.safe_down

    def safe2(why):
        ext.da_set(False, "이상 처리 맨 먼저")
        orig_safe(why)
    P.safe_down = safe2

    attach_soft_fault(app, lambda: ext.da_set(False, "Vdd 전류 이상 - 샘플 보호"))
    attach_cfg_ui(app, ext)
    attach_panel(app, ext)
    attach_pk_field(app, ext)
    attach_top_row(app, ext)
    attach_addr_ui1(app)


def attach_addr_ui1(app):
    """Site1: '파워서플라이 연결 · SCPI' 의 장비 찾기 목록 아래에 E36155A · N6705C 주소 직접 입력 칸 + 연결 버튼
    (기존 주소 칸과 같은 값을 공유 - 어느 쪽에서 바꿔도 같이 바뀜)"""
    import tkinter as tk
    from tkinter import ttk
    P = getattr(app, "psu_panel", None)
    if P is None or not hasattr(P, "box_extra"):
        return
    box = P.box_extra
    first = None
    for w in box.winfo_children():
        if "Labelframe" in w.winfo_class():
            first = w
            break
    lf = ttk.LabelFrame(box, text=" 장비 주소 (직접 입력 · 위 목록 더블클릭으로도 넣기) ", padding=8)
    if first is not None:
        lf.pack(fill="x", padx=8, pady=(4, 4), after=first)
    else:
        lf.pack(fill="x", padx=8, pady=(4, 4))
    g = ttk.Frame(lf)
    g.pack(fill="x")
    btn = {}
    for r_, (k, txt, var) in enumerate((("e", "E36155A (드레인 Vdd)", P.var_e), ("n", "N6705C (게이트 바이어스)", P.var_n))):
        ttk.Label(g, text=txt).grid(row=r_, column=0, sticky="w", pady=2)
        en = ttk.Entry(g, textvariable=var, width=34)
        en.grid(row=r_, column=1, sticky="we", padx=6, pady=2)
        b = ttk.Button(g, text="연결", width=8, command=lambda k=k: P.connect(k))
        b.grid(row=r_, column=2, pady=2)
        en.bind("<Return>", lambda e, k=k: (P.inst.get(k) is None) and P.connect(k))
        btn[k] = b
        lb = ttk.Label(g, text="", foreground="#666")
        lb.grid(row=r_, column=3, sticky="w", padx=6)
        btn[k + "_l"] = lb
    g.columnconfigure(1, weight=1)
    ttk.Label(lf, text="예: GPIB0::6::INSTR · TCPIP0::192.168.0.13::inst0::INSTR · 장비 없이: DEMO   "
                       "(주소를 바꿨으면 [연결 해제] 후 다시 [연결])", foreground="#607d8b").pack(anchor="w", pady=(4, 0))
    ttk.Label(lf, text="※ 같은 장비를 GPIB 와 LAN 두 주소로 따로 넣지 마세요 (드라이버 앰프 · 외부 PK 칸 포함) - 읽기가 꼬여 전류를 못 읽을 수 있음",
              foreground="#b26a00").pack(anchor="w")

    def sync():
        try:
            for k in ("e", "n"):
                t = (P.btn_e if k == "e" else P.btn_n).cget("text")
                if btn[k].cget("text") != t:
                    btn[k].configure(text=t)
                idn = (P.lbl_e_idn if k == "e" else P.lbl_n_idn).cget("text")
                on = P.inst.get(k) is not None
                tx = (", ".join(idn.split(",")[1:3]) if on else "연결 안 됨")[:40]
                if btn[k + "_l"].cget("text") != tx:
                    btn[k + "_l"].configure(text=tx, foreground="#2e7d32" if on else "#999")
        except Exception:
            return
        app.root.after(500, sync)        # (lf.after 쓰면 설정 불러오기 때 창 닫기가 멈춤)
    sync()


def gates_on(P):
    n = P.inst.get("n")
    if n is None:
        return False
    try:
        return any(x.strip() in ("1", "ON") for x in n.query("OUTP? (@1,2,3)").split(","))
    except Exception:
        return True


def pk_ext_val(app, name=None):
    """샘플 종류의 '외부 PK V' (빈칸 = 안 씀) → float 또는 None"""
    try:
        v = (app.stype_bias(name) or {}).get("pk_ext", "")
        return float(v) if str(v).strip() else None
    except (TypeError, ValueError):
        return None


def attach_pk_field(app, ext):
    """파워서플라이 화면 '게이트 기본값' 줄에 [외부 PK] 칸: 지금 샘플의 값 (빈칸 = 외부 PK 안 씀)"""
    import tkinter as tk
    from tkinter import ttk
    P = app.psu_panel
    try:
        tv = str(P.sq["g3"])
    except Exception:
        return
    row = None
    stack = [P.box_left]
    while stack and row is None:
        w_ = stack.pop()
        for c in w_.winfo_children():
            try:
                if c.winfo_class() in ("TEntry", "Entry") and str(c.cget("textvariable")) == tv:
                    row = c.master
                    break
            except Exception:
                pass
            stack.append(c)
    if row is None:
        return
    app.var_pk_ext = tk.StringVar(value="")
    kids = row.pack_slaves()
    after = kids[-1] if kids else None
    chip = tk.Label(row, text=" 외부 PK ", bg=EXT_COL, fg="white", font=("맑은 고딕", 8, "bold"))
    chip.pack(side="left", padx=(10, 1))
    ttk.Entry(row, textvariable=app.var_pk_ext, width=6).pack(side="left", padx=2)
    ttk.Label(row, text="V (빈칸 = 안 씀)", foreground="#666").pack(side="left", padx=(2, 0))
    if after is not None:                                   # 'V' 글 뒤에
        pass

    def load(*_):
        v = (app.stype_bias() or {}).get("pk_ext", "")
        app.var_pk_ext.set(str(v))
        refresh_panel(app, ext)
    app.ext_pk_load = load
    app.var_stype.trace_add("write", lambda *a: app.root.after(70, load))
    app.var_pk_ext.trace_add("write", lambda *a: app.root.after(50, lambda: refresh_panel(app, ext)))
    load()

    # [💾 샘플 종류 기본값으로 저장] 에 외부 PK 도 같이
    orig_save = app.save_stype_bias

    def save2():
        orig_save()
        t = app.cur_stype()
        for st in app.sample_types:
            if st["name"] == t:
                b = st.setdefault("bias", {})
                v = app.var_pk_ext.get().strip()
                if v:
                    b["pk_ext"] = v
                else:
                    b.pop("pk_ext", None)
        app.s["sample_types"] = [dict(x) for x in app.sample_types]
    app.save_stype_bias = save2


EXT_COL = "#00897b"     # 외부 PK 게이트 색 (청록)
DA_COL = "#8e24aa"      # 드라이버 앰프 전원 색 (보라)


def attach_panel(app, ext):
    """파워서플라이 화면 오른쪽: N6705C 아래에 외부 전원 앞면 (PK 게이트 · 드라이버 앰프) - 쓰는 것만 보임"""
    import tkinter as tk
    P = app.psu_panel
    try:
        mid = P.lbl_n_idn.master.master.master.master
        nw = P.lbl_n_idn.master.master.master
    except Exception:
        return
    BEZEL, LCD, DIM = "#41464d", "#0a0f14", "#5a6470"
    wrap = tk.Frame(mid)
    pnl = tk.Frame(wrap, bg=BEZEL, bd=3, relief="raised")
    pnl.pack(fill="both", expand=True, pady=(6, 0))
    hd = tk.Frame(pnl, bg=BEZEL)
    hd.pack(fill="x", padx=8, pady=(6, 2))
    lbl_model = tk.Label(hd, text="외부 전원", bg=BEZEL, fg="#e8e8e8", font=("Arial", 11, "bold"))
    lbl_model.pack(side="left")
    tk.Label(hd, text="  DC Power Supply (GPIB)", bg=BEZEL, fg="#c9cdd2", font=("Arial", 8)).pack(side="left")
    tk.Label(hd, text="외부 게이트 · 드라이버 앰프", bg=EXT_COL, fg="white", font=("맑은 고딕", 8, "bold")).pack(side="right")
    scr = tk.Frame(pnl, bg=LCD, bd=2, relief="sunken")
    scr.pack(fill="both", expand=True, padx=8, pady=(0, 4))
    keys = tk.Frame(pnl, bg=BEZEL)
    keys.pack(fill="x", padx=8, pady=(0, 8))
    cells = {}
    for i, (k, col, name) in enumerate((("pk", EXT_COL, "외부 PK 게이트"), ("da", DA_COL, "드라이버 앰프"))):
        scr.columnconfigure(i, weight=1, uniform="x")
        keys.columnconfigure(i, weight=1, uniform="x")
        cell = tk.Frame(scr, bg=LCD, highlightthickness=2, highlightbackground=col)
        top = tk.Frame(cell, bg=col)
        top.pack(fill="x")
        tk.Label(top, text=" %s " % name, bg=col, fg="white", font=("맑은 고딕", 10, "bold")).pack(side="left")
        r = {"addr": tk.Label(top, text="", bg=col, fg="white", font=("Arial", 8))}
        r["addr"].pack(side="left")
        r["o"] = tk.Label(top, text="Off", bg=col, fg="white", font=("Arial", 8, "bold"))
        r["o"].pack(side="right", padx=3)
        r["vm"] = tk.Label(cell, text="---", bg=LCD, fg=col if k == "pk" else "#ce93d8", font=("Consolas", 18, "bold"), anchor="e")
        r["vm"].pack(fill="x", padx=6, pady=(4, 0))
        r["im"] = tk.Label(cell, text="---", bg=LCD, fg="#e0e0e0", font=("Consolas", 12, "bold"), anchor="e")
        r["im"].pack(fill="x", padx=6)
        r["vs"] = tk.Label(cell, text="Set ---", bg=LCD, fg=DIM, font=("Consolas", 8), anchor="e")
        r["vs"].pack(fill="x", padx=6, pady=(0, 4))
        btn = tk.Button(keys, text="On/Off", bg="#555", fg="white", font=("Arial", 8, "bold"), bd=2, relief="raised",
                        highlightthickness=2, highlightbackground=col, activebackground="#777",
                        command=lambda k=k: toggle(k))
        r["cell"], r["btn"], r["i"] = cell, btn, i
        cells[k] = r

    def toggle(k):
        if not P.var_ctrl.get():
            P.post("log", "제어 잠금 상태 - 왼쪽 '🔓 제어 사용' 을 켜세요", "err")
            return

        def job():
            try:
                o = ext.pk_out() if k == "pk" else ext.da_out()
                on = not o.is_on()
                if k == "da":
                    ext.da_set(on, "손으로")
                else:
                    if on:
                        v = ext.pk_v()
                        if v is None:
                            P.post("log", "이 샘플 외부 PK 칸이 빈칸 - 값을 넣고 켜세요", "err")
                            return
                        o.w("VOLT", "%.3f" % v)
                    o.w("OUTP", "ON" if on else "OFF")
                    P.post("log", "외부 PK 게이트 %s" % ("ON" if on else "OFF"), "ok")
            except Exception as e:
                P.post("log", "외부 전원 On/Off 실패: %s" % e, "err")
            ext._rd_t = 0
            read()
        P.worker.tasks.put(job)

    def show(k, d):
        r = cells[k]
        if d is None:
            for x in ("vm", "im"):
                r[x].configure(text="---")
            r["vs"].configure(text="Set ---")
            r["o"].configure(text="연결 안 됨")
            r["btn"].configure(bg="#555")
            return
        on = d.get("on")
        r["vm"].configure(text="%.3f V" % d["vm"] if d.get("vm") == d.get("vm") else "---")
        r["im"].configure(text="%.4f A" % d["im"] if d.get("im") == d.get("im") else "---")
        r["vs"].configure(text="Set %.3f V · Lim %.3f A" % (d.get("vs", float("nan")), d.get("is", float("nan"))))
        r["o"].configure(text="ON" if on else "Off")
        r["btn"].configure(bg="#2e7d32" if on else "#555")

    def rd(o):
        f = lambda x: float(str(x).split(",")[0])
        d = {"on": o.is_on()}
        for k_, q in (("vm", "MEAS:VOLT?"), ("im", "MEAS:CURR?"), ("vs", "VOLT?"), ("is", "CURR?")):
            try:
                d[k_] = f(o.q(q))
            except Exception:
                d[k_] = float("nan")
        return d

    def read():
        c = ext.cfg()
        out = {}
        for k, use, addr in (("pk", bool((c.get("pk_addr") or "").strip()), c.get("pk_addr", "")),
                             ("da", bool(c.get("da_use")), c.get("da_addr", ""))):
            if not use:
                out[k] = None
                continue
            a_ = (addr or "").strip().upper()
            if a_ not in ext.dev and (time.time() - ext.fail_t.get(a_, 0) < 60
                                      or getattr(app, "sweep", None) or getattr(app, "ramp", None) or dpd_busy()):
                out[k] = None                              # 연결 안 된 장비: 60 초에 한 번만 다시 시도 · 측정 중엔 안 함 (Vdd 읽기를 막지 않게)
                continue
            try:
                out[k] = rd(ext.pk_out() if k == "pk" else ext.da_out())
            except Exception:
                ext.fail_t[a_] = time.time()
                out[k] = None
        ext.last = out
        P.post("call", lambda: ([show(k, d) for k, d in out.items()], top_row_update(app, ext)))

    orig_read = P.read_job

    def dpd_busy():
        try:
            return bool(app.dpd_running())                 # DPD 측정 중 (I/Q 받을 때마다 Vdd 전류가 필요)
        except Exception:
            return False

    def read2(full=True):
        busy = bool(getattr(app, "sweep", None) or getattr(app, "ramp", None)) or dpd_busy()
        now = time.time()
        if busy and full and now - getattr(ext, "_full_t", 0) < 3.0:
            full = False          # 자동 측정 중엔 설정값 · 출력 상태 (질문 6 개 더) 는 3 초에 한 번 → Vdd 전류를 자주 읽게
        if full:
            ext._full_t = now
        t0 = time.time()
        orig_read(full)
        dt = time.time() - t0
        if dt > 1.5 and time.time() - getattr(ext, "_slow_t", 0) > 10:      # 진단: 전원 한 번 읽는 데 오래 걸림
            ext._slow_t = time.time()
            P.post("log", "⚠ 전원 읽기 느림: E36155A · N6705C 한 번 읽는 데 %.1f 초%s (GPIB 응답 지연 · 시간 초과 의심)"
                   % (dt, " (설정값 포함)" if full else ""), "err")
        # 외부 전원 (드라이버 앰프 · 외부 PK) 읽기: 자동 측정 · 천천히 내리기 중엔 아예 안 읽음 (켜고 끄기만)
        #   E3631A 같은 외부 전원은 한 번 읽는 데 (출력 고르기 + 질문 5 개) 오래 걸려, 같은 GPIB 줄의 Vdd 전류 읽기가 밀림
        #   → Mod. 측정 '전류 못 읽음' → 파워 내려 다시 재기 (v2.5.88 ~ v2.5.103). 측정 아닐 땐 2 초에 한 번
        if busy or time.time() - getattr(ext, "_rd_t", 0) < 2.0:
            return
        ext._rd_t = time.time()
        t0 = time.time()
        try:
            read()
        except Exception:
            pass
        dt = time.time() - t0
        if dt > 1.0 and time.time() - getattr(ext, "_slow2_t", 0) > 10:
            ext._slow2_t = time.time()
            P.post("log", "⚠ 외부 전원 (드라이버 앰프 · 외부 PK) 읽기 %.1f 초 - 주소 · 연결 확인" % dt, "err")
    P.read_job = read2

    def refresh():
        c = ext.cfg()
        use = {"pk": bool((c.get("pk_addr") or "").strip()), "da": bool(c.get("da_use"))}
        for k, r in cells.items():
            if use[k]:
                r["cell"].grid(row=0, column=r["i"], sticky="nsew", padx=3, pady=6)
                r["btn"].grid(row=0, column=r["i"], padx=6, sticky="we")
                a = c.get("pk_addr" if k == "pk" else "da_addr", "")
                o = Out(None, c.get("pk_out" if k == "pk" else "da_out", ""))
                extra = ""
                if k == "pk":
                    v = app.var_pk_ext.get().strip() if hasattr(app, "var_pk_ext") else ""
                    extra = " · 이 샘플 %s" % (("%s V" % v) if v else "안 씀")
                r["addr"].configure(text="%s %s%s" % (a, o.name(), extra))
            else:
                r["cell"].grid_forget()
                r["btn"].grid_forget()
        if use["pk"] or use["da"]:
            if not wrap.winfo_ismapped():
                wrap.pack(side="top", fill="x", after=nw)
        else:
            wrap.pack_forget()
    app.ext_panel_refresh = refresh
    app.ext_panel_cells = cells
    refresh()


def refresh_panel(app, ext):
    f = getattr(app, "ext_panel_refresh", None)
    if f:
        f()
    try:
        top_row_update(app, ext)
    except Exception:
        pass


def attach_cfg_ui(app, ext):
    """'파워서플라이 연결 · SCPI' 탭: 외부 전원 (드라이버 앰프 · PK 게이트) 주소 · 출력 · 전압"""
    import tkinter as tk
    from tkinter import ttk
    P = app.psu_panel
    box = P.box_extra
    first = None
    for w in box.winfo_children():
        if "Labelframe" in w.winfo_class():
            first = w
            break
    lf = ttk.LabelFrame(box, text=" 외부 전원 (GPIB) - 드라이버 앰프 전원 · PK 게이트 ", padding=8)
    if first is not None:
        lf.pack(fill="x", padx=8, pady=(4, 4), after=first)
    else:
        lf.pack(fill="x", padx=8, pady=(4, 4))
    c = ext.cfg()
    v = dict(da_use=tk.BooleanVar(value=bool(c.get("da_use"))),
             da_addr=tk.StringVar(value=c.get("da_addr", "")), da_out=tk.StringVar(value=c.get("da_out", "") or "(단일)"),
             pk_addr=tk.StringVar(value=c.get("pk_addr", "")), pk_out=tk.StringVar(value=c.get("pk_out", "") or "(단일)"),
             pk_min=tk.StringVar(value=str(c.get("pk_min", "-6"))), pk_max=tk.StringVar(value=str(c.get("pk_max", "6"))))
    g = ttk.Frame(lf)
    g.pack(fill="x")
    ttk.Checkbutton(g, text="드라이버 앰프 전원 사용", variable=v["da_use"]).grid(row=0, column=0, sticky="w")
    ttk.Label(g, text="주소").grid(row=0, column=1, sticky="e", padx=(8, 2))
    ttk.Entry(g, textvariable=v["da_addr"], width=22).grid(row=0, column=2, sticky="we")
    ttk.Label(g, text="출력").grid(row=0, column=3, padx=(8, 2))
    ttk.Combobox(g, textvariable=v["da_out"], values=OUTS, width=7).grid(row=0, column=4)
    ttk.Label(g, text="PK 게이트 외부 전원").grid(row=1, column=0, sticky="w", pady=(4, 0))
    ttk.Label(g, text="주소").grid(row=1, column=1, sticky="e", padx=(8, 2), pady=(4, 0))
    ttk.Entry(g, textvariable=v["pk_addr"], width=22).grid(row=1, column=2, sticky="we", pady=(4, 0))
    ttk.Label(g, text="출력").grid(row=1, column=3, padx=(8, 2), pady=(4, 0))
    ttk.Combobox(g, textvariable=v["pk_out"], values=OUTS, width=7).grid(row=1, column=4, pady=(4, 0))
    lf_ = ttk.Frame(g)
    lf_.grid(row=2, column=1, columnspan=4, sticky="w", pady=(4, 0))
    ttk.Label(lf_, text="외부 PK 안전 한계").pack(side="left", padx=(8, 4))
    ttk.Entry(lf_, textvariable=v["pk_min"], width=6, justify="right").pack(side="left")
    ttk.Label(lf_, text=" ~ ").pack(side="left")
    ttk.Entry(lf_, textvariable=v["pk_max"], width=6, justify="right").pack(side="left")
    ttk.Label(lf_, text="V  (N6705C 게이트 한계와 따로 · 음수 가능 · 예 -3.5 ~ -3.0 · 이 밖으로는 안 걸음)", foreground="#666").pack(side="left", padx=(4, 0))
    g.columnconfigure(2, weight=1)
    ttk.Label(lf, text="드라이버 앰프: ON / OFF 만 (전압은 장비에 맞춰 둔 값) - POWER SUPPLY ON 때 바이어스 · 전류 맞추기 끝난 뒤 맨 마지막 ON · OFF 때 맨 먼저 OFF\n"
                       "외부 PK 게이트: 샘플 종류마다 '외부 PK V' 칸 (빈칸 = 안 씀) · 파워서플라이 화면 N6705C 아래에 앞면이 보임  ·  "
                       "출력: (단일) · E3631A = P6V/P25V/N25V · 채널 장비 = 1~4  ·  장비 없이: DEMO",
              foreground="#607d8b", justify="left").pack(anchor="w", pady=(4, 0))
    bf = ttk.Frame(lf)
    bf.pack(fill="x", pady=(6, 0))

    def apply(log=True):
        c = ext.cfg()
        try:
            lo_, hi_ = float(v["pk_min"].get()), float(v["pk_max"].get())
        except ValueError:
            P.post("log", "외부 PK 안전 한계가 숫자가 아님", "err")
            return False
        if lo_ >= hi_:
            P.post("log", "외부 PK 안전 한계: 아래 값이 위 값보다 작아야 함 (%g ~ %g)" % (lo_, hi_), "err")
            return False
        c["pk_min"], c["pk_max"] = "%g" % lo_, "%g" % hi_
        import psu_panel as PM
        for k in ("da_addr", "pk_addr"):
            a = v[k].get().strip()
            a = PM.fix_addr(a) if a and not a.upper().startswith("DEMO") else a
            if a != c.get(k, ""):
                old = c.get(k, "")
                if old and old != c.get("pk_addr" if k == "da_addr" else "da_addr", ""):
                    P.worker.tasks.put(lambda o=old: ext.drop(o))
            v[k].set(a)
        c.update(da_use=bool(v["da_use"].get()), da_addr=v["da_addr"].get().strip(), da_out=norm_out(v["da_out"].get()),
                 pk_addr=v["pk_addr"].get().strip(), pk_out=norm_out(v["pk_out"].get()))
        refresh_panel(app, ext)
        if log:
            P.post("log", "외부 전원 적용: 드라이버 앰프 %s · PK 게이트 %s" % (
                ("%s %s" % (c["da_addr"], Out(None, c["da_out"]).name()))
                if c["da_use"] else "안 씀",
                ("%s %s · 안전 %s ~ %s V" % (c["pk_addr"], Out(None, c["pk_out"]).name(), c["pk_min"], c["pk_max"])) if c["pk_addr"] else "없음"), "ok")
        return True

    import psu_panel as PM_

    def da_btn(on):
        if not apply(log=False):
            return
        if not ext.cfg().get("da_use"):
            P.post("log", "드라이버 앰프 전원 사용에 체크하세요", "err")
            return
        if on and getattr(P, "bias_state", "") != "on":
            if not PM_.messagebox.askyesno("PA Bench Monitor", "바이어스가 다 켜져 있지 않습니다.\n그래도 드라이버 앰프 전원을 켤까요?"):
                return
        P.worker.tasks.put(lambda: ext.da_set(on, "손으로"))

    ttk.Button(bf, text="적용", command=apply).pack(side="left")
    ttk.Button(bf, text="드라이버 앰프 ON", command=lambda: da_btn(True)).pack(side="left", padx=(6, 0))
    ttk.Button(bf, text="드라이버 앰프 OFF", command=lambda: da_btn(False)).pack(side="left", padx=(4, 0))
    app.ext_cfg = dict(vars=v, apply=apply)


# ---------------------------------------------------------------- Vdd 전류 이상 → 먼저 샘플 보호 (SG RF OFF · 드라이버 앰프 OFF) → 그래도 이상하면 긴급 종료
def attach_soft_fault(app, da_off):
    """측정 중 Vdd 전류 급상승 · 최대 전류 넘음 (파워서플라이 '이상 판정 2)'):
    예전: 바로 바이어스 긴급 종료 (게이트 기본값 → Vdd 0 V)
    지금: ① SG RF OFF + 측정 중지 · 드라이버 앰프 OFF (샘플 보호)
          ② 약 1 초 뒤 Vdd 전류를 다시 읽어 바이어스 목표 Idq (목표 CA + 목표 DRV) 와 비슷하면
             → 바이어스는 그대로 · '설정값을 확인하세요' 안내
          ③ SG · 드라이버 앰프를 껐는데도 전류가 이상하면 → 예전처럼 바로 긴급 종료
    게이트 이상 (kind 'n') 은 예전 그대로. psu_panel.py 는 그대로 (이 화면의 check_faults 만 감쌈)"""
    import psu_panel as PM
    P = getattr(app, "psu_panel", None)
    if P is None or getattr(P, "_soft_fault", False):
        return
    P._soft_fault = True
    orig = P.check_faults

    def cf(kind, vals):
        if kind != "e":
            return orig(kind, vals)
        if P.bias_state != "on" or P.seq_running or getattr(P, "fault_busy", False):
            return
        i = vals.get("im")
        if i is None or i != i:
            return
        ref = getattr(P, "mon_ref", i)
        pre = P.lim_prefix()
        pct = P.sqf(pre + "spike_pct", P.sqf("spike_pct", 50)) / 100.0
        dma = P.sqf(pre + "spike_ma", P.sqf("spike_ma", 100)) / 1e3
        imax = P.sqf(pre + "imax", P.sqf("imax", 1.5))
        tag = {"": "", "mod_": " (Mod. 한계)", "dpd_": " (DPD 한계)"}.get(pre, "")
        why = None
        if i > imax:
            why = "Vdd 전류가 최대 %.2f A 를 넘음 (%.3f A)%s" % (imax, i, tag)
        elif i - ref >= dma and i >= ref * (1 + pct):
            why = "Vdd 전류 급상승: %.1f → %.1f mA%s" % (ref * 1e3, i * 1e3, tag)
        if why is None:
            P.mon_ref = i
            return
        P.mon_ref = ref
        P.fault_busy = True
        msg1 = "Vdd 전류가 지정값보다 많이 올라가 샘플 보호를 위해 SG RF OFF · Driver Amp 를 끕니다"
        try:
            if P.on_fault:
                P.on_fault(why)                            # (Bench) 측정 중지 + SG RF OFF - 먼저
        except Exception:
            pass
        try:
            app.status("⚠ " + msg1 + " (" + why + ")", "#ff6b6b")
        except Exception:
            pass
        P.post("log", "⚠ %s → %s" % (why, msg1), "err")
        idq = (P.sqf("t_green", 0) + P.sqf("t_yellow", 0)) / 1e3
        tol = 0.10                                         # Idq ±10 % 안이면 '비슷함' (바이어스 그대로)

        def job():
            safe = False
            try:
                time.sleep(0.1)                            # SG RF OFF 가 먼저 가게
                try:
                    da_off()
                except Exception as ex:
                    P.post("log", "드라이버 앰프 OFF 실패: %s" % ex, "err")
                time.sleep(0.7)                            # 전류가 가라앉을 시간 (SG · 드라이버 앰프 OFF 부터 약 1 초 안에 판단)
                e = P.inst.get("e")
                xs = []
                for _ in range(2):
                    try:
                        xs.append(PM.fnum(e.query("MEAS:CURR?")))
                    except Exception:
                        pass
                    time.sleep(0.1)
                xs = [x for x in xs if x == x]
                now = sum(xs) / len(xs) if xs else float("nan")
                band = idq * tol
                safe = idq > 0 and now == now and abs(now - idq) <= band
                if safe:
                    P.mon_ref = now
                    txt = ("%s\n\n%s.\n\n지금 Vdd 전류 %.1f mA 가 바이어스 목표 Idq %.1f mA (CA + DRV) 와 비슷합니다 (±10 %%, ±%.1f mA 안)\n"
                           "→ 바이어스는 문제가 없어 긴급 종료하지 않고 그대로 둡니다.\n\n"
                           "측정 중 전류 한계가 너무 낮을 수 있습니다. 파워서플라이 설정 '이상 판정 2)' 의\n"
                           "급상승 %% · mA · 최대 Id 값을 확인해서 바꿔 주세요. (SG RF · 드라이버 앰프는 꺼진 상태)"
                           % (why, msg1, now * 1e3, idq * 1e3, band * 1e3))
                    P.post("log", "Vdd 전류 %.1f mA ≈ Idq %.1f mA → 바이어스 그대로 (긴급 종료 안 함)" % (now * 1e3, idq * 1e3), "ok")
                    P.post("call", lambda: PM.messagebox.showwarning("샘플 보호 - 바이어스는 그대로", txt))
                else:
                    why2 = "%s\nSG RF OFF · 드라이버 앰프 OFF 뒤에도 Vdd 전류 %s (바이어스 Idq %.1f mA)" % (
                        why, ("%.1f mA" % (now * 1e3)) if now == now else "못 읽음", idq * 1e3)
                    P.red(why2 + "\n\n→ 게이트 기본값으로 올리고 Vdd 를 0 V 까지 빠르게 내립니다.")
                    P.safe_down(why2)
            finally:
                P.fault_busy = False
                try:
                    P.read_job(full=True)
                except Exception:
                    pass
        P.worker.tasks.put(job)
    P.check_faults = cf



# ---------------------------------------------------------------- 맨 위 오른쪽 바이어스 칸: 외부 PK 줄 (휠 · ↑↓ · 입력 · On/Off · 읽음)
def attach_top_row(app, ext):
    import tkinter as tk
    import psu_panel as PM
    P = app.psu_panel
    rows = getattr(app, "bias_rows", None)
    if not rows or ("n", 3) not in rows:
        return
    rowsf = rows[("n", 3)]["ent"].master.master
    c = app.BT
    fr = tk.Frame(rowsf, bg=c["bg"])
    tk.Label(fr, text="외PK", bg=EXT_COL, fg="white", width=4, font=("맑은 고딕", 8, "bold")).pack(side="left", ipady=1)
    var = tk.StringVar(value="---")
    ent = tk.Entry(fr, textvariable=var, width=8, justify="right", font=("Consolas", 11, "bold"),
                   relief="flat", bd=1, highlightthickness=1, bg=c["entry"], fg=c["fg"],
                   insertbackground=c["fg"], highlightbackground="#2b3540", highlightcolor="#1e88e5")
    ent.pack(side="left", padx=(5, 3))
    dl = PM.Dial(fr, c["bg"], size=22, accent="#ff9800", on_turn=lambda d, m: nudge(d, m))
    dl.pack(side="left")
    on = tk.Button(fr, text="Off", width=3, relief="flat", bd=0, font=("맑은 고딕", 7, "bold"), command=lambda: toggle())
    on.pack(side="left", padx=(4, 4))
    cur = tk.Label(fr, text="", bg=c["bg"], fg=c["dim"], font=("Consolas", 9), anchor="w", width=15)
    cur.pack(side="left")
    st = dict(typing=False)

    def hint(t):
        try:
            app.lbl_bias_hint.configure(text=t)
        except Exception:
            pass

    def can():
        if not (ext.cfg().get("pk_addr") or "").strip():
            hint("외부 PK 주소가 없음 - 장비 설정 > 파워서플라이 연결 '외부 전원' 칸")
            return False
        if not P.var_ctrl.get():
            hint("제어 잠금 상태 - [⚡ 파워서플라이] 화면에서 '제어 사용' 체크 (POWER SUPPLY ON 하면 자동 해제)")
            return False
        return True

    def lims():
        return ext.pk_lims()

    def set_v(v):
        lo, hi = lims()
        if not (lo <= v <= hi):
            hint("외부 PK %.3f V 는 외부 PK 안전 한계 %g ~ %g V 밖 (장비 설정 > 파워서플라이 연결)" % (v, lo, hi))
            return

        def job():
            try:
                o = ext.pk_out()
                o.w("VOLT", "%.3f" % v)
                P.post("log", "외부 PK ← %.3f V" % v, "ok")
            except Exception as e:
                P.post("log", "외부 PK 설정 실패: %s" % e, "err")
            ext._rd_t = 0
            P.read_job(full=True)
        P.worker.tasks.put(job)
        hint("")

    def now_set():
        d = (ext.last or {}).get("pk") or {}
        v = d.get("vs")
        return v if isinstance(v, float) and v == v else None

    def nudge(d, m=1):
        if not can():
            return False
        v = now_set()
        if v is None:
            hint("외부 PK 설정값을 아직 못 읽었습니다")
            return False
        nv = round(v + d * 0.001 * m, 3)
        var.set("%.3f V" % nv)
        set_v(nv)
        return True

    def enter(e=None):
        if not can():
            return
        try:
            v = float(var.get().replace("V", "").strip())
        except ValueError:
            hint("숫자를 입력하세요")
            return
        set_v(v)
        app.root.focus_set()

    def toggle():
        if not can():
            return
        d = (ext.last or {}).get("pk")
        want = not (d and d.get("on"))

        def job():
            try:
                o = ext.pk_out()
                o.w("OUTP", "ON" if want else "OFF")
                P.post("log", "외부 PK 게이트 %s (손으로)" % ("ON" if want else "OFF"), "ok")
            except Exception as e:
                P.post("log", "외부 PK On/Off 실패: %s" % e, "err")
            ext._rd_t = 0
            P.read_job(full=True)
        P.worker.tasks.put(job)

    for seq_, d_ in (("<Up>", 1), ("<Down>", -1)):
        ent.bind(seq_, lambda e, d_=d_: (nudge(d_, 10 if e.state & 1 else 1), "break")[1])
    ent.bind("<MouseWheel>", lambda e: (nudge(1 if e.delta > 0 else -1, 10 if e.state & 1 else 1), "break")[1])
    ent.bind("<Button-4>", lambda e: (nudge(1, 1), "break")[1])
    ent.bind("<Button-5>", lambda e: (nudge(-1, 1), "break")[1])
    ent.bind("<Enter>", lambda e: ent.focus_set())
    ent.bind("<Return>", enter)
    app.ext_top = dict(frame=fr, var=var, ent=ent, dial=dl, on=on, cur=cur)
    top_row_update(app, ext)


def top_row_update(app, ext):
    r = getattr(app, "ext_top", None)
    if r is None:
        return
    c = app.BT
    P = app.psu_panel
    use = bool((ext.cfg().get("pk_addr") or "").strip())
    fr = r["frame"]
    if not use:
        if fr.winfo_ismapped():
            fr.pack_forget()
        return
    if not fr.winfo_ismapped():
        fr.pack(fill="x", pady=1)
    d = (ext.last or {}).get("pk")
    if app.root.focus_get() is not r["ent"]:
        vs = d.get("vs") if d else None
        r["var"].set(("%.3f V" % vs) if isinstance(vs, float) and vs == vs else "---")
    if d is None:
        r["on"].configure(text="-", bg=c["head"], fg=c["unit"])
        r["cur"].configure(text="연결 안 됨", fg=c["unit"])
    elif d.get("on"):
        r["on"].configure(text="On", bg="#2e7d32", fg="white")
        vm = d.get("vm")
        r["cur"].configure(text=("읽음 %.3f V" % vm) if isinstance(vm, float) and vm == vm else "읽음 ---", fg=c["ok"])
    else:
        r["on"].configure(text="Off", bg=c["head"], fg=c["dim"])
        r["cur"].configure(text="OFF", fg=c["dim"])
    try:
        r["dial"].set_enabled(d is not None and P.var_ctrl.get())
    except Exception:
        pass
