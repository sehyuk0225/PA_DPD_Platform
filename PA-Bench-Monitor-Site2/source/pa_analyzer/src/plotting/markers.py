"""Marker overlay for Pout-based graphs.

vertical marker   : Pout (dBm)  -> value of every curve at that Pout
horizontal marker : Gain / PAE level -> Pout where each curve crosses it
back-off points   : Psat - BO on every curve
Drawn on top of the rendered Axes so dragging only redraws the overlay.
"""
from __future__ import annotations

from matplotlib.lines import Line2D
from matplotlib.offsetbox import AnchoredOffsetbox, DrawingArea, HPacker, TextArea, VPacker

from core.quantities import QUANTITIES

V_STYLE = dict(color="#333333", linewidth=1.1, linestyle=(0, (5, 3)), zorder=6)
H_STYLE = dict(color="#6a1b9a", linewidth=1.1, linestyle=(0, (5, 3)), zorder=6)
SEL = dict(color="#e65100", linewidth=2.2)
HIT_PX = 7


class MarkerOverlay:
    def __init__(self, controller):
        self.c = controller
        self.ax = None
        self.artists, self.v_lines, self.h_lines = [], [], []
        self.panel = None
        self.pins = []        # Annotation per point marker ("M")

    def attach(self, ax):
        self.ax = ax
        self.artists, self.v_lines, self.h_lines, self.panel = [], [], [], None
        self.pins = []
        self.draw()

    def clear(self):
        for a in self.artists:
            try:
                a.remove()
            except Exception:
                pass
        self.artists, self.v_lines, self.h_lines, self.panel = [], [], [], None
        self.pins = []

    @property
    def k(self) -> float:
        from plotting.renderer import font_scale
        return font_scale(self.ax.figure) if self.ax is not None else 1.0

    def draw(self):
        ax, c = self.ax, self.c
        if ax is None:
            return
        self.clear()
        sel = c.selected
        for i, x in enumerate(c.v_markers):
            ln = ax.axvline(x, label="_marker", **dict(V_STYLE, **(SEL if sel == ("v", i) else {})))
            t = ax.annotate(f"M{i + 1}", xy=(x, 1.0), xycoords=("data", "axes fraction"), xytext=(3, -12),
                            textcoords="offset points", fontsize=8.5 * self.k, fontweight="bold", color="#333", zorder=7)
            self.v_lines.append(ln)
            self.artists += [ln, t]
        for i, y in enumerate(c.cur_h):
            ln = ax.axhline(y, label="_marker", **dict(H_STYLE, **(SEL if sel == ("h", i) else {})))
            t = ax.annotate(f"H{i + 1}", xy=(1.0, y), xycoords=("axes fraction", "data"), xytext=(-20, 3),
                            textcoords="offset points", fontsize=8.5 * self.k, fontweight="bold", color="#6a1b9a", zorder=7)
            self.h_lines.append(ln)
            self.artists += [ln, t]
        for x, y, color, text in c.backoff_points():
            (p,) = ax.plot([x], [y], marker="D", markersize=6.5, markerfacecolor="white", markeredgecolor=color,
                           markeredgewidth=1.6, linestyle="none", zorder=8, label="_bo")
            t = ax.annotate(text, (x, y), xytext=(5, -11), textcoords="offset points", fontsize=7.5 * self.k,
                            color=color, zorder=8)
            self.artists += [p, t]
        for i, p in enumerate(c.cur_pins):
            sel = c.selected == ("p", i)
            (dot,) = ax.plot([p["x"]], [p["y"]], marker="o", markersize=6.5 * self.k, markerfacecolor="#ffeb3b",
                             markeredgecolor="#e65100" if sel else "#333", markeredgewidth=1.4, linestyle="none",
                             zorder=11, label="_pin")
            ann = ax.annotate(c.pin_text(p), xy=(p["x"], p["y"]), xytext=(p["dx"], p["dy"]), textcoords="offset points",
                              fontsize=8.2 * self.k * p["size"], zorder=12, annotation_clip=False,
                              bbox=dict(boxstyle="round,pad=0.35", fc="#fffde7", ec="#e65100" if sel else "#888",
                                        lw=1.6 if sel else 0.9, alpha=0.96),
                              arrowprops=dict(arrowstyle="-", color="#666", lw=0.9, shrinkA=0, shrinkB=4))
            ann.set_gid("pin")
            self.pins.append(ann)
            self.artists += [dot, ann]
        if c.show_marker_values and (c.v_markers or c.cur_h):
            self.panel = self._panel()
            self.artists.append(self.panel)

    def _panel(self):
        c = self.c
        unit = QUANTITIES[c.graph.y].unit
        rows, full = [], not c.panel_collapsed
        for i, x in enumerate(c.v_markers):
            rows.append(TextArea(f"M{i + 1}  Pout {x:.2f} dBm", textprops=dict(fontsize=8.5 * self.k, fontweight="bold")))
            if full:
                rows += [self._row(r) for r in c.v_readout(x)]
        for i, y in enumerate(c.cur_h):
            rows.append(TextArea(f"H{i + 1}  {QUANTITIES[c.graph.y].label} {y:.2f} {unit}",
                                 textprops=dict(fontsize=8.5 * self.k, fontweight="bold", color="#6a1b9a")))
            if full:
                rows += [self._row(r) for r in c.h_readout(y)]
        if not full:
            rows.append(TextArea("(right-click: expand)", textprops=dict(fontsize=7.5 * self.k, color="#888")))
        box = VPacker(children=rows, align="left", pad=0, sep=2)
        if c.panel_xy:                               # dragged by the user: free position
            anchored = AnchoredOffsetbox(loc="upper left", child=box, pad=0.4, borderpad=0, frameon=True,
                                         bbox_to_anchor=tuple(c.panel_xy), bbox_transform=self.ax.transAxes)
        else:
            anchored = AnchoredOffsetbox(loc=c.panel_loc, child=box, pad=0.4, borderpad=0.6, frameon=True)
        anchored.patch.set_alpha(0.88)
        anchored.patch.set_edgecolor("#999")
        anchored.set_zorder(9)
        self.ax.add_artist(anchored)
        return anchored

    def _row(self, r):
        da = DrawingArea(22 * self.k, 8 * self.k, 0, 0)
        if r.linestyle == "sim":
            da.add_artist(Line2D([1, 21 * self.k], [4 * self.k] * 2, color=r.color, linewidth=3.0 * self.k, alpha=0.6))
        else:
            da.add_artist(Line2D([1, 21 * self.k], [4 * self.k] * 2, color=r.color, linewidth=1.2))
            da.add_artist(Line2D([11 * self.k], [4 * self.k], color=r.color, marker="o", markersize=4.5 * self.k))
        return HPacker(children=[da, TextArea(r.text, textprops=dict(fontsize=8.2 * self.k, color=r.color))],
                       align="center", pad=0, sep=4)

    def panel_corner(self):
        """Upper-left corner of the value box in axes fraction (where a drag starts from)."""
        bb = self.panel.get_window_extent(self.ax.figure.canvas.get_renderer())
        fx, fy = self.ax.transAxes.inverted().transform((bb.x0, bb.y1))
        return float(fx), float(fy)

    def panel_hit(self, x_px, y_px) -> bool:
        if self.panel is None:
            return False
        try:
            bb = self.panel.get_window_extent(self.ax.figure.canvas.get_renderer())
        except Exception:
            return False
        return bb.x0 <= x_px <= bb.x1 and bb.y0 <= y_px <= bb.y1

    def pin_hit(self, event):
        """Index of the callout box under the mouse, or None."""
        for i in range(len(self.pins) - 1, -1, -1):
            try:
                bb = self.pins[i].get_bbox_patch().get_window_extent(self.ax.figure.canvas.get_renderer())
            except Exception:
                continue
            if bb.x0 <= event.x <= bb.x1 and bb.y0 <= event.y <= bb.y1:
                return i
        return None

    def hit(self, x_px, y_px):
        if self.ax is None:
            return None
        best = None
        for i, ln in enumerate(self.v_lines):
            d = abs(self.ax.transData.transform((ln.get_xdata()[0], 0))[0] - x_px)
            if d <= HIT_PX and (best is None or d < best[2]):
                best = ("v", i, d)
        for i, ln in enumerate(self.h_lines):
            d = abs(self.ax.transData.transform((0, ln.get_ydata()[0]))[1] - y_px)
            if d <= HIT_PX and (best is None or d < best[2]):
                best = ("h", i, d)
        return best[:2] if best else None
