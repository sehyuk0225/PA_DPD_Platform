"""Renderer: PlotRequest -> matplotlib Figure (same output on screen and in PNG)."""
from __future__ import annotations

from matplotlib.figure import Figure

import plotting.fonts  # noqa: F401  (same fonts as the PA tab: Arial Narrow + Korean)

from sparam.core.models import PlotRequest
from sparam.plotting.smith import draw_smith_grid


class PlotRenderer:
    def __init__(self, figure: Figure):
        self.figure = figure
        self.ax = None
        self.mode = "rect"
        self.series_keys = []      # legend entry i -> Series.key

    def render(self, req: PlotRequest):
        fig = self.figure
        fig.clear()
        fig.set_facecolor("white")
        ax = fig.add_subplot(111)
        self.ax, self.mode = ax, req.mode
        self.series_keys = [s.key for s in req.series]
        if req.mode == "smith":
            draw_smith_grid(ax)
        if not req.series:
            ax.text(0.5, 0.5, "Add CSV / Excel / Touchstone (.s2p) files" if req.mode == "rect"
                    else "Smith Chart needs magnitude + phase (e.g. .s2p)",
                    ha="center", va="center", transform=ax.transAxes, color="#888", fontsize=11)
        for s in req.series:
            (line,) = ax.plot(s.x, s.y, color=s.color, linestyle=s.linestyle, linewidth=s.linewidth, label=s.label,
                              alpha=getattr(s, "alpha", 1.0), zorder=getattr(s, "zorder", 2.0))
            line.sp_freq_ghz = s.freq_ghz       # used by hover / Smith markers
            line.sp_z0 = s.z0
            line.sp_key = tuple(s.key)          # (file uid, param): click on a curve -> that file
            line.set_gid(f"{s.key[0]}|{s.key[1]}" if s.key else None)
        ax.set_title(req.title, fontsize=12.5, fontweight="bold")
        if req.mode == "rect":
            ax.set_xlabel(req.x_label, fontsize=10.5, fontweight="bold")
            ax.set_ylabel(req.y_label, fontsize=10.5, fontweight="bold")
            ax.grid(True, which="major", alpha=0.35)
            ax.minorticks_on()
            ax.grid(True, which="minor", alpha=0.12)
            ax.tick_params(labelsize=9.5)
        n = len(req.series)
        if n:
            ax.legend(loc="lower right" if req.mode == "smith" else "best",
                      fontsize=8.5 if n > 6 else 9.5, ncol=2 if n > 10 else 1, framealpha=0.9)
        if req.xlim:
            ax.set_xlim(*req.xlim)
        if req.ylim:
            ax.set_ylim(*req.ylim)
        if req.mode == "smith":
            fig.subplots_adjust(left=0.03, right=0.97, bottom=0.03, top=0.93)
        else:
            fig.subplots_adjust(left=0.1, right=0.97, bottom=0.11, top=0.92)
        return ax

    def legend_index_at(self, event):
        """Index of the legend entry under a mouse event (for rename), or None."""
        leg = self.ax.get_legend() if self.ax is not None else None
        if leg is None:
            return None
        for i, t in enumerate(leg.get_texts()):
            if t.contains(event)[0]:
                return i
        for i, h in enumerate(getattr(leg, "legend_handles", None) or getattr(leg, "legendHandles", [])):
            try:
                if h.contains(event)[0]:
                    return i
            except Exception:
                pass
        return None

    def relayout(self):
        pass
