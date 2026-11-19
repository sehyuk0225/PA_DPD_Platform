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
    _jsh_topbar(self)                # JSH: 맨 위 막대 배경을 짙은 녹색으로 (원본 프로그램과 구분)


# ---- JSH: 맨 위 막대 색 (원본 Bench 파일은 그대로 두고 여기서만 칠함) ----
JSH_OLD_TB = "#1c2530"               # 원본 맨 위 막대 색 (거의 검정)
JSH_TB = "#14532d"                   # 짙은 녹색


def _jsh_paint(w):
    """막대와 그 안에서 막대 색을 그대로 쓰는 칸만 녹색으로 (장비 칩 · 버튼 같은 자기 색은 그대로)"""
    try:
        if str(w.cget("bg")).lower() == JSH_OLD_TB:
            w.configure(bg=JSH_TB)
    except Exception:
        pass
    for c in w.winfo_children():
        _jsh_paint(c)


def _jsh_topbar(app):
    try:
        bar = app.cb_stype_top.master.master          # 샘플 칸 → 샘플 묶음 → 맨 위 막대
    except Exception:
        return
    _jsh_paint(bar)
    try:
        app.root.after(1500, lambda: _jsh_paint(bar))  # 나중에 붙는 칸 (외부 전원 등) 도 한 번 더
    except Exception:
        pass


bm.BenchApp.__init__ = _init2
bm.VERSION = "%s (%s %s)" % (bm.VERSION, site2.SITE, site2.SITE_VER)

if __name__ == "__main__":
    bm.main()
