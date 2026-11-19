# -*- coding: utf-8 -*-
"""PA DPD Tester - SMW200A + FSW 로 PA 에 간단한 DPD (메모리 다항식 / GMP) 를 적용해 성능 확인

- 신호: 5G NR 100 MHz 1 캐리어 (SCS 30 kHz, 273 RB) OFDM 만들기 또는 내 .wv (암호화 없는 것)
- 순서: [DPD OFF 측정] → DPD 반복 (간접 학습 ILA) → 매번 Pout · ACLR · EVM · PAPR
- 화면은 PA Bench Monitor 와 같은 모양 (위쪽 줄 · 큰 숫자 · 번호 카드 · 화이트/블랙) → 나중에 합칠 때 그대로
- 장비에는 아무것도 설치하지 않음 (LAN, SCPI raw socket 5025). 주소에 demo = 가상 PA
"""
import json
import math
import re
import os
import queue
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

import bench_link as BL

try:
    import dpd_engine as E
except Exception as _e:                     # numpy 가 없는 등
    E = None
    _IMPORT_ERR = _e

VERSION = "v0.2.3.94"
APP = "PA DPD Tester"

SET_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "PADPDTester")
SET_FILE = os.path.join(SET_DIR, "settings.json")
ILC_DIR = os.path.join(SET_DIR, "ilc")                       # ILC 결과 저장 (끝날 때마다 자동 · 최근 60 개)

# PA Bench Monitor 와 같은 색 · 테마
BG = "#0b0f14"
DASH = "#141b22"
TOPBAR = "#1c2530"
C_ACLR = "#ffb74d"       # 주황 (Bench SG 색)
C_EVM = "#4fc3f7"        # 하늘 (Bench SA 색)
C_POUT = "#39ff88"       # 초록 (Bench 전류 색)
C_PAPR = "#e0e0e0"
C_EFF = "#ce93d8"        # 보라 (효율)
THEMES = {
    "light": dict(bg="#e9edf1", panel="#ffffff", border="#cfd8dc", fg="#1f2933", dim="#546e7a", unit="#78909c",
                  entry="#ffffff", entry_bd="#b0bec5", accent="#1e88e5", line="#eceff1", sel="#e3f2fd",
                  head="#eceff1", plot_bg="#ffffff", grid="#e6e6e6", plot_fg="#333333", plot_dim="#a0a0a0",
                  btn="#ffffff", btn_act="#eef2f5", warn="#c0504d", ok="#2e7d32", info="#1f4f8f",
                  log_bg="#263238", log_fg="#eceff1"),
    "dark": dict(bg="#0f141a", panel="#18212a", border="#2b3743", fg="#dfe6ec", dim="#9fb0bd", unit="#7d8f9c",
                 entry="#0f151b", entry_bd="#3a4855", accent="#1e88e5", line="#26313c", sel="#173451",
                 head="#1f2a35", plot_bg="#11181f", grid="#22303b", plot_fg="#cfd8dc", plot_dim="#6c7a86",
                 btn="#22303c", btn_act="#2c3b48", warn="#ef9a9a", ok="#81c784", info="#90caf9",
                 log_bg="#0b0f14", log_fg="#cfd8dc"),
}
THEME = dict(THEMES["light"])
C_OFF = "#9e9e9e"
C_DPD = "#1e88e5"
C_ITER = ["#c6dbef", "#9ecae1", "#6baed6", "#4292c6", "#2171b5", "#08519c"]
F = ("맑은 고딕", 9)
FB = ("맑은 고딕", 9, "bold")


def err_where(e, n=3):
    """오류가 난 곳: 이 프로그램 .py 파일 · 줄 번호 · 함수 (안쪽부터 n 개) → '위치: dpd_engine.py 845줄 auto_model ← …'"""
    import traceback
    try:
        tb = traceback.extract_tb(e.__traceback__)
    except Exception:
        return ""
    mine = [f for f in tb if not re.search(r"[\\/](site-packages|lib[\\/]python|Lib)[\\/]", f.filename or "")
            and str(f.filename).endswith(".py")]
    fr = (mine or list(tb))[-n:]
    if not fr:
        return ""
    return "위치: " + " ← ".join("%s %d줄 %s" % (os.path.basename(f.filename), f.lineno, f.name) for f in reversed(fr))


def err_text(e):
    """오류 글 = 종류: 내용 + 줄 바꿈 + 위치 (파일 · 줄)"""
    w = err_where(e)
    return "%s: %s%s" % (type(e).__name__, e, ("\n" + w) if w else "")


def fnum(s, d=0.0):
    try:
        return float(str(s).strip())
    except (ValueError, TypeError):
        return d


def mix(c1, c2, k):
    a = [int(c1.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    b = [int(c2.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    return "#%02x%02x%02x" % tuple(int(round(x + (y - x) * k)) for x, y in zip(a, b))


# ---------------------------------------------------------------- 그래프 (Tk Canvas, 테마 색)

def aw(r):
    """측정 한 줄의 가장 나쁜 ACLR (dBc) - 반송파 여러 개면 안쪽 인접 채널 (aclr_in) 도 포함"""
    v = [r.get("aclr_l"), r.get("aclr_u")] + list(r.get("aclr_in") or [])
    v = [float(t) for t in v if t is not None and t == t]
    return max(v) if v else float("nan")


def aclr_iq_cols(r, f):
    """기록 표 I/Q ACLR 칸 (DPD 가 쓰는 값 · 항상 있음): 반송파 2 개면 '아래 / 위' 둘씩"""
    q = dict(aclr_l=r.get("aclr_iq_l", r["aclr_l"]), aclr_u=r.get("aclr_iq_u", r["aclr_u"]),
             aclr_in=r.get("aclr_iq_in") if r.get("aclr_iq_l") is not None else r.get("aclr_in"))
    return aclr_cols(q, f)


def aclr_used_src(r):
    return "장비" if r.get("aclr_src") == "fsw" else "I/Q"


def aclr_cols(r, f):
    """기록 표 ACLR L · U 칸: 반송파 1 개 = 아래 / 위, 2 개 = 왼쪽 반송파 '아래 / 위' · 오른쪽 반송파 '아래 / 위'"""
    ain = r.get("aclr_in") or []
    if len(ain) == 2:
        return ("%s / %s" % (f(r["aclr_l"], 1), f(ain[0], 1)), "%s / %s" % (f(ain[1], 1), f(r["aclr_u"], 1)))
    if ain:
        return ("%s (%s)" % (f(r["aclr_l"], 1), " ".join(f(v, 1) for v in ain)), f(r["aclr_u"], 1))
    return (f(r["aclr_l"], 1), f(r["aclr_u"], 1))

class Plot(tk.Canvas):
    def __init__(self, master, title="", xl="", yl="", **kw):
        super().__init__(master, highlightthickness=1, bd=0, **kw)
        self.title, self.xl, self.yl = title, xl, yl
        self.series = []
        self.xr = self.yr = None
        self.vlines = []
        self.spans = []
        self.user_yr = None                 # 더블클릭으로 정한 세로축 (None = 자동)
        self.store = None                   # (설정 dict, 키) - 정한 범위를 기억
        self.cur_yr = (0, 1)
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Double-Button-1>", lambda e: self.ask_range())
        self.umarks = None                  # 사용자 세로 마커 (주파수 등 x 값 목록) - enable_marks 로 켬
        self._xf = None

    def enable_marks(self, store, key, unit="MHz"):
        """오른쪽 클릭 메뉴로 세로 마커 추가 · 지우기 (store[key] 에 기억). 마커마다 곡선 값 표시"""
        self.umarks = [float(v) for v in (store.get(key) or [])]
        self._mstore, self._mkey, self._munit = store, key, unit
        self._msel = None                    # 고른 마커 (번호) - 끌기 · 방향키 · Del
        self._mdrag = False
        self.bind("<Button-3>", self.mark_menu)
        self.bind("<Button-2>", self.mark_menu)
        # V = 마우스 자리에 마커 · 마커 위 클릭 = 고르기 (끌어서 옮김) · ←/→ = 옮기기 (Shift = 크게) · Del = 지우기
        self.bind("<Enter>", lambda e: self.focus_set(), add="+")
        self.bind("<Motion>", lambda e: setattr(self, "_mx", e.x), add="+")
        for k in ("<KeyPress-v>", "<KeyPress-V>"):
            self.bind(k, self._key_v)
        self.bind("<ButtonPress-1>", self._m_press, add="+")
        self.bind("<B1-Motion>", self._m_drag, add="+")
        self.bind("<ButtonRelease-1>", self._m_release, add="+")
        for k, d in (("<Left>", -1), ("<Right>", 1), ("<Shift-Left>", -10), ("<Shift-Right>", 10)):
            self.bind(k, lambda e, d=d: self._m_step(d))
        self.bind("<Delete>", self._m_delete)
        self.bind("<BackSpace>", self._m_delete)

    def _step_size(self):
        if not self._xf:
            return 0.1
        L, x0, x1, Wd = self._xf
        return max(0.1, round((x1 - x0) / 1000, 1))      # 한 칸 (보통 0.5 MHz · Shift = 10 칸)

    def _key_v(self, e=None):
        x = self._x_at(getattr(self, "_mx", None) or 0) if getattr(self, "_mx", None) else None
        if x is None:
            return
        x = round(x, 1)
        self._mark_set(sorted(set(self.umarks + [x]))[:12])
        self._msel = self.umarks.index(x)
        self.redraw()
        return "break"

    def _near_mark(self, ex):
        if not self._xf or not self.umarks:
            return None
        L, x0, x1, Wd = self._xf
        px = lambda v: L + (v - x0) / (x1 - x0) * (Wd - L)
        i = min(range(len(self.umarks)), key=lambda k: abs(px(self.umarks[k]) - ex))
        return i if abs(px(self.umarks[i]) - ex) <= 7 else None

    def _m_press(self, e):
        i = self._near_mark(e.x)
        self._msel = i
        self._mdrag = i is not None
        self.focus_set()
        if i is not None:
            self.redraw()

    def _m_drag(self, e):
        if not self._mdrag or self._msel is None:
            return
        x = self._x_at(e.x)
        if x is None:
            return
        self.umarks[self._msel] = round(x, 1)
        self.redraw()

    def _m_release(self, e):
        if self._mdrag and self._msel is not None:
            self._mdrag = False
            self._mark_set(list(self.umarks), keep_sel=True)

    def _m_step(self, d):
        if self._msel is None or self._msel >= len(self.umarks or []):
            return "break"
        self.umarks[self._msel] = round(self.umarks[self._msel] + d * self._step_size(), 3)
        self._mark_set(list(self.umarks), keep_sel=True)
        return "break"

    def _m_delete(self, e=None):
        if self._msel is None or self._msel >= len(self.umarks or []):
            return "break"
        vals = [v for k, v in enumerate(self.umarks) if k != self._msel]
        self._msel = None
        self._mark_set(vals)
        return "break"

    def _x_at(self, ex):
        if not self._xf:
            return None
        L, x0, x1, Wd = self._xf
        return x0 + (ex - L) / (Wd - L) * (x1 - x0)

    def mark_menu(self, e):
        x = self._x_at(e.x)
        if x is None:
            return
        x = round(x, 1)
        m = tk.Menu(self, tearoff=0, font=("맑은 고딕", 10))
        m.add_command(label="여기에 마커 (%.1f %s)" % (x, self._munit), command=lambda: self._mark_add([x]))
        m.add_command(label="양쪽 대칭 마커 (±%.1f %s)" % (abs(x), self._munit), command=lambda: self._mark_add([-abs(x), abs(x)]))
        m.add_command(label="값 넣어서 마커…", command=self._mark_ask)
        if self.umarks:
            near = min(self.umarks, key=lambda v: abs(v - x))
            m.add_separator()
            m.add_command(label="가까운 마커 지우기 (%.1f)" % near, command=lambda: self._mark_set([v for v in self.umarks if v != near]))
            m.add_command(label="마커 모두 지우기", command=lambda: self._mark_set([]))
        m.tk_popup(e.x_root, e.y_root)

    def _mark_ask(self):
        from tkinter import simpledialog
        v = simpledialog.askstring("마커", "마커 위치 (%s, 여러 개는 쉼표 · 예: -51, 51)" % self._munit, parent=self)
        if not v:
            return
        vals = []
        for t in v.replace(";", ",").split(","):
            try:
                vals.append(float(t))
            except ValueError:
                pass
        self._mark_add(vals)

    def _mark_add(self, vals):
        self._mark_set(sorted(set(self.umarks + [round(v, 3) for v in vals]))[:12])

    def _mark_set(self, vals, keep_sel=False):
        if not keep_sel:
            self._msel = None if not vals else getattr(self, "_msel", None)
            if self._msel is not None and self._msel >= len(vals):
                self._msel = None
        self.umarks = list(vals)
        if getattr(self, "_mstore", None) is not None:
            self._mstore[self._mkey] = list(vals)
        self.redraw()
        pe = getattr(self, "peer", None)                     # 새 창 그래프와 마커 같이
        if pe is not None:
            try:
                pe.umarks = list(vals)
                pe.redraw()
            except tk.TclError:
                self.peer = None

    def remember(self, settings, key):
        self.store = (settings, key)
        v = settings.get(key)
        self.user_yr = (float(v[0]), float(v[1])) if isinstance(v, (list, tuple)) and len(v) == 2 else None

    def ask_range(self):
        """세로축 범위 창 (가운데): 최소 · 최대 입력 → 적용, [자동] = 기본값으로"""
        top = tk.Toplevel(self)
        top.title("세로축 범위")
        top.transient(self.winfo_toplevel())
        top.resizable(False, False)
        f = ttk.Frame(top, padding=14)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="%s - 세로축 (%s)" % (self.title.split(" (")[0], self.yl), font=FB).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        y0, y1 = self.cur_yr
        v_hi, v_lo = tk.StringVar(value="%g" % round(y1, 2)), tk.StringVar(value="%g" % round(y0, 2))
        ttk.Label(f, text="최대").grid(row=1, column=0, sticky="w")
        e1 = ttk.Entry(f, textvariable=v_hi, width=10, justify="right")
        e1.grid(row=1, column=1, padx=6, pady=2)
        ttk.Label(f, text="최소").grid(row=2, column=0, sticky="w")
        ttk.Entry(f, textvariable=v_lo, width=10, justify="right").grid(row=2, column=1, padx=6, pady=2)
        ttk.Label(f, text="지금: %s" % ("직접 정한 범위" if self.user_yr else "자동 (기본값)"), foreground="#78909c").grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(4, 8))
        bf = ttk.Frame(f)
        bf.grid(row=4, column=0, columnspan=3, sticky="e")

        def keep(v):
            self.user_yr = v
            if self.store:
                st, k = self.store
                if v is None:
                    st.pop(k, None)
                else:
                    st[k] = list(v)
            self.redraw()

        def ok(e=None):
            try:
                a, b = float(v_lo.get()), float(v_hi.get())
            except ValueError:
                return
            if b <= a:
                a, b = b, a
            if b > a:
                keep((a, b))
            top.destroy()
        ttk.Button(bf, text="적용", command=ok).pack(side="left", padx=3)
        ttk.Button(bf, text="자동 (기본값)", command=lambda: (keep(None), top.destroy())).pack(side="left", padx=3)
        ttk.Button(bf, text="취소", command=top.destroy).pack(side="left", padx=3)
        top.bind("<Return>", ok)
        top.bind("<Escape>", lambda e: top.destroy())
        top.update_idletasks()
        rt = self.winfo_toplevel()
        x = rt.winfo_rootx() + (rt.winfo_width() - top.winfo_width()) // 2
        y = rt.winfo_rooty() + (rt.winfo_height() - top.winfo_height()) // 2
        top.geometry("+%d+%d" % (x, y))
        e1.focus_set()
        e1.select_range(0, "end")
        top.grab_set()

    def set(self, series, xr=None, yr=None, vlines=(), bubbles=(), spans=()):
        self.series, self.xr, self.yr, self.vlines = series, xr, yr, list(vlines)
        self.bubbles = list(bubbles)
        self.spans = list(spans)
        self.redraw()

    @staticmethod
    def nice(lo, hi, n=5):
        if not (math.isfinite(lo) and math.isfinite(hi)) or hi <= lo:
            hi = lo + 1
        raw = (hi - lo) / n
        mag = 10 ** math.floor(math.log10(raw))
        step = min((m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw), default=mag * 10)
        v = math.floor(lo / step) * step
        out = []
        while v <= hi + step * 1e-6:
            if v >= lo - step * 1e-6:
                out.append(round(v, 10))
            v += step
        return out

    def redraw(self):
        T = THEME
        self.configure(bg=T["plot_bg"], highlightbackground=T["border"])
        self.delete("all")
        W, H = self.winfo_width(), self.winfo_height()
        if W < 50 or H < 50:
            return
        L, R, Tp, B = 52, 12, 26, 34
        self.create_text(L, 13, text=self.title, anchor="w", font=FB, fill=T["plot_fg"])
        self.create_text(W - R, 13, text=("세로축 직접 · " if self.user_yr else "") + "더블클릭 = 범위" +
                         (" · V = 마커 (끌기 · ←→ · Del) · 오른쪽 클릭 = 메뉴" if self.umarks is not None else ""), anchor="e",
                         font=("맑은 고딕", 7), fill=T["plot_dim"])
        xs = [v for s in self.series for v in s["x"]]
        ys = [v for s in self.series for v in s["y"]]
        x0, x1 = self.xr if self.xr else ((min(xs), max(xs)) if xs else (0, 1))
        if self.user_yr:
            y0, y1 = self.user_yr
        elif self.yr:
            y0, y1 = self.yr
        elif ys:
            y0, y1 = min(ys), max(ys)
            pad = (y1 - y0) * 0.08 or 1
            y0, y1 = y0 - pad, y1 + pad
        else:
            y0, y1 = 0, 1
        x1 = x1 if x1 > x0 else x0 + 1
        y1 = y1 if y1 > y0 else y0 + 1
        self.cur_yr = (y0, y1)
        px = lambda v: L + (v - x0) / (x1 - x0) * (W - L - R)
        py = lambda v: H - B - (v - y0) / (y1 - y0) * (H - Tp - B)
        for t in self.nice(x0, x1, 6):
            self.create_line(px(t), Tp, px(t), H - B, fill=T["grid"])
            self.create_text(px(t), H - B + 4, text="%g" % t, anchor="n", font=("맑은 고딕", 8), fill=T["plot_dim"])
        for t in self.nice(y0, y1, 5):
            self.create_line(L, py(t), W - R, py(t), fill=T["grid"])
            self.create_text(L - 4, py(t), text="%g" % t, anchor="e", font=("맑은 고딕", 8), fill=T["plot_dim"])
        self.create_rectangle(L, Tp, W - R, H - B, outline=T["border"])
        self.create_text((L + W - R) / 2, H - 3, text=self.xl, anchor="s", font=("맑은 고딕", 8), fill=T["plot_dim"])
        self.create_text(6, Tp - 3, text=self.yl, anchor="sw", font=("맑은 고딕", 8), fill=T["plot_dim"])
        self._xf = (L, x0, x1, W - R)
        for a_, b_, col in getattr(self, "spans", []):      # 색칠한 구간 (집중 구간 등) - 트레이스 뒤에
            a2, b2 = max(a_, x0), min(b_, x1)
            if b2 > a2:
                try:                                             # 배경과 섞은 옅은 색 (Windows 는 반투명 · 무늬가 안 됨)
                    bg_ = T.get("plot_bg", "#ffffff")
                    c1, c2 = [tuple(int(h_[i:i + 2], 16) for i in (1, 3, 5)) for h_ in (col, bg_)]
                    fillc = "#%02x%02x%02x" % tuple(int(0.18 * a + 0.82 * b) for a, b in zip(c1, c2))
                except Exception:
                    fillc = col
                self.create_rectangle(px(a2), Tp + 1, px(b2), H - B - 1, fill=fillc, outline="")
        for v, col in self.vlines:
            if x0 <= v <= x1:
                self.create_line(px(v), Tp, px(v), H - B, fill=col, dash=(3, 3))
        for s in self.series:
            pts = [(px(a), py(b)) for a, b in zip(s["x"], s["y"])
                   if math.isfinite(a) and math.isfinite(b) and x0 <= a <= x1 and y0 <= b <= y1]
            if not pts:
                continue
            if s.get("dots"):
                r = s.get("r", 1)
                for a, b in pts:
                    self.create_rectangle(a - r, b - r, a + r, b + r, outline="", fill=s["color"])
            elif len(pts) > 1:
                self.create_line(*[c for p in pts for c in p], fill=s["color"], width=s.get("width", 1),
                                 dash=s.get("dash") or "")
            if s.get("marks"):
                r = s.get("r", 3)
                for a, b in pts:
                    if s.get("hollow"):
                        self.create_oval(a - r, b - r, a + r, b + r, outline=s["color"], width=2, fill=T["plot_bg"])
                    else:
                        self.create_oval(a - r, b - r, a + r, b + r, outline="", fill=s["color"])
        for bub in getattr(self, "bubbles", []):
            bx, by, txt, col = bub[:4]
            left = len(bub) > 4 and bub[4] == "l"
            if not (math.isfinite(bx) and math.isfinite(by)) or not (x0 <= bx <= x1 and y0 <= by <= y1):
                continue
            a, b = px(bx), py(by)
            self.create_oval(a - 8, b - 8, a + 8, b + 8, outline=col, width=3)
            t = self.create_text(0, 0, text=txt, fill="white", font=FB)
            bb = self.bbox(t)
            tw, th = bb[2] - bb[0] + 14, bb[3] - bb[1] + 6
            bx0 = min(max((a - 12 - tw) if left else (a + 12), L + 2), W - R - tw - 2)
            by0 = max(b - th - 10, Tp + 2)
            if len(bub) > 5:                                  # 줄 번호: 위쪽에 겹치지 않게 쌓고 점까지 선으로
                by0 = Tp + 4 + bub[5] * (th + 4)
                self.create_line(a, b - 8, bx0 + (tw if left else 0), by0 + th / 2, fill=col, width=1)
            self.create_rectangle(bx0, by0, bx0 + tw, by0 + th, fill=col, outline="")
            self.coords(t, bx0 + tw / 2, by0 + th / 2)
            self.tag_raise(t)
        # 사용자 세로 마커: 보라 선 + 아래쪽에 이름이 있는 곡선들의 그 위치 값
        for i, v in enumerate(self.umarks or []):
            if not (x0 <= v <= x1):
                continue
            a = px(v)
            selm = getattr(self, "_msel", None) == i
            self.create_line(a, Tp, a, H - B, fill="#d500f9" if selm else "#8e24aa", width=3 if selm else 2)
            parts = ["%g %s" % (v, getattr(self, "_munit", ""))]
            for s_ in self.series:
                if not s_.get("label") or not s_["x"]:
                    continue
                j = min(range(len(s_["x"])), key=lambda k: abs(s_["x"][k] - v))
                yv = s_["y"][j]
                if math.isfinite(yv):
                    parts.append("%s %.1f" % (s_["label"], yv))
            t = self.create_text(0, 0, text="\n".join(parts), fill="white", font=("맑은 고딕", 8), justify="left", anchor="nw")
            bb = self.bbox(t)
            tw, th = bb[2] - bb[0] + 8, bb[3] - bb[1] + 6
            bx0 = min(max(a + 4, L + 2), W - R - tw - 2)
            by0 = H - B - th - 4 - (i % 2) * (th + 4)
            self.create_rectangle(bx0, by0, bx0 + tw, by0 + th, fill="#d500f9" if selm else "#8e24aa", outline="")
            self.coords(t, bx0 + 4, by0 + 3)
            self.tag_raise(t)
        ly = Tp + 8
        for s in self.series:
            if s.get("label"):
                self.create_line(W - R - 150, ly, W - R - 128, ly, fill=s["color"], width=3, dash=s.get("dash") or "")
                self.create_text(W - R - 124, ly, text=s["label"], anchor="w", font=("맑은 고딕", 8), fill=T["plot_fg"])
                ly += 14


class ScrollFrame(ttk.Frame):
    """세로 스크롤 틀 (내용은 .body)"""

    def __init__(self, parent, **kw):
        super().__init__(parent, **kw)
        self.cv = tk.Canvas(self, highlightthickness=0, bd=0)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.cv.yview)
        self.cv.configure(yscrollcommand=self.vsb.set)
        self.cv.pack(side="left", fill="both", expand=True)
        self.vsb.pack(side="right", fill="y")
        self.body = ttk.Frame(self.cv)
        self.win = self.cv.create_window(0, 0, window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda e: self.cv.configure(scrollregion=(0, 0, self.body.winfo_reqwidth(),
                                                                                 self.body.winfo_reqheight())))
        self.cv.bind("<Configure>", lambda e: self.cv.itemconfigure(self.win, width=e.width))
        self._scrollable = True
        # 마우스가 이 틀 (카드 · 칸 · 버튼 위 어디든) 에 들어오면 휠을 이 틀이 받음 (Bench 안에서도)
        self.bind("<Enter>", self._wheel_on, add="+")
        self.bind_all("<MouseWheel>", self._wheel, add="+")

    def _wheel_on(self, e=None):
        for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.bind_all(ev, wheel_any, add="")

    def _wheel(self, e):
        return wheel_any(e)

    def scroll(self, e):
        if self.body.winfo_reqheight() <= self.cv.winfo_height():
            return
        n = -1 if (getattr(e, "delta", 0) > 0 or getattr(e, "num", 0) == 4) else 1
        self.cv.yview_scroll(n * 2, "units")


def wheel_any(e):
    """마우스 아래에서 가장 가까운 스크롤 틀을 굴림 (DPD 카드 · Bench 틀 모두)"""
    try:
        w = e.widget.winfo_containing(e.x_root, e.y_root)
    except (tk.TclError, KeyError, AttributeError):
        return
    while w is not None:
        if isinstance(w, (tk.Text, ttk.Treeview, tk.Listbox)):
            return                                    # 로그 · 표는 자기 스크롤
        if getattr(w, "_scrollable", False) and hasattr(w, "scroll"):
            w.scroll(e)
            return "break"
        if hasattr(w, "cv") and hasattr(w, "vsb"):    # Bench 의 ScrollFrame
            try:
                if w.vsb.winfo_ismapped():
                    n = -1 if (getattr(e, "delta", 0) > 0 or getattr(e, "num", 0) == 4) else 1
                    w.cv.yview_scroll(n * 2, "units")
                    return "break"
            except tk.TclError:
                pass
        w = w.master


# ---------------------------------------------------------------- 화면
class Tip:
    """마우스를 올리면 오른쪽 옆에 짧은 설명 (0.4초 뒤), 마우스가 나가거나 누르면 사라짐"""
    cur = None

    def __init__(self, w, text):
        self.w, self.text, self.top, self.job = w, text, None, None
        w.bind("<Enter>", self.enter, add="+")
        w.bind("<Leave>", self.hide, add="+")
        w.bind("<ButtonPress>", self.hide, add="+")
        w.bind("<Motion>", self.move, add="+")

    def enter(self, e):
        self.hide()
        self.job = self.w.after(400, lambda: self.show(e.x_root, e.y_root))

    def move(self, e):
        if self.top is not None:
            self.place(e.x_root, e.y_root)

    def place(self, x, y):
        t = self.top
        t.update_idletasks()
        sw, sh = t.winfo_screenwidth(), t.winfo_screenheight()
        w_, h_ = t.winfo_reqwidth(), t.winfo_reqheight()
        x_ = x + 18 if x + 18 + w_ < sw else x - 18 - w_
        y_ = min(y + 12, sh - h_ - 8)
        t.geometry("+%d+%d" % (x_, y_))

    def show(self, x, y):
        self.job = None
        if Tip.cur is not None and Tip.cur is not self:
            Tip.cur.hide()
        Tip.cur = self
        t = self.top = tk.Toplevel(self.w)
        t.overrideredirect(True)
        try:
            t.attributes("-topmost", True)
        except tk.TclError:
            pass
        title, _, body = self.text.partition("\n")
        f = tk.Frame(t, bg="#263238", highlightthickness=1, highlightbackground="#546e7a")
        f.pack()
        tk.Label(f, text=title, bg="#263238", fg="#ffd54f", font=("맑은 고딕", 10, "bold"), justify="left",
                 anchor="w").pack(fill="x", padx=10, pady=(6, 2))
        if body:
            tk.Label(f, text=body, bg="#263238", fg="#eceff1", font=("맑은 고딕", 9), justify="left", anchor="w",
                     wraplength=340).pack(fill="x", padx=10, pady=(0, 7))
        self.place(x, y)

    def hide(self, e=None):
        if self.job is not None:
            try:
                self.w.after_cancel(self.job)
            except tk.TclError:
                pass
            self.job = None
        if self.top is not None:
            try:
                self.top.destroy()
            except tk.TclError:
                pass
            self.top = None
        if Tip.cur is self:
            Tip.cur = None


# 칸 설명 (첫 줄 = 제목, 아래 = 무엇 · 이렇게 바꾸세요) - 사용 가이드 PPT 와 같은 내용
TIPS = {
    "bw_mode": "대역\n100 MHz DPD: NR 100 MHz 만들기 또는 내 .wv.\n400 MHz DPD: 내 .wv 만 (샘플레이트 그대로, 줄이기 없음).\n파일 · ACLR 채널 폭 · 간격은 대역마다 따로 기억.",
    "sig_src": "신호\nNR 100 MHz 1C 를 직접 만들거나, 내 .wv (R&S, 암호화 안 된 것) 를 불러옴.\n회사 신호가 있으면 내 .wv, 아니면 NR.",
    "wv_path": "내 .wv 파일\nSMW 에서 재생할 신호. 샘플레이트 그대로 씀.",
    "file_half400": "1000 MSps 넘으면 절반으로 (400 MHz DPD, 권장)\n예: 1966.08 MSps 파일 → 983.04 MSps (주기 신호라 FFT 로 정확히, ±491 MHz 까지 남음).\n신호 (±200 MHz) · 바깥 인접 채널 (±300 MHz) 은 그대로 · 샘플 수 반 → 측정 · 학습이 2 배 빠르고\nFSW 가 한 주기를 다 받음 (FSW I/Q 쓸 수 있는 대역 ≈ 0.8 × 샘플레이트 = ±393 MHz).\n끄면 파일 샘플레이트 그대로.",
    "file_halve": "500 MSps 넘으면 절반으로 (100 MHz DPD 만)\n끄면 (기본) 내 .wv 샘플레이트 그대로.\n켜면 계산 · 전송이 2배 빨라지지만 샘플레이트 1/4 밖이 잘림.",
    "carrier": "캐리어\n1C (100 MHz). 2C 는 다음 버전.",
    "mod": "변조\nQPSK ~ 256QAM. 보통 256QAM (EVM 기준이 가장 엄격).",
    "n_sym": "OFDM 심볼 수\n신호 길이 (14 = 0.5 ms). 길수록 통계가 정확하지만 측정 · 전송이 느려짐.\n기본 28, 느리면 14.",
    "papr_db": "CFR 목표 PAPR\n피크를 잘라 PAPR 을 이 값으로 맞춤.\n기본 8 dB. PA 포화 (AM-AM 끝 처짐) 가 심하면 7 ~ 7.5.",
    "cfr_on": "CFR 사용\n피크 자르기. 끄면 원래 PAPR (약 11 dB) 그대로 → SG 최대 · PA 포화에 쉽게 걸림. 보통 켬.",
    "os": "샘플레이트\n×2 / ×3 / ×4 (245.76 ~ 491.52 MSps).\nDPD 는 신호보다 3 ~ 5 배 넓은 대역을 봐야 해서 ×4 권장.",
    "ch_bw_mhz": "ACLR 채널 폭\n채널 · 인접 채널 전력을 재는 폭 (MHz). NR 100 MHz = 98.28, 400 MHz 기본 380.16.\n바꾸면 프로그램이 계산하는 ACLR (I/Q) 위치가 바뀜 (대역마다 기억).",
    "adj_mhz": "인접 채널 간격\n채널 중심 ~ 인접 채널 중심 (MHz). 100 MHz = 100, 400 MHz = 400.\n줄이면 채널 끝에 더 가까운 곳의 ACLR 을 봄. (FSW Mod. 화면 ACLR 은 FSW 화면 설정 그대로)",
    "freq_ghz": "주파수\nSMW · FSW 중심 주파수 (GHz). 예 3.35 / 3.6 / 3.75.",
    "sg_dbm": "SG 레벨 (RMS)\n한 레벨 실행 때 SMW 출력. 3 카드 [SG 레벨 정하기] 로 목표 Pout 에서 자동 입력 가능.",
    "sg_max_dbm": "SG 최대\nDPD 가 SG 를 올려도 이 값을 넘지 않음 (PA 보호). 넘으면 최대로 제한하고 로그에 경고.",
    "in_loss_db": "입력 손실 (SG → PA)\n케이블 손실 dB. Pin · Gain · PAE 계산에 씀.",
    "out_loss_db": "출력 손실 (PA → FSW)\n감쇠기 포함 dB. 틀리면 Pout · PAE 가 전부 틀림 → 꼭 확인.",
    "cal_db": "Pout 보정\nPout 에 더하는 값. 3 카드 '기록에 맞춤' 이 켜져 있으면 자동으로 바뀜.",
    "fsw_ref_dbm": "FSW Ref Level (I/Q 받기)\nauto (권장) = 처음엔 FSW 자동 레벨, 그 뒤엔 측정 최고값 + 1.5 dB 로 매번 맞춤 → ADC 를 꽉 채워 잡음 바닥 ↓.\n숫자 (dBm) = 고정. 너무 높으면 잡음 바닥 ↑ (DPD 가 그 바닥에서 멈춤), 너무 낮으면 과입력.",
    "fsw_rbw_khz": "FSW RBW (kHz)\n예 100 → FSW Mod. 화면 ACLR 측정과 I/Q 화면 스펙트럼 창에 RBW 100 kHz.\nMod. 화면은 측정 때만 바꾸고 끝나면 원래대로 (Bench Mod. 측정 그대로). 빈칸 = FSW 화면 설정 그대로.\n작을수록 잡음 바닥 ↓ · 스윕 느려짐.",
    "fsw_att_db": "FSW 감쇠 (dB)\nauto = Ref Level 에 맞춰 자동. 숫자 = 고정 (I/Q 받기 · Mod. 화면 ACLR 측정 모두, Mod. 화면은 끝나면 원래대로).\n작을수록 잡음 바닥 ↓ · 너무 작으면 과입력 (FSW 경고).",
    "force_iters": "반복 횟수 끝까지\n켜면 목표 ACLR 에 닿아도 4 카드 반복 횟수 (ILC 는 ILC 반복) 만큼 끝까지 돌림.\n끄면 목표 ACLR 에 닿는 순간 그 레벨 반복 끝.",
    "focus_mhz": "집중 구간 (반송파 중심에서 MHz)\n기준 0 = 신호 가운데 (SMW · FSW 중심 주파수). 반송파가 여러 개면 반송파마다 그 중심에서 ±.\n100 MHz 반송파: 예 50-70 → 채널 끝 바로 바깥 (어깨). 400 MHz 반송파 (끝 ±190): 예 200-260.\nILC · DLA 가 이 구간을 먼저 고치게 오차를 키움.\nILC 는 이 구간 전력으로 좋고 나쁨을 판단 · 로그에 '집중 구간 dBc'. 빈칸 = 끔 (전 대역 똑같이).",
    "focus_on": "집중 켜기\n끄면 집중 구간 값은 그대로 두고 ILC · DLA 가 전 대역을 똑같이 고침 (그래프의 주황 선도 숨김).",
    "focus_w": "집중 가중\n주파수 영역 보정 때 고르는 구간 (집중 구간 또는 ACLR 채널) 오차를 몇 배로 볼지 (4 기본, 2 ~ 10).\n너무 크면 다른 구간 (채널 안 · EVM) 이 나빠질 수 있음.",
    "w_mode": "보정 기준 (ILC · DLA 가 무엇을 줄일지)\n시간 영역 = 보낸 신호와 PA 출력 차이를 샘플마다 전 대역 똑같이 (EVM · 전체 오차).\n주파수 · 집중 구간 = 오차를 FFT 해서 집중 구간 (반송파마다 ±) 만 키워 그 구간을 먼저 고침 · 좋고 나쁨도 그 구간 전력.\n주파수 · ACLR 채널 = ACLR 인접 채널 (반송파마다 양옆, 자동) 만 키움 → ACLR 을 바로 노림.\n모델 학습은 셋 다 시간 영역 I/Q (가중만 다름) · 4 카드와 스펙트럼 위 '보정' 은 같은 값.",
    "ref_margin_db": "Ref 여유 (dB)\nFSW Ref Level auto 때 FSW 입력 최고값보다 얼마나 위로 둘지 (3 기본).\nFSW 에 IF OVLD 가 뜨면 프로그램이 3 dB 씩 더 올려 다시 받음 (로그에 표시). 계속 뜨면 이 값을 5 ~ 6 으로.",
    "fsw_park": "ACLR 화면 보기\n켜면: I/Q 를 받은 뒤 FSW 를 Mod. 화면 (Spectrum · ACLR) 으로 돌려 둠 → 장비 화면에 늘 ACLR 스펙트럼 (계속 스윕),\n프로그램 스펙트럼 그래프도 Mod. 화면에서 잰 것만 (OFF · 최종). I/Q 받을 때만 잠깐 I/Q 로.\n끄면: FSW 는 MultiView (Mod. + I/Q), 그래프는 측정마다 I/Q 스펙트럼.",
    "iq_avg": "I/Q 평균 주기\n신호 몇 주기를 시간까지 맞춰 평균할지 (2 기본). 2 배마다 잡음 3 dB ↓ (8 = -6 dB · 32 = -12 dB).\n한 번에 약 400만 샘플까지 받고, 넘으면 여러 번 받아 평균 (그만큼 느려짐).\n많을수록 DLA · ILC 가 더 내려감.",
    "aclr_src": "ACLR 읽기 (학습은 셋 다 I/Q)\nMod. OFF·최종 (권장): DPD OFF 와 레벨마다 마지막 결과만 FSW Mod. 화면 (스펙트럼, 잡음 낮음) 으로 → 반복 중엔 FSW 화면 고정, 남는 숫자는 진짜 ACLR.\nMod. 매번: 측정마다 Mod. 화면으로 (화면이 왔다 갔다).\nI/Q: 받은 I/Q 로 계산 (빠름, I/Q 잡음 바닥에서 막혀 보일 수 있음).",
    "bench_loss": "Bench 손실 표 사용\n켜 두면 주파수별 입력 · 출력 손실을 Bench 설정에서 그대로 가져옴.",
    "b_stype": "샘플 종류\nBench 의 샘플 종류. 바꾸면 그 샘플에서 쓰던 1 ~ 5 카드 설정으로 자동 전환.",
    "b_rec": "기록\nSG → Pout 계산에 쓸 .pam 기록. 지금 샘플 번호 (★) 의 Mod. 기록이 지금 주파수에 있으면 자동으로 고르고,\n없으면 '선택 안 함' (기록 Pout · 기록에 맞춘 Pout 보정 안 씀). 다른 샘플 · 조건 기록은 고르지 마세요 - Pout 이 안 맞음.",
    "b_target": "목표 Pout\n원하는 DUT 출력 (dBm). [SG 레벨 정하기] 를 누르면 기록에서 SG 레벨을 계산해 2 카드에 넣음.",
    "pout_lock_on": "목표 Pout 에 SG 맞추고 고정\n[실행] 때 먼저 DPD OFF 신호를 보내며 실제 Pout 을 재서 SG 를 맞춤 (보통 2 ~ 4 번, ±0.1 dB)\n→ 그 SG 를 2 카드에 넣고 고정한 채 DPD OFF · DPD 시작. 한 레벨 · ILC · 방식 비교에서 (파워 스윕은 아님).\nDPD · ILC 반복 중에도 Pout 이 목표에서 벗어나면 목표 이득을 조금씩 고쳐 목표 Pout 을 유지 (로그 'Pout 유지').\n압축 구간이면 잰 기울기로 더 크게 움직임 · SG 최대에서 멈춤.",
    "pout_src": "Pout 기준 (효율 계산도 이 Pout)\nI/Q + 보정 = 모든 측정을 I/Q 전력 + Pout 보정으로 (빠름 · 반복마다).\n스펙트럼 (읽는 측정) = FSW Mod. 화면을 읽은 측정 (DPD OFF · 최종 · 'Mod. 매번' 이면 전부) 은 Tx 합 + 출력 손실 그대로\n(Bench Mod. 측정과 같은 방식) · 안 읽은 반복은 I/Q + 보정.\n어느 쪽이든 로그 'Pout 두 가지' 와 엑셀에 두 값 · 두 효율을 같이 적음 → 비교용.",
    "cal_every": "매 측정마다 다시 맞춤\n켜면 DPD 반복마다 FSW 스펙트럼 (Mod. 화면) 을 읽어 Pout 보정을 다시 맞추고 그 측정 Pout = 스펙트럼 값.\n(I/Q 와 스펙트럼 차이가 반복 중에 0.1 dB 쯤 변하는 것까지 따라감) · 반복마다 스펙트럼 스윕 시간만큼 느려짐.\n로그에 'Pout 보정 → x dB (이번 차이 …)'.",
    "cal_mod": "Pout 을 FSW Mod. 화면에 맞춤\n실행 첫 측정 (Pout 맞추기 첫 번째 또는 DPD OFF) 에서 FSW Mod. 화면 (Bench Mod. 측정과 같은 스펙트럼) 의\nPout 대역 채널 전력 + 출력 손실 을 Pout 으로 보고, I/Q Pout 과의 차이를 'Pout 보정' 에 자동으로 넣음 → 이후 측정도 같은 보정.\n(Mod. 화면 Span 이 신호보다 넓어야 함 · Bench 기록에 맞춤보다 먼저) · 그 뒤 Mod. 화면을 읽을 때마다 로그에 'Pout 비교'.",
    "pout_band": "Pout 대역 (채널 안 전력만 Pout 으로)\nauto = 반송파 채널 안만 (반송파 1 개: 가운데 ± 채널 폭/2 · 여러 개: 반송파마다) - 잡음 · ACLR · 대역 밖은 뺌.\n직접: MHz 범위 (중심 0 기준), 여러 개는 쉼표. 예 -50~50 · -200~-100, 100~200\nall = 받은 대역 전체 (예전 방식).\n로그에 첫 측정 때 쓴 대역과 '전체 대비 dB'.",
    "pout_strict": "반복마다 Pout 딱 맞춤\n켜면 DPD · ILC 반복마다 기록 측정 전에 그 입력으로 Pout 만 빠르게 재서 (I/Q, 기록 안 함)\n목표 ± 허용 오차를 벗어나면 SG 를 고쳐 다시 재고, 맞으면 그 SG 로 기록 → 모든 기록이 목표 Pout 안.\n반복마다 I/Q 측정이 1 ~ 2 번 더 (느려짐) · 로그 'Pout 딱 맞춤'.",
    "bad_gain_db": "이상 측정 기준 (dB)\nDPD · ILC 반복에서 앞 측정보다 Gain 이 이만큼 넘게 갑자기 떨어지면 (PA 순간 이상 · 바이어스 · 연결)\n그 측정은 버리고 같은 입력으로 한 번 다시 잼 → 또 떨어지면 측정을 멈추고 SG RF OFF (바이어스는 그대로 · 로그에 지금 Vdd · Id). 0 = 끔.",
    "pout_tol": "Pout 허용 오차 (dB) · 기본 0.05\n실행 처음 '목표 Pout 에 SG 맞추기' 와 반복마다 '맞춤' 둘 다 이 값 (예전엔 처음 맞추기가 늘 0.1 dB 였음).\n목표 가까이 오면 두 번 재서 평균 · 맞을 때까지 최대 6 번 고침. 측정이 흔들리면 2 카드 '읽기 전 기다림' 을 늘리세요.",
    "settle_s": "읽기 전 기다림 (초) · 기본 0.5\nSG 에 신호 · 레벨을 올린 뒤 FSW 로 읽기 전에 기다리는 시간. PA 출력 · 전류가 자리 잡게.\n너무 빨리 읽어 Pout · ACLR 이 흔들리면 늘리세요 (1 ~ 2 초). 측정마다 이만큼 더 걸림.",
    "auto_cal": "Pout 보정을 기록에 맞춤\nDPD OFF 측정 때 같은 SG 의 기록 Pout 과 비교해 보정값을 자동으로 → Bench 와 Pout 이 같아짐. 켬 권장.",
    "method": "학습 방식\nILA (간접): PA 출력 → 입력 역모델을 배움. 기본 · 안정.\nDLA (직접): 출력 오차를 직접 줄임 - 보통 0.5 ~ 3 dB 더 (조금 느림, 신경망은 ILA 로).\nILA+보정: ILA 반복 뒤 '오차 보정 반복' 만큼 측정하며 출력 오차를 PA 기울기로 보정해 계수를 더 고침 (μ 따로 · 나빠지면 자동 되돌림).\nILC→모델: 먼저 ILC (4 카드 ILC 반복) 로 이 PA 에 맞는 입력을 직접 찾고, 모델이 '원래 신호 → 그 입력' 을 배움 → 그 뒤 ILA 로 다듬음.\n  측정 잡음을 덜 타고 ILC 성능에 가깝게 (대신 ILC 반복만큼 시간 더).",
    "model": "DPD 모델\nGMP: 기본 · 빠름.\n구간 GMP: Doherty 피크 켜지는 레벨 앞뒤로 GMP 두 벌 (경계 자동).\nDVR: 크기 구간마다 다른 기울기 - Doherty 꺾임에 강함.\n신경망 · 위상정규 신경망: 포화 근처 · 특이한 PA.\n[목록 편집…] 으로 안 쓰는 모델 숨기기.",
    "dvr_seg": "DVR 구간 수\n입력 크기를 몇 구간으로 나눠 기울기를 따로 줄지 (6 ~ 12, 기본 8).\n많을수록 꺾임을 잘 따라가지만 계수 · 잡음이 늘어남.",
    "K": "비선형 차수 K\n|x| 의 몇 제곱까지 쓸지 (5 ~ 9, 기본 7).\n피크 근처 압축 보정이 모자라면 ↑, 결과가 흔들리면 ↓.",
    "M": "메모리 깊이 M\n지금 + 이전 몇 샘플까지 볼지 (2 ~ 6, 기본 4).\nAM-AM 이 두껍게 퍼지면 (메모리 효과) ↑. 신경망도 씀.",
    "L": "GMP 교차 지연 L\n앞뒤 샘플 크기를 섞은 항 (0 ~ 2, 기본 1).\nDoherty · 넓은 대역이면 1 ~ 2. 0 = 메모리 다항식.",
    "L_lag": "GMP Lagging 항 수\n지난 샘플 크기를 섞은 항 x(n-m)·|x(n-m-l)|^k, l = 1 ~ 이 값 (0 ~ 3).\n빈칸 = 위 L 과 같음. 0 = Lagging 항 안 씀.\n'K · M · L 자동' 이 켜져 있으면 자동이 고른 L 로 앞뒤 같게 (이 칸은 안 씀).",
    "L_lead": "GMP Leading 항 수\n앞 샘플 크기를 섞은 항 x(n-m)·|x(n-m+l)|^k, l = 1 ~ 이 값 (0 ~ 3).\n빈칸 = 위 L 과 같음. 0 = Leading 항 안 씀.\n'K · M · L 자동' 이 켜져 있으면 자동이 고른 L 로 앞뒤 같게 (이 칸은 안 씀).",
    "ila_guard_db": "되돌림 기준 (ILA · DLA · ILA+보정 · 신경망)\nDPD 반복 중 ACLR 이 지금까지 가장 좋았던 값보다 이만큼 (dB) 넘게 나빠지면\n가장 좋았던 계수로 돌아가 섞기 비율 μ 를 반으로 줄여 이어서 반복 (기본 1.0 dB, 0 = 끔).\n반복이 끝나면 마지막이 아니라 가장 좋았던 계수 · 신호로 끝냄.",
    "retry_off": "DPD OFF 보다 나쁘면 다시 (ILA · DLA · ILA+보정 · 신경망)\n반복이 끝났는데도 DPD 결과가 DPD OFF 보다 ACLR 이 나쁘면 처음부터 다시 (기본 2 번, 0 = 끔).\n다시 할 때: 계수 버림 → DPD OFF 다시 재서 목표 이득 다시 → μ ½ 로 반복.\n2 번째부터는 목표 이득 크기를 '작은 신호 이득' 대신 '전체 평균 이득' 으로 (피크가 포화에 걸려 나빠지는 경우).\n다 해도 나쁘면 가장 좋았던 시도로 끝내고 로그에 확인할 것 (SG · 최고값 제한 · 측정 바닥).",
    "ec_iters": "오차 보정 반복 (학습 = ILA+보정 일 때)\nILA 반복이 끝난 뒤 '측정 → 출력 오차 → 계수 고침' 을 몇 번 더 (기본 6, 0 = 안 함).\n출력 오차를 PA 기울기 (AM-AM · AM-PM) 로 입력 쪽 보정량으로 바꿔 GMP 계수에 더함 → ILA 가 멈춘 곳에서 더 내려감.\n나빠지면 가장 좋았던 계수로 돌아가 μ 를 반으로.",
    "ec_mu": "오차 보정 μ (학습 = ILA+보정 일 때)\n한 번에 보정량을 얼마나 반영할지 (0.05 ~ 1, 기본 0.5).\n크면 빨리 내려가지만 흔들릴 수 있음 · 반복마다 ACLR 이 오르내리면 0.2 ~ 0.3.\n나빠지면 자동으로 반씩 줄어듦 (최소 0.05). 위 '섞기 비율 μ' 는 ILA 쪽 값 (따로).",
    "iters": "반복 횟수\n학습 → 측정을 몇 번 (3 ~ 5). 보통 2 ~ 3 번째에 거의 수렴.",
    "mu": "섞기 비율 μ\n새 계수를 얼마나 반영 (0.5 ~ 1, 기본 0.7). 반복마다 ACLR 이 흔들리면 0.5.",
    "peak_lim_db": "최고값 제한\nDPD 가 피크를 원래보다 몇 dB 까지 키울 수 있나 (기본 3).\nSG 최대 · PA 포화에 걸리면 2.",
    "lam": "정규화 λ\n계수가 너무 커지지 않게 누르는 값 (기본 1e-6).\n결과가 들쭉날쭉하거나 계수가 폭주하면 1e-5 ~ 1e-4.",
    "n_fit": "학습 샘플 수\n한 번 학습에 쓰는 샘플 (기본 40000). 늘리면 정확 ↑ · 느려짐 (2 ~ 10만).",
    "nn_hidden": "신경망 은닉 크기\n은닉층 크기 (16 ~ 32, 기본 24). 크면 강하지만 학습이 느림.",
    "nn_epochs": "신경망 학습 횟수\n(20 ~ 50, 기본 30). 결과가 덜 좋으면 ↑.",
    "ilc_iters": "ILC 반복\n한계 ACLR 찾기 반복 (10 ~ 15).",
    "ilc_gain": "ILC 이득\n한 번에 고치는 양 (기본 0.8). 흔들리면 0.5.",
    "odd_only": "홀수 차수만\nK = 1, 3, 5 … 만 사용. 보통 끔 (짝수 항도 도움이 됨).",
    "auto_model": "K · M · L 자동\n계산으로 36 조합 → 후보 3개 실측 → ACLR 가장 좋은 것. 고른 값은 칸에 들어가고 샘플별 기억.\n처음 · 새 샘플이면 켬, 주파수 · 샘플 비교 측정이면 끄고 고정.",
    "run_mode": "실행 방식\n한 레벨: 2 카드 SG 레벨에서. 파워 스윕: SG 시작 ~ 끝을 간격마다 (레벨마다 OFF → DPD).\n처음엔 한 레벨로 확인 → 파워 스윕.",
    "sw_start": "SG 시작\n파워 스윕 첫 SG 레벨 (dBm).",
    "sw_stop": "SG 끝\n파워 스윕 마지막 SG 레벨. SG 최대 이하.",
    "sw_step": "SG 간격\n레벨 간격 dB (보통 1).",
    "pout_stop": "Pout 멈춤\n이 Pout 을 넘으면 스윕 끝 (PA 보호). 빈칸 = 끝까지.",
    "target_aclr": "목표 ACLR\n닿으면 그 레벨 반복을 끝냄 (dBc, 기본 -50). 빈칸 = 반복 수만큼.",
    "run_name": "기록 이름\n기록 목록 · 엑셀 이름. 빈칸 = 샘플 종류 · 시각.",
    "rf_off_end": "끝나면 SG RF OFF\n끝나거나 오류가 나면 RF 를 끔. 켬 권장 (중지는 항상 RF OFF).",
    "y_q": "세로축\nACLR vs Pout 그래프의 세로축 값.",
}
BTN_TIPS = {
    "btn_off": "DPD OFF 측정만\nDPD 없이 한 번 측정 - 연결 · 손실 · Pout 확인용.",
    "btn_go": "▶ DPD 실행\nDPD OFF → (K · M · L 자동) → DPD 반복. 파워 스윕이면 레벨마다, 앞 레벨 계수로 시작.",
    "cb_model": TIPS["model"],
    "btn_xloss": "추가 손실 표 (커플러 · 아이솔레이터 등)\n케이블 손실 (Bench 표 또는 위 칸) 에 주파수마다 더할 In · Out 손실.\nBench 케이블 손실 txt 와 같은 형식 · txt 불러오기 / 저장. 비우면 0 dB.",
    "btn_stop5": "DPD 측정 중지\nDPD 측정만 바로 멈춤 (SG RF OFF · FSW 원래 화면).\n바이어스 · 파워서플라이는 그대로. Bench 오른쪽 위 [긴급 정지] 와 다름.",
    "btn_stsave": "FSW 설정 저장\nFSW 지금 설정 전부 (모든 채널 · 화면 · 마커 · ACP · Ref · 감쇠) 를 PC 에 .dfl 로.\n원하는 화면을 만들어 두고 저장 → 다음에 [FSW 설정 불러오기] 로 그대로.",
    "btn_stload": "FSW 설정 불러오기\nPC 의 .dfl 을 FSW 로 보내 그 설정으로 (저장할 때 화면 그대로). 측정 중에는 안 됨.\n불러오면 다음 DPD 실행 때 장비에 새로 연결.",
    "btn_conn": "장비 연결 한 버튼 (● 연결됨 초록 / ○ 연결 안 됨)\n연결 해제 = 이 프로그램의 SMW · FSW 연결을 끊고 Local (&GTL) → 장비 앞 패널 바로 조작.\n연결 = 연결해서 확인만 (앞 패널은 Local). 측정은 [실행] 하면 알아서 연결.\n측정이 끝나면 자동으로 Local 로 돌려 둠 (연결은 유지).",
    "btn_shot": "FSW 화면 저장\nFSW 에 지금 떠 있는 화면 그대로 PNG 로 PC 에 저장 (FSW 에서 원하는 화면 · 마커를 맞춘 뒤 누르기).\n측정 중에도 됨 (FSW 에 따로 연결). 파일 이름에 대역 · 마지막 측정 · 시각.",
    "btn_rf": "SG RF 한 버튼\n● RF ON (빨강) = 지금 켜짐 → 누르면 끔 · ○ RF OFF = 꺼짐 → 누르면 (확인 후) 켬 · RF ? = 몰라서 SMW 에 물어본 뒤 반대로.\n측정 중에 누르면 중지 (RF OFF). 바이어스 · 파워서플라이 · FSW 화면은 그대로.",
    "btn_send": "고른 입력 신호 계속 내보내기\n아래 측정 기록 표에서 고른 줄 (없으면 ★ 가장 좋은 측정) 의 입력 신호를 SMW 로 보내고 RF 를 켜 둠 (계속 쏨).\n중지한 뒤에도 됨 (최근 30 개 · 가장 좋은 것 보관). 끄려면 [SG RF OFF].",
    "btn_recheck": "고른 신호로 다시 재기\n표에서 고른 줄 (없으면 ★) 의 입력 신호를 다시 보내 한 번 측정 (ACLR 은 FSW Mod. 화면) → 같은 결과가 나오는지 확인.",
    "btn_savefinal": "고른 입력 .wv 저장\n표에서 고른 줄 (없으면 ★) 의 입력 신호를 R&S .wv 로. 파일 이름에 ACLR · SMW 레벨 (dBm) · 주파수.\n같은 이름 _설정.xlsx 도 같이: 그 측정 때 1 ~ 5 카드 설정 · 자동으로 정해진 값 · 결과 (다음에 참고).\nPA 출력 I/Q 는 측정 기록 위 [고른 출력 I/Q 저장…].",
    "btn_more": "이어서 반복\n끝난 (또는 중지한) DPD · ILC 를 그 결과 (계수 · 입력 · 이득) 에서 이어서 N 번 더. 같은 SG 레벨.\n표 · 그래프 · 기록은 이어 붙임. 반복을 짧게 돌려 보고 좋으면 더 돌릴 때.",
    "btn_cmp": "방식 비교 실행\n같은 SG 레벨 · 같은 DPD OFF 에서 고른 방식들을 차례로 DPD.\n끝나면 요약 표 (★ = 가장 좋은 것) + 방식마다 기록.\n처음 몇 번 해 보고 맞는 방식을 4 카드에 넣으면 됨.",
    "btn_models": "모델 목록 편집\n4 카드 · 방식 비교에 보일 모델을 넣고 빼기 (숨김일 뿐 · 언제든 다시).",
    "btn_ilc": "한계 ACLR (ILC)\n모델 없이 측정을 되풀이해 이 PA 를 DPD 로 어디까지 만들 수 있나 (기준선).\nDPD 와 3 dB 넘게 차이 나면 모델 · 방식을 바꿔 볼 것.",
}


class App:
    FIELDS = [
        # (키, 이름, 기본값, 단위, 카드)
        ("smw_host", "SMW200A 주소", "demo", "", "dev"),
        ("fsw_host", "FSW 주소", "demo", "", "dev"),
        ("mod", "변조", "256QAM", "", "sig"),
        ("n_sym", "OFDM 심볼 수", "28", "개 (14 = 0.5 ms)", "sig"),
        ("papr_db", "CFR 목표 PAPR", "8.0", "dB", "sig"),
        ("freq_ghz", "주파수", "3.6", "GHz", "rf"),
        ("sg_dbm", "SG 레벨 (RMS)", "-10", "dBm", "rf"),
        ("sg_max_dbm", "SG 최대", "0", "dBm (넘지 않음)", "rf"),
        ("in_loss_db", "입력 손실 (SG→PA)", "0.5", "dB", "rf"),
        ("out_loss_db", "출력 손실 (PA→FSW)", "40", "dB (감쇠기 포함)", "rf"),
        ("cal_db", "Pout 보정", "0", "dB", "rf"),
        ("pout_band", "Pout 대역", "auto", "MHz (auto = 반송파 채널)", "rf"),
        ("fsw_ref_dbm", "FSW Ref Level", "auto", "dBm 또는 auto (권장)", "rf"),
        ("ref_margin_db", "Ref 여유", "3", "dB (auto 때 최고값 위로)", "rf"),
        ("fsw_att_db", "FSW 감쇠", "auto", "dB 또는 auto", "rf"),
        ("fsw_rbw_khz", "FSW RBW", "", "kHz (빈칸 = 화면 그대로)", "rf"),
        ("iq_avg", "I/Q 평균", "2", "주기 (2 ~ 32)", "rf"),
        ("settle_s", "읽기 전 기다림", "0.5", "초 (SG 신호 · 레벨 바꾼 뒤 · 0 = 바로)", "rf"),
        ("K", "비선형 차수 K", "7", "", "dpd"),
        ("M", "메모리 깊이 M", "4", "탭", "dpd"),
        ("L", "GMP 교차 지연 L", "1", "0 = 메모리 다항식", "dpd"),
        ("L_lag", "  Lagging 항 (l+)", "", "빈칸 = L 과 같음", "dpd"),
        ("L_lead", "  Leading 항 (l−)", "", "빈칸 = L 과 같음", "dpd"),
        ("dvr_seg", "DVR 구간 수", "8", "개 (6 ~ 12)", "dpd"),
        ("iters", "반복 횟수", "4", "번", "dpd"),
        ("mu", "섞기 비율 μ", "0.7", "1 = 새 계수만", "dpd"),
        ("ila_guard_db", "되돌림 기준", "1.0", "dB 나빠지면 최고로 (0 = 끔)", "dpd"),
        ("retry_off", "OFF 보다 나쁘면 다시", "2", "번 (0 = 끔)", "dpd"),
        ("ec_iters", "오차 보정 반복", "6", "번 (ILA 반복 뒤 · 0 = 안 함)", "dpd"),
        ("ec_mu", "오차 보정 μ", "0.5", "(0.05 ~ 1 · 나빠지면 자동 ½)", "dpd"),
        ("peak_lim_db", "최고값 제한", "3", "dB (원래 최고값 +)", "dpd"),
        ("lam", "정규화 λ", "1e-6", "", "dpd"),
        ("n_fit", "학습 샘플 수", "40000", "개", "dpd"),
        ("nn_hidden", "신경망 은닉 크기", "24", "개 (신경망만)", "dpd"),
        ("nn_epochs", "신경망 학습 횟수", "30", "번 (신경망만)", "dpd"),
        ("ilc_iters", "ILC 반복", "15", "번 (한계 ACLR)", "dpd"),
        ("ilc_gain", "ILC 이득", "0.8", "(작을수록 천천히 · 안정)", "dpd"),
        ("focus_mhz", "집중 구간", "", "MHz 예 50-70 (빈칸 = 끔)", "dpd"),
        ("focus_w", "집중 가중", "4", "배 (ILC · DLA)", "dpd"),
    ]
    MODELS = (("gmp", "GMP"), ("sgmp", "구간 GMP"), ("dvr", "DVR"), ("nn", "신경망"), ("pnn", "위상정규 신경망"),
              ("tnn", "torch 신경망"))
    MODEL_TIP = {"gmp": "기본 · 빠르고 안정", "sgmp": "Doherty 피크 레벨 앞뒤로 GMP 두 벌",
                 "dvr": "크기 구간마다 다른 기울기 (Doherty 꺾임에 강함)", "nn": "작은 신경망 (ARVTDNN)",
                 "pnn": "신경망 + 위상 정규화 (진폭 왜곡부터 배움)",
                 "tnn": "PyTorch 큰 신경망 (2층 · 위상 정규화) - torch 없으면 numpy 로 대신"}
    # 모델마다 보이는 칸 (없는 칸은 모든 모델 공통)
    MODEL_ROWS = {"K": ("gmp", "sgmp"), "L": ("gmp", "sgmp"), "L_lag": ("gmp", "sgmp"), "L_lead": ("gmp", "sgmp"), "ec_iters": ("gmp", "sgmp", "dvr"), "ec_mu": ("gmp", "sgmp", "dvr"), "dvr_seg": ("dvr",), "mu": ("gmp", "sgmp", "dvr"),
                  "lam": ("gmp", "sgmp", "dvr"), "nn_hidden": ("nn", "pnn", "tnn"), "nn_epochs": ("nn", "pnn", "tnn"),
                  "_odd": ("gmp", "sgmp"), "_auto": ("gmp",), "_learn": ("gmp", "sgmp", "dvr", "nn", "pnn", "tnn")}
    METHOD_ROWS = {"ec_iters": ("ilaec",), "ec_mu": ("ilaec",), "retry_off": ("ila", "dla", "ilaec")}        # JSH: 학습 방식마다 보이는 칸
    CMP_DEF = ["gmp:ila", "gmp:dla", "sgmp:ila", "dvr:ila", "nn:ila", "pnn:ila"]
    OS_TXT = {"2": "245.76 MSps · 인접 채널 일부만", "3": "368.64 MSps", "4": "491.52 MSps (권장)"}

    def __init__(self, root, host=None, dash_parent=None, main_parent=None):
        """host = PA Bench Monitor (합친 프로그램 안 DPD 측정 화면). 그때는 위쪽 바 · 창 설정 · Esc 는 Bench 가 맡고,
        설정은 Bench 설정 안 'dpd' 에 저장, 큰 숫자는 dash_parent 에, 아래 화면은 main_parent 에 만듦"""
        self.root = root
        self.host = host
        self.dash_parent, self.main_parent = dash_parent, main_parent
        if host is None:                                # 단독: 화면 (버튼 · 칸) 오류도 로그에 파일 · 줄 번호로
            root.report_callback_exception = lambda et, ev, tb_: self.q.put(("log", "✖ 화면 오류 %s" % err_text(ev)))
        if host is not None:
            ds = host.s.get("dpd")
            if not isinstance(ds, dict) or not ds:
                ds = self.load_settings()                # 예전 PA DPD Tester 설정을 처음 한 번 가져옴
                host.s["dpd"] = ds
            self.s = ds
        else:
            self.s = self.load_settings()
            root.title("%s %s" % (APP, VERSION))
            try:
                base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
                ico = os.path.join(base, "assets", "dpd.png")
                if os.path.isfile(ico):
                    self._icon = tk.PhotoImage(file=ico)
                    root.iconphoto(True, self._icon)
            except Exception:
                pass
            root.geometry(self.s.get("geometry", "1500x920"))
        if not self.s.get("_mig_ref_auto"):
            # 예전 기본값 (FSW Ref 10 dBm 고정) → auto : 고정 Ref 가 높으면 I/Q 잡음 바닥이 올라가 DPD 가 거기서 멈춤
            for d_ in [self.s] + [v_ for v_ in (self.s.get("dpd_by_type") or {}).values() if isinstance(v_, dict)]:
                if str(d_.get("fsw_ref_dbm", "")).strip() in ("10", "10.0"):
                    d_["fsw_ref_dbm"] = "auto"
            self.s["_mig_ref_auto"] = 1
        self.v = {}
        self.q = queue.Queue()
        self.engine = None
        self.worker = None
        self.results = []
        self.recs = []
        self.bench = None
        self._themed = []
        if host is not None:
            self.apply_theme(host.s.get("theme", "light"), redraw=False, styles=False)
        else:
            self.apply_theme(self.s.get("theme", "light"), redraw=False)
        self.build()
        if host is None:
            root.protocol("WM_DELETE_WINDOW", self.on_close)
            root.bind_all("<Escape>", lambda e: self.stop())
        root.after(100, self.pump)
        if E is None:
            messagebox.showerror(APP, "계산 모듈을 불러오지 못했습니다 (numpy 필요):\n%s" % _IMPORT_ERR)

    # ------------------------------------------------------------ 설정
    def load_settings(self):
        try:
            with open(SET_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_settings(self):
        s = self.s
        try:
            if getattr(self, "_cur_type", None) is not None:
                self.store_type(key=self._cur_type)      # 지금 샘플 종류 · 대역의 값
        except Exception:
            pass

        for k, var in self.v.items():
            s[k] = var.get()
        if self.host is not None:
            return                                     # Bench 가 자기 설정 파일에 같이 저장 (30초마다 · 닫을 때)
        try:
            s["geometry"] = self.root.geometry()
            os.makedirs(SET_DIR, exist_ok=True)
            with open(SET_FILE, "w", encoding="utf-8") as f:
                json.dump(s, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    # ------------------------------------------------------------ 테마 (PA Bench Monitor 와 같음)
    @staticmethod
    def draw_switch(img, bg, track, knob, on, W=30, H=16):
        def hx(c):
            c = c.lstrip("#")
            return [int(c[i:i + 2], 16) for i in (0, 2, 4)]
        Bc, TR, KN = hx(bg), hx(track), hx(knob)
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
                        px_, py_ = x + (sx + 0.5) / SS, y + (sy + 0.5) / SS
                        cx = min(max(px_, r), W - r)
                        if (px_ - cx) ** 2 + (py_ - r) ** 2 <= r * r:
                            ct += 1
                            if (px_ - kx) ** 2 + (py_ - r) ** 2 <= kr * kr:
                                ck += 1
                a, k = ct / 9.0, ck / 9.0
                col = [Bc[i] + (TR[i] - Bc[i]) * a for i in range(3)]
                col = [col[i] + (KN[i] - col[i]) * k for i in range(3)]
                line.append("#%02x%02x%02x" % tuple(int(round(v)) for v in col))
            rows.append("{" + " ".join(line) + "}")
        img.put(" ".join(rows))

    def apply_switch_style(self, st):
        T = THEME
        off_t = "#455a64" if self.s.get("theme") == "dark" else "#b0bec5"
        if not hasattr(self, "_sw_imgs"):
            self._sw_imgs = {k: tk.PhotoImage(width=30, height=16) for k in ("on", "off", "don", "doff")}
            im = self._sw_imgs
            try:
                st.element_create("Switchp.indicator", "image", im["off"], ("disabled", "selected", im["don"]),
                                  ("disabled", im["doff"]), ("selected", im["on"]), border=0, padding=(0, 0, 6, 0), sticky="w")
            except tk.TclError:
                pass
        im = self._sw_imgs
        bg = T["panel"]
        self.draw_switch(im["on"], bg, T["accent"], "#ffffff", True)
        self.draw_switch(im["off"], bg, off_t, "#ffffff", False)
        self.draw_switch(im["don"], bg, mix(T["accent"], bg, 0.55), mix("#ffffff", bg, 0.3), True)
        self.draw_switch(im["doff"], bg, mix(off_t, bg, 0.55), mix("#ffffff", bg, 0.3), False)
        try:
            st.layout("Card.TCheckbutton", [("Checkbutton.padding", {"sticky": "nswe", "children": [
                ("Switchp.indicator", {"side": "left", "sticky": ""}),
                ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [("Checkbutton.label", {"sticky": "nswe"})]})]})])
            st.configure("Card.TCheckbutton", padding=(1, 2, 4, 2))
        except tk.TclError:
            pass

    def apply_theme(self, name="light", redraw=True, styles=True):
        name = name if name in THEMES else "light"
        self.s["theme"] = name
        THEME.clear()
        THEME.update(THEMES[name])
        T = THEME
        if not styles:                                 # Bench 안: ttk 모양은 Bench 가 정함 → 색만 다시 칠함
            for w, opts in list(self._themed):
                try:
                    w.configure(**{k: THEME[v] for k, v in opts.items()})
                except tk.TclError:
                    pass
            if redraw and hasattr(self, "p_spec"):
                for p in (self.p_spec, self.p_trend, self.p_amam, self.p_ampm, self.p_hist):
                    p.redraw()
            if hasattr(self, "f_recinfo"):
                self.show_rec()
            return
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=T["bg"], foreground=T["fg"], fieldbackground=T["entry"], font=F,
                     bordercolor=T["border"], lightcolor=T["panel"], darkcolor=T["border"], troughcolor=T["bg"],
                     selectbackground=T["accent"], selectforeground="white", insertcolor=T["fg"])
        st.map(".", foreground=[("disabled", T["unit"])])
        for sty in ("TFrame", "TLabel", "TCheckbutton", "TRadiobutton"):
            st.configure(sty, background=T["bg"], foreground=T["fg"])
        st.configure("Card.TFrame", background=T["panel"])
        st.configure("Card.TLabel", background=T["panel"], foreground=T["fg"])
        st.configure("Lab.TLabel", background=T["panel"], foreground=T["dim"])
        st.configure("Unit.TLabel", background=T["panel"], foreground=T["unit"])
        st.configure("Hint.TLabel", background=T["panel"], foreground=T["unit"], font=("맑은 고딕", 8))
        st.configure("Info.TLabel", background=T["panel"], foreground=T["info"])
        st.configure("Head.TLabel", background=T["panel"], foreground=T["fg"], font=("맑은 고딕", 10, "bold"))
        st.configure("Num.TLabel", background=T["accent"], foreground="white", font=("맑은 고딕", 8, "bold"), anchor="center", width=2)
        st.configure("Card.TCheckbutton", background=T["panel"], foreground=T["fg"])
        st.map("Card.TCheckbutton", background=[("active", T["panel"])])
        self.apply_switch_style(st)
        st.configure("TEntry", fieldbackground=T["entry"], foreground=T["fg"], bordercolor=T["entry_bd"],
                     lightcolor=T["entry"], darkcolor=T["entry"], padding=(4, 2))
        st.map("TEntry", bordercolor=[("focus", T["accent"])], lightcolor=[("focus", T["accent"])])
        st.configure("TCombobox", fieldbackground=T["entry"], foreground=T["fg"], background=T["btn"],
                     arrowcolor=T["fg"], bordercolor=T["entry_bd"])
        st.map("TCombobox", fieldbackground=[("readonly", T["entry"])], foreground=[("readonly", T["fg"])],
               selectbackground=[("readonly", T["entry"])], selectforeground=[("readonly", T["fg"])])
        self.root.option_add("*TCombobox*Listbox.background", T["entry"])
        self.root.option_add("*TCombobox*Listbox.foreground", T["fg"])
        st.configure("TButton", background=T["btn"], foreground=T["fg"], bordercolor=T["entry_bd"],
                     lightcolor=T["btn"], darkcolor=T["btn"], padding=(8, 3), focuscolor=T["btn"])
        st.map("TButton", background=[("pressed", T["btn_act"]), ("active", T["btn_act"])])
        st.configure("Pri.TButton", background=T["accent"], foreground="white", bordercolor="#1565c0",
                     lightcolor=T["accent"], darkcolor=T["accent"], font=FB)
        st.map("Pri.TButton", background=[("pressed", "#1565c0"), ("active", "#1976d2"), ("disabled", T["border"])])
        st.configure("Seg.Toolbutton", background=T["panel"], foreground=T["accent"], padding=(10, 3), borderwidth=1,
                     relief="solid", bordercolor=T["accent"], lightcolor=T["panel"], darkcolor=T["panel"], font=F, anchor="center")
        st.map("Seg.Toolbutton", background=[("selected", T["accent"]), ("active", T["sel"])], foreground=[("selected", "white")],
               lightcolor=[("selected", T["accent"])], darkcolor=[("selected", T["accent"])], relief=[("selected", "solid")])
        st.configure("Tab.Toolbutton", background=T["head"], foreground=T["dim"], padding=(16, 6), font=("맑은 고딕", 10),
                     borderwidth=0, relief="flat", lightcolor=T["head"], darkcolor=T["head"])
        st.map("Tab.Toolbutton", background=[("selected", T["accent"]), ("active", T["sel"])], foreground=[("selected", "white")],
               lightcolor=[("selected", T["accent"])], darkcolor=[("selected", T["accent"])], relief=[("selected", "flat")])
        st.configure("Treeview", background=T["panel"], fieldbackground=T["panel"], foreground=T["fg"], bordercolor=T["border"], rowheight=22)
        st.map("Treeview", background=[("selected", T["sel"])], foreground=[("selected", T["fg"])])
        st.configure("Treeview.Heading", background=T["head"], foreground=T["dim"], bordercolor=T["border"], font=FB, relief="flat")
        for sb_ in ("TScrollbar", "Vertical.TScrollbar", "Horizontal.TScrollbar"):
            st.configure(sb_, background=T["border"], troughcolor=T["bg"], bordercolor=T["bg"], arrowcolor=T["dim"],
                         lightcolor=T["border"], darkcolor=T["border"], gripcount=0)
        st.configure("TPanedwindow", background=T["bg"])
        self.root.configure(bg=T["bg"])
        for w, opts in list(self._themed):
            try:
                w.configure(**{k: THEME[v] for k, v in opts.items()})
            except tk.TclError:
                pass
        if hasattr(self, "theme_btns"):
            for k, b in self.theme_btns.items():
                b.configure(bg="#1e88e5" if k == name else "#263340")
        if redraw and hasattr(self, "p_spec"):
            for p in (self.p_spec, self.p_trend, self.p_amam, self.p_ampm, self.p_hist):
                p.redraw()
        if hasattr(self, "f_recinfo"):
            self.show_rec()

    def reg(self, w, **opts):
        self._themed.append((w, opts))
        try:
            w.configure(**{k: THEME[v] for k, v in opts.items()})
        except tk.TclError:
            pass
        return w

    def card(self, parent, title, num=None, right_text=None):
        """PA Bench Monitor 와 같은 카드: 번호 · 제목 · 구분선 · 내용"""
        outer = tk.Frame(parent, highlightthickness=1, bd=0)
        self.reg(outer, bg="panel", highlightbackground="border", highlightcolor="border")
        hd = ttk.Frame(outer, style="Card.TFrame")
        hd.pack(fill="x", padx=10, pady=(7, 5))
        arrow = None
        if num is not None:
            arrow = ttk.Label(hd, text="▾", style="Head.TLabel", width=2, cursor="hand2")
            arrow.pack(side="left")
            ttk.Label(hd, text=str(num), style="Num.TLabel").pack(side="left", padx=(0, 7))
        ttk.Label(hd, text=title, style="Head.TLabel").pack(side="left")
        rt = ttk.Label(hd, text=right_text or "", style="Hint.TLabel")
        rt.pack(side="right")
        sep = tk.Frame(outer, height=1)
        self.reg(sep, bg="line")
        sep.pack(fill="x")
        body = ttk.Frame(outer, style="Card.TFrame", padding=(10, 6, 10, 8))
        body.pack(fill="both", expand=True)
        outer.body, outer.right = body, rt
        if num is not None:
            self.__dict__.setdefault("cards_num", {})[num] = (title, body)
        if num is not None:
            # 제목 줄을 누르면 접기 · 펼치기 (▾ 펼침 · ▸ 접힘), 상태는 설정에 기억
            key = str(num)

            def show(open_):
                if open_:
                    sep.pack(fill="x")
                    body.pack(fill="both", expand=True)
                else:
                    sep.pack_forget()
                    body.pack_forget()
                arrow.configure(text="▾" if open_ else "▸")

            def toggle(e=None):
                fold = self.s.setdefault("fold", {})
                fold[key] = not fold.get(key, False)
                show(not fold[key])
                self.save_settings()
            for w in [hd] + list(hd.winfo_children()):
                if w is not rt:
                    w.bind("<Button-1>", toggle)
            hd.configure(cursor="hand2")
            if self.s.get("fold", {}).get(key):
                show(False)
            outer.toggle = toggle
        return outer

    def seg(self, parent, var, items):
        f = ttk.Frame(parent, style="Card.TFrame")
        for val, txt in items:
            ttk.Radiobutton(f, text=txt, value=val, variable=var, style="Seg.Toolbutton").pack(side="left")
        return f

    def set_theme(self, name):
        self.apply_theme(name)
        self.save_settings()

    # ------------------------------------------------------------ 화면 만들기
    def attach_tips(self):
        """칸 · 버튼에 마우스를 올리면 설명 (TIPS). 그 칸과 같은 줄 이름 (왼쪽 글자) 에도"""
        names = {}
        for k, var in self.v.items():
            names[str(var)] = k

        def walk(w):
            for c in w.winfo_children():
                key = None
                for opt in ("textvariable", "variable"):
                    try:
                        key = names.get(str(c.cget(opt)))
                    except tk.TclError:
                        pass
                    if key:
                        break
                if key and key in TIPS:
                    Tip(c, TIPS[key])
                    try:                                  # 같은 줄 왼쪽 이름에도
                        gi = c.grid_info()
                        if gi:
                            for sib in c.master.grid_slaves(row=int(gi["row"]), column=0):
                                if sib is not c and not getattr(sib, "_tip", False):
                                    Tip(sib, TIPS[key])
                                    sib._tip = True
                    except (tk.TclError, KeyError, ValueError):
                        pass
                walk(c)
        walk(self.left_root if hasattr(self, "left_root") else self.root)
        for attr, txt in BTN_TIPS.items():
            if hasattr(self, attr):
                Tip(getattr(self, attr), txt)

    def build(self):
        if self.host is None:
            self.build_topbar()
            self.build_dashboard()
            main = ttk.PanedWindow(self.root, orient="horizontal")
            main.pack(fill="both", expand=True, padx=6, pady=(6, 6))
        else:
            self.chips = {}
            self.build_tiles(self.dash_parent)
            main = ttk.PanedWindow(self.main_parent, orient="horizontal")
            main.pack(fill="both", expand=True)
        self.main_pw = main
        left = ScrollFrame(main)
        self.reg(left.cv, bg="bg")
        main.add(left, weight=0)
        inner = left.body
        right = ttk.Frame(main)
        main.add(right, weight=1)
        self.build_left(inner)
        self.build_right(right)
        self.left_sf = left
        # 왼쪽 카드: 폭이 줄면 설명 · 단위 · 체크 글자가 다음 줄로 (잘리지 않게) · 폭은 가운데 경계를 끌어 바꾸고 기억
        left.cv.bind("<Configure>", lambda e: self.reflow_soon(), add="+")
        main.bind("<ButtonRelease-1>", lambda e: self.root.after(100, self.save_left_w), add="+")
        self.root.after(300, self.restore_left_w)

        def no_combo_wheel(w):
            # 왼쪽 카드의 고르기 칸 (Combobox) 위에서 휠 = 값 바뀌지 않고 카드 스크롤
            for c_ in w.winfo_children():
                if isinstance(c_, ttk.Combobox):
                    c_.bind("<MouseWheel>", lambda e: (wheel_any(e), "break")[1])
                    c_.bind("<Button-4>", lambda e: (wheel_any(e), "break")[1])
                    c_.bind("<Button-5>", lambda e: (wheel_any(e), "break")[1])
                no_combo_wheel(c_)
        no_combo_wheel(inner)
        self.left_root = main
        try:
            self.attach_tips()
        except Exception as e:
            print("tips", e)
        self.log("%s %s - 주소에 demo 를 넣으면 장비 없이 가상 PA 로 확인합니다." % (APP, VERSION))
        self.sig_src_changed()
        self.bench_refresh()
        self.update_chips()
        self.refresh_hist_list()
        self.draw_hist()

    def build_topbar(self):
        bar = tk.Frame(self.root, bg=TOPBAR)
        bar.pack(side="top", fill="x")
        tk.Label(bar, text=APP, bg=TOPBAR, fg="white", font=("맑은 고딕", 12, "bold")).pack(side="left", padx=(12, 4), pady=6)
        tk.Label(bar, text=VERSION + "  (NR 100 MHz · 메모리 다항식 / GMP)", bg=TOPBAR, fg="#90a4ae",
                 font=("맑은 고딕", 8)).pack(side="left", padx=(0, 14))
        self.chips = {}
        for key, name in (("smw", "SMW200A"), ("fsw", "FSW"), ("psu", "Vdd 전원"), ("bench", "PA Bench 기록")):
            c = tk.Label(bar, text="● " + name, bg="#263340", fg="#78909c", font=("맑은 고딕", 8), padx=8, pady=2)
            c.pack(side="left", padx=2)
            self.chips[key] = c
        mk = dict(relief="flat", padx=10, pady=2, activeforeground="#ffffff", font=F, bd=0)
        tk.Button(bar, text="⚙ 장비 설정", command=lambda: self.show_tab("equip"), bg="#2f3d4a", fg="#e0e0e0",
                  activebackground="#3c4d5c", **mk).pack(side="left", padx=(12, 3))
        self.btn_rf_top = tk.Button(bar, text="SG RF", command=self.rf_toggle, bg="#2f3d4a", fg="#e0e0e0",
                                    activebackground="#3c4d5c", **mk)
        self.btn_rf_top.pack(side="left", padx=3)
        self.btn_cfg = tk.Button(bar, text="💾 설정 ▾", command=self.cfg_menu, bg="#2f3d4a", fg="#e0e0e0",
                                 activebackground="#3c4d5c", **mk)
        self.btn_cfg.pack(side="left", padx=3)
        tf = tk.Frame(bar, bg="#455a64")
        tf.pack(side="left", padx=(10, 3))
        self.theme_btns = {}
        for key, txt in (("light", "☀ 화이트"), ("dark", "🌙 블랙")):
            b = tk.Button(tf, text=txt, command=lambda k=key: self.set_theme(k), relief="flat", bd=0, padx=10, pady=1,
                          font=F, bg="#1e88e5" if self.s.get("theme", "light") == key else "#263340", fg="white",
                          activebackground="#1976d2", activeforeground="white")
            b.pack(side="left", padx=1, pady=1)
            self.theme_btns[key] = b
        self.btn_stop = tk.Button(bar, text="■ 중지 (Esc)", command=self.stop, bg="#c62828", fg="white",
                                  activebackground="#e53935", activeforeground="white", relief="flat", bd=0, padx=16, pady=3,
                                  font=("맑은 고딕", 10, "bold"))
        self.btn_stop.pack(side="right", padx=10, pady=5)

    def build_dashboard(self):
        d = tk.Frame(self.root, bg=DASH)
        d.pack(side="top", fill="x")
        cols = tk.Frame(d, bg=DASH)
        cols.pack(fill="x", padx=8, pady=6)
        self.big = {}
        for i, (key, title, color) in enumerate((("aclr", "ACLR  L / U", C_ACLR),
                                                 ("pout", "Pout · DUT", C_POUT), ("eff", "PAE (효율)", C_EFF),
                                                 ("papr", "PAPR · DPD 신호", C_PAPR))):
            cols.columnconfigure(i, weight={"aclr": 13, "papr": 8}.get(key, 10), uniform="c")
            box = tk.Frame(cols, bg=BG, highlightthickness=1, highlightbackground="#2b3540")
            box.grid(row=0, column=i, sticky="nsew", padx=4)
            tk.Label(box, text=title, bg=BG, fg="#b0bec5", font=("맑은 고딕", 12, "bold")).pack(anchor="w", padx=12, pady=(5, 0))
            v = tk.Label(box, text="---", bg=BG, fg=color, font=("Consolas", 34, "bold"), anchor="w", width=1)
            v.pack(fill="x", padx=10)
            sub = tk.Label(box, text="", bg=BG, fg="#90a4ae", font=("Consolas", 11), anchor="w")
            sub.pack(fill="x", padx=12, pady=(0, 6))
            self.big[key] = (v, sub, color)
        # 진행 상태 (PA Bench Monitor 의 오른쪽 위 작은 상태판처럼) - 모양 A / B / C (설정 status_ui)
        ui = self.s.get("status_ui", "A")
        self.st_donut = None
        self.st_rows = {}
        if ui == "B":                                   # B: 큰 숫자 아래 얇은 진행 줄 (전체 폭)
            strip = tk.Frame(d, bg=BG, highlightthickness=1, highlightbackground="#2b3540")
            strip.pack(fill="x", padx=12, pady=(0, 6))
            self.st_state = tk.Label(strip, text="● 대기", bg="#37474f", fg="white", font=("맑은 고딕", 10, "bold"), padx=8)
            self.st_state.pack(side="left", padx=(6, 8), pady=4)
            self.st_bar = tk.Canvas(strip, width=180, height=8, bg="#263340", highlightthickness=0, bd=0)
            self.st_bar.pack(side="left", padx=(0, 6))
            self.st_left = tk.Label(strip, text="", bg=BG, fg="#ffd54f", font=("Consolas", 13, "bold"), width=6, anchor="w")
            self.st_left.pack(side="left")
            for k, t_ in (("prog", ""), ("stage", "단계"), ("level", "레벨"), ("model", "모델"), ("dc", "Vdd · Id")):
                if t_:
                    tk.Label(strip, text=t_, bg=BG, fg="#78909c", font=("맑은 고딕", 9)).pack(side="left", padx=(14, 4))
                lb = tk.Label(strip, text="-", bg=BG, fg="#cfd8dc", font=("Consolas", 10), anchor="w")
                lb.pack(side="left")
                self.st_rows[k] = lb
        else:
            cols.columnconfigure(4, weight=12 if ui == "A" else 11, uniform="c")
            box = tk.Frame(cols, bg=BG, highlightthickness=1, highlightbackground="#2b3540")
            box.grid(row=0, column=4, sticky="nsew", padx=4)
            if ui == "C":                               # C: 왼쪽 동그란 남은 시간 + 오른쪽 4줄
                self.st_donut = tk.Canvas(box, width=96, height=96, bg=BG, highlightthickness=0, bd=0)
                self.st_donut.pack(side="left", padx=(10, 6), pady=6)
                rt = tk.Frame(box, bg=BG)
                rt.pack(side="left", fill="both", expand=True, pady=6, padx=(0, 8))
                self.st_state = tk.Label(rt, text="● 대기", bg="#37474f", fg="white", font=("맑은 고딕", 10, "bold"), padx=8)
                self.st_state.pack(anchor="w", pady=(0, 4))
                self.st_left = tk.Label(rt)               # 남은 시간은 동그라미 안에
                self.st_bar = None
                g = tk.Frame(rt, bg=BG)
                g.pack(fill="x")
                g.columnconfigure(1, weight=1)
                for r_, (k, t_) in enumerate((("stage", "단계"), ("level", "레벨"), ("model", "모델"), ("dc", "Vdd·Id"))):
                    tk.Label(g, text=t_, bg=BG, fg="#78909c", font=("맑은 고딕", 9), anchor="w").grid(row=r_, column=0, sticky="w")
                    lb = tk.Label(g, text="-", bg=BG, fg="#cfd8dc", font=("Consolas", 10), anchor="w", width=1)
                    lb.grid(row=r_, column=1, sticky="ew", padx=(6, 0))
                    self.st_rows[k] = lb
                self.st_rows["prog"] = tk.Label(rt)
            else:                                       # A: 상태 · 남은 시간 · 막대 + 2열 칸
                hd = tk.Frame(box, bg=BG)
                hd.pack(fill="x", padx=10, pady=(6, 2))
                self.st_state = tk.Label(hd, text="● 대기", bg="#37474f", fg="white", font=("맑은 고딕", 10, "bold"), padx=8)
                self.st_state.pack(side="left")
                self.st_left = tk.Label(hd, text="", bg=BG, fg="#ffd54f", font=("Consolas", 15, "bold"))
                self.st_left.pack(side="right")
                self.st_bar = tk.Canvas(box, height=6, bg="#263340", highlightthickness=0, bd=0)
                self.st_bar.pack(fill="x", padx=10, pady=(2, 4))
                g = tk.Frame(box, bg=BG)
                g.pack(fill="both", expand=True, padx=10, pady=(0, 6))
                g.columnconfigure(1, weight=1)
                for r_, (k, t_) in enumerate((("stage", "단계"), ("level", "레벨"), ("model", "모델"))):
                    tk.Label(g, text=t_, bg=BG, fg="#78909c", font=("맑은 고딕", 9), anchor="w").grid(row=r_, column=0, sticky="w")
                    lb = tk.Label(g, text="-", bg=BG, fg="#cfd8dc", font=("Consolas", 10), anchor="w", width=1)
                    lb.grid(row=r_, column=1, sticky="ew", padx=(8, 0))
                    self.st_rows[k] = lb
                self.st_rows["prog"] = tk.Label(hd, text="", bg=BG, fg="#90a4ae", font=("Consolas", 10))
                self.st_rows["prog"].pack(side="right", padx=(0, 10))
                self.st_rows["dc"] = tk.Label(g)
        self.st_last = None                         # 끝 · 중지 표시
        self.root.after(500, self.tick_status)
        d.bind("<Configure>", lambda e: self.root.after_idle(self.fit_big))

    def build_tiles(self, cols):
        """Bench 안: 큰 숫자 3칸 (ACLR · Pout · PAE, PAE 아래 줄에 DE · Id · PAPR) - Bench 큰 숫자와 같은 칸 크기"""
        self.big = {}
        for i, (key, title, color) in enumerate((("aclr", "ACLR  L / U", C_ACLR), ("pout", "Pout · DUT", C_POUT),
                                                 ("eff", "PAE (효율)", C_EFF))):
            box = tk.Frame(cols, bg=BG, highlightthickness=1, highlightbackground="#2b3540")
            box.grid(row=0, column=i, sticky="nsew", padx=4)
            tk.Label(box, text=title, bg=BG, fg="#b0bec5", font=("맑은 고딕", 12, "bold")).pack(anchor="w", padx=12, pady=(5, 0))
            v = tk.Label(box, text="---", bg=BG, fg=color, font=("Consolas", 34, "bold"), anchor="w", width=1)
            v.pack(fill="x", padx=10)
            sub = tk.Label(box, text="", bg=BG, fg="#90a4ae", font=("Consolas", 11), anchor="w", width=1)
            sub.pack(fill="x", padx=12, pady=(0, 6))
            self.big[key] = (v, sub, color)
        cols.bind("<Configure>", lambda e: self.root.after_idle(self.fit_big), add="+")
        self.st_rows, self.st_bar, self.st_donut = {}, None, None
        self.st_last = None

    def status_info(self):
        """Bench 공통 진행 줄에 보여 줄 것 → (상태 글, 색, 진행 0~1 또는 None, 남은 시간 글, [(이름, 값)])"""
        eng, run = self.engine, bool(self.worker)
        e = getattr(eng, "eta", None) if eng is not None else None
        done, tot = (e["done"], e["total"]) if e else (0, 0)
        if run:
            prog = getattr(self, "_last_prog", "") or ""
            st, col = ("● 모델 계산", "#6a1b9a") if "계산" in prog else ("● 신경망 학습", "#6a1b9a") if "학습 중" in prog else ("● DPD 측정 중", "#2e7d32")
            left = eng.eta_left() if eng is not None and hasattr(eng, "eta_left") else None
            lt = ("%d:%02d" % (left // 60, left % 60)) if left is not None else ""
        else:
            st, col = self.st_last or ("● 대기", "#37474f")
            lt = ""
        info = self._stage_info()
        items = [("진행", ("%d / %d 측정" % (done, tot)) if tot else "-"), ("단계", info[0]), ("레벨", info[1]), ("모델", info[2])]
        return st, col, (min(done / tot, 1.0) if tot else None), lt, items

    def _stage_info(self):
        eng = self.engine
        p = self.params_safe()
        cur = getattr(eng, "cur", None) if eng is not None else None
        lab = cur.get("label", "") if cur else ""
        stage = "-"
        if lab:
            import re
            m = re.match(r"(?:.*\s)?DPD (\d+)$", lab)
            if lab.endswith("OFF"):
                stage = "DPD OFF 측정"
            elif "후보" in lab:
                stage = "모델 후보 실측 · " + lab.split("후보", 1)[1].strip()
            elif lab.startswith("ILC"):
                stage = "%s / %s (한계 ACLR)" % (lab, p.get("ilc_iters", "?"))
            elif "이전 계수" in lab:
                stage = "DPD 0 (앞 레벨 계수)"
            elif m:
                stage = "DPD 반복 %s / %s" % (m.group(1), p.get("iters", "?"))
            else:
                stage = lab
        stp = cur.get("step", "") if cur else ""
        mtxt = self.model_text(p, eng)
        return stage, (stp.replace("레벨 ", "") if stp else "-"), mtxt

    def follow_sample(self, name):
        """Bench 에서 고른 샘플 종류 · 번호를 3 카드에 → 그 샘플에서 쓰던 DPD 설정 · 그 번호의 기록으로"""
        if not name or self.worker:
            return
        num = self.sample_num()
        if self.v["b_stype"].get() != name:
            self.bench_refresh()
            vals = list(self.cb_stype.cget("values") or ())
            if name in vals:
                self.v["b_stype"].set(name)
                self._rec_num = num
                self.load_records()
        elif num != getattr(self, "_rec_num", None):
            self._rec_num = num                        # 같은 종류 · 다른 번호 (S1 → S2): 기록만 다시 고름
            self.bench_refresh()
            self.load_records()

    def shutdown(self, wait=4.0):
        """Bench 를 닫을 때: 설정 저장 (화면 스레드) → 측정 멈춤 · FSW 원래대로 · 연결 끊기는 따로 (최대 wait 초)
        장비가 응답 없어도 닫기가 멈추지 않게 (예전: FSW 응답을 30 초씩 기다려 '응답 없음' → 작업 관리자로 꺼야 했음)"""
        BL.set_busy(False)
        try:
            self.save_settings()
        except Exception:
            pass
        eng, run = self.engine, bool(self.worker)
        self.engine = None
        if eng is None:
            return
        self.close_engine(eng, run, wait)

    @staticmethod
    def close_engine(eng, running, wait=4.0):
        def job():
            try:
                if running:
                    eng.abort(rf_off=True)
                    return
                for d in (getattr(eng, "smw", None), getattr(eng, "fsw", None)):
                    io = getattr(d, "io", None)
                    if io is not None:
                        try:
                            io.set_timeout(3.0)            # 닫을 땐 3 초만 기다림
                        except Exception:
                            pass
                eng.close()
            except Exception:
                pass
        t = threading.Thread(target=job, daemon=True)
        t.start()
        t.join(wait)

    def tick_status(self):
        """오른쪽 위 진행 상태판 (0.4초마다): 상태 · 남은 시간 · 진행 막대 · 단계 · 레벨 · 모델 · Vdd/Id"""
        try:
            self._tick_status()
        except Exception:
            pass
        self.root.after(400, self.tick_status)

    def _tick_status(self):
        if not hasattr(self, "st_state"):
            return
        eng, run = self.engine, bool(self.worker)
        R = self.st_rows
        e = getattr(eng, "eta", None) if eng is not None else None
        p = self.params_safe()
        if run:
            prog = getattr(self, "_last_prog", "") or ""
            st, col = ("● 모델 계산", "#6a1b9a") if "계산" in prog else ("● 신경망 학습", "#6a1b9a") if "학습 중" in prog else ("● 측정 중", "#2e7d32")
            self.st_state.configure(text=st, bg=col)
            left = eng.eta_left() if eng is not None and hasattr(eng, "eta_left") else None
            self.st_left.configure(text=("%d:%02d" % (left // 60, left % 60)) if left is not None else "")
        else:
            txt, col = self.st_last or ("● 대기", "#37474f")
            self.st_state.configure(text=txt, bg=col)
            self.st_left.configure(text="")
        done, tot = (e["done"], e["total"]) if e else (0, 0)
        R["prog"].configure(text=("%d / %d 측정" % (done, tot)) if tot else "-")
        cv = self.st_bar
        if cv is not None:
            W = max(cv.winfo_width(), 10)
            H_ = max(cv.winfo_height(), 6)
            cv.delete("all")
            if tot:
                cv.create_rectangle(0, 0, W * min(done / tot, 1.0), H_, fill="#43a047" if not run else "#ffb300", outline="")
        dn = self.st_donut
        if dn is not None:
            dn.delete("all")
            dn.create_oval(8, 8, 88, 88, outline="#263340", width=9)
            if tot:
                dn.create_arc(8, 8, 88, 88, start=90, extent=-359.9 * min(done / tot, 1.0), style="arc",
                              outline="#43a047" if not run else "#ffb300", width=9)
            left = eng.eta_left() if (run and eng is not None and hasattr(eng, "eta_left")) else None
            dn.create_text(48, 42, text=("%d:%02d" % (left // 60, left % 60)) if left is not None else ("완료" if tot and not run else "-"),
                           fill="#ffd54f", font=("Consolas", 15, "bold"))
            dn.create_text(48, 63, text=("%d / %d" % (done, tot)) if tot else "", fill="#90a4ae", font=("Consolas", 9))
        cur = getattr(eng, "cur", None) if eng is not None else None
        lab = cur.get("label", "") if cur else ""
        stage = "-"
        if lab:
            import re
            m = re.match(r"(?:.*\s)?DPD (\d+)$", lab)
            if lab.endswith("OFF"):
                stage = "DPD OFF 측정"
            elif "후보" in lab:
                stage = "모델 후보 실측 · " + lab.split("후보", 1)[1].strip()
            elif lab.startswith("ILC"):
                stage = "%s / %s (한계 ACLR)" % (lab, p.get("ilc_iters", "?"))
            elif "이전 계수" in lab:
                stage = "DPD 0 (앞 레벨 계수)"
            elif m:
                stage = "DPD 반복 %s / %s" % (m.group(1), p.get("iters", "?"))
            else:
                stage = lab
        R["stage"].configure(text=stage)
        stp = cur.get("step", "") if cur else ""
        R["level"].configure(text=stp.replace("레벨 ", "") if stp else "-")
        mtxt = self.model_text(p, eng)
        R["model"].configure(text=mtxt)
        dc = self.read_dc()
        R["dc"].configure(text=("%.1f V · %.3f A" % (dc[0], dc[1])) if dc else "Bench 전원 없음",
                          fg="#cfd8dc" if dc else "#78909c")

    def params_safe(self):
        out = {}
        for k in ("iters", "ilc_iters", "K", "M", "L", "L_lag", "L_lead", "method", "model", "auto_model", "dvr_seg", "nn_hidden"):
            try:
                out[k] = self.v[k].get()
            except Exception:
                pass
        return out

    def fit_big(self):
        if self.host is not None and hasattr(self.host, "fit_big"):
            return self.host.fit_big()                 # Bench 안: 모든 큰 숫자를 같은 크기로 (Bench 가 정함)
        fonts = self.__dict__.setdefault("_bigf", {})
        w = max(self.root.winfo_width(), 600)
        base = int(max(18, min(40, w / 46)))
        if self.host is not None:
            base = getattr(self.host, "_big_base", base)       # Bench 큰 숫자와 같은 크기
        need = base                                     # 모든 칸 같은 크기 (가장 긴 값 기준), 커지는 건 창 크기가 바뀔 때만
        for key, (v, sub, col) in self.big.items():
            avail = v.winfo_width() - 6
            if avail < 40:
                continue
            size = need
            while size > 12:
                f = fonts.get(size) or fonts.setdefault(size, tkfont.Font(family="Consolas", size=size, weight="bold"))
                if f.measure(v.cget("text")) <= avail:
                    break
                size -= 1
            need = min(need, size)
        if w != getattr(self, "_big_w", None) or need < getattr(self, "_big_size", 999):
            self._big_size, self._big_w = need, w
        for key, (v, sub, col) in self.big.items():
            if getattr(v, "_sz", None) != self._big_size:
                v.configure(font=("Consolas", self._big_size, "bold"))
                v._sz = self._big_size

    def field(self, body, r, key, name, dflt, unit, width=8):
        ttk.Label(body, text=name, style="Lab.TLabel").grid(row=r, column=0, sticky="w", pady=2, padx=(0, 8))
        var = tk.StringVar(value=str(self.s.get(key, dflt)))
        self.v[key] = var
        if key == "mod":
            w = ttk.Combobox(body, textvariable=var, values=["QPSK", "16QAM", "64QAM", "256QAM"], state="readonly", width=9)
        else:
            w = ttk.Entry(body, textvariable=var, width=width, justify="right" if width < 12 else "left")
        w.grid(row=r, column=1, sticky="w")
        ttk.Label(body, text=unit, style="Unit.TLabel").grid(row=r, column=2, sticky="w", padx=(5, 0))
        return w

    def build_left(self, inner):
        W = 400
        fields = {}
        for key, name, dflt, unit, card in self.FIELDS:
            fields.setdefault(card, []).append((key, name, dflt, unit))
        # 1 신호
        c = self.card(inner, "신호", 1)
        c.pack(fill="x", padx=(2, 6), pady=(0, 6))
        b = c.body
        self.v["bw_mode"] = tk.StringVar(value=str(self.s.get("bw_mode", "100")))
        ttk.Label(b, text="대역", style="Lab.TLabel").grid(row=0, column=0, sticky="w", pady=2)
        self.seg(b, self.v["bw_mode"], (("100", "100 MHz DPD"), ("400", "400 MHz DPD"))).grid(row=0, column=1, columnspan=2, sticky="w")
        self.v["sig_src"] = tk.StringVar(value=self.s.get("sig_src", "nr"))
        self.v["wv_path"] = tk.StringVar(value=self.s.get("wv_path", ""))
        self.lbl_src = ttk.Label(b, text="신호", style="Lab.TLabel")
        self.lbl_src.grid(row=1, column=0, sticky="w", pady=2)
        self.seg_src = self.seg(b, self.v["sig_src"], (("nr", "NR 100 MHz 1C 만들기"), ("file", "내 .wv")))
        self.seg_src.grid(row=1, column=1, columnspan=2, sticky="w")
        self.f_file = ttk.Frame(b, style="Card.TFrame")
        self.f_file.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        ln1 = ttk.Frame(self.f_file, style="Card.TFrame")
        ln1.pack(side="top", fill="x")
        self.btn_wv = ttk.Button(ln1, text="불러오기…", command=self.pick_wv)
        self.btn_wv.pack(side="left")
        self.lbl_wv = ttk.Label(ln1, text="", style="Info.TLabel", wraplength=280)
        self.lbl_wv.pack(side="left", padx=(6, 0))
        self.lbl_wv_dir = ttk.Label(self.f_file, text="", style="Unit.TLabel", wraplength=360)
        self.lbl_wv_dir.pack(side="top", anchor="w", pady=(2, 0))
        self.v["file_halve"] = tk.BooleanVar(value=bool(self.s.get("file_halve", False)))
        self.chk_halve = ttk.Checkbutton(self.f_file, text="500 MSps 넘으면 절반으로 (빠르게)", variable=self.v["file_halve"],
                                         style="Card.TCheckbutton")
        self.chk_halve.pack(side="top", anchor="w", pady=(4, 0))
        self.v["file_half400"] = tk.BooleanVar(value=bool(self.s.get("file_half400", True)))
        self.chk_up = ttk.Checkbutton(self.f_file, text="1000 MSps 넘으면 절반으로 (권장 · 빠르고 FSW 가 다 받음)", variable=self.v["file_half400"],
                                      style="Card.TCheckbutton")
        self.f_nr = ttk.Frame(b, style="Card.TFrame")
        self.f_nr.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        nb = self.f_nr
        ttk.Label(nb, text="캐리어", style="Lab.TLabel").grid(row=0, column=0, sticky="w", pady=2, padx=(0, 8))
        self.v["carrier"] = tk.StringVar(value="1C")
        self.seg(nb, self.v["carrier"], (("1C", "1C (100 MHz)"), ("2C", "2C · 다음 버전"))).grid(row=0, column=1, columnspan=2, sticky="w")
        self.v["carrier"].trace_add("write", lambda *a: self.v["carrier"].get() != "1C" and self.v["carrier"].set("1C"))
        r = 1
        for key, name, dflt, unit in fields["sig"]:
            self.field(nb, r, key, name, dflt, unit)
            r += 1
        ttk.Label(nb, text="샘플레이트", style="Lab.TLabel").grid(row=r, column=0, sticky="w", pady=2)
        self.v["os"] = tk.StringVar(value=str(self.s.get("os", "4")))
        self.seg(nb, self.v["os"], (("2", "×2"), ("3", "×3"), ("4", "×4"))).grid(row=r, column=1, sticky="w")
        self.lbl_os = ttk.Label(nb, text="", style="Unit.TLabel")
        self.lbl_os.grid(row=r, column=2, sticky="w", padx=(5, 0))
        self.v["os"].trace_add("write", lambda *a: self.lbl_os.configure(text=self.OS_TXT.get(self.v["os"].get(), "")))
        self.lbl_os.configure(text=self.OS_TXT.get(self.v["os"].get(), ""))
        self.v["cfr_on"] = tk.BooleanVar(value=bool(self.s.get("cfr_on", True)))
        ttk.Checkbutton(nb, text="CFR 사용 (클리핑 + 필터로 PAPR 줄이기)", variable=self.v["cfr_on"],
                        style="Card.TCheckbutton").grid(row=r + 1, column=0, columnspan=3, sticky="w", pady=(4, 0))
        af = ttk.Frame(b, style="Card.TFrame")
        af.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        ttk.Label(af, text="ACLR 채널", style="Lab.TLabel").pack(side="left")
        self.v["ch_bw_mhz"] = tk.StringVar(value=str(self.s.get("ch_bw_mhz", "98.28")))
        self.e_chbw = ttk.Entry(af, textvariable=self.v["ch_bw_mhz"], width=6, justify="right")
        self.e_chbw.pack(side="left", padx=3)
        ttk.Label(af, text="MHz · 인접 간격", style="Lab.TLabel").pack(side="left")
        self.v["adj_mhz"] = tk.StringVar(value=str(self.s.get("adj_mhz", "100")))
        self.e_adj = ttk.Entry(af, textvariable=self.v["adj_mhz"], width=5, justify="right")
        self.e_adj.pack(side="left", padx=3)
        ttk.Label(af, text="MHz", style="Unit.TLabel").pack(side="left")
        bf = ttk.Frame(b, style="Card.TFrame")
        bf.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Button(bf, text="신호 만들기", command=self.make_signal).pack(side="left")
        ttk.Button(bf, text=".wv 저장…", command=self.save_wv_orig).pack(side="left", padx=4)
        self.lbl_sig = ttk.Label(b, text="", style="Hint.TLabel", wraplength=W - 40)
        self.lbl_sig.grid(row=6, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self._bw_cur = self.v["bw_mode"].get()
        self.v["bw_mode"].trace_add("write", lambda *a: self.bw_changed())
        self.v["sig_src"].trace_add("write", lambda *a: self.sig_src_changed())
        self.v["wv_path"].trace_add("write", lambda *a: self.sig_src_changed())

        # 2 RF · 손실
        c = self.card(inner, "RF · 케이블 손실", 2)
        c.pack(fill="x", padx=(2, 6), pady=(0, 6))
        b = c.body
        r = 0
        for key, name, dflt, unit in fields["rf"]:
            self.field(b, r, key, name, dflt, unit)
            r += 1
        self.v["bench_loss"] = tk.BooleanVar(value=bool(self.s.get("bench_loss", True)))
        ttk.Checkbutton(b, text="손실 = PA Bench Monitor 손실 표 사용", variable=self.v["bench_loss"],
                        style="Card.TCheckbutton", command=self.apply_bench_loss).grid(row=r, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.lbl_loss = ttk.Label(b, text="", style="Info.TLabel", wraplength=W - 40)
        self.lbl_loss.grid(row=r + 1, column=0, columnspan=3, sticky="w")
        self.v["aclr_src"] = tk.StringVar(value=self.s.get("aclr_src", "fsw_end"))
        ttk.Label(b, text="ACLR 읽기", style="Lab.TLabel").grid(row=r + 4, column=0, sticky="w", pady=(8, 0))
        self.seg(b, self.v["aclr_src"], (("fsw_end", "Mod. OFF·최종"), ("fsw", "Mod. 매번"), ("iq", "I/Q"))).grid(
            row=r + 4, column=1, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(b, text="Mod. = FSW Mod. 화면 (스펙트럼 · 잡음 낮음) ACLR · OFF·최종 = 반복 중엔 I/Q (화면 고정), OFF 와 레벨 끝만 Mod. · 학습은 늘 I/Q",
                  style="Unit.TLabel", wraplength=W - 40).grid(row=r + 5, column=0, columnspan=3, sticky="w")
        self.v["fsw_park"] = tk.BooleanVar(value=bool(self.s.get("fsw_park", True)))
        ttk.Checkbutton(b, text="FSW 화면은 Mod. 화면 (ACLR) 에 두기 (I/Q 받을 때만 잠깐 I/Q)", variable=self.v["fsw_park"],
                        style="Card.TCheckbutton").grid(
            row=r + 6, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.v["cal_mod"] = tk.BooleanVar(value=bool(self.s.get("cal_mod", True)))
        ttk.Checkbutton(b, text="Pout 을 FSW Mod. 화면 채널 전력에 맞춤 (실행 첫 측정에서 Pout 보정 자동)", variable=self.v["cal_mod"],
                        style="Card.TCheckbutton").grid(row=r + 7, column=0, columnspan=3, sticky="w", pady=(2, 0))
        self.v["cal_every"] = tk.BooleanVar(value=bool(self.s.get("cal_every", False)))
        ttk.Checkbutton(b, text="  └ 매 측정마다 다시 맞춤 (반복마다 스펙트럼을 읽음 · 느림)", variable=self.v["cal_every"],
                        style="Card.TCheckbutton").grid(row=r + 9, column=0, columnspan=3, sticky="w", pady=(0, 0))
        ps0 = self.s.get("pout_src", "iq")
        self.v["pout_src"] = tk.StringVar(value=ps0 if ps0 in ("iq", "spec") else "iq")
        ttk.Label(b, text="Pout 기준", style="Lab.TLabel").grid(row=r + 8, column=0, sticky="w", pady=(4, 0))
        self.seg(b, self.v["pout_src"], (("iq", "I/Q + 보정"), ("spec", "스펙트럼 (읽는 측정)"))).grid(
            row=r + 8, column=1, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Label(b, text="추가 손실", style="Lab.TLabel").grid(row=r + 2, column=0, sticky="w", pady=(8, 0))
        xf = ttk.Frame(b, style="Card.TFrame")
        xf.grid(row=r + 2, column=1, columnspan=2, sticky="w", pady=(8, 0))
        self.btn_xloss = ttk.Button(xf, text="표 편집…", command=self.edit_xloss)
        self.btn_xloss.pack(side="left")
        ttk.Button(xf, text="없음", command=self.clear_xloss).pack(side="left", padx=(4, 0))
        self.v["xloss_on"] = tk.BooleanVar(value=bool(self.s.get("xloss_on", True)))
        ttk.Checkbutton(xf, text="사용 (커플러 없으면 끄기)", variable=self.v["xloss_on"], style="Card.TCheckbutton",
                        command=self.show_xloss).pack(side="left", padx=(8, 0))
        self.lbl_xloss = ttk.Label(b, text="", style="Info.TLabel", wraplength=W - 40)
        self.lbl_xloss.grid(row=r + 3, column=0, columnspan=3, sticky="w")
        self.v["freq_ghz"].trace_add("write", lambda *a: self.apply_bench_loss())

        # 3 Bench 기록 → Pout
        c = self.card(inner, "PA Bench 기록 → Pout", 3, "읽기만 함")
        c.pack(fill="x", padx=(2, 6), pady=(0, 6))
        b = c.body
        ttk.Label(b, text="샘플 종류", style="Lab.TLabel").grid(row=0, column=0, sticky="w", pady=2, padx=(0, 8))
        self.v["b_stype"] = tk.StringVar(value=self.s.get("b_stype", ""))
        self.cb_stype = ttk.Combobox(b, textvariable=self.v["b_stype"], state="readonly", width=14)
        self.cb_stype.grid(row=0, column=1, sticky="w")
        self.cb_stype.bind("<<ComboboxSelected>>", lambda e: self.load_records())
        ttk.Button(b, text="새로 읽기", command=self.bench_refresh).grid(row=0, column=2, sticky="w", padx=(4, 0))
        ttk.Label(b, text="기록", style="Lab.TLabel").grid(row=1, column=0, sticky="w", pady=2)
        self.v["b_rec"] = tk.StringVar(value="")
        self.cb_rec = ttk.Combobox(b, textvariable=self.v["b_rec"], state="readonly", width=30)
        self.cb_rec.grid(row=1, column=1, columnspan=2, sticky="ew")
        self.cb_rec.bind("<<ComboboxSelected>>", lambda e: self.show_rec())
        rf_ = ttk.Frame(b, style="Card.TFrame")
        rf_.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(4, 2))
        self.lbl_rec = ttk.Label(rf_, text="", style="Hint.TLabel", wraplength=W - 40)
        self.lbl_rec.pack(anchor="w")
        self.f_recinfo = tk.Frame(rf_, highlightthickness=0, bd=0)          # 기록 내용 (이름 · 값 두 줄 칸)
        self.reg(self.f_recinfo, bg="head")
        self.f_recinfo.pack(fill="x")
        ttk.Label(b, text="목표 Pout", style="Lab.TLabel").grid(row=3, column=0, sticky="w", pady=(4, 2))
        tf_ = ttk.Frame(b, style="Card.TFrame")
        tf_.grid(row=3, column=1, columnspan=2, sticky="w", pady=(4, 2))
        self.v["b_target"] = tk.StringVar(value=str(self.s.get("b_target", "")))
        ttk.Entry(tf_, textvariable=self.v["b_target"], width=7, justify="right").pack(side="left")
        ttk.Label(tf_, text="dBm", style="Unit.TLabel").pack(side="left", padx=(4, 6))
        ttk.Button(tf_, text="→ SG 레벨 정하기", command=self.sg_from_target).pack(side="left")
        self.v["auto_cal"] = tk.BooleanVar(value=bool(self.s.get("auto_cal", True)))
        ttk.Checkbutton(b, text="DPD OFF 측정 때 Pout 보정을 이 기록에 맞춤", variable=self.v["auto_cal"],
                        style="Card.TCheckbutton").grid(row=4, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.v["pout_lock_on"] = tk.BooleanVar(value=bool(self.s.get("pout_lock_on", False)))
        ttk.Checkbutton(b, text="실행 때 목표 Pout 에 SG 를 재며 맞추고 그 SG 로 고정 → DPD", variable=self.v["pout_lock_on"],
                        style="Card.TCheckbutton", command=lambda: self.v["pout_strict"].set(True) if self.v["pout_lock_on"].get() else None
                        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(2, 0))
        if not self.s.get("_strict2"):                    # v0.2.3.80: 목표 Pout 에 딱 맞추기가 기본 (한 번만 켬 · 끄면 그대로)
            self.s["pout_strict"], self.s["_strict2"] = True, True
        self.v["pout_strict"] = tk.BooleanVar(value=bool(self.s.get("pout_strict", True)))
        self.v["pout_tol"] = tk.StringVar(value=str(self.s.get("pout_tol", "0.05")))
        sf_ = ttk.Frame(b, style="Card.TFrame")
        sf_.grid(row=6, column=0, columnspan=3, sticky="w")
        ttk.Checkbutton(sf_, text="  └ 반복마다 Pout 딱 맞춤 ±", variable=self.v["pout_strict"],
                        style="Card.TCheckbutton").pack(side="left")
        ttk.Entry(sf_, textvariable=self.v["pout_tol"], width=5, justify="right").pack(side="left")
        ttk.Label(sf_, text=" dB (벗어나면 SG 고쳐 다시 · 느림)", style="Card.TLabel").pack(side="left")
        self.v["bad_gain_db"] = tk.StringVar(value=str(self.s.get("bad_gain_db", "3")))
        bf_ = ttk.Frame(b, style="Card.TFrame")
        bf_.grid(row=7, column=0, columnspan=3, sticky="w", pady=(2, 0))
        ttk.Label(bf_, text="이상 측정: Gain 이", style="Card.TLabel").pack(side="left")
        ttk.Entry(bf_, textvariable=self.v["bad_gain_db"], width=4, justify="right").pack(side="left", padx=2)
        ttk.Label(bf_, text="dB 넘게 떨어지면 버리고 다시 → 또 그러면 멈춤 (0 = 끔)", style="Card.TLabel").pack(side="left")

        # 4 DPD
        c = self.card(inner, "DPD (모델 · 학습)", 4, "GMP · DVR · 신경망")
        c.pack(fill="x", padx=(2, 6), pady=(0, 6))
        b = c.body
        self.dpd_body = b
        m0 = self.s.get("model") or ("nn" if self.s.get("method") == "nn" else "gmp")
        self.v["model"] = tk.StringVar(value=m0)
        self.v["method"] = tk.StringVar(value=self.s.get("method") if self.s.get("method") in ("ila", "dla", "ilcm", "ilaec") else "ila")
        ttk.Label(b, text="모델", style="Lab.TLabel").grid(row=0, column=0, sticky="w")
        mf = ttk.Frame(b, style="Card.TFrame")
        mf.grid(row=0, column=1, columnspan=2, sticky="w", pady=(0, 2))
        self._model_disp = tk.StringVar()
        self.cb_model = ttk.Combobox(mf, textvariable=self._model_disp, state="readonly", width=15)
        self.cb_model.pack(side="left")
        self.cb_model.bind("<<ComboboxSelected>>", lambda e: self.v["model"].set(
            {n: k for k, n in self.MODELS}.get(self._model_disp.get(), "gmp")))
        self.btn_models = ttk.Button(mf, text="목록 편집…", command=self.edit_models)
        self.btn_models.pack(side="left", padx=(4, 0))
        self.lbl_model = ttk.Label(b, text="", style="Unit.TLabel")
        self.lbl_model.grid(row=1, column=1, columnspan=2, sticky="w")
        ttk.Label(b, text="학습", style="Lab.TLabel").grid(row=2, column=0, sticky="w")
        self.seg(b, self.v["method"], (("ila", "ILA"), ("dla", "DLA"), ("ilcm", "ILC→모델"), ("ilaec", "ILA+보정"))).grid(
            row=2, column=1, columnspan=2, sticky="w", pady=(2, 4))
        self.dpd_rows = {"_learn": 2}
        r = 3
        for key, name, dflt, unit in fields["dpd"]:
            self.field(b, r, key, name, dflt, unit)
            self.dpd_rows[key] = r
            r += 1
        # 보정 기준 (시간 영역 / 주파수 영역 집중 구간 / 주파수 영역 ACLR 채널 자동) - 예전 '집중 켜기' 대신
        self.v["focus_on"] = tk.BooleanVar(value=bool(self.s.get("focus_on", True)))      # 예전 설정 호환용
        wm0 = self.s.get("w_mode") or ("focus" if self.s.get("focus_on", True) else "time")
        self.v["w_mode"] = tk.StringVar(value=wm0 if wm0 in self.W_MODES else "focus")
        ttk.Label(b, text="보정 기준", style="Lab.TLabel").grid(row=r, column=0, sticky="w", pady=(4, 0))
        self.seg_w = self.seg(b, self.v["w_mode"], (("time", "시간"), ("focus", "주파수·집중"), ("aclr", "주파수·ACLR")))
        self.seg_w.grid(row=r, column=1, columnspan=2, sticky="w", pady=(4, 0))
        self.v["w_mode"].trace_add("write", lambda *a: (self.v["focus_on"].set(self.v["w_mode"].get() == "focus"),
                                                        self.draw() if self.results else None))
        if "focus_mhz" in self.v:                          # 집중 구간을 바꾸면 그래프에 바로 (측정 끝 · 중지 뒤 미리 보기)
            self.v["focus_mhz"].trace_add("write", lambda *a: self.redraw_soon())
        self.dpd_rows["_wmode"] = r
        r += 1
        self.v["odd_only"] = tk.BooleanVar(value=bool(self.s.get("odd_only", False)))
        ttk.Checkbutton(b, text="홀수 차수만 (K = 1, 3, 5, …)", variable=self.v["odd_only"],
                        style="Card.TCheckbutton").grid(row=r, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.v["auto_model"] = tk.BooleanVar(value=bool(self.s.get("auto_model", True)))
        self.v["auto_wide"] = tk.BooleanVar(value=bool(self.s.get("auto_wide", False)))
        af_ = ttk.Frame(b, style="Card.TFrame")
        af_.grid(row=r + 1, column=0, columnspan=3, sticky="w", pady=(2, 0))
        ttk.Checkbutton(af_, text="K · M · L 자동 (계산으로 후보 → 실측 ACLR 로 고름)", variable=self.v["auto_model"],
                        style="Card.TCheckbutton").pack(side="left")
        ttk.Checkbutton(af_, text="넓게", variable=self.v["auto_wide"], style="Card.TCheckbutton").pack(side="left", padx=(8, 0))
        Tip(af_.winfo_children()[-1], "K · M · L 자동 넓게\n끄면: K 5·7·9 × M 2·3·4·6 × L 0·1·2 (36 조합) → 실측 후보 3 개.\n"
                 "켜면: K 3·5·7·9·11 × M 1·2·3·4·6·8 × L 0·1·2·3 (100 조합 넘게, 계산 10 초 안팎) → 실측 후보 5 개.\n"
                 "계산은 측정 없이 DPD OFF I/Q 로만 · 실측이 2 번 늘어남.")
        self.dpd_rows["_odd"], self.dpd_rows["_auto"] = r, r + 1
        self.lbl_auto = ttk.Label(b, text="", style="Lab.TLabel")
        self.lbl_auto.grid(row=r + 2, column=0, columnspan=3, sticky="w")
        ttk.Label(b, text="Ctrl+Z = 이 카드 값 되돌리기", style="Unit.TLabel").grid(row=r + 3, column=0, columnspan=3, sticky="w")
        self.dpd_row_w = {k: list(b.grid_slaves(row=r_)) for k, r_ in self.dpd_rows.items()}   # 숨겼다 다시 보이려고 미리
        self.v["model"].trace_add("write", lambda *a: self.model_changed())
        self.v["method"].trace_add("write", lambda *a: self.method_compat())
        self.v["method"].trace_add("write", lambda *a: self.model_changed())      # JSH: 'ILA+보정' 일 때만 오차 보정 칸
        self.refresh_models()
        self.dpd_keys = [k for k, *_ in fields["dpd"]] + ["odd_only", "auto_model", "model", "method", "w_mode"]
        self.setup_undo(self.dpd_keys)
        self._cur_type = self.type_name()
        self.show_auto_kml()

        # 5 실행
        c = self.card(inner, "실행", 5)
        c.pack(fill="x", padx=(2, 6), pady=(0, 6))
        b = c.body
        b.columnconfigure(0, weight=1)
        b.columnconfigure(1, weight=1)
        self.v["run_mode"] = tk.StringVar(value=self.s.get("run_mode", "sweep"))
        ttk.Label(b, text="방식", style="Lab.TLabel").grid(row=0, column=0, sticky="w")
        self.seg(b, self.v["run_mode"], (("one", "한 레벨 (위 SG 레벨)"), ("sweep", "파워 스윕"))).grid(row=0, column=1, sticky="w")
        sw = ttk.Frame(b, style="Card.TFrame")
        sw.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.f_sweep = sw
        for i, (key, name, dflt, unit) in enumerate((("sw_start", "SG 시작", "-15", "dBm"), ("sw_stop", "SG 끝", "0", "dBm"),
                                                      ("sw_step", "간격", "1", "dB"),
                                                      ("sw_pout_stop", "Pout 멈춤", "", "dBm (빈칸 = 끝까지)"))):
            self.field(sw, i, key, name, dflt, unit, width=7)
        of = ttk.Frame(b, style="Card.TFrame")
        of.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        for i, (key, name, dflt, unit) in enumerate((("target_aclr", "목표 ACLR", "-50", "dBc (닿으면 반복 끝 · 빈칸 = 끝까지)"),
                                                      ("run_name", "기록 이름", "", "(빈칸 = 샘플 종류 · 시각)"))):
            self.field(of, i, key, name, dflt, unit, width=7 if key == "target_aclr" else 16)
        self.v["force_iters"] = tk.BooleanVar(value=bool(self.s.get("force_iters", False)))
        ttk.Checkbutton(of, text="반복 횟수 끝까지 (목표 ACLR 에 닿아도 멈추지 않음 · DPD · ILC)", variable=self.v["force_iters"],
                        style="Card.TCheckbutton").grid(row=2, column=0, columnspan=3, sticky="w", pady=(2, 0))
        self.v["run_mode"].trace_add("write", lambda *a: self.run_mode_changed())
        self.btn_off = ttk.Button(b, text="DPD OFF 측정만", command=lambda: self.start(False))
        self.btn_off.grid(row=3, column=0, sticky="ew", padx=(0, 4), pady=(8, 0))
        self.btn_go = ttk.Button(b, text="▶ DPD 실행", style="Pri.TButton", command=lambda: self.start(True))
        self.btn_go.grid(row=3, column=1, sticky="ew", pady=(8, 0))
        self.btn_ilc = ttk.Button(b, text="한계 ACLR 측정 (ILC · 모델 없이 직접 찾기)", command=lambda: self.start(True, ilc=True))
        self.btn_ilc.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self.btn_cmp = ttk.Button(b, text="방식 비교 실행… (같은 SG 레벨에서 여러 방식)", command=self.ask_compare)
        self.btn_cmp.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self.btn_stop5 = tk.Button(b, text="■ DPD 측정 중지 (Esc · 바이어스 그대로)", command=self.stop, bg="#b0bec5", fg="white",
                                   activebackground="#b71c1c", activeforeground="white", disabledforeground="#e0e0e0",
                                   relief="flat", bd=0, font=("맑은 고딕", 10, "bold"), pady=4, state="disabled", cursor="hand2")
        self.btn_stop5.grid(row=9, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        f7 = ttk.Frame(b, style="Card.TFrame")
        f7.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        f7.columnconfigure(0, weight=1)
        f7.columnconfigure(1, weight=1)
        self.btn_recheck = ttk.Button(f7, text="✔ 고른 (★) 신호로 다시 재기", command=self.recheck_final)
        self.btn_recheck.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.btn_savefinal = ttk.Button(f7, text="💾 고른 (★) 입력 .wv 저장", command=self.save_wv_dpd)
        self.btn_savefinal.grid(row=0, column=1, sticky="ew")
        self.btn_send = ttk.Button(b, text="▶ 고른 (★) 입력 신호 계속 내보내기 (SG RF 켜 둠)", command=self.send_final)
        self.btn_send.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        mf_ = ttk.Frame(b, style="Card.TFrame")
        mf_.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self.v["more_n"] = tk.StringVar(value=str(self.s.get("more_n", "5")))
        self.btn_more = ttk.Button(mf_, text="＋ 이어서 반복", command=lambda: self.start(True, more=max(1, int(fnum(self.v["more_n"].get(), 5)))))
        self.btn_more.pack(side="left", fill="x", expand=True)
        ttk.Entry(mf_, textvariable=self.v["more_n"], width=4, justify="right").pack(side="left", padx=(6, 3))
        ttk.Label(mf_, text="번 더 (끝난 · 중지한 DPD · ILC 결과에서)", style="Unit.TLabel").pack(side="left")
        f9_ = ttk.Frame(b, style="Card.TFrame")
        f9_.grid(row=9, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self.btn_ilc2m = ttk.Button(f9_, text="🧠 ILC 결과로 모델 만들기  (ILC 다시 안 돌림 → 4 카드 모델로 학습 → 반복)",
                                    command=lambda: self.start(True, from_ilc=True))
        self.btn_ilc2m.pack(side="top", fill="x")
        f9b_ = ttk.Frame(f9_, style="Card.TFrame")
        f9b_.pack(side="top", fill="x", pady=(4, 0))
        bl_ = ttk.Button(f9b_, text="📂 저장한 ILC 고르기 …", command=self.ilc_open)
        bl_.pack(side="left")
        Tip(bl_, "저장한 ILC 결과 불러오기\nILC 가 끝날 때마다 자동으로 저장됨 (샘플 · 주파수 · ACLR · 시각이 파일 이름에).\n"
                 "불러오면 ILC 를 다시 안 돌리고 바로 4 카드 모델 (GMP 등) 로 학습 → 측정.\n"
                 "신호 (1 카드) 는 ILC 때와 같아야 함 (다르면 알려 줌)")
        self.v["ilcm_widen"] = tk.BooleanVar(value=bool(self.s.get("ilcm_widen", True)))
        cw_ = ttk.Checkbutton(f9b_, text="학습 오차 크면 K·M·L 자동 넓힘 (GMP)", variable=self.v["ilcm_widen"], style="Card.TCheckbutton")
        cw_.pack(side="left", padx=(10, 0))
        self.v["ilcm_lin"] = tk.BooleanVar(value=bool(self.s.get("ilcm_lin", True)))
        cl_ = ttk.Checkbutton(f9b_, text="긴 선형 등화 ±64 탭 (넓은 대역)", variable=self.v["ilcm_lin"], style="Card.TCheckbutton")
        cl_.pack(side="left", padx=(10, 0))
        Tip(cl_, "ILC 결과로 모델 만들기: GMP + 긴 선형 등화\n400 MHz 처럼 넓은 대역은 PA · 케이블 · 필터의 이득 리플 · 지연이 대역 안에서 커서\n"
                 "GMP 의 짧은 메모리로는 ILC 가 고친 '채널 안' 을 못 따라감 → 앞뒤 ±64 탭 선형 항을 같이 학습.\n"
                 "로그 '선형 진단': 크기 · 위상만 vs 긴 선형 등화 → 차이가 크면 이게 원인. 좋아질 때만 바꿈 (GMP 만)")
        self.v["ilcm_trap"] = tk.BooleanVar(value=bool(self.s.get("ilcm_trap", True)))
        ct_ = ttk.Checkbutton(f9b_, text="긴 메모리 (트래핑)", variable=self.v["ilcm_trap"], style="Card.TCheckbutton")
        ct_.pack(side="left", padx=(10, 0))
        Tip(ct_, "ILC 결과로 모델 만들기: 긴 메모리 (GaN 트래핑 · 열) 항\n최근 0.1 · 1 · 10 µs 평균 전력에 따라 바뀌는 이득 · 위상을 모델에 더함\n"
                 "(GMP · 신경망은 수십 ns 만 기억 → ILC 가 고친 µs 단위 효과를 못 따라감).\n"
                 "로그 '긴 메모리 진단': 몇 dB 좋아지는지 → 3 dB 넘게 좋아지면 이게 원인. 좋아질 때만 바꿈 (GMP 만)")
        Tip(cw_, "ILC 결과로 모델 만들기: 학습 오차 자동 넓히기\n지금 K · M · L 로 배운 오차가 −40 dB 보다 크면 (= ILC 입력을 다 못 흉내 냄)\n"
                 "K 5~11 × M 1~8 × L 0~3 을 계산만으로 (측정 없이) 다시 찾아 더 좋은 조합으로 바꿈 (GMP 만).\n"
                 "끝나면 로그에 '판정': ILC 최고 vs 모델 최고 · 학습 오차 → 모델 한계인지 측정 한계인지")
        Tip(self.btn_ilc2m, "ILC 결과로 모델 만들기\n방금 끝낸 (또는 중지한) [한계 ACLR (ILC)] 의 가장 좋았던 입력을 정답으로\n"
                 "4 카드 모델 (GMP 등 · K · M · L) 이 '원래 신호 → 그 입력' 을 배움 → 측정 → 반복 횟수만큼 ILA 로 다듬음.\n"
                 "ILC 를 다시 돌리지 않아 빠름 · 같은 SG 레벨 · 목표 Pout 고정이 켜져 있으면 그대로.")
        self.v["rf_off_end"] = tk.BooleanVar(value=bool(self.s.get("rf_off_end", True)))
        ttk.Checkbutton(b, text="끝나면 SG RF OFF", variable=self.v["rf_off_end"], style="Card.TCheckbutton").grid(
            row=10, column=0, columnspan=2, sticky="w", pady=(6, 0))
        rf_ = ttk.Frame(b, style="Card.TFrame")             # 장비: RF · 연결 (한 버튼씩, 색 = 지금 상태)
        rf_.grid(row=11, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        rf_.columnconfigure(0, weight=1, uniform="st")
        rf_.columnconfigure(1, weight=1, uniform="st")
        self.btn_rf = self.state_btn(rf_, self.rf_toggle)
        self.btn_rf.grid(row=0, column=0, sticky="ew", padx=(0, 3))
        self.btn_conn = self.state_btn(rf_, self.conn_toggle)
        self.btn_conn.grid(row=0, column=1, sticky="ew")
        fs_ = ttk.Frame(b, style="Card.TFrame")             # FSW: 화면 그림 · 설정 파일
        fs_.grid(row=12, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        ttk.Label(fs_, text="FSW", style="Lab.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 6))
        for i_, (attr, txt, cmd) in enumerate((("btn_shot", "화면 저장…", self.fsw_shot), ("btn_stsave", "설정 저장…", self.fsw_state_save),
                                               ("btn_stload", "설정 불러오기…", self.fsw_state_load))):
            fs_.columnconfigure(i_ + 1, weight=1, uniform="fsw")
            b_ = ttk.Button(fs_, text=txt, command=cmd)
            b_.grid(row=0, column=i_ + 1, sticky="ew", padx=(0, 3) if i_ < 2 else 0)
            setattr(self, attr, b_)
        self.v["fsw_dfl_auto"] = tk.BooleanVar(value=bool(self.s.get("fsw_dfl_auto", True)))
        self.chk_dfl = ttk.Checkbutton(b, text="", variable=self.v["fsw_dfl_auto"], style="Card.TCheckbutton",
                                       command=lambda: (self.s.__setitem__("fsw_dfl_auto", bool(self.v["fsw_dfl_auto"].get())),
                                                        self.dfl_label()))
        self.chk_dfl.grid(row=13, column=0, columnspan=2, sticky="w", pady=(2, 0))
        self.v["fsw_guard"] = tk.BooleanVar(value=bool(self.s.get("fsw_guard", True)))
        cg_ = ttk.Checkbutton(b, text="측정 중 FSW 화면 놓치면 [시작] 때 FSW 설정으로 되돌리고 다시", variable=self.v["fsw_guard"],
                              style="Card.TCheckbutton")
        cg_.grid(row=14, column=0, columnspan=2, sticky="w", pady=(2, 0))
        Tip(cg_, "FSW 화면 놓침 자동 복구\n[시작] 할 때 지금 FSW 설정 (맞춰 둔 화면 · 채널 · 레벨) 을 FSW 안에 저장 →\n"
                 "측정이 실패하거나 (응답 없음 · 데이터 오류) Gain 이 갑자기 떨어지면 그 설정을 다시 불러오고 그 측정만 다시 잼.\n"
                 "한 실행에 최대 5 번. 저장은 FSW 안 C:\\R_S\\Instr\\user\\pa_dpd_guard.dfl")
        self.dfl_label()
        self.run_mode_changed()

    def build_right(self, right):
        T = THEME
        # 탭 (Bench 와 같은 모양)
        tb = ttk.Frame(right)
        tb.pack(fill="x")
        self.v_tab = tk.StringVar(value="aclr")
        tabs_ = [("aclr", "ACLR vs Pout"), ("graph", "스펙트럼 · AM-AM · AM-PM"), ("equip", "장비 (SMW · FSW)"), ("log", "로그")]
        if self.host is not None:                          # Bench 안: 장비는 Bench [장비 설정] 에서 연결 → 탭 빼기
            tabs_ = [t_ for t_ in tabs_ if t_[0] != "equip"]
        for key, txt in tabs_:
            ttk.Radiobutton(tb, text=txt, value=key, variable=self.v_tab, style="Tab.Toolbutton",
                            command=lambda: self.show_tab(self.v_tab.get())).pack(side="left")
        self.tabs = {}
        holder = ttk.Frame(right)
        holder.pack(fill="both", expand=True)
        # 그래프 (위: 스펙트럼 보기 고르기)
        gg = ttk.Frame(holder)
        gbar = ttk.Frame(gg)
        gbar.pack(fill="x", pady=(6, 0))
        ttk.Label(gbar, text="스펙트럼").pack(side="left", padx=(0, 6))
        self.v["spec_view"] = tk.StringVar(value=self.s.get("spec_view", "both"))
        for val, txt in (("aclr", "ACLR (Mod. 화면)"), ("iq", "I/Q"), ("both", "둘 다")):
            ttk.Radiobutton(gbar, text=txt, value=val, variable=self.v["spec_view"], style="Seg.Toolbutton",
                            command=lambda: self.draw() if self.results else None).pack(side="left")
        ttk.Label(gbar, text="   보정").pack(side="left", padx=(0, 6))
        self.seg(gbar, self.v["w_mode"], self.W_MODES_SHORT).pack(side="left")    # 4 카드 '보정 기준' 과 같은 값
        ttk.Label(gbar, text="  ACLR · 둘 다 = 반복마다 FSW Mod. 화면도 · ★ = 가장 좋은 것",
                  foreground=T["unit"]).pack(side="left")
        ttk.Button(gbar, text="⧉ 새 창 (크게)",
                   command=lambda: self.open_graph_window(self.s.get("gwin_lay", "four") if self.s.get("gwin_lay") != "hist" else "four")
                   ).pack(side="right")
        g = ttk.Frame(gg)
        g.pack(fill="both", expand=True)
        g.columnconfigure(0, weight=3)
        g.columnconfigure(1, weight=2)
        g.rowconfigure(0, weight=1)
        g.rowconfigure(1, weight=1)
        self.p_spec = Plot(g, "스펙트럼 (채널 최고 = 0 dB)", "주파수 오프셋 MHz", "dB")
        self.p_spec.remember(self.s, "yr_spec")
        self.p_spec.enable_marks(self.s, "spec_marks", "MHz")
        self.p_spec.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=(6, 6))
        self.p_trend = Plot(g, "측정 순서별 ACLR (빨강 L · 주황 U)", "측정 순서", "dBc")
        self.p_trend.remember(self.s, "yr_trend")
        self.p_trend.grid(row=0, column=1, sticky="nsew", pady=(6, 6))
        self.p_amam = Plot(g, "AM-AM (원래 신호 → PA 출력, 작은 신호 이득 = 0)", "입력 dB (최고값 기준)", "dB")
        self.p_amam.remember(self.s, "yr_amam")
        self.p_amam.grid(row=1, column=0, sticky="nsew", padx=(0, 6))
        self.p_ampm = Plot(g, "AM-PM", "입력 dB (최고값 기준)", "°")
        self.p_ampm.remember(self.s, "yr_ampm")
        self.p_ampm.grid(row=1, column=1, sticky="nsew")
        self.tabs["graph"] = gg
        # ACLR vs Pout (PA Analyzer 처럼: 과거 기록과 겹쳐 보기)
        a = ttk.Frame(holder)
        a.columnconfigure(0, weight=1)
        a.rowconfigure(1, weight=1)
        bar = ttk.Frame(a)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(6, 4))
        self.v["y_q"] = tk.StringVar(value=self.s.get("y_q", "aclr"))
        ttk.Label(bar, text="세로축").pack(side="left", padx=(0, 6))
        for val, txt in (("aclr", "ACLR (나쁜 쪽)"), ("aclr_l", "ACLR L"), ("aclr_u", "ACLR U"), ("evm", "EVM"),
                         ("de", "DE"), ("pae", "PAE")):
            ttk.Radiobutton(bar, text=txt, value=val, variable=self.v["y_q"], style="Seg.Toolbutton",
                            command=self.draw_hist).pack(side="left")
        ttk.Button(bar, text="⧉ 새 창 (크게)", command=lambda: self.open_graph_window("hist")).pack(side="right")
        ttk.Label(bar, text="   점선 = DPD OFF · 실선 = DPD ON · 색 = 주파수 (같은 주파수는 같은 색)", foreground=T["unit"]).pack(side="left")
        self.p_hist = Plot(a, "ACLR vs Pout", "DUT Pout (dBm)", "dBc")
        self.p_hist.remember(self.s, "yr_hist_" + self.s.get("y_q", "aclr"))
        self.p_hist.grid(row=1, column=0, sticky="nsew", padx=(0, 6))
        side = self.card(a, "기록", None, "")
        side.grid(row=1, column=1, sticky="ns")
        self.hist_card = side
        hb = side.body
        self.htree = ttk.Treeview(hb, columns=("on", "name", "ghz", "n", "when"), show="headings", height=8, selectmode="extended")
        for c_, h, w_ in (("on", "표시", 36), ("name", "이름", 130), ("ghz", "GHz", 50), ("n", "점", 34), ("when", "날짜", 92)):
            self.htree.heading(c_, text=h)
            self.htree.column(c_, width=w_, anchor="center" if c_ != "name" else "w", stretch=c_ == "name")
        self.htree.pack(fill="both", expand=True)
        hb.pack_propagate(True)
        self.htree.bind("<Button-1>", self.hist_click)
        self.htree.bind("<Double-1>", lambda e: self.hist_rename())
        hb2 = ttk.Frame(hb, style="Card.TFrame")
        hb2.pack(fill="x", pady=(6, 0))
        for txt, cmd in (("전체 표시", lambda: self.hist_show(True)), ("숨김", lambda: self.hist_show(False)),
                         ("이름", self.hist_rename), ("삭제", self.hist_delete)):
            ttk.Button(hb2, text=txt, command=cmd).pack(side="left", padx=(0, 3))
        ttk.Button(hb, text="PA Analyzer 용 엑셀 내보내기…", command=self.hist_export).pack(fill="x", pady=(6, 0))
        ttk.Label(hb, text="표시 칸 클릭 = 켜기/끄기 · 이름 더블클릭 = 바꾸기", style="Hint.TLabel").pack(anchor="w", pady=(4, 0))
        self.tabs["aclr"] = a
        self.root.after(1000, self.live_dc)
        self.root.after(1500, self.conn_refresh)
        # 장비
        e = ttk.Frame(holder)
        c = self.card(e, "장비 연결", None, "LAN · SCPI 5025 · demo = 가상 PA")
        c.pack(fill="x", padx=0, pady=6)
        r = 0
        for key, name, dflt, unit, card in self.FIELDS:
            if card == "dev":
                self.field(c.body, r, key, name, dflt, unit, width=20)
                r += 1
        ttk.Label(c.body, text="SMW 는 /var/user/pa_dpd_tx_0.wv · _1.wv 에 번갈아 저장, FSW 는 'DPD IQ' I/Q Analyzer 채널을 만들어 씀.\n"
                               "10 MHz 기준 클럭을 연결하면 가장 안정적입니다.", style="Hint.TLabel").grid(row=r, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.lbl_idn = ttk.Label(c.body, text="", style="Info.TLabel")
        self.lbl_idn.grid(row=r + 1, column=0, columnspan=3, sticky="w", pady=(4, 0))
        for k in ("smw_host", "fsw_host"):
            self.v[k].trace_add("write", lambda *a: self.update_chips())
        self.tabs["equip"] = e
        # 로그
        lg = ttk.Frame(holder)
        lbar = ttk.Frame(lg)
        lbar.pack(fill="x", pady=(6, 0))
        self.v["log_follow"] = tk.BooleanVar(value=True)
        cf_ = ttk.Checkbutton(lbar, text="실시간 따라가기 (새 로그가 오면 맨 아래로)", variable=self.v["log_follow"],
                              command=lambda: self.v["log_follow"].get() and self.txt.see("end"))
        cf_.pack(side="left")
        Tip(cf_, "끄면 새 로그가 와도 지금 보는 곳에 그대로 (스크롤해서 읽기 편하게).\n"
                 "켜 있어도 위로 스크롤해 읽는 중이면 따라가지 않고, 맨 아래로 내려오면 다시 따라감.")
        def _bottom():
            self.v["log_follow"].set(True)
            self.txt.see("end")
            self._log_new = 0
            self.lbl_lognew.configure(text="")
        ttk.Button(lbar, text="⏬ 맨 아래 (최신)", command=_bottom).pack(side="left", padx=(8, 0))
        self.lbl_lognew = ttk.Label(lbar, text="", style="Info.TLabel")
        self.lbl_lognew.pack(side="left", padx=(8, 0))
        lw_ = ttk.Frame(lg)
        lw_.pack(fill="both", expand=True, pady=6)
        self.txt = tk.Text(lw_, font=("Consolas", 9), relief="flat", wrap="none", bd=0)
        self.reg(self.txt, bg="log_bg", fg="log_fg", insertbackground="log_fg")
        sb_ = ttk.Scrollbar(lw_, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb_.set)
        sb_.pack(side="right", fill="y")
        self.txt.pack(side="left", fill="both", expand=True)
        self._log_new = 0
        self.tabs["log"] = lg
        self.show_tab("aclr")
        # 아래: 측정 기록 (Bench 의 측정 기록과 같은 자리)
        rec = self.card(right, "측정 기록", None, "")
        rec.pack(side="bottom", fill="x", pady=(6, 0), before=holder)
        self.rec_card = rec
        top = ttk.Frame(rec.body, style="Card.TFrame")
        top.pack(fill="x", pady=(0, 4))
        self.lbl_last = ttk.Label(top, text="", style="Info.TLabel")
        self.lbl_last.pack(side="left")
        # 가정 Vdd: DE · PAE 를 이 Vdd 로 다시 계산해 봄 (전원은 안 바뀜 · 끄면 원래 값) - Bench 의 '가정 Vdd' 와 같음
        wf = ttk.Frame(top, style="Card.TFrame")
        wf.pack(side="left", padx=(14, 0))
        self.var_wi_on = tk.BooleanVar(value=False)
        self.var_wi_vdd = tk.StringVar(value="")
        ttk.Checkbutton(wf, text="가정 Vdd", variable=self.var_wi_on, command=self.wi_recalc).pack(side="left")
        we = ttk.Entry(wf, textvariable=self.var_wi_vdd, width=6, justify="right")
        we.pack(side="left", padx=(2, 2))
        we.bind("<Return>", lambda e: (self.var_wi_on.set(True), self.wi_recalc()))
        ttk.Label(wf, text="V", style="Hint.TLabel").pack(side="left")
        self.lbl_wi = ttk.Label(wf, text="", foreground="#ffb300")
        self.lbl_wi.pack(side="left", padx=(6, 0))
        for txt, cmd in (("계수 CSV…", self.save_coef), ("엑셀 저장…", self.save_xlsx), ("▶ 고른 입력 내보내기", self.send_final), ("▶ 저장한 .wv 내보내기…", self.send_wv_file),
                         ("고른 출력 I/Q 저장…", self.save_out), ("고른 입력 .wv 저장…", self.save_wv_dpd)):
            ttk.Button(top, text=txt, command=cmd).pack(side="right", padx=(4, 0))
        cols = ("label", "sg", "pin", "pout", "rec", "gain", "aclr_l", "aclr_u", "amod", "evm", "id", "de", "pae", "papr", "nmse", "t")
        heads = ("단계", "SG(dBm)", "Pin(dBm)", "Pout(dBm)", "기록 Pout", "Gain(dB)", "I/Q ACLR L", "I/Q ACLR U", "FSW ACLR L / U",
                 "EVM(%)", "Id(mA)", "DE(%)", "PAE(%)", "PAPR(dB)", "학습 NMSE", "시각")
        self.tree = ttk.Treeview(rec.body, columns=cols, show="headings", height=5, selectmode="browse")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.on_pick())
        for c_, h in zip(cols, heads):
            self.tree.heading(c_, text=h)
            self.tree.column(c_, width={"label": 80, "amod": 120}.get(c_, 72), anchor="center")
        self.tree_cols = cols
        self.fsw_col_show(False)
        self.tree.pack(fill="x")
        # 한 줄 로그 (로그 탭이 아닐 때도 마지막 줄은 보이게)
        self.lbl_status = ttk.Label(right, text="", foreground=T["dim"])
        self.lbl_status.pack(side="bottom", fill="x", pady=(4, 0), before=rec)

    # ------------------------------------------------------------ 설정 저장 · 불러오기
    PRESET_DIR = os.path.join(SET_DIR, "presets")
    YR_KEYS = ("yr_spec", "yr_trend", "yr_amam", "yr_ampm")

    def cfg_menu(self):
        m = tk.Menu(self.root, tearoff=0, font=("맑은 고딕", 10))
        m.add_command(label="지금 설정 저장 (다음 실행 때 그대로)", command=lambda: (self.save_settings(), self.log("설정 저장됨")))
        m.add_separator()
        m.add_command(label="torch 폴더 지정… (torch 신경망 · 선택)", command=self.pick_torch)
        m.add_separator()
        m.add_command(label="설정을 파일로 저장…", command=self.cfg_save_as)
        m.add_command(label="설정 파일 불러오기…", command=self.cfg_load)
        m.add_separator()
        m.add_command(label="기본값으로 되돌리기", command=self.cfg_default)
        b = self.btn_cfg
        m.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())

    def cfg_values(self):
        d = {k: var.get() for k, var in self.v.items()}
        for k in self.YR_KEYS + tuple(k for k in self.s if k.startswith("yr_hist_")):
            if k in self.s:
                d[k] = self.s[k]
        return d

    def cfg_apply(self, d):
        for k, val in d.items():
            if k in self.v:
                try:
                    self.v[k].set(val)
                except tk.TclError:
                    pass
        for k in self.YR_KEYS:
            if k in d:
                self.s[k] = d[k]
            else:
                self.s.pop(k, None)
        for pl, k in ((self.p_spec, "yr_spec"), (self.p_trend, "yr_trend"), (self.p_amam, "yr_amam"), (self.p_ampm, "yr_ampm")):
            pl.remember(self.s, k)
            pl.redraw()
        for k in [k for k in self.s if k.startswith("yr_hist_")]:
            self.s.pop(k)
        self.s.update({k: v for k, v in d.items() if k.startswith("yr_hist_")})
        try:
            self.run_mode_changed()
        except Exception:
            pass
        self.draw_hist()

    def cfg_save_as(self):
        os.makedirs(self.PRESET_DIR, exist_ok=True)
        path = filedialog.asksaveasfilename(parent=self.root, title="설정 저장", initialdir=self.PRESET_DIR,
                                            defaultextension=".json", filetypes=[("DPD 설정", "*.json")],
                                            initialfile="DPD설정_%s.json" % time.strftime("%m%d_%H%M"))
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"app": "PA DPD Tester " + VERSION, "settings": self.cfg_values()}, f, ensure_ascii=False, indent=1)
            self.log("설정 파일 저장: %s" % path)
        except Exception as e:
            messagebox.showerror(APP, "저장하지 못했습니다: %s" % e)

    def cfg_load(self):
        os.makedirs(self.PRESET_DIR, exist_ok=True)
        path = filedialog.askopenfilename(parent=self.root, title="설정 불러오기", initialdir=self.PRESET_DIR,
                                          filetypes=[("DPD 설정", "*.json")])
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            self.cfg_apply(d.get("settings", d))
            self.save_settings()
            self.log("설정 불러옴: %s" % path)
        except Exception as e:
            messagebox.showerror(APP, "불러오지 못했습니다: %s" % e)

    def cfg_default(self):
        if not messagebox.askyesno(APP, "입력 칸 · 그래프 세로축을 기본값으로 되돌릴까요?\n(장비 주소 · 기록 · 샘플별 K·M·L 은 그대로)"):
            return
        d = {key: dflt for key, name, dflt, unit, card in self.FIELDS
             if key not in ("smw_host", "fsw_host")}
        d.update(odd_only=False, auto_model=True, cfr_on=True, auto_cal=True)
        for k in [k for k in self.s if k.startswith("yr_")]:
            self.s.pop(k)
        self.cfg_apply(d)
        self.save_settings()
        self.log("기본값으로 되돌림")

    def setup_undo(self, keys):
        """4 DPD 카드 값 되돌리기 (Ctrl+Z): 바뀔 때마다 이전 값을 쌓음 (같은 칸을 연달아 치면 한 번으로)"""
        self.undo_stack, self._undo_prev, self._undo_busy, self._undo_t = [], {}, False, (None, 0.0)
        for k in keys:
            var = self.v[k]
            self._undo_prev[k] = var.get()

            def on_w(*_, k=k, var=var):
                new = var.get()
                old = self._undo_prev.get(k)
                self._undo_prev[k] = new
                if self._undo_busy or new == old:
                    return
                lk, lt = self._undo_t
                now = time.time()
                if not (lk == k and now - lt < 1.5 and self.undo_stack and self.undo_stack[-1][0] == k):
                    self.undo_stack.append((k, old))
                    del self.undo_stack[:-100]
                self._undo_t = (k, now)
            var.trace_add("write", on_w)
        if self.host is None:                            # Bench 안에서는 Bench 의 Ctrl+Z 가 DPD 화면일 때 이걸 부름
            self.root.bind_all("<Control-z>", self.undo)
            self.root.bind_all("<Control-Z>", self.undo)

    def undo(self, e=None):
        if not getattr(self, "undo_stack", None):
            self.log("되돌릴 값이 없습니다")
            return "break"
        item = self.undo_stack.pop()
        grp = item if isinstance(item, list) else [item]
        self._undo_busy = True
        txt = []
        try:
            for k, old in grp:
                cur = self.v[k].get()
                self.v[k].set(old)
                self._undo_prev[k] = self.v[k].get()
                txt.append("%s %s → %s" % (k, cur, old))
        finally:
            self._undo_busy = False
        self._undo_t = (None, 0.0)
        self.log("되돌림: " + " · ".join(txt))
        return "break"

    def take_auto_kml(self):
        """자동으로 고른 K · M · L → 4 카드 칸에 넣고 샘플 종류별로 기억"""
        eng = self.engine
        ch = getattr(eng, "chosen", None) if eng is not None else None
        if not ch:
            return
        eng.chosen = None
        grp = [(k, self.v[k].get()) for k, v_ in zip(("K", "M", "L"), ch) if self.v[k].get() != str(v_)]
        self._undo_busy = True
        try:
            for k, v_ in zip(("K", "M", "L"), ch):
                self.v[k].set(str(v_))
                self._undo_prev[k] = str(v_)
        finally:
            self._undo_busy = False
        if grp:
            self.undo_stack.append(grp)                 # 자동으로 바뀐 K · M · L 은 Ctrl+Z 한 번에 되돌림
        self.store_type()
        self.show_auto_kml()
        self.save_settings()

    # ------------------------------------------------------------ 샘플 종류마다 4 DPD 카드 값 기억
    def type_name(self, st=None, bw=None):
        """기억 이름 = 샘플 종류 (+ ' · 400 MHz') → 샘플 종류 × 대역마다 1 ~ 5 카드 값을 따로 기억"""
        st = self.v["b_stype"].get() if st is None else st
        bw = (self.v["bw_mode"].get() if "bw_mode" in self.v else "100") if bw is None else bw
        return (st or "(샘플 종류 없음)") + ("" if bw == "100" else " · %s MHz" % bw)

    # 샘플과 상관없는 것 (장비 주소 등) · 신호 파일 위치와 대역은 따로 (대역마다 공통)
    TYPE_SKIP = ("fsw_dfl_auto", "fsw_guard", "smw_host", "fsw_host", "b_stype", "b_rec", "y_q", "wv_path", "bw_mode", "torch_dir", "spec_view",
                 "cal_mod", "cal_every", "pout_src")       # Pout 맞춤 방식은 대역 · 샘플 종류 상관없이 하나 (100 ↔ 400 바꿔도 그대로)

    def type_keys(self):
        """샘플 종류마다 따로 기억하는 칸: 1 신호 · 2 RF · 3 목표 · 4 DPD · 5 실행 (장비 주소 · 그래프 보기는 빼고)"""
        if not getattr(self, "dpd_keys", None):
            return []
        return [k for k in self.v if k not in self.TYPE_SKIP]

    def store_type(self, st=None, key=None):
        """지금 칸 값 → 기억 (key = 이미 만든 기억 이름 · 샘플 종류 × 대역)"""
        keys = self.type_keys()
        if not keys:
            return
        name = key if key is not None else (st if (st is not None and st == getattr(self, "_cur_type", None)) else self.type_name(st))
        self.s.setdefault("dpd_by_type", {})[name] = dict({k: self.v[k].get() for k in keys}, _strict2=True)

    def show_auto_kml(self):
        st = self.type_name()
        mem = self.s.get("dpd_by_type", {}).get(st)
        if hasattr(self, "lbl_auto"):
            self.lbl_auto.configure(text=("샘플 '%s' 설정 기억 중 · K%s M%s L%s · 반복 %s" % (
                st, mem.get("K"), mem.get("M"), mem.get("L"), mem.get("iters"))) if mem else
                "샘플 종류마다 설정 (1 ~ 5 카드) 을 따로 기억 - 바꾸면 그 샘플에서 쓰던 값으로")

    def apply_auto_kml(self):
        """샘플 종류를 바꾸면: 지금 값은 이전 종류에 저장 → 새 종류의 값을 칸에 (Ctrl+Z 한 번에 되돌림)"""
        new = self.type_name()
        old = getattr(self, "_cur_type", None)
        if old is not None and old == new:
            self.show_auto_kml()
            return                                         # 같은 종류 다시 읽기: 칸은 그대로
        if old is not None:
            self.store_type(key=old)
        self._cur_type = new
        mem = dict(self.s.get("dpd_by_type", {}).get(new) or {})
        if mem and "model" not in mem and "method" in mem:              # 예전 기억 (방식 = ILA / DLA / 신경망)
            mem["model"] = "nn" if mem["method"] == "nn" else "gmp"
            if mem["method"] == "nn":
                mem["method"] = "ila"
        if mem and not mem.get("_strict2"):                              # v0.2.3.80: 목표 Pout 딱 맞추기 기본 켬 (샘플 기억마다 한 번)
            mem["pout_strict"] = True
            mem["_strict2"] = True
            self.s.setdefault("dpd_by_type", {})[new] = dict(self.s["dpd_by_type"].get(new) or {}, pout_strict=True, _strict2=True)
        if not mem and new in self.s.get("auto_kml", {}):              # 예전 버전 (K · M · L 만) 기억
            mem = dict(zip(("K", "M", "L"), (str(v) for v in self.s["auto_kml"][new])))
        if mem:                                                          # Lagging · Leading 칸이 생기기 전 기억 → 빈칸 (L 과 같음)
            mem.setdefault("L_lag", "")
            mem.setdefault("L_lead", "")
        grp, changed = [], []
        if mem and getattr(self, "dpd_keys", None):
            self._undo_busy = True
            try:
                for k in self.type_keys():
                    if k in mem and str(self.v[k].get()) != str(mem[k]):
                        old_v = self.v[k].get()
                        try:
                            self.v[k].set(mem[k])
                        except tk.TclError:
                            continue
                        changed.append(k)
                        if k in self.dpd_keys:
                            grp.append((k, old_v))
                            self._undo_prev[k] = self.v[k].get()
            finally:
                self._undo_busy = False
            if grp:
                self.undo_stack.append(grp)
            if changed:
                for fn in ("run_mode_changed", "sig_src_changed"):
                    try:
                        getattr(self, fn)()
                    except Exception:
                        pass
                self.log("샘플 '%s' 에서 쓰던 설정으로 (%d 칸): %s" % (new, len(changed), " · ".join(
                    "%s %s" % (k, self.v[k].get()) for k in changed[:8]) + (" …" if len(changed) > 8 else "")))
        self.show_auto_kml()

    def get_bias(self):
        """지금 바이어스: Bench 안 = 파워서플라이에서 읽은 값 (Vdd · 게이트 · I_DRV · I_CA · Idq) ·
        단독 = Bench 설정의 샘플 종류 바이어스 기본값 (설정값) → dict 또는 None"""
        h = self.host
        b = None
        if h is not None and hasattr(h, "bias_now"):
            try:
                b = h.bias_now()
            except Exception:
                b = None
            if b:
                b = dict(b, src="파워서플라이 읽음")
        st = self.v["b_stype"].get().strip() if "b_stype" in self.v else ""
        pre = None
        try:
            if h is not None and hasattr(h, "stype_bias"):
                pre = h.stype_bias(st or None) if st else h.stype_bias()
            else:
                t = next((t for t in (self.bench or {}).get("sample_types", []) if t.get("name") == st), None)
                pre = (t or {}).get("bias")
        except Exception:
            pre = None
        if pre:
            pv = lambda k: fnum(pre.get(k), float("nan"))
            pset = dict(vdd=pv("vdd"), vg_drv=pv("g1"), vg_ca=pv("g2"), vg_pk=pv("g3"),
                        t_ca=pv("t_green"), t_drv=pv("t_yellow"))
            if b is None:
                b = dict(vdd=pset["vdd"], vg_drv=pset["vg_drv"], vg_ca=pset["vg_ca"], vg_pk=pset["vg_pk"], src="샘플 종류 기본값 (설정)")
            b["preset"] = pset
        return b

    @staticmethod
    def bias_rows(b):
        """엑셀 표시용 [(이름, 값, 단위)]"""
        if not b:
            return []
        out = []
        for nm, k, mul, nd, unit in (("Vdd", "vdd", 1, 2, "V"), ("VG_DRV", "vg_drv", 1, 3, "V"), ("VG_CA", "vg_ca", 1, 3, "V"),
                                     ("VG_PK", "vg_pk", 1, 3, "V"), ("Idq", "idq", 1e3, 1, "mA"), ("I_DRV", "i_drv", 1e3, 1, "mA"),
                                     ("I_CA", "i_ca", 1e3, 1, "mA"), ("I_leak", "i_leak", 1e3, 2, "mA")):
            v = b.get(k)
            if isinstance(v, (int, float)) and v == v:
                out.append((nm, round(v * mul, nd), unit))
        pre = b.get("preset") or {}
        for nm, k, unit in (("목표 CA", "t_ca", "mA"), ("목표 DRV", "t_drv", "mA")):
            v = pre.get(k)
            if isinstance(v, (int, float)) and v == v:
                out.append((nm, round(v, 1), unit))
        return out

    def read_dc(self):
        """Vdd 전원 (Vdd V, Id A, 측정 시각, 출처) - PA Bench Monitor 가 공유하는 값만 읽음 (전원에는 연결 안 함)"""
        h = self.host
        if h is not None:                              # 합친 프로그램: Bench 파워서플라이 값을 바로
            pp = getattr(h, "psu_panel", None)
            try:
                i, v, t = pp.e_last
                if pp.inst.get("e") is not None and i == i and v == v and time.time() - t < 5:
                    return (v, i, t, "파워서플라이")
            except Exception:
                pass
            return None
        d = BL.read_live()
        if d and d.get("vdd") is not None and d.get("id") is not None:
            return (float(d["vdd"]), float(d["id"]), float(d["t"]), "PA Bench")
        return None

    def live_dc(self):
        """측정하지 않을 때 DE 칸 아래에 지금 Vdd · Id 표시 (출처 포함) · 연결 표시"""
        try:
            dc = self.read_dc()
            ch = self.chips.get("psu")
            if ch is not None:
                ch.configure(fg="#39ff88" if dc else "#78909c", text="● Vdd 전원%s" % ((" (" + dc[3] + ")") if dc else ""))
            if not self.worker and not self.results:
                v_, sb_, col_ = self.big["eff"]
                sb_.configure(text=("지금 Id %.3f A · Vdd %.2f V (%s)" % (dc[1], dc[0], dc[3])) if dc else
                              "Vdd 전류 없음 - PA Bench Monitor 에서 파워서플라이 연결")
        except Exception:
            pass
        self.root.after(1000, self.live_dc)

    def show_tab(self, key):
        self.v_tab.set(key)
        for k, f in self.tabs.items():
            if k == key:
                f.pack(fill="both", expand=True)
            else:
                f.pack_forget()

    def update_chips(self):
        def chip(k, on, warn=False):
            c = self.chips.get(k)
            if c:
                c.configure(fg="#39ff88" if on else ("#ffd54f" if warn else "#78909c"))
        conn = self.engine is not None and getattr(self.engine, "smw", None) is not None
        demo = self.v.get("smw_host") and self.v["smw_host"].get().strip().lower() == "demo"
        chip("smw", conn, demo)
        chip("fsw", conn, demo)
        chip("bench", bool(self.recs))
        if conn:
            self.lbl_idn.configure(text="SMW: %s\nFSW: %s" % (self.engine.smw.idn, self.engine.fsw.idn))

    # ------------------------------------------------------------ 신호 파일
    def pick_wv(self):
        p = filedialog.askopenfilename(title=".wv 불러오기 (암호화 안 된 R&S 파형)", filetypes=[("R&S waveform", "*.wv")],
                                       initialdir=self.s.get("wv_dir_" + self.v["bw_mode"].get()) or self.s.get("wv_dir") or None)
        if not p:
            return
        try:
            x, fs = E.C.read_wv(p)
        except Exception as e:
            messagebox.showerror(APP, str(e))
            return
        self.s["wv_dir"] = os.path.dirname(p)
        self.s["wv_dir_" + self.v["bw_mode"].get()] = os.path.dirname(p)
        self.s["wv_" + self.v["bw_mode"].get()] = p                 # 100 / 400 MHz 마다 파일 기억
        self.v["wv_path"].set(p)
        self.v["sig_src"].set("file")
        self.log("불러옴: %s (%d 샘플 · %.2f MSps)" % (p, len(x), fs / 1e6))

    ACLR_DEF = {"100": ("98.28", "100"), "400": ("380.16", "400")}
    W_MODES = ("time", "focus", "aclr")
    W_MODES_SHORT = (("time", "시간 영역"), ("focus", "주파수 · 집중 구간"), ("aclr", "주파수 · ACLR 채널"))

    def bw_changed(self):
        """100 / 400 MHz DPD 바꾸기: 파일 · ACLR 채널 폭 · 간격을 대역마다 따로 기억해 바꿔 끼움"""
        new = self.v["bw_mode"].get()
        old = getattr(self, "_bw_cur", new)
        if new == old:
            return
        try:
            self.store_type(key=self.type_name(bw=old))  # 지금 값 (K · M · L 등) 은 이전 대역 이름으로 기억
        except Exception:
            pass
        self.s["wv_" + old] = self.v["wv_path"].get()
        self.s["aclr_" + old] = (self.v["ch_bw_mhz"].get(), self.v["adj_mhz"].get())
        self.s["src_" + old] = self.v["sig_src"].get()
        self._bw_cur = new
        ch, adj = self.s.get("aclr_" + new) or self.ACLR_DEF[new]
        self.v["ch_bw_mhz"].set(ch)
        self.v["adj_mhz"].set(adj)
        self.v["wv_path"].set(self.s.get("wv_" + new, ""))
        self.v["sig_src"].set("file" if new == "400" else self.s.get("src_100", self.v["sig_src"].get()))
        self.sig_src_changed()
        self.log("대역 %s MHz DPD - 신호 파일 · ACLR 채널 %s MHz · 간격 %s MHz" % (new, ch, adj))
        if self.host is not None and hasattr(self.host, "fsw_dpd_auto"):
            try:
                self.root.after(50, self.host.fsw_dpd_auto)  # Bench: 이 샘플 · 이 대역의 FSW DPD 화면으로
            except Exception:
                pass
        if getattr(self, "dpd_keys", None):
            fresh = self.type_name() not in self.s.get("dpd_by_type", {})
            self._cur_type = None                       # 이전 대역 값은 위에서 저장 → 이 대역에서 쓰던 값 불러오기
            self.apply_auto_kml()
            if fresh and "focus_mhz" in self.v:         # 이 대역 처음: 집중 구간은 대역마다 위치가 달라 비워 둠
                self.v["focus_mhz"].set("")

    def sig_src_changed(self):
        w400 = self.v["bw_mode"].get() == "400" if "bw_mode" in self.v else False
        if w400 and self.v["sig_src"].get() != "file":
            self.v["sig_src"].set("file")                  # 400 MHz 는 내 .wv 만
            return
        f = self.v["sig_src"].get() == "file"
        p = self.v["wv_path"].get()
        if hasattr(self, "lbl_src"):
            for w_ in (self.lbl_src, self.seg_src):
                w_.grid_remove() if w400 else w_.grid()
            if w400:
                self.chk_halve.pack_forget()               # 400 MHz 는 줄이기 없음
                self.v["file_halve"].set(False)
                if not self.chk_up.winfo_ismapped():
                    self.chk_up.pack(side="top", anchor="w", pady=(4, 0))
            else:
                self.chk_up.pack_forget()
                if not self.chk_halve.winfo_ismapped():
                    self.chk_halve.pack(side="top", anchor="w", pady=(4, 0))
            self.btn_wv.configure(text="파일 바꾸기…" if (p and os.path.isfile(p)) else "불러오기…")
            self.lbl_wv_dir.configure(text=("위치: " + os.path.dirname(p)) if p else "")
        if hasattr(self, "f_nr"):
            if f:
                self.f_nr.grid_remove()
                self.f_file.grid()
            else:
                self.f_file.grid_remove()
                self.f_nr.grid()
            for e_ in (self.e_chbw, self.e_adj):
                e_.configure(state="normal")             # NR 신호여도 ACLR 재는 위치 (채널 폭 · 인접 간격) 를 바꿀 수 있음
        txt = "(파일을 고르세요)"
        if p and os.path.isfile(p):
            txt = os.path.basename(p)
            try:
                raw = open(p, "rb").read(4096).decode("latin-1")
                i = raw.find("{CLOCK:")
                if i >= 0:
                    txt += " · %.2f MSps" % (float(raw[i + 7:raw.find("}", i)]) / 1e6)
            except Exception:
                pass
        self.lbl_wv.configure(text=txt)
        if self.engine is not None:
            self.engine.sig = None

    # ------------------------------------------------------------ PA Bench 연동
    def bench_refresh(self):
        self.bench = self.host.s if self.host is not None else BL.load_settings()
        if not self.bench:
            self.cb_stype.configure(values=[])
            self.lbl_rec.configure(text="PA Bench Monitor 설정을 찾지 못했습니다 (%s)" % BL.BENCH_SET)
            self.lbl_loss.configure(text="")
            return
        names = BL.sample_types(self.bench)
        self.cb_stype.configure(values=names)
        if self.v["b_stype"].get() not in names:
            self.v["b_stype"].set(self.bench.get("stype") if self.bench.get("stype") in names else (names[0] if names else ""))
        self.apply_bench_loss()
        self.load_records()

    # ------------------------------------------------------------ 추가 손실 (커플러 · 아이솔레이터, 주파수마다)
    @staticmethod
    def parse_loss_text(text):
        """Bench 케이블 손실 txt 와 같은 형식: 한 줄에 주파수(GHz) · In(dB) · Out(dB) (탭 · 공백 · 쉼표), 숫자 3개 줄만"""
        rows = []
        for line in text.splitlines():
            parts = line.replace(",", " ").replace(";", " ").split()
            vals = [fnum(x, float("nan")) for x in parts[:3]]
            if len(vals) == 3 and all(v_ == v_ for v_ in vals):
                rows.append(vals)
        return sorted(rows)

    @staticmethod
    def loss_text(rows):
        return "Freq(GHz)\tIn loss(dB)\tOut loss(dB)\n" + "".join("%.6g\t%.3f\t%.3f\n" % tuple(r) for r in rows)

    def xloss_at(self, g=None):
        """지금 주파수의 추가 손실 (In, Out, 쓴 행 GHz) - Bench 손실 표처럼 가장 가까운 주파수 행"""
        rows = self.s.get("xloss") or []
        g = fnum(self.v["freq_ghz"].get(), float("nan")) if g is None else g
        if not rows or g != g or ("xloss_on" in self.v and not self.v["xloss_on"].get()):
            return 0.0, 0.0, None
        r = min(rows, key=lambda r: abs(r[0] - g))
        return float(r[1]), float(r[2]), float(r[0])

    def show_xloss(self):
        if not hasattr(self, "lbl_xloss"):
            return
        rows = self.s.get("xloss") or []
        if not rows:
            self.lbl_xloss.configure(text="없음 (0 dB) - 지금 케이블 손실 그대로")
            return
        if "xloss_on" in self.v and not self.v["xloss_on"].get():
            self.lbl_xloss.configure(text="끔 (0 dB) - Bench Pulse · Mod. 측정과 같은 손실 그대로 (표는 남겨 둠)")
            return
        xi, xo, fr = self.xloss_at()
        li = fnum(self.v["in_loss_db"].get(), 0.0)
        lo = fnum(self.v["out_loss_db"].get(), 0.0)
        self.lbl_xloss.configure(text="%s · %.6g GHz 행: In +%.2f · Out +%.2f dB\n→ 측정 손실 In %.2f · Out %.2f dB"
                                 % (os.path.basename(self.s.get("xloss_file") or "") or "직접 입력", fr, xi, xo, li + xi, lo + xo))

    def clear_xloss(self):
        if self.s.get("xloss") and not messagebox.askyesno(APP, "추가 손실 표를 비울까요? (0 dB 로)"):
            return
        self.s["xloss"], self.s["xloss_file"] = [], ""
        self.show_xloss()
        self.save_settings()

    def edit_xloss(self):
        """추가 손실 표 편집 창: Bench 케이블 손실 txt 와 같은 형식 · txt 불러오기 / 저장"""
        top = tk.Toplevel(self.root)
        top.title("추가 손실 표 (커플러 · 아이솔레이터 등)")
        top.transient(self.root.winfo_toplevel())
        f = ttk.Frame(top, padding=12)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="케이블 손실에 더할 값 - 한 줄에 주파수(GHz) · In(dB) · Out(dB)", font=FB).pack(anchor="w")
        ttk.Label(f, text="Bench 케이블 손실 txt 와 같은 형식 (탭 · 공백 · 쉼표). 지금 주파수와 가장 가까운 행을 씀. 빈 표 = 0 dB",
                  foreground="#78909c").pack(anchor="w", pady=(0, 6))
        txt = tk.Text(f, width=46, height=14, font=("Consolas", 10), undo=True)
        txt.pack(fill="both", expand=True)
        rows = self.s.get("xloss") or []
        txt.insert("1.0", self.loss_text(rows) if rows else self.loss_text([[3.35, 0, 0], [3.6, 0, 0], [3.75, 0, 0]]))
        info = ttk.Label(f, text="", foreground="#78909c")
        info.pack(anchor="w", pady=(4, 6))
        src = {"file": self.s.get("xloss_file") or ""}

        def check(*a):
            n = len(self.parse_loss_text(txt.get("1.0", "end")))
            info.configure(text="읽히는 행 %d 개%s" % (n, (" · " + os.path.basename(src["file"])) if src["file"] else ""))
        txt.bind("<KeyRelease>", check)
        check()
        bf = ttk.Frame(f)
        bf.pack(fill="x")

        def load():
            path = filedialog.askopenfilename(parent=top, title="추가 손실 txt 불러오기",
                                              initialdir=os.path.dirname(src["file"]) if src["file"] else None,
                                              filetypes=[("텍스트 파일", "*.txt"), ("CSV", "*.csv"), ("모든 파일", "*.*")])
            if not path:
                return
            try:
                try:
                    with open(path, encoding="utf-8-sig") as fh:
                        t = fh.read()
                except UnicodeDecodeError:
                    with open(path, encoding="cp949", errors="replace") as fh:
                        t = fh.read()
            except OSError as e:
                messagebox.showerror(APP, "파일을 읽지 못했습니다.\n%s" % e, parent=top)
                return
            txt.delete("1.0", "end")
            txt.insert("1.0", t)
            src["file"] = path
            check()

        def save():
            rows_ = self.parse_loss_text(txt.get("1.0", "end"))
            path = filedialog.asksaveasfilename(parent=top, title="추가 손실 txt 저장", defaultextension=".txt",
                                                initialdir=os.path.dirname(src["file"]) if src["file"] else None,
                                                initialfile=os.path.basename(src["file"]) if src["file"] else "extra_loss.txt",
                                                filetypes=[("텍스트 파일", "*.txt")])
            if not path:
                return
            with open(path, "w", encoding="utf-8-sig", newline="\r\n") as fh:
                fh.write(self.loss_text(rows_))
            src["file"] = path
            check()

        def ok():
            rows_ = self.parse_loss_text(txt.get("1.0", "end"))
            if not rows_ and txt.get("1.0", "end").strip() and not messagebox.askyesno(
                    APP, "읽히는 행이 없습니다. 추가 손실을 0 dB 로 할까요?", parent=top):
                return
            self.s["xloss"], self.s["xloss_file"] = rows_, src["file"]
            self.show_xloss()
            self.save_settings()
            self.log("추가 손실 표: %d 개 주파수%s" % (len(rows_), (" (%s)" % os.path.basename(src["file"])) if src["file"] else ""))
            top.destroy()
        ttk.Button(bf, text="txt 불러오기…", command=load).pack(side="left")
        ttk.Button(bf, text="txt 저장…", command=save).pack(side="left", padx=4)
        ttk.Button(bf, text="취소", command=top.destroy).pack(side="right")
        ttk.Button(bf, text="적용", command=ok).pack(side="right", padx=4)
        top.bind("<Escape>", lambda e: top.destroy())
        self.center_popup(top)
        top.grab_set()
        txt.focus_set()

    def apply_bench_loss(self):
        self.root.after_idle(self.show_xloss)
        if not hasattr(self, "lbl_loss"):
            return
        on = bool(self.v["bench_loss"].get())
        if not on:
            self.lbl_loss.configure(text="손실은 위 칸 값 사용")
            return
        g_ = fnum(self.v["freq_ghz"].get(), float("nan"))
        r = None
        if self.host is not None and hasattr(self.host, "losses_at") and g_ == g_:
            try:                                           # Bench 안: Bench 가 Pulse · Mod. 계산에 쓰는 손실 그대로 (지금 값)
                li_, lo_ = self.host.losses_at(g_ * 1e9)
                ix_ = self.host.loss_index(g_ * 1e9)
                r = (float(li_), float(lo_), ("Bench 손실 표 %.6g GHz 행" % self.host.loss_table[ix_][0]) if ix_ is not None
                     else "Bench 고정값")
            except Exception:
                r = None
        if r is None:
            r = BL.losses_at(self.bench, g_) if self.bench else None
        if r is None:
            self.lbl_loss.configure(text="PA Bench Monitor 손실 표 없음 → 위 칸 값 사용")
            return
        li, lo, how = r
        self.v["in_loss_db"].set("%.2f" % li)
        self.v["out_loss_db"].set("%.2f" % lo)
        self.lbl_loss.configure(text="In %.2f dB · Out %.2f dB  (%s)" % (li, lo, how))

    REC_NONE = "— 선택 안 함 (기록 Pout · 기록 보정 안 씀)"

    def load_records(self):
        self.apply_auto_kml()
        self.recs = []
        st = self.v["b_stype"].get()
        pam = BL.latest_pam(self.bench, st) if (self.bench and st) else None
        if not pam:
            self.cb_rec.configure(values=[])
            self.v["b_rec"].set("")
            self.lbl_rec.configure(text="이 샘플 종류의 .pam 기록이 없습니다" if st else "")
            return
        try:
            recs = BL.read_records(pam)
        except Exception as e:
            self.lbl_rec.configure(text=".pam 읽기 실패: %s" % e)
            return
        g = fnum(self.v["freq_ghz"].get(), float("nan"))
        num = self.sample_num()
        self._rec_num = num
        mine = lambda r: bool(num) and num.lower() in [t.lower() for t in re.split(r"[\s_\-·,/()]+", "%s %s" % (r["name"], r["display"])) if t]
        # 지금 샘플 번호 (예: S2) 기록 먼저 → 지금 주파수에 가까운 것 → Mod. → 최근 순서
        recs.sort(key=lambda r: r["when"], reverse=True)
        recs.sort(key=lambda r: (0 if mine(r) else 1,
                                 abs(r["freq_ghz"] - g) if (g == g and r["freq_ghz"] == r["freq_ghz"]) else 99, 0 if r["mod"] else 1))
        self.recs = recs
        labels = ["%s%s · %.6g GHz%s" % ("★ " if mine(r) else "", r["display"], r["freq_ghz"], "" if r["mod"] else " (Pulse)") for r in recs]
        # 자동 선택: 지금 샘플 번호 (★) 의 Mod. 기록이 지금 주파수 (±50 MHz) 에 있을 때만 · 아니면 '선택 안 함'
        #  (다른 샘플 · 다른 조건 기록을 골라 두면 기록 Pout 이 지금 Pout 과 안 맞아 헷갈림)
        ok0 = bool(recs) and mine(recs[0]) and recs[0]["mod"] and g == g and abs(recs[0]["freq_ghz"] - g) <= 0.05
        self.cb_rec.configure(values=[self.REC_NONE] + labels)
        self.v["b_rec"].set(labels[0] if ok0 else self.REC_NONE)
        if recs and not ok0:
            self.log("3 카드 기록: 지금 샘플 · 주파수의 Mod. 기록이 없어 '선택 안 함' (필요하면 [기록] 에서 직접 고르기)")
        self.pam_path = pam
        self.show_rec()
        self.update_chips()

    def sample_num(self):
        """지금 샘플 번호 (Bench 에서 고른 S1 · S2 …) - 같은 번호 기록을 먼저 고름"""
        try:
            if self.host is not None:
                return (self.host.var_sample.get() or "").strip()
            return str((self.bench or {}).get("sample") or "").strip()
        except Exception:
            return ""

    def cur_rec(self):
        labels = [l_ for l_ in (self.cb_rec.cget("values") or []) if l_ != self.REC_NONE]
        cur = self.v["b_rec"].get()
        if cur in labels and labels.index(cur) < len(self.recs):
            return self.recs[labels.index(cur)]
        return None                                        # '선택 안 함' · 기록 없음

    def show_rec(self):
        """3 카드 기록 내용: 파일 · 기록 · SG · Pout · 손실 을 이름 / 값 칸으로"""
        f = self.f_recinfo
        for w in f.winfo_children():
            w.destroy()
        r = self.cur_rec()
        if r is None:
            f.pack_forget()
            if self.v["b_rec"].get() == self.REC_NONE:
                self.lbl_rec.configure(text="기록 안 씀 → 기록 Pout 칸은 '-' · Pout 은 FSW 측정 (2 카드 Pout 기준) 그대로")
            self.lbl_rec.pack(anchor="w")
            return
        self.lbl_rec.pack_forget()
        f.pack(fill="x")
        num = getattr(self, "_rec_num", "") or ""
        mine = bool(num) and self.v["b_rec"].get().startswith("★")
        rows = [("파일", os.path.basename(getattr(self, "pam_path", "")) or "-", None),
                ("기록", "%s%s" % (r["display"], ("  ★ 지금 샘플 %s" % num) if mine else ""), None),
                ("신호", "%.6g GHz · %s" % (r["freq_ghz"], "Mod. (변조)" if r["mod"] else "Pulse"),
                 None if r["mod"] else "#ef6c00"),
                ("SG", "%.1f ~ %.1f dBm  (%d 점)" % (min(r["sg"]), max(r["sg"]), len(r["sg"])), None),
                ("Pout", "%.2f ~ %.2f dBm" % (min(r["pout"]), max(r["pout"])), None)]
        if r.get("inl") == r.get("inl") and r.get("outl") == r.get("outl"):
            rows.append(("손실", "In %.2f · Out %.2f dB  (기록할 때)" % (r["inl"], r["outl"]), None))
        T = THEME
        for i, (k, v_, col) in enumerate(rows):
            tk.Label(f, text=k, font=("맑은 고딕", 9), fg=T["dim"], bg=T["head"], anchor="w", width=5).grid(
                row=i, column=0, sticky="w", padx=(8, 4), pady=(3 if i == 0 else 0, 3 if i == len(rows) - 1 else 0))
            tk.Label(f, text=v_, font=("맑은 고딕", 9, "bold"), fg=col or T["fg"], bg=T["head"], anchor="w",
                     wraplength=300, justify="left").grid(row=i, column=1, sticky="w", pady=(3 if i == 0 else 0, 3 if i == len(rows) - 1 else 0))
        if not r["mod"]:
            tk.Label(f, text="⚠ Pulse 기록 - DPD 는 Mod. 기록이 더 맞음", font=("맑은 고딕", 9), fg="#ef6c00", bg=T["head"],
                     anchor="w").grid(row=len(rows), column=0, columnspan=2, sticky="w", padx=8, pady=(0, 3))

    def sg_from_target(self):
        r = self.cur_rec()
        t = fnum(self.v["b_target"].get(), float("nan"))
        if r is None or t != t:
            messagebox.showinfo(APP, "기록을 고르고 목표 Pout 을 넣으세요.")
            return
        sg = BL.sg_for_pout(r, t)
        if sg != sg:
            return
        out = t > max(r["pout"]) or t < min(r["pout"])
        mx = fnum(self.v["sg_max_dbm"].get(), 0.0)
        if sg > mx:
            messagebox.showwarning(APP, "필요한 SG %.2f dBm 이 SG 최대 %.2f dBm 보다 큽니다." % (sg, mx))
            return
        self.v["sg_dbm"].set("%.2f" % sg)
        self.log("목표 Pout %.2f dBm → SG %.2f dBm (Bench 기록 보간%s)" % (t, sg, ", 기록 범위 밖 - 연장" if out else ""))

    # ------------------------------------------------------------ 모델 (GMP · 구간 GMP · DVR · 신경망) 선택
    def model_text(self, p, eng):
        """상태 · 기록 이름에 쓸 '방식 · 크기' 글"""
        if eng is not None and self.worker is not None:
            p = eng.p
        q = dict(p)
        try:
            E.migrate_method(q)
            meth = E.method_label(q)
        except Exception:
            meth = str(q.get("method", ""))
        mdl = getattr(eng, "model", None) if eng is not None else None
        m = q.get("model", "gmp")
        if mdl is not None and hasattr(mdl, "H"):
            return "%s · M%d H%d" % (meth, mdl.M, mdl.H)
        if mdl is not None and hasattr(mdl, "nseg"):
            return "%s · 구간%d M%d" % (meth, mdl.nseg, mdl.M)
        if mdl is not None:
            return "%s · K%d M%d %s" % (meth, mdl.K, mdl.M, mdl.lag_text() if hasattr(mdl, "lag_text") else "L%d" % mdl.L)
        if m in ("nn", "pnn", "tnn"):
            return "%s · M%s H%s" % (meth, q.get("M"), q.get("nn_hidden"))
        if m == "dvr":
            return "%s · 구간%s M%s" % (meth, q.get("dvr_seg"), q.get("M"))
        return "%s · K%s M%s L%s%s" % (meth, q.get("K"), q.get("M"), q.get("L"),
                                       " (자동)" if q.get("auto_model") and m == "gmp" else "")

    def pick_torch(self):
        """torch 폴더 (pip --target 으로 받은 곳) 를 정하고 바로 읽어 봄 → 되면 모델 목록에 torch 신경망 넣기"""
        d = filedialog.askdirectory(parent=self.root, title="torch 폴더 (pip install --target 으로 받은 곳)",
                                    initialdir=self.s.get("torch_dir") or os.path.expanduser("~"))
        if not d:
            return
        import dpd_torch
        if dpd_torch.available() and self.s.get("torch_dir") != d:
            messagebox.showinfo(APP, "torch 는 이미 읽혀 있습니다. 새 폴더는 프로그램을 다시 켜면 적용됩니다.")
            self.s["torch_dir"] = d
            self.save_settings()
            return
        self.root.configure(cursor="watch")
        self.root.update()
        try:
            ok, msg = dpd_torch.load(d)
        finally:
            self.root.configure(cursor="")
        if ok:
            self.s["torch_dir"] = d
            on = self.models_on()
            if "tnn" not in on:
                self.s["models_on"] = on + ["tnn"]
            self.refresh_models()
            self.save_settings()
            self.log("torch 읽음: " + msg)
            messagebox.showinfo(APP, "torch 를 읽었습니다.\n%s\n\n4 카드 모델에 'torch 신경망' 이 생겼습니다." % msg)
        else:
            self.log("torch 못 읽음: " + msg)
            messagebox.showwarning(APP, "torch 를 못 읽었습니다.\n%s\n\n이 프로그램은 %s 입니다. torch 폴더도 같은 버전 파이썬으로 받아야 합니다:\n"
                                   "python -m pip install --target <폴더> torch --index-url https://download.pytorch.org/whl/cpu"
                                   % (msg, dpd_torch.py_tag()))

    def models_on(self):
        on = [k for k in self.s.get("models_on", [k for k, _ in self.MODELS if k != "tnn"]) if k in dict(self.MODELS)]
        return on or ["gmp"]

    def refresh_models(self):
        on = self.models_on()
        names = dict(self.MODELS)
        self.cb_model.configure(values=[names[k] for k in on])
        if self.v["model"].get() not in on:
            self.v["model"].set(on[0])
        self.model_changed()

    def method_compat(self):
        if self.v["method"].get() == "nn":           # 예전 설정 · 기억 (방식 = 신경망)
            self.v["method"].set("ila")
            self.v["model"].set("nn")

    def model_changed(self):
        m = self.v["model"].get()
        if m not in dict(self.MODELS):
            self.v["model"].set("gmp")
            return
        self._model_disp.set(dict(self.MODELS)[m])
        tip = self.MODEL_TIP.get(m, "")
        if m == "tnn":
            tip = "PyTorch 2층 신경망 · " + (("torch 폴더: %s" % self.s["torch_dir"]) if self.s.get("torch_dir")
                                              else "torch 폴더 안 정함") + " · 못 읽으면 numpy 신경망으로 대신"
        self.lbl_model.configure(text=tip)
        meth_ = self.v["method"].get() if "method" in self.v else "ila"
        for key in list(self.MODEL_ROWS) + [k_ for k_ in self.METHOD_ROWS if k_ not in self.MODEL_ROWS]:
            models = self.MODEL_ROWS.get(key)
            show = (models is None or m in models) and (key not in self.METHOD_ROWS or meth_ in self.METHOD_ROWS[key])
            for w in self.dpd_row_w.get(key, ()):
                if show:
                    w.grid()
                else:
                    w.grid_remove()

    def center_popup(self, top):
        top.update_idletasks()
        rt = self.root.winfo_toplevel()
        x = rt.winfo_rootx() + (rt.winfo_width() - top.winfo_width()) // 2
        y = rt.winfo_rooty() + (rt.winfo_height() - top.winfo_height()) // 2
        top.geometry("+%d+%d" % (max(x, 0), max(y, 0)))

    def edit_models(self):
        """4 카드 모델 목록에 넣고 뺄 것 고르기 (써 본 뒤 필요 없는 건 빼 두기)"""
        top = tk.Toplevel(self.root)
        top.title("DPD 모델 목록")
        top.transient(self.root.winfo_toplevel())
        top.resizable(False, False)
        f = ttk.Frame(top, padding=14)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="4 카드 · 방식 비교에 보일 모델", font=FB).pack(anchor="w", pady=(0, 6))
        on = self.models_on()
        vs = {}
        for k, n in self.MODELS:
            vs[k] = tk.BooleanVar(value=k in on)
            ttk.Checkbutton(f, text="%s  -  %s" % (n, self.MODEL_TIP[k]), variable=vs[k]).pack(anchor="w", pady=1)
        ttk.Label(f, text="빼도 지운 게 아니라 숨김 - 언제든 다시 넣을 수 있음", foreground="#78909c").pack(anchor="w", pady=(6, 4))
        tf = ttk.Frame(f)
        tf.pack(fill="x", pady=(0, 8))
        ttk.Label(tf, text="torch 폴더: %s" % (self.s.get("torch_dir") or "안 정함 (torch 신경망 쓸 때만)"),
                  foreground="#78909c").pack(side="left")
        ttk.Button(tf, text="torch 폴더…", command=lambda: (top.destroy(), self.pick_torch())).pack(side="right")
        bf = ttk.Frame(f)
        bf.pack(anchor="e")

        def ok():
            sel = [k for k, _ in self.MODELS if vs[k].get()]
            if not sel:
                messagebox.showwarning(APP, "모델을 하나 이상 고르세요", parent=top)
                return
            self.s["models_on"] = sel
            self.refresh_models()
            self.save_settings()
            top.destroy()
        ttk.Button(bf, text="적용", command=ok).pack(side="left", padx=3)
        ttk.Button(bf, text="취소", command=top.destroy).pack(side="left", padx=3)
        top.bind("<Escape>", lambda e: top.destroy())
        self.center_popup(top)
        top.grab_set()

    def cmp_items(self):
        """방식 비교 후보: 켜 둔 모델 × 학습 방식"""
        out = []
        names = dict(self.MODELS)
        for k in self.models_on():
            if k in ("nn", "pnn", "tnn"):
                out.append(("%s:ila" % k, names[k]))
            else:
                out.append(("%s:ila" % k, "%s-ILA" % names[k]))
                out.append(("%s:dla" % k, "%s-DLA" % names[k]))
                out.append(("%s:ilaec" % k, "%s-ILA+보정" % names[k]))
            out.append(("%s:ilcm" % k, "%s-ILC→모델" % names[k]))
        return out

    def ask_compare(self):
        if E is None or self.worker:
            return
        top = tk.Toplevel(self.root)
        top.title("방식 비교 실행")
        top.transient(self.root.winfo_toplevel())
        top.resizable(False, False)
        f = ttk.Frame(top, padding=14)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="같은 SG 레벨 · 같은 DPD OFF 측정에서 방식마다 DPD", font=FB).pack(anchor="w")
        ttk.Label(f, text="SG %s dBm · 반복 %s 번씩 · 결과는 방식마다 따로 기록 + 요약 표" % (
            self.v["sg_dbm"].get(), self.v["iters"].get()), foreground="#78909c").pack(anchor="w", pady=(0, 8))
        sel0 = self.s.get("cmp_sel", self.CMP_DEF)
        vs = {}
        g = ttk.Frame(f)
        g.pack(anchor="w")
        for i, (k, n) in enumerate(self.cmp_items()):
            vs[k] = tk.BooleanVar(value=k in sel0)
            ttk.Checkbutton(g, text=n, variable=vs[k]).grid(row=i // 2, column=i % 2, sticky="w", padx=(0, 18), pady=1)
        v_ilc = tk.BooleanVar(value=bool(self.s.get("cmp_ilc", False)))
        ttk.Checkbutton(f, text="ILC 한계도 같이 (기준선 · +%s 번 측정)" % self.v["ilc_iters"].get(), variable=v_ilc).pack(anchor="w", pady=(8, 0))
        lbl = ttk.Label(f, text="", foreground="#78909c")
        lbl.pack(anchor="w", pady=(6, 8))

        def est(*a):
            n = sum(1 for v in vs.values() if v.get())
            it = int(fnum(self.v["iters"].get(), 4))
            m = 1 + n * it + (int(fnum(self.v["ilc_iters"].get(), 10)) if v_ilc.get() else 0)
            lbl.configure(text="측정 약 %d 번 · 대략 %d ~ %d 분" % (m, max(1, m * 4 // 60), max(1, m * 7 // 60 + 1)))
        for v in list(vs.values()) + [v_ilc]:
            v.trace_add("write", est)
        est()
        bf = ttk.Frame(f)
        bf.pack(anchor="e")

        def ok():
            sel = [k for k, _ in self.cmp_items() if vs[k].get()]
            if not sel:
                messagebox.showwarning(APP, "비교할 방식을 하나 이상 고르세요", parent=top)
                return
            self.s["cmp_sel"], self.s["cmp_ilc"] = sel, bool(v_ilc.get())
            top.destroy()
            self.start(True, compare=([tuple(k.split(":")) for k in sel], bool(v_ilc.get())))
        ttk.Button(bf, text="▶ 비교 시작", command=ok).pack(side="left", padx=3)
        ttk.Button(bf, text="취소", command=top.destroy).pack(side="left", padx=3)
        top.bind("<Escape>", lambda e: top.destroy())
        self.center_popup(top)
        top.grab_set()

    def show_compare(self, comp, off):
        """방식 비교 결과 표 (가운데 창) · 고른 방식을 4 카드에 바로 넣기"""
        if not comp:
            return
        top = tk.Toplevel(self.root)
        top.title("방식 비교 결과")
        top.transient(self.root.winfo_toplevel())
        f = ttk.Frame(top, padding=12)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="방식 비교 · DPD OFF ACLR %.1f / %.1f dBc · Pout %.2f dBm" % (off["aclr_l"], off["aclr_u"], off["pout"]),
                  font=FB).pack(anchor="w", pady=(0, 6))
        cols = ("name", "aclr", "imp", "evm", "pout", "pae", "t", "nc", "size")
        hd = ("방식", "최고 ACLR L / U", "개선", "EVM", "Pout", "PAE", "시간", "계수", "크기")
        wd = (130, 120, 60, 60, 70, 60, 55, 55, 110)
        tv = ttk.Treeview(f, columns=cols, show="headings", height=min(len(comp), 10), selectmode="browse")
        for c_, h, w in zip(cols, hd, wd):
            tv.heading(c_, text=h)
            tv.column(c_, width=w, anchor="w" if c_ in ("name", "size") else "e")
        tv.pack(fill="both", expand=True)
        ok_ = [c for c in comp if c["best"] and c["model"] != "ilc"]
        best = min(ok_, key=lambda c: aw(c["best"])) if ok_ else None
        o = aw(off)
        iid = {}
        for c in comp:
            b = c["best"]
            if b:
                w = aw(b)
                vals = (("★ " if c is best else "") + c["name"], "%.1f / %.1f" % (b["aclr_l"], b["aclr_u"]), "%.1f dB" % (o - w),
                        "%.2f %%" % b["evm"], "%.2f" % b["pout"], ("%.1f %%" % b["pae"]) if b.get("pae") == b.get("pae") else "-",
                        "%.0f초" % c["t"], str(c["n_coef"] or "-"), c["kml"])
            else:
                vals = (c["name"], "실패", "", "", "", "", "%.0f초" % c["t"], "", (c.get("err") or "")[:30])
            iid[tv.insert("", "end", values=vals)] = c
        ttk.Label(f, text="★ = ACLR 가장 좋은 방식 · 개선 = DPD OFF 대비 · ILC 한계와 1 ~ 2 dB 안이면 모델은 충분",
                  foreground="#78909c").pack(anchor="w", pady=(6, 6))
        bf = ttk.Frame(f)
        bf.pack(anchor="e")

        def use():
            sel = tv.selection()
            c = iid.get(sel[0]) if sel else best
            if not c or c["model"] == "ilc":
                return
            self.v["model"].set(c["model"])
            if c["learn"]:
                self.v["method"].set(c["learn"])
            self.log("4 카드 모델 → %s" % c["name"])
            top.destroy()
        ttk.Button(bf, text="고른 방식을 4 카드에 넣기", command=use).pack(side="left", padx=3)
        ttk.Button(bf, text="닫기", command=top.destroy).pack(side="left", padx=3)
        top.bind("<Escape>", lambda e: top.destroy())
        self.center_popup(top)

    # ------------------------------------------------------------ 값 모으기
    def params(self):
        p = {}
        for key, name, dflt, unit, card in self.FIELDS:
            val = self.v[key].get().strip()
            if key in ("focus_mhz", "pout_band"):
                p[key] = val
            elif key in ("L_lag", "L_lead"):              # 빈칸 = None (L 과 같음)
                p[key] = max(0, int(fnum(val, 0))) if val else None
            elif key == "fsw_rbw_khz":
                p[key] = max(fnum(val, 0.0), 0.0) if val else 0.0
            elif key in ("smw_host", "fsw_host", "mod", "fsw_att_db", "fsw_ref_dbm"):
                p[key] = val
            elif key in ("n_sym", "K", "M", "L", "ec_iters", "retry_off", "iters", "n_fit", "nn_hidden", "nn_epochs", "ilc_iters", "dvr_seg", "iq_avg"):
                p[key] = int(fnum(val, float(dflt)))
            else:
                p[key] = fnum(val, float(dflt))
        p["os"] = int(fnum(self.v["os"].get(), 4))
        p["cfr_on"] = bool(self.v["cfr_on"].get())
        p["odd_only"] = bool(self.v["odd_only"].get())
        p["auto_model"] = bool(self.v["auto_model"].get())
        p["auto_wide"] = bool(self.v["auto_wide"].get())
        p["ilcm_widen"] = bool(self.v["ilcm_widen"].get())
        p["ilcm_lin"] = bool(self.v["ilcm_lin"].get())
        p["ilcm_trap"] = bool(self.v["ilcm_trap"].get())
        p["fsw_guard"] = bool(self.v["fsw_guard"].get())
        p["bw_mode"] = self.v["bw_mode"].get()
        p["file_up"] = 1                                # SMW 샘플레이트 올리기는 안 씀 (SMW ARB 한계 → -222 Data out of range)
        p["cap_up"] = 1
        if p["bw_mode"] == "400":                        # 400 MHz: 1966.08 MSps 같은 파일 → 983.04 MSps (±491 MHz 면 바깥 인접 채널까지 충분)
            p["file_max_msps"] = 1000.0 if self.v["file_half400"].get() else 0.0
        else:
            p["file_max_msps"] = 500.0 if self.v["file_halve"].get() else 0.0  # 기본: 그대로
        p["method"] = self.v["method"].get()
        p["aclr_src"] = self.v["aclr_src"].get()
        p["fsw_park"] = bool(self.v["fsw_park"].get())
        p["mod_every"] = self.v["spec_view"].get() in ("aclr", "both")
        p["cal_mod"] = bool(self.v["cal_mod"].get()) and self.v["aclr_src"].get() != "iq"
        p["pout_src"] = self.v["pout_src"].get()
        p["cal_every"] = bool(self.v["cal_every"].get()) and p["cal_mod"]
        p["w_mode"] = self.v["w_mode"].get()
        p["focus_on"] = p["w_mode"] == "focus"
        r_ = str(p.get("fsw_ref_dbm", "")).strip().lower()
        if r_ not in ("auto", "") and fnum(r_, float("nan")) != fnum(r_, float("nan")):
            raise ValueError("FSW Ref Level 은 숫자 (dBm) 또는 auto")
        p["model"] = self.v["model"].get()
        p["torch_dir"] = self.s.get("torch_dir", "")
        E.migrate_method(p)
        p["sig_src"] = "file" if p["bw_mode"] == "400" else self.v["sig_src"].get()
        p["wv_path"] = self.v["wv_path"].get()
        if p["sig_src"] == "file" and not os.path.isfile(p["wv_path"]):
            raise ValueError("불러올 .wv 파일을 고르세요" + (" (400 MHz DPD 는 내 .wv 신호로)" if p["bw_mode"] == "400" else ""))
        p["ch_bw_mhz"] = fnum(self.v["ch_bw_mhz"].get(), 98.28)
        p["adj_mhz"] = fnum(self.v["adj_mhz"].get(), 100.0)
        if self.v["bench_loss"].get():                       # 실행 때 Bench 손실을 다시 읽음 (Bench 에서 바꿨어도 같게)
            self.apply_bench_loss()
            p["in_loss_db"] = fnum(self.v["in_loss_db"].get(), p["in_loss_db"])
            p["out_loss_db"] = fnum(self.v["out_loss_db"].get(), p["out_loss_db"])
        xi, xo, _fr = self.xloss_at(p["freq_ghz"])          # 커플러 · 아이솔레이터 등 추가 손실 (주파수마다)
        p["in_loss_db"] += xi
        p["out_loss_db"] += xo
        p["xloss_in"], p["xloss_out"] = xi, xo
        if self.host is not None:                          # Bench 안: 장비 주소는 Bench [장비 설정] 의 것
            for k, hv in (("smw_host", "var_smw_host"), ("fsw_host", "var_fsw_host")):
                try:
                    p[k] = getattr(self.host, hv).get().strip() or p[k]
                except Exception:
                    pass
            for k, hv in (("smw_port", "var_smw_port"), ("fsw_port", "var_fsw_port")):
                try:
                    p[k] = int(fnum(getattr(self.host, hv).get(), 5025))
                except Exception:
                    pass
        p["rec"] = self.cur_rec()
        p["auto_cal"] = bool(self.v["auto_cal"].get()) and p["rec"] is not None
        p["pout_lock"] = None
        if self.v["pout_lock_on"].get() and self.v["run_mode"].get() == "sweep":
            self.log("⚠ 파워 스윕에서는 목표 Pout 고정 안 함 (레벨마다 SG 그대로) - 목표 Pout 에 맞추려면 4 카드 실행 방식 '한 레벨'")
        if self.v["pout_lock_on"].get() and self.v["run_mode"].get() != "sweep":
            t_ = fnum(self.v["b_target"].get(), float("nan"))
            if t_ != t_:
                raise ValueError("3 카드 목표 Pout 을 넣으세요 (목표 Pout 에 SG 맞추기가 켜져 있음)")
            p["pout_lock"] = t_
        p["pout_strict"] = bool(self.v["pout_strict"].get()) and p["pout_lock"] is not None
        p["pout_tol"] = max(0.01, fnum(self.v["pout_tol"].get(), 0.05))
        p["bad_gain_db"] = max(0.0, fnum(self.v["bad_gain_db"].get(), 3.0))
        if p["K"] < 1 or p["M"] < 1 or p["n_sym"] < 2:
            raise ValueError("K, M 은 1 이상 · 심볼 수는 2 이상")
        return p

    def log(self, s):
        try:
            at_end = (not self.txt.winfo_ismapped()) or self.txt.yview()[1] >= 0.999   # 로그 탭을 안 보고 있으면 늘 따라감
        except Exception:
            at_end = True
        self.txt.insert("end", time.strftime("%H:%M:%S  ") + s + "\n")
        fol = self.v.get("log_follow")
        if (fol is None or fol.get()) and at_end:      # 위로 스크롤해 읽는 중이면 따라가지 않음
            self.txt.see("end")
            if self._log_new_n():
                self._log_new = 0
                self.lbl_lognew.configure(text="")
            return_ = None
        else:
            self._log_new = getattr(self, "_log_new", 0) + 1
            if hasattr(self, "lbl_lognew"):
                self.lbl_lognew.configure(text="새 로그 %d 줄 ↓" % self._log_new)
        if hasattr(self, "lbl_status"):
            self.lbl_status.configure(text=s.strip())

    def _log_new_n(self):
        return getattr(self, "_log_new", 0) and hasattr(self, "lbl_lognew")

    # ------------------------------------------------------------ 실행
    def busy(self, on):
        self.btn_go.configure(state="disabled" if on else "normal")
        self.btn_off.configure(state="disabled" if on else "normal")
        self.btn_ilc.configure(state="disabled" if on else "normal")
        if hasattr(self, "btn_cmp"):
            self.btn_cmp.configure(state="disabled" if on else "normal")
        for b_ in ("btn_send", "btn_recheck", "btn_savefinal", "btn_more", "btn_ilc2m"):
            if hasattr(self, b_):
                getattr(self, b_).configure(state="disabled" if on else "normal")
        if hasattr(self, "btn_stop5"):
            self.btn_stop5.configure(state="normal" if on else "disabled", bg="#c62828" if on else "#b0bec5")
        if hasattr(self, "btn_stop"):
            self.btn_stop.configure(state="normal" if on else "disabled")
        gb = getattr(self, "g_btn", None) if getattr(self, "gwin", None) is not None else None
        if gb:                                             # 그래프 새 창 실행 줄도 같이
            try:
                for k_ in ("go", "ilc", "off", "more"):
                    gb[k_].configure(state="disabled" if on else "normal")
                gb["stop"].configure(state="normal" if on else "disabled", bg="#c62828" if on else "#b0bec5")
            except tk.TclError:
                pass

    SIG_KEYS = ("os", "n_sym", "mod", "cfr_on", "papr_db", "sig_src", "wv_path", "file_max_msps", "bw_mode", "file_up")

    def get_engine(self, p):
        if self.engine is None or self.engine.p.get("smw_host") != p["smw_host"] or self.engine.p.get("fsw_host") != p["fsw_host"]:
            if self.engine is not None:
                self.engine.close()
            self.engine = E.Engine(p, log=lambda s: self.q.put(("log", s)), on_result=lambda r: self.q.put(("res", r)),
                                   on_progress=lambda s: self.q.put(("prog", s)), dc_reader=self.read_dc)
            self.engine.on_lock = lambda sg: self.q.put(("sglock", sg))
            self.engine.on_ilc = lambda e, partial: self.ilc_autosave(e, e.p, partial)    # JSH: ILC 결과마다 .npz 저장
            ks = getattr(self, "_keep_sig", None)
            self._keep_sig = None
            if ks is not None and all(ks[1].get(k) == p.get(k) for k in self.SIG_KEYS):
                self.engine.sig = ks[0]               # 중지 뒤 다시 실행: 신호 설정이 같을 때만 이어 씀 (대역 · 파일 바뀌면 새로)
                self.engine.model = self.engine.make_model()   # 신호만 이어 쓰고 모델은 새로 (예전: 모델이 없어 'NoneType … fit' 오류)
        else:
            sig_keys = self.SIG_KEYS
            mdl_keys = ("K", "M", "L", "L_lag", "L_lead", "odd_only", "lam", "model", "method", "nn_hidden", "nn_epochs", "dvr_seg", "torch_dir")
            if any(self.engine.p.get(k) != p[k] for k in sig_keys):
                self.engine.sig = None            # 신호가 바뀌면 다시 만들기 (모델도 같이)
                self.engine.p.update(p)
            elif self.engine.sig is not None and any(self.engine.p.get(k) != p[k] for k in mdl_keys):
                self.engine.p.update(p)
                self.engine.model = self.engine.make_model()          # 모델만 새로
            self.engine.p.update(p)
        return self.engine

    def make_signal(self):
        if E is None or self.worker:
            return
        try:
            p = self.params()
        except ValueError as e:
            messagebox.showwarning(APP, str(e))
            return
        eng = self.get_engine(p)
        self.busy(True)

        def job():
            try:
                sig = eng.make_signal()
                self.q.put(("sig", "%d 샘플 · %.2f MSps · 길이 %.3f ms · PAPR %.2f dB"
                            % (len(sig.x), sig.fs / 1e6, len(sig.x) / sig.fs * 1e3, E.C.papr(sig.x))))
            except Exception as e:
                self.q.put(("err", "신호 만들기 실패: %s" % err_text(e)))
            self.q.put(("done", None))
        self.worker = threading.Thread(target=job, daemon=True)
        self.worker.start()

    def run_mode_changed(self):
        if self.v["run_mode"].get() == "sweep":
            self.f_sweep.grid()
            self.btn_go.configure(text="▶ 파워 스윕 + DPD")
        else:
            self.f_sweep.grid_remove()
            self.btn_go.configure(text="▶ DPD 실행 (OFF → 반복)")

    def sweep_levels(self):
        a, b, st = (fnum(self.v[k].get(), float("nan")) for k in ("sw_start", "sw_stop", "sw_step"))
        if not (a == a and b == b and st == st) or st <= 0:
            raise ValueError("파워 스윕: SG 시작 · 끝 · 간격을 확인하세요")
        n = int(math.floor(abs(b - a) / st + 1e-6)) + 1
        if n > 60:
            raise ValueError("파워 스윕 레벨이 너무 많습니다 (%d개, 60개까지)" % n)
        sgn = 1 if b >= a else -1
        return [round(a + sgn * st * k, 3) for k in range(n)]

    def start(self, do_dpd, ilc=False, compare=None, more=None, from_ilc=False):
        if E is None or self.worker:
            return
        if from_ilc:
            more = 0                                       # ILC 결과로 모델: 이어서 하기와 같은 엔진 · 표 이어 붙임
        sweep = do_dpd and self.v["run_mode"].get() == "sweep" and compare is None and more is None
        try:
            p = self.params()
            levels = self.sweep_levels() if sweep else None
        except ValueError as e:
            messagebox.showwarning(APP, str(e))
            return
        self.ilc_ctx()                                     # JSH: ILC 파일 이름용 샘플 종류 · 신호 파일 (화면 스레드에서 읽어 둠)
        self.log("손실: In %.2f · Out %.2f dB%s%s" % (
            p["in_loss_db"], p["out_loss_db"],
            (" (%s)" % self.lbl_loss.cget("text").split("(")[-1].rstrip(")")) if self.v["bench_loss"].get() else " (칸 값)",
            (" · 추가 손실 In +%.2f · Out +%.2f 포함" % (p["xloss_in"], p["xloss_out"])) if (p["xloss_in"] or p["xloss_out"]) else " · 추가 손실 없음"))
        self.log("Pout 기준: Pout 보정 칸 %.2f dB · Pout 대역 %s · %s · %s" % (
            p["cal_db"], p.get("pout_band") or "auto",
            "FSW Mod. 화면 Tx 에 맞춤 켬 (첫 측정에서 로그 'Pout 보정 = …')" if p.get("cal_mod") else
            ("FSW Mod. 화면 맞춤 꺼짐" + (" (ACLR 읽기 = I/Q 라 안 됨)" if self.v["aclr_src"].get() == "iq" else "")),
            "Bench 기록 맞춤 켬" if (p.get("auto_cal") and not p.get("cal_mod")) else "Bench 기록 맞춤 안 함"))
        if sweep and max(levels) > p["sg_max_dbm"]:
            if not messagebox.askyesno(APP, "스윕 끝 (%.2f dBm) 이 SG 최대 (%.2f dBm) 보다 큽니다.\nSG 최대까지만 측정할까요?"
                                       % (max(levels), p["sg_max_dbm"])):
                return
            levels = [l for l in levels if l <= p["sg_max_dbm"] + 1e-9]
        if not sweep and more is None and p["sg_dbm"] > p["sg_max_dbm"]:
            messagebox.showwarning(APP, "SG 레벨 (%.2f) 이 SG 최대 (%.2f) 보다 큽니다." % (p["sg_dbm"], p["sg_max_dbm"]))
            return
        t = fnum(self.v["target_aclr"].get(), float("nan"))
        p["target_aclr"] = t if (t == t and not self.v["force_iters"].get()) else None
        pstop = fnum(self.v["sw_pout_stop"].get(), float("nan"))
        self.save_settings()
        self._res0 = len(self.results) if more is not None else 0       # 이번 실행 결과가 시작하는 자리 (기록 파일 저장용)
        if more is None:
            try:
                self.run_bias = self.get_bias()                # 이번 실행의 바이어스 (엑셀 · 기록에)
            except Exception:
                self.run_bias = None
        if more is not None:
            # 이어서: 끝난 (또는 중지한) 실행의 엔진 · 계수 · 입력을 그대로 (표 · 그래프 · 기록도 이어 붙임)
            pend = getattr(self, "_ilc_pending", None) if from_ilc else None
            if from_ilc and pend is None:
                src0 = self.engine if (self.engine is not None and getattr(self.engine, "_resume", None)) else getattr(self, "_last_eng", None)
                if src0 is None or (getattr(src0, "_resume", None) or {}).get("kind") != "ilc":
                    # 메모리에 ILC 결과가 없음 (프로그램을 새로 켰음 등) → 같은 샘플 · 주파수의 가장 최근 저장 파일 자동
                    pend = self.ilc_latest(p)
                    if pend == "cancel":
                        return
            if pend is not None:                           # 저장한 ILC 결과를 불러와서 → 그걸로 모델 만들기
                self._ilc_pending = None
                eng = self.get_engine(p)
                eng._resume = pend
            else:
                src = self.engine if (self.engine is not None and getattr(self.engine, "_resume", None)) else getattr(self, "_last_eng", None)
                if src is None or not getattr(src, "_resume", None):
                    messagebox.showinfo(APP, "이어서 할 결과가 없습니다. DPD 또는 ILC 를 먼저 실행하세요\n(ILC 결과로 모델: 📂 저장한 ILC 로 불러와도 됨).")
                    return
                if from_ilc and (src._resume or {}).get("kind") != "ilc":
                    messagebox.showinfo(APP, "ILC 결과가 없습니다. 5 카드 [한계 ACLR (ILC)] 를 먼저 실행하거나\n📂 저장한 ILC 로 불러오세요.")
                    return
                eng = src if src is self.engine else self._live_engine(src)
            for k in ("target_aclr", "focus_mhz", "focus_w", "focus_on", "w_mode", "pout_src", "cal_every", "pout_strict", "pout_tol", "bad_gain_db", "iq_avg", "settle_s", "aclr_src", "mod_every", "fsw_park",
                      "ilc_gain", "mu", "peak_lim_db"):
                if k in p:
                    eng.p[k] = p[k]
            if from_ilc:                                   # 모델은 지금 4 카드 값으로
                for k in ("model", "method", "K", "M", "L", "L_lag", "L_lead", "odd_only", "lam", "n_fit", "iters", "nn_hidden", "nn_epochs", "dvr_seg", "torch_dir"):
                    if k in p:
                        eng.p[k] = p[k]
            self.cmp_mode = False
            self.cur_run = getattr(self, "_last_run", None)
            if pend is not None:                           # 불러온 ILC: 새 기록으로 (표 · 그래프 새로)
                self.results = []
                self._res0 = 0
                for i in self.tree.get_children():
                    self.tree.delete(i)
                self.clear_view()
                self.new_run(p, False, False)
        else:
            eng = self.get_engine(p)
            self.results = []
            for i in self.tree.get_children():
                self.tree.delete(i)
            self.fsw_col_show(False)
            self.clear_view()                              # 새 측정: 그래프 · 큰 숫자 칸 깨끗하게 (지난 결과와 안 헷갈리게)
        if more is not None:
            pass
        elif compare is not None:
            self.cmp_mode, self.cmp_runs, self.cmp_off, self.cur_run = True, {}, None, None
            self.cmp_name = self.v["run_name"].get().strip() or ("%s %s" % (self.v["b_stype"].get().strip() or "DPD",
                                                                              time.strftime("%m-%d %H:%M"))).strip()
            self.cmp_p = p
        else:
            self.cmp_mode = False
            self.new_run(p, sweep, ilc)
        self.busy(True)
        self._last_prog = ""
        self._had_err = False
        rf_end = bool(self.v["rf_off_end"].get())

        rid = self.run_id = getattr(self, "run_id", 0) + 1
        hb = {"on": True}

        def heartbeat():                               # 측정하는 동안 Bench 에 "장비 쓰는 중" 알림 (0.5초마다)
            while hb["on"] and getattr(self, "run_id", 0) == rid:
                BL.set_busy(True)
                time.sleep(0.5)
            if getattr(self, "run_id", 0) == rid or not self.worker:
                BL.set_busy(False)

        def wait_bench():
            """PA Bench Monitor 가 SMW · FSW 에 연결돼 있으면 읽기를 멈출 때까지 기다림 (최대 5초)"""
            d = BL.read_ack()
            if d is None:
                return
            if d.get("sweep"):
                raise RuntimeError("PA Bench Monitor 가 자동 측정 중입니다. 끝난 뒤에 실행하세요.")
            conn = [k for k, v in (d.get("inst") or {}).items() if v.get("connected")]
            if not conn:
                return
            self.q.put(("log", "PA Bench Monitor 가 %s 에 연결돼 있음 → 읽기 멈추기를 기다리는 중…" % " · ".join(c.upper() for c in conn)))
            t_end = time.time() + 5.0
            while time.time() < t_end:
                eng.check()
                d = BL.read_ack() or {}
                inst = d.get("inst") or {}
                if all(inst.get(k, {}).get("paused") or not inst.get(k, {}).get("connected") for k in conn):
                    time.sleep(0.4)                    # 하던 읽기가 끝날 시간
                    self.q.put(("log", "  Bench 읽기 멈춤 확인 → 측정 시작 (끝나면 Bench 가 다시 읽음)"))
                    return
                time.sleep(0.2)
            self.q.put(("log", "  ⚠ Bench 가 멈춤 확인을 안 보냄 (Bench v2.3.24 이상 필요) - 그대로 진행. "
                               "안 되면 Bench 에서 SMW · FSW 연결을 끊어 주세요"))

        def wait_abort():
            """앞에서 [중지] 한 실행의 마무리 (SG RF OFF · FSW 원래 모드로) 가 끝날 때까지 기다림 (최대 20초)
            예전: 중지 바로 뒤 다시 시작하면 그 마무리가 새 측정 중간에 RF 를 끄거나 FSW 설정을 되돌려
            Pout 을 못 잡고 (DPD OFF 맞추기가 아주 오래) FSW 화면이 이상해질 수 있었음"""
            th = getattr(self, "_abort_th", None)
            if th is None or not th.is_alive():
                return
            self.q.put(("log", "앞 실행 중지 마무리 (SG RF OFF · FSW 되돌리기) 끝나기를 기다리는 중…"))
            th.join(20.0)
            self.q.put(("log", "  ✔ 중지 마무리 끝 → 측정 시작" if not th.is_alive() else "  ⚠ 중지 마무리가 20초 넘게 걸림 - 그대로 진행"))

        def job():
            threading.Thread(target=heartbeat, daemon=True).start()
            try:
                wait_abort()
                wait_bench()
                if eng.sig is None:
                    sig = eng.make_signal()
                    self.q.put(("sig", "%d 샘플 · %.2f MSps · PAPR %.2f dB" % (len(sig.x), sig.fs / 1e6, E.C.papr(sig.x))))
                if from_ilc:
                    eng.run_ilc_model()
                elif more is not None:
                    eng.run_more(more)
                elif compare is not None:
                    eng.run_compare(compare[0], compare[1])
                    self.q.put(("cmp", (list(eng.compare), getattr(eng, "compare_off", None))))
                elif ilc:
                    eng.run_ilc(levels if sweep else None)        # .npz 저장은 ILC 가 끝날 때 엔진이 알려 줌 (on_ilc)
                elif sweep:
                    eng.run_sweep(levels, pstop if pstop == pstop else None)
                else:
                    eng.run(do_dpd)
                self.q.put(("log", "✔ 끝"))
            except E.Stop:
                self.q.put(("log", "■ 중지됨"))
            except Exception as e:
                if not eng.stop:                         # 중지로 끊긴 건 오류로 안 띄움
                    self.q.put(("err", "측정 실패: %s" % err_text(e)))
            finally:
                try:
                    eng.auto_summary()                   # auto 로 둔 설정이 실제 어떤 값이 됐는지 로그에
                except Exception:
                    pass
                if not eng.stop:                         # 중지면 abort 가 따로 RF OFF · FSW 복귀
                    self.rf_state = True
                    if rf_end:
                        eng.rf_off()
                        self.rf_state = False
                    try:
                        eng.fsw_restore(keep_view=True)  # 끝 · 오류: FSW 를 원래 모드로 (Mod. 화면은 신호 가운데 그대로)
                    except Exception:
                        pass
                    eng.go_local()                       # 끝나면 SMW · FSW 앞 패널 바로 조작 (Local)
                hb["on"] = False
                self.q.put(("done", rid))
        self.worker = threading.Thread(target=job, daemon=True)
        self.worker.start()

    # ------------------------------------------------------------ 기록 (ACLR vs Pout, 과거와 비교)
    HIST_FILE = os.path.join(SET_DIR, "history.json")

    def load_hist(self):
        if not hasattr(self, "hist"):
            try:
                with open(self.HIST_FILE, encoding="utf-8") as f:
                    self.hist = json.load(f)
            except Exception:
                self.hist = []
        return self.hist

    # ------------------------------------------------------------ 기록마다 측정 결과 전체 (스펙트럼 · AM-AM · AM-PM) 파일
    RUNS_DIR = os.path.join(SET_DIR, "runs")

    def run_file(self, rid):
        return os.path.join(self.RUNS_DIR, "%s.json.gz" % rid)

    @staticmethod
    def slim_result(r):
        """기록 파일용: 그래프에 필요한 값만 · 소수 2 자리 · AM 점은 1500 개까지"""
        def rnd(a, n=2, mx=None):
            if a is None:
                return None
            a = list(a)
            if mx and len(a) > mx:
                k = len(a) / float(mx)
                a = [a[int(i * k)] for i in range(mx)]
            return [round(float(v), n) if v == v else None for v in a]
        keep = ("label", "kind", "aclr_l", "aclr_u", "aclr_iq_l", "aclr_iq_u", "aclr_mod_l", "aclr_mod_u", "aclr_src", "pout", "pin", "sg", "gain", "evm",
                "pae", "de", "papr", "nmse", "t", "base_sg", "method", "ch_bw", "adj", "focus_l", "focus_u", "step",
                "cars", "aclr_in", "aclr_in_nm", "aclr_pos", "id", "vdd", "pout_iq", "pout_spec", "pout_used", "de_iq", "de_spec",
                "fsw_ref", "fsw_att", "mod_att")
        o = {k: r.get(k) for k in keep if r.get(k) is not None}
        if r.get("vdd_wi") is not None:                  # 가정 Vdd 로 다시 계산한 값은 기록 파일에 안 남김 (측정 때 값으로)
            for k in ("vdd", "de", "pae", "de_iq", "de_spec"):
                if r.get(k + "_meas") is not None:
                    o[k] = r[k + "_meas"]
        for k in list(o):
            if isinstance(o[k], float) and o[k] != o[k]:
                o[k] = None
        for k in ("spec_f", "spec_db", "iq_f", "iq_db", "mod_f", "mod_db"):
            if r.get(k) is not None:
                o[k] = rnd(r[k], 2)
        n_am = len(r.get("am_x") or [])
        for k in ("am_x", "am_g", "am_p"):
            if r.get(k) is not None:
                o[k] = rnd(r[k], 3, 1500 if n_am > 1500 else None)
        return o

    def fsw_view_back(self):
        """DPD 가 남겨 둔 FSW Mod. 화면 (DPD 주파수 · 반송파 설정) 을 원래대로 - Bench 의 Pulse · Mod. 탭 / 측정 시작 때
        (돌고 있으면 안 함 · 엔진 연결이 없으면 잠깐 새로 연결)"""
        if self.worker is not None and self.worker.is_alive():
            return
        I = getattr(E, "I", None) if E is not None else None
        if I is None:
            return
        try:
            if not I.FSW._load_keep(I.FSW):
                return
        except Exception:
            return

        def job():
            eng = self.engine or getattr(self, "_last_eng", None)
            f = getattr(eng, "fsw", None) if eng is not None else None
            try:
                if f is not None and hasattr(f, "restore_view") and getattr(f, "io", None) is not None:
                    f.restore_view()
                    return
                host = (self.v["fsw_host"].get() or "").strip()
                if not host or host.lower() == "demo":
                    return
                g = I.FSW.__new__(I.FSW)
                g.io = I.ScpiSocket(host, int(self.s.get("fsw_port") or 5025), timeout=10.0, name="FSW")
                g.log = lambda m: self.q.put(("log", m))
                g.restore_view()
                g.io.close()
            except Exception as ex:
                self.q.put(("log", "⚠ FSW Mod. 화면 원래대로 못 돌림: %s" % ex))
        threading.Thread(target=job, daemon=True).start()

    def save_run_data(self):
        """이번 실행 결과 전체를 기록 (run) 마다 파일로 → 그래프 새 창 [기록 보기] 에서 다시 띄움"""
        new = self.results[getattr(self, "_res0", 0):]
        if not new:
            return
        jobs = []
        if getattr(self, "cmp_mode", False):
            for m, run in (getattr(self, "cmp_runs", {}) or {}).items():
                jobs.append((run, [r for r in new if r.get("method") in (m, "OFF")], False))
        elif getattr(self, "cur_run", None) is not None:
            jobs.append((self.cur_run, new, getattr(self, "_res0", 0) > 0))
        try:
            import gzip
            os.makedirs(self.RUNS_DIR, exist_ok=True)
            for run, rs, append in jobs:
                path = self.run_file(run["id"])
                data = []
                if append and os.path.isfile(path):
                    try:
                        with gzip.open(path, "rt", encoding="utf-8") as f:
                            data = json.load(f)
                    except Exception:
                        data = []
                data += [self.slim_result(r) for r in rs]
                with gzip.open(path + ".tmp", "wt", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                os.replace(path + ".tmp", path)
                if hasattr(self, "_run_cache"):
                    self._run_cache.pop(run["id"], None)
            self._res0 = len(self.results)
            if getattr(self, "gwin", None) is not None:
                self.gwin_fill_runs()
            # 오래된 파일 정리 (기록 목록에 없는 것)
            ids = {r["id"] for r in self.load_hist()}
            for fn in os.listdir(self.RUNS_DIR):
                if fn.endswith(".json.gz") and fn[:-8] not in ids:
                    try:
                        os.remove(os.path.join(self.RUNS_DIR, fn))
                    except OSError:
                        pass
        except Exception as e:
            self.log("⚠ 기록 파일 저장 실패: %s" % e)

    def load_run_data(self, rid):
        cache = self.__dict__.setdefault("_run_cache", {})
        if rid in cache:
            return cache[rid]
        try:
            import gzip
            with gzip.open(self.run_file(rid), "rt", encoding="utf-8") as f:
                cache[rid] = json.load(f)
        except Exception:
            cache[rid] = None
        return cache[rid]

    def save_hist(self):
        try:
            os.makedirs(SET_DIR, exist_ok=True)
            tmp = self.HIST_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.hist[-300:], f, ensure_ascii=False)
            os.replace(tmp, self.HIST_FILE)
        except Exception as e:
            self.log("기록 저장 실패: %s" % e)

    def new_run(self, p, sweep, ilc=False):
        self.load_hist()
        st = self.v["b_stype"].get().strip()
        name = self.v["run_name"].get().strip() or ("%s %s" % (st or "DPD", time.strftime("%m-%d %H:%M"))).strip()
        sig = ("내 .wv %s" % os.path.basename(p["wv_path"])) if p["sig_src"] == "file" else \
              "NR100 1C %s ×%d CFR %s" % (p["mod"], p["os"], ("%.1f" % p["papr_db"]) if p["cfr_on"] else "OFF")
        run = dict(id="%d" % (time.time() * 1000), name=name, when=time.strftime("%Y-%m-%d %H:%M"), freq=p["freq_ghz"],
                   stype=st, sig=sig, dpd=("ILC 한계" if ilc else self.model_text(p, None)),
                   sweep=bool(sweep), on=True, pts=[], bias=getattr(self, "run_bias", None), bw=p.get("bw_mode", "100"))
        self.hist.append(run)
        self.cur_run = run
        self._last_run = run
        self.refresh_hist_list()

    def add_hist_point(self, r):
        pt = dict(kind=r.get("kind", "dpd"), base=r.get("base_sg"), sg=r["sg"], pout=r["pout"],
                  aclr_l=r["aclr_l"], aclr_u=r["aclr_u"], evm=r["evm"], label=r["label"],
                  de=r.get("de"), pae=r.get("pae"), id=r.get("id"), pdc=r.get("pdc"))
        if getattr(self, "cmp_mode", False):
            # 방식 비교: 방식마다 기록 하나 (같은 DPD OFF 점을 앞에)
            m = r.get("method") or "?"
            if m == "OFF":
                self.cmp_off = pt
                return
            run = self.cmp_runs.get(m)
            if run is None:
                p = self.cmp_p
                sig = ("내 .wv %s" % os.path.basename(p["wv_path"])) if p["sig_src"] == "file" else \
                      "NR100 1C %s ×%d CFR %s" % (p["mod"], p["os"], ("%.1f" % p["papr_db"]) if p["cfr_on"] else "OFF")
                run = dict(id="%d" % (time.time() * 1000), name="%s · %s" % (self.cmp_name, m), when=time.strftime("%Y-%m-%d %H:%M"),
                           freq=p["freq_ghz"], stype=self.v["b_stype"].get().strip(), sig=sig, dpd="비교: " + m,
                           sweep=False, on=True, pts=[dict(self.cmp_off)] if self.cmp_off else [], bias=getattr(self, "run_bias", None))
                self.load_hist().append(run)
                self.cmp_runs[m] = run
            run["pts"].append(pt)
            self.refresh_hist_list()
            self.draw_hist()
            return
        run = getattr(self, "cur_run", None)
        if run is None:
            return
        run["pts"].append(pt)
        self.refresh_hist_list()
        self.draw_hist()

    @staticmethod
    def run_curves(run):
        """기록 → (OFF 점들, ON 점들): 레벨(base SG)마다 OFF 1개 · DPD 는 그 레벨의 마지막 결과"""
        off, on = {}, {}
        for q in run.get("pts", []):
            k = round(q.get("base") if q.get("base") is not None else q["sg"], 3)
            if q["kind"] == "ilc" and k in on and aw(on[k]) <= aw(q):
                continue                                   # ILC 는 그 레벨에서 가장 좋은 것
            (off if q["kind"] == "off" else on)[k] = q
        return [off[k] for k in sorted(off)], [on[k] for k in sorted(on)]

    def yval(self, q):
        y = self.v["y_q"].get()
        if y == "aclr":
            return aw(q)
        v = q.get(y)
        return v if isinstance(v, (int, float)) and v is not None else float("nan")

    def hist_colors(self):
        """주파수 → 고정 색 (낮은 주파수부터 파랑 · 초록 · 빨강 …, PA Bench Monitor 와 같은 순서)"""
        pal = ["#1f77b4", "#2ca02c", "#d62728", "#ff7f0e", "#9467bd", "#8c564b", "#e377c2", "#17becf", "#bcbd22", "#7f7f7f"]
        fs = sorted({round(fnum(r.get("freq"), 0), 6) for r in self.load_hist()})
        return {f: pal[i % len(pal)] for i, f in enumerate(fs)}

    def draw_hist(self):
        if not hasattr(self, "p_hist"):
            return
        self.p_hist.remember(self.s, "yr_hist_" + self.v["y_q"].get())     # 세로축 값 (ACLR · EVM · DE …) 마다 따로
        fc = self.hist_colors()
        shown = [r for r in self.load_hist() if r.get("on") and r.get("pts")]
        same = {}
        ser = []
        for r in shown:
            f = round(fnum(r.get("freq"), 0), 6)
            n = same.get(f, 0)
            same[f] = n + 1
        seen = {}
        for r in shown:
            f = round(fnum(r.get("freq"), 0), 6)
            k = seen.get(f, 0)
            seen[f] = k + 1
            col = fc.get(f, "#1f77b4")
            if same[f] > 1:                              # 같은 주파수 여러 기록 → 예전 것일수록 옅게
                col = mix(col, THEME["plot_bg"], 0.55 * (same[f] - 1 - k) / max(same[f] - 1, 1))
            off, on = self.run_curves(r)
            nm = "%s %.4g GHz" % (r["name"], fnum(r.get("freq"), 0))
            if off:
                ser.append(dict(x=[q["pout"] for q in off], y=[self.yval(q) for q in off], color=col, width=2, dash=(5, 4),
                                marks=True, hollow=True, r=3, label=nm + " OFF"))
            if on:
                ser.append(dict(x=[q["pout"] for q in on], y=[self.yval(q) for q in on], color=col, width=2,
                                marks=True, r=3, label=nm + " DPD"))
        bub = []
        if self.worker and getattr(self, "cur_run", None) and self.cur_run.get("pts"):
            q = self.cur_run["pts"][-1]
            y = self.yval(q)
            unit = "%" if self.v["y_q"].get() in ("evm", "de", "pae") else "dBc"
            bub.append((q["pout"], y, "● 측정 중 %.1f %s" % (y, unit) if unit == "dBc" else "● 측정 중 %.1f %%" % y,
                        "#e53935" if q["kind"] == "off" else "#1e88e5"))
        ylab = {"aclr": "ACLR 나쁜 쪽 (dBc)", "aclr_l": "ACLR L (dBc)", "aclr_u": "ACLR U (dBc)", "evm": "EVM (%)",
                "de": "DE (%)", "pae": "PAE (%)"}[self.v["y_q"].get()]
        self.p_hist.title = ylab.split(" (")[0] + " vs Pout"
        self.p_hist.yl = "%" if self.v["y_q"].get() in ("evm", "de", "pae") else "dBc"
        self.p_hist.set(ser[-24:], bubbles=bub)
        gp = getattr(self, "gplots", None)
        if gp and "hist" in gp:                            # 그래프 새 창 'ACLR vs Pout' 도 같이
            try:
                gp["hist"].remember(self.s, "yr_hist_" + self.v["y_q"].get())
                gp["hist"].title, gp["hist"].yl = self.p_hist.title, self.p_hist.yl
                gp["hist"].set(ser[-24:], bubbles=bub)
            except tk.TclError:
                pass

    def refresh_hist_list(self):
        for tr in (getattr(self, "htree", None), getattr(self, "g_htree", None)):     # 메인 · 그래프 새 창 기록 목록
            if tr is None:
                continue
            try:
                sel = set(tr.selection())
                tr.delete(*tr.get_children())
                for r in reversed(self.load_hist()):
                    off, on = self.run_curves(r)
                    tr.insert("", "end", iid=r["id"], values=("☑" if r.get("on") else "☐", r["name"], "%.4g" % fnum(r.get("freq"), 0),
                                                              len(off) + len(on), r.get("when", "")[2:]))
                for i in sel:
                    if tr.exists(i):
                        tr.selection_add(i)
            except tk.TclError:
                pass

    def hist_run(self, iid):
        return next((r for r in self.load_hist() if r["id"] == iid), None)

    def hist_click(self, e):
        tr = e.widget
        if tr.identify_column(e.x) != "#1":
            return
        iid = tr.identify_row(e.y)
        r = self.hist_run(iid) if iid else None
        if r is not None:
            r["on"] = not r.get("on")
            self.save_hist()
            self.refresh_hist_list()
            self.draw_hist()
            return "break"

    def hist_show(self, on, tree=None):
        sel = (tree or self.htree).selection()
        for r in self.load_hist():
            if not sel or r["id"] in sel:
                r["on"] = on
        self.save_hist()
        self.refresh_hist_list()
        self.draw_hist()

    def hist_rename(self):
        sel = self.htree.selection()
        r = self.hist_run(sel[0]) if sel else None
        if r is None:
            return
        from tkinter import simpledialog
        nm = simpledialog.askstring(APP, "기록 이름", initialvalue=r["name"], parent=self.root)
        if nm:
            r["name"] = nm.strip()
            self.save_hist()
            self.refresh_hist_list()
            self.draw_hist()

    def hist_delete(self):
        sel = self.htree.selection()
        if not sel:
            return
        if not messagebox.askyesno(APP, "고른 기록 %d개를 지울까요?" % len(sel)):
            return
        self.hist = [r for r in self.load_hist() if r["id"] not in sel]
        self.save_hist()
        for rid_ in sel:
            try:
                os.remove(self.run_file(rid_))
            except OSError:
                pass
        self.refresh_hist_list()
        self.draw_hist()

    def hist_export(self):
        """PA Analyzer 가 읽는 엑셀: 기록 × (DPD OFF / DPD ON) 마다 시트 하나 (조건 + 표, ACLR · EVM 열)"""
        sel = self.htree.selection()
        runs = [r for r in self.load_hist() if (r["id"] in sel if sel else r.get("on")) and r.get("pts")]
        if not runs:
            messagebox.showinfo(APP, "내보낼 기록을 고르세요 (고른 게 없으면 표시 중인 기록).")
            return
        try:
            import openpyxl
        except ImportError:
            messagebox.showerror(APP, "openpyxl 이 없습니다.")
            return
        path = self.ask("PA Analyzer 용 엑셀", ".xlsx", [("Excel", "*.xlsx")], "DPD_%sACLR_%s.xlsx" % ("".join("%sM_" % b_ for b_ in sorted({str(r_["bw"]) for r_ in runs if r_.get("bw")})), self.stamp()))
        if not path:
            return
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        used = set()
        for r in runs:
            for kind, pts in zip(("OFF", "DPD"), self.run_curves(r)):
                if not pts:
                    continue
                nm = ("%s %s" % (r["name"], kind))[:28]
                for ch in '[]:*?/\\':
                    nm = nm.replace(ch, "_")
                base, k = nm, 2
                while nm in used:
                    nm = "%s(%d)" % (base[:25], k)
                    k += 1
                used.add(nm)
                ws = wb.create_sheet(nm)
                ws.append(["FREQ", "%.6g GHz" % fnum(r.get("freq"), 0)])
                ws.append(["DUT", "%s %s" % (r["name"], "DPD OFF" if kind == "OFF" else "DPD ON")])
                ws.append(["Signal", r.get("sig", "")])
                ws.append(["DPD", r.get("dpd", "") if kind == "DPD" else "OFF"])
                b = r.get("bias") or {}
                if b.get("vdd") is not None and b.get("vdd") == b.get("vdd"):
                    ws.append(["VDD", round(b["vdd"], 2)])
                for lab, kk, mul, nd in (("VGS,DRV", "vg_drv", 1.0, 3), ("VGS,CA", "vg_ca", 1.0, 3), ("VGS,PK", "vg_pk", 1.0, 3),
                                         ("IQ_DRV", "i_drv", 1e3, 1), ("IQ_CA", "i_ca", 1e3, 1)):
                    x = b.get(kk)
                    if isinstance(x, (int, float)) and x == x:
                        ws.append([lab, round(x * mul, nd)])
                ws.append([])
                ws.append(["SG Pout (dBm)", "DUT_Pout (dBm)", "ACLR (dBc)", "EVM (%)", "Id (mA)", "DC_Power(mW)", "DE(%)", "PAE(%)",
                           "Lower (dBc)", "Upper (dBc)"])

                def rr(v, n):
                    return round(v, n) if isinstance(v, (int, float)) and v is not None and math.isfinite(v) else None
                for q in pts:
                    ws.append([rr(q["sg"], 3), rr(q["pout"], 3), rr(aw(q), 3), rr(q["evm"], 3),
                               rr((q.get("id") or float("nan")) * 1e3, 2), rr((q.get("pdc") or float("nan")) * 1e3, 1),
                               rr(q.get("de"), 3), rr(q.get("pae"), 3), rr(q["aclr_l"], 3), rr(q["aclr_u"], 3)])
        try:
            wb.save(path)
        except PermissionError:
            messagebox.showerror(APP, "파일이 열려 있어 저장하지 못했습니다:\n%s" % path)
            return
        self.remember_dir(path)
        self.log("PA Analyzer 용 엑셀 저장: %s (시트 %d개) - PA Analyzer 에서 엑셀 불러오기로 열면 됩니다" % (path, len(used)))

    def stop(self):
        """즉시 중지: 장비 통신을 바로 끊고 화면은 바로 '중지됨' (계산 중이던 스레드는 뒤에서 조용히 끝남)"""
        eng = self.engine
        if eng is None or not self.worker:
            return
        self.engine = None                                    # 다음 실행은 새 연결 (신호는 이어 씀)
        self._keep_sig = (eng.sig, {k: eng.p.get(k) for k in self.SIG_KEYS})
        self._last_eng = eng                                  # 중지해도 측정마다 보관한 입력 · 출력 신호를 고를 수 있게
        th = threading.Thread(target=lambda: eng.abort(rf_off=True), daemon=True)   # 중지 = 안전하게 RF OFF
        th.start()
        self._abort_th = th                                   # 다음 실행은 이 마무리 (RF OFF · FSW 되돌리기) 가 끝난 뒤에 시작
        self.rf_state = False
        self.run_id = getattr(self, "run_id", 0) + 1         # 하트비트 멈춤 · 이전 실행 끝 알림 무시
        BL.set_busy(False)                                     # Bench 가 바로 다시 읽게
        self.worker = None
        self.st_last = ("■ 중지 %s" % time.strftime("%H:%M"), "#c62828")
        self.log("■ 중지")
        self.busy(False)
        self.update_chips()
        self.save_run_data()
        if getattr(self, "cur_run", None) is not None:
            if not self.cur_run.get("pts"):
                self.hist = [r for r in self.load_hist() if r is not self.cur_run]
            self.save_hist()
            self.refresh_hist_list()
            self.cur_run = None
        self.draw_hist()
        self.big["aclr"][1].configure(text="■ 중지됨", fg="#90a4ae")

    def restore_left_w(self):
        w_ = int(fnum(self.s.get("left_w"), 0) or 0)
        if w_ >= 260:
            try:
                self.main_pw.sashpos(0, w_)
            except tk.TclError:
                pass
        self.reflow_soon()

    def save_left_w(self):
        try:
            self.s["left_w"] = int(self.main_pw.sashpos(0))
        except tk.TclError:
            pass
        self.reflow_soon()

    def reflow_soon(self):
        if getattr(self, "_rf_job", None):
            try:
                self.root.after_cancel(self._rf_job)
            except tk.TclError:
                pass
        self._rf_job = self.root.after(120, lambda: self.reflow_left(passes=2))

    def reflow_left(self, passes=1):
        """왼쪽 카드 글자 줄바꿈: 지금 폭에 맞춰 단위 · 설명 (회색 글자) · 체크 글자의 줄 길이를 정함"""
        self._rf_job = None
        sf = getattr(self, "left_sf", None)
        if sf is None:
            return
        try:
            W = sf.cv.winfo_width()
            bx = sf.body.winfo_rootx()
        except tk.TclError:
            return
        if W < 120:
            return
        st_ = ttk.Style()
        made = getattr(self, "_wstyles", set())
        self._wstyles = made

        def cb_style(wl):
            nm = "W%d.Card.TCheckbutton" % wl
            if nm not in made:
                st_.configure(nm, wraplength=wl)
                made.add(nm)
            return nm

        def walk(w):
            for c in w.winfo_children():
                cls = c.winfo_class()
                try:
                    x = c.winfo_rootx() - bx if c.winfo_ismapped() else int(W * 0.45)
                    wl = max(70, W - x - 30)
                    if cls == "TCheckbutton":
                        sty = str(c.cget("style"))
                        if sty == "Card.TCheckbutton" or (sty.startswith("W") and sty.endswith(".Card.TCheckbutton")):
                            nm = cb_style(max(60, (W - x - 40) // 10 * 10))
                            if sty != nm:
                                c.configure(style=nm)
                    elif cls == "TLabel":
                        sty = str(c.cget("style"))
                        long_ = len(str(c.cget("text"))) > 14
                        if sty in ("Unit.TLabel", "Hint.TLabel", "Info.TLabel") or (sty in ("Lab.TLabel", "Card.TLabel", "") and long_):
                            if int(str(c.cget("wraplength") or 0) or 0) != wl:
                                c.configure(wraplength=wl)
                    elif cls == "Label" and int(c.cget("wraplength") or 0) > 0:
                        if int(c.cget("wraplength")) != wl:
                            c.configure(wraplength=wl)
                    elif cls == "TCombobox" and c.winfo_ismapped() and x + c.winfo_reqwidth() > W - 8:
                        c.configure(width=max(8, int(c.cget("width")) - 4))   # 넘치는 고르기 칸은 좁게
                except (tk.TclError, ValueError):
                    pass
                walk(c)
        walk(sf.body)
        if passes > 1:                                     # 줄바꿈으로 칸 위치가 바뀜 → 한 번 더
            self.root.after(60, lambda: self.reflow_left(passes - 1))

    def dev_addr(self, kind):
        """장비 주소 · 포트 (kind = 'smw' · 'fsw'): Bench 안이면 Bench [장비 설정] 의 것 (예전: DPD 쪽 옛 주소를 써서 안 먹힐 때가 있었음)"""
        host, port = "", 5025
        if self.host is not None:
            try:
                host = getattr(self.host, "var_%s_host" % kind).get().strip()
                port = int(fnum(getattr(self.host, "var_%s_port" % kind).get(), 5025))
            except Exception:
                pass
        if not host and "%s_host" % kind in self.v:
            host = (self.v["%s_host" % kind].get() or "").strip()
            port = int(fnum(self.s.get("%s_port" % kind), 5025) or 5025)
        return host, port

    def connected(self):
        e_ = self.engine
        return e_ is not None and getattr(e_, "fsw", None) is not None

    BTN_ST = dict(relief="flat", bd=0, font=("맑은 고딕", 9, "bold"), padx=10, pady=3, cursor="hand2",
                  activeforeground="white", fg="white")

    def state_btn(self, parent, cmd):
        return tk.Button(parent, text="…", command=cmd, bg="#78909c", activebackground="#607d8b", **self.BTN_ST)

    def rf_now(self):
        """SG RF 상태: Bench 안이면 Bench 가 읽는 SMW 상태 · 아니면 마지막으로 이 프로그램이 한 것 (None = 모름)"""
        if self.worker is not None and self.worker.is_alive():
            return True
        h = self.host
        if h is not None:
            try:
                if h.smw.get("conn") and h.smw.get("rf") is not None and not h.smw_w.paused:
                    return bool(h.smw.get("rf"))
            except Exception:
                pass
        return getattr(self, "rf_state", None)

    def conn_refresh(self):
        """[RF ON/OFF] · [장비 연결] 버튼 = 지금 상태 (1 초마다): 켜져 있으면 색 · 글자로 ON / 연결됨"""
        try:
            rf = self.rf_now()
            if rf is True:
                t_, bg_ = "● RF ON  (누르면 끄기)", "#c62828"
            elif rf is False:
                t_, bg_ = "○ RF OFF  (누르면 켜기)", "#546e7a"
            else:
                t_, bg_ = "RF ?  (눌러서 확인)", "#78909c"
            con = self.connected()
            t2, bg2 = ("● 장비 연결됨  (누르면 해제)", "#2e7d32") if con else ("○ 장비 연결 안 됨  (누르면 연결)", "#546e7a")
            if getattr(self, "_conn_busy", False):
                t2, bg2 = "… 바꾸는 중", "#78909c"
            btns = [(getattr(self, "btn_rf", None), t_, bg_), (getattr(self, "btn_conn", None), t2, bg2)]
            if getattr(self, "btn_rf_top", None) is not None:
                btns.append((self.btn_rf_top, "● SG RF ON" if rf is True else ("○ SG RF OFF" if rf is False else "SG RF ?"),
                             "#c62828" if rf is True else "#2f3d4a"))
            gb = getattr(self, "g_btn", None) if getattr(self, "gwin", None) is not None else None
            if gb:
                btns += [(gb.get("rf"), t_, bg_), (gb.get("conn"), t2, bg2)]
            for b_, t__, c__ in btns:
                if b_ is not None and (b_.cget("text") != t__ or b_.cget("bg") != c__):
                    b_.configure(text=t__, bg=c__, activebackground=c__)
        except (tk.TclError, AttributeError):
            return
        self.root.after(1000, self.conn_refresh)

    def rf_toggle(self):
        """RF 한 버튼: 켜져 있으면 끄기 · 꺼져 있으면 (확인 후) 켜기 · 모르면 SMW 에 물어봐서 반대로. 측정 중이면 = 중지"""
        if self.worker is not None and self.worker.is_alive():
            self.stop()
            return
        host, port = self.dev_addr("smw")
        eng = self.engine or getattr(self, "_last_eng", None)
        smw = getattr(eng, "smw", None) if eng is not None else None

        def ask_then(st):
            if st:
                self.rf_set(False)
            elif messagebox.askyesno(APP, "SMW RF 를 켤까요? (지금 SMW 에 올라가 있는 신호 · 레벨 그대로 나감)"):
                self.rf_set(True)

        def job():
            st = None
            try:
                if smw is not None and getattr(smw, "io", None) is not None:
                    st = smw.io.query("OUTP1?").strip() in ("1", "ON")
                elif smw is not None:
                    st = bool(getattr(self, "rf_state", False))       # 가상 장비
                elif host and host.lower() != "demo":
                    io = E.I.ScpiSocket(host, port, timeout=5.0, name="SMW")
                    try:
                        st = io.query("OUTP1?").strip() in ("1", "ON")
                        io.write("&GTL")
                    finally:
                        io.close()
                else:
                    st = bool(getattr(self, "rf_state", False))
            except Exception as e:
                self.q.put(("log", "⚠ SMW RF 상태 못 읽음: %s (SMW 주소 · 연결 확인)" % e))
                return
            self.q.put(("call", lambda: ask_then(st)))
        threading.Thread(target=job, daemon=True).start()

    def rf_set(self, on):
        if not on:
            self.rf_off()
            return
        host, port = self.dev_addr("smw")
        eng = self.engine or getattr(self, "_last_eng", None)
        smw = getattr(eng, "smw", None) if eng is not None else None

        def job():
            try:
                done = False
                if smw is not None:
                    try:
                        smw.rf(True)
                        done = True
                    except Exception:
                        pass
                if not done and host and host.lower() != "demo":
                    io = E.I.ScpiSocket(host, port, timeout=5.0, name="SMW")
                    try:
                        io.write("OUTP1 ON")
                        io.opc()
                        io.write("&GTL")
                    finally:
                        io.close()
                self.rf_state = True
                self.q.put(("log", "● SG RF ON"))
            except Exception as e:
                self.q.put(("log", "⚠ SG RF ON 실패: %s" % e))
        threading.Thread(target=job, daemon=True).start()

    def conn_toggle(self):
        """장비 연결 해제 = SMW · FSW 연결 끊고 Local (앞 패널 바로 조작 · 다른 프로그램이 써도 안 섞임)
        장비 연결 = 연결해서 확인 (IDN) 후 Local. 측정은 실행하면 알아서 연결"""
        if self.worker is not None and self.worker.is_alive():
            messagebox.showinfo(APP, "측정 중에는 못 바꿉니다. 중지하거나 끝난 뒤에 하세요.")
            return
        if getattr(self, "_conn_busy", False):
            return
        if self.connected():
            eng = self.engine
            self._conn_busy = True

            def job():
                try:
                    eng.go_local(close=True)
                    self.q.put(("log", "장비 연결 해제 - SMW · FSW 앞 패널 바로 조작 가능 (다음 실행 때 자동 연결)"))
                finally:
                    self._conn_busy = False
            threading.Thread(target=job, daemon=True).start()
            return
        try:
            eng = self.get_engine(self.params())
        except Exception as e:
            messagebox.showwarning(APP, str(e))
            return

        self._conn_busy = True

        def job2():
            try:
                eng.stop = False
                if getattr(eng, "fsw", None) is None or getattr(eng, "smw", None) is None:
                    eng.smw = eng.fsw = None             # 반쯤 끊긴 연결은 버리고 새로
                eng._connect0()
                eng.go_local()
                self.q.put(("log", "장비 연결됨 (앞 패널은 Local 로 둠)"))
            except Exception as e:
                self.q.put(("log", "⚠ 장비 연결 실패: %s (주소 %s / %s)" % (e, self.dev_addr("smw")[0], self.dev_addr("fsw")[0])))
            finally:
                self._conn_busy = False
        threading.Thread(target=job2, daemon=True).start()

    def fsw_state_save(self):
        """FSW 지금 설정 (모든 채널 · 화면 · 마커 · ACP) → PC 에 .dfl (나중에 [FSW 설정 불러오기] 로 그대로)"""
        host, port = self.dev_addr("fsw")
        if not host:
            messagebox.showwarning(APP, "FSW 주소가 없습니다 (장비 설정)")
            return
        path = self.ask("FSW 설정 저장", ".dfl", [("FSW 설정", "*.dfl")], "FSW_%s_%s.dfl" % (self.bw_tag(), self.stamp()))
        if not path:
            return
        self.remember_dir(path)
        self.log("FSW 설정 받는 중…")

        def job():
            try:
                d = E.I.fsw_state_save(host, port)
                with open(path, "wb") as f:
                    f.write(d)
                self.q.put(("log", "FSW 설정 저장: %s (%.0f kB) → [FSW 설정 불러오기] 로 그대로 되돌림" % (path, len(d) / 1024)))
            except Exception as e:
                self.q.put(("log", "⚠ FSW 설정 저장 실패: %s" % e))
        threading.Thread(target=job, daemon=True).start()

    def fsw_state_load(self):
        """PC 의 .dfl → FSW 로 보내 불러오기 (저장할 때 화면 · 채널 · 마커 그대로). 측정 중에는 안 됨"""
        if self.worker is not None and self.worker.is_alive():
            messagebox.showinfo(APP, "측정 중에는 FSW 설정을 못 불러옵니다 (측정이 꼬임). 중지하거나 끝난 뒤에 하세요.")
            return
        host, port = self.dev_addr("fsw")
        if not host:
            messagebox.showwarning(APP, "FSW 주소가 없습니다 (장비 설정)")
            return
        path = filedialog.askopenfilename(title="FSW 설정 불러오기", filetypes=[("FSW 설정", "*.dfl"), ("모든 파일", "*.*")],
                                          initialdir=os.path.dirname(self.s.get("fsw_dfl_last") or "") or self.s.get("save_dir") or None)
        if not path:
            return
        self.s["fsw_dfl_last"] = path                      # 최근 불러온 설정 → DPD 화면을 열 때 자동으로 (아래 체크)
        self.save_settings()
        self.dfl_label()
        self.dfl_send(path, host, port)

    def dfl_label(self):
        if not hasattr(self, "chk_dfl"):
            return
        pth = self.s.get("fsw_dfl_last") or ""
        self.chk_dfl.configure(text=("DPD 화면 열 때 이 FSW 설정 자동: %s" % os.path.basename(pth)) if pth else
                               "DPD 화면 열 때 최근 불러온 FSW 설정 자동 (아직 불러온 설정 없음)")

    def fsw_dfl_auto(self, why="DPD 화면"):
        """DPD 화면으로 올 때: 최근 [설정 불러오기…] 한 .dfl 을 FSW 로 다시 보냄 (체크 켜짐 · 측정 중 아님 · 파일 있음) → True"""
        if not self.v.get("fsw_dfl_auto") or not self.v["fsw_dfl_auto"].get():
            return False
        pth = self.s.get("fsw_dfl_last") or ""
        if not pth:
            return False
        if self.worker is not None and self.worker.is_alive():
            return False
        if not os.path.isfile(pth):
            self.log("⚠ 최근 FSW 설정 파일이 없음: %s (다시 [설정 불러오기…])" % pth)
            return False
        host, port = self.dev_addr("fsw")
        if not host:
            return False
        if time.time() - getattr(self, "_dfl_t", 0) < 3:     # 연달아 누를 때 한 번만
            return True
        self._dfl_t = time.time()
        self.log("%s → 최근 FSW 설정 불러옴: %s" % (why, os.path.basename(pth)))
        self.dfl_send(pth, host, port)
        return True

    def dfl_send(self, path, host, port):
        """.dfl → FSW (프로그램이 기억하던 FSW 연결 · 상태는 버리고 다음 실행 때 새로)"""
        data = open(path, "rb").read()
        eng = self.engine
        self.log("FSW 설정 불러오는 중: %s" % os.path.basename(path))

        def job():
            try:
                if eng is not None and getattr(eng, "fsw", None) is not None:
                    eng.go_local(close=True)              # 프로그램이 기억하던 FSW 상태는 버림 → 다음 실행 때 새로 연결
                try:
                    os.remove(E.I.FSW._keep_path())       # 예전 ACP 원래값 기억도 버림 (불러온 설정을 덮지 않게)
                except Exception:
                    pass
                e = E.I.fsw_state_load(host, data, port)
                ok = (not e) or e.startswith("0") or e == "demo"
                self.q.put(("log", "FSW 설정 불러옴: %s%s" % (os.path.basename(path), "" if ok else "  (FSW: %s)" % e)))
            except Exception as ex:
                self.q.put(("log", "⚠ FSW 설정 불러오기 실패: %s" % ex))
        threading.Thread(target=job, daemon=True).start()

    def fsw_shot(self):
        """FSW 지금 화면 → PNG 저장 (따로 연결해서 측정 중에도)"""
        host, port = self.dev_addr("fsw")
        if not host:
            messagebox.showwarning(APP, "FSW 주소가 없습니다 (장비 설정)")
            return
        lab = ""
        if self.results:
            lab = "_" + re.sub(r"[^0-9A-Za-z가-힣.+-]+", "", str(self.results[-1].get("label", "")))[:20]
        path = self.ask("FSW 화면 저장", ".png", [("PNG", "*.png")], "FSW_%s%s_%s.png" % (self.bw_tag(), lab, self.stamp()))
        if not path:
            return
        self.remember_dir(path)
        self.log("FSW 화면 받는 중…")

        def job():
            try:
                png = E.I.fsw_screenshot(host, port)
                with open(path, "wb") as f:
                    f.write(png)
                self.q.put(("log", "FSW 화면 저장: %s (%.0f kB)" % (path, len(png) / 1024)))
            except Exception as e:
                self.q.put(("log", "⚠ FSW 화면 저장 실패: %s" % e))
        threading.Thread(target=job, daemon=True).start()

    def rf_off(self):
        """SG RF OFF - 언제든 (측정 중이면 중지 = RF OFF · 끝난 뒤 · 중지한 뒤 · 내보내기 뒤). 바이어스 · FSW 화면은 그대로"""
        if self.worker is not None and self.worker.is_alive():
            self.stop()
            return
        eng = self.engine or getattr(self, "_last_eng", None)
        smw = getattr(eng, "smw", None) if eng is not None else None
        host, port = self.dev_addr("smw")

        def job():
            try:
                done = False
                if smw is not None:
                    try:
                        smw.rf(False)
                        done = True
                    except Exception:
                        pass                                   # 끊긴 연결이면 새로 연결해서
                if not done and host and host.lower() != "demo":
                    io = E.I.ScpiSocket(host, port, timeout=5.0, name="SMW")
                    try:
                        io.write("OUTP1 OFF")
                        io.opc()
                        io.write("&GTL")
                    finally:
                        io.close()
                self.rf_state = False
                self.q.put(("log", "■ SG RF OFF (신호 끔 · 바이어스 · FSW 화면은 그대로)"))
            except Exception as e:
                self.q.put(("log", "⚠ SG RF OFF 실패: %s (SMW 주소 · 연결 확인)" % e))
        threading.Thread(target=job, daemon=True).start()

    def pump(self):
        """큐 처리 (로그 · 결과 · 끝). 화면 그리다 오류가 나도 멈추지 않고 로그 · 오류 파일에 남김"""
        try:
            self._pump()
        except Exception as ex:
            self.report_err("화면 갱신", ex)
            self.root.after(100, self.pump)

    def report_err(self, where, ex):
        import traceback
        tb = traceback.format_exc()
        try:
            os.makedirs(SET_DIR, exist_ok=True)
            with open(os.path.join(SET_DIR, "error.log"), "a", encoding="utf-8") as f_:
                f_.write("==== %s %s %s\n%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), VERSION, where, tb))
        except Exception:
            pass
        last = [l_ for l_ in tb.strip().splitlines() if l_.strip().startswith("File ")][-1:]
        try:
            self.log("⚠ %s 오류: %s: %s %s (자세히: %s)" % (where, type(ex).__name__, ex, " ".join(l_.strip() for l_ in last),
                                                       os.path.join(SET_DIR, "error.log")))
        except Exception:
            pass

    def _pump(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "call":
                    try:
                        val()
                    except Exception as e:
                        self.log("⚠ %s" % e)
                    continue
                if kind == "log":
                    self.log(val)
                elif kind == "err":
                    self._had_err = True
                    self.log("✖ " + val)
                    messagebox.showerror(APP, val)
                elif kind == "sig":
                    self.lbl_sig.configure(text=val)
                elif kind == "sglock":                       # 목표 Pout 에 맞춘 SG → 2 카드 SG 레벨 칸에
                    self.v["sg_dbm"].set("%.2f" % val)
                elif kind == "cmp":
                    if val[1] is not None:
                        self.show_compare(*val)
                elif kind == "res":
                    self.add_result(val)
                    self.add_hist_point(val)
                elif kind == "prog":
                    self._last_prog = val
                    v_, sb_, col_ = self.big["aclr"]
                    sb_.configure(text="● " + val, fg="#ffd54f")
                elif kind == "done":
                    if val is not None and val != getattr(self, "run_id", 0):
                        continue                         # 중지한 이전 실행의 끝 알림
                    if val is not None and self.worker is None:
                        continue
                    self.worker = None
                    self.busy(False)
                    self.update_chips()
                    self.take_auto_kml()
                    self.st_last = (("✖ 실패 %s" if getattr(self, "_had_err", False) else "✔ 끝 %s") % time.strftime("%H:%M"),
                                    "#c62828" if getattr(self, "_had_err", False) else "#1565c0")
                    self.save_run_data()
                    if getattr(self, "cmp_mode", False):
                        self.cmp_mode = False
                        self.hist = [r for r in self.load_hist() if r.get("pts") and any(q["kind"] != "off" for q in r["pts"])
                                     or r not in self.cmp_runs.values()]
                        self.save_hist()
                        self.refresh_hist_list()
                    if getattr(self, "cur_run", None) is not None:
                        if not self.cur_run.get("pts"):
                            self.hist = [r for r in self.load_hist() if r is not self.cur_run]
                        self.save_hist()
                        self.refresh_hist_list()
                        self.cur_run = None
                    self.draw_hist()
                    if self.results:
                        last = self.results[-1]
                        offs = [q for q in self.results if q.get("kind") == "off" and q.get("base_sg") == last.get("base_sg")]
                        off = offs[-1] if offs else self.results[0]
                        txt = "✔ 끝 · %s" % last["label"] if last is off else "✔ 끝 · 같은 SG 의 OFF %.1f / %.1f → %.1f dB 개선" % (
                            off["aclr_l"], off["aclr_u"], aw(off) - aw(last))
                        self.big["aclr"][1].configure(text=txt, fg="#90a4ae")
        except queue.Empty:
            pass
        self.root.after(100, self.pump)

    # ------------------------------------------------------------ 결과 표시
    def wi_value(self):
        """가정 Vdd 를 켰으면 그 값 (V), 아니면 None"""
        if not getattr(self, "var_wi_on", None) or not self.var_wi_on.get():
            return None
        try:
            v = float(self.var_wi_vdd.get())
        except ValueError:
            return None
        return v if v > 0 else None

    @staticmethod
    def wi_apply(r, vdd):
        """측정 하나 (r) 의 Vdd · Pdc · DE · PAE 를 vdd 로 다시 (vdd=None = 원래 값). 원래 값은 *_meas 에 보관"""
        keys = ("vdd", "pdc", "de", "pae", "de_iq", "de_spec")
        if "vdd_meas" not in r:
            for k in keys:
                r[k + "_meas"] = r.get(k)
        if vdd is None:
            for k in keys:
                if k + "_meas" in r:
                    r[k] = r[k + "_meas"]
            r.pop("vdd_wi", None)
            return
        idd = r.get("id")
        if not isinstance(idd, (int, float)) or not math.isfinite(idd) or idd <= 0:
            return                                         # 전류 없음 → 그대로
        pdc = vdd * idd
        w = lambda dbm: 10 ** ((dbm - 30) / 10)
        r["vdd"], r["pdc"], r["vdd_wi"] = vdd, pdc, vdd
        po = r.get("pout")
        if isinstance(po, (int, float)) and math.isfinite(po):
            r["de"] = 100 * w(po) / pdc
            pi = r.get("pin")
            if isinstance(pi, (int, float)) and math.isfinite(pi):
                r["pae"] = 100 * (w(po) - w(pi)) / pdc
        for k, pk in (("de_iq", "pout_iq"), ("de_spec", "pout_spec")):
            x = r.get(pk)
            if isinstance(x, (int, float)) and math.isfinite(x) and r.get(k + "_meas") is not None:
                r[k] = 100 * w(x) / pdc

    def wi_recalc(self):
        """[가정 Vdd] 켜기 · 끄기 · 값 바꾸기 → 지금 측정 기록 전부 다시 계산 (표 · 큰 숫자 · 그래프)"""
        if self.var_wi_on.get() and not self.var_wi_vdd.get().strip():
            vs = [r.get("vdd_meas", r.get("vdd")) for r in self.results]
            vs = [v for v in vs if isinstance(v, (int, float)) and math.isfinite(v) and v > 0]
            if vs:
                self.var_wi_vdd.set("%.2f" % vs[-1])         # 처음엔 측정 때 Vdd 로 채움 → 바꿔서 Enter
        v = self.wi_value()
        if self.var_wi_on.get() and v is None:
            self.lbl_wi.configure(text="가정 Vdd 값이 올바르지 않음")
            return
        items = list(self.results)
        for r in items:
            self.wi_apply(r, v)
        if items:
            sel = self.tree.selection()
            self.tree.delete(*self.tree.get_children())
            self.results = []
            for r in items:
                self.add_result(r, wi_done=True)
            if sel and self.tree.exists(sel[0]):
                self.tree.selection_set(sel[0])
        self.lbl_wi.configure(text=("DE · PAE = %.2f V 로 계산 중" % v) if v else "")
        self.redraw_soon() if hasattr(self, "redraw_soon") else None

    def add_result(self, r, wi_done=False):
        if not wi_done:
            v = self.wi_value()
            if v is not None:
                self.wi_apply(r, v)                       # 가정 Vdd 를 켜 둔 채 새 측정 → 그 값으로
        self.results.append(r)
        f = lambda v, n=2: ("%.*f" % (n, v)) if isinstance(v, (int, float)) and v is not None and math.isfinite(v) else "-"
        idm = r.get("id")
        self.tree.insert("", "end", iid=str(len(self.results) - 1), values=(r["label"], f(r["sg"]), f(r["pin"]), f(r["pout"]), f(r.get("rec_pout")), f(r["gain"]),
                                             *aclr_iq_cols(r, f), self.aclr_mod_txt(r, f), f(r["evm"]),
                                             f(idm * 1e3 if isinstance(idm, float) else None, 1), f(r.get("de"), 1), f(r.get("pae"), 1),
                                             f(r["papr"]),
                                             f(r["nmse"], 1) if r.get("nmse") is not None else "-", r["t"]))
        self.tree.see(self.tree.get_children()[-1])
        if self.aclr_mod_txt(r, f) != "-":
            self.fsw_col_show(True)
        self.update_star()
        last = r
        offs = [q for q in self.results if q.get("kind", "off") == "off" and q.get("base_sg") == r.get("base_sg")]
        off = offs[-1] if offs else self.results[0]          # 같은 SG 레벨의 DPD OFF 와 비교
        is_off = last is off

        def tile(k, big, sub):
            if k not in self.big:                 # 메인에서 뺀 칸 (EVM)
                return
            v, sb, col = self.big[k]
            v.configure(text=big)
            if not (k == "aclr" and self.worker):
                sb.configure(text=sub, fg="#90a4ae")
        ain = last.get("aclr_in") or []
        tile("aclr", "%.1f / %.1f" % (last["aclr_l"], last["aclr_u"]),
             ("FSW · " if last.get("aclr_src") == "fsw" else "I/Q · ") +
             ("안쪽 %s · " % " / ".join("%.1f" % v_ for v_ in ain) if ain else "") +
             ("DPD OFF (dBc)" if is_off else "%s · OFF %.1f / %.1f → %.1f dB 개선" % (
                 last["label"], off["aclr_l"], off["aclr_u"], aw(off) - aw(last)))
             + ("" if last.get("aclr_full", True) else "  ⚠ 일부 대역"))
        tile("evm", "%.2f %%" % last["evm"], ("%s EVM" % ("파형" if last.get("evm_kind") == "파형" else "OFDM"))
             + ("" if is_off else " · OFF %.2f %%" % off["evm"]))
        rp = last.get("rec_pout")
        tile("pout", "%.2f dBm" % last["pout"], "Gain %.2f dB · Pin %.2f dBm%s" % (
            last["gain"], last["pin"], (" · 기록 %.2f" % rp) if isinstance(rp, float) and math.isfinite(rp) else ""))
        de, pae = last.get("de"), last.get("pae")
        ok_ = isinstance(pae, float) and math.isfinite(pae)
        tile("eff", ("%.1f %%" % pae) if ok_ else "---",
             ("DE %.1f %%%s · Id %.3f A%s" % (de, "" if is_off or "papr" not in self.big or not isinstance(off.get("pae"), float) or not math.isfinite(off["pae"])
                                              else " · OFF %.1f %%" % off["pae"], last["id"],
                                              "" if "papr" in self.big else " · PAPR %.2f dB" % last["papr"])) if ok_ else
             "Vdd 전류 없음 - PA Bench Monitor 에서 파워서플라이 연결")
        tile("papr", "%.2f dB" % last["papr"], "SG %.2f dBm%s" % (last["sg"], "" if is_off else " · OFF %.2f dB" % off["papr"]))
        self.root.after_idle(self.fit_big)
        self.lbl_last.configure(text="%s · %s" % (last["label"], last["t"]))
        if isinstance(last.get("cal_db"), float):
            self.v["cal_db"].set("%.2f" % last["cal_db"])
        self.draw()

    # ------------------------------------------------------------ 그래프 새 창 (다른 모니터에서 크게)
    REC_COLORS = ["#1e88e5", "#e53935", "#43a047", "#fb8c00", "#8e24aa", "#00acc1", "#6d4c41", "#d81b60",
                  "#7cb342", "#3949ab", "#f4511e", "#00897b", "#c0ca33", "#5e35b1"]

    GLAYS = (("four", "4 개"), ("spec", "스펙트럼 크게"), ("hist", "ACLR vs Pout"))

    def open_graph_window(self, lay=None):
        """그래프 새 창 (다른 모니터에서 크게): 위 = 큰 숫자 (메인과 같이) · 화면 = [4 개 | 스펙트럼 크게 | ACLR vs Pout]
        [지금 측정] = 메인 그래프와 같이 · [기록 보기] = 지난 기록의 측정을 골라 겹쳐 보기 (왼쪽 목록은 이때만)"""
        w = getattr(self, "gwin", None)
        if w is not None:
            try:
                w.deiconify()
                w.lift()
                if lay:
                    self.v_glay.set(lay)
                    self.gwin_layout()
                return
            except tk.TclError:
                self.gwin = None
        w = tk.Toplevel(self.root)
        w.title("%s - 그래프" % APP)
        self.reg(w, bg="bg")
        w.geometry(self.s.get("gwin_geo", "1500x920"))
        # 위: 큰 숫자 (메인 화면 큰 숫자 칸을 그대로 따라감)
        dash = tk.Frame(w, bg=DASH)
        dash.pack(fill="x")
        dc = tk.Frame(dash, bg=DASH)
        dc.pack(fill="x", padx=8, pady=6)
        self.g_big = {}
        for i_, (key, title, color) in enumerate((("aclr", "ACLR  L / U", C_ACLR), ("pout", "Pout · DUT", C_POUT),
                                                  ("eff", "PAE (효율)", C_EFF), ("papr", "PAPR · DPD 신호", C_PAPR))):
            dc.columnconfigure(i_, weight={"aclr": 13, "papr": 8}.get(key, 10), uniform="g")
            box = tk.Frame(dc, bg=BG, highlightthickness=1, highlightbackground="#2b3540")
            box.grid(row=0, column=i_, sticky="nsew", padx=4)
            tk.Label(box, text=title, bg=BG, fg="#b0bec5", font=("맑은 고딕", 11, "bold")).pack(anchor="w", padx=12, pady=(4, 0))
            v = tk.Label(box, text="---", bg=BG, fg=color, font=("Consolas", 30, "bold"), anchor="w", width=1)
            v.pack(fill="x", padx=10)
            sub = tk.Label(box, text="", bg=BG, fg="#90a4ae", font=("Consolas", 10), anchor="w")
            sub.pack(fill="x", padx=12, pady=(0, 5))
            self.g_big[key] = (v, sub)
        # 막대: 화면 · 보기 · 스펙트럼 · 세로축 · 전체 화면
        bar = ttk.Frame(w)
        bar.pack(fill="x", padx=8, pady=(6, 0))
        self.v_glay = tk.StringVar(value=lay or self.s.get("gwin_lay", "four"))
        ttk.Label(bar, text="화면").pack(side="left", padx=(0, 6))
        for val, txt in self.GLAYS:
            ttk.Radiobutton(bar, text=txt, value=val, variable=self.v_glay, style="Seg.Toolbutton",
                            command=self.gwin_layout).pack(side="left")
        self.v_gmode = tk.StringVar(value="live")
        gm = ttk.Frame(bar)
        ttk.Label(gm, text="   보기").pack(side="left", padx=(0, 6))
        for val, txt in (("live", "지금 측정"), ("rec", "기록 보기 (겹쳐서)")):
            ttk.Radiobutton(gm, text=txt, value=val, variable=self.v_gmode, style="Seg.Toolbutton",
                            command=self.gwin_mode).pack(side="left")
        ttk.Label(gm, text="   스펙트럼").pack(side="left", padx=(0, 6))
        for val, txt in (("aclr", "ACLR (Mod. 화면)"), ("iq", "I/Q"), ("both", "둘 다")):
            ttk.Radiobutton(gm, text=txt, value=val, variable=self.v["spec_view"], style="Seg.Toolbutton",
                            command=self.gwin_redraw).pack(side="left")
        hm = ttk.Frame(bar)
        ttk.Label(hm, text="   세로축").pack(side="left", padx=(0, 6))
        for val, txt in (("aclr", "ACLR (나쁜 쪽)"), ("aclr_l", "ACLR L"), ("aclr_u", "ACLR U"), ("evm", "EVM"),
                         ("de", "DE"), ("pae", "PAE")):
            ttk.Radiobutton(hm, text=txt, value=val, variable=self.v["y_q"], style="Seg.Toolbutton",
                            command=self.draw_hist).pack(side="left")
        self.g_bar_spec, self.g_bar_hist = gm, hm
        self.btn_full = ttk.Button(bar, text="▣ 전체 화면 (F11)", command=self.gwin_full)
        self.btn_full.pack(side="right")
        ttk.Label(bar, text="V = 마커 · 더블클릭 = 세로축", foreground="#78909c").pack(side="right", padx=8)
        w.bind("<F11>", lambda e: self.gwin_full())
        # 실행 줄: 이 창에서 바로 돌리고 · 이어서 · 멈추고 · FSW 화면 저장 (메인 5 카드와 같은 버튼)
        rb = ttk.Frame(w)
        rb.pack(fill="x", padx=8, pady=(6, 0))
        self.g_btn = {}
        for key, txt, cmd, sty in (("go", self.btn_go.cget("text"), lambda: self.start(True), "Pri.TButton"),
                                   ("ilc", "한계 ACLR (ILC)", lambda: self.start(True, ilc=True), None),
                                   ("off", "DPD OFF 측정만", lambda: self.start(False), None)):
            b_ = ttk.Button(rb, text=txt, command=cmd, **({"style": sty} if sty else {}))
            b_.pack(side="left", padx=(0, 4))
            self.g_btn[key] = b_
        self.g_btn["more"] = ttk.Button(rb, text="＋ 이어서", command=lambda: self.start(True, more=max(1, int(fnum(self.v["more_n"].get(), 5)))))
        self.g_btn["more"].pack(side="left", padx=(8, 2))
        ttk.Entry(rb, textvariable=self.v["more_n"], width=4, justify="right").pack(side="left")
        ttk.Label(rb, text="번").pack(side="left", padx=(2, 8))
        self.g_btn["stop"] = tk.Button(rb, text="■ 중지 (바이어스 그대로)", command=self.stop, bg="#b0bec5", fg="white",
                                       activebackground="#b71c1c", activeforeground="white", disabledforeground="#e0e0e0",
                                       relief="flat", bd=0, font=("맑은 고딕", 10, "bold"), padx=10, pady=3, state="disabled", cursor="hand2")
        self.g_btn["stop"].pack(side="left", padx=(0, 8))
        self.g_btn["rf"] = self.state_btn(rb, self.rf_toggle)
        self.g_btn["rf"].pack(side="right")
        self.g_btn["conn"] = self.state_btn(rb, self.conn_toggle)
        self.g_btn["conn"].pack(side="right", padx=(0, 4))
        ttk.Button(rb, text="FSW 설정 불러오기…", command=self.fsw_state_load).pack(side="right", padx=(0, 4))
        ttk.Button(rb, text="FSW 설정 저장…", command=self.fsw_state_save).pack(side="right", padx=(0, 4))
        ttk.Button(rb, text="FSW 화면 저장…", command=self.fsw_shot).pack(side="right", padx=(0, 4))
        self.g_prog = ttk.Label(rb, text="", foreground="#78909c")
        self.g_prog.pack(side="left", padx=6)
        pan = ttk.PanedWindow(w, orient="horizontal")
        pan.pack(fill="both", expand=True, padx=8, pady=8)
        # 왼쪽: 기록 · 측정 목록 (기록 보기 때만)
        side = ttk.Frame(pan, width=340)
        ttk.Label(side, text="기록 (Ctrl · Shift 로 여러 개)", font=FB).pack(anchor="w")
        self.g_runs = ttk.Treeview(side, columns=("name", "ghz", "when"), show="headings", height=12, selectmode="extended")
        for c_, h, wd in (("name", "이름", 170), ("ghz", "GHz", 50), ("when", "날짜", 110)):
            self.g_runs.heading(c_, text=h)
            self.g_runs.column(c_, width=wd, anchor="w" if c_ == "name" else "center")
        self.g_runs.pack(fill="both", expand=True)
        self.g_runs.bind("<<TreeviewSelect>>", lambda e: self.gwin_fill_meas())
        ttk.Label(side, text="측정 (고른 것만 그래프에 · 색 = 그래프 색 · ★ = 기록별 최고)", font=FB).pack(anchor="w", pady=(8, 0))
        self.g_meas = ttk.Treeview(side, columns=("item", "aclr", "pout"), show="headings", height=14, selectmode="extended")
        for c_, h, wd in (("item", "측정", 200), ("aclr", "ACLR", 60), ("pout", "Pout", 60)):
            self.g_meas.heading(c_, text=h)
            self.g_meas.column(c_, width=wd, anchor="w" if c_ == "item" else "center")
        self.g_meas.pack(fill="both", expand=True)
        self.g_meas.bind("<<TreeviewSelect>>", lambda e: self.gwin_redraw())
        bf = ttk.Frame(side)
        bf.pack(fill="x", pady=(4, 0))
        ttk.Button(bf, text="OFF · ★ 만", command=lambda: self.gwin_pick("best")).pack(side="left")
        ttk.Button(bf, text="전부", command=lambda: self.gwin_pick("all")).pack(side="left", padx=4)
        ttk.Button(bf, text="새로 읽기", command=self.gwin_fill_runs).pack(side="right")
        self.g_side, self.g_pan = side, pan
        # 오른쪽: 그래프 (화면 모양에 따라 다시 배치) + 밑 표
        right = ttk.Frame(pan)
        pan.add(right, weight=1)
        g = ttk.Frame(right)
        g.pack(fill="both", expand=True)
        pw = {}
        for k, src, key in (("spec", self.p_spec, "yr_spec"), ("trend", self.p_trend, "yr_trend"),
                            ("amam", self.p_amam, "yr_amam"), ("ampm", self.p_ampm, "yr_ampm"),
                            ("hist", self.p_hist, "yr_hist_" + self.v["y_q"].get())):
            pl = Plot(g, src.title, src.xl, src.yl)
            pl.remember(self.s, key)
            pw[k] = pl
        pw["spec"].enable_marks(self.s, "spec_marks", "MHz")
        pw["spec"].peer, self.p_spec.peer = self.p_spec, pw["spec"]
        # ACLR vs Pout 화면 오른쪽: 기록 목록 (메인 '기록' 과 같은 것 · 표시 칸 클릭 = 켜기/끄기)
        hs = self.card(g, "기록", None, "")
        self.g_htree = ttk.Treeview(hs.body, columns=("on", "name", "ghz", "n", "when"), show="headings", height=16, selectmode="extended")
        for c_, h, w_ in (("on", "표시", 40), ("name", "이름", 170), ("ghz", "GHz", 50), ("n", "점", 34), ("when", "날짜", 100)):
            self.g_htree.heading(c_, text=h)
            self.g_htree.column(c_, width=w_, anchor="center" if c_ != "name" else "w", stretch=c_ == "name")
        self.g_htree.pack(fill="both", expand=True)
        self.g_htree.bind("<Button-1>", self.hist_click)
        hb2 = ttk.Frame(hs.body, style="Card.TFrame")
        hb2.pack(fill="x", pady=(6, 0))
        ttk.Button(hb2, text="전체 표시", command=lambda: self.hist_show(True, self.g_htree)).pack(side="left", padx=(0, 3))
        ttk.Button(hb2, text="숨김", command=lambda: self.hist_show(False, self.g_htree)).pack(side="left")
        ttk.Label(hs.body, text="표시 칸 클릭 = 켜기/끄기", style="Hint.TLabel").pack(anchor="w", pady=(4, 0))
        pw_side = hs
        # 장비 화면처럼: 그래프 밑에 값 표 (지금 측정 = DPD OFF · ★ · 지금 / 기록 보기 = 고른 측정마다)
        tf = ttk.Frame(right)
        tf.pack(fill="x", pady=(6, 0))
        cols = (("row", "", 70), ("item", "측정", 150), ("it", "반복", 55), ("pout", "Pout dBm", 85), ("gain", "Gain dB", 75),
                ("aclr", "I/Q ACLR L / U", 130), ("aclr_in", "FSW ACLR L / U", 150), ("pae", "PAE %", 70), ("de", "DE %", 70),
                ("papr", "PAPR dB", 75), ("evm", "EVM %", 70), ("sg", "SG dBm", 75), ("att", "Ref / 감쇠", 110))
        try:
            ttk.Style().configure("GTab.Treeview", font=("맑은 고딕", 12), rowheight=30)
            ttk.Style().configure("GTab.Treeview.Heading", font=("맑은 고딕", 10, "bold"))
        except tk.TclError:
            pass
        self.g_tab = ttk.Treeview(tf, columns=[c[0] for c in cols], show="headings", height=3, style="GTab.Treeview")
        for c_, h, wd in cols:
            self.g_tab.heading(c_, text=h)
            self.g_tab.column(c_, width=wd, anchor="center")
        self.g_tab.tag_configure("now", foreground="#1e88e5")
        self.g_tab.tag_configure("best", foreground="#e65100")
        self.g_tab.pack(fill="x")
        self.g_tabf = tf
        self.gwin, self.gplots, self.g_grid, self.g_hside = w, pw, g, pw_side

        def close():
            try:
                self.s["gwin_geo"] = w.geometry()
                self.s["gwin_lay"] = self.v_glay.get()
            except tk.TclError:
                pass
            self.p_spec.peer = None
            self.gwin, self.gplots = None, None
            self.g_tab = None
            self.g_htree = None
            w.destroy()
        w.protocol("WM_DELETE_WINDOW", close)
        self.busy(bool(self.worker))
        self.gwin_layout()
        self.gwin_fill_runs()
        self.refresh_hist_list()
        self.draw_hist()
        self.sync_graph_window()
        self.gwin_tick()

    def gwin_layout(self):
        """화면 모양: 4 개 (스펙트럼 · ACLR 순서 · AM-AM · AM-PM) / 스펙트럼 크게 (위 크게 + 밑 3 개) / ACLR vs Pout (+ 기록 목록)"""
        pw, g = getattr(self, "gplots", None), getattr(self, "g_grid", None)
        if not pw or g is None:
            return
        lay = self.v_glay.get()
        self.s["gwin_lay"] = lay
        for pl in list(pw.values()) + [self.g_hside]:
            pl.grid_forget()
        for i_ in range(3):
            g.columnconfigure(i_, weight=0, uniform="")
            g.rowconfigure(i_, weight=0)
        px = dict(sticky="nsew")
        if lay == "spec":
            for i_ in range(3):
                g.columnconfigure(i_, weight=1, uniform="s")
            g.rowconfigure(0, weight=3)
            g.rowconfigure(1, weight=1)
            pw["spec"].grid(row=0, column=0, columnspan=3, pady=(0, 6), **px)
            pw["trend"].grid(row=1, column=0, padx=(0, 6), **px)
            pw["amam"].grid(row=1, column=1, padx=(0, 6), **px)
            pw["ampm"].grid(row=1, column=2, **px)
        elif lay == "hist":
            g.columnconfigure(0, weight=1)
            g.rowconfigure(0, weight=1)
            pw["hist"].grid(row=0, column=0, padx=(0, 6), **px)
            self.g_hside.grid(row=0, column=1, sticky="ns")
        else:
            g.columnconfigure(0, weight=3)
            g.columnconfigure(1, weight=2)
            g.rowconfigure(0, weight=3)
            g.rowconfigure(1, weight=2)
            pw["spec"].grid(row=0, column=0, padx=(0, 6), pady=(0, 6), **px)
            pw["trend"].grid(row=0, column=1, pady=(0, 6), **px)
            pw["amam"].grid(row=1, column=0, padx=(0, 6), **px)
            pw["ampm"].grid(row=1, column=1, **px)
        # 막대: ACLR vs Pout 이면 세로축 · 아니면 보기 · 스펙트럼
        if lay == "hist":
            self.g_bar_spec.pack_forget()
            self.g_bar_hist.pack(side="left")
            self.g_tabf.pack_forget()
        else:
            self.g_bar_hist.pack_forget()
            self.g_bar_spec.pack(side="left")
            if not self.g_tabf.winfo_ismapped():
                self.g_tabf.pack(fill="x", pady=(6, 0))
        self.gwin_side()

    def gwin_side(self):
        """왼쪽 기록 · 측정 목록은 [기록 보기] 일 때만 (지금 측정 · ACLR vs Pout 화면에선 그래프를 넓게)"""
        pan, side = getattr(self, "g_pan", None), getattr(self, "g_side", None)
        if pan is None:
            return
        want = self.v_gmode.get() == "rec" and self.v_glay.get() != "hist"
        has = str(side) in pan.panes()
        try:
            if want and not has:
                pan.insert(0, side, weight=0)
            elif not want and has:
                pan.forget(side)
        except tk.TclError:
            pass

    def gwin_full(self):
        w = getattr(self, "gwin", None)
        if w is None:
            return
        on = not bool(w.attributes("-fullscreen"))
        w.attributes("-fullscreen", on)
        self.btn_full.configure(text="▣ 전체 화면 끄기 (F11)" if on else "▣ 전체 화면 (F11)")

    def gwin_tick(self):
        """새 창 위 큰 숫자 = 메인 큰 숫자 칸 그대로 (0.4 초마다)"""
        if getattr(self, "gwin", None) is None:
            return
        try:
            for k, (v, sub) in self.g_big.items():
                if k in self.big:
                    mv, ms, _c = self.big[k]
                    if v.cget("text") != mv.cget("text") or v.cget("fg") != mv.cget("fg"):
                        v.configure(text=mv.cget("text"), fg=mv.cget("fg"))
                    if sub.cget("text") != ms.cget("text") or sub.cget("fg") != ms.cget("fg"):
                        sub.configure(text=ms.cget("text"), fg=ms.cget("fg"))
            gb = getattr(self, "g_btn", None)
            if gb and gb["go"].cget("text") != self.btn_go.cget("text"):
                gb["go"].configure(text=self.btn_go.cget("text"))      # 한 레벨 · 파워 스윕 글자 따라감
            pt = self.lbl_last.cget("text") if hasattr(self, "lbl_last") else ""
            if self.g_prog.cget("text") != pt:
                self.g_prog.configure(text=pt)
            self.gwin.after(400, self.gwin_tick)
        except tk.TclError:
            pass

    def redraw_soon(self):
        if not self.results:
            return
        if getattr(self, "_rd_job", None):
            try:
                self.root.after_cancel(self._rd_job)
            except tk.TclError:
                pass
        self._rd_job = self.root.after(300, lambda: (setattr(self, "_rd_job", None), self.draw()))

    def clear_view(self):
        """그래프 4 개 · 큰 숫자 칸 (ACLR · Pout · 효율 …) · 그래프 새 창 표 비우기"""
        for pl in (getattr(self, "p_spec", None), getattr(self, "p_trend", None),
                   getattr(self, "p_amam", None), getattr(self, "p_ampm", None)):
            if pl is not None:
                try:
                    pl.set([])
                except tk.TclError:
                    pass
        for k, (v_, sb_, _col) in list(getattr(self, "big", {}).items()):
            try:
                v_.configure(text="---")
                sb_.configure(text="", fg="#90a4ae")
            except tk.TclError:
                pass
        try:
            self.sync_graph_window()
        except Exception:
            pass

    def gwin_table(self):
        """그래프 새 창 밑 표: DPD OFF · ★ 가장 좋은 것 · 지금 (ACLR · Gain · Pout · PAE · PAPR · 반복)"""
        t = getattr(self, "g_tab", None)
        if t is None:
            return
        rs = self.results
        t.delete(*t.get_children())
        t.configure(height=3)
        t.column("item", width=150)
        t.column("it", width=55)
        if not rs:
            return
        f1 = self._f1
        offs = [r for r in rs if r.get("kind") == "off"]
        cand = [(aw(r), i) for i, r in enumerate(rs) if r.get("kind") != "off" and aw(r) == aw(r)]
        best = rs[min(cand)[1]] if cand else None
        rows = []
        if offs:
            rows.append(("DPD OFF", offs[-1], ""))
        if best is not None:
            rows.append(("★ 최고", best, "best"))
        rows.append(("지금", rs[-1], "now"))
        for nm, r, tag in rows:
            t.insert("", "end", tags=(tag,) if tag else (), values=self._gtab_vals(nm, r.get("label", ""), r))

    @staticmethod
    def _f1(v, n=1):
        return ("%.*f" % (n, v)) if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v else "-"

    def _gtab_vals(self, nm, item, r):
        f1 = self._f1
        iq_l, iq_u = r.get("aclr_iq_l", r.get("aclr_l")), r.get("aclr_iq_u", r.get("aclr_u"))
        return (nm, item, r.get("it", ""), f1(r.get("pout"), 2), f1(r.get("gain"), 2),
                "%s / %s" % (f1(iq_l), f1(iq_u)), self.aclr_mod_txt(r, lambda v_, n_=1: f1(v_, n_)),
                f1(r.get("pae")), f1(r.get("de")), f1(r.get("papr"), 2), f1(r.get("evm"), 2), f1(r.get("sg"), 2),
                "%s / %s dB" % (f1(r.get("fsw_ref")), f1(r.get("fsw_att"), 0)))

    def gwin_table_rec(self, items, names, bests):
        """기록 보기: 고른 측정마다 한 줄 (색 = 그래프 색 · ★ = 그 기록에서 가장 좋은 측정) → 기록끼리 숫자 비교"""
        t = getattr(self, "g_tab", None)
        if t is None:
            return
        t.delete(*t.get_children())
        t.configure(height=max(3, min(len(items), 8)))
        t.column("item", width=250)
        t.column("it", width=0, minwidth=0, stretch=False)       # 기록 보기: 반복 칸 대신 측정 이름을 넓게
        for k, (rid, i, q) in enumerate(items):
            col = self.REC_COLORS[k % len(self.REC_COLORS)]
            tg = "rc%d" % (k % len(self.REC_COLORS))
            t.tag_configure(tg, foreground=col)
            star = "★ " if bests.get(rid) == i else ""
            nm = "● %s%s" % (star, "OFF" if q.get("kind") == "off" else "")
            t.insert("", "end", tags=(tg,), values=self._gtab_vals(nm.strip(), "%s · %s" % (names.get(rid, rid)[:16], q["label"]), q))

    def gwin_mode(self):
        if self.v_gmode.get() == "rec":
            self.gwin_fill_runs()
        self.gwin_side()
        self.gwin_redraw()

    def gwin_redraw(self):
        if self.results:
            self.draw()                                    # 메인 그래프 (지금 측정) - 새 창이 '지금 측정' 이면 같이
        if getattr(self, "gplots", None) and getattr(self, "v_gmode", None) is not None and self.v_gmode.get() == "rec":
            self.draw_records()

    def gwin_fill_runs(self):
        t = getattr(self, "g_runs", None)
        if t is None:
            return
        keep = set(t.selection())
        t.delete(*t.get_children())
        for r in reversed(self.load_hist()):
            if os.path.isfile(self.run_file(r["id"])):
                t.insert("", "end", iid=r["id"], values=(r["name"], "%.4g" % fnum(r.get("freq"), 0), r.get("when", "")))
        sel = [i for i in keep if t.exists(i)]
        if sel:
            t.selection_set(sel)

    @staticmethod
    def run_best(data):
        """기록 안에서 가장 좋은 측정 (DPD OFF 빼고 ACLR 나쁜 쪽이 가장 낮은 것) 번호 · 없으면 None"""
        ons = [(aw(q), i) for i, q in enumerate(data) if q.get("kind") != "off" and aw(q) == aw(q)]
        return min(ons)[1] if ons else None

    def gwin_fill_meas(self):
        t = self.g_meas
        t.delete(*t.get_children())
        t.tag_configure("best", foreground="#e65100")
        names = {r["id"]: r["name"] for r in self.load_hist()}
        for rid in self.g_runs.selection():
            data = self.load_run_data(rid) or []
            bi = self.run_best(data)
            for i, q in enumerate(data):
                wv = (aw(q) if aw(q) == aw(q) else -999)
                t.insert("", "end", iid="%s|%d" % (rid, i), tags=("best",) if i == bi else (),
                         values=("%s%s · %s" % ("★ " if i == bi else "", names.get(rid, rid), q["label"]),
                                                                     "%.1f" % wv if wv > -900 else "-",
                                                                     "%.2f" % q["pout"] if q.get("pout") is not None else "-"))
        self.gwin_pick("best")
        if self.v_gmode.get() != "rec":
            self.v_gmode.set("rec")
        self.draw_records()

    def gwin_pick(self, how):
        t = self.g_meas
        items = t.get_children()
        if how == "all":
            t.selection_set(items)
            return
        sel, by = [], {}
        for iid in items:
            rid, i = iid.split("|")
            by.setdefault(rid, []).append(int(i))
        for rid, idx in by.items():
            data = self.load_run_data(rid) or []
            offs = [i for i in idx if data[i].get("kind") == "off"]
            ons = [i for i in idx if data[i].get("kind") != "off"]
            if offs:
                sel.append("%s|%d" % (rid, offs[0]))
            if ons:
                b = min(ons, key=lambda i: (aw(data[i]) if aw(data[i]) == aw(data[i]) else 0))
                sel.append("%s|%d" % (rid, b))
        if sel:
            t.selection_set(sel)
        else:
            self.draw_records()

    def draw_records(self):
        """기록 보기: 고른 측정들을 색을 달리해 겹쳐 그림 (스펙트럼 · AM-AM · AM-PM) + 고른 기록마다 측정 순서별 ACLR"""
        pw = getattr(self, "gplots", None)
        if not pw:
            return
        mode = self.v["spec_view"].get()
        names = {r["id"]: r["name"] for r in self.load_hist()}
        items, items_t, bests = [], [], {}
        for iid in self.g_meas.get_children():            # 목록 순서대로 (색 = 그래프 · 표 같은 순서)
            if iid not in self.g_meas.selection():
                continue
            rid, i = iid.split("|")
            data = self.load_run_data(rid) or []
            if rid not in bests:
                bests[rid] = self.run_best(data)
            if int(i) < len(data):
                items.append((rid, data[int(i)]))
                items_t.append((rid, int(i), data[int(i)]))
        spec, amam, ampm, bub = [], [], [], []
        fmax, ad_, cb_, cmax_ = 150.0, 100.0, 49.14, 0.0
        for k, (rid, q) in enumerate(items):
            col = self.REC_COLORS[k % len(self.REC_COLORS)]
            nm = "%s · %s" % (names.get(rid, rid)[:18], q["label"])
            use_mod = mode in ("aclr", "both") and q.get("mod_f")
            fx = q.get("mod_f") if use_mod else (q.get("iq_f") or q.get("spec_f"))
            fy = q.get("mod_db") if use_mod else (q.get("iq_db") or q.get("spec_db"))
            if fx:
                spec.append(dict(x=fx, y=[v if v is not None else float("nan") for v in fy], color=col,
                                 width=2 if q.get("kind") != "off" else 1, dash=(4, 3) if q.get("kind") == "off" else None,
                                 label=nm + (" (ACLR)" if use_mod else " (I/Q)")))
                if mode == "both" and use_mod and q.get("iq_f"):
                    spec.append(dict(x=q["iq_f"], y=[v if v is not None else float("nan") for v in q["iq_db"]], color=col, width=1,
                                     dash=(2, 4)))
                fmax = max(fmax, min(abs(fx[0]), abs(fx[-1])))
            ad_, cb_ = q.get("adj") or ad_, (q.get("ch_bw") or 98.28) / 2
            cmax_ = max([cmax_] + [abs(c_) for c_ in (q.get("cars") or [0.0])])
            if q.get("am_x"):
                amam.append(dict(x=q["am_x"], y=q["am_g"], color=col, dots=True, label=nm))
                ampm.append(dict(x=q["am_x"], y=q["am_p"], color=col, dots=True, label=nm))
        fmax = min(fmax, cmax_ + ad_ + cb_ * 2 + 20)            # 반송파 여러 개면 바깥 반송파 인접 채널까지
        if cmax_ > 0:
            vl = []
            for c_ in (-cmax_, cmax_):
                vl += [(c_ - cb_, "#90caf9"), (c_ + cb_, "#90caf9")]
            vl += [(-cmax_ - ad_ - cb_, "#ef9a9a"), (cmax_ + ad_ + cb_, "#ef9a9a")]
        else:
            vl = [(-ad_ - cb_, "#ef9a9a"), (-cb_, "#90caf9"), (cb_, "#90caf9"), (ad_ + cb_, "#ef9a9a")]
        pw["spec"].title = "기록 스펙트럼 (%s · 채널 최고 = 0 dB · 점선 = OFF)" % {"aclr": "ACLR", "iq": "I/Q", "both": "ACLR + I/Q"}[mode]
        pw["spec"].set(spec, xr=(-fmax, fmax), yr=(-90, 5), vlines=vl)
        tr = []
        for k, rid in enumerate(self.g_runs.selection()):
            data = self.load_run_data(rid) or []
            if not data:
                continue
            col = self.REC_COLORS[k % len(self.REC_COLORS)]
            xs = list(range(len(data)))
            ys = [aw(q) for q in data]
            tr.append(dict(x=xs, y=ys, color=col, width=2, label=names.get(rid, rid)[:20]))
            tr.append(dict(x=xs, y=ys, color=col, dots=True, r=3))
        pw["trend"].title = "기록별 측정 순서 ACLR (나쁜 쪽)"
        pw["trend"].set(tr)
        g0 = 0.0
        pw["amam"].title = "기록 AM-AM"
        pw["amam"].set(amam, xr=(-30, 1), yr=(g0 - 4, g0 + 3))
        pw["ampm"].title = "기록 AM-PM"
        pw["ampm"].set(ampm, xr=(-30, 1), yr=(-20, 20))
        self.gwin_table_rec(items_t, names, bests)

    def sync_graph_window(self):
        pw = getattr(self, "gplots", None)
        if not pw:
            return
        if getattr(self, "v_gmode", None) is not None and self.v_gmode.get() == "rec":
            return                                           # 기록 보기 중엔 지금 측정으로 덮지 않음
        try:
            for k, src in (("spec", self.p_spec), ("trend", self.p_trend), ("amam", self.p_amam), ("ampm", self.p_ampm)):
                dst = pw[k]
                dst.title = src.title
                dst.set(src.series, src.xr, src.yr, src.vlines, getattr(src, "bubbles", []), getattr(src, "spans", []))
            self.gwin_table()
        except tk.TclError:
            self.gplots = None

    def draw(self):
        rs_all = self.results
        if not rs_all:
            return
        mode = self.v["spec_view"].get() if "spec_view" in self.v else "both"
        pi = self.tree.selection()
        hi = rs_all[int(pi[0])] if pi and pi[0].isdigit() and int(pi[0]) < len(rs_all) else rs_all[-1]   # 굵게 볼 측정

        def iqc(r):
            return (r.get("iq_f") or r["spec_f"]), (r.get("iq_db") or r["spec_db"])

        def modc(r):
            if r.get("mod_f"):
                return r["mod_f"], r["mod_db"]
            if r.get("aclr_src") == "fsw" and not r.get("iq_f"):       # 예전 결과
                return r["spec_f"], r["spec_db"]
            return None
        ms = [r for r in rs_all if modc(r)]
        use_mod = mode in ("aclr", "both") and bool(ms)
        main = modc if use_mod else iqc                      # 말풍선 · 마커 값은 이 곡선
        rs = ms if use_mod else rs_all
        off = next((r for r in rs if r.get("kind") == "off"), rs[0])
        last = hi if (not use_mod or modc(hi)) else rs[-1]
        tag = " (ACLR)" if use_mod else (" (I/Q)" if mode == "iq" or not ms else " (I/Q · ACLR 없음)")
        ser = []
        mid = [r for r in rs if r is not off and r is not last]
        for i, r in enumerate(mid[-8:]):                   # 중간 반복 (옅게, 최근 8 개)
            x_, y_ = main(r)
            ser.append(dict(x=x_, y=y_, color=C_ITER[min(i, len(C_ITER) - 1)]))
        if mode == "both" and use_mod:                       # 둘 다: I/Q 곡선 (점선) 도 같이
            for r, col, nm in ((off, "#bdbdbd", "OFF (I/Q)"), (last, "#64b5f6", "%s (I/Q)" % last["label"])):
                if r is off and last is off and nm.startswith("%s" % last["label"]):
                    continue
                x_, y_ = iqc(r)
                ser.append(dict(x=x_, y=y_, color=col, width=1, dash=(4, 3), label=nm))
        x_, y_ = main(off)
        ser.append(dict(x=x_, y=y_, color=C_OFF, width=2, label="DPD OFF" + tag))
        if last is not off:
            x_, y_ = main(last)
            ser.append(dict(x=x_, y=y_, color=C_DPD, width=2, label=last["label"] + tag))
        self.p_spec.title = "스펙트럼%s (채널 최고 = 0 dB)" % tag
        fo, _ = main(off)
        fmax = max(abs(fo[0]), abs(fo[-1]))
        cb_, ad_ = off.get("ch_bw", 98.28) / 2, off.get("adj", 100.0)
        cars_ = off.get("cars") or [0.0]                      # 반송파 중심 (MHz) - 1 개면 [0]
        multi = len(cars_) > 1 and bool(off.get("aclr_pos"))
        fmax = min(fmax, max(abs(c_) for c_ in cars_) + ad_ + cb_ * 2 + 20)   # 인접 채널 조금 바깥까지만

        def at(r, f0):                                        # 그 주파수의 스펙트럼 높이 (±2 MHz 평균, 마커 위치)
            xs_, ys_ = main(r)
            v = [y for x, y in zip(xs_, ys_) if abs(x - f0) <= 2.0 and math.isfinite(y)]
            return sum(v) / len(v) if v else float("nan")
        # 세로축: 위 +5 dB, 아래 = DPD ON 인접 채널 바닥보다 15 dB 아래 (10 dB 단위, -40 ~ -90)
        fl = [at(r, f0) for r in (off, last) for f0 in ([q_[1] for q_ in off["aclr_pos"]] if multi else (-ad_, ad_))]
        fl = [v for v in fl if math.isfinite(v)]
        lo = max(-90.0, min(-40.0, math.floor((min(fl) - 15) / 10) * 10)) if fl else -90.0
        def aval(r, key):
            """말풍선 ACLR 글: I/Q 보기 = I/Q 로 계산한 값 · ACLR 보기 = FSW Mod. 화면 값 · 둘 다 = 둘 다"""
            iqv = r.get("aclr_iq_" + key[-1])
            modv = r[key] if r.get("aclr_src") == "fsw" else None
            if mode == "iq" or modv is None:
                return "%.1f dBc%s" % (iqv if iqv is not None else r[key], " (I/Q)" if modv is not None or mode == "both" else "")
            if mode == "both" and iqv is not None:
                return "%.1f (I/Q %.1f) dBc" % (modv, iqv)
            return "%.1f dBc" % modv
        bub = []
        if multi:
            # 반송파 여러 개: 반송파마다 양옆 인접 채널 (그 반송파 기준 dBc) - 'C1 아래 OFF → 지금'
            lv = {n_: v_ for n_, _f, v_ in (last.get("aclr_pos") or [])}
            nl = nr = 0
            for n_, f0, v_ in off["aclr_pos"]:
                side = "l" if f0 < 0 else "r"
                txt = "%s OFF %.1f" % (n_, v_) + (" → %.1f" % lv[n_] if last is not off and n_ in lv else "")
                y0 = at(last if last is not off else off, f0)
                bub.append((f0, y0, txt, C_DPD if last is not off else C_OFF, side, nl if side == "l" else nr + 1))
                if side == "l":
                    nl += 1
                else:
                    nr += 1
            vl = []
            for c_ in cars_:
                vl += [(c_ - cb_, "#90caf9"), (c_ + cb_, "#90caf9")]
            for _n, f0, _v in off["aclr_pos"]:
                vl += [(f0 - cb_, "#ef9a9a"), (f0 + cb_, "#ef9a9a")]
        else:
            for f0, side, key in ((-ad_, "l", "aclr_l"), (ad_, "r", "aclr_u")):
                r0 = 0 if side == "l" else 1                       # 오른쪽은 범례 아래부터
                bub.append((f0, at(off, f0), "OFF " + aval(off, key), C_OFF, side, r0))
                if last is not off:
                    bub.append((f0, at(last, f0), "%s %s" % (last["label"], aval(last, key)), C_DPD, side, r0 + 1))
            vl = [(-ad_ - cb_, "#ef9a9a"), (-cb_, "#90caf9"), (cb_, "#90caf9"), (ad_ + cb_, "#ef9a9a")]
        # 집중 구간 (4 카드): 주황 세로선 + 그 구간 전력 (dBc)
        try:
            t_ = str(self.v["focus_mhz"].get()).replace("~", "-").replace(",", "-").replace(" ", "")
            fa, fb = sorted(abs(float(v_)) for v_ in t_.split("-") if v_)[:2]
        except (ValueError, KeyError, TypeError):
            fa = fb = None
        fa_show, fb_show = fa, fb                             # 칸에 넣은 집중 구간 (보정 기준이 달라도 미리 보기로 칠함)
        if "w_mode" in self.v and self.v["w_mode"].get() != "focus":
            fa = fb = None
        if fa is not None and fb > fa:
            for c_ in cars_:                                  # 반송파마다 그 중심에서 ±구간
                vl += [(c_ - fb, "#e53935"), (c_ - fa, "#e53935"), (c_ + fa, "#e53935"), (c_ + fb, "#e53935")]
            mid_ = 0.5 * (fa + fb)
            fpos = ((cars_[0] - mid_, "l", "focus_l"), (cars_[-1] + mid_, "r", "focus_u"))
            for f0, side, key in fpos:
                r0 = 2 if side == "l" else 3
                if multi:                                     # 반송파 여러 개: 한 칸에 'OFF → 지금' (나쁜 쪽, 그 반송파 기준)
                    if key in off:
                        bub.append((f0, at(last if key in last else off, f0), "집중 OFF %.1f%s" % (
                            off[key], (" → %.1f" % last[key]) if (last is not off and key in last) else ""), "#ef6c00", side, r0))
                    continue
                if key in off:
                    bub.append((f0, at(off, f0), "집중 OFF %.1f" % off[key], "#ffb74d", side, r0))
                if last is not off and key in last:
                    bub.append((f0, at(last, f0), "집중 %s %.1f" % (last["label"], last[key]), "#ef6c00", side, r0 + 1))
            fmax = max(fmax, max(abs(c_) for c_ in cars_) + fb + 10)
        spans = []
        wm_ = self.v["w_mode"].get() if "w_mode" in self.v else "focus"
        if wm_ == "aclr":                                  # 보정 기준 = ACLR 채널: 가중하는 인접 채널 색칠
            pos_ = [q_[1] for q_ in off.get("aclr_pos") or []] if multi else [-ad_, ad_]
            spans += [(f0 - cb_, f0 + cb_, "#e53935") for f0 in pos_]
        if fa_show is not None and fb_show > fa_show:       # 집중 구간 (반송파마다 ±): 집중이면 빨강 · 아니면 회색 미리 보기
            col_ = "#e53935" if wm_ == "focus" else "#9e9e9e"
            for c_ in cars_:
                spans += [(c_ - fb_show, c_ - fa_show, col_), (c_ + fa_show, c_ + fb_show, col_)]
            fmax = max(fmax, max(abs(c_) for c_ in cars_) + fb_show + 10)
        self.p_spec.set(ser, xr=(-fmax, fmax), yr=(lo, 5), bubbles=bub, vlines=vl, spans=spans)
        rs = rs_all
        off, last = rs[0], hi
        its = list(range(len(rs)))
        self.p_trend.set([dict(x=its, y=[r["aclr_l"] for r in rs], color="#e53935", width=2, label="ACLR L"),
                          dict(x=its, y=[r["aclr_u"] for r in rs], color="#fb8c00", width=2, label="ACLR U"),
                          dict(x=its, y=[r["aclr_l"] for r in rs], color="#e53935", dots=True, r=3),
                          dict(x=its, y=[r["aclr_u"] for r in rs], color="#fb8c00", dots=True, r=3)],
                         xr=(-0.2, max(its) + 0.2 if max(its) > 0 else 1))
        ser = [dict(x=off["am_x"], y=off["am_g"], color=C_OFF, dots=True, label="DPD OFF")]
        if last is not off:
            ser.append(dict(x=last["am_x"], y=last["am_g"], color=C_DPD, dots=True, label=last["label"]))
        g0 = sorted(off["am_g"])[len(off["am_g"]) // 2] if off["am_g"] else 0
        self.p_amam.set(ser, xr=(-30, 1), yr=(g0 - 4, g0 + 3))
        ser = [dict(x=off["am_x"], y=off["am_p"], color=C_OFF, dots=True, label="DPD OFF")]
        if last is not off:
            ser.append(dict(x=last["am_x"], y=last["am_p"], color=C_DPD, dots=True, label=last["label"]))
        self.p_ampm.set(ser, xr=(-30, 1), yr=(-20, 20))
        self.sync_graph_window()

    # ------------------------------------------------------------ 저장
    def ask(self, title, ext, ft, name):
        return filedialog.asksaveasfilename(title=title, defaultextension=ext, filetypes=ft, initialfile=name,
                                            initialdir=self.s.get("save_dir") or None)

    def remember_dir(self, path):
        if path:
            self.s["save_dir"] = os.path.dirname(path)

    def stamp(self):
        return time.strftime("%Y%m%d_%H%M")

    def save_wv_orig(self):
        if not self.engine or self.engine.sig is None:
            messagebox.showinfo(APP, "먼저 [신호 만들기] 를 누르세요.")
            return
        p = self.ask(".wv 저장", ".wv", [("R&S waveform", "*.wv")], "%s_NR100M_1C_%s.wv" % (self.bw_tag(self.engine), self.stamp()) if self.engine.p.get("sig_src") != "file"
                     else "%s_%s_%s.wv" % (self.bw_tag(self.engine), os.path.splitext(os.path.basename(self.engine.p.get("wv_path") or "sig"))[0], self.stamp()))
        if p:
            E.C.write_wv(p, self.engine.sig.x, self.engine.sig.fs, "NR 100 MHz 1C (PA DPD Tester)")
            self.remember_dir(p)
            self.log("저장: %s" % p)

    # ------------------------------------------------------------ 측정 고르기 (표) · 그 입력 / 출력 신호
    def best_idx(self):
        """가장 좋은 측정 (DPD · ILC 중 ACLR 나쁜 쪽이 가장 낮은 것)"""
        c = [(aw(r), i) for i, r in enumerate(self.results) if r.get("kind") != "off"]
        return min(c)[1] if c else None

    def update_star(self):
        b = self.best_idx()
        for iid in self.tree.get_children():
            i = int(iid)
            if i >= len(self.results):
                continue
            vals = list(self.tree.item(iid, "values"))
            lab = self.results[i]["label"]
            vals[0] = ("★ " + lab) if i == b else lab
            self.tree.item(iid, values=vals)

    def pick_idx(self):
        sel = self.tree.selection()
        if sel:
            try:
                return int(sel[0])
            except ValueError:
                pass
        return self.best_idx()

    def on_pick(self):
        i = self.pick_idx()
        if i is not None and i < len(self.results):
            r = self.results[i]
            self.lbl_last.configure(text="고름: %s · ACLR %.1f / %.1f dBc · Pout %.2f dBm  (표 줄 = 그래프 굵게 · 아래 버튼으로 저장 · 내보내기)"
                                         % (r["label"], r["aclr_l"], r["aclr_u"], r["pout"]))
        self.draw()

    def bw_tag(self, eng=None):
        """파일 이름에 넣을 대역: 그 측정 엔진의 대역 (100M · 400M) → 없으면 지금 고른 대역"""
        for e_ in (eng, self.engine, getattr(self, "_last_eng", None)):
            bw = getattr(e_, "p", {}).get("bw_mode") if e_ is not None else None
            if bw:
                return "%sM" % bw
        return "%sM" % (self.v["bw_mode"].get() if "bw_mode" in self.v else "100")

    def pick_signal(self):
        """고른 측정 (표에서 고른 줄, 없으면 ★) 의 (엔진, 결과, 입력 신호, SG dBm, GHz, PA 출력)"""
        i = self.pick_idx()
        if i is None or i >= len(self.results):
            messagebox.showinfo(APP, "먼저 측정하세요 (DPD · ILC · 방식 비교).")
            return None
        r = self.results[i]
        eng = self.engine if self.engine is not None and hasattr(self.engine, "pd_store") else getattr(self, "_last_eng", None)
        st = getattr(eng, "pd_store", {}).get(r.get("pd_id")) if eng is not None else None
        if st is None:
            messagebox.showinfo(APP, "'%s' 의 신호는 보관 기간이 지났습니다 (최근 30 개 · 가장 좋은 것만 보관)." % r["label"])
            return None
        pd, sg, ghz, y = st
        return eng, r, E.np.asarray(pd, dtype=complex), sg, ghz, E.np.asarray(y, dtype=complex)

    def ilc_ctx(self):
        """JSH: 파일 이름 · 정보에 쓸 샘플 종류 · 신호 파일 (화면 칸은 화면 스레드에서만 읽음 → 실행 시작 때 담아 둠)"""
        try:
            stype = (self.v["b_stype"].get().strip() if "b_stype" in self.v else "") or "샘플"
            sig = self.v["wv_path"].get() if "wv_path" in self.v else ""
            self._ilc_ctx_v = (stype, sig)
        except Exception:
            pass
        return getattr(self, "_ilc_ctx_v", ("샘플", ""))

    def ilc_autosave(self, eng, p, partial=False):
        """ILC 가 끝나면 (또는 중지하면 그때까지 최고) 결과를 파일로 (작업 스레드에서) → 📂 저장한 ILC 로 나중에 바로 모델 학습"""
        try:
            st = getattr(eng, "_resume", None)
            if not st or st.get("kind") != "ilc":
                return
            os.makedirs(ILC_DIR, exist_ok=True)
            r = st["best"][1] if isinstance(st["best"][1], dict) else {}
            stype, sig_path = getattr(self, "_ilc_ctx_v", None) or ("샘플", "")
            stype = re.sub(r'[\\/:*?"<>|]', "_", stype)[:40]
            name = "ILC_%s_%.3fGHz_%.1fdBc_Pout%.2f_%s%s.npz" % (stype, p["freq_ghz"], st["best"][0], float(r.get("pout", float("nan"))),
                                                                   time.strftime("%Y%m%d_%H%M%S"), "_중지" if partial else "")
            meta = dict(when=time.strftime("%Y-%m-%d %H:%M"), stype=stype, freq=float(p["freq_ghz"]), sg=float(st["sg"]),
                        best=float(st["best"][0]), pout=(float(r["pout"]) if isinstance(r.get("pout"), (int, float)) else None),
                        r={k: (float(r[k]) if isinstance(r[k], (int, float)) else r[k]) for k in ("aclr_l", "aclr_u", "pout", "label") if k in r},
                        sig=sig_path, ver=VERSION, partial=bool(partial), iters=int(st.get("k") or 0),
                        w_mode=p.get("w_mode"), focus_mhz=p.get("focus_mhz"), focus_w=p.get("focus_w"))
            path = eng.ilc_save(os.path.join(ILC_DIR, name), meta)
            if path:
                self.q.put(("log", "  💾 ILC 결과 저장%s: %s\n     폴더: %s (📂 저장한 ILC 로 나중에 바로 모델 학습)"
                            % (" (중지 · 그때까지 최고)" if partial else "", name, ILC_DIR)))
            fs = sorted((f for f in os.listdir(ILC_DIR) if f.lower().endswith(".npz")),
                        key=lambda f: os.path.getmtime(os.path.join(ILC_DIR, f)))
            for f in fs[:-60]:
                try:
                    os.remove(os.path.join(ILC_DIR, f))
                except Exception:
                    pass
        except Exception as e:
            self.q.put(("log", "  ⚠ ILC 결과 저장 못 함: %s" % e))

    def ilc_latest(self, p):
        """저장한 ILC 중 지금 샘플 · 주파수와 같은 가장 최근 파일 → 확인 후 _resume 모양 (없으면 None · 취소면 'cancel')"""
        try:
            if not os.path.isdir(ILC_DIR):
                return None
            stype = (self.v["b_stype"].get().strip() if "b_stype" in self.v else "") or "샘플"
            stype = re.sub(r'[\\/:*?"<>|]', "_", stype)[:40]
            fs = sorted((os.path.join(ILC_DIR, f) for f in os.listdir(ILC_DIR) if f.lower().endswith(".npz")),
                        key=os.path.getmtime, reverse=True)
            for f in fs:
                try:
                    st = E.Engine.ilc_load(f)
                except Exception:
                    continue
                m = st.get("meta") or {}
                try:
                    same_f = abs(float(m.get("freq")) - float(p["freq_ghz"])) < 1e-6
                except Exception:
                    same_f = False
                if not same_f or (m.get("stype") or "") != stype:
                    continue
                txt = ("메모리에 ILC 결과가 없어 저장한 것 중 가장 최근 것을 씁니다.\n\n%s\n샘플 %s · %s GHz · ILC 최고 %.1f dBc · SG %.2f dBm\n%s\n\n"
                       "이걸로 모델 만들까요?\n(아니요 = 📂 저장한 ILC 로 직접 고르기)"
                       % (os.path.basename(f), m.get("stype", "?"), m.get("freq", "?"), st["best"][0], st["sg"], m.get("when", "")))
                if messagebox.askyesno(APP, txt, parent=self.root):
                    self.log("📂 저장한 ILC 자동 (가장 최근 · 같은 샘플 · 주파수): %s" % os.path.basename(f))
                    return st
                self.ilc_open()
                return "cancel"
        except Exception as e:
            self.log("  ⚠ 저장한 ILC 찾기 실패: %s" % e)
        return None

    def ilc_pick(self):
        """저장한 ILC 고르기 창 (표: 샘플 · 주파수 · ILC 최고 · Pout · 저장 시각) → 파일 경로 / None"""
        import numpy as np
        files = []
        for f in os.listdir(ILC_DIR):
            if not f.lower().endswith(".npz"):
                continue
            pth = os.path.join(ILC_DIR, f)
            m = {}
            try:
                import json as _j
                with np.load(pth, allow_pickle=False) as d:
                    m = _j.loads(str(d["meta"])) if "meta" in d.files else {}
                    m.setdefault("best", float(d["best"]))
            except Exception:
                pass
            files.append((os.path.getmtime(pth), pth, m))
        files.sort(reverse=True)
        top = tk.Toplevel(self.root)
        top.title("저장한 ILC 결과 고르기")
        top.transient(self.root.winfo_toplevel())
        f = ttk.Frame(top, padding=14)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="저장한 ILC 결과", font=("맑은 고딕", 12, "bold")).pack(anchor="w")
        ttk.Label(f, text="고르면 지금 4 카드 모델로 바로 학습 → 측정 (ILC 다시 안 돌림) · 1 카드 신호는 ILC 때와 같아야 함 · 두 번 클릭 = 바로 선택",
                  font=("맑은 고딕", 10)).pack(anchor="w", pady=(2, 8))
        stl = ttk.Style(top)
        stl.configure("Ilc.Treeview", font=("맑은 고딕", 11), rowheight=28)
        stl.configure("Ilc.Treeview.Heading", font=("맑은 고딕", 10, "bold"))
        cols = (("stype", "샘플", 180), ("freq", "주파수 GHz", 100), ("best", "ILC 최고 dBc", 110), ("pout", "Pout dBm", 90),
                ("focus", "보정 기준", 150), ("when", "저장 시각", 150))
        tf = ttk.Frame(f)
        tf.pack(fill="both", expand=True)
        tv = ttk.Treeview(tf, columns=[c[0] for c in cols], show="headings", height=min(14, max(6, len(files))), style="Ilc.Treeview")
        for k, t, w_ in cols:
            tv.heading(k, text=t)
            tv.column(k, width=w_, anchor="center" if k != "stype" else "w")
        sb = ttk.Scrollbar(tf, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        cur_f = None
        try:
            cur_f = float(self.v["freq_ghz"].get())
        except Exception:
            pass
        tv.tag_configure("same", foreground="#1565c0")
        for i, (mt, pth, m) in enumerate(files):
            wm = m.get("w_mode") or "time"
            fz = {"focus": "집중 %s MHz" % (m.get("focus_mhz") or ""), "aclr": "ACLR 채널", "time": "시간 영역"}.get(wm, wm)
            fq = m.get("freq")
            tags = ("same",) if (cur_f is not None and isinstance(fq, (int, float)) and abs(fq - cur_f) < 1e-6) else ()
            tv.insert("", "end", iid=str(i), tags=tags, values=(
                m.get("stype", os.path.basename(pth)), ("%.3f" % fq) if isinstance(fq, (int, float)) else "?",
                ("%.1f" % m["best"]) if isinstance(m.get("best"), (int, float)) else "?",
                ("%.2f" % m["pout"]) if isinstance(m.get("pout"), (int, float)) else "?", fz,
                m.get("when") or time.strftime("%Y-%m-%d %H:%M", time.localtime(mt))))
        if files:
            tv.selection_set("0")
            tv.focus("0")
        res = [None]
        info = ttk.Label(f, text=("파란 줄 = 지금 주파수와 같음 · 파일 %d 개 (최근 60 개 보관)" % len(files)) if files else
                         "저장한 ILC 가 없습니다. 5 카드 [한계 ACLR (ILC)] 를 한 번 끝까지 돌리면 자동 저장됩니다.", font=("맑은 고딕", 10))
        info.pack(anchor="w", pady=(6, 0))

        def ok(*_):
            sel = tv.selection()
            if sel:
                res[0] = files[int(sel[0])][1]
                top.destroy()

        def other():
            pth = filedialog.askopenfilename(parent=top, title="ILC 결과 파일", initialdir=ILC_DIR,
                                             filetypes=[("ILC 결과", "*.npz"), ("모든 파일", "*.*")])
            if pth:
                res[0] = pth
                top.destroy()

        def opendir():
            try:
                os.startfile(ILC_DIR)
            except Exception as e:
                info.configure(text="폴더: %s (%s)" % (ILC_DIR, e))

        tv.bind("<Double-1>", ok)
        tv.bind("<Return>", ok)
        bb = ttk.Frame(f)
        bb.pack(fill="x", pady=(10, 0))
        ttk.Button(bb, text="✔ 이걸로 모델 만들기", command=ok, style="Pri.TButton").pack(side="left")
        ttk.Button(bb, text="다른 파일…", command=other).pack(side="left", padx=(6, 0))
        ttk.Button(bb, text="📁 폴더 열기", command=opendir).pack(side="left", padx=(6, 0))
        ttk.Button(bb, text="취소", command=top.destroy).pack(side="right")
        top.bind("<Escape>", lambda e: top.destroy())
        self.center_popup(top)
        top.grab_set()
        tv.focus_set()
        top.wait_window()
        return res[0]

    def ilc_open(self):
        """📂 저장한 ILC 불러오기 → 바로 ILC 결과로 모델 만들기"""
        if E is None or self.worker:
            return
        os.makedirs(ILC_DIR, exist_ok=True)
        path = self.ilc_pick()
        if not path:
            return
        try:
            st = E.Engine.ilc_load(path)
        except Exception as e:
            messagebox.showwarning(APP, "불러오기 실패: %s" % e, parent=self.root)
            return
        m = st.get("meta") or {}
        self._ilc_pending = st
        txt = ("%s\n\n샘플 %s · %s GHz · ILC 최고 %.1f dBc · SG %.2f dBm · Pout %s\n%s\n\n"
               "지금 4 카드 모델 (%s) 로 바로 학습 → 측정할까요?\n(1 카드 신호는 ILC 때와 같아야 함)"
               % (os.path.basename(path), m.get("stype", "?"), m.get("freq", "?"), st["best"][0], st["sg"],
                  ("%.2f dBm" % m["pout"]) if isinstance(m.get("pout"), (int, float)) else "?", m.get("when", ""),
                  self.model_text(self.params(), None) if hasattr(self, "model_text") else ""))
        self.log("📂 ILC 결과 불러옴: %s (ILC 최고 %.1f dBc)" % (os.path.basename(path), st["best"][0]))
        self.start(True, from_ilc=True)                # 고르기 창에서 [이걸로 모델 만들기] 를 눌렀으니 바로

    def _live_engine(self, src):
        """중지된 실행의 엔진이면 새로 연결한 엔진으로 (신호 · 보관 신호는 넘김)"""
        if self.engine is not None:
            return self.engine
        eng = self.get_engine(self.params())
        for k in ("pd_store", "_best_id", "_best_sc", "_pd_n", "last_pd", "last_sg", "G", "_resume", "model"):
            if hasattr(src, k):
                setattr(eng, k, getattr(src, k))
        if eng.sig is None:
            eng.sig = src.sig
        return eng

    def smw_level(self, eng, pd, sg):
        return min(sg + 20 * math.log10(E.C.rms(pd) / E.C.rms(eng.sig.x)), eng.p["sg_max_dbm"])

    def fsw_col_show(self, on):
        """FSW ACLR 칸: FSW Mod. 화면을 읽은 측정이 하나라도 있을 때만 (I/Q 만 쓰면 I/Q ACLR 만 보임)"""
        cols = getattr(self, "tree_cols", None)
        if not cols or getattr(self, "_fsw_col", None) == on:
            return
        self._fsw_col = on
        try:
            self.tree.configure(displaycolumns=[c_ for c_ in cols if (on or c_ != "amod") and c_ != "rec"])   # 기록 Pout 칸은 안 보임
        except tk.TclError:
            pass

    @staticmethod
    def aclr_src_txt(r):
        """표 'ACLR 출처': 장비 = FSW Mod. 화면 (스펙트럼 ACLR) · I/Q = 받은 I/Q 로 계산"""
        return aclr_used_src(r)

    @staticmethod
    def aclr_mod_txt(r, f):
        """표 '장비 ACLR': 이 측정에서 FSW Mod. 화면을 읽었으면 그 값 (ACLR 출처가 I/Q 여도) · 안 읽었으면 '-'"""
        ml, mu = r.get("aclr_mod_l"), r.get("aclr_mod_u")
        if not (isinstance(ml, float) and ml == ml):
            return "-"
        if r.get("aclr_src") == "fsw" and len(r.get("aclr_in") or []) == 2:    # 반송파 2 개: 반송파마다 아래 / 위
            a_, b_ = aclr_cols(r, f)
            return "%s | %s" % (a_, b_)
        return "%s / %s" % (f(ml, 1), f(mu, 1))

    def send_final(self):
        """고른 측정 (표에서 고른 줄, 없으면 ★ 가장 좋은 것) 의 입력 신호를 SMW 로 보내고 RF 를 켜 둠 (계속 내보냄)"""
        if self.worker:
            return
        got = self.pick_signal()
        if not got:
            return
        eng0, r, pd, sg, ghz, _y = got
        if not messagebox.askyesno(APP, "'%s' (ACLR %.1f / %.1f dBc) 의 입력 신호를 SMW 로 보내고 RF 를 켜 둘까요?\n\n"
                                        "SG %.2f dBm · %.4g GHz · 끄려면 [신호 끄기]" % (r["label"], r["aclr_l"], r["aclr_u"],
                                                                                  self.smw_level(eng0, pd, sg), ghz)):
            return
        try:
            eng = self._live_engine(eng0)
        except ValueError as e:
            messagebox.showwarning(APP, str(e))
            return
        self.busy(True)

        def job():
            BL.set_busy(True)
            try:
                base = eng.p["freq_ghz"]
                eng.p["freq_ghz"] = ghz
                try:
                    lvl = eng.send_final(pd, sg)
                    try:
                        eng.fsw_like(r, ghz)               # FSW 를 그 측정 때의 감쇠 · RBW 로 (기록값과 같은 조건)
                    except Exception as e_:
                        self.q.put(("log", "  ⚠ FSW 를 기록 때 조건으로 못 맞춤: %s" % e_))
                finally:
                    eng.p["freq_ghz"] = base
                self.rf_state = True
                self.q.put(("log", "'%s' 입력 신호 내보내는 중 (SG %.2f dBm)" % (r["label"], lvl)))
                eng.go_local()                         # 앞 패널 바로 조작 · FSW 화면은 건드리지 않음
            except Exception as e:
                if not eng.stop:
                    self.q.put(("err", "신호 보내기 실패: %s" % err_text(e)))
            finally:
                BL.set_busy(False)
                self.q.put(("done", None))
        self.worker = threading.Thread(target=job, daemon=True)
        self.worker.start()

    @staticmethod
    def wv_info(path, raw):
        """저장한 .wv 의 SMW 레벨 · 주파수: 설명 (COMMENT: … SMW level x dBm @ y GHz) → 없으면 파일 이름 (…_3.6GHz_SG+1.23dBm_…)"""
        head = raw[:4096].decode("latin-1", "replace")
        m = re.search(r"SMW level\s*([-+]?\d+(?:\.\d+)?)\s*dBm\s*@\s*([-+]?\d+(?:\.\d+)?(?:e[-+]?\d+)?)\s*GHz", head)
        lvl = ghz = None
        if m:
            lvl, ghz = float(m.group(1)), float(m.group(2))
        nm = os.path.basename(path)
        if lvl is None:
            m = re.search(r"SG([-+]?\d+(?:\.\d+)?)dBm", nm)
            lvl = float(m.group(1)) if m else None
        if ghz is None:
            m = re.search(r"_(\d+(?:\.\d+)?)GHz", nm)
            ghz = float(m.group(1)) if m else None
        return lvl, ghz

    def send_wv_file(self):
        """저장해 둔 입력 .wv (고른 입력 .wv 저장… 으로 만든 것 · 다른 .wv 도) 를 골라 SMW 로 보내고 RF 를 켜 둠.
        레벨 · 주파수는 파일 설명 · 이름에서 읽어 채움 (고쳐서 보낼 수 있음)"""
        if E is None or self.worker:
            return
        path = filedialog.askopenfilename(title="SMW 로 내보낼 .wv 고르기", filetypes=[("R&S waveform", "*.wv"), ("모든 파일", "*.*")],
                                          initialdir=self.s.get("save_dir") or None)
        if not path:
            return
        try:
            raw = open(path, "rb").read()
            x_, fs_ = E.C.read_wv(path)
        except Exception as e:
            messagebox.showwarning(APP, "이 파일을 못 읽음: %s" % e)
            return
        lvl0, ghz0 = self.wv_info(path, raw)
        top = tk.Toplevel(self.root)
        top.title("저장한 .wv 내보내기")
        top.transient(self.root.winfo_toplevel())
        top.resizable(False, False)
        f = ttk.Frame(top, padding=14)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text=os.path.basename(path), font=FB, wraplength=520).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(f, text="%.2f MSps · %.2f ms · PAPR %.2f dB" % (fs_ / 1e6, len(x_) / fs_ * 1e3 if fs_ else 0, E.C.papr(x_)),
                  foreground="#78909c").grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 8))
        v_l = tk.StringVar(value="%.2f" % lvl0 if lvl0 is not None else str(self.v["sg_dbm"].get() if "sg_dbm" in self.v else ""))
        v_g = tk.StringVar(value="%.6g" % ghz0 if ghz0 is not None else str(self.v["freq_ghz"].get() if "freq_ghz" in self.v else ""))
        ttk.Label(f, text="SG 레벨").grid(row=2, column=0, sticky="w")
        ttk.Entry(f, textvariable=v_l, width=9, justify="right").grid(row=2, column=1, sticky="w", padx=6)
        ttk.Label(f, text="dBm (RMS)%s" % ("  ← 파일에서 읽음" if lvl0 is not None else "  ← 파일에 없음 · 지금 SG 값"),
                  foreground="#78909c").grid(row=2, column=2, sticky="w")
        ttk.Label(f, text="주파수").grid(row=3, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(f, textvariable=v_g, width=9, justify="right").grid(row=3, column=1, sticky="w", padx=6, pady=(4, 0))
        ttk.Label(f, text="GHz%s" % ("  ← 파일에서 읽음" if ghz0 is not None else "  ← 파일에 없음 · 지금 주파수"),
                  foreground="#78909c").grid(row=3, column=2, sticky="w", pady=(4, 0))
        ttk.Label(f, text="보내면 SMW RF 가 켜진 채로 계속 나감 (끄려면 SG RF OFF) · SG 최대를 넘으면 최대로 제한\n"
                          "바이어스 · 케이블 · 온도가 저장할 때와 같아야 같은 ACLR (PA 가 바뀌면 DPD 를 다시)",
                  foreground="#78909c").grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 4))
        bf = ttk.Frame(f)
        bf.grid(row=6, column=0, columnspan=3, sticky="e")

        def ok():
            try:
                lvl, ghz = float(v_l.get()), float(v_g.get())
            except ValueError:
                messagebox.showwarning(APP, "SG 레벨 · 주파수를 숫자로 넣으세요", parent=top)
                return
            if not (0.001 < ghz < 100):
                messagebox.showwarning(APP, "주파수는 GHz 로 넣으세요", parent=top)
                return
            top.destroy()
            try:
                eng = self.get_engine(self.params())
            except Exception as e:
                messagebox.showwarning(APP, str(e))
                return
            self.remember_dir(path)
            self.busy(True)

            def job():
                BL.set_busy(True)
                try:
                    lv = eng.send_wv_file(raw, ghz, lvl, os.path.basename(path))
                    self.rf_state = True
                    self.q.put(("log", "'%s' 내보내는 중 (SG %.2f dBm · %.4g GHz)" % (os.path.basename(path), lv, ghz)))
                    eng.go_local()
                except Exception as e:
                    if not eng.stop:
                        self.q.put(("err", "신호 보내기 실패: %s" % err_text(e)))
                finally:
                    BL.set_busy(False)
                    self.q.put(("done", None))
            self.worker = threading.Thread(target=job, daemon=True)
            self.worker.start()
        ttk.Button(bf, text="▶ 보내고 RF ON", command=ok).pack(side="left", padx=3)
        ttk.Button(bf, text="취소", command=top.destroy).pack(side="left", padx=3)
        top.bind("<Escape>", lambda e: top.destroy())
        self.center_popup(top)
        top.grab_set()

    def setting_rows(self):
        """1 ~ 5 카드의 설정 칸 → [(카드 번호, 카드 이름, 키, 이름, {값: 버튼 글자})] (화면 순서)"""
        names = {str(var): k for k, var in self.v.items()}
        out, seen = [], set()
        for num, (title, body) in sorted(getattr(self, "cards_num", {}).items()):
            found = []

            def walk(w):
                for c in w.winfo_children():
                    key = None
                    for opt in ("textvariable", "variable"):
                        try:
                            key = names.get(str(c.cget(opt)))
                        except tk.TclError:
                            pass
                        if key:
                            break
                    if key:
                        found.append((key, c))
                    walk(c)
            walk(body)
            for key, c in found:
                if key in seen:
                    if c.winfo_class() == "TRadiobutton":
                        for row in out:
                            if row[2] == key:
                                try:
                                    row[4][str(c.cget("value"))] = str(c.cget("text"))
                                except tk.TclError:
                                    pass
                    continue
                seen.add(key)
                lab, segs = "", {}
                cls = c.winfo_class()
                try:
                    if cls == "TCheckbutton":
                        lab = str(c.cget("text")).strip(" └")
                    elif cls == "TRadiobutton":
                        segs[str(c.cget("value"))] = str(c.cget("text"))
                except tk.TclError:
                    pass
                if not lab:
                    try:                                    # 같은 줄 왼쪽 이름 (grid 0 열)
                        w_ = c if c.grid_info() else c.master
                        gi = w_.grid_info()
                        for sib in w_.master.grid_slaves(row=int(gi["row"]), column=0):
                            if sib is not w_ and sib.winfo_class() == "TLabel":
                                lab = str(sib.cget("text"))
                    except (tk.TclError, KeyError, ValueError):
                        pass
                if not lab:
                    lab = {"more_n": "이어서 반복 횟수"}.get(key) or (TIPS.get(key) or key).split("\n")[0]
                lab = {"xloss_on": "추가 손실 사용 (커플러 없으면 끄기)"}.get(key, lab)
                out.append([num, title, key, lab, segs])
        return out

    def save_settings_xlsx(self, path, eng, r, lvl, ghz, wv_path):
        """입력 .wv 와 같이: 그 측정 때 1 ~ 5 카드 설정 · 자동으로 정해진 값 · 결과 → 엑셀 (다음에 같은 조건으로 돌릴 때 참고)"""
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "설정"
        hb = Font(bold=True, color="FFFFFF")
        hf = PatternFill("solid", fgColor="1E88E5")
        cf = PatternFill("solid", fgColor="E3F2FD")
        bold = Font(bold=True)

        def fmt(v):
            if isinstance(v, bool):
                return "켬" if v else "끔"
            if isinstance(v, float):
                return v if math.isfinite(v) else "-"
            if isinstance(v, (list, tuple, dict)):
                return str(v)
            return v
        f2 = lambda v_: round(v_, 3) if isinstance(v_, float) and math.isfinite(v_) else "-"
        info = [("저장 시각", time.strftime("%Y-%m-%d %H:%M:%S")), ("입력 .wv", os.path.basename(wv_path)),
                ("프로그램", "%s %s" % (APP, VERSION)), ("대역", self.bw_tag(eng).replace("M", " MHz DPD")),
                ("측정", r["label"]), ("단계", r.get("step") or ""), ("DPD 모델 · 학습", self.model_text(getattr(eng, "p", {}) or {}, None) if getattr(eng, "p", None) else "-"), ("SMW 레벨 (이 .wv)", "%.2f dBm" % lvl), ("주파수", "%.6g GHz" % ghz),
                ("ACLR L / U", "%s / %s dBc" % (f2(r["aclr_l"]), f2(r["aclr_u"]))),
                ("ACLR 안쪽", " / ".join("%.1f" % v_ for v_ in (r.get("aclr_in") or [])) or "-"),
                ("ACLR 출처", {"fsw": "FSW Mod. 화면", "iq": "I/Q"}.get(r.get("aclr_src"), r.get("aclr_src") or "-")),
                ("FSW ACLR L / U (Mod. 화면)", self.aclr_mod_txt(r, lambda v_, n_=1: ("%.*f" % (n_, v_)) if isinstance(v_, float) and v_ == v_ else "-")),
                ("Pout", "%s dBm" % f2(r["pout"])), ("Gain", "%s dB" % f2(r["gain"])), ("Pin", "%s dBm" % f2(r["pin"])),
                ("EVM", "%s %%" % f2(r.get("evm"))), ("PAE / DE", "%s / %s %%" % (f2(r.get("pae")), f2(r.get("de")))),
                ("Vdd / Id", "%s V / %s A" % (f2(r.get("vdd")), f2(r.get("id")))), ("PAPR (입력)", "%s dB" % f2(r.get("papr"))),
                ("FSW Ref (그 측정)", "%s dBm" % f2(r.get("fsw_ref"))), ("FSW 감쇠 I/Q · Mod.", "%s / %s dB" % (r.get("fsw_att", "-"), r.get("mod_att", "-"))),
                ("Pout 보정", "%s dB" % f2(r.get("cal_db"))), ("I/Q 평균 (주기)", r.get("n_avg", "-"))]
        ws.append(["■ 이번 측정", ""])
        ws.cell(row=1, column=1).font = bold
        for k_, v_ in info:
            ws.append([k_, v_])
        al_ = getattr(eng, "_auto_lines", None) or []
        if al_:
            ws.append([])
            ws.append(["■ 자동 (auto) 으로 정해진 값", ""])
            ws.cell(row=ws.max_row, column=1).font = bold
            for l_ in al_:
                ws.append(["", l_])
        ws.append([])
        ws.append(["카드", "항목", "그 실행 때 값", "지금 화면 값", "키"])
        h_row = ws.max_row
        for c_ in ws[h_row]:
            c_.font, c_.fill = hb, hf
        ep = getattr(eng, "p", {}) or {}
        last_card = None
        for num, title, key, lab, segs in self.setting_rows():
            try:
                now = self.v[key].get()
            except Exception:
                now = ""
            run = ep.get(key, now)
            if segs:
                run, now = segs.get(str(run), run), segs.get(str(now), now)
            ws.append(["%d %s" % (num, title) if num != last_card else "", lab, fmt(run), fmt(now), key])
            if num != last_card:
                for c_ in ws[ws.max_row]:
                    c_.fill = cf
            if str(run) != str(now) and fmt(run) != fmt(now):
                ws.cell(row=ws.max_row, column=4).font = Font(bold=True, color="C62828")
            last_card = num
        for col, w_ in zip("ABCDE", (22, 40, 34, 22, 16)):
            ws.column_dimensions[col].width = w_
        for row in ws.iter_rows():
            for c_ in row:
                c_.alignment = Alignment(vertical="top", wrap_text=False)
        ws.freeze_panes = ws.cell(row=h_row + 1, column=1)
        wb.save(path)

    def save_wv_dpd(self):
        """고른 측정 (없으면 ★) 의 입력 신호를 .wv 로 - 파일 이름 · 설명에 SMW 에 넣을 SG 레벨"""
        got = self.pick_signal()
        if not got:
            return
        eng, r, pd, sg, ghz, _y = got
        lvl = self.smw_level(eng, pd, sg)
        st = self.v["b_stype"].get().strip() or "DPD"
        tag = re.sub(r"[^0-9A-Za-z가-힣._-]+", "_", r["label"]).strip("_")
        p = self.ask("입력 신호 .wv 저장 (%s)" % r["label"], ".wv", [("R&S waveform", "*.wv")],
                     "%s_%s_%s_ACLR%.1f_%.4gGHz_SG%+.2fdBm_%s.wv" % (st, self.bw_tag(eng), tag, aw(r), ghz, lvl, self.stamp()))
        if p:
            E.C.write_wv(p, pd, eng.sig.fs, "PA DPD Tester input %s DPD (%s, ACLR %.1f/%.1f dBc) - SMW level %.2f dBm @ %.4g GHz"
                         % (self.bw_tag(eng).replace("M", " MHz"), r["label"], r["aclr_l"], r["aclr_u"], lvl, ghz))
            self.remember_dir(p)
            self.log("저장: %s  → SMW 에서 이 파일 + 레벨 %.2f dBm (%.4g GHz) 로 내보내면 같은 조건" % (p, lvl, ghz))
            if self.s.get("save_set_xlsx", True):
                px = os.path.splitext(p)[0] + "_설정.xlsx"
                try:
                    self.save_settings_xlsx(px, eng, r, lvl, ghz, p)
                    self.log("저장: %s  (그 측정 때 1 ~ 5 카드 설정 · 자동 값 · 결과)" % px)
                except PermissionError:
                    self.log("⚠ 설정 엑셀이 열려 있어 저장 못 함: %s" % px)
                except Exception as e:
                    self.log("⚠ 설정 엑셀 저장 실패: %s" % e)

    def save_out(self):
        """고른 측정 (없으면 ★) 의 PA 출력 I/Q (시간 맞춤 · 작은 신호 이득으로 나눔) 저장 - .wv 또는 .csv (I, Q)"""
        got = self.pick_signal()
        if not got:
            return
        eng, r, pd, sg, ghz, y = got
        st = self.v["b_stype"].get().strip() or "DPD"
        tag = re.sub(r"[^0-9A-Za-z가-힣._-]+", "_", r["label"]).strip("_")
        p = self.ask("PA 출력 I/Q 저장 (%s)" % r["label"], ".wv", [("R&S waveform", "*.wv"), ("CSV (I, Q)", "*.csv")],
                     "%s_%s_%s_OUT_ACLR%.1f_%.4gGHz_%s.wv" % (st, self.bw_tag(eng), tag, aw(r), ghz, self.stamp()))
        if not p:
            return
        if p.lower().endswith(".csv"):
            with open(p, "w", encoding="utf-8") as f:
                f.write("# PA 출력 (%s) · %.2f MSps · 작은 신호 이득으로 나눈 값 (원래 신호와 같은 크기)\nI,Q\n" % (r["label"], eng.sig.fs / 1e6))
                for v in y:
                    f.write("%.6e,%.6e\n" % (v.real, v.imag))
        else:
            E.C.write_wv(p, y, eng.sig.fs, "PA DPD Tester PA output (%s, ACLR %.1f/%.1f dBc, Pout %.2f dBm)"
                         % (r["label"], r["aclr_l"], r["aclr_u"], r["pout"]))
        self.remember_dir(p)
        self.log("저장: %s  (PA 출력 · %s)" % (p, r["label"]))

    def recheck_final(self):
        """고른 측정 (없으면 ★) 의 입력 신호를 다시 보내 한 번 재 봄 (같은 결과가 나오는지 · FSW Mod. 화면 ACLR)"""
        if self.worker:
            return
        got = self.pick_signal()
        if not got:
            return
        eng0, r, pd, sg, ghz, _y = got
        try:
            eng = self._live_engine(eng0)
        except ValueError as e:
            messagebox.showwarning(APP, str(e))
            return
        self.busy(True)
        rid = self.run_id = getattr(self, "run_id", 0) + 1
        lab = "다시 재기 (%s)" % r["label"]

        def job():
            BL.set_busy(True)
            try:
                eng.stop = False
                eng._connect0()
                base, bf = eng.p["sg_dbm"], eng.p["freq_ghz"]
                eng.p["sg_dbm"], eng.p["freq_ghz"] = sg, ghz
                eng._force_mod = eng.p.get("aclr_src") != "iq"
                try:
                    eng.measure(pd, lab, 99, None, "SG %.2f dBm" % sg)
                finally:
                    eng._force_mod = False
                    eng.p["sg_dbm"], eng.p["freq_ghz"] = base, bf
                self.q.put(("log", "✔ %s 끝" % lab))
            except Exception as e:
                self.q.put(("err", "다시 재기 실패: %s" % err_text(e)))
            finally:
                try:
                    eng.fsw_restore()
                except Exception:
                    pass
                BL.set_busy(False)
                self.q.put(("done", rid))
        self.worker = threading.Thread(target=job, daemon=True)
        self.worker.start()

    def save_coef(self):
        m = self.engine.model if self.engine else None
        if m is None or m.w is None:
            messagebox.showinfo(APP, "DPD 를 실행한 뒤에 저장할 수 있습니다.")
            return
        p = self.ask("DPD 계수 저장", ".csv", [("CSV", "*.csv")], "DPD_%s_coef_%s.csv" % (self.bw_tag(self.engine), self.stamp()))
        if p and not hasattr(m, "term_names"):            # 신경망: 가중치를 CSV 한 줄씩
            with open(p, "w", encoding="utf-8-sig") as f:
                f.write("# 신경망 DPD (ARVTDNN) M=%d hidden=%d\nname,index,value\n" % (m.M, m.H))
                for k_, a_ in m.w.items():
                    for i_, v_ in enumerate(E.np.ravel(a_)):
                        f.write("%s,%d,%.9e\n" % (k_, i_, v_))
            self.remember_dir(p)
            self.log("저장: %s" % p)
            return
        if p:
            names = m.term_names()
            with open(p, "w", encoding="utf-8-sig") as f:
                f.write("# K=%d M=%d L=%d%s odd=%s lambda=%g\nterm,re,im\n" % (m.K, m.M, m.L,
                        (" lag=%d lead=%d" % (m.lag, m.lead)) if getattr(m, "lag", m.L) != m.L or getattr(m, "lead", m.L) != m.L else "",
                        m.odd, m.lam))
                for nm, c in zip(names, m.w):
                    f.write("%s,%.9e,%.9e\n" % (nm, c.real, c.imag))
            self.remember_dir(p)
            self.log("저장: %s" % p)

    def save_xlsx(self):
        if not self.results:
            messagebox.showinfo(APP, "저장할 결과가 없습니다.")
            return
        try:
            import openpyxl
            from openpyxl.styles import Font, PatternFill, Alignment
        except ImportError:
            messagebox.showerror(APP, "openpyxl 이 없습니다.")
            return
        path = self.ask("결과 엑셀 저장", ".xlsx", [("Excel", "*.xlsx")], "DPD_%s_%s.xlsx" % (self.bw_tag(), self.stamp()))
        if not path:
            return
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "결과"
        p = self.params()
        ws.append(["PA DPD Tester %s" % VERSION, time.strftime("%Y-%m-%d %H:%M")])
        ws.append(["신호", "NR 100 MHz 1C · %s · %d 심볼 · ×%d · CFR %s %.1f dB" % (p["mod"], p["n_sym"], p["os"], "ON" if p["cfr_on"] else "OFF", p["papr_db"])])
        ws.append(["DPD", "K=%d M=%d L=%d%s %s λ=%g μ=%g 반복 %d" % (p["K"], p["M"], p["L"],
                   "".join(" %s=%d" % (n_, p[k_]) for k_, n_ in (("L_lag", "Lag"), ("L_lead", "Lead")) if p.get(k_) is not None),
                   "홀수만" if p["odd_only"] else "", p["lam"], p["mu"], p["iters"])
                  + ((" · ILA+보정: 오차 보정 %d 번 · μ %g" % (p.get("ec_iters", 0), p.get("ec_mu", 0))) if p.get("method") == "ilaec" else "")])
        ws.append(["RF", "%.4f GHz · 입력 손실 %.2f dB · 출력 손실 %.2f dB · 보정 %.2f dB" % (p["freq_ghz"], p["in_loss_db"], p["out_loss_db"], p["cal_db"])])
        bias_ = getattr(self, "run_bias", None) or self.get_bias()
        br = self.bias_rows(bias_)
        ws.append(["바이어스", ("  ".join("%s %s %s" % x for x in br) + ("  (%s)" % bias_.get("src", "") if bias_ else "")) if br else "정보 없음 (Bench 파워서플라이 연결 안 됨)"])
        ws.append([])
        hd = ["단계", "SG dBm", "Pin dBm", "Pout dBm", "Gain dB", "ACLR L dBc", "ACLR U dBc", "EVM %", "PAPR dB", "학습 NMSE dB", "시각",
              "Id mA", "DE %", "PAE %", "Pout 기준", "Pout I/Q+보정", "Pout 스펙트럼", "DE I/Q+보정 %", "DE 스펙트럼 %",
              "FSW Ref dBm", "FSW 감쇠 dB (I/Q)", "FSW 감쇠 dB (Mod.)", "ACLR 출처", "FSW ACLR L dBc", "FSW ACLR U dBc",
              "I/Q ACLR L dBc", "I/Q ACLR U dBc"]
        ws.append(hd)
        for c in ws[ws.max_row]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="1565C0")
            c.alignment = Alignment(horizontal="center")
        for r in self.results:
            ws.append([r["label"], round(r["sg"], 2), round(r["pin"], 2), round(r["pout"], 2), round(r["gain"], 2),
                       round(r["aclr_l"], 2), round(r["aclr_u"], 2), round(r["evm"], 3), round(r["papr"], 2),
                       round(r["nmse"], 2) if r.get("nmse") is not None else None, r["t"],
                       round(r["id"] * 1e3, 1) if isinstance(r.get("id"), float) and r["id"] == r["id"] else None,
                       round(r["de"], 2) if isinstance(r.get("de"), float) and r["de"] == r["de"] else None,
                       round(r["pae"], 2) if isinstance(r.get("pae"), float) and r["pae"] == r["pae"] else None,
                       {"spec": "스펙트럼", "iq": "I/Q+보정"}.get(r.get("pout_used"), ""),
                       *[(round(r[k_], 2) if isinstance(r.get(k_), float) and r[k_] == r[k_] else None)
                         for k_ in ("pout_iq", "pout_spec", "de_iq", "de_spec", "fsw_ref", "fsw_att", "mod_att")],
                       aclr_used_src(r),
                       *[(round(r[k_], 2) if isinstance(r.get(k_), float) and r[k_] == r[k_] else None)
                         for k_ in ("aclr_mod_l", "aclr_mod_u", "aclr_iq_l", "aclr_iq_u")]])
        for col, w in zip("ABCDEFGHIJK", (10, 9, 9, 10, 9, 11, 11, 8, 9, 12, 9)):
            ws.column_dimensions[col].width = w
        ws2 = wb.create_sheet("스펙트럼")
        ws2.append(["MHz"] + [r["label"] + " dB" for r in self.results])
        sp = [(r.get("iq_f") or r["spec_f"], r.get("iq_db") or r["spec_db"]) for r in self.results]   # 같은 주파수 칸 (I/Q)
        for i, fval in enumerate(sp[0][0]):
            ws2.append([round(fval, 3)] + [round(d_[i], 2) if i < len(d_) else None for _f, d_ in sp])
        ws3 = wb.create_sheet("AM-AM AM-PM")
        off, last = self.results[0], self.results[-1]
        ws3.append(["OFF 입력 dB", "OFF 이득 dB", "OFF 위상 °", "", last["label"] + " 입력 dB", "이득 dB", "위상 °"])
        for i in range(max(len(off["am_x"]), len(last["am_x"]))):
            a = [off["am_x"][i], off["am_g"][i], off["am_p"][i]] if i < len(off["am_x"]) else [None] * 3
            b = [last["am_x"][i], last["am_g"][i], last["am_p"][i]] if i < len(last["am_x"]) else [None] * 3
            ws3.append([round(v, 3) if v is not None else None for v in a] + [None] + [round(v, 3) if v is not None else None for v in b])
        wsb = wb.create_sheet("바이어스")
        wsb.append(["항목", "값", "단위"])
        for x in br:
            wsb.append(list(x))
        wsb.append(["출처", (bias_ or {}).get("src", "없음"), ""])
        try:
            self.xlsx_charts(wb, ws, ws2, ws3, len(self.results))
        except Exception as e:                         # 그래프가 안 들어가도 데이터는 저장
            self.log("⚠ 엑셀 그래프 만들기 실패: %s" % e)
        try:
            wb.save(path)
        except PermissionError:
            messagebox.showerror(APP, "파일이 열려 있어 저장하지 못했습니다:\n%s" % path)
            return
        self.remember_dir(path)
        self.log("저장: %s (그래프 포함)" % path)

    def xlsx_charts(self, wb, ws, ws2, ws3, n):
        """엑셀 안에 그래프 (엑셀에서 바로 고칠 수 있는 진짜 차트): 측정 순서별 ACLR · 스펙트럼 · AM-AM · AM-PM · ACLR vs Pout"""
        from openpyxl.chart import ScatterChart, LineChart, Reference, Series
        from openpyxl.chart.marker import Marker

        def style_line(sr, color, width=1.5, dash=None, marker=False):
            sr.graphicalProperties.line.solidFill = color
            sr.graphicalProperties.line.width = int(width * 12700)
            if dash:
                sr.graphicalProperties.line.dashStyle = dash
            sr.marker = Marker(symbol="circle", size=5) if marker else Marker(symbol="none")
            if marker:
                sr.marker.graphicalProperties.solidFill = color
                sr.marker.graphicalProperties.line.solidFill = color
            sr.smooth = False

        def axes(ch, xt, yt, xmin=None, xmax=None, ymin=None, ymax=None):
            ch.x_axis.title, ch.y_axis.title = xt, yt
            for ax, lo, hi in ((ch.x_axis, xmin, xmax), (ch.y_axis, ymin, ymax)):
                if lo is not None:
                    ax.scaling.min = lo
                if hi is not None:
                    ax.scaling.max = hi
                ax.delete = False
            ch.x_axis.crosses = "min"                   # 가로축 글자를 아래에 (값이 모두 음수여도)
            ch.y_axis.crosses = "min"
            ch.legend.position = "b"
            ch.width, ch.height = 22, 11

        hdr_row = 7                                     # 결과 표 머리 줄 (위에 신호 · DPD · RF · 바이어스 · 빈 줄)
        r0, r1 = hdr_row + 1, hdr_row + n
        # 1) 측정 순서별 ACLR (결과 시트)
        lc = LineChart()
        lc.title = "측정 순서별 ACLR"
        for col, color in ((6, "E53935"), (7, "FB8C00")):
            ref = Reference(ws, min_col=col, min_row=hdr_row, max_row=r1)
            lc.add_data(ref, titles_from_data=True)
        lc.set_categories(Reference(ws, min_col=1, min_row=r0, max_row=r1))
        for sr, color in zip(lc.series, ("E53935", "FB8C00")):
            style_line(sr, color, 2, marker=True)
        lc.y_axis.title, lc.x_axis.title = "dBc", "단계"
        lc.x_axis.delete = lc.y_axis.delete = False
        lc.x_axis.crosses = "min"
        lc.legend.position = "b"
        lc.width, lc.height = 22, 10
        ws.add_chart(lc, "M2")
        # 2) 스펙트럼: OFF (회색 굵게) · 중간 (옅은 파랑) · 마지막 (파랑 굵게)
        sc = ScatterChart()
        sc.title = "스펙트럼 (채널 최고 = 0 dB)"
        sc.style = 13
        nr = ws2.max_row
        xref = Reference(ws2, min_col=1, min_row=2, max_row=nr)
        for i in range(n):
            yref = Reference(ws2, min_col=2 + i, min_row=1, max_row=nr)
            sr = Series(yref, xref, title_from_data=True)
            if i == 0:
                style_line(sr, "9E9E9E", 2)
            elif i == n - 1:
                style_line(sr, "1E88E5", 2)
            else:
                style_line(sr, "90CAF9", 0.75)
            sc.series.append(sr)
        lo = min(min(v for v in (r.get("iq_db") or r["spec_db"]) if v == v) for r in self.results)
        axes(sc, "주파수 오프셋 (MHz)", "dB", ymin=max(-100, math.floor(lo / 10) * 10), ymax=5)
        ws2.add_chart(sc, "%s2" % ws2.cell(row=1, column=n + 3).column_letter)
        # 3) AM-AM · AM-PM (점)
        nr3 = ws3.max_row
        for title, yt, ycol_off, ycol_on, anchor in (("AM-AM (작은 신호 이득 = 0)", "이득 dB", 2, 6, "I2"),
                                                     ("AM-PM", "위상 °", 3, 7, "I26")):
            ch = ScatterChart()
            ch.title = title
            ch.style = 13
            for xc, yc, color in ((1, ycol_off, "9E9E9E"), (5, ycol_on, "1E88E5")):
                sr = Series(Reference(ws3, min_col=yc, min_row=1, max_row=nr3), Reference(ws3, min_col=xc, min_row=2, max_row=nr3),
                            title_from_data=True)
                sr.marker = Marker(symbol="circle", size=3)
                sr.marker.graphicalProperties.solidFill = color
                sr.marker.graphicalProperties.line.solidFill = color
                sr.graphicalProperties.line.noFill = True
                ch.series.append(sr)
            axes(ch, "입력 dB (최고값 기준)", yt, xmin=-30, xmax=1)
            ws3.add_chart(ch, anchor)
        # 4) ACLR vs Pout (레벨마다 OFF · DPD 마지막) - 파워 스윕이면 곡선, 한 레벨이면 점 2개
        lv = {}
        for r in self.results:
            k = round(r.get("base_sg") if r.get("base_sg") is not None else r["sg"], 3)
            d = lv.setdefault(k, {})
            if r.get("kind") == "off":
                d["off"] = r
            elif r.get("kind") == "ilc":
                if "on" not in d or aw(r) < aw(d["on"]):
                    d["on"] = r
            else:
                d["on"] = r
        ws4 = wb.create_sheet("ACLR vs Pout")
        ws4.append(["SG 레벨 dBm", "OFF Pout dBm", "OFF ACLR dBc", "DPD Pout dBm", "DPD ACLR dBc", "OFF PAE %", "DPD PAE %"])
        for k in sorted(lv):
            o, d = lv[k].get("off"), lv[k].get("on")
            rr = lambda q, key: (round(aw(q), 2) if key == "aclr" else
                                 (round(q[key], 2) if isinstance(q.get(key), float) and math.isfinite(q[key]) else None)) if q else None
            ws4.append([k, rr(o, "pout"), rr(o, "aclr"), rr(d, "pout"), rr(d, "aclr"), rr(o, "pae"), rr(d, "pae")])
        m = ws4.max_row
        ch = ScatterChart()
        ch.title = "ACLR (나쁜 쪽) vs Pout"
        ch.style = 13
        for xc, yc, color, dash in ((2, 3, "FB8C00", "dash"), (4, 5, "1E88E5", None)):
            sr = Series(Reference(ws4, min_col=yc, min_row=1, max_row=m), Reference(ws4, min_col=xc, min_row=2, max_row=m),
                        title_from_data=True)
            style_line(sr, color, 2, dash=dash, marker=True)
            ch.series.append(sr)
        axes(ch, "DUT Pout (dBm)", "ACLR (dBc)")
        ws4.add_chart(ch, "I2")
        for col, w in zip("ABCDEFG", (12, 13, 13, 13, 13, 11, 11)):
            ws4.column_dimensions[col].width = w

    def on_close(self):
        if self.worker and self.engine:
            if not messagebox.askyesno(APP, "측정 중입니다. 멈추고 닫을까요?"):
                return
        try:
            self.root.withdraw()                         # 창은 바로 닫힌 것처럼
            self.root.update_idletasks()
        except tk.TclError:
            pass
        self.shutdown()
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()
    os._exit(0)                                          # 장비 연결 · 스레드가 남아 있어도 프로그램은 확실히 끝냄


if __name__ == "__main__":
    main()
