"""PA Bench Monitor Site2 - Vdd 파워서플라이 2 대 (예: E3632A × 2, GPIB) 를 1 대처럼 (직렬 24 V + 24 V 등)
나머지는 PA Bench Monitor 와 같음. 자세한 것: site2.py · README_Site2.txt"""
import site2
site2.install()                      # psu_panel 의 장비 열기만 감쌈 (바이어스 자동 순서는 그대로)
import pa_bench_monitor as bm

bm.APP_NAME = "PA Bench Monitor %s" % site2.SITE
_init = bm.BenchApp.__init__


def _init2(self, *a, **k):
    _init(self, *a, **k)
    site2.attach_ui(self)            # 맨 위 막대 [PK 게이트 +6V | +25V] (샘플 종류마다)
    site2.attach_vdd_ui(self)        # 파워서플라이 화면: Site2 Vdd 2 대 최대 (앞 · 뒤)
    site2.attach_da_order(self)      # 드라이버 앰프 전원: 바이어스 다 맞춘 뒤 ON · 끌 때 맨 먼저 OFF
    site2.attach_addr_ui(self)       # '파워서플라이 연결 · SCPI' 탭: 주소 칸 직접 입력 + 목록 더블클릭 → 칸 고르기
    _jsh_skin(self)                  # JSH: 위쪽 패널 (맨 위 막대 · 측정 탭 줄 · 큰 숫자 칸) 배경을 녹색 #00704A 로


# ---- JSH: 위쪽 패널 색 (원본 Bench · DPD 파일은 그대로 두고 여기서만 칠함) ----
# '녹색 테마' 느낌만: 바탕 칸 (Frame) 과 그 바탕 위에 바로 놓인 글씨 (Label) 만 녹색.
# 큰 숫자 칸 (ACLR · Pout · PAE 등) · 진행 줄 · 버튼 · 장비 칩은 원래 색 그대로
JSH_GREEN = "#00704A"                # 스타벅스 그린
JSH_OLD_BG = {"#1c2530",             # 맨 위 막대 바탕
              "#141b22"}             # 측정 탭 줄 · 큰 숫자 칸 사이 바탕 (Bench · DPD · 그래프 새 창)


def _jsh_paint(w):
    """바탕 칸 (tk.Frame) 과 바탕 위 글씨 (tk.Label) 만 녹색으로. 큰 숫자 칸 (#0b0f14) · 버튼 · 칩은 그대로"""
    try:
        if w.winfo_class() in ("Frame", "Label") and str(w.cget("bg")).lower() in JSH_OLD_BG:
            w.configure(bg=JSH_GREEN)
    except Exception:
        pass
    try:
        kids = w.winfo_children()
    except Exception:
        return
    for c in kids:
        _jsh_paint(c)


def _jsh_top(w, root):
    """w 를 품은 맨 바깥 칸 (root 바로 아래)"""
    while w is not None and getattr(w, "master", None) is not None and w.master is not root:
        w = w.master
    return w


def _jsh_skin(app):
    root = app.root
    areas = []
    try:
        areas.append(_jsh_top(app.cb_stype_top, root))                  # 맨 위 막대
    except Exception:
        pass
    try:
        areas.append(_jsh_top(next(iter(app.view_btns.values())), root))  # Pulse · Mod. · DPD 탭 줄
    except Exception:
        pass
    if getattr(app, "dash", None) is not None:
        areas.append(app.dash)                                          # 큰 숫자 (DPD 화면의 ACLR · Pout · PAE 칸 포함)

    def tick():
        for a in areas:
            _jsh_paint(a)
        try:                                                            # DPD '그래프 새 창' 의 큰 숫자 줄
            for t in root.winfo_children():
                if t.winfo_class() == "Toplevel":
                    for c in t.winfo_children():
                        if str(c.cget("bg")).lower() == "#141b22":
                            _jsh_paint(c)
        except Exception:
            pass
        try:
            root.after(700, tick)                                       # DPD 탭처럼 나중에 생기는 칸도 칠함
        except Exception:
            pass
    tick()


bm.BenchApp.__init__ = _init2
bm.VERSION = "%s (%s %s)" % (bm.VERSION, site2.SITE, site2.SITE_VER)

if __name__ == "__main__":
    bm.main()
