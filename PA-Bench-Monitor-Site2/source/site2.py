"""
site2  -  PA Bench Monitor Site2 용: Vdd 파워서플라이 2 대 (예: Keysight E3632A × 2, GPIB) 를 1 대처럼

  · 파워서플라이 화면의 'E36155A (Vdd) 주소' 칸에 두 주소를 이어서 넣음
        직렬 (24 V + 24 V = 48 V):   GPIB0::5::INSTR + GPIB0::6::INSTR
        병렬 (같은 전압, 전류 합):    GPIB0::5::INSTR || GPIB0::6::INSTR
        장비 없이 시험:              DEMO + DEMO
  · 프로그램에는 1 대 (Vdd · Id) 로 보임 → 효율 (DE · PAE) 은 두 대 전력 합으로 정확히
        DC 전력 = V1·I1 + V2·I2
        직렬: Vdd = V1 + V2,  Id = (V1·I1 + V2·I2) / Vdd   (직렬이라 두 전류는 거의 같음)
        병렬: Vdd = (V1 + V2) / 2,  Id = I1 + I2
  · 설정 (직렬): 앞 주소 장비를 먼저 24 V 까지 올리고, 그 다음 뒤 장비를 24 V 까지 → 합 48 V
        Vdd 30 V → 24 V + 6 V · 48 V → 24 V + 24 V · 한 대에 자기 최대 넘게는 절대 안 걸음 (합은 앞+뒤 최대와 합 상한 50 V 중 작은 값까지)
        내릴 땐 반대로 (뒤 장비 먼저 0 V 쪽으로)
    병렬이면 각 대에 Vdd. 전류 제한은 직렬 = 각 대 그대로, 병렬 = 반씩
  · E3632A 는 처음에 15 V 범위 (최대 15 V · 7 A) 라서 출력이 꺼져 있으면 30 V 범위 (최대 30 V · 4 A) 로 바꿔 둠

게이트 (Site2): 캐리어 = N6705C CH1 · 드라이버 = E3631A (1) +6V 출력 (반대로 연결) · 피킹 = E3631A (2) +6V 출력 (반대로 연결)
  · 'N6705C 주소' 칸에:  N6705C=GPIB0::5::INSTR, DRV=GPIB0::7::INSTR, PK=GPIB0::8::INSTR
        (장비 없이: N6705C=DEMO, DRV=DEMO, PK=DEMO · 바꿀 때: CA_CH=1 · DA_CH=3)
  · 프로그램에는 예전처럼 N6705C 4 채널로 보임: CH1 = DRV (E3631A 1) · CH2 = CA (N6705C CH1) · CH3 = PK (E3631A 2) · CH4 = N6705C CH4
  · 드라이버 앰프 전원 (N6705C CH3): 게이트를 켤 때 맨 먼저 켜고, 게이트를 다 끈 뒤 맨 마지막에 끔 (전압은 N6705C 에 미리 맞춰 둔 값)
  · E3631A 는 출력 켜기 · 끄기가 세 출력 (+6V · +25V · −25V) 같이 → 안 쓰는 출력은 0 V 로 둠 (게이트가 꺼져 있을 때)
  · 피킹 게이트 출력은 샘플 종류마다: 기본 +6V · '+25V' 로 정한 샘플만 E3631A (2) 의 +25V (다른 샘플일 땐 +25V 는 0 V)
    (맨 위 막대 [PK 게이트 +6V | +25V] · 게이트가 꺼져 있을 때만 바뀜)

psu_panel.py (바이어스 자동 순서) 는 고치지 않음: 장비를 여는 곳 (Visa.open) 만 바깥에서 감싸서 2 대를 1 대처럼 보이게 함.
"""
import math

SITE = "Site2"
CURRENT_GATE = None     # 지금 연결된 게이트 (GateMux) - 화면 (PK 출력 고르기) 에서 씀
PK_OUT = "P6V"          # 피킹 게이트에 쓸 E3631A (2) 출력: P6V (기본) · P25V (그 출력을 쓰는 샘플만)
EACH_MAX = 24.0          # 직렬 Vdd: 한 대 기본 최대 (Site2 = 24 V + 24 V = 48 V)
VSUM = 50.0             # 직렬 Vdd 합 상한 (화면에서 바꿈 · 기본 50 V)
VMAX = [24.5, 24.0]     # 직렬 Vdd: 앞 · 뒤 장비 각각 최대 (파워서플라이 화면 'Site2 Vdd 2 대' 에서 바꿈 · 예: 뒤 장비 24.5 = IR 드롭 보상)
SITE_VER = "v1.4.34.1_JSH"


def _f(s):
    try:
        return float(str(s).strip().split(",")[0])
    except ValueError:
        return float("nan")


