"""Application Controller — all state and logic, no GUI code.

The Qt window is a thin view on top of this class, which keeps the logic
unit-testable headless and makes a future CLI / batch-report mode easy.
"""
from __future__ import annotations

import math
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from core.models import Condition, Dataset, Level, Message, PlotRequest, Series, Source
from core.quantities import ALWAYS_SHOWN_CONDITIONS, CONDITION_FIELDS, DEFAULT_DISPLAY_CONDITIONS, QUANTITIES
from measurement.loader import MeasurementFile, ReaderError, load_measurement_file
from plotting.export import default_png_name
from plotting.graph_types import GRAPHS, GraphSpec, enabled_graphs
from simulation.repository import SimulationRepository, load_simulation_repository
from utils.freq import format_freq, same_freq
from utils.numeric import nice_bounds

Range = Tuple[float, float]
ALL_SETS = "*"          # sim_set value: auto-match from every simulation set
# one colour per FREQUENCY: measurement and simulation at the same FREQ share it
# fixed order from the lowest FREQ: blue, green, red, orange, purple, ... (same as PA Bench Monitor)
FREQ_COLORS = ["#1f77b4", "#2ca02c", "#d62728", "#ff7f0e", "#9467bd", "#8c564b", "#e377c2",
               "#17becf", "#bcbd22", "#7f7f7f", "#003f5c", "#a05195"]


