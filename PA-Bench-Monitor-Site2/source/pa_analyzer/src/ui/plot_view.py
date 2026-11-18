"""Interactive plot widget (Pout graphs).

Mouse:  left drag = pan · on a marker = move it · right drag = box zoom · wheel = zoom
        right-click on the value box / a marker = menu · click a marker = select (arrow keys move it)
Keys (plot focused, also in Korean IME mode):  V / H markers · F fit all · Del delete · arrows
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QSizePolicy, QVBoxLayout, QWidget

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle

from plotting.interaction import HoverTooltip, ScrollZoom
from plotting.markers import MarkerOverlay
from plotting.renderer import PlotRenderer

_KEYS = {"ㅍ": "v", "ㅗ": "h", "ㄹ": "f", "ㅡ": "m", "V": "v", "H": "h", "F": "f", "M": "m"}


ACTIVE_CSS = "QWidget#plotview { border: 2px solid #1f77b4; }"
IDLE_CSS = "QWidget#plotview { border: 1px solid #d0d0d0; }"


class PlotView(QWidget):
    """One plot pane. Every controller access runs inside `using_pane(self.idx)`."""

    def __init__(self, controller, on_changed: Callable, on_context: Callable, on_key: Callable,
                 idx: int = 0, on_activate: Callable = None, parent=None):
        super().__init__(parent)
        self.c = controller
        self.idx = idx
        self._on_changed, self._on_context, self._on_key = on_changed, on_context, on_key
        self._on_activate = on_activate or (lambda v: None)
        self.setObjectName("plotview")
        self.setAcceptDrops(True)                  # rows dragged from the Datasets list -> shown on this plot
        self.on_drop_datasets = lambda view: None
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(IDLE_CSS)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)   # equal share of the grid
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        self.figure = Figure(figsize=(8, 5.5), dpi=100)
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.canvas.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.canvas.setFocusPolicy(Qt.StrongFocus)
        lay.addWidget(self.canvas)
        self.renderer = PlotRenderer(self.figure)
        self.overlay = MarkerOverlay(controller)
        self.hover = HoverTooltip(self.canvas, lambda: self.renderer.ax, self._labels)
        self.zoom = ScrollZoom(self.canvas, lambda: self.renderer.ax)
        self.setMinimumSize(160, 120)
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(120)
        self._resize_timer.timeout.connect(self.render)   # re-render -> fonts follow the new size
        self.mouse = None
        self._pin_drag = None     # (index, start dx, start dy, start mouse px, start mouse py)
        self._drag = None
        self._panning = False
        self._box = None
        self._panel_drag = None     # (start corner fx, fy, start mouse px, py) while moving the value box
        self._click = None          # left press on empty plot area: (x, y) -> click if released in place
        self.on_pick = lambda key, add=False: None
        self.on_edit_labels = lambda part: None       # double-click on the plot title / an axis title
        self.on_open_data = lambda key, x: None     # double-click on a curve -> data editor
        self.on_marker_value = lambda hit: None     # double-click on a V / H marker -> type its value
        self._updating = False
        for name, fn in (("motion_notify_event", self._move), ("button_press_event", self._press),
                         ("button_release_event", self._release), ("key_press_event", self._key),
                         ("resize_event", self._resized)):
            self.canvas.mpl_connect(name, fn)
        self.canvas.mpl_connect("axes_leave_event", lambda e: setattr(self, "mouse", None))
        self.canvas.mpl_connect("figure_leave_event", lambda e: setattr(self, "mouse", None))

    @property
    def ax(self):
        return self.renderer.ax

    def pane(self):
        return self.c.using_pane(self.idx)

    def _labels(self):
        with self.pane():
            return self.c.axis_labels()

    def set_active(self, on: bool):
        self.setStyleSheet(ACTIVE_CSS if on else IDLE_CSS)

    # ------------------------------------------------------------ drawing
    def render(self):
        with self.pane():
            return self._impl_render()

    def _impl_render(self):
        self._updating = True
        try:
            self.hover.hide()
            ax = self.renderer.render(self.c.build_plot_request(), show_condition=self.c.show_condition_in_plot)
            if self.c.build_plot_request() is not None:
                self.overlay.attach(ax)
                ax.callbacks.connect("xlim_changed", self._limits)
                ax.callbacks.connect("ylim_changed", self._limits)
            else:
                self.overlay.ax = None
            self.canvas.draw_idle()
        finally:
            self._updating = False

    def redraw_markers(self):
        with self.pane():
            return self._impl_redraw_markers()

    def _impl_redraw_markers(self):
        if self.overlay.ax is not None:
            self.overlay.draw()
            self.canvas.draw_idle()

    def _resized(self, _e):
        self.renderer.relayout()
        self.canvas.draw_idle()
        try:
            self._resize_timer.start()
        except Exception:
            pass

    def _limits(self, ax):
        with self.pane():
            return self._impl__limits(ax)

    def _impl__limits(self, ax):
        if not self._updating:
            self.c.set_ranges(tuple(ax.get_xlim()), tuple(ax.get_ylim()))
            self._on_changed("range")

    # ------------------------------------------------------------ markers
    def cursor_data(self):
        with self.pane():
            return self._impl_cursor_data()

    def _impl_cursor_data(self):
        if self.mouse is not None:
            return self.mouse[2], self.mouse[3]
        (x0, x1), (y0, y1) = self.ax.get_xlim(), self.ax.get_ylim()
        return (x0 + x1) / 2, (y0 + y1) / 2

    def add_v(self):
        with self.pane():
            return self._impl_add_v()

    def _impl_add_v(self):
        if self.overlay.ax is not None and self.c.add_v_marker(self.cursor_data()[0]) is not None:
            self.redraw_markers()
            self._on_changed("markers")

    def add_h(self):
        with self.pane():
            return self._impl_add_h()

    def _impl_add_h(self):
        if self.overlay.ax is not None and self.c.add_h_marker(self.cursor_data()[1]) is not None:
            self.redraw_markers()
            self._on_changed("markers")

    def add_pin(self):
        with self.pane():
            return self._impl_add_pin()

    def _impl_add_pin(self) -> bool:
        """M: fix a callout on the curve point nearest to the mouse."""
        from plotting.interaction import nearest_point
        if self.overlay.ax is None or self.mouse is None:
            return False
        hit = nearest_point(self.ax, self.mouse[0], self.mouse[1], max_dist_px=40)
        if hit is None:
            return False
        line, i, _ = hit
        idx = self.c.add_pin(line.get_label(), line.get_xdata()[i], line.get_ydata()[i])
        if idx is not None:
            self.c.select_marker(("p", idx))
            self.redraw_markers()
        return idx is not None

    # ------------------------------------------------------------ drop from the Datasets list
    @staticmethod
    def _from_list(event) -> bool:
        try:
            from PySide6.QtWidgets import QListWidget
            return isinstance(event.source(), QListWidget)
        except Exception:
            return False

    def dragEnterEvent(self, event):
        if self._from_list(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self._from_list(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        if self._from_list(event):
            event.acceptProposedAction()
            self.on_drop_datasets(self)
        else:
            event.ignore()

    def delete_marker(self):
        with self.pane():
            return self._impl_delete_marker()

    def _impl_delete_marker(self):
        hit = self.c.selected or (self.overlay.hit(self.mouse[0], self.mouse[1]) if self.mouse else None)
        if hit:
            self.c.remove_marker(*hit)
        elif self.c.cur_h:
            self.c.cur_h.pop()
        elif self.c.v_markers:
            self.c.v_markers.pop()
        self.redraw_markers()
        self._on_changed("markers")

    def nudge(self, direction, axis):
        with self.pane():
            return self._impl_nudge(direction, axis)

    def _impl_nudge(self, direction, axis):
        if self.c.nudge_selected(direction, axis):
            self.redraw_markers()
            self._on_changed("markers")

    def _key(self, event):
        key = _KEYS.get(event.key, event.key) if event.key else ""
        moves = {"left": (-1, "x"), "right": (1, "x"), "down": (-1, "y"), "up": (1, "y")}
        if key in moves:
            self.nudge(*moves[key])
        elif key in ("v", "h", "f", "m", "delete", "backspace"):
            self._on_key(key)

    # ------------------------------------------------------------ mouse
    def _press(self, event):
        with self.pane():
            return self._impl__press(event)

    def _impl__press(self, event):
        ax = self.ax
        if event.inaxes is ax:
            self._on_activate(self)
        if getattr(event, "dblclick", False) and event.button == 1 and ax is not None:
            part = self.renderer.label_at(event)          # plot title / axis title -> type your own text
            if part:
                self._click = self._drag = None
                self.on_edit_labels(part)
                return
        if getattr(event, "dblclick", False) and event.button == 1 and ax is not None and event.inaxes is ax:
            hit = self.overlay.hit(event.x, event.y) if self.overlay.ax is not None else None
            if hit and hit[0] in ("v", "h"):
                self._click = self._drag = None
                with self.pane():
                    self.c.select_marker(hit)
                self.on_marker_value(hit)                # type the exact Pout / level
                return
            from plotting.pick import curve_at
            gid = curve_at(ax, event.x, event.y)
            if gid:
                self._click = None
                self.on_open_data(gid, event.xdata)     # see / edit the numbers of that curve
                return
        pin = self.overlay.pin_hit(event) if (ax is not None and self.overlay.ax is not None) else None
        if ax is None or self.overlay.ax is None or (event.inaxes is not ax and pin is None):
            return                                        # (a callout may stick out of the plot area)
        try:
            self.canvas.setFocus()
        except Exception:
            pass
        if pin is not None and event.button == 1:
            p = self.c.cur_pins[pin]
            self._pin_drag = (pin, p["dx"], p["dy"], event.x, event.y)
            self.c.select_marker(("p", pin))
            self.hover.enabled = False
            self.hover.hide()
            self.redraw_markers()
            return
        if pin is not None and event.button == 3:
            self.c.select_marker(("p", pin))
            self.redraw_markers()
            self._on_context(("pin", pin))
            return
        if event.button == 1 and self.overlay.hit(event.x, event.y) is None and self.overlay.panel_hit(event.x, event.y):
            fx, fy = self.overlay.panel_corner()          # grab the marker value box -> drag it anywhere
            pb = self.overlay.panel.get_window_extent(self.canvas.get_renderer())
            self._panel_drag = (fx, fy, event.x, event.y, pb.width / ax.bbox.width, pb.height / ax.bbox.height)
            self.hover.enabled = False
            self.hover.hide()
            self.canvas.setCursor(Qt.ClosedHandCursor)
            return
        if event.button == 1:
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
            if self.overlay.panel_hit(event.x, event.y):
                self._on_context(("panel", None))
                return
            hit = self.overlay.hit(event.x, event.y)
            if hit:
                self.c.select_marker(hit)
                self.redraw_markers()
                self._on_context(("marker", hit))
                return
            rect = Rectangle((event.xdata, event.ydata), 0, 0, facecolor="#1f77b4", alpha=0.12,
                             edgecolor="#1f77b4", linewidth=1, zorder=20, label="_box")
            ax.add_patch(rect)
            self._box = (event.xdata, event.ydata, rect)
            self._box_px = (event.x, event.y)       # right-click without dragging -> plot menu

    def _move(self, event):
        with self.pane():
            return self._impl__move(event)

    def _impl__move(self, event):
        ax = self.ax
        if ax is None:
            return
        if event.inaxes is ax and event.xdata is not None:
            self.mouse = (event.x, event.y, event.xdata, event.ydata)
        if self._panel_drag:
            fx, fy, x0, y0, bw, bh = self._panel_drag
            bb = ax.bbox
            nx = min(max(fx + (event.x - x0) / bb.width, 0.0), max(1.0 - bw, 0.0))     # whole box stays
            ny = min(max(fy + (event.y - y0) / bb.height, min(bh, 1.0)), 1.0)           # inside the plot
            self.c.panel_xy = (nx, ny)
            self.redraw_markers()
            return
        if self._pin_drag:
            i, dx0, dy0, x0, y0 = self._pin_drag
            k = 72.0 / self.figure.dpi                    # pixels -> points
            self.c.move_pin(i, dx0 + (event.x - x0) * k, dy0 + (event.y - y0) * k)
            self.redraw_markers()
            return
        if self._panning:
            ax.drag_pan(1, None, event.x, event.y)
            self.canvas.draw_idle()
        elif self._drag and event.inaxes is ax and event.xdata is not None:
            kind, i = self._drag
            self.c.move_marker(kind, i, event.xdata if kind == "v" else event.ydata)
            self.redraw_markers()
        elif self._box and event.inaxes is ax and event.xdata is not None:
            x0, y0, rect = self._box
            rect.set_bounds(min(x0, event.xdata), min(y0, event.ydata), abs(event.xdata - x0), abs(event.ydata - y0))
            self.canvas.draw_idle()
        elif event.inaxes is ax and self.overlay.ax is not None and self.overlay.panel_hit(event.x, event.y):
            self.canvas.setCursor(Qt.OpenHandCursor)
        elif event.inaxes is ax and self.overlay.ax is not None:
            over = self.overlay.hit(event.x, event.y)
            self.canvas.setCursor(Qt.SizeHorCursor if over and over[0] == "v" else
                                  Qt.SizeVerCursor if over else Qt.ArrowCursor)

    def _release(self, event):
        with self.pane():
            return self._impl__release(event)

    def _impl__release(self, event):
        ax = self.ax
        click, self._click = self._click, None
        if click and event.x is not None and abs(event.x - click[0]) <= 3 and abs(event.y - click[1]) <= 3:
            from plotting.pick import curve_at
            add = False                                  # Ctrl / Shift + click = add to the multi-selection
            try:
                from PySide6.QtWidgets import QApplication
                m = QApplication.keyboardModifiers() & (Qt.ControlModifier | Qt.ShiftModifier)
                v = getattr(m, "value", m)
                add = isinstance(v, int) and v != 0
            except Exception:
                add = False
            add = add or str(getattr(event, "key", "") or "") in ("control", "shift", "ctrl+shift") \
                or bool({"ctrl", "shift"} & set(getattr(event, "modifiers", None) or ()))
            gid = curve_at(ax, click[0], click[1]) if ax is not None else None
            try:
                self.on_pick(gid, add=add)
            except TypeError:
                self.on_pick(gid)
        if self._panning:
            self._panning = False
            ax.end_pan()
            self.canvas.setCursor(Qt.ArrowCursor)
        if self._panel_drag:
            self._panel_drag = None
            self.canvas.setCursor(Qt.ArrowCursor)
            self._on_changed("markers")
        if self._pin_drag:
            self._pin_drag = None
        if self._drag:
            self._drag = None
            self._on_changed("markers")
        if self._box:
            x0, y0, rect = self._box
            self._box = None
            x1 = event.xdata if event.inaxes is ax and event.xdata is not None else x0
            y1 = event.ydata if event.inaxes is ax and event.ydata is not None else y0
            rect.remove()
            p0, p1 = ax.transData.transform((x0, y0)), ax.transData.transform((x1, y1))
            if abs(p1[0] - p0[0]) > 5 and abs(p1[1] - p0[1]) > 5:
                ax.set_xlim(min(x0, x1), max(x0, x1))
                ax.set_ylim(min(y0, y1), max(y0, y1))
                self.canvas.draw_idle()
            else:                                     # right-click (no drag): the plot menu
                self.canvas.draw_idle()
                bx, by = getattr(self, "_box_px", (event.x, event.y))
                if event.x is None or (abs(event.x - bx) <= 5 and abs(event.y - by) <= 5):
                    from plotting.pick import curve_at
                    self.hover.enabled = True
                    self._on_context(("plot", curve_at(ax, bx, by)))
                    return
        self.hover.enabled = True