class DualPSU:
    """두 대를 1 대 (E36155A 와 같은 SCPI) 처럼. query · write · close 만 씀 (psu_panel 이 쓰는 것)"""

    def __init__(self, a, b, mode="series", names=("A", "B"), log=None):
        self.d = [a, b]
        self.mode = mode
        self.names = names
        self.log = log or (lambda *_: None)
        self.imax = [float("nan"), float("nan")]
        self.idn = []
        for i, x in enumerate(self.d):
            try:
                self.idn.append(x.query("*IDN?").strip())
            except Exception as e:
                self.idn.append("? (%s)" % e)
        self._prep()

    # --- 처음 연결: E3632A 범위 · 최대 전류
    def _prep(self):
        for i, x in enumerate(self.d):
            idn = self.idn[i].upper()
            if "E363" in idn:                               # E3632A · E3633A · E3634A: 범위가 둘
                try:
                    if x.query("OUTP?").strip() in ("0", "OFF"):
                        x.write("VOLT:RANG P30V" if "E3632" in idn else "VOLT:RANG HIGH")
                except Exception:
                    pass
            try:
                self.imax[i] = _f(x.query("CURR? MAX"))
            except Exception:
                pass
            if self.mode == "series":                       # 이미 24 V 넘게 맞춰져 있으면 24 V 로 (안전)
                self.clamp(i)

    def clamp(self, i):
        """i 번 장비 설정이 그 장비 최대를 넘으면 최대로 내림 (안전)"""
        x = self.d[i]
        try:
            if _f(x.query("VOLT?")) > VMAX[i] + 1e-6:
                x.write("VOLT %.4f" % VMAX[i])
                self.log("Vdd %d 번 장비 설정이 %.2f V 를 넘어 %.2f V 로 내림" % (i + 1, VMAX[i], VMAX[i]))
                return True
        except Exception:
            pass
        return False

    @property
    def timeout(self):
        return getattr(self.d[0], "timeout", 3000)

    @timeout.setter
    def timeout(self, v):
        for x in self.d:
            try:
                x.timeout = v
            except Exception:
                pass

    def _both(self, q):
        return [x.query(q).strip() for x in self.d]

    def query(self, cmd):
        c = cmd.strip().upper()
        if c == "*IDN?":
            return "%s %s 2 대 (E361 호환 · %s): %s  +  %s" % (SITE, "직렬" if self.mode == "series" else "병렬",
                                                          "Vdd 합" if self.mode == "series" else "전류 합", self.idn[0], self.idn[1])
        if c.startswith("SYST:ERR"):
            r = self._both(cmd)
            for x in r:
                if not x.startswith(("+0", "0")):
                    return x
            return r[0]
        if c.startswith("MEAS:VOLT") or c.startswith("MEAS:CURR"):
            v = [_f(x) for x in self._both("MEAS:VOLT?")]
            i = [_f(x) for x in self._both("MEAS:CURR?")]
            if c.startswith("MEAS:VOLT"):
                return "%+.6E" % (v[0] + v[1] if self.mode == "series" else (v[0] + v[1]) / 2)
            if self.mode == "series":
                vs = v[0] + v[1]
                p = v[0] * i[0] + v[1] * i[1]
                return "%+.6E" % (p / vs if vs > 0.05 else (i[0] + i[1]) / 2)
            return "%+.6E" % (i[0] + i[1])
        if c.startswith("VOLT?"):
            v = [_f(x) for x in self._both(cmd)]
            return "%+.6E" % (v[0] + v[1] if self.mode == "series" else (v[0] + v[1]) / 2)
        if c.startswith("CURR?"):
            i = [_f(x) for x in self._both(cmd)]
            return "%+.6E" % (min(i) if self.mode == "series" else i[0] + i[1])
        if c.startswith("OUTP?"):
            r = self._both(cmd)
            return "1" if any(x in ("1", "ON") for x in r) else "0"      # 한 대라도 켜져 있으면 켜짐 (끌 때 둘 다 끄게)
        return self.d[0].query(cmd)

    def write(self, cmd):
        c = cmd.strip()
        cu = c.upper()
        parts = cu.split()
        if cu.startswith("VOLT ") and len(parts) > 1 and not cu.startswith("VOLT:"):
            v = _f(parts[1])
            if self.mode == "series":
                ma, mb = VMAX[0], VMAX[1]
                v = max(0.0, min(v, ma + mb, VSUM))
                va = min(v, ma)                              # 앞 장비 먼저 그 최대 (예: 24 V) 까지
                vb = min(max(v - ma, 0.0), mb)               # 그 다음 뒤 장비 (예: 24.5 V 까지)
                try:
                    up = v >= _f(self.query("VOLT?")) - 1e-6
                except Exception:
                    up = True
                order = [(0, va), (1, vb)] if up else [(1, vb), (0, va)]   # 올릴 땐 앞 → 뒤 · 내릴 땐 뒤 → 앞
                for j, (k, vv) in enumerate(order):
                    try:
                        old = _f(self.d[k].query("VOLT?"))
                    except Exception:
                        old = float("nan")
                    self.d[k].write("VOLT %.4f" % vv)
                    if j == 0 and abs(vv - old) > 1.0 and abs(order[1][1] - _f(self.d[order[1][0]].query("VOLT?"))) > 1e-3:
                        time.sleep(0.4)                     # 앞 장비가 다 올라간 (내려간) 뒤 다음 장비
                return
            for x in self.d:
                x.write("VOLT %.4f" % v)
            return
        if cu.startswith("CURR ") and len(parts) > 1 and not cu.startswith("CURR:"):
            a = _f(parts[1])
            each = a if self.mode == "series" else a / 2
            for k, x in enumerate(self.d):
                m = self.imax[k]
                x.write("CURR %.4f" % (min(each, m) if m == m and m > 0 else each))
            return
        if cu.startswith("OUTP"):
            on = len(parts) > 1 and parts[1] in ("ON", "1")
            order = self.d if on else list(reversed(self.d))
            for x in order:                                 # 켤 땐 A → B, 끌 땐 B → A (거의 동시에)
                x.write("OUTP %s" % ("ON" if on else "OFF"))
            return
        for x in self.d:                                    # *CLS 등: 둘 다
            x.write(cmd)

    def close(self):
        for x in self.d:
            try:
                x.close()
            except Exception:
                pass


import re
import time


def _chs(cmd):
    """'(@1,2,3)' · '(@1:4)' → [1, 2, 3]"""
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


