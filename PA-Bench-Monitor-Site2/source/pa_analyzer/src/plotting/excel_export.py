"""Export the plotted curves to Excel: the numbers + a native (editable) Excel chart per plot.

Each plot on screen becomes one sheet:
  * an Excel scatter chart (lines + markers) with the same colours, line styles, axis titles and
    axis ranges as on screen -> edit it in Excel / copy it into a report;
  * below the chart, the data of every curve as  [Pout | Y]  column pairs.
An "Info" sheet lists the curves (type, FREQ, number of points) and the measurement conditions.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from openpyxl import Workbook
from openpyxl.chart import Reference, ScatterChart, Series
from openpyxl.chart.axis import ChartLines
from openpyxl.chart.marker import Marker
from openpyxl.chart.text import RichText
from openpyxl.drawing.text import CharacterProperties, Font, Paragraph, ParagraphProperties
from openpyxl.styles import Alignment, Font as CellFont, PatternFill
from openpyxl.utils import get_column_letter

CHART_FONT = "Arial Narrow"
DATA_ROW = 32                     # first row of the data table (chart sits above it)
_DASH = {"-": "solid", "--": "dash", ":": "sysDot", "-.": "dashDot", "solid": "solid", "dashed": "dash",
         "dotted": "sysDot", "dashdot": "dashDot"}
_MARK = {"o": "circle", "s": "square", "^": "triangle", "v": "triangle", "D": "diamond", "d": "diamond",
         "x": "x", "P": "plus", "+": "plus", "*": "star", ".": "dot"}


@dataclass
class CurveOut:
    label: str
    x: Sequence[float]
    y: Sequence[float]
    kind: str                      # "Measurement" / "Simulation"
    color: str = "#1f77b4"
    linestyle: str = "-"
    linewidth: float = 1.2         # pt
    marker: str = ""               # matplotlib marker code ("" = none)
    markersize: float = 4.5
    alpha: float = 1.0
    freq: str = ""


@dataclass
class PlotOut:
    title: str
    x_label: str
    y_label: str
    curves: List[CurveOut]
    xlim: Optional[Tuple[float, float]] = None
    ylim: Optional[Tuple[float, float]] = None
    conditions: List[str] = field(default_factory=list)
    x_step: Optional[float] = None        # tick spacing on screen -> same grid in Excel
    y_step: Optional[float] = None


def _hex(color: str, alpha: float = 1.0) -> str:
    """'#d62728' (+ transparency, blended on white like on screen) -> 'D62728'."""
    c = (color or "#1f77b4").lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    try:
        r, g, b = (int(c[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return "1F77B4"
    a = min(max(float(alpha), 0.0), 1.0)
    r, g, b = (round(v * a + 255 * (1 - a)) for v in (r, g, b))
    return f"{r:02X}{g:02X}{b:02X}"


def _text(size_pt: float = 10, bold: bool = False) -> RichText:
    cp = CharacterProperties(latin=Font(typeface=CHART_FONT), sz=int(size_pt * 100), b=bold)
    return RichText(p=[Paragraph(pPr=ParagraphProperties(defRPr=cp), endParaRPr=cp)])


def _sheet_name(title: str, used: set) -> str:
    base = re.sub(r"[\[\]:*?/\\]", "_", title).strip()[:28] or "Plot"
    name, i = base, 2
    while name.lower() in used:
        name = f"{base[:25]}_{i}"
        i += 1
    used.add(name.lower())
    return name


MAX_CHART_SERIES = 255


def _style_series(ser: Series, cv: CurveOut) -> None:
    col = _hex(cv.color, cv.alpha)
    gp = ser.graphicalProperties
    gp.line.solidFill = col
    gp.line.width = int(max(cv.linewidth, 0.25) * 12700)          # pt -> EMU
    gp.line.prstDash = _DASH.get(cv.linestyle or "-", "solid")
    if cv.linestyle in ("None", "none", "", " "):
        gp.line.noFill = True
    sym = _MARK.get(cv.marker or "", "none")
    ser.marker = Marker(symbol=sym)
    if sym != "none":
        ser.marker.size = int(min(max(round(cv.markersize + 0.5), 2), 72))
        from openpyxl.chart.shapes import GraphicalProperties
        mp = GraphicalProperties(solidFill=col)
        mp.line.solidFill = col
        ser.marker.graphicalProperties = mp
    ser.smooth = False


def write_plot_sheet(ws, plot: PlotOut) -> None:
    ws.sheet_view.showGridLines = True
    ws.page_setup.orientation = "landscape"        # printing / PDF: chart + legend on one page width
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
    head = CellFont(name=CHART_FONT, bold=True)
    small = CellFont(name=CHART_FONT, color="555555")
    ws.cell(row=DATA_ROW - 4, column=1, value=plot.title).font = CellFont(name=CHART_FONT, bold=True, size=12)
    for i, line in enumerate(plot.conditions[:2]):
        ws.cell(row=DATA_ROW - 3 + i, column=1, value=line).font = small
    fill = PatternFill("solid", fgColor="EEF2F8")
    chart = ScatterChart()
    chart.title = plot.title
    chart.style = 13
    chart.scatterStyle = "lineMarker"
    chart.height, chart.width = 12.5, 22
    chart.x_axis.title = plot.x_label
    chart.y_axis.title = plot.y_label
    for axis in (chart.x_axis, chart.y_axis):
        axis.delete = False                       # newer Excel hides axes otherwise
        axis.txPr = _text(10)
        axis.majorTickMark = "out"
        axis.majorGridlines = ChartLines()
    chart.legend.position = "r"
    chart.legend.txPr = _text(9)
    for axis, lim, step in ((chart.x_axis, plot.xlim, plot.x_step), (chart.y_axis, plot.ylim, plot.y_step)):
        if not lim:
            continue
        lo, hi = lim
        if step and step > 0:                     # whole ticks: 25, 30, 35 … instead of 26, 31, 36 …
            import math
            lo = math.floor(lo / step + 1e-9) * step
            hi = math.ceil(hi / step - 1e-9) * step
            axis.majorUnit = round(step, 6)
        axis.scaling.min, axis.scaling.max = round(lo, 4), round(hi, 4)
    chart.x_axis.crosses = "min"                  # axes at the lower-left corner, like on screen
    chart.y_axis.crosses = "min"

    xu = plot.x_label
    yu = plot.y_label
    skipped = 0
    for i, cv in enumerate(plot.curves):
        c0 = 1 + 2 * i
        ws.merge_cells(start_row=DATA_ROW, start_column=c0, end_row=DATA_ROW, end_column=c0 + 1)
        top = ws.cell(row=DATA_ROW, column=c0, value=cv.label)
        top.font, top.fill = head, fill
        top.alignment = Alignment(horizontal="center", wrap_text=True)
        ws.cell(row=DATA_ROW + 1, column=c0, value=xu).font = small
        ws.cell(row=DATA_ROW + 1, column=c0 + 1, value=yu).font = small
        n = 0
        for r, (xv, yv) in enumerate(zip(cv.x, cv.y), start=DATA_ROW + 2):
            try:
                xv, yv = float(xv), float(yv)
            except (TypeError, ValueError):
                continue
            if xv != xv or yv != yv:                      # NaN -> leave the cell empty
                continue
            ws.cell(row=r, column=c0, value=round(xv, 4))
            ws.cell(row=r, column=c0 + 1, value=round(yv, 4))
            n = r
        for col in (c0, c0 + 1):
            ws.column_dimensions[get_column_letter(col)].width = 11
        if not n:
            continue
        xref = Reference(ws, min_col=c0, min_row=DATA_ROW + 2, max_row=n)
        yref = Reference(ws, min_col=c0 + 1, min_row=DATA_ROW + 2, max_row=n)
        if len(chart.series) >= MAX_CHART_SERIES:       # Excel: at most 255 series per chart
            skipped += 1
            continue
        ser = Series(yref, xref, title=cv.label[:250])
        _style_series(ser, cv)
        chart.series.append(ser)
    if skipped:
        note = ws.cell(row=DATA_ROW - 1, column=1,
                       value=f"※ Excel 차트는 곡선 {MAX_CHART_SERIES}개까지만 그릴 수 있어 나머지 {skipped}개는 "
                             f"아래 데이터 열에만 있습니다.")
        note.font = CellFont(color="C62828", bold=True)
    try:                                          # title in the chart font, bold
        chart.title.tx.rich.p[0].pPr = ParagraphProperties(defRPr=CharacterProperties(
            latin=Font(typeface=CHART_FONT), sz=1300, b=True))
    except AttributeError:
        pass
    ws.add_chart(chart, "A1")


def write_info_sheet(ws, plots: List[PlotOut]) -> None:
    head = CellFont(name=CHART_FONT, bold=True)
    fill = PatternFill("solid", fgColor="EEF2F8")
    rows = [("Plot", "Curve", "Type", "FREQ", "Points", "Colour")]
    for p in plots:
        for cv in p.curves:
            rows.append((p.title, cv.label, cv.kind, cv.freq, len(cv.x), "#" + _hex(cv.color)))
    for r, row in enumerate(rows, start=1):
        for c, v in enumerate(row, start=1):
            cell = ws.cell(row=r, column=c, value=v)
            cell.font = head if r == 1 else CellFont(name=CHART_FONT)
            if r == 1:
                cell.fill = fill
    r = len(rows) + 2
    conds = list(dict.fromkeys(line for p in plots for line in p.conditions))
    if conds:
        ws.cell(row=r, column=1, value="Conditions (selected dataset)").font = head
        for i, line in enumerate(conds, start=1):
            ws.cell(row=r + i, column=1, value=line).font = CellFont(name=CHART_FONT)
    for col, w in zip("ABCDEF", (18, 34, 13, 11, 8, 10)):
        ws.column_dimensions[col].width = w


def export_xlsx(path: str, plots: List[PlotOut]) -> str:
    if not path.lower().endswith(".xlsx"):
        path += ".xlsx"
    wb = Workbook()
    used: set = set()
    first = True
    for p in plots:
        ws = wb.active if first else wb.create_sheet()
        first = False
        ws.title = _sheet_name(p.title, used)
        write_plot_sheet(ws, p)
    info = wb.create_sheet(_sheet_name("Info", used))
    write_info_sheet(info, plots)
    wb.save(path)
    return path
