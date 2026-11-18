# -*- coding: utf-8 -*-
"""
PA Bench Monitor - 엑셀 내보내기
  · "전체" 탭 : 모든 기록 + 오른쪽 위 Gain vs Pout, 아래 PAE vs Pout (주파수 겹쳐 그림)
  · 주파수별 탭 : 그 주파수 기록 + 같은 배치의 그래프 2개
  · 두 그래프의 X축(DUT Pout) 범위·간격은 같게, 색상은 프로그램 화면과 같게
필요 패키지: openpyxl (build_exe.bat 에서 자동 설치)
"""
import datetime
import math

from openpyxl import Workbook
from openpyxl.chart import ScatterChart, Reference, Series
from openpyxl.chart.layout import Layout, ManualLayout
from openpyxl.chart.shapes import GraphicalProperties
from openpyxl.drawing.line import LineProperties
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

FONT = "Arial"
HEAD_FILL = PatternFill("solid", fgColor="1F4E78")
THIN = Side(style="thin", color="BFBFBF")


def _axis(ax, title, lo, hi, major, minor=None, number_format="General"):
    ax.title = title
    ax.scaling.min = lo
    ax.scaling.max = hi
    ax.majorUnit = major
    if minor:
        ax.minorUnit = minor
        ax.minorTickMark = "out"
    ax.majorTickMark = "out"
    ax.number_format = number_format
    ax.delete = False                      # 새 버전 엑셀에서 축이 숨겨지지 않도록


def _chart(title, ytitle, yaxis, xaxis, width_cm=17.0, height_cm=8.6):
    ch = ScatterChart()
    ch.title = title
    ch.style = 13
    ch.height = height_cm
    ch.width = width_cm
    ch.legend.position = "r"
    ch.display_blanks = "gap"              # 비어 있는 값(PAE 없음)은 끊어서 표시
    x0, x1, xmaj, xmin = xaxis
    _axis(ch.x_axis, "DUT Pout (dBm)", x0, x1, xmaj, xmin)
    y0, y1, ymaj = yaxis
    _axis(ch.y_axis, ytitle, y0, y1, ymaj)
    ch.x_axis.crosses = "min"
    ch.y_axis.crosses = "min"
    return ch


SYMBOLS = {"o": "circle", "s": "square", "^": "triangle", "D": "diamond", "v": "triangle", "P": "plus"}


def _add_series(ch, ws, xcol, ycol, r0, r1, label, color, shape="o"):
    xs = Reference(ws, min_col=xcol, min_row=r0, max_row=r1)
    ys = Reference(ws, min_col=ycol, min_row=r0, max_row=r1)
    s = Series(ys, xs, title=label)
    s.marker.symbol = SYMBOLS.get(shape, "circle")
    s.marker.size = 5
    s.marker.graphicalProperties = GraphicalProperties(solidFill=color.lstrip("#"))
    s.marker.graphicalProperties.line.solidFill = color.lstrip("#")
    s.graphicalProperties = GraphicalProperties(ln=LineProperties(solidFill=color.lstrip("#"), w=22000))
    s.smooth = False
    ch.series.append(s)


def _write_table(ws, cols, rows, row_values, start_row=1):
    for c, (_, title, _w) in enumerate(cols, 1):
        cell = ws.cell(row=start_row, column=c, value=title)
        cell.font = Font(name=FONT, bold=True, color="FFFFFF", size=10)
        cell.fill = HEAD_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = Border(top=THIN, bottom=THIN, left=THIN, right=THIN)
        ws.column_dimensions[get_column_letter(c)].width = max(8.5, min(14, len(title) * 1.1 + 2))
    ws.row_dimensions[start_row].height = 30
    text_cols = {i for i, c in enumerate(cols, 1) if c[0] in ("time", "sample")}
    for i, r in enumerate(rows, start_row + 1):
        vals = row_values(r)
        for c, v in enumerate(vals, 1):
            if v in ("", None):
                v2 = None
            elif c in text_cols:  # 시간 · 샘플은 글자 그대로
                v2 = v
            else:
                try:
                    v2 = float(v)
                except (TypeError, ValueError):
                    v2 = v
            cell = ws.cell(row=i, column=c, value=v2)
            cell.font = Font(name=FONT, size=10)
            cell.alignment = Alignment(horizontal="center")
            cell.border = Border(bottom=Side(style="hair", color="D9D9D9"))
    ws.freeze_panes = ws.cell(row=start_row + 1, column=1)
    # 인쇄: 가로 방향, 표+그래프를 한 페이지 폭에 맞춤
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0