class Demo3631:
    """가상 E3631A (+6V · +25V · −25V, 출력 켜기는 세 출력 같이)"""

    def __init__(self):
        self.out = False
        self.sel = "P6V"
        self.v = {"P6V": 0.0, "P25V": 3.0, "N25V": -2.0}
        self.i = {"P6V": 1.0, "P25V": 1.0, "N25V": 1.0}

    def query(self, cmd):
        import random
        c = cmd.strip().upper()
        if c == "*IDN?":
            return "HEWLETT-PACKARD,E3631A,0,2.1-5.0-1.0 (DEMO)"
        if c.startswith("SYST:ERR"):
            return '+0,"No error"'
        if c.startswith("OUTP?"):
            return "1" if self.out else "0"
        if c.startswith("INST:SEL?") or c.startswith("INST?"):
            return self.sel
        o = c.split()[-1] if c.startswith("MEAS") and " " in c else self.sel
        if c.startswith("MEAS:VOLT"):
            return "%+.6E" % ((self.v[o] + random.uniform(-0.0005, 0.0005)) if self.out else 0.0)
        if c.startswith("MEAS:CURR"):
            return "%+.6E" % (random.uniform(0, 2e-4) if self.out else 0.0)
        if c.startswith("VOLT?"):
            return "%+.6E" % self.v[self.sel]
        if c.startswith("CURR?"):
            return "%+.6E" % self.i[self.sel]
        return "0"

    def write(self, cmd):
        c = cmd.strip().upper()
        val = c.split()[1] if " " in c else ""
        if c.startswith("OUTP"):
            self.out = val in ("ON", "1")
        elif c.startswith("INST:SEL") or c.startswith("INST "):
            self.sel = val
        elif c.startswith("VOLT ") and not c.startswith("VOLT:"):
            self.v[self.sel] = float(val)
        elif c.startswith("CURR ") and not c.startswith("CURR:"):
            self.i[self.sel] = float(val)

    def close(self):
        pass


