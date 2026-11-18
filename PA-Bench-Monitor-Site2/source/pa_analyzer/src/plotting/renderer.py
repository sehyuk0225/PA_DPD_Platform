"""Plot Renderer: PlotRequest -> matplotlib Figure.

Backend-agnostic (works with QtAgg on screen and Agg in tests / export).
The title and Measurement Condition block are drawn *inside the figure*, so
Copy Plot / Save PNG produce exactly what is on screen, conditions included.
"""
from __future__ import annotations

import functools

from typing import List, Optional

import matplotlib
from matplotlib.figure import Figure

from core.models import PlotRequest, Source

SIM_STYLE = dict(color="#1f4e9c", linewidth=2.0, linestyle="-", zorder=2)
MEAS_STYLE = dict(color="#d62728", linewidth=1.2, linestyle="-", marker="o",
                  markersize=4.5, markeredgewidth=0.6, markeredgecolor="#7a1414", zorder=3)
# extra colours for future multi-curve plots (multi-frequency / multi-simulation)
SIM_CYCLE = ["#1f4e9c", "#2ca02c", "#9467bd", "#17becf", "#8c564b"]
MEAS_CYCLE = ["#d62728", "#ff7f0e", "#e377c2", "#bcbd22", "#7f7f7f"]


@functools.lru_cache(maxsize=4096)
def _text_w_in(text: str, size_pt: float) -> float:
    """Width of `text` in the plot font, inch (no renderer needed). Cached: same texts on every redraw."""
    from matplotlib.font_manager import FontProperties
    from matplotlib.textpath import TextToPath
    try:
        w, _h, _d = TextToPath().get_text_width_height_descent(text, FontProperties(family=FAMILY, size=size_pt), False)
        return w / 72.0
    except Exception:
        return len(text) * CHAR_IN * size_pt / COND_PT


from plotting.fonts import FAMILY, LATIN_FONT  # noqa: E402  (sets the plot fonts, Korean included)

FONT = LATIN_FONT
TITLE_PT = 12.5
COND_PT = 8.3
CHAR_IN = 0.062         # approx. width of one character at COND_PT (inch)
LINE_IN = 0.21          # height of one condition line (inch)
MARGIN_IN = dict(left=0.85, right=0.25, bottom=0.62)
BASE_SIZE_IN = (8.0, 5.5)
COL_GAP_IN = 0.28       # gap between the DRV / CA / PK columns of the condition header   # figure size at which fonts are 100 %


def font_scale(fig) -> float:
    """Fonts / margins shrink with the plot window (never below 55 %, at most 115 %)."""
    w, h = fig.get_figwidth(), fig.get_figheight()
    k = min(w / BASE_SIZE_IN[0], h / BASE_SIZE_IN[1])
    return max(0.55, min(1.15, k ** 0.7))


