"""One interactive plot pane (up to 4 are shown in the main window).

Mouse
  left drag on empty area   : pan
  left drag on a marker     : move it (frequency snaps to the marker step, default 0.05 GHz)
  right drag                : box zoom
  wheel                     : zoom (Shift = X only, Ctrl = Y only)
  double-click legend entry : rename that curve
  click a marker            : select it -> arrow keys move it (Left/Right: GHz step, Up/Down: dB step)
  right-click               : menu on marker value box / legend entry / marker line, else box zoom
"""
from __future__ import annotations

import math
from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QSizePolicy, QVBoxLayout

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle

from sparam.app.controller import Controller
from sparam.plotting.interaction import HoverTooltip, ScrollZoom
from sparam.plotting.markers import MarkerOverlay
from sparam.plotting.renderer import PlotRenderer
from sparam.plotting.smith import gamma_to_z

# Korean IME (Hangul mode) turns v/h/f into jamo: accept both
_HANGUL_KEYS = {"ㅍ": "v", "ㅗ": "h", "ㄹ": "f", "ㅡ": "m", "V": "v", "H": "h", "F": "f", "M": "m"}

ACTIVE_CSS = "QFrame#plotpane { border: 2px solid #1f77b4; border-radius: 3px; }"
IDLE_CSS = "QFrame#plotpane { border: 1px solid #c8c8c8; border-radius: 3px; }"