def _write_bias(ws, keys, ds_label, bias_of):
    """맨 위: 데이터셋마다 바이어스 한 줄 (VDD · VG_DRV · VG_CA · VG_PK · Idq · I_DRV · I_CA). 다음 표 시작 행 반환"""
    if bias_of is None:
        return 1
    items = [(k, bias_of(k)) for k in keys]
    items = [(k, b) for k, b in items if b]
    if not items:
        return 1
    heads = ["바이어스", "VDD (V)", "VG_DRV (V)", "VG_CA (V)", "VG_PK (V)", "Idq (mA)", "I_DRV (mA)", "I_CA (mA)", "I_leak (mA)"]
    fill = PatternFill("solid", fgColor="FFF8E1")
    vfill = PatternFill("solid", fgColor="FFFDE7")
    for c, t in enumerate(heads, 1):
        cell = ws.cell(row=1, column=c, value=t)
        cell.font = Font(name=FONT, bold=True, color="5D4037", size=10)
        cell.fill = fill
        cell.alignment = Alignment(horizontal="center")
        cell.border = Border(top=THIN, bottom=THIN, left=THIN, right=THIN)

    def num(v, k=1.0, d=3):
        return round(v * k, d) if (v is not None and v == v) else None
    for i, (k, b) in enumerate(items, 2):
        vals = [ds_label(k), num(b.get("vdd"), 1, 2), num(b.get("vg_drv")), num(b.get("vg_ca")), num(b.get("vg_pk")),
                num(b.get("idq"), 1e3, 1), num(b.get("i_drv"), 1e3, 1), num(b.get("i_ca"), 1e3, 1), num(b.get("i_leak"), 1e3, 2)]
        for c, v in enumerate(vals, 1):
            cell = ws.cell(row=i, column=c, value=v)
            cell.font = Font(name=FONT, bold=(c > 1), size=10)
            cell.fill = vfill
            cell.alignment = Alignment(horizontal="center")
            cell.border = Border(bottom=Side(style="hair", color="D9D9D9"))
    return len(items) + 3