def distinct_colors(n: int) -> List[str]:
    """n clearly different colours: the 12 FREQ colours first, then evenly spread hues."""
    base = FREQ_COLORS + ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#42d4f4", "#f032e6", "#9a6324"]
    if n <= len(base):
        return base[:n]
    import colorsys
    extra = []
    for i in range(n - len(base)):
        h = (i * 0.618033988749895) % 1.0
        r, g, b = colorsys.hsv_to_rgb(h, 0.75, 0.80 if i % 2 else 0.60)
        extra.append("#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255)))
    return base + extra


SIM_SET_STYLES = ["-", "--", ":", "-."]          # several sim sets at one FREQ -> line style
MEAS_MARKERS = ["o", "s", "^", "D", "v", "P"]    # several measurements at one FREQ -> marker
DATASET_COLORS = ["#d62728", "#1f77b4", "#2ca02c", "#ff7f0e", "#9467bd", "#8c564b", "#e377c2",
                  "#17becf", "#bcbd22", "#7f7f7f", "#003f5c", "#a05195"]


@dataclass
class Readout:
    label: str
    color: str
    linestyle: str
    text: str


@dataclass
class AxisRanges:
    xlim: Range
    ylim: Optional[Range]


@dataclass
class PaneState:
    """Everything that belongs to ONE plot pane."""
    graph_key: str = "pae_vs_pout"
    ranges: Dict[str, AxisRanges] = field(default_factory=dict)
    v_markers: List[float] = field(default_factory=list)            # Pout (dBm)
    h_markers: Dict[str, List[float]] = field(default_factory=dict)  # graph -> levels
    selected: Optional[Tuple[str, int]] = None
    show_marker_values: bool = True
    panel_loc: str = "upper left"
    panel_collapsed: bool = False
    panel_xy: Optional[Tuple[float, float]] = None   # dragged: upper-left corner in axes fraction
    pins: Dict[str, List[dict]] = field(default_factory=dict)   # graph -> point markers (M key)
    labels: Dict[str, Dict[str, str]] = field(default_factory=dict)  # graph -> own title / x / y text
    # own datasets: None = this plot shows the common selection (Plot 1 / 2); a list = only what was
    # dragged onto THIS plot (Plot 3 / 4 start empty)
    own_meas: Optional[List[str]] = None
    own_sim_on: List[str] = field(default_factory=list)
    own_sim_off: List[str] = field(default_factory=list)


def _pane_attr(name):
    """Controller attribute that lives in the current pane."""
    return property(lambda self: getattr(self.pane, name), lambda self, v: setattr(self.pane, name, v))


@dataclass
class AppController:
    sim_root: str = ""
    measurement: Optional[MeasurementFile] = None
    simulation: SimulationRepository = field(default_factory=SimulationRepository)
    sheet_name: Optional[str] = None
    sim_set: Optional[str] = None
    show_condition_in_plot: bool = True
    highlight_selected: bool = True      # selected dataset drawn bold, the others faded
    highlight_sim: Optional[str] = None  # "sim:<name>" when a simulation row is selected (else: sheet_name)
    highlight_cleared: bool = False      # empty space in the Datasets list clicked -> nothing emphasised
    highlight_multi: List[str] = field(default_factory=list)   # several rows selected (Ctrl/Shift/Ctrl+A)
    muted: List[str] = field(default_factory=list)     # eye off: "always hidden" until switched on again
    match_tol_hz: float = 1e6
    log: List[Message] = field(default_factory=list)
    # ---- up to 4 plot panes: each has its own graph / ranges / markers (see PaneState)
    panes: List["PaneState"] = field(default_factory=lambda: [PaneState("pae_vs_pout"), PaneState("gain_vs_pout"),
                                                               PaneState("pae_vs_pout", own_meas=[]),
                                                               PaneState("gain_vs_pout", own_meas=[])])
    pane_index: int = 0
    dataset_styles: Dict[str, dict] = field(default_factory=dict)   # name -> {color, linestyle, linewidth, marker}
    condition_keys: List[str] = field(default_factory=lambda: list(DEFAULT_DISPLAY_CONDITIONS))
    # ---- simulation curves: auto-matched to shown measurements, plus the user's own choices
    sim_enabled: bool = True
    auto_sim: bool = True
    sim_files: List[str] = field(default_factory=list)  # user-chosen simulation files (empty = folder mode)
    shared_sim_on: List[str] = field(default_factory=list)   # explicitly checked simulation datasets
    shared_sim_off: List[str] = field(default_factory=list)  # explicitly unchecked (wins over auto-match)
    # ---- overlay of several datasets
    shared_checked: List[str] = field(default_factory=list)  # datasets drawn together (check boxes), Plot 1 / 2
    # ---- browsing (after 'uncheck all'): a clicked row is shown as a preview (box stays empty)
    #      until Space / its box pins it; nothing checked -> nothing drawn (no fallback)
    browsing: bool = False
    favorites: List[str] = field(default_factory=list)     # ★ 즐겨찾기 section of the Datasets list (always listed)
    browse_name: Optional[str] = None
    _colors: Dict[str, str] = field(default_factory=dict)
    snap_x: float = 0.1                                    # dBm
    snap_y: Dict[str, float] = field(default_factory=lambda: {"gain": 0.1, "pae": 0.5, "de": 0.5})
    backoffs: List[float] = field(default_factory=list)    # dB back-off from Psat

    # ------------------------------------------------------------- panes
    # checked / sim_on / sim_off: what the pane being worked on shows — the common selection,
    # or the pane's own list (Plot 3 / 4: only what was dragged onto it)
    @property
    def pane_is_own(self) -> bool:
        return self.pane.own_meas is not None

    def _own_or_shared(attr_own, attr_shared):
        def get(self):
            p = self.pane
            return getattr(p, attr_own) if p.own_meas is not None else getattr(self, attr_shared)

        def put(self, v):
            p = self.pane
            setattr(p if p.own_meas is not None else self, attr_own if p.own_meas is not None else attr_shared, list(v))
        return property(get, put)

    checked = _own_or_shared("own_meas", "shared_checked")
    sim_on = _own_or_shared("own_sim_on", "shared_sim_on")
    sim_off = _own_or_shared("own_sim_off", "shared_sim_off")
    del _own_or_shared

    graph_key = _pane_attr("graph_key")
    _ranges = _pane_attr("ranges")
    v_markers = _pane_attr("v_markers")
    h_markers = _pane_attr("h_markers")
    selected = _pane_attr("selected")
    show_marker_values = _pane_attr("show_marker_values")
    panel_loc = _pane_attr("panel_loc")
    panel_collapsed = _pane_attr("panel_collapsed")
    panel_xy = _pane_attr("panel_xy")
    pins_by_graph = _pane_attr("pins")
    pane_labels = _pane_attr("labels")

    # ---- own plot title / axis titles (per plot pane and graph; empty = the graph's default)
    def plot_labels(self) -> Dict[str, str]:
        g = self.graph
        own = self.pane_labels.get(self.graph_key, {})
        return {"title": own.get("title") or g.title, "x": own.get("x") or g.x_label, "y": own.get("y") or g.y_label}

    def default_labels(self) -> Dict[str, str]:
        g = self.graph
        return {"title": g.title, "x": g.x_label, "y": g.y_label}

    def set_plot_labels(self, title=None, x=None, y=None) -> None:
        """Own texts for this pane's graph; '' = back to the default, None = unchanged."""
        own = dict(self.pane_labels.get(self.graph_key, {}))
        for k, v in (("title", title), ("x", x), ("y", y)):
            if v is None:
                continue
            v = str(v).strip()
            if v and v != self.default_labels()[k]:
                own[k] = v
            else:
                own.pop(k, None)
        if own:
            self.pane_labels[self.graph_key] = own
        else:
            self.pane_labels.pop(self.graph_key, None)

    # ---- point markers ("M"): a callout fixed on one data point, movable, resizable
    @property
    def cur_pins(self) -> List[dict]:
        return self.pins_by_graph.setdefault(self.graph_key, [])

    def add_pin(self, label: str, x: float, y: float) -> Optional[int]:
        for p in self.cur_pins:
            if p["label"] == label and abs(p["x"] - x) < 1e-9 and abs(p["y"] - y) < 1e-9:
                return None
        n = len(self.cur_pins)
        self.cur_pins.append(dict(label=label, x=float(x), y=float(y), dx=18.0 + 6 * (n % 3), dy=22.0 + 14 * (n % 3),
                                  size=1.0))
        return len(self.cur_pins) - 1

    def move_pin(self, i: int, dx: float, dy: float) -> None:
        if 0 <= i < len(self.cur_pins):
            self.cur_pins[i].update(dx=float(dx), dy=float(dy))

    def resize_pin(self, i: int, how: str) -> None:
        if 0 <= i < len(self.cur_pins):
            p = self.cur_pins[i]
            p["size"] = 1.0 if how == "reset" else max(0.5, min(3.0, p["size"] * (1.2 if how == "bigger" else 1 / 1.2)))

    def pin_text(self, p: dict) -> str:
        q = QUANTITIES[self.graph.y]
        return f"{p['label']}\nPout = {p['x']:.2f} dBm\n{q.label} = {p['y']:.2f} {q.unit}"

    @property
    def pane(self) -> PaneState:
        return self.panes[self.pane_index]

    @contextmanager
    def using_pane(self, i: int):
        """Act on pane i (rendering / mouse events of any pane). Re-entrant; when the
        outermost block ends we go back to the ACTIVE pane (which the block may have changed)."""
        old = self.pane_index
        depth = getattr(self, "_pane_depth", 0)
        self._pane_depth = depth + 1
        self.pane_index = i
        try:
            yield self
        finally:
            self._pane_depth = depth
            self.pane_index = old if depth else getattr(self, "active_pane", 0)

    def set_active_pane(self, i: int) -> None:
        self.active_pane = self.pane_index = i

    # ---- per-dataset look (right-click quick menu)
    def style_of_dataset(self, name: str) -> dict:
        return self.dataset_styles.get(name, {})

    def set_dataset_style(self, name: str, **kw) -> None:
        st = self.dataset_styles.setdefault(name, {})
        st.update({k: v for k, v in kw.items() if v is not None})
        if "color" in kw and kw["color"]:
            self._colors[name] = kw["color"]

    def set_styles(self, names, **kw) -> None:
        for n in names:
            self.set_dataset_style(n, **kw)

    def set_distinct_colors(self, names) -> None:
        """Give every dataset in `names` its own colour (multi-selection -> "each a different colour")."""
        for n, col in zip(names, distinct_colors(len(names))):
            self.set_dataset_style(n, color=col)

    def reset_line_styles(self, names) -> None:
        """Line style / width / dots back to default (colours kept)."""
        for n in names:
            st = self.dataset_styles.get(n)
            if st:
                for k in ("linestyle", "linewidth", "marker"):
                    st.pop(k, None)
                if not st:
                    self.dataset_styles.pop(n, None)

    def reset_colors(self, names) -> None:
        """Back to the automatic colour (= FREQ colour); line style / width / dots are kept."""
        for n in names:
            st = self.dataset_styles.get(n)
            if st:
                st.pop("color", None)
                if not st:
                    self.dataset_styles.pop(n, None)
            self._colors.pop(n, None)

    def reset_dataset_style(self, name: str) -> None:
        self.dataset_styles.pop(name, None)
        self._colors.pop(name, None)

    # ------------------------------------------------------------------ setup
    def __post_init__(self):
        if self.sim_root:
            self.load_simulation(self.sim_root)

    # ------------------------------------------------------------- loading
    def load_measurement(self, path: str) -> List[Message]:
        try:
            if path.lower().endswith(".pam"):
                from measurement.project import load_project
                mf = load_project(path)
                self._apply_settings(getattr(mf, "settings", {}))
            else:
                mf = load_measurement_file(path)
        except ReaderError as exc:
            msg = [Message(Level.ERROR, str(exc))]
            self.log += msg
            return msg
        except Exception as exc:  # never crash the GUI
            msg = [Message(Level.ERROR, f"파일을 읽는 중 예기치 않은 오류: {exc}")]
            self.log += msg
            return msg
        self.measurement = mf
        st = getattr(mf, "settings", {}) or {}
        saved_sheet = st.get("sheet")
        self.sheet_name = saved_sheet if (saved_sheet and mf.sheet(saved_sheet)) else mf.default_sheet_name()
        self.shared_checked = [n for n in st.get("checked", []) if mf.sheet(n)] or ([self.sheet_name] if self.sheet_name else [])
        for p in self.panes:
            if p.own_meas is not None:
                p.own_meas = [n for n in p.own_meas if mf.sheet(n)]
        self._colors = {}
        self.dataset_styles = {k: dict(v) for k, v in (st.get("styles") or {}).items() if mf.sheet(k)}
        for k, v in self.dataset_styles.items():
            if v.get("color"):
                self._colors[k] = v["color"]
        for p in self.panes:
            p.v_markers, p.h_markers, p.selected = [], {}, None
        self.log += mf.messages
        return list(mf.messages)

    # ------------------------------------------------------- paste / project
    def paste_text(self, text: str, name: Optional[str] = None) -> Tuple[Optional[str], List[Message]]:
        """Data copied from Excel (Ctrl+C) -> new dataset. Returns (sheet name, messages)."""
        from excel.reader import read_clipboard_text
        if self.measurement is None:
            self.measurement = MeasurementFile("붙여넣기 (저장 안 됨)")
        mf = self.measurement
        n = sum(1 for s in mf.sheets if s.sheet_name.startswith("Paste")) + 1
        try:
            raw = read_clipboard_text(text, name or f"Paste_{n}")
        except ReaderError as exc:
            return None, [Message(Level.ERROR, str(exc))]
        res = mf.add_raw_sheet(raw)
        self.sheet_name = res.sheet_name
        if res.sheet_name not in self.checked:
            self.checked.append(res.sheet_name)          # keep pasting -> curves pile up
        return res.sheet_name, list(res.messages)

    def datasets_payload(self, names) -> dict:
        """Everything needed to recreate these datasets elsewhere (clipboard: Ctrl+C on the list)."""
        from measurement.project import _cell
        out = []
        for n in names:
            r = self.measurement.sheet(n) if self.measurement else None
            if r is None:
                continue
            out.append({"name": r.sheet_name, "display": r.label, "rows": [[_cell(v) for v in row] for row in r.raw.rows],
                        "freq_override_hz": r.freq_override_hz, "user_mapping": bool(r.user_mapping),
                        "header_row": r.header_row if r.user_mapping else None,
                        "mapping": dict(r.mapping) if r.user_mapping else None,
                        "style": dict(self.dataset_styles.get(n, {}))})
        return {"version": 1, "datasets": out}

    def paste_datasets(self, payload: dict) -> List[str]:
        """Ctrl+V of datasets copied with Ctrl+C (this program or another copy of it). Returns new names."""
        from excel.reader import RawSheet
        if self.measurement is None:
            self.measurement = MeasurementFile("붙여넣기 (저장 안 됨)")
        mf = self.measurement
        new = []
        for d in (payload or {}).get("datasets", []):
            raw = RawSheet(d.get("name") or "Paste", [list(r) for r in d.get("rows", [])], "clipboard")
            res = mf.add_raw_sheet(raw, d.get("freq_override_hz"), display_name=d.get("display"),
                                   how="복사해서 붙여넣기 (Ctrl+C → Ctrl+V)")
            if d.get("user_mapping") and d.get("mapping"):
                res = mf.apply_mapping(res.sheet_name, d.get("header_row"),
                                       {k: int(v) for k, v in d["mapping"].items()})
            if d.get("style"):
                self.dataset_styles[res.sheet_name] = dict(d["style"])
            if res.sheet_name not in self.checked:
                self.checked.append(res.sheet_name)
            new.append(res.sheet_name)
        if new:
            self.sheet_name = new[-1]
        return new

    def cancel_paste(self, name: str) -> None:
        self.remove_sheet(name)

    def reorder_sheets(self, names) -> None:
        """New order of the measurement datasets (names not listed keep their place at the end)."""
        mf = self.measurement
        if not mf:
            return
        by = {r.sheet_name: r for r in mf.sheets}
        new = [by[n] for n in names if n in by]
        new += [r for r in mf.sheets if r.sheet_name not in set(names)]
        if [r.sheet_name for r in new] != [r.sheet_name for r in mf.sheets]:
            mf.sheets = new
            mf.dirty = True

    def sort_by_name_and_freq(self) -> bool:
        """'정렬': datasets with the same name next to each other, lowest FREQ first
        (groups keep the position of their first entry). Returns True if the order changed."""
        mf = self.measurement
        if not mf:
            return False
        first = {}
        for i, r in enumerate(mf.sheets):
            first.setdefault(r.label, i)

        def key(ir):
            i, r = ir
            hz = r.dataset.freq_hz if r.dataset is not None else None
            return first[r.label], hz if hz else float("inf"), i
        new = [r for _i, r in sorted(enumerate(mf.sheets), key=key)]
        if [r.sheet_name for r in new] == [r.sheet_name for r in mf.sheets]:
            return False
        mf.sheets = new
        mf.dirty = True
        return True

    def remove_sheet(self, name: str) -> None:
        if not self.measurement:
            return
        self.measurement.remove_sheet(name)
        self.shared_checked = [n for n in self.shared_checked if n != name]
        for p in self.panes:
            if p.own_meas is not None:
                p.own_meas = [n for n in p.own_meas if n != name]
        self._colors.pop(name, None)
        self.dataset_styles.pop(name, None)
        if self.sheet_name == name:
            self.sheet_name = self.checked[-1] if self.checked else self.measurement.default_sheet_name()

    def rename_sheet(self, old: str, new: str) -> None:
        if self.measurement:
            name = self.measurement.rename_sheet(old, new)
            if self.sheet_name == old:
                self.sheet_name = name
            self.shared_checked = [name if n == old else n for n in self.shared_checked]
            for p in self.panes:
                if p.own_meas is not None:
                    p.own_meas = [name if n == old else n for n in p.own_meas]
            if old in self._colors:
                self._colors[name] = self._colors.pop(old)
            if old in self.dataset_styles:
                self.dataset_styles[name] = self.dataset_styles.pop(old)

    def save_project(self, path: str, extras: Optional[dict] = None) -> str:
        """extras: state of the other tabs (e.g. {"sparam": {...}}) stored in the same .pam file."""
        from measurement.project import save_project
        if not self.measurement:
            if not extras:
                raise ValueError("저장할 데이터가 없습니다.")
            self.measurement = MeasurementFile(path)       # S-Parameter only: an empty PA part
        def rng(d):
            return {k: [list(v.xlim), list(v.ylim) if v.ylim else None] for k, v in d.items()}
        settings = {"graph": self.graph_key, "sim_set": self.sim_set, "sheet": self.sheet_name,
                    "sim_files": list(getattr(self, "sim_files", []) or []),
                    "checked": list(self.shared_checked), "backoffs": list(self.backoffs),
                    "conditions": list(self.condition_keys),
                    "sim": {"enabled": self.sim_enabled, "auto": self.auto_sim, "on": self.shared_sim_on, "off": self.shared_sim_off},
                    "ranges": rng(self._ranges), "styles": self.dataset_styles,
                    "panes": [{"graph": p.graph_key, "ranges": rng(p.ranges), "labels": p.labels, "own": p.own_meas,
                               "own_sim_on": p.own_sim_on, "own_sim_off": p.own_sim_off} for p in self.panes],
                    "n_panes": getattr(self, "n_panes", 2), "extras": extras or {}, "muted": list(self.muted),
                    "favorites": list(self.favorites)}
        return save_project(self.measurement, path, settings)

    def _apply_settings(self, st: dict) -> None:
        if st.get("graph") in GRAPHS:
            self.graph_key = st["graph"]
        files = [f for f in (st.get("sim_files") or []) if os.path.isfile(f)]
        if files and files != list(getattr(self, "sim_files", []) or []):
            self.load_simulation_files(files)
        if st.get("sim_set") in self.simulation.set_names or st.get("sim_set") == ALL_SETS:
            self.sim_set = st["sim_set"]
        self.backoffs = [float(b) for b in st.get("backoffs", [])]
        sim = st.get("sim") or {}
        self.sim_enabled, self.auto_sim = bool(sim.get("enabled", True)), bool(sim.get("auto", True))
        self.shared_sim_on, self.shared_sim_off = list(sim.get("on", [])), list(sim.get("off", []))
        if st.get("conditions"):
            self.condition_keys = [k for k in st["conditions"] if k in CONDITION_FIELDS]
        for p, ps in zip(self.panes, st.get("panes") or []):
            if ps.get("graph") in GRAPHS:
                p.graph_key = ps["graph"]
            p.ranges = {k: AxisRanges(tuple(x), tuple(y) if y else None)
                        for k, (x, y) in (ps.get("ranges") or {}).items() if k in GRAPHS}
            p.labels = {k: dict(v) for k, v in (ps.get("labels") or {}).items() if k in GRAPHS}
            if "own" in ps:
                p.own_meas = list(ps["own"]) if ps["own"] is not None else None
                p.own_sim_on, p.own_sim_off = list(ps.get("own_sim_on") or []), list(ps.get("own_sim_off") or [])
        if st.get("n_panes"):
            self.n_panes = int(st["n_panes"])
        self.muted = list(st.get("muted", []))
        self.favorites = list(st.get("favorites", []))
        for k, (x, y) in (st.get("ranges") or {}).items():
            if k in GRAPHS:
                self._ranges[k] = AxisRanges(tuple(x), tuple(y) if y else None)

    @property
    def unsaved(self) -> bool:
        return bool(self.measurement and self.measurement.dirty)

    def load_simulation(self, root: str) -> List[Message]:
        self.sim_root = root
        self.sim_files = []
        self.simulation = load_simulation_repository(root)
        names = self.simulation.set_names
        if self.sim_set != ALL_SETS and self.sim_set not in names:
            self.sim_set = names[0] if names else None
        self.log += self.simulation.messages
        return list(self.simulation.messages)

    def load_simulation_files(self, paths) -> List[Message]:
        """Use exactly these simulation files (one set per file, e.g. 3B / 3X / 3P / 3K)."""
        from simulation.repository import load_simulation_files
        self.sim_files = [os.path.abspath(p) for p in paths]
        self.simulation = load_simulation_files(self.sim_files)
        self.sim_root = self.simulation.root
        names = self.simulation.set_names
        if self.sim_set != ALL_SETS and self.sim_set not in names:
            self.sim_set = ALL_SETS if len(names) > 1 else (names[0] if names else None)
        self.log += self.simulation.messages
        return list(self.simulation.messages)

    # ------------------------------------------------------------ selection
    @property
    def graph(self) -> GraphSpec:
        return GRAPHS[self.graph_key]

    @property
    def graphs(self) -> List[GraphSpec]:
        return enabled_graphs()

    def select_graph(self, key: str) -> None:
        if key in GRAPHS:
            self.graph_key = key

    def select_sheet(self, name: str) -> None:
        """Make `name` the current dataset. With 0-1 boxes checked this simply switches the plot
        (browse one frequency at a time); with several checked the overlay is kept."""
        if self.measurement and self.measurement.sheet(name):
            self.sheet_name = name
            self.highlight_sim = None
            self.highlight_cleared = False
            if self.pane_is_own:
                pass                                 # Plot 3 / 4: only what was dragged / ticked for it
            elif self.browsing:
                if self.browse_name and self.browse_name != name:
                    self.checked = [n for n in self.checked if n != self.browse_name]
                    self.browse_name = None
                if name not in self.checked:
                    self.checked.append(name)
                    self.browse_name = name
            elif len(self.checked) <= 1:
                self.checked = [name]

    def select_sim_set(self, name: Optional[str]) -> None:
        self.sim_set = name

    @property
    def sheet_result(self):
        return self.measurement.sheet(self.sheet_name) if (self.measurement and self.sheet_name) else None

    @property
    def measurement_dataset(self) -> Optional[Dataset]:
        r = self.sheet_result
        return r.dataset if r else None

    @property
    def current_freq_hz(self) -> Optional[float]:
        ds = self.measurement_dataset
        return ds.freq_hz if ds else None

    def simulation_dataset(self) -> Optional[Dataset]:
        sset = self.simulation.get(self.sim_set) if self.sim_set else None
        return sset.find(self.current_freq_hz, self.match_tol_hz) if sset else None

    # ----------------------------------------------------------- conditions
    def current_condition(self) -> Condition:
        ds = self.measurement_dataset
        if ds is not None:
            return ds.condition
        # sheet failed to parse: still show what could be read (FREQ from name)
        return Condition()

    def condition_panel_items(self) -> List[Tuple[str, str]]:
        return self.current_condition().display_items(keys=self.condition_keys)

    def condition_lines(self) -> List[str]:
        return [f"{lab} = {val}" for lab, val in self.current_condition().display_items(include_extra=True)]

    # ---------------------------------------------------------------- axes
    def default_ranges(self, key: Optional[str] = None) -> AxisRanges:
        g = GRAPHS[key or self.graph_key]
        return AxisRanges(tuple(g.default_xlim), tuple(g.default_ylim) if g.default_ylim else None)

    def ranges(self) -> AxisRanges:
        return self._ranges.get(self.graph_key) or self.default_ranges()

    def set_ranges(self, xlim: Optional[Range] = None, ylim: Optional[Range] = None) -> List[Message]:
        cur = self.ranges()
        msgs = []
        nx, ny = cur.xlim, cur.ylim
        if xlim is not None:
            if xlim[0] < xlim[1]:
                nx = (float(xlim[0]), float(xlim[1]))
            else:
                msgs.append(Message(Level.WARNING, "X Min은 X Max보다 작아야 합니다."))
        if ylim is not None:
            if ylim[0] < ylim[1]:
                ny = (float(ylim[0]), float(ylim[1]))
            else:
                msgs.append(Message(Level.WARNING, "Y Min은 Y Max보다 작아야 합니다."))
        self._ranges[self.graph_key] = AxisRanges(nx, ny)
        return msgs

    def reset_ranges(self) -> None:
        self._ranges.pop(self.graph_key, None)

    def auto_scale(self) -> AxisRanges:
        xs, ys = [], []
        for s in self.build_series():
            if len(s.x):
                xs.append(s.x); ys.append(s.y)
        if not xs:
            return self.ranges()
        x = np.concatenate(xs); y = np.concatenate(ys)
        xlim = nice_bounds(float(np.nanmin(x)), float(np.nanmax(x)), 0.03)
        ylim = nice_bounds(float(np.nanmin(y)), float(np.nanmax(y)), 0.06)
        if self.graph.y in ("pae", "de") and ylim[0] < 0 <= float(np.nanmin(y)):
            ylim = (0.0, ylim[1])
        self._ranges[self.graph_key] = AxisRanges(xlim, ylim)
        return self._ranges[self.graph_key]

    # ------------------------------------------------------------- plotting
    # ------------------------------------------------------------ overlay
    def set_checked(self, name: str, on: bool) -> None:
        if name == self.browse_name:
            self.browse_name = None                  # preview row ticked (Space / box) -> pinned
        if on:
            self.set_muted([name], False)            # ticking the box = eye on again
        if on and name not in self.checked:
            self.checked.append(name)
        elif not on:
            self.checked = [n for n in self.checked if n != name]
            if not self.checked:
                self.browsing = True                 # last one unticked -> really nothing on the plot

    # ------------------------------------------------------------ undo / redo snapshots
    def snapshot(self) -> dict:
        """Everything Ctrl+Z should bring back. 'key' compares states (selection is not part of it)."""
        import copy
        mf = self.measurement
        sheets = list(mf.sheets) if mf else []
        panes = []
        for p in self.panes:
            q = copy.deepcopy(p)
            q.selected = None
            panes.append(q)
        state = dict(
            shared_checked=list(self.shared_checked), shared_sim_on=list(self.shared_sim_on),
            shared_sim_off=list(self.shared_sim_off),
            sim_enabled=self.sim_enabled, auto_sim=self.auto_sim, sim_set=self.sim_set,
            styles=copy.deepcopy(self.dataset_styles), condition_keys=list(self.condition_keys),
            backoffs=list(self.backoffs), favorites=list(self.favorites), browsing=self.browsing, browse_name=self.browse_name, n_panes=getattr(self, "n_panes", 2), panes=panes, muted=list(self.muted),
            names=[getattr(r, "display_name", None) for r in sheets])
        key = repr((sorted((k, v) for k, v in state.items() if k != "panes"), [repr(p) for p in panes],
                    id(mf), [id(r) for r in sheets]))
        return dict(state, key=key, measurement=mf, sheets=sheets, sheet_name=self.sheet_name)

    def restore(self, snap: dict) -> None:
        import copy
        self.measurement = snap["measurement"]
        if self.measurement is not None:
            self.measurement.sheets = list(snap["sheets"])
            for r, name in zip(self.measurement.sheets, snap["names"]):
                r.display_name = name
            self.measurement.dirty = True
        for k in ("shared_checked", "shared_sim_on", "shared_sim_off", "condition_keys", "backoffs"):
            setattr(self, k, list(snap[k]))
        self.muted = list(snap.get("muted", []))
        self.browsing = bool(snap.get("browsing", False))
        self.favorites = list(snap.get("favorites", []))
        self.browse_name = snap.get("browse_name") if snap.get("browse_name") in self.checked else None
        self.sim_enabled, self.auto_sim, self.sim_set = snap["sim_enabled"], snap["auto_sim"], snap["sim_set"]
        self.dataset_styles = copy.deepcopy(snap["styles"])
        self._colors = {}
        self.panes = [copy.deepcopy(p) for p in snap["panes"]]
        self.n_panes = snap["n_panes"]
        names = {r.sheet_name for r in (self.measurement.sheets if self.measurement else [])}
        if self.sheet_name not in names:
            self.sheet_name = snap["sheet_name"] if snap["sheet_name"] in names else \
                (self.checked[0] if self.checked else (next(iter(names), None)))
        if self.highlight_sim and self.highlight_sim[4:] not in {d.name for d in self.visible_sims()}:
            self.highlight_sim = None

    # ------------------------------------------------------------ eye: always hidden
    def is_muted(self, name: str) -> bool:
        return name in self.muted

    def set_muted(self, names, on: bool) -> None:
        """Eye off = never on the plot, skipped by 'show all' / search Enter / multi 'show also'."""
        for n in names:
            if on and n not in self.muted:
                self.muted.append(n)
            elif not on:
                self.muted = [m for m in self.muted if m != n]
        if on:
            self.checked = [n for n in self.checked if n not in set(names)]
            self.highlight_multi = [k for k in self.highlight_multi if k not in set(names)]

    # ------------------------------------------------------------ ★ favourites section
    def favorite_names(self) -> List[str]:
        """Favourites that still exist (deleted datasets drop out by themselves)."""
        mf = self.measurement
        return [n for n in dict.fromkeys(self.favorites) if mf and mf.sheet(n)]

    def add_favorites(self, names, before: Optional[str] = None) -> int:
        """Put measurements into ★ 즐겨찾기 (before `before`, else at the end); already there -> moved."""
        names = [n for n in dict.fromkeys(names) if not str(n).startswith("sim:")
                 and self.measurement and self.measurement.sheet(n)]
        if not names:
            return 0
        rest = [n for n in self.favorite_names() if n not in set(names)]
        i = rest.index(before) if before in rest else len(rest)
        self.favorites = rest[:i] + names + rest[i:]
        return len(names)

    def remove_favorites(self, names) -> None:
        """Out of ★ 즐겨찾기 only — the dataset itself stays in Measurement."""
        self.favorites = [n for n in self.favorites if n not in set(names)]

    def add_to_pane(self, names) -> int:
        """Datasets dragged onto the plot being worked on: shown there (Plot 3 / 4: in its own list)."""
        n0 = len(self.checked) + len(self.sim_on)
        for n in dict.fromkeys(names):
            if n.startswith("sim:"):
                self.toggle_sim(n[4:], True)
            elif self.measurement and self.measurement.sheet(n) and n not in self.checked:
                self.set_muted([n], False) if len(names) == 1 else None
                if not self.is_muted(n):
                    self.checked = list(self.checked) + [n]
        if not self.pane_is_own:
            self.browse_name = None
        return len(self.checked) + len(self.sim_on) - n0

    def check_all(self, on: bool) -> None:
        """[모두 체크] / [모두 해제]. Unchecking really empties the plot (then a clicked row is previewed)."""
        self.browse_name = None
        self.sim_on, self.sim_off = [], []          # simulations back to automatic (follow the measurements)
        if on:
            self.browsing = False
            self.checked = [r.sheet_name for r in (self.measurement.sheets if self.measurement else [])
                            if r.ok and not self.is_muted(r.sheet_name)]
        else:
            self.browsing = True
            self.checked = []
        self.highlight_multi = []

    def show_only(self, names) -> None:
        """'이것들만 보기' / [선택 Plot]: exactly these. Simulations among them -> exactly those simulations
        (others off); none among them -> simulations stay automatic."""
        names = list(names)
        names = list(dict.fromkeys(names))
        meas = [n for n in names if not n.startswith("sim:")]
        sims = [n[4:] for n in names if n.startswith("sim:")]
        if len(names) == 1:
            self.set_muted(names, False)
        meas = [n for n in meas if not self.is_muted(n) and self.measurement and self.measurement.sheet(n)]
        sims = [n for n in sims if not self.is_muted("sim:" + n)]
        self.browse_name = None
        self.checked = meas
        self.browsing = not meas                     # simulations only -> no measurement at all
        if sims:
            self.sim_on = list(sims)
            self.sim_off = [d.name for d in self.all_sim_datasets() if d.name not in set(sims)]
        if meas and self.sheet_name not in meas:
            self.sheet_name = meas[0]
        self.highlight_multi = []

    def show_also(self, names) -> None:
        """Add datasets to what is on the plot (= check them), keeping everything already shown."""
        names = list(names)
        if len(names) == 1:
            self.set_muted(names, False)             # asked for this one explicitly -> eye back on
        else:
            names = [n for n in names if n not in self.muted]
        if self.measurement and not self.checked and self.sheet_name and not self.browsing:
            self.checked = [self.sheet_name]      # browse mode showed the current one -> keep it
        self.browse_name = None                   # a previewed row stays
        for n in names:
            if n.startswith("sim:"):
                self.toggle_sim(n[4:], True)
            elif self.measurement and self.measurement.sheet(n) and n not in self.checked:
                self.checked.append(n)

    def hide_datasets(self, names) -> None:
        """Take datasets off the plot (= uncheck). Simulations are switched off explicitly."""
        names = set(names)
        for n in names:
            if n.startswith("sim:"):
                self.toggle_sim(n[4:], False)
        self.checked = [n for n in self.checked if n not in names]
        if not self.checked and not self.pane_is_own:
            self.browsing = True                  # last one hidden -> empty plot (no fallback curve)
        if self.sheet_name in names:          # the current dataset must not come back as the fallback
            rest = [n for n in self.checked] or [r.sheet_name for r in (self.measurement.sheets if self.measurement else [])
                                                  if r.ok and r.sheet_name not in names]
            if rest:
                self.sheet_name = rest[0]
        if self.highlight_key() in names:
            self.highlight_sim = None
            self.clear_highlight()
        self.highlight_multi = [k for k in self.highlight_multi if k not in names]

    def visible_sheets(self) -> List:
        """Datasets on the plot: the checked ones (list order), or the current one if none is checked."""
        if not self.measurement:
            return []
        order = [s.sheet_name for s in self.measurement.sheets]
        muted = set(self.muted)
        cset = set(self.checked)
        checked = [n for n in order if n in cset]
        if checked:
            names = [n for n in checked if n not in muted]      # eye off -> never drawn
        elif self.browsing or self.pane_is_own:
            names = []                                          # 'uncheck all' / own plot -> really nothing
        else:
            names = [self.sheet_name] if (self.sheet_name and self.sheet_name not in muted) else []
        return [r for r in (self.measurement.sheet(n) for n in names) if r is not None and r.ok]

    # ------------------------------------------------------------ colours by frequency
    def set_freq_ref(self, ghz_list):
        """Reference FREQs (e.g. the sample type's frequency list from PA Bench Monitor):
        lowest -> FREQ_COLORS[0] (blue), next -> green, red, ... Fixed whatever is shown."""
        self._freq_ref = sorted({int(round(float(g) * 1e3)) for g in (ghz_list or [])})
        self._freq_colors = {}

    def _all_freq_keys(self):
        hz = [r.dataset.freq_hz for r in (self.measurement.sheets if self.measurement else []) if r.ok]
        try:
            hz += [d.freq_hz for d in self.all_sim_datasets()]
        except Exception:
            pass
        return {int(round(h / 1e6)) for h in hz if h}

    def freq_color(self, hz) -> Optional[str]:
        """Same FREQ -> same colour, fixed: reference FREQs (lowest first) then every other loaded
        FREQ in ascending order. Hiding / unchecking datasets never changes a colour."""
        if not hz:
            return None
        if not hasattr(self, "_freq_colors"):
            self._freq_colors = {}
        key = int(round(hz / 1e6))                     # 1 MHz buckets
        if key not in self._freq_colors:
            self._assign_freq_colors()
        if key not in self._freq_colors:
            used = set(self._freq_colors.values())
            free = [c for c in FREQ_COLORS if c not in used]
            self._freq_colors[key] = free[0] if free else FREQ_COLORS[len(self._freq_colors) % len(FREQ_COLORS)]
        return self._freq_colors[key]

    def _assign_freq_colors(self):
        """Colour order = reference FREQs, then all loaded FREQs (not only the visible ones), ascending."""
        ref = list(getattr(self, "_freq_ref", None) or [])
        extra = sorted(self._all_freq_keys() - set(ref))
        order = ref + extra
        if not hasattr(self, "_freq_colors"):
            self._freq_colors = {}
        for i, k in enumerate(order):
            self._freq_colors[k] = FREQ_COLORS[i % len(FREQ_COLORS)]

    def color_of(self, name: str) -> str:
        """Colour of a dataset: user's colour > its FREQ colour > per-name fallback."""
        custom = self.style_of_dataset(name).get("color")
        if custom:
            return custom
        hz = None
        if name.startswith("sim:"):
            d = next((d for d in self.all_sim_datasets() if d.name == name[4:]), None)
            hz = d.freq_hz if d else None
        elif self.measurement and self.measurement.sheet(name) and self.measurement.sheet(name).ok:
            hz = self.measurement.sheet(name).dataset.freq_hz
        c = self.freq_color(hz)
        if c:
            return c
        if name not in self._colors:
            used = set(self._colors.values())
            free = [c for c in DATASET_COLORS if c not in used]
            self._colors[name] = free[0] if free else DATASET_COLORS[len(self._colors) % len(DATASET_COLORS)]
        return self._colors[name]

    def _sims_for(self, hz) -> List[Dataset]:
        """Simulation datasets auto-matched to a measurement FREQ (one set, or every set)."""
        if not hz or not self.sim_set:
            return []
        names = self.simulation.set_names if self.sim_set == ALL_SETS else [self.sim_set]
        out = []
        for n in names:
            sset = self.simulation.get(n)
            d = sset.find(hz, self.match_tol_hz) if sset else None
            if d is not None:
                out.append(d)
        return out

    def _sim_for(self, hz):
        found = self._sims_for(hz)
        return found[0] if found else None

    # ------------------------------------------------------------ simulation curves
    def all_sim_datasets(self) -> List[Dataset]:
        out = []
        for name in self.simulation.set_names:
            sset = self.simulation.get(name)
            out += sorted(sset.datasets, key=lambda d: d.freq_hz or 0)
        return out

    def _auto_sims(self) -> List[str]:
        if not self.auto_sim:
            return []
        ids = []
        for r in self.visible_sheets():
            for sim in self._sims_for(r.dataset.freq_hz):
                if sim.name not in ids:
                    ids.append(sim.name)
        return ids

    def visible_sims(self) -> List[Dataset]:
        if not self.sim_enabled:
            return []
        want = set(self._auto_sims()) | set(self.sim_on)
        muted = set(self.muted)
        return [d for d in self.all_sim_datasets() if d.name in want and d.name not in self.sim_off
                and "sim:" + d.name not in muted]

    def toggle_sim(self, sim_id: str, on: bool) -> None:
        self.sim_on = [x for x in self.sim_on if x != sim_id]
        self.sim_off = [x for x in self.sim_off if x != sim_id]
        (self.sim_on if on else self.sim_off).append(sim_id)

    def build_series(self) -> List[Series]:
        """Colour = FREQ (measurement and simulation at the same FREQ match).
        Simulation: thick translucent line (line style per sim set if several sets share a FREQ).
        Measurement: line + markers (marker shape per dataset if several share a FREQ)."""
        g = self.graph
        vis = self.visible_sheets()
        sims_ds = self.visible_sims()
        self._assign_freq_colors()
        single = len(vis) <= 1 and len(sims_ds) <= 1
        set_order = self.simulation.set_names
        sims, meas = [], []
        for sim in sims_ds:
            x, y = sim.xy(g.x, g.y)
            if not len(x):
                continue
            key = "sim:" + sim.name
            custom = self.style_of_dataset(key)
            label = f"Simulation ({sim.group})" if single else f"Sim {sim.group} · {format_freq(sim.freq_hz)}"
            # a measurement at the same FREQ with a user colour -> the simulation follows it
            partner = next((r for r in vis if same_freq(r.dataset.freq_hz, sim.freq_hz)
                            and self.style_of_dataset(r.sheet_name).get("color")), None)
            color = custom.get("color") or (self.style_of_dataset(partner.sheet_name)["color"] if partner
                                             else self.color_of(key))
            same_f = [d for d in sims_ds if same_freq(d.freq_hz, sim.freq_hz)]
            ls = SIM_SET_STYLES[(set_order.index(sim.group) if sim.group in set_order else 0) % len(SIM_SET_STYLES)] \
                if len({d.group for d in same_f}) > 1 else "-"
            style = {"color": color, "alpha": 0.55, "linewidth": 2.4, "linestyle": ls}
            for k in ("linestyle", "linewidth"):
                if custom.get(k):
                    style[k] = custom[k]
            if custom.get("linestyle") or custom.get("linewidth"):
                style["alpha"] = 0.85
            if custom.get("marker"):
                style["marker"] = custom["marker"]
            style["gid"] = key                         # lets a click on the plot find the dataset
            sims.append(Series(label, x, y, Source.SIMULATION, style))
        freq_groups = []                               # [(freq, [names in order])] — once, not per curve
        group_of = {}
        for v in vis:
            f = v.dataset.freq_hz
            if not f:
                continue
            grp = next((gr for gr in freq_groups if same_freq(gr[0], f)), None)
            if grp is None:
                grp = (f, [])
                freq_groups.append(grp)
            grp[1].append(v.sheet_name)
            group_of[v.sheet_name] = grp[1]
        for r in vis:
            ds = r.dataset
            x, y = ds.xy(g.x, g.y)
            if not len(x):
                continue
            color = self.color_of(r.sheet_name)
            label = "Measurement" if single else f"{r.label} · {format_freq(ds.freq_hz)}"
            custom = self.style_of_dataset(r.sheet_name)
            style = {"color": color, "markeredgecolor": color}
            same_f = group_of.get(r.sheet_name, [])
            if len(same_f) > 1:
                style["marker"] = MEAS_MARKERS[same_f.index(r.sheet_name) % len(MEAS_MARKERS)]
            for k in ("linestyle", "linewidth"):
                if custom.get(k):
                    style[k] = custom[k]
            if "marker" in custom:
                style["marker"] = custom["marker"] or "None"
            style["gid"] = r.sheet_name
            meas.append(Series(label, x, y, Source.MEASUREMENT, style))
        self._apply_highlight(sims, meas, sims_ds, vis)
        return sims + meas

    # ------------------------------------------------------------ highlight of the selected dataset
    def highlight_key(self) -> Optional[str]:
        """Dataset to emphasise: the selected simulation row, else the current measurement."""
        if not self.highlight_selected or self.highlight_cleared:
            return None
        return self.highlight_sim or self.sheet_name

    def highlight_keys(self) -> List[str]:
        """All datasets to emphasise: the multi-selection, else the single selected one."""
        if not self.highlight_selected or self.highlight_cleared:
            return []
        if len(self.highlight_multi) > 1:
            return list(self.highlight_multi)
        k = self.highlight_key()
        return [k] if k else []

    def set_highlight_multi(self, keys) -> None:
        keys = list(dict.fromkeys(keys))
        self.highlight_multi = keys if len(keys) > 1 else []
        if self.highlight_multi:
            self.highlight_cleared = False

    def select_highlight_sim(self, key: str) -> None:
        self.highlight_sim = key
        self.highlight_cleared = False

    def clear_highlight(self) -> None:
        self.highlight_cleared = True
        self.highlight_multi = []

    def pick_dataset(self, key: Optional[str]) -> None:
        """A curve was clicked in the plot (key = sheet name or "sim:<name>"); None = empty space."""
        if not key:
            self.clear_highlight()
        elif key.startswith("sim:"):
            self.select_highlight_sim(key)
        elif self.measurement and self.measurement.sheet(key):
            self.sheet_name = key                     # overlay unchanged: the curve is already shown
            self.highlight_sim = None
            self.highlight_cleared = False

    def _apply_highlight(self, sims, meas, sims_ds, vis) -> None:
        """Selected curve(s): bold + on top. Their counterparts at the same FREQ (sim <-> meas) stay normal
        so the comparison remains readable. Everything else is faded. Only with 2+ curves."""
        keys = self.highlight_keys()
        if not keys or len(sims) + len(meas) < 2:
            return
        sim_keys = ["sim:" + d.name for d in sims_ds if len(d.xy(self.graph.x, self.graph.y)[0])]
        meas_keys = [r.sheet_name for r in vis if len(r.dataset.xy(self.graph.x, self.graph.y)[0])]
        keys = [k for k in keys if k in sim_keys or k in meas_keys]
        if not keys:
            return
        freq = {"sim:" + d.name: d.freq_hz for d in sims_ds}
        freq.update({r.sheet_name: r.dataset.freq_hz for r in vis})
        hl_sim_hz = [freq.get(k) for k in keys if k in sim_keys]       # FREQs of selected simulations
        hl_meas_hz = [freq.get(k) for k in keys if k in meas_keys]     # FREQs of selected measurements
        sel = set(keys)

        def role(k, is_sim):
            if k in sel:
                return "hl"
            f = freq.get(k)
            other = hl_meas_hz if is_sim else hl_sim_hz      # a selected curve of the OTHER kind at this FREQ
            partner = bool(f) and any(h and same_freq(f, h) for h in other)
            return "pair" if partner else "fade"
        roles = [(s, role(k, True), True) for k, s in zip(sim_keys, sims)] + \
                [(s, role(k, False), False) for k, s in zip(meas_keys, meas)]
        if not any(r == "fade" for _, r, _ in roles) and sum(1 for _, r, _ in roles if r == "hl") < 2:
            return                       # one curve selected and nothing else to fade -> look unchanged
        for s, r, is_sim in roles:
            if r == "hl" and is_sim:
                s.style.update(alpha=1.0, zorder=9, linewidth=float(s.style.get("linewidth", 2.4)) + 1.4)
            elif r == "hl":
                s.style.update(alpha=1.0, zorder=10, linewidth=float(s.style.get("linewidth", 1.2)) * 2.0,
                               markersize=7.0, markeredgewidth=1.0)
            elif r == "pair":
                s.style.update(zorder=8 if is_sim else 9)   # counterpart at the same FREQ: normal look, on top
            else:
                s.style.update(alpha=0.15 if is_sim else 0.22)

    def condition_panel_rows(self) -> List[List[Tuple[str, str]]]:
        """Condition panel of the selected dataset in the same layout as the plot header."""
        items = dict(self.condition_panel_items())
        vg = {"DRV": "vgs_drv", "CA": "vgs_ca", "PK": "vgs_pk"}
        iq = {"DRV": "iq_drv", "CA": "iq_ca", "PK": "iq_pk"}
        cols = [r for r in self._ROLES if vg[r] in self.condition_keys or iq[r] in self.condition_keys]
        rows = []
        for table in (vg, iq):
            row = []
            for r in cols:
                lab = CONDITION_FIELDS[table[r]].label
                row.append((lab, items[lab]) if table[r] in self.condition_keys else ("", ""))
            if any(lab for lab, _ in row):
                rows.append(row)
        used = {CONDITION_FIELDS[k].label for k in list(vg.values()) + list(iq.values())}
        extra = [(lab, val) for lab, val in items.items() if lab not in used]
        if extra:
            rows.append(extra)
        return rows

    # ------------------------------------------------------------ condition header layout
    _ROLES = ("DRV", "CA", "PK")

    def condition_grid(self) -> Tuple[List[List[str]], List[str]]:
        """Aligned header: row 1 = VG_DRV | VG_CA | VG_PK, row 2 = IQ_* under the matching VG.
        Other selected conditions go to `extra` (one line)."""
        lines = dict((l.split(" = ", 1)[0], l) for l in self.condition_lines())
        vg = {"DRV": "vgs_drv", "CA": "vgs_ca", "PK": "vgs_pk"}
        iq = {"DRV": "iq_drv", "CA": "iq_ca", "PK": "iq_pk"}
        cols = [r for r in self._ROLES if vg[r] in self.condition_keys or iq[r] in self.condition_keys]
        top = [lines.get(CONDITION_FIELDS[vg[r]].label, "") if vg[r] in self.condition_keys else "" for r in cols]
        bottom = [lines.get(CONDITION_FIELDS[iq[r]].label, "") if iq[r] in self.condition_keys else "" for r in cols]
        grid = [row for row in (top, bottom) if any(row)]
        used = {CONDITION_FIELDS[k].label for k in list(vg.values()) + list(iq.values())}
        extra = [l for lab, l in lines.items() if lab not in used]
        return grid, extra

    def status_messages(self) -> List[Message]:
        """Messages explaining what is (not) on the plot right now."""
        msgs: List[Message] = []
        g = self.graph
        r = self.sheet_result
        if r is not None:
            msgs += r.messages
        meas = self.measurement_dataset
        if meas is not None and not meas.has(g.y):
            msgs.append(Message(Level.ERROR, f"{QUANTITIES[g.y].label} column을 찾을 수 없습니다. "
                                             f"Column Mapping을 확인해주세요."))
        if self.sim_set is None:
            msgs.append(Message(Level.WARNING, "Simulation Dataset이 없습니다. Simulation 폴더를 확인해주세요."))
        elif self.sim_enabled and self.auto_sim:
            for v in self.visible_sheets():
                hz = v.dataset.freq_hz
                if hz is None:
                    msgs.append(Message(Level.WARNING, f"{v.label}: FREQ를 알 수 없어 Simulation을 매칭할 수 없습니다."))
                elif self._sim_for(hz) is None:
                    msgs.append(Message(Level.WARNING,
                                        f"{format_freq(hz)} Simulation Dataset을 찾을 수 없습니다 ({self.sim_set})."))
        return msgs

    def condition_lines(self) -> List[str]:
        """'FREQ = 3.60 GHz' ...; several datasets with different values -> 'a / b'."""
        vis = self.visible_sheets()
        if len(vis) <= 1:
            return [f"{lab} = {val}" for lab, val in self.current_condition().display_items(keys=self.condition_keys)]
        lines = []
        per = [dict(v.dataset.condition.display_items(keys=self.condition_keys)) for v in vis]
        labels = []
        for d in per:
            for k in d:
                if k not in labels:
                    labels.append(k)
        for lab in labels:
            vals = []
            for d in per:
                v = d.get(lab, "N/A")
                if v not in vals:
                    vals.append(v)

            lines.append(f"{lab} = {' / '.join(vals[:4])}{' …' if len(vals) > 4 else ''}")
        return lines

    def build_plot_request(self) -> Optional[PlotRequest]:
        if self.measurement is None and not self.simulation.sets:
            return None
        g = self.graph
        rng = self.ranges()
        notice = ""
        vis = self.visible_sheets()
        missing = list(dict.fromkeys(                   # each FREQ once, even with many datasets per FREQ
            format_freq(v.dataset.freq_hz) for v in vis
            if self.sim_enabled and self.auto_sim and self.sim_set and self._sim_for(v.dataset.freq_hz) is None))
        if missing:
            notice = (f"Simulation ({self.sim_set}) not available at {', '.join(missing)}"
                      if all(v.dataset.freq_hz for v in vis) else "Simulation not matched (FREQ unknown)")
        series = self.build_series()
        return PlotRequest(
            graph_key=g.key, title=self.plot_labels()["title"], x_label=self.plot_labels()["x"],
            y_label=self.plot_labels()["y"],
            series=series, legend=self.compact_legend(series), condition_lines=(self.condition_grid()[1] if self.measurement else []),
            condition_grid=(self.condition_grid()[0] if self.measurement else []),
            xlim=rng.xlim, ylim=rng.ylim, x_quantity=g.x, y_quantity=g.y, notice=notice,
            empty_text=("이 Plot 전용: Datasets에서 데이터를 여기로 끌어다 놓으세요\n"
                        "(이 Plot을 클릭한 뒤 목록 체크도 이 Plot에만 적용)" if self.pane_is_own and not series else ""))

    LEGEND_MAX = 14     # more curves than this -> one legend entry per FREQ (fast + readable)

    def series_freqs(self) -> Dict[str, Optional[float]]:
        """Series gid (sheet name / "sim:<name>") -> FREQ in Hz."""
        freq = {"sim:" + d.name: d.freq_hz for d in self.visible_sims()}
        freq.update({r.sheet_name: r.dataset.freq_hz for r in self.visible_sheets()})
        return freq

    def compact_legend(self, series) -> Optional[list]:
        """With many curves a full legend is unreadable and slow to draw (most of the render time).
        Colours already mean FREQ, so list each FREQ once + what line / dots mean + the selected curve."""
        if len(series) <= self.LEGEND_MAX:
            return None
        freq = self.series_freqs()
        groups = {}
        for s in series:
            hz = freq.get(s.style.get("gid"))
            key = (round(hz / 1e6) if hz else None, s.style.get("color"))
            if key not in groups or s.role == Source.MEASUREMENT:
                groups[key] = (hz, s.style.get("color"))
        out = []
        hl = set(self.highlight_keys())
        for s in series[:]:
            if len(out) >= 8:
                break
            if s.style.get("gid") in hl and float(s.style.get("alpha", 1)) >= 1:
                st = {k: v for k, v in s.style.items() if k in ("color", "linestyle", "linewidth", "marker",
                                                               "markeredgecolor", "markersize")}
                out.append(("▶ " + s.label, dict(st, alpha=1.0)))
        for (_k, color), (hz, _c) in sorted(groups.items(), key=lambda kv: (kv[1][0] is None, kv[1][0] or 0)):
            out.append((format_freq(hz) if hz else "FREQ ?", {"color": color, "linewidth": 4, "linestyle": "-"}))
        roles = {s.role for s in series}
        if Source.MEASUREMENT in roles:
            out.append((f"Measurement (line+dots) ×{sum(1 for s in series if s.role == Source.MEASUREMENT)}",
                        {"color": "#666666", "linewidth": 1.2, "marker": "o", "markersize": 4.5}))
        if Source.SIMULATION in roles:
            out.append((f"Simulation (thick line) ×{sum(1 for s in series if s.role == Source.SIMULATION)}",
                        {"color": "#666666", "linewidth": 2.4, "alpha": 0.55}))
        return out

    # ------------------------------------------------------------ markers
    @property
    def y_step(self) -> float:
        return self.snap_y.get(self.graph.y, 0.1)

    def snap_xv(self, x: float) -> float:
        return round(x / self.snap_x) * self.snap_x if self.snap_x > 0 else x

    def snap_yv(self, y: float) -> float:
        st = self.y_step
        return round(y / st) * st if st > 0 else y

    @property
    def cur_h(self) -> List[float]:
        return self.h_markers.setdefault(self.graph_key, [])

    def add_v_marker(self, x: float) -> Optional[int]:
        x = round(self.snap_xv(float(x)), 6)
        if any(abs(x - m) < 1e-9 for m in self.v_markers):
            return None
        self.v_markers.append(x)
        return len(self.v_markers) - 1

    def add_h_marker(self, y: float) -> Optional[int]:
        self.cur_h.append(round(self.snap_yv(float(y)), 6))
        return len(self.cur_h) - 1

    def move_marker(self, kind: str, i: int, value: float) -> None:
        lst = self.v_markers if kind == "v" else self.cur_h
        if 0 <= i < len(lst):
            lst[i] = round(self.snap_xv(value) if kind == "v" else self.snap_yv(value), 6)

    def set_marker_value(self, kind: str, i: int, value: float) -> None:
        """Typed value (double-click on a marker): exactly what was entered, no snapping."""
        lst = self.v_markers if kind == "v" else self.cur_h
        if 0 <= i < len(lst):
            lst[i] = float(value)

    def nudge_selected(self, direction: int, axis: str) -> bool:
        if not self.selected:
            return False
        kind, i = self.selected
        if kind == "v" and axis == "x" and 0 <= i < len(self.v_markers):
            step = self.snap_x or 0.1
            self.v_markers[i] = round(self.snap_xv(self.v_markers[i]) + direction * step, 6)
            return True
        if kind == "h" and axis == "y" and 0 <= i < len(self.cur_h):
            step = self.y_step or 0.1
            self.cur_h[i] = round(self.snap_yv(self.cur_h[i]) + direction * step, 6)
            return True
        return False

    def select_marker(self, hit) -> None:
        self.selected = tuple(hit) if hit else None

    def remove_marker(self, kind: str, i: int) -> None:
        lst = self.v_markers if kind == "v" else self.cur_h if kind == "h" else self.cur_pins
        if 0 <= i < len(lst):
            lst.pop(i)
            if self.selected == (kind, i):
                self.selected = None

    def clear_markers(self) -> None:
        self.v_markers.clear()
        self.cur_h.clear()
        self.cur_pins.clear()
        self.selected = None

    def v_readout(self, x: float) -> List[Readout]:
        g = self.graph
        unit = QUANTITIES[g.y].unit
        out = []
        for s in self.build_series():
            if len(s.x) < 2 or not (s.x.min() <= x <= s.x.max()):
                continue
            y = float(np.interp(x, s.x, s.y))
            out.append(Readout(s.label, *self._style_of(s), f"{y:.2f} {unit}"))
        return out

    def h_readout(self, level: float) -> List[Readout]:
        out = []
        for s in self.build_series():
            y = s.y - level
            idx = np.where(np.signbit(y[:-1]) != np.signbit(y[1:]))[0]
            xs = [s.x[i] - y[i] * (s.x[i + 1] - s.x[i]) / (y[i + 1] - y[i]) for i in idx if y[i + 1] != y[i]]
            text = ", ".join(f"{v:.2f}" for v in xs[:4]) + (" dBm" if xs else "no crossing")
            out.append(Readout(s.label, *self._style_of(s), text))
        return out

    def _style_of(self, s: Series):
        """(color, sample) for the marker value box: 'sim' = thick translucent line, 'meas' = line + dot."""
        kind = "sim" if s.role == Source.SIMULATION else "meas"
        if s.style.get("color"):
            return s.style["color"], kind
        return ("#1f4e9c" if kind == "sim" else "#d62728"), kind

    # ------------------------------------------------------------ back-off from Psat
    def set_backoffs(self, text: str) -> List[str]:
        vals, errs = [], []
        for tok in str(text).replace(";", ",").replace(" ", ",").split(","):
            if not tok.strip():
                continue
            try:
                v = float(tok)
            except ValueError:
                errs.append(f"숫자가 아닙니다: {tok}")
                continue
            if v < 0 or v > 30:
                errs.append(f"Back-off는 0~30 dB 범위로 입력하세요: {tok}")
                continue
            vals.append(v)
        if not errs:
            self.backoffs = sorted(set(vals))
        return errs

    def backoff_rows(self) -> List[dict]:
        """For every curve shown: Psat (= max Pout), and Gain / PAE at Psat - BO."""
        rows = []
        vis = self.visible_sheets()
        entries = []
        style_of = {s.label: (s.style.get("color") or ("#1f4e9c" if s.role == Source.SIMULATION else "#d62728"))
                    for s in self.build_series()}
        sims = self.visible_sims()
        single = len(vis) <= 1 and len(sims) <= 1
        for r in vis:
            lab = "Measurement" if single else f"{r.label} · {format_freq(r.dataset.freq_hz)}"
            entries.append((r.label if not single else "Measurement", style_of.get(lab, "#d62728"),
                            r.dataset, Source.MEASUREMENT))
        for sim in sims:
            lab = f"Simulation ({sim.group})" if single else f"Sim {sim.group} · {format_freq(sim.freq_hz)}"
            entries.append((f"Sim {sim.group} · {format_freq(sim.freq_hz)}", style_of.get(lab, "#1f4e9c"),
                            sim, Source.SIMULATION))
        for label, color, ds, role in entries:
            pout = ds.columns.get("pout")
            if pout is None or not np.isfinite(pout).any():
                continue
            psat = float(np.nanmax(pout))
            for bo in self.backoffs:
                target = psat - bo
                vals = {}
                for q in ("gain", "pae"):
                    x, y = ds.xy("pout", q)
                    vals[q] = float(np.interp(target, x, y)) if len(x) > 1 and x.min() <= target <= x.max() else math.nan
                rows.append(dict(label=label, color=color, role=role, psat=psat, bo=bo, pout=target, **vals))
        return rows

    def backoff_points(self):
        """(x, y, color, text) on the current graph for each back-off row."""
        q = self.graph.y
        pts = []
        for r in self.backoff_rows():
            if q in r and not math.isnan(r[q]):
                unit = "%" if q in ("pae", "de") else "dB"
                pts.append((r["pout"], r[q], r["color"], f"-{r['bo']:g}dB, {r[q]:.1f}{unit}"))
        return pts

    def backoff_tsv(self) -> str:
        head = ["Dataset", "Psat (dBm)", "Back-off (dB)", "Pout (dBm)", "Gain (dB)", "PAE (%)"]
        lines = ["\t".join(head)]
        for r in self.backoff_rows():
            lines.append("\t".join([r["label"], f"{r['psat']:.2f}", f"{r['bo']:g}", f"{r['pout']:.2f}",
                                    "" if math.isnan(r["gain"]) else f"{r['gain']:.2f}",
                                    "" if math.isnan(r["pae"]) else f"{r['pae']:.2f}"]))
        return "\n".join(lines)

    def axis_labels(self) -> Dict[str, Tuple[str, str]]:
        g = self.graph
        return {"x": (QUANTITIES[g.x].label, QUANTITIES[g.x].unit),
                "y": (QUANTITIES[g.y].label, QUANTITIES[g.y].unit)}

    def default_export_name(self) -> str:
        return default_png_name(self.graph.file_stem, self.current_freq_hz, self.sheet_name or "")


def default_simulation_root(_unused: str = "") -> str:
    """Prefer private local data (git-ignored) next to the program; else the bundled example."""
    from utils.paths import default_simulation_root as _d
    return _d()