class PlotRenderer:
    def __init__(self, figure: Figure):
        self.figure = figure
        self.ax = None
        self._cond_items: List[str] = []
        self._title = ""
        self._header_artists = []
        self._cid = None

    # ------------------------------------------------------------------
    def render(self, req: Optional[PlotRequest], show_condition: bool = True):
        fig = self.figure
        fig.clear()
        fig.set_facecolor("white")
        ax = fig.add_subplot(111)
        self.ax = ax
        k = font_scale(fig)
        self._header_artists = []

        if req is None:
            self._title = ""
            self._cond_items = []
            self._cond_grid = []
            ax.text(0.5, 0.5, "Open a measurement Excel file (Ctrl+O)",
                    ha="center", va="center", transform=ax.transAxes, color="#888", fontsize=11 * k)
            ax.set_xticks([]); ax.set_yticks([])
            self._layout()
            return ax

        self._title = req.title
        self._cond_items = list(req.condition_lines) if show_condition else []
        self._cond_grid = [list(r) for r in req.condition_grid] if show_condition else []

        sim_i = meas_i = 0
        for s in req.series:
            if s.role == Source.SIMULATION:
                st = dict(SIM_STYLE, color=SIM_CYCLE[sim_i % len(SIM_CYCLE)]); sim_i += 1
            else:
                c = MEAS_CYCLE[meas_i % len(MEAS_CYCLE)]
                st = dict(MEAS_STYLE, color=c); meas_i += 1
            st.update(s.style or {})
            if "markersize" in st:
                st["markersize"] = st["markersize"] * k
            if "linewidth" in st:
                st["linewidth"] = st["linewidth"] * max(k, 0.7)
            ax.plot(s.x, s.y, label=s.label, **st)

        ax.set_xlabel(req.x_label, fontsize=10.5 * k, fontweight="bold")
        ax.set_ylabel(req.y_label, fontsize=10.5 * k, fontweight="bold")
        ax.grid(True, which="major", alpha=0.35)
        ax.minorticks_on()
        ax.grid(True, which="minor", alpha=0.12)
        ax.tick_params(labelsize=9.5 * k, length=3.5 * k)
        if req.series and getattr(req, "legend", None):
            from matplotlib.lines import Line2D       # compact legend: proxies, the curves keep their labels
            hs = []
            for lab, st in req.legend:
                st = dict(st)
                if "markersize" in st:
                    st["markersize"] = st["markersize"] * k
                hs.append(Line2D([], [], label=lab, **st))
            ax.legend(handles=hs, loc="upper left", fontsize=8.5 * k, framealpha=0.92,
                      title=f"Colour = FREQ  ({len(req.series)} curves)", title_fontsize=8.5 * k)
        elif req.series:
            ax.legend(loc="best", fontsize=9.5 * k, framealpha=0.92)
        if req.notice:
            ax.text(0.99, 0.01, req.notice, transform=ax.transAxes, ha="right", va="bottom",
                    fontsize=8.5 * k, color="#b45f06", style="italic")
        if not req.series:
            ax.text(0.5, 0.5, getattr(req, "empty_text", "") or "No data for this graph", ha="center", va="center",
                    transform=ax.transAxes, color="#888", fontsize=11 * k)
        if req.xlim:
            ax.set_xlim(*req.xlim)
        if req.ylim:
            ax.set_ylim(*req.ylim)
        self._layout()
        return ax

    # ------------------------------------------------------------------
    def _layout(self, *_):
        """Fixed-inch margins so the header never overlaps when the window resizes."""
        fig = self.figure
        for a in self._header_artists:
            try:
                a.remove()
            except Exception:
                pass
        self._header_artists = []
        w, h = fig.get_figwidth(), fig.get_figheight()
        k = font_scale(fig)
        y_in = 0.12 * k
        if self._title:
            t = fig.text(0.5, 1 - y_in / h, self._title, ha="center", va="top",
                         fontsize=TITLE_PT * k, fontweight="bold", family=FAMILY)
            t.set_gid("_plot_title")
            self._header_artists.append(t)
            y_in += 0.30 * k
        grid = getattr(self, "_cond_grid", [])
        if grid:
            # aligned columns: IQ_* exactly under the matching VG_*
            n = max(len(r) for r in grid)
            # column width = widest text actually measured in the plot font (Arial Narrow is much
            # narrower than a per-character estimate) + a small gap
            col_w = [max((_text_w_in(r[j], COND_PT * k) if j < len(r) and r[j] else 0) for r in grid) + COL_GAP_IN * k
                     for j in range(n)]
            total = sum(col_w)
            x0_in = max((w - total) / 2, 0.1)
            for row in grid:
                x_in = x0_in
                for j in range(n):
                    txt = row[j] if j < len(row) else ""
                    if txt:
                        t = fig.text(x_in / w, 1 - y_in / h, txt, ha="left", va="top",
                                     fontsize=COND_PT * k, family=FAMILY, color="#222")
                        self._header_artists.append(t)
                    x_in += col_w[j]
                y_in += LINE_IN * 0.92 * k
        for line in _wrap_condition(self._cond_items, w):
            t = fig.text(0.5, 1 - y_in / h, line, ha="center", va="top",
                         fontsize=COND_PT * k, family=FAMILY, color="#222")
            self._header_artists.append(t)
            y_in += LINE_IN * 0.92 * k
        top_in = y_in + 0.12 * k
        left = MARGIN_IN["left"] * k / w
        right = 1 - MARGIN_IN["right"] * k / w
        bottom = MARGIN_IN["bottom"] * k / h
        top = max(1 - top_in / h, bottom + 0.1)
        fig.subplots_adjust(left=min(left, 0.3), right=max(right, 0.7), bottom=min(bottom, 0.3), top=top)

    def label_at(self, event):
        """'title' / 'x' / 'y' when the mouse is on the plot title or an axis title, else None."""
        try:
            for a in getattr(self, "_header_artists", []):
                if a.get_gid() == "_plot_title" and a.contains(event)[0]:
                    return "title"
            ax = self.ax
            if ax is not None:
                if ax.xaxis.label.contains(event)[0]:
                    return "x"
                if ax.yaxis.label.contains(event)[0]:
                    return "y"
        except Exception:
            pass
        return None

    def relayout(self):
        """Call after the canvas was resized."""
        if self.ax is not None:
            self._layout()


def _wrap_condition(items: List[str], fig_width_in: float) -> List[str]:
    """Join 'FREQ = 3.60 GHz' items into lines that fit the figure width."""
    if not items:
        return []
    per_line = 4 if fig_width_in >= 6.8 else 2
    sep = "     "
    return [sep.join(items[i:i + per_line]) for i in range(0, len(items), per_line)]