class PlotPane(QFrame):
    def __init__(self, index: int, controller: Controller, on_activate: Callable, on_changed: Callable,
                 on_rename: Callable, on_context: Callable = None, on_key: Callable = None, parent=None):
        super().__init__(parent)
        self.index = index
        self.c = controller
        self._on_activate, self._on_changed, self._on_rename = on_activate, on_changed, on_rename
        self._on_context = on_context or (lambda *a: None)
        self._on_key = on_key or (lambda *a: None)
        # equal share of the grid regardless of content -> all panes the same size
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.setObjectName("plotpane")
        self.setStyleSheet(IDLE_CSS)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)

        self.figure = Figure(figsize=(7, 4.8), dpi=100)
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.canvas.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.canvas.setFocusPolicy(Qt.StrongFocus)
        lay.addWidget(self.canvas)

        self.renderer = PlotRenderer(self.figure)
        self.overlay = MarkerOverlay(controller)
        self.hover = HoverTooltip(self.canvas, lambda: self.renderer.ax, controller.axis_labels, self._hover_text)
        self.zoom = ScrollZoom(self.canvas, lambda: self.renderer.ax)
        self.mouse = None        # (x_px, y_px, xdata, ydata) while over the axes
        self._drag = None        # ('v'|'h', i) marker being moved
        self._panning = False
        self._box = None         # (x0, y0, Rectangle) right-drag zoom
        self._updating = False
        self._click = None       # left press on empty plot area -> a click if released in place
        self._pin_drag = None    # (index, start dx, dy, start mouse x, y)
        self._panel_drag = None  # (corner fx, fy, mouse x, y, box w, h in axes fraction)
        self.on_pick = lambda pane, key: None     # set by the main window: curve clicked (key) / empty (None)
        self.on_open_data = lambda pane, key, f_ghz: None   # double-click on a curve -> data editor
        self.on_marker_value = lambda pane, hit: None      # double-click on a V / H marker -> type it
        for name, fn in (("motion_notify_event", self._move), ("button_press_event", self._press),
                         ("button_release_event", self._release)):
            self.canvas.mpl_connect(name, fn)
        self.canvas.mpl_connect("axes_leave_event", self._leave)
        self.canvas.mpl_connect("key_press_event", self._key)

    # ------------------------------------------------------------ drawing
    def set_active(self, on: bool) -> None:
        self.setStyleSheet(ACTIVE_CSS if on else IDLE_CSS)

    def render(self) -> None:
        self._updating = True
        try:
            self.hover.hide()
            ax = self.renderer.render(self.c.build_plot_request())
            if self.c.is_smith:
                xl, yl = self.c.current_limits()
                ax.set_xlim(*xl); ax.set_ylim(*yl)
            self.overlay.attach(ax)
            ax.callbacks.connect("xlim_changed", self._limits_changed)
            ax.callbacks.connect("ylim_changed", self._limits_changed)
            self.canvas.draw_idle()
        finally:
            self._updating = False

    def redraw_markers(self) -> None:
        self.overlay.draw()
        self.canvas.draw_idle()

    def _limits_changed(self, ax):
        if self._updating:
            return
        self.c.set_view(ax.get_xlim(), ax.get_ylim())
        self._on_changed(self, "range")

    @property
    def ax(self):
        return self.renderer.ax

    # ------------------------------------------------------------ markers / keys
    def cursor_data(self):
        if self.mouse is not None:
            return self.mouse[2], self.mouse[3]
        (x0, x1), (y0, y1) = self.ax.get_xlim(), self.ax.get_ylim()
        return (x0 + x1) / 2, (y0 + y1) / 2

    def _freq_at(self, x, y) -> Optional[float]:
        hz = self.c.snap_freq_smith(x, y) if self.c.is_smith else x * 1e9
        return self.c.snap(hz) if hz else None

    def add_v_marker(self) -> bool:
        hz = self._freq_at(*self.cursor_data())
        if hz and self.c.add_marker(hz) is not None:
            self.redraw_markers()
            self._on_changed(self, "markers")
            return True
        return False

    def add_h_marker(self) -> bool:
        if self.c.is_smith:
            return False
        if self.c.add_h_marker(self.cursor_data()[1]) is not None:
            self.redraw_markers()
            return True
        return False

    def nearest_curve_point(self, x_px, y_px, max_px: float = 40):
        """(series key, frequency GHz) of the curve point closest to the mouse, or None."""
        import numpy as np
        best = None
        for line in self.ax.get_lines() if self.ax is not None else []:
            key, f = getattr(line, "sp_key", None), getattr(line, "sp_freq_ghz", None)
            if not key or f is None or not line.get_visible():
                continue
            xy = np.column_stack([line.get_xdata(), line.get_ydata()]).astype(float)
            ok = np.isfinite(xy).all(axis=1)
            if not ok.any():
                continue
            px = self.ax.transData.transform(xy[ok])
            d = np.hypot(px[:, 0] - x_px, px[:, 1] - y_px)
            i = int(np.argmin(d))
            if d[i] <= max_px and (best is None or d[i] < best[0]):
                best = (d[i], key, float(np.asarray(f)[ok][i]))
        return (best[1], best[2]) if best else None

    def add_pin(self) -> bool:
        """M: callout on the curve point nearest to the mouse."""
        if self.ax is None or self.mouse is None:
            return False
        hit = self.nearest_curve_point(self.mouse[0], self.mouse[1])
        if hit is None:
            return False
        i = self.c.add_pin(*hit)
        if i is not None:
            self.c.select_marker(("p", i))
            self.redraw_markers()
            self._on_changed(self, "markers")
        return True

    def delete_marker(self) -> None:
        hit = self.c.selected or (self.overlay.hit(self.mouse[0], self.mouse[1]) if self.mouse else None)
        if hit and hit[0] == "p":
            self.c.remove_pin(hit[1])
        elif hit:
            self.c.remove_marker(*hit)
        elif self.c.h_markers:
            self.c.h_markers.pop()
        elif self.c.markers_hz:
            self.c.markers_hz.pop()
        self.redraw_markers()
        self._on_changed(self, "markers")

    def nudge(self, direction: int, axis: str) -> bool:
        if self.c.nudge_selected(direction, axis):
            self.redraw_markers()
            self._on_changed(self, "markers")
            return True
        return False

    def _key(self, event):
        key = _HANGUL_KEYS.get(event.key, event.key) if event.key else ""
        moves = {"left": (-1, "x"), "right": (1, "x"), "down": (-1, "y"), "up": (1, "y")}
        if key in moves:
            self.nudge(*moves[key])
        elif key in ("v", "h", "f", "m", "delete", "backspace", "escape"):
            self._on_key(self, key)

    def rename_series(self, i: int, text: str) -> None:
        if 0 <= i < len(self.renderer.series_keys):
            self.c.set_label(self.renderer.series_keys[i], text)
            self._on_changed(self, "labels")

    def current_label(self, i: int) -> str:
        leg = self.ax.get_legend()
        return leg.get_texts()[i].get_text() if leg is not None else ""

    def _hover_text(self, line, i):
        f = getattr(line, "sp_freq_ghz", None)
        if f is None or not self.c.is_smith:
            return None
        g = complex(line.get_xdata()[i], line.get_ydata()[i])
        z = gamma_to_z(g, getattr(line, "sp_z0", 50.0))
        return (f"{line.get_label()}\nf = {f[i]:.3f} GHz\nΓ = {abs(g):.3f} ∠ "
                f"{math.degrees(math.atan2(g.imag, g.real)):.1f}°\n"
                f"Z = {z.real:.1f} {'+' if z.imag >= 0 else '-'} j{abs(z.imag):.1f} Ω")

    # ------------------------------------------------------------ mouse
    def _leave(self, _event):
        self.mouse = None

    def _press(self, event):
        self._on_activate(self)
        ax = self.ax
        if ax is not None and event.inaxes is not ax and event.button == 1 and self.overlay.ax is not None:
            pin = self.overlay.pin_hit(event.x, event.y)       # callouts may stick out of the plot area
            if pin is not None:
                p = self.c.pins[pin]
                self._pin_drag = (pin, p["dx"], p["dy"], event.x, event.y)
                self.c.select_marker(("p", pin))
                self.redraw_markers()
                return
        if ax is None or event.inaxes is not ax:
            return
        if getattr(event, "dblclick", False):
            hit = self.overlay.hit(event.x, event.y) if event.button == 1 else None
            if hit:                                         # marker (even over the legend): type the value
                self._click = self._drag = None
                self.c.select_marker(hit)
                self.on_marker_value(self, hit)
                return
            i = self.renderer.legend_index_at(event)
            if i is not None:
                self._on_rename(self, i)
                return
            from plotting.pick import curve_at
            gid = curve_at(ax, event.x, event.y) if event.button == 1 else None
            if gid:
                self._click = None
                uid, _, param = gid.partition("|")
                hit = self.nearest_curve_point(event.x, event.y)
                self.on_open_data(self, (int(uid), param), hit[1] if hit else None)
            return
        try:
            self.canvas.setFocus()
        except Exception:
            pass
        if event.button == 1:
            pin = self.overlay.pin_hit(event.x, event.y)
            if pin is not None:                           # grab a point-marker callout -> move it
                p = self.c.pins[pin]
                self._pin_drag = (pin, p["dx"], p["dy"], event.x, event.y)
                self.c.select_marker(("p", pin))
                self.hover.enabled = False
                self.hover.hide()
                self.redraw_markers()
                return
            if self.overlay.hit(event.x, event.y) is None and self.overlay.panel_hit(event.x, event.y):
                # grab the marker value box -> move it (a marker line under the box still wins)
                fx, fy = self.overlay.panel_corner()
                pb = self.overlay.panel.get_window_extent(self.canvas.get_renderer())
                self._panel_drag = (fx, fy, event.x, event.y, pb.width / ax.bbox.width, pb.height / ax.bbox.height)
                self.hover.enabled = False
                self.hover.hide()
                self.canvas.setCursor(Qt.ClosedHandCursor)
                return
            hit = self.overlay.hit(event.x, event.y)
            self.hover.enabled = False
            self.hover.hide()
            if hit != self.c.selected:
                self.c.select_marker(hit)
                self.redraw_markers()
            if hit:
                self._drag = hit
            else:
                self._click = (event.x, event.y)
                self._panning = True
                ax.start_pan(event.x, event.y, 1)
                self.canvas.setCursor(Qt.ClosedHandCursor)
        elif event.button == 3:
            pin = self.overlay.pin_hit(event.x, event.y)
            if pin is not None:
                self.c.select_marker(("p", pin))
                self.redraw_markers()
                self._on_context(self, ("pin", pin), event)
                return
            if self.overlay.panel_hit(event.x, event.y):
                self._on_context(self, ("panel", None), event)
                return
            i = self.renderer.legend_index_at(event)
            if i is not None:
                self._on_context(self, ("legend", i), event)
                return
            hit = self.overlay.hit(event.x, event.y)
            if hit:
                self.c.select_marker(hit)
                self.redraw_markers()
                self._on_context(self, ("marker", hit), event)
                return
            rect = Rectangle((event.xdata, event.ydata), 0, 0, fill=True, facecolor="#1f77b4", alpha=0.12,
                             edgecolor="#1f77b4", linewidth=1, zorder=20, label="_box")
            ax.add_patch(rect)
            self._box = (event.xdata, event.ydata, rect)
            self._box_px = (event.x, event.y)      # right-click without dragging -> plot menu

    def _move(self, event):
        ax = self.ax
        if ax is None:
            return
        if event.inaxes is ax and event.xdata is not None:
            self.mouse = (event.x, event.y, event.xdata, event.ydata)
        if self._pin_drag:
            i, dx0, dy0, x0, y0 = self._pin_drag
            k = 72.0 / self.figure.dpi                    # pixels -> points
            self.c.move_pin(i, dx0 + (event.x - x0) * k, dy0 + (event.y - y0) * k)
            self.redraw_markers()
            return
        if self._panel_drag:
            fx, fy, x0, y0, bw, bh = self._panel_drag
            bb = ax.bbox
            nx = min(max(fx + (event.x - x0) / bb.width, 0.0), max(1.0 - bw, 0.0))
            ny = min(max(fy + (event.y - y0) / bb.height, min(bh, 1.0)), 1.0)
            self.c.panel_xy = (nx, ny)
            self.redraw_markers()
            return
        if self._panning:
            ax.drag_pan(1, None, event.x, event.y)
            self.canvas.draw_idle()
            return
        if self._drag and event.inaxes is ax and event.xdata is not None:
            kind, i = self._drag
            if kind == "v":
                hz = self._freq_at(event.xdata, event.ydata)
                if hz:
                    self.c.move_marker(i, hz)
            else:
                self.c.move_h_marker(i, event.ydata)
            self.redraw_markers()
            return
        if self._box and event.inaxes is ax and event.xdata is not None:
            x0, y0, rect = self._box
            rect.set_bounds(min(x0, event.xdata), min(y0, event.ydata), abs(event.xdata - x0), abs(event.ydata - y0))
            self.canvas.draw_idle()
            return
        if event.inaxes is ax and (self.overlay.panel_hit(event.x, event.y) or
                                   self.overlay.pin_hit(event.x, event.y) is not None):
            self.canvas.setCursor(Qt.OpenHandCursor)
            return
        if event.inaxes is ax:
            over = self.overlay.hit(event.x, event.y)
            self.canvas.setCursor(Qt.SizeHorCursor if over and over[0] == "v" and not self.c.is_smith else
                                  Qt.SizeVerCursor if over and over[0] == "h" else
                                  Qt.PointingHandCursor if over else Qt.ArrowCursor)

    def _release(self, event):
        ax = self.ax
        click, self._click = self._click, None
        if click and event.x is not None and abs(event.x - click[0]) <= 3 and abs(event.y - click[1]) <= 3:
            from plotting.pick import curve_at              # click on a curve -> that file; empty -> none
            gid = curve_at(ax, click[0], click[1]) if ax is not None else None
            key = None
            if gid:
                uid, _, param = gid.partition("|")
                key = (int(uid), param)
            self.on_pick(self, key)
        if self._pin_drag:
            self._pin_drag = None
            self._on_changed(self, "markers")
        if self._panel_drag:
            self._panel_drag = None
            self.canvas.setCursor(Qt.ArrowCursor)
            self._on_changed(self, "markers")
        if self._panning:
            self._panning = False
            ax.end_pan()
            self.canvas.setCursor(Qt.ArrowCursor)
        if self._drag:
            self._drag = None
            self._on_changed(self, "markers")
        if self._box:
            x0, y0, rect = self._box
            self._box = None
            x1 = event.xdata if event.inaxes is ax and event.xdata is not None else x0
            y1 = event.ydata if event.inaxes is ax and event.ydata is not None else y0
            rect.remove()
            p0 = ax.transData.transform((x0, y0))
            p1 = ax.transData.transform((x1, y1))
            if abs(p1[0] - p0[0]) > 5 and abs(p1[1] - p0[1]) > 5:
                ax.set_xlim(min(x0, x1), max(x0, x1))
                ax.set_ylim(min(y0, y1), max(y0, y1))
                self.canvas.draw_idle()
            else:
                self.canvas.draw_idle()
                bx, by = getattr(self, "_box_px", (event.x, event.y))
                if event.x is None or (abs(event.x - bx) <= 5 and abs(event.y - by) <= 5):
                    from plotting.pick import curve_at        # right-click (no drag): the plot menu
                    gid = curve_at(ax, bx, by)
                    key = None
                    if gid:
                        uid, _, param = gid.partition("|")
                        key = (int(uid), param)
                    self.hover.enabled = True
                    self._on_context(self, ("plot", key), event)
                    return
        self.hover.enabled = True
