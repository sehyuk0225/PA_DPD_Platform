"""Application state & logic (no GUI code -> testable headless).

Axis ranges
  home (xlim/ylim) : the range the user SET (Apply / Auto Scale). None = automatic.
  view             : current zoom/pan. Fit (F) drops the view and returns to home.
Markers
  vertical   (markers_hz) : frequency markers -> values of every curve (+ Marker Table)
  horizontal (h_markers)  : level markers (dB / deg) -> crossing frequencies of every curve
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import numpy as np

from sparam.core.models import PARAM_LABEL, PARAM_STYLE, PARAMS, LoadError, PlotRequest, Series, SParamData
from sparam.loaders import load_file
from sparam.plotting.export import default_png_name
from sparam.utils.freq import fmt_ghz
from sparam.utils.numeric import nice_bounds

COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#ff7f0e", "#9467bd", "#8c564b", "#e377c2",
          "#17becf", "#bcbd22", "#7f7f7f", "#003f5c", "#a05195", "#665191", "#f95d6a"]
FORMATS = ("db", "phase", "smith")
SMITH_LIM = (-1.12, 1.12)


LINESTYLES = {"solid": "-", "dashed": "--", "dotted": ":", "dashdot": "-."}
LINEWIDTHS = {"thin": 0.9, "normal": 1.6, "thick": 2.6}
PANEL_LOCS = ("upper left", "upper right", "lower left", "lower right")


@dataclass
class Entry:
    data: SParamData
    color: str
    visible: bool = True
    uid: int = 0
    linestyle: Optional[str] = None    # None -> per-parameter default (S21 solid, S11 dashed, ...)
    linewidth: Optional[float] = None


@dataclass
class Readout:
    label: str
    color: str
    linestyle: str
    text: str


@dataclass
class DataStore:
    """Files + legend names, shared by every plot pane."""
    entries: List[Entry] = field(default_factory=list)
    labels: Dict[tuple, str] = field(default_factory=dict)   # (uid, param) -> custom legend text
    styles: Dict[tuple, dict] = field(default_factory=dict)  # (uid, param) -> {color, linestyle, linewidth}
    next_uid: int = 0
    color_i: int = 0
    highlight: List[int] = field(default_factory=list)   # selected files (uids) -> bold, others faded
    highlight_on: bool = True


@dataclass
class Controller:
    """State of ONE plot pane. Several panes share one DataStore."""
    store: DataStore = field(default_factory=DataStore)
    params: Set[str] = field(default_factory=lambda: {"s21"})
    fmt: str = "db"                       # 'db' | 'phase' | 'smith'
    xlim: Optional[Tuple[float, float]] = None   # home range (GHz); None = auto
    ylim: Optional[Tuple[float, float]] = None
    view_xlim: Optional[Tuple[float, float]] = None   # current zoom/pan
    view_ylim: Optional[Tuple[float, float]] = None
    markers_hz: List[float] = field(default_factory=list)
    h_markers: List[float] = field(default_factory=list)
    show_marker_values: bool = True
    smith_frange: Optional[Tuple[float, float]] = None   # GHz shown on the Smith chart (None = all)
    snap_ghz: float = 0.05                               # marker move / placement step (0 = off)
    snap_db: float = 0.5                                 # horizontal marker step (dB / deg)
    selected: Optional[Tuple[str, int]] = None           # ('v'|'h', index) -> arrow keys
    panel_loc: str = "upper left"
    panel_collapsed: bool = False
    panel_xy: Optional[Tuple[float, float]] = None       # value box dragged: upper-left corner (axes fraction)
    pins: List[dict] = field(default_factory=list)       # "M" point markers: {key, f_ghz, dx, dy}

    # ------------------------------------------------------------ shared data
    @property
    def entries(self) -> List[Entry]:
        return self.store.entries

    @entries.setter
    def entries(self, value):
        self.store.entries = value

    # ------------------------------------------------------------ files
    def add_files(self, paths) -> Tuple[int, List[str]]:
        added, errors = 0, []
        for p in paths:
            try:
                for d in load_file(p):
                    d.name = self._unique_name(d.name)
                    st = self.store
                    self.entries.append(Entry(d, COLORS[st.color_i % len(COLORS)], True, st.next_uid))
                    self.entries[-1].base_color = self.entries[-1].color
                    st.color_i += 1
                    st.next_uid += 1
                    added += 1
            except LoadError as exc:
                errors.append(f"{os.path.basename(p)}: {exc}")
            except Exception as exc:
                errors.append(f"{os.path.basename(p)}: 예기치 않은 오류 ({exc})")
        return added, errors

    def _unique_name(self, name: str) -> str:
        names = {e.data.name for e in self.entries}
        if name not in names:
            return name
        i = 2
        while f"{name} ({i})" in names:
            i += 1
        return f"{name} ({i})"

    def remove(self, uids) -> None:
        uids = set(uids)
        self.entries = [e for e in self.entries if e.uid not in uids]
        self.store.highlight = [u for u in self.store.highlight if u not in uids]

    def reorder(self, uids) -> None:
        """New file order (drag & drop in the list) -> legend / drawing order."""
        by = {e.uid: e for e in self.entries}
        new = [by[u] for u in uids if u in by]
        new += [e for e in self.entries if e.uid not in set(uids)]
        self.entries[:] = new

    def show_only(self, uids) -> None:
        for e in self.entries:
            e.visible = e.uid in set(uids)

    def show_also(self, uids) -> None:
        uids = list(uids)
        if len(uids) == 1:
            self.set_muted(uids, False)              # asked for this one explicitly -> eye on
        for e in self.entries:
            if e.uid in set(uids):
                e.visible = True

    def hide(self, uids) -> None:
        for e in self.entries:
            if e.uid in set(uids):
                e.visible = False
        self.store.highlight = [u for u in self.store.highlight if u not in set(uids)]

    def set_distinct_colors(self, uids) -> None:
        from app.controller import distinct_colors
        for u, col in zip(uids, distinct_colors(len(uids))):
            self.set_file_style(u, color=col)

    def reset_colors(self, uids) -> None:
        """Back to each file's first colour (order of loading); line style / width kept."""
        for u in uids:
            e = self.entry(u)
            if e is not None:
                e.color = getattr(e, "base_color", None) or e.color
                for k in list(self.store.styles):
                    if k[0] == u:
                        self.store.styles[k].pop("color", None)

    # ------------------------------------------------------------ highlight
    def set_highlight(self, uids) -> None:
        self.store.highlight = [u for u in dict.fromkeys(uids) if self.entry(u) is not None]

    def clear_highlight(self) -> None:
        self.store.highlight = []

    def _emphasis(self, uid: int):
        """(alpha, width factor, zorder) for a curve of file `uid`."""
        hl = set(self.store.highlight) if self.store.highlight_on else set()
        vis = {e.uid for e in self.visible_entries}
        if not hl or not (hl & vis) or not (vis - hl):
            return 1.0, 1.0, 2.0
        return (1.0, 1.8, 10.0) if uid in hl else (0.22, 1.0, 2.0)

    def clear(self) -> None:
        self.entries.clear()
        self.store.labels.clear()
        self.store.color_i = 0
        self.store.highlight = []

    def rename_file(self, uid: int, name: str) -> None:
        name = (name or "").strip()
        for e in self.entries:
            if e.uid == uid and name:
                e.data.name = name

    def set_label(self, key: tuple, text: str) -> None:
        """Custom legend text for one curve; empty text restores the default."""
        text = (text or "").strip()
        if text:
            self.store.labels[tuple(key)] = text
        else:
            self.store.labels.pop(tuple(key), None)

    def label_for(self, e: "Entry", p: str) -> str:
        return self.store.labels.get((e.uid, p)) or f"{e.data.name}: {PARAM_LABEL[p]}"

    def snap(self, hz: float) -> float:
        step = self.snap_ghz * 1e9
        return round(hz / step) * step if step > 0 else hz

    def snap_level(self, v: float) -> float:
        return round(v / self.snap_db) * self.snap_db if self.snap_db > 0 else v

    # ------------------------------------------------------------ styles
    def entry(self, uid: int) -> Optional[Entry]:
        return next((e for e in self.entries if e.uid == uid), None)

    def style_for(self, e: Entry, p: str):
        """(color, linestyle, linewidth): curve override > file override > defaults."""
        st = self.store.styles.get((e.uid, p), {})
        return (st.get("color") or e.color,
                st.get("linestyle") or e.linestyle or PARAM_STYLE[p],
                st.get("linewidth") or e.linewidth or 1.6)

    def set_curve_style(self, key: tuple, **kw) -> None:
        st = self.store.styles.setdefault(tuple(key), {})
        st.update({k: v for k, v in kw.items() if v is not None})

    def set_file_style(self, uid: int, color=None, linestyle=None, linewidth=None) -> None:
        e = self.entry(uid)
        if e is None:
            return
        if color:
            e.color = color
            for k in list(self.store.styles):          # file colour wins over old curve colours
                if k[0] == uid:
                    self.store.styles[k].pop("color", None)
        if linestyle:
            e.linestyle = linestyle
            for k in list(self.store.styles):
                if k[0] == uid:
                    self.store.styles[k].pop("linestyle", None)
        if linewidth:
            e.linewidth = linewidth
            for k in list(self.store.styles):
                if k[0] == uid:
                    self.store.styles[k].pop("linewidth", None)

    def reset_style(self, uid: int, param: Optional[str] = None) -> None:
        if param:
            self.store.styles.pop((uid, param), None)
            return
        e = self.entry(uid)
        if e:
            e.linestyle = e.linewidth = None
        for k in [k for k in self.store.styles if k[0] == uid]:
            del self.store.styles[k]

    def set_visible(self, uid: int, visible: bool) -> None:
        for e in self.entries:
            if e.uid == uid:
                e.visible = visible
                if visible:
                    e.muted = False                  # ticking the box = eye on again

    @property
    def visible_entries(self) -> List[Entry]:
        return [e for e in self.entries if e.visible and not getattr(e, "muted", False)]

    def set_muted(self, uids, on: bool) -> None:
        """Eye off = always hidden (skipped by search Enter / 이것들만 / multi 같이 보기) until switched on."""
        for e in self.entries:
            if e.uid in set(uids):
                e.muted = bool(on)
                if not on:
                    e.visible = True

    def reset_line_styles(self, uids) -> None:
        for u in uids:
            e = self.entry(u)
            if e is not None:
                e.linestyle = e.linewidth = None
            for k in list(self.store.styles):
                if k[0] == u:
                    self.store.styles[k].pop("linestyle", None)
                    self.store.styles[k].pop("linewidth", None)

    # ------------------------------------------------------------ options
    def set_param(self, param: str, on: bool) -> None:
        (self.params.add if on else self.params.discard)(param)

    @property
    def ordered_params(self) -> List[str]:
        return [p for p in PARAMS if p in self.params]

    def phase_available(self) -> bool:
        return any(e.data.phase for e in self.entries)

    def set_format(self, fmt: str) -> None:
        if fmt not in FORMATS or fmt == self.fmt:
            return
        self.fmt = fmt
        self.xlim = self.ylim = None
        self.view_xlim = self.view_ylim = None
        self.h_markers.clear()  # levels are unit-specific (dB vs deg)

    @property
    def is_smith(self) -> bool:
        return self.fmt == "smith"

    # ------------------------------------------------------------ ranges
    def set_ranges(self, xlim=None, ylim=None) -> List[str]:
        """Set the HOME range (what Fit returns to)."""
        errs = []
        if xlim is not None:
            if xlim[0] < xlim[1]:
                self.xlim = (float(xlim[0]), float(xlim[1]))
            else:
                errs.append("X Min은 X Max보다 작아야 합니다.")
        if ylim is not None:
            if ylim[0] < ylim[1]:
                self.ylim = (float(ylim[0]), float(ylim[1]))
            else:
                errs.append("Y Min은 Y Max보다 작아야 합니다.")
        self.view_xlim = self.view_ylim = None
        return errs

    def set_view(self, xlim, ylim) -> None:
        """Zoom / pan / marker interaction — does not change the home range."""
        self.view_xlim, self.view_ylim = tuple(xlim), tuple(ylim)

    def fit(self) -> None:
        """Back to the user's range (F key)."""
        self.view_xlim = self.view_ylim = None

    def reset_ranges(self) -> None:
        self.xlim = self.ylim = None
        self.smith_frange = None
        self.fit()

    def set_smith_frange(self, lo: float, hi: float) -> List[str]:
        if lo >= hi:
            return ["시작 주파수는 끝 주파수보다 작아야 합니다."]
        self.smith_frange = (float(lo), float(hi))
        return []

    def data_frange_ghz(self):
        f = [e.data.freq_hz for e in self.visible_entries if len(e.data.freq_hz)]
        if not f:
            return None
        return float(min(x.min() for x in f)) / 1e9, float(max(x.max() for x in f)) / 1e9

    def auto_scale(self) -> None:
        if self.is_smith:
            self.fit()
            return
        xs, ys = [], []
        for s in self.build_series():
            ok = np.isfinite(s.y)
            xs.append(s.x[ok]); ys.append(s.y[ok])
        if not xs or not sum(len(x) for x in xs):
            self.reset_ranges()
            return
        x, y = np.concatenate(xs), np.concatenate(ys)
        self.xlim = (float(x.min()), float(x.max()))
        self.ylim = nice_bounds(float(y.min()), float(y.max()), 0.05)
        self.fit()

    def current_limits(self):
        if self.is_smith:
            return (self.view_xlim or SMITH_LIM), (self.view_ylim or SMITH_LIM)
        return (self.view_xlim or self.xlim), (self.view_ylim or self.ylim)

    # ------------------------------------------------------------ markers
    def add_marker(self, hz: float) -> Optional[int]:
        if not hz or hz <= 0 or any(abs(hz - m) <= 1 for m in self.markers_hz):
            return None
        self.markers_hz.append(float(hz))
        return len(self.markers_hz) - 1

    def move_marker(self, i: int, hz: float) -> None:
        if 0 <= i < len(self.markers_hz) and hz > 0:
            self.markers_hz[i] = float(hz)

    def add_h_marker(self, level: float) -> Optional[int]:
        if self.is_smith or level is None or not math.isfinite(level):
            return None
        self.h_markers.append(self.snap_level(float(level)))
        return len(self.h_markers) - 1

    def move_h_marker(self, i: int, level: float) -> None:
        if 0 <= i < len(self.h_markers):
            self.h_markers[i] = self.snap_level(float(level))

    def remove_marker(self, kind: str, i: int) -> None:
        lst = self.markers_hz if kind == "v" else self.h_markers
        if 0 <= i < len(lst):
            lst.pop(i)
            if self.selected == (kind, i):
                self.selected = None
            elif self.selected and self.selected[0] == kind and self.selected[1] > i:
                self.selected = (kind, self.selected[1] - 1)

    def clear_markers(self) -> None:
        self.markers_hz.clear()
        self.h_markers.clear()
        self.selected = None

    def select_marker(self, hit: Optional[Tuple[str, int]]) -> None:
        self.selected = tuple(hit) if hit else None

    def nudge_selected(self, direction: int, axis: str) -> bool:
        """Arrow keys. axis 'x' moves a vertical marker by the GHz step,
        axis 'y' moves a horizontal marker by the dB step. Returns True if moved."""
        if not self.selected:
            return False
        kind, i = self.selected
        if kind == "v" and axis == "x" and 0 <= i < len(self.markers_hz):
            step = (self.snap_ghz or 0.01) * 1e9
            self.markers_hz[i] = max(step, self.snap(self.markers_hz[i]) + direction * step) \
                if self.snap_ghz else self.markers_hz[i] + direction * step
            return True
        if kind == "h" and axis == "y" and 0 <= i < len(self.h_markers):
            step = self.snap_db or 0.1
            self.h_markers[i] = (self.snap_level(self.h_markers[i]) if self.snap_db else self.h_markers[i]) \
                + direction * step
            return True
        return False

    # ------------------------------------------------------------ point markers ("M")
    def add_pin(self, key, f_ghz: float) -> Optional[int]:
        key = tuple(key)
        if any(p["key"] == key and abs(p["f_ghz"] - f_ghz) < 1e-9 for p in self.pins):
            return None
        self.pins.append({"key": key, "f_ghz": float(f_ghz), "dx": 18.0, "dy": 22.0})
        return len(self.pins) - 1

    def move_pin(self, i: int, dx: float, dy: float) -> None:
        if 0 <= i < len(self.pins):
            self.pins[i]["dx"], self.pins[i]["dy"] = float(dx), float(dy)

    def remove_pin(self, i: int) -> None:
        if 0 <= i < len(self.pins):
            self.pins.pop(i)
            if self.selected == ("p", i):
                self.selected = None

    def pin_point(self, pin: dict):
        """(x, y, label, colour, text) of a point marker on the current plot, or None."""
        for s in self.build_series():
            if tuple(s.key) != tuple(pin["key"]) or s.freq_ghz is None or not len(s.freq_ghz):
                continue
            f = pin["f_ghz"]
            if not (s.freq_ghz.min() - 1e-9 <= f <= s.freq_ghz.max() + 1e-9):
                return None
            x = float(np.interp(f, s.freq_ghz, s.x))
            y = float(np.interp(f, s.freq_ghz, s.y))
            if self.is_smith:
                g = complex(x, y)
                z0 = s.z0 or 50.0
                z = z0 * (1 + g) / (1 - g) if abs(1 - g) > 1e-9 else complex(float("inf"))
                text = (f"{s.label}\n{f:.3f} GHz\nΓ = {abs(g):.3f}∠{math.degrees(np.angle(g)):.1f}°\n"
                        f"Z = {z.real:.1f}{z.imag:+.1f}j Ω")
            else:
                text = f"{s.label}\n{f:.3f} GHz\n{y:.2f} {'dB' if self.fmt == 'db' else '°'}"
            return x, y, s.label, s.color, text
        return None

    # ------------------------------------------------------------ undo / redo
    _PANE_FIELDS = ("params", "fmt", "xlim", "ylim", "view_xlim", "view_ylim", "markers_hz", "h_markers",
                    "show_marker_values", "smith_frange", "panel_loc", "panel_collapsed", "panel_xy", "pins")

    def pane_state(self) -> dict:
        import copy
        return {k: copy.deepcopy(getattr(self, k)) for k in self._PANE_FIELDS}

    def set_pane_state(self, st: dict) -> None:
        import copy
        for k, v in st.items():
            setattr(self, k, copy.deepcopy(v))
        self.selected = None

    def snap_freq_smith(self, re: float, im: float) -> Optional[float]:
        """Smith chart: frequency of the trace point closest to (re, im)."""
        best = None
        for s in self.build_series():
            d = np.hypot(s.x - re, s.y - im)
            if not np.isfinite(d).any():
                continue
            i = int(np.nanargmin(d))
            if best is None or d[i] < best[0]:
                best = (d[i], s.freq_ghz[i] * 1e9)
        return best[1] if best else None

    def v_readout(self, hz: float) -> List[Readout]:
        out = []
        for e in self.visible_entries:
            for p in self.ordered_params:
                label = self.label_for(e, p)
                if self.is_smith:
                    g = e.data.gamma(p)
                    if g is None or not (e.data.f_min <= hz <= e.data.f_max):
                        continue
                    color, ls, _lw = self.style_for(e, p)
                    gi = np.interp(hz, e.data.freq_hz, g.real) + 1j * np.interp(hz, e.data.freq_hz, g.imag)
                    z0 = e.data.z0 or 50.0
                    z = z0 * (1 + gi) / (1 - gi) if abs(1 - gi) > 1e-9 else complex(float("inf"))
                    text = f"{abs(gi):.3f}∠{math.degrees(np.angle(gi)):.1f}°  Z={z.real:.1f}{z.imag:+.1f}j Ω"
                    out.append(Readout(label, color, ls, text))
                    continue
                else:
                    v = e.data.value_at(p, hz, self.fmt)
                    if math.isnan(v):
                        continue
                    text = f"{v:.2f} {'dB' if self.fmt == 'db' else '°'}"
                color, ls, _lw = self.style_for(e, p)
                out.append(Readout(label, color, ls, text))
        return out

    def h_readout(self, level: float) -> List[Readout]:
        """Frequencies where each curve crosses `level` (linear interpolation)."""
        out = []
        for s in self.build_series():
            x, y = s.x, s.y - level
            ok = np.isfinite(y)
            x, y = x[ok], y[ok]
            idx = np.where(np.signbit(y[:-1]) != np.signbit(y[1:]))[0]
            f = [x[i] - y[i] * (x[i + 1] - x[i]) / (y[i + 1] - y[i]) for i in idx if y[i + 1] != y[i]]
            text = ", ".join(f"{v:.3f}" for v in f[:4]) + (" GHz" if f else "no crossing")
            if len(f) > 4:
                text += " …"
            out.append(Readout(s.label, s.color, s.linestyle, text))
        return out

    def marker_table(self):
        unit = {"db": "dB", "phase": "deg", "smith": "Γ"}[self.fmt]
        header = ["File", "Param"] + [f"M{i + 1} {fmt_ghz(m)} ({unit})" for i, m in enumerate(self.markers_hz)]
        rows = []
        for e in self.visible_entries:
            for p in self.ordered_params:
                if self.is_smith:
                    g = e.data.gamma(p)
                    if g is None:
                        continue
                    vals = []
                    for m in self.markers_hz:
                        if e.data.f_min <= m <= e.data.f_max:
                            gi = np.interp(m, e.data.freq_hz, g.real) + 1j * np.interp(m, e.data.freq_hz, g.imag)
                            vals.append(f"{abs(gi):.3f}∠{math.degrees(np.angle(gi)):.1f}°")
                        else:
                            vals.append("")
                else:
                    if e.data.values(p, self.fmt) is None:
                        continue
                    vals = [e.data.value_at(p, m, self.fmt) for m in self.markers_hz]
                    vals = ["" if math.isnan(v) else f"{v:.2f}" for v in vals]
                rows.append([e.data.name, PARAM_LABEL[p]] + vals)
        return header, rows

    def marker_table_tsv(self) -> str:
        header, rows = self.marker_table()
        return "\n".join("\t".join(r) for r in [header] + rows)

    # ------------------------------------------------------------ plot
    def build_series(self) -> List[Series]:
        out = []
        for e in self.visible_entries:
            for p in self.ordered_params:
                label = self.label_for(e, p)
                f_ghz = e.data.freq_hz / 1e9
                key = (e.uid, p)
                color, ls, lw = self.style_for(e, p)
                alpha, kw, z = self._emphasis(e.uid)
                lw = lw * kw
                if self.is_smith:
                    g = e.data.gamma(p)
                    if g is None:
                        continue
                    if self.smith_frange:
                        m = (f_ghz >= self.smith_frange[0] - 1e-9) & (f_ghz <= self.smith_frange[1] + 1e-9)
                        if not m.any():
                            continue
                        g, f_ghz = g[m], f_ghz[m]
                    out.append(Series(label, g.real, g.imag, color, ls, f_ghz, e.data.z0 or 50.0, key, lw, alpha, z))
                else:
                    y = e.data.values(p, self.fmt)
                    if y is None:
                        continue
                    out.append(Series(label, f_ghz, y, color, ls, f_ghz, key=key, linewidth=lw, alpha=alpha, zorder=z))
        return out

    def build_plot_request(self) -> PlotRequest:
        names = " / ".join(PARAM_LABEL[p] for p in self.ordered_params) or "S-Parameters"
        if self.is_smith:
            span = (f"  {self.smith_frange[0]:g}–{self.smith_frange[1]:g} GHz" if self.smith_frange else "")
            return PlotRequest(title=f"{names}  (Smith Chart){span}", x_label="", y_label="", series=self.build_series(),
                               markers_ghz=[m / 1e9 for m in self.markers_hz], mode="smith")
        xlim, ylim = self.current_limits()
        y_label = "Magnitude (dB)" if self.fmt == "db" else "Phase (deg)"
        return PlotRequest(title=names, x_label="Frequency (GHz)", y_label=y_label,
                           series=self.build_series(), markers_ghz=[m / 1e9 for m in self.markers_hz],
                           xlim=xlim, ylim=ylim)

    def axis_labels(self):
        if self.is_smith:
            return {"x": ("Re", ""), "y": ("Im", "")}
        return {"x": ("Freq", "GHz"), "y": ("dB" if self.fmt == "db" else "Phase", "dB" if self.fmt == "db" else "deg")}

    def missing_params_messages(self) -> List[str]:
        msgs = []
        for e in self.visible_entries:
            if self.is_smith:
                miss = [PARAM_LABEL[p] for p in self.ordered_params if e.data.gamma(p) is None]
                what = "Phase 데이터가 없어 Smith Chart 불가"
            else:
                miss = [PARAM_LABEL[p] for p in self.ordered_params if e.data.values(p, self.fmt) is None]
                what = "Phase 데이터 없음" if self.fmt == "phase" else "데이터 없음"
            if miss:
                msgs.append(f"{e.data.name}: {', '.join(miss)} {what}")
        return msgs

    def default_export_name(self) -> str:
        name = default_png_name(self.ordered_params, "db" if self.fmt == "db" else "phase", len(self.visible_entries))
        return name.replace("_Phase_", "_Smith_") if self.is_smith else name