class GateMux:
    """게이트 3 곳 (N6705C · E3631A × 2) 을 N6705C 1 대 (4 채널) 처럼.
    가상 CH1 = DRV (E3631A 1 · +6V) · CH2 = CA (N6705C ca_ch) · CH3 = PK (E3631A 2 · +6V) · CH4 = N6705C CH4
    드라이버 앰프 전원 N6705C da_ch: 바이어스 다 켜고 전류 맞춘 뒤 맨 마지막에 ON · 끌 때 맨 먼저 OFF (attach_da_order)"""
    OUT = "P6V"

    def __init__(self, n, drv, pk, ca_ch=1, da_ch=3, log=None):
        self.n, self.drv, self.pk = n, drv, pk
        self.ca_ch, self.da_ch = int(ca_ch), int(da_ch)
        self.log = log or (lambda *_: None)
        free = [c for c in (4, 2, 1, 3) if c not in (self.ca_ch, self.da_ch)]
        self.map = {1: ("e", drv), 2: ("n", self.ca_ch), 3: ("e", pk), 4: ("n", free[0] if free else None)}
        self.outs = {id(drv): self.OUT, id(pk): PK_OUT if PK_OUT in ("P6V", "P25V") else self.OUT}
        self.idn = {}
        for nm, d in (("N6705C", n), ("DRV", drv), ("PK", pk)):
            try:
                self.idn[nm] = d.query("*IDN?").strip()
            except Exception as e:
                self.idn[nm] = "? (%s)" % e
        for d in (drv, pk):                                 # 안 쓰는 출력은 0 V (출력이 꺼져 있을 때만)
            self._zero_unused(d)

    def out_of(self, d):
        return self.outs.get(id(d), self.OUT)

    def _zero_unused(self, d):
        try:
            if d.query("OUTP?").strip() in ("0", "OFF"):
                for o in ("P6V", "P25V", "N25V"):
                    if o != self.out_of(d):
                        d.write("INST:SEL %s" % o)
                        d.write("VOLT 0")
            d.write("INST:SEL %s" % self.out_of(d))
        except Exception:
            pass

    def set_pk_out(self, out):
        """피킹 게이트 출력 바꾸기 (P6V · P25V). 게이트가 하나라도 켜져 있으면 안 바꿈 → False"""
        if out not in ("P6V", "P25V"):
            return False
        if self.out_of(self.pk) == out:
            return True
        if self._gates_on() or self.pk.query("OUTP?").strip() in ("1", "ON"):
            return False
        old = self.out_of(self.pk)
        try:                                               # 새 출력은 지금 쓰던 값으로 · 안 쓰는 출력은 0 V
            self.pk.write("INST:SEL %s" % old)
            v = _f(self.pk.query("VOLT?"))
            i = _f(self.pk.query("CURR?"))
        except Exception:
            v = i = float("nan")
        self.outs[id(self.pk)] = out
        if v == v:
            self.pk.write("INST:SEL %s" % out)
            self.pk.write("VOLT %.3f" % v)
            if i == i:
                self.pk.write("CURR %.4f" % i)
        self._zero_unused(self.pk)
        return True

    @property
    def timeout(self):
        return getattr(self.n, "timeout", 3000)

    @timeout.setter
    def timeout(self, v):
        for d in (self.n, self.drv, self.pk):
            try:
                d.timeout = v
            except Exception:
                pass

    # 가상 장비 (데모 Vdd 전류 계산용): out · vset
    @property
    def out(self):
        return {c: self._one("OUTP?", c) in ("1", "ON") for c in (1, 2, 3, 4)}

    @property
    def vset(self):
        return {c: _f(self._one("VOLT?", c)) for c in (1, 2, 3, 4)}

    def _one(self, head, c):
        kind, x = self.map.get(c, ("n", None))
        if x is None:
            return "0" if head.startswith("OUTP") else "+0.000000E+00"
        if kind == "n":
            return self.n.query("%s (@%d)" % (head, x)).strip()
        if head.startswith("OUTP"):
            return x.query("OUTP?").strip()
        if head.startswith("MEAS:"):
            return x.query("%s %s" % (head, self.out_of(x))).strip()
        x.write("INST:SEL %s" % self.out_of(x))
        return x.query(head).strip()

    def query(self, cmd):
        c = cmd.strip().upper()
        if c == "*IDN?":
            return "Site2 게이트 (N67 호환 · CH1 DRV=E3631A +6V · CH2 CA=N6705C CH%d · CH3 PK=E3631A %s): %s | %s | %s" % (
                self.ca_ch, "+25V" if self.out_of(self.pk) == "P25V" else "+6V", self.idn["N6705C"], self.idn["DRV"], self.idn["PK"])
        if c.startswith("SYST:ERR"):
            for d in (self.n, self.drv, self.pk):
                r = d.query(cmd).strip()
                if not r.startswith(("+0", "0")):
                    return r
            return '+0,"No error"'
        if c.startswith("SYST:CHAN:COUN"):
            return "4"
        chs = _chs(c)
        if c.startswith("SYST:CHAN:MOD"):
            nm = []
            for ch in chs or [1, 2, 3, 4]:
                kind, x = self.map[ch]
                if kind == "e":
                    nm.append("E3631A %s" % {"P6V": "+6V", "P25V": "+25V"}.get(self.out_of(x), self.out_of(x)))
                elif x is None:
                    nm.append("-")
                else:
                    try:
                        nm.append(self.n.query("SYST:CHAN:MOD? (@%d)" % x).strip().strip('"'))
                    except Exception:
                        nm.append("N6705C CH%d" % x)
            return ",".join(nm)
        if chs is None:
            return self.n.query(cmd)
        head = c.split()[0]
        return ",".join(self._one(head, ch) for ch in chs)

    def da_on(self):
        try:
            return self.n.query("OUTP? (@%d)" % self.da_ch).strip() in ("1", "ON")
        except Exception:
            return False

    def da_set(self, on, why=""):
        """드라이버 앰프 전원 (N6705C da_ch) ON / OFF"""
        if self.da_on() == bool(on):
            return
        self.n.write("OUTP %s,(@%d)" % ("ON" if on else "OFF", self.da_ch))
        self.log("드라이버 앰프 전원 %s (N6705C CH%d)%s" % ("ON" if on else "OFF", self.da_ch, " - " + why if why else ""))
        time.sleep(0.2)

    def _gates_on(self):
        return any(self._one("OUTP?", c) in ("1", "ON") for c in (1, 2, 3))

    def write(self, cmd):
        c = cmd.strip()
        cu = c.upper()
        chs = _chs(cu)
        if chs is None:
            self.n.write(cmd)
            return
        head = cu.split()[0]
        val = cu.split()[1].split(",")[0] if " " in cu else ""
        if head == "OUTP":
            on = val in ("ON", "1")
            for ch in chs:
                kind, x = self.map.get(ch, ("n", None))
                if x is None:
                    continue
                if kind == "n":
                    self.n.write("OUTP %s,(@%d)" % ("ON" if on else "OFF", x))
                else:
                    x.write("OUTP %s" % ("ON" if on else "OFF"))
            if not on and self.da_on() and not self._gates_on():   # 안전: 게이트가 다 꺼졌는데 남아 있으면 OFF
                self.da_set(False, "게이트가 다 꺼짐 (안전)")
            return
        if head in ("VOLT", "CURR"):
            for ch in chs:
                kind, x = self.map.get(ch, ("n", None))
                if x is None:
                    continue
                if kind == "n":
                    self.n.write("%s %s,(@%d)" % (head, val, x))
                else:
                    x.write("INST:SEL %s" % self.out_of(x))
                    x.write("%s %s" % (head, val))
            return
        # 그 밖에 채널 붙은 명령: N6705C 채널만 바꿔서
        for ch in chs:
            kind, x = self.map.get(ch, ("n", None))
            if kind == "n" and x is not None:
                self.n.write(re.sub(r"\(@[^)]*\)", "(@%d)" % x, c))

    def close(self):
        for d in (self.n, self.drv, self.pk):
            try:
                d.close()
            except Exception:
                pass


def gate_addr(res):
    """'N6705C=GPIB0::5::INSTR, DRV=GPIB0::7::INSTR, PK=GPIB0::8::INSTR [, CA_CH=1, DA_CH=3]' → dict · 아니면 None"""
    r = (res or "").strip()
    if "=" not in r:
        return None
    d = {}
    for part in re.split(r"[,;]", r):
        if "=" in part:
            k, v = part.split("=", 1)
            d[k.strip().upper()] = v.strip()
    if not all(k in d for k in ("N6705C", "DRV", "PK")):
        return None
    return d


def split_addr(res):
    r = (res or "").strip()
    for sep, mode in (("||", "parallel"), ("+", "series")):
        if sep in r:
            parts = [p.strip() for p in r.split(sep) if p.strip()]
            if len(parts) == 2:
                return parts, mode
    return None, None


