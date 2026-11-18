"""Source-independent data model.

The Plot Renderer only ever sees `Dataset` objects, so it does not care whether
the data came from a measurement workbook, a simulation CSV, or (later) a
Touchstone file.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

import numpy as np

from core.quantities import CONDITION_FIELDS, ALWAYS_SHOWN_CONDITIONS
from utils.freq import format_freq


class Source(str, Enum):
    MEASUREMENT = "measurement"
    SIMULATION = "simulation"


class Level(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass
class Message:
    level: Level
    text: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"[{self.level.value.upper()}] {self.text}"


@dataclass
class Condition:
    """Measurement / simulation condition (scalar values)."""
    values: Dict[str, object] = field(default_factory=dict)  # key -> float | str
    freq_source: Optional[str] = None  # 'cell' | 'column' | 'sheet_name' | 'file_name' | 'user'

    @property
    def freq_hz(self) -> Optional[float]:
        v = self.values.get("freq")
        return float(v) if isinstance(v, (int, float)) else None

    def set(self, key: str, value: object) -> None:
        self.values[key] = value

    def get(self, key: str, default=None):
        return self.values.get(key, default)

    def format_value(self, key: str, na: str = "N/A") -> str:
        v = self.values.get(key)
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return na
        if key == "freq":
            return format_freq(float(v))
        f = CONDITION_FIELDS.get(key)
        if f is not None and f.numeric and isinstance(v, (int, float)):
            txt = f.fmt.format(float(v))
            return f"{txt} {f.unit}".strip()
        return str(v)

    def display_items(self, include_extra: bool = True, keys=None):
        """[(label, text)] — with `keys`: exactly those conditions (N/A if missing), in that order.
        Without: the mandatory items first, then any extras found."""
        if keys is not None:
            return [(CONDITION_FIELDS[k].label, self.format_value(k)) for k in keys if k in CONDITION_FIELDS]
        items = [(CONDITION_FIELDS[k].label, self.format_value(k)) for k in ALWAYS_SHOWN_CONDITIONS]
        if include_extra:
            for k, f in CONDITION_FIELDS.items():
                if k in ALWAYS_SHOWN_CONDITIONS or k not in self.values:
                    continue
                items.append((f.label, self.format_value(k)))
        return items


@dataclass
class Dataset:
    """One curve family (e.g. one frequency sheet) with aligned columns."""
    name: str                               # e.g. sheet name or "Typical @ 3.60 GHz"
    source: Source
    condition: Condition = field(default_factory=Condition)
    columns: Dict[str, np.ndarray] = field(default_factory=dict)  # quantity key -> values
    origin: str = ""                        # file path / sheet, for traceability
    group: str = ""                         # e.g. simulation set name ("Typical")

    @property
    def freq_hz(self) -> Optional[float]:
        return self.condition.freq_hz

    def has(self, quantity: str) -> bool:
        arr = self.columns.get(quantity)
        return arr is not None and np.isfinite(arr).any()

    def xy(self, x: str, y: str):
        """Return finite (x, y) pairs sorted by x. No interpolation is done."""
        if not (self.has(x) and self.has(y)):
            return np.array([]), np.array([])
        xa = np.asarray(self.columns[x], dtype=float)
        ya = np.asarray(self.columns[y], dtype=float)
        n = min(len(xa), len(ya))
        xa, ya = xa[:n], ya[:n]
        ok = np.isfinite(xa) & np.isfinite(ya)
        xa, ya = xa[ok], ya[ok]
        order = np.argsort(xa, kind="stable")
        return xa[order], ya[order]

    @property
    def n_points(self) -> int:
        return max((len(v) for v in self.columns.values()), default=0)


@dataclass
class Series:
    """A single drawable curve handed to the renderer."""
    label: str
    x: np.ndarray
    y: np.ndarray
    role: Source
    style: Dict[str, object] = field(default_factory=dict)  # optional mpl overrides


@dataclass
class PlotRequest:
    graph_key: str
    title: str
    x_label: str
    y_label: str
    series: List[Series]
    condition_lines: List[str] = field(default_factory=list)
    condition_grid: List[List[str]] = field(default_factory=list)   # aligned rows (VG_* over IQ_*)
    xlim: Optional[tuple] = None
    ylim: Optional[tuple] = None
    x_quantity: str = ""
    y_quantity: str = ""
    notice: str = ""                       # e.g. "Simulation not found for 3.60 GHz"
    empty_text: str = ""                   # shown when nothing is drawn (default: No data for this graph)
    # many curves -> compact legend: [(label, style)] drawn instead of one entry per curve
    legend: Optional[List[tuple]] = None
