"""Interactive helpers that work on any matplotlib canvas.

* HoverTooltip  : nearest data point (in screen pixels) -> annotation
* ScrollZoom    : mouse wheel zoom around the cursor
                  (Shift = X only, Ctrl = Y only)
Pan / box-zoom / home come from matplotlib's NavigationToolbar.
"""
from __future__ import annotations

from typing import Callable, Dict, Optional, Tuple

import numpy as np


def nearest_point(ax, x_px: float, y_px: float, max_dist_px: float = 12.0):
    """Return (line, index, dist_px) of the nearest visible data point, or None."""
    best = None
    for line in ax.get_lines():
        if not line.get_visible() or line.get_label().startswith("_"):
            continue
        xd = np.asarray(line.get_xdata(), dtype=float)
        yd = np.asarray(line.get_ydata(), dtype=float)
        if xd.size == 0:
            continue
        pts = ax.transData.transform(np.column_stack([xd, yd]))
        d = np.hypot(pts[:, 0] - x_px, pts[:, 1] - y_px)
        i = int(np.nanargmin(d))
        if d[i] <= max_dist_px and (best is None or d[i] < best[2]):
            best = (line, i, float(d[i]))
    return best


def zoom_limits(lo: float, hi: float, center: float, factor: float) -> Tuple[float, float]:
    """Zoom an interval around `center`. factor < 1 zooms in, > 1 zooms out."""
    return center - (center - lo) * factor, center + (hi - center) * factor


class HoverTooltip:
    def __init__(self, canvas, get_ax: Callable, labels: Callable[[], Dict[str, Tuple[str, str]]],
                 formatter: Optional[Callable] = None):
        """labels() -> {'x': (name, unit), 'y': (name, unit)} for the current graph.
        formatter(line, index) -> str  overrides the tooltip text when it returns a string."""
        self.formatter = formatter
        self.enabled = True
        self.canvas = canvas
        self.get_ax = get_ax
        self.labels = labels
        self.annot = None
        self._ax = None
        self.cid = canvas.mpl_connect("motion_notify_event", self._on_move)

    def _ensure(self, ax):
        if self.annot is None or self._ax is not ax:
            self._ax = ax
            self.annot = ax.annotate(
                "", xy=(0, 0), xytext=(12, 12), textcoords="offset points",
                fontsize=9, zorder=10, annotation_clip=False,
                bbox=dict(boxstyle="round,pad=0.35", fc="#fffbe6", ec="#999", alpha=0.95),
                arrowprops=dict(arrowstyle="-", color="#999"))
            self.annot.set_visible(False)
            self.annot.set_gid("hover")
        return self.annot

    def hide(self):
        if self.annot is not None and self.annot.get_visible():
            self.annot.set_visible(False)
            self.canvas.draw_idle()

    def _on_move(self, event):
        ax = self.get_ax()
        if ax is None:
            return
        annot = self._ensure(ax)
        if event.inaxes is not ax:
            if annot.get_visible():
                annot.set_visible(False)
                self.canvas.draw_idle()
            return
        hit = nearest_point(ax, event.x, event.y) if self.enabled else None
        if hit is None:
            if annot.get_visible():
                annot.set_visible(False)
                self.canvas.draw_idle()
            return
        line, i, _ = hit
        x, y = line.get_xdata()[i], line.get_ydata()[i]
        lab = self.labels()
        (xn, xu), (yn, yu) = lab["x"], lab["y"]
        annot.xy = (x, y)
        custom = self.formatter(line, i) if self.formatter else None
        annot.set_text(custom or f"{line.get_label()}\n{xn} = {x:.3f} {xu}\n{yn} = {y:.2f} {yu}")
        # flip to the left near the right edge
        bbox = ax.get_window_extent()
        annot.set_position((-110, 12) if event.x > bbox.x0 + bbox.width * 0.7 else (12, 12))
        annot.set_visible(True)
        self.canvas.draw_idle()


class ScrollZoom:
    STEP = 0.85

    def __init__(self, canvas, get_ax: Callable, on_change: Optional[Callable] = None):
        self.canvas = canvas
        self.get_ax = get_ax
        self.on_change = on_change
        self.cid = canvas.mpl_connect("scroll_event", self._on_scroll)

    def _on_scroll(self, event):
        ax = self.get_ax()
        if ax is None or event.inaxes is not ax or event.xdata is None:
            return
        f = self.STEP if event.button == "up" else 1 / self.STEP
        key = (event.key or "").lower()
        if "control" not in key and "ctrl" not in key:
            ax.set_xlim(*zoom_limits(*ax.get_xlim(), event.xdata, f))
        if "shift" not in key:
            ax.set_ylim(*zoom_limits(*ax.get_ylim(), event.ydata, f))
        self.canvas.draw_idle()
        if self.on_change:
            self.on_change()