def install():
    """psu_panel 의 장비 열기를 감쌈: 주소에 '+' (직렬) · '||' (병렬) 이 있으면 2 대를 1 대로"""
    import psu_panel as P
    if getattr(P.Visa, "_site2", False):
        return
    orig_open = P.Visa.open

    def one(self, a, kind, timeout_ms):
        if a.upper().startswith("DEMO"):
            if kind == "3631":
                return Demo3631()
            d = P.DemoInst("N6705C")
            d.out = {1: False, 2: False, 3: False, 4: False}      # 데모: 처음엔 다 꺼짐 (실제 장비처럼)
            d.vset[3] = 5.0                                         # CH3 = 드라이버 앰프 전원 (데모 값)
            return d
        return orig_open(self, P.fix_addr(a), timeout_ms)

    def open2(self, res, timeout_ms=3000):
        g = gate_addr(res)
        if g is not None:
            opened = []
            try:
                n = one(self, g["N6705C"], "n", timeout_ms)
                opened.append(n)
                drv = one(self, g["DRV"], "3631", timeout_ms)
                opened.append(drv)
                pk = one(self, g["PK"], "3631", timeout_ms)
                opened.append(pk)
            except Exception:
                for x in opened:
                    try:
                        x.close()
                    except Exception:
                        pass
                raise
            gm = GateMux(n, drv, pk, int(g.get("CA_CH", 1) or 1), int(g.get("DA_CH", 3) or 3))
            global CURRENT_GATE
            CURRENT_GATE = gm
            self.demo = getattr(self, "demo", {})
            self.demo["N6705C"] = gm
            for v_ in getattr(self, "demo_vdd", []):          # 데모 Vdd 전류가 게이트를 따라가게
                v_.peer = gm
            return gm
        parts, mode = split_addr(res)
        if parts is None:
            return orig_open(self, res, timeout_ms)
        insts = []
        try:
            for i, a in enumerate(parts):
                if a.upper().startswith("DEMO"):
                    d = P.DemoInst("E36155A")
                    d.ev = 24.0 if mode == "series" else 48.0
                    d.ei = 3.0 if mode == "series" else 1.5
                    if getattr(self, "demo", {}).get("N6705C") is not None:
                        d.peer = self.demo["N6705C"]
                    self.demo = getattr(self, "demo", {})
                    self.demo["E36155A"] = d
                    self.demo_vdd = getattr(self, "demo_vdd", []) + [d]
                    insts.append(d)
                else:
                    insts.append(orig_open(self, P.fix_addr(a), timeout_ms))
        except Exception:
            for x in insts:
                try:
                    x.close()
                except Exception:
                    pass
            raise
        return DualPSU(insts[0], insts[1], mode)

    P.Visa.open = open2
    P.Visa._site2 = True
    # 주소 칸 자동 고침이 '+' · '||' 를 망가뜨리지 않게: 각 주소만 고침
    orig_fix = P.fix_addr

    def fix2(a):
        if gate_addr(a) is not None:                      # 게이트 여러 대 주소는 그대로
            return a.strip()
        parts, mode = split_addr(a)
        if parts is None:
            return orig_fix(a)
        return (" + " if mode == "series" else " || ").join(orig_fix(p) for p in parts)
    P.fix_addr = fix2


# ---------------------------------------------------------------- 화면: 피킹 게이트 출력 (샘플 종류마다)
def attach_ui(app):
    """맨 위 막대 [샘플] 옆에 [PK 게이트 +6V | +25V]: 샘플 종류마다 기억 · 게이트가 꺼져 있을 때만 장비에 바로 적용"""
    import tkinter as tk
    global PK_OUT
    try:
        parent = app.cb_stype_top.master
    except AttributeError:
        return
    TB = parent.cget("bg")
    app.var_pk_out = tk.StringVar(value="P6V")
    f = tk.Frame(parent, bg=TB)
    f.pack(side="left", padx=(8, 0))
    tk.Label(f, text="PK 게이트", bg=TB, fg="#90a4ae", font=("맑은 고딕", 9)).pack(side="left", padx=(0, 4))
    btns = {}
    for val, txt in (("P6V", "+6V"), ("P25V", "+25V")):
        b = tk.Radiobutton(f, text=txt, value=val, variable=app.var_pk_out, indicatoron=0, bd=0, relief="flat",
                           font=("맑은 고딕", 9, "bold"), padx=8, pady=2, cursor="hand2",
                           bg="#263340", fg="#e0f2f1", selectcolor="#1e88e5", activebackground="#33475a", activeforeground="white",
                           command=lambda: pick(user=True))
        b.pack(side="left", padx=(0, 1))
        btns[val] = b

    def saved():
        return (app.s.get("site2_pk_out") or {}).get(app.cur_stype() or "", "P6V")

    def show(val):
        app.var_pk_out.set(val)
        for k_, b_ in btns.items():
            b_.configure(fg="white" if k_ == val else "#90a4ae")

    def pick(user=False):
        global PK_OUT
        val = app.var_pk_out.get() if user else saved()
        st = app.cur_stype() or ""
        gm = CURRENT_GATE
        P = app.psu_panel
        if gm is None:                                     # 아직 연결 전: 연결할 때 이 출력으로
            PK_OUT = val
            if user:
                app.s.setdefault("site2_pk_out", {})[st] = val
            show(val)
            return

        def job():
            global PK_OUT
            ok = gm.set_pk_out(val)
            if ok:
                PK_OUT = val
                if user:
                    app.s.setdefault("site2_pk_out", {})[st] = val
                P.post("log", "피킹 게이트 출력: E3631A (2) %s%s" % ("+25V" if val == "P25V" else "+6V",
                                                               " (샘플 '%s')" % st if st else ""), "ok")
                P.post("call", lambda: show(val))
            else:
                now = gm.out_of(gm.pk)
                P.post("log", "피킹 게이트 출력을 못 바꿈 - 게이트가 켜져 있음 (바이어스 OFF 뒤에 바꾸세요). 지금 %s"
                       % ("+25V" if now == "P25V" else "+6V"), "err")
                P.post("call", lambda: show(now))
        P.worker.tasks.put(job)

    app.site2_pk_pick = pick
    app.var_stype.trace_add("write", lambda *a: app.root.after(50, pick))     # 샘플 종류 바꾸면 그 샘플의 PK 출력
    show(saved())
    PK_OUT = saved()


