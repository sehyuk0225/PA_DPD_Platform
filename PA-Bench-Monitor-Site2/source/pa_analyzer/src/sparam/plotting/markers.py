"""Marker overlay: vertical (frequency) / horizontal (level) markers + value panel.

Drawn on top of an already rendered Axes, so dragging a marker only redraws the
overlay, not the whole plot. The value panel shows, per curve, a short line
sample in the curve's colour and line style followed by the value.
"""
from __future__ import annotations

import numpy as np
from matplotlib.lines import Line2D
from matplotlib.offsetbox import AnchoredOffsetbox, DrawingArea, HPacker, TextArea, VPacker

from sparam.utils.freq import fmt_ghz

V_STYLE = dict(color="#333333", linewidth=1.1, linestyle=(0, (5, 3)), zorder=6)
H_STYLE = dict(color="#6a1b9a", linewidth=1.1, linestyle=(0, (5, 3)), zorder=6)
SEL = dict(color="#e65100", linewidth=2.2)   # selected marker (arrow keys move it)
HIT_PX = 7


class MarkerOverlay:
    def __init__(self, controller):
        self.c = controller
        self.ax = None
        self.artists = []
        self.v_lines = []     # rect mode: Line2D per vertical marker
        self.h_lines = []
        self.smith_dots = []  # smith mode: (marker index, Line2D dot)
        self.panel = None
        self.pins = []        # Annotation per point marker ("M")

    # ------------------------------------------------------------------
    def attach(self, ax):
        self.ax = ax
        self.artists, self.v_lines, self.h_lines, self.smith_dots = [], [], [], []
        self.pins = []
        self.draw()

    def clear(self):
        for a in self.artists:
            try:
                a.remove()
            except Exception:
                pass
        self.artists, self.v_lines, self.h_lines, self.smith_dots = [], [], [], []
        self.panel = None
        self.pins = []

    def draw(self):
        ax = self.ax
        if ax is None:
            return
        self.clear()
        smith = self.c.is_smith
        sel = self.c.selected
        for i, hz in enumerate(self.c.markers_hz):
            name = f"M{i + 1}"
            is_sel = sel == ("v", i)
            if smith:
                for line in [l for l in ax.get_lines() if not l.get_label().startswith("_")]:
                    f = getattr(line, "sp_freq_ghz", None)
                    if f is None or not (f.min() <= hz / 1e9 <= f.max()):
                        continue
                    xr = np.interp(hz / 1e9, f, line.get_xdata())
                    yi = np.interp(hz / 1e9, f, line.get_ydata())
                    (p,) = ax.plot([xr], [yi], marker="v", markersize=10 if is_sel else 8, color=line.get_color(),
                                   markeredgecolor=SEL["color"] if is_sel else "black",
                                   markeredgewidth=1.6 if is_sel else 0.6, zorder=7, label="_marker")
                    t = ax.annotate(name, (xr, yi), xytext=(0, 7), textcoords="offset points", ha="center",
                                    fontsize=8.5, fontweight="bold", zorder=7)
                    self.artists += [p, t]
                    self.smith_dots.append((i, p))
            else:
                ln = ax.axvline(hz / 1e9, label="_marker", **dict(V_STYLE, **(SEL if is_sel else {})))
                t = ax.annotate(name, xy=(hz / 1e9, 1.0), xycoords=("data", "axes fraction"), xytext=(3, -12),
                                textcoords="offset points", fontsize=8.5, fontweight="bold", color="#333", zorder=7)
                self.v_lines.append(ln)
                self.artists += [ln, t]
        if not smith:
            for i, lvl in enumerate(self.c.h_markers):
                ln = ax.axhline(lvl, label="_marker", **dict(H_STYLE, **(SEL if sel == ("h", i) else {})))
                t = ax.annotate(f"H{i + 1}", xy=(1.0, lvl), xycoords=("axes fraction", "data"), xytext=(-20, 3),
                                textcoords="offset points", fontsize=8.5, fontweight="bold", color="#6a1b9a", zorder=7)
                self.h_lines.append(ln)
                self.artists += [ln, t]
        for i, pin in enumerate(self.c.pins):           # "M" point markers with a movable callout
            pt = self.c.pin_point(pin)
            if pt is None:
                self.pins.append(None)
                continue
            x, y, _lab, color, text = pt
            is_sel = sel == ("p", i)
            (dot,) = ax.plot([x], [y], marker="o", markersize=6.5, markerfacecolor="#ffeb3b",
                             markeredgecolor=SEL["color"] if is_sel else "#333", markeredgewidth=1.4,
                             linestyle="none", zorder=11, label="_pin")
            ann = ax.annotate(text, xy=(x, y), xytext=(pin["dx"], pin["dy"]), textcoords="offset points",
                              fontsize=8.2, zorder=12, annotation_clip=False,
                              bbox=dict(boxstyle="round,pad=0.35", fc="#fffde7", ec=SEL["color"] if is_sel else "#888",
                                        lw=1.6 if is_sel else 0.9, alpha=0.96),
                              arrowprops=dict(arrowstyle="-", color="#666", lw=0.9, shrinkA=0, shrinkB=4))
            self.pins.append(ann)
            self.artists += [dot, ann]
        if self.c.show_marker_values and (self.c.markers_hz or (self.c.h_markers and not smith)):
            self.panel = self._panel()
            self.artists.append(self.panel)

    def _panel(self):
        rows = []
        unit = "°" if self.c.fmt == "phase" else "dB"
        full = not self.c.panel_collapsed
        for i, hz in enumerate(self.c.markers_hz):
            rows.append(TextArea(f"M{i + 1}  {fmt_ghz(hz)}", textprops=dict(fontsize=8.5, fontweight="bold")))
            if full:
                rows += [self._row(r) for r in self.c.v_readout(hz)]
        if not self.c.is_smith:
            for i, lvl in enumerate(self.c.h_markers):
                rows.append(TextArea(f"H{i + 1}  {lvl:.2f} {unit}",
                                     textprops=dict(fontsize=8.5, fontweight="bold", color="#6a1b9a")))
                if full:
                    rows += [self._row(r) for r in self.c.h_readout(lvl)]
        if not full:
            rows.append(TextArea("(right-click: expand)", textprops=dict(fontsize=7.5, color="#888")))
        box = VPacker(children=rows, align="left", pad=0, sep=2)
        if self.c.panel_xy:                              # dragged by the user: free position
            anchored = AnchoredOffsetbox(loc="upper left", child=box, pad=0.4, borderpad=0, frameon=True,
                                         bbox_to_anchor=tuple(self.c.panel_xy), bbox_transform=self.ax.transAxes)
        else:
            anchored = AnchoredOffsetbox(loc=self.c.panel_loc, child=box, pad=0.4, borderpad=0.6, frameon=True)
        anchored.patch.set_alpha(0.88)
        anchored.patch.set_edgecolor("#999")
        anchored.set_zorder(8)
        self.ax.add_artist(anchored)
        return anchored

    @staticmethod
    def _row(r):
        da = DrawingArea(22, 8, 0, 0)
        da.add_artist(Line2D([1, 21], [4, 4], color=r.color, linestyle=r.linestyle, linewidth=1.8))
        txt = TextArea(r.text, textprops=dict(fontsize=8.2, color=r.color))
        return HPacker(children=[da, txt], align="center", pad=0, sep=4)

    # ------------------------------------------------------------------
    def panel_hit(self, x_px: float, y_px: float) -> bool:
        if self.panel is None:
            return False
        try:
            bb = self.panel.get_window_extent(self.ax.figure.canvas.get_renderer())
        except Exception:
            return False
        return bb.x0 <= x_px <= bb.x1 and bb.y0 <= y_px <= bb.y1

    def panel_corner(self):
        bb = self.panel.get_window_extent(self.ax.figure.canvas.get_renderer())
        fx, fy = self.ax.transAxes.inverted().transform((bb.x0, bb.y1))
        return float(fx), float(fy)

    def pin_hit(self, x_px: float, y_px: float):
        """Index of the point-marker callout under the mouse, or None."""
        for i in range(len(self.pins) - 1, -1, -1):
            ann = self.pins[i]
            if ann is None:
                continue
            try:
                bb = ann.get_bbox_patch().get_window_extent(self.ax.figure.canvas.get_renderer())
            except Exception:
                continue
            if bb.x0 <= x_px <= bb.x1 and bb.y0 <= y_px <= bb.y1:
                return i
        return None

    def hit(self, x_px: float, y_px: float):
        """('v', i) / ('h', i) for a marker line under the mouse, else None."""
        if self.ax is None:
            return None
        best = None
        if self.c.is_smith:
            for i, dot in self.smith_dots:
                px, py = self.ax.transData.transform((dot.get_xdata()[0], dot.get_ydata()[0]))
                d = ((px - x_px) ** 2 + (py - y_px) ** 2) ** 0.5
                if d <= HIT_PX + 3 and (best is None or d < best[2]):
                    best = ("v", i, d)
            return best[:2] if best else None
        for i, ln in enumerate(self.v_lines):
            px = self.ax.transData.transform((ln.get_xdata()[0], 0))[0]
            d = abs(px - x_px)
            if d <= HIT_PX and (best is None or d < best[2]):
                best = ("v", i, d)
        for i, ln in enumerate(self.h_lines):
            py = self.ax.transData.transform((0, ln.get_ydata()[0]))[1]
            d = abs(py - y_px)
            if d <= HIT_PX and (best is None or d < best[2]):
                best = ("h", i, d)
        return best[:2] if best else None