def export_xlsx(path, cols, rows, row_values, ds_key, ds_label, ds_color, axes, vdd=None, note="", ds_shape=None,
                bias_of=None):
    """
    cols       : [(key, 제목, 폭), ...]  (기록 표 열)
    rows       : 기록 행 dict 목록
    row_values : 행 → 표시 값 튜플
    ds_key     : 행 → 데이터셋 키 (샘플, 주파수)
    ds_label   : 키 → 이름 ("S1 · 3.5 GHz")
    ds_color   : 키 → "#rrggbb" (화면 그래프와 같은 색)
    axes       : (pae(lo,hi,step), gain(lo,hi,step), x(lo,hi,label,minor))  None 이면 기본값
    """
    pae_ax = axes[0] or (20, 80, 10)
    gain_ax = axes[1] or (20, 40, 2.5)
    x_ax = axes[2] or (26, 54, 5, 1)
    # 엑셀은 축 숫자를 최솟값부터 찍음 → 30, 35 … 처럼 나오도록 표시 간격의 배수로 범위를 맞춤 (26~54 → 25~55)
    import math as _m
    lab = x_ax[2] or 5
    x_ax = (_m.floor(x_ax[0] / lab + 1e-9) * lab, _m.ceil(x_ax[1] / lab - 1e-9) * lab, lab, x_ax[3])
    keys = [c[0] for c in cols]
    xcol = keys.index("pout") + 1
    gcol = keys.index("gain") + 1
    pcol = keys.index("pae") + 1

    # 데이터셋(샘플 × 주파수)별로 묶고(측정 순서), 그 안에서는 SG 파워 순으로 정렬
    groups = {}
    for r in rows:
        groups.setdefault(ds_key(r), []).append(r)
    order = list(groups)
    for k in order:
        groups[k].sort(key=lambda r: (r.get("sg") is None, r.get("sg") or 0))
    freqs = []
    for k in order:
        if k[1] not in freqs:
            freqs.append(k[1])

    wb = Workbook()
    ws = wb.active
    ws.title = "전체"
    all_rows = [r for k in order for r in groups[k]]
    h0 = _write_bias(ws, order, ds_label, bias_of)
    _write_table(ws, cols, all_rows, row_values, start_row=h0)
    anchor_col = get_column_letter(len(cols) + 2)

    def charts_for(ws_, blocks, suffix):
        g = _chart("Gain vs. Pout" + suffix, "Gain (dB)", gain_ax, x_ax)
        p = _chart("PAE vs. Pout" + suffix, "PAE (%)", pae_ax, x_ax)
        for (k, r0, r1) in blocks:
            label = ds_label(k)
            color = ds_color(k)
            shape = ds_shape(k) if ds_shape else "o"
            _add_series(g, ws_, xcol, gcol, r0, r1, label, color, shape)
            _add_series(p, ws_, xcol, pcol, r0, r1, label, color, shape)
        ws_.add_chart(g, "%s2" % anchor_col)     # 위: Gain
        ws_.add_chart(p, "%s20" % anchor_col)    # 아래: PAE

    blocks, r = [], h0 + 1
    for k in order:
        n = len(groups[k])
        blocks.append((k, r, r + n - 1))
        r += n
    if blocks:
        charts_for(ws, blocks, " (전체 주파수)")

    for fk in freqs:   # 주파수별 탭 (그 주파수의 샘플들을 겹쳐 그림)
        name = (("%.6gGHz" % fk) if fk is not None else "주파수없음")[:31]
        wsf = wb.create_sheet(name)
        ks = [k for k in order if k[1] == fk]
        frows = [r for k in ks for r in groups[k]]
        hf = _write_bias(wsf, ks, ds_label, bias_of)
        _write_table(wsf, cols, frows, row_values, start_row=hf)
        fblocks, rr = [], hf + 1
        for k in ks:
            fblocks.append((k, rr, rr + len(groups[k]) - 1))
            rr += len(groups[k])
        charts_for(wsf, fblocks, " @ %s" % (("%.6g GHz" % fk) if fk is not None else "-"))

    # 정보 탭
    wi = wb.create_sheet("정보")
    info = [("내보낸 시각", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            ("기록 수", len(rows)), ("Vdd (V)", vdd if vdd is not None else "미입력"),
            ("Gain 축", "%g ~ %g dB, %g 간격" % tuple(gain_ax)),
            ("PAE 축", "%g ~ %g %%, %g 간격" % tuple(pae_ax)),
            ("Pout 축", "%g ~ %g dBm, 숫자 %g / 눈금 %s" % (x_ax[0], x_ax[1], x_ax[2], x_ax[3] or "-")),
            ("계산식", "DUT Pin = SG − In loss,  DUT Pout = SA + Out loss,  Gain = Pout − Pin,  "
                      "PAE = (Pout − Pin)[W] / (Vdd·Id) × 100"),
            ("비고", note or "")]
    for i, (a, b) in enumerate(info, 1):
        wi.cell(row=i, column=1, value=a).font = Font(name=FONT, bold=True)
        wi.cell(row=i, column=2, value=b).font = Font(name=FONT)
    wi.column_dimensions["A"].width = 14
    wi.column_dimensions["B"].width = 90
    wb.save(path)
    return path