def attach_vdd_ui(app):
    """파워서플라이 화면 (설정 쪽) 아래에 'Site2 Vdd 2 대 (직렬)': 앞 · 뒤 장비 최대 V (예: 24 · 24.5)"""
    import tkinter as tk
    from tkinter import ttk
    global VMAX, VSUM
    P = getattr(app, "psu_panel", None)
    if P is None:
        return
    box = P.box_left
    first = app.s.get("site2_vmax_v") != 2                # v1.4.3: 지금 장비 = 앞 24.5 · 뒤 24 V 로 한 번 맞춤
    sv = [24.5, 24.0] if first else (app.s.get("site2_vmax") or [24.5, 24.0])
    try:
        VMAX = [float(sv[0]), float(sv[1])]
    except (TypeError, ValueError, IndexError):
        VMAX = [24.5, 24.0]
    if first:
        app.s["site2_vmax"] = list(VMAX)
        app.s["site2_vmax_v"] = 2
    try:
        VSUM = float(app.s.get("site2_vsum", 50.0))
    except (TypeError, ValueError):
        VSUM = 50.0
    lf = ttk.LabelFrame(box, text=" Site2 Vdd 2 대 (직렬) ", padding=8)
    lf.pack(fill="x", padx=6, pady=(10, 6))
    va = tk.StringVar(value="%g" % VMAX[0])
    vb = tk.StringVar(value="%g" % VMAX[1])
    vs = tk.StringVar(value="%g" % VSUM)
    r = ttk.Frame(lf)
    r.pack(fill="x")
    ttk.Label(r, text="1 번 (앞) Vdd").grid(row=0, column=0, sticky="w")
    ea = ttk.Entry(r, textvariable=va, width=7, justify="right")
    ea.grid(row=0, column=1, padx=4)
    la = ttk.Label(r, text="V  (먼저 올림)")
    la.grid(row=0, column=2, sticky="w")
    ttk.Label(r, text="2 번 (뒤) Vdd").grid(row=1, column=0, sticky="w", pady=(3, 0))
    eb = ttk.Entry(r, textvariable=vb, width=7, justify="right")
    eb.grid(row=1, column=1, padx=4, pady=(3, 0))
    lb = ttk.Label(r, text="V  (그 다음)")
    lb.grid(row=1, column=2, sticky="w", pady=(3, 0))
    ttk.Label(r, text="합 상한").grid(row=2, column=0, sticky="w", pady=(3, 0))
    es = ttk.Entry(r, textvariable=vs, width=7, justify="right")
    es.grid(row=2, column=1, padx=4, pady=(3, 0))
    ttk.Label(r, text="V  (두 대 합 · 기본 50)").grid(row=2, column=2, sticky="w", pady=(3, 0))
    info = ttk.Label(lf, text="", foreground="#607d8b", wraplength=380, justify="left")
    info.pack(anchor="w", fill="x", pady=(6, 0))
    info.bind("<Configure>", lambda e: info.configure(wraplength=max(200, e.width - 8)))

    def top_v():
        return round(min(VMAX[0] + VMAX[1], VSUM), 4)

    def show(*_):
        parts, _m = split_addr(P.var_e.get())
        a_, b_ = (parts if parts else ("", ""))
        la.configure(text="V  (먼저 올림 · %s)" % (a_ or "주소 앞"))
        lb.configure(text="V  (그 다음 · %s)" % (b_ or "주소 뒤"))
        top = top_v()
        try:
            tgt = float(P.sq["vdd"].get())
        except Exception:
            tgt = top
        if abs(tgt - top) > 1e-6:
            sa = min(tgt, VMAX[0])
            info.configure(foreground="#e65100",
                           text="⚠ 바이어스 Vdd 목표 %.3f V ≠ 합 %.3f V → 앞 %.3f · 뒤 %.3f V 로 걸림 ([적용] 하면 맞춤)"
                                % (tgt, top, sa, max(min(tgt - VMAX[0], VMAX[1]), 0.0)))
        else:
            info.configure(foreground="#607d8b",
                           text="Vdd = 1 번 %.3f + 2 번 %.3f = %.3f V → 바이어스 Vdd 목표 (자동)" % (VMAX[0], VMAX[1], top))

    def sync_target():
        try:
            if abs(float(P.sq["vdd"].get()) - top_v()) <= 1e-6:
                return
        except Exception:
            pass
        try:
            P.sq["vdd"].set("%g" % top_v())                 # 바이어스 순서의 Vdd 목표 = 앞 + 뒤 (딱 그 값으로)
        except Exception:
            pass

    def apply():
        global VMAX, VSUM
        try:
            a, b, t = float(va.get()), float(vb.get()), float(vs.get())
        except ValueError:
            P.post("log", "Site2 Vdd 최대: 숫자를 넣으세요", "err")
            return
        if not (0 < a <= 30 and 0 < b <= 30):
            P.post("log", "Site2 Vdd 최대는 0 ~ 30 V (E3632A 30 V 범위)", "err")
            va.set("%g" % VMAX[0])
            vb.set("%g" % VMAX[1])
            return
        if not (0 < t <= 60):
            P.post("log", "Site2 Vdd 합 상한은 0 ~ 60 V", "err")
            vs.set("%g" % VSUM)
            return
        if a + b > t + 1e-9:
            P.post("log", "Site2 Vdd: 1 번 %.3f + 2 번 %.3f = %.3f V 가 합 상한 %.3f V 를 넘음 → 합 상한을 올리거나 값을 낮추세요"
                   % (a, b, a + b, t), "err")
            try:
                app.root.bell()
            except Exception:
                pass
            return
        VMAX = [a, b]
        VSUM = t
        app.s["site2_vsum"] = t
        app.s["site2_vmax"] = [a, b]
        sync_target()
        show()
        e = P.inst.get("e")
        if isinstance(e, DualPSU):                          # 지금 설정이 새 최대를 넘으면 바로 내림
            def job():
                for i in (0, 1):
                    e.clamp(i)
            P.worker.tasks.put(job)
        P.post("log", "Site2 Vdd: 앞 %.3f V · 뒤 %.3f V → 바이어스 Vdd 목표 %.3f V (합 상한 %.2f V)"
               % (a, b, top_v(), t), "ok")
    ttk.Button(r, text="적용", command=apply).grid(row=0, column=3, rowspan=3, padx=(8, 0), sticky="ns")
    for w_ in (ea, eb, es):                               # Enter 로도 적용
        w_.bind("<Return>", lambda e: apply())

    # 바이어스 순서의 'Vdd 목표' 칸 = 1 번 + 2 번 (Site2 에서는 여기 두 칸이 정함 · 그 칸은 읽기 전용)
    def lock_target():
        try:
            tv = str(P.sq["vdd"])
        except Exception:
            return
        stack = [P.box_left]
        while stack:
            w_ = stack.pop()
            for c in w_.winfo_children():
                try:
                    if c.winfo_class() in ("TEntry", "Entry") and str(c.cget("textvariable")) == tv:
                        c.configure(state="readonly")
                except Exception:
                    pass
                stack.append(c)

    def follow(*_):                                       # 샘플 기본값을 불러와 목표가 바뀌어도 → 다시 합으로
        app.root.after(80, lambda: (sync_target(), show()))
    lock_target()
    try:
        P.sq["vdd"].trace_add("write", follow)
        P.var_e.trace_add("write", lambda *a: app.root.after(50, show))
    except Exception:
        pass
    sync_target()                                         # 바이어스 Vdd 목표 = 1 번 + 2 번
    show()


# ---------------------------------------------------------------- 화면: '파워서플라이 연결 · SCPI' 탭에서 주소 직접 입력
def attach_addr_ui(app):
    """장비 찾기 (VISA) 목록 아래에 Site2 주소 칸 (직접 입력 · 목록 더블클릭 → 넣을 칸 고르기) + 연결 버튼"""
    import tkinter as tk
    from tkinter import ttk
    P = getattr(app, "psu_panel", None)
    if P is None:
        return
    box = P.box_extra
    first = None
    for w in box.winfo_children():
        if "Labelframe" in w.winfo_class():
            first = w
            break
    lf = ttk.LabelFrame(box, text=" Site2 주소 (직접 입력 · 위 목록 더블클릭 = 넣을 칸 고르기) ", padding=8)
    if first is not None:
        lf.pack(fill="x", padx=8, pady=(4, 4), after=first)
    else:
        lf.pack(fill="x", padx=8, pady=(4, 4))
    v = {k: tk.StringVar() for k in ("va", "vb", "n", "drv", "pk", "ca", "da")}
    mode = tk.StringVar(value="series")
    slots = [("va", "Vdd 앞 장비 (먼저 올림)"), ("vb", "Vdd 뒤 장비"), ("n", "N6705C (CA · 드라이버 앰프)"),
             ("drv", "E3631A - DRV 게이트"), ("pk", "E3631A - PK 게이트")]

    def load(*_):
        parts, md = split_addr(P.var_e.get())
        if parts:
            v["va"].set(parts[0])
            v["vb"].set(parts[1])
            mode.set(md)
        elif P.var_e.get().strip():
            v["va"].set(P.var_e.get().strip())
            v["vb"].set("")
        g = gate_addr(P.var_n.get())
        if g:
            v["n"].set(g["N6705C"])
            v["drv"].set(g["DRV"])
            v["pk"].set(g["PK"])
            v["ca"].set(g.get("CA_CH", "1"))
            v["da"].set(g.get("DA_CH", "3"))
        else:
            if P.var_n.get().strip():
                v["n"].set(P.var_n.get().strip())
            v["ca"].set(v["ca"].get() or "1")
            v["da"].set(v["da"].get() or "3")

    g_ = ttk.Frame(lf)
    g_.pack(fill="x")
    for r_, (k, txt) in enumerate(slots):
        ttk.Label(g_, text=txt).grid(row=r_, column=0, sticky="w", pady=2)
        ttk.Entry(g_, textvariable=v[k], width=30).grid(row=r_, column=1, sticky="we", padx=6, pady=2)
    g_.columnconfigure(1, weight=1)
    mf = ttk.Frame(g_)
    mf.grid(row=0, column=2, rowspan=2, sticky="w")
    ttk.Radiobutton(mf, text="직렬 (+)", value="series", variable=mode).pack(anchor="w")
    ttk.Radiobutton(mf, text="병렬 (||)", value="parallel", variable=mode).pack(anchor="w")
    cf = ttk.Frame(g_)
    cf.grid(row=2, column=2, rowspan=3, sticky="w")
    ttk.Label(cf, text="CA 채널").grid(row=0, column=0, sticky="w")
    ttk.Entry(cf, textvariable=v["ca"], width=3, justify="center").grid(row=0, column=1, padx=4)
    ttk.Label(cf, text="드라이버 앰프 채널").grid(row=1, column=0, sticky="w")
    ttk.Entry(cf, textvariable=v["da"], width=3, justify="center").grid(row=1, column=1, padx=4)
    ttk.Label(lf, text="Vdd 뒤 장비를 비우면 1 대로 연결 · 예: GPIB0::8::INSTR (앞) / GPIB0::9::INSTR (뒤) · 장비 없이: DEMO",
              foreground="#607d8b").pack(anchor="w", pady=(4, 0))
    bf = ttk.Frame(lf)
    bf.pack(fill="x", pady=(6, 0))

    import psu_panel as PM

    def fix(a):
        return PM.fix_addr(a) if a.strip() else ""

    def apply(log=True):
        a, b = fix(v["va"].get()), fix(v["vb"].get())
        if a and b:
            P.var_e.set(a + (" + " if mode.get() == "series" else " || ") + b)
        elif a or b:
            P.var_e.set(a or b)
        n, d, p = fix(v["n"].get()), fix(v["drv"].get()), fix(v["pk"].get())
        if n and d and p:
            ca = (v["ca"].get().strip() or "1")
            da = (v["da"].get().strip() or "3")
            P.var_n.set("N6705C=%s, DRV=%s, PK=%s, CA_CH=%s, DA_CH=%s" % (n, d, p, ca, da))
        elif n and not (d or p):
            P.var_n.set(n)
        elif log:
            P.post("log", "Site2 주소: 게이트는 N6705C · DRV · PK 세 칸을 모두 넣으세요 (N6705C 만 쓰면 둘은 비우기)", "err")
            return False
        try:
            app.s["psu_e_addr"] = P.var_e.get().strip()
            app.s["psu_n_addr"] = P.var_n.get().strip()
        except Exception:
            pass
        if log:
            P.post("log", "Site2 주소 적용: Vdd = %s · 게이트 = %s" % (P.var_e.get(), P.var_n.get()), "ok")
        return True

    def conn(k):
        if P.inst.get(k) is None and not apply(log=False):
            P.post("log", "Site2 주소: 게이트 칸 (N6705C · DRV · PK) 을 확인하세요", "err")
            return
        P.connect(k)

    ttk.Button(bf, text="주소 적용", command=apply).pack(side="left")
    ttk.Button(bf, text="Vdd 연결 / 해제", command=lambda: conn("e")).pack(side="left", padx=6)
    ttk.Button(bf, text="게이트 연결 / 해제", command=lambda: conn("n")).pack(side="left")
    ttk.Button(bf, text="되돌리기 (지금 주소 다시 읽기)", command=load).pack(side="left", padx=6)

    # 위 목록 더블클릭: 넣을 칸 고르기 (가운데 작은 메뉴)
    def pick(event=None):
        it = P.found.focus()
        if not it:
            return "break"
        res = P.found.item(it, "values")[0]
        m = tk.Menu(app.root, tearoff=0)
        m.add_command(label="%s → 어느 칸에?" % res, state="disabled")
        m.add_separator()
        for k, txt in slots:
            m.add_command(label=txt, command=lambda k=k: (v[k].set(res), apply(log=False)
                                                         if k in ("va", "vb") else None))
        root = app.root
        root.update_idletasks()
        x = root.winfo_rootx() + root.winfo_width() // 2 - 120
        y = root.winfo_rooty() + root.winfo_height() // 2 - 80
        try:
            m.tk_popup(x, y)
        finally:
            m.grab_release()
        return "break"
    try:
        P.found.bind("<Double-1>", pick)
    except Exception:
        pass
    load()
    app.site2_addr = dict(vars=v, mode=mode, apply=apply, load=load, pick=pick)


# ---------------------------------------------------------------- 드라이버 앰프 전원 순서 (Site2 만)
def attach_da_order(app):
    """POWER SUPPLY ON: 게이트 · Vdd · 전류 맞추기 다 끝난 뒤 드라이버 앰프 전원 ON (그 다음에 측정 · RF)
    POWER SUPPLY OFF · 이상 처리: 드라이버 앰프 전원을 맨 먼저 OFF → 그 다음 원래 끄는 순서
    psu_panel.py 는 그대로 두고, 이 화면 (인스턴스) 의 post · run_seq · safe_down 만 감쌈"""
    P = getattr(app, "psu_panel", None)
    if P is None:
        return

    def gm():
        g = P.inst.get("n")
        return g if isinstance(g, GateMux) else None

    def da(on, why):
        g = gm()
        if g is None:
            return
        try:
            g.da_set(on, why)
            P.q.put(("log", "Site2 드라이버 앰프 전원 %s (N6705C CH%d) - %s" % ("ON" if on else "OFF", g.da_ch, why),
                     "ok" if on else ""))
        except Exception as ex:
            P.q.put(("log", "Site2 드라이버 앰프 전원 %s 실패: %s" % ("ON" if on else "OFF", ex), "err"))

    orig_post = P.post

    def post2(*msg):
        if msg and msg[0] == "bias_done" and getattr(P, "bias_state", "") == "on":
            da(True, "바이어스 · 전류 맞추기 끝난 뒤 마지막")      # 순서 쓰레드에서 바로 → 측정 (RF) 보다 먼저
        orig_post(*msg)
    P.post = post2

    orig_run = P.run_seq

    def run2(fn, plan):
        if getattr(fn, "__name__", "") == "job_power_off":
            da(False, "끄기 맨 먼저")
        orig_run(fn, plan)
    P.run_seq = run2

    orig_safe = P.safe_down

    def safe2(why):
        da(False, "이상 처리 맨 먼저")
        orig_safe(why)
    P.safe_down = safe2

    import ext_psu                                       # Vdd 전류 이상: 먼저 SG RF OFF · 드라이버 앰프 OFF → Idq 와 비슷하면 바이어스 그대로
    ext_psu.attach_soft_fault(app, lambda: da(False, "Vdd 전류 이상 - 샘플 보호"))
