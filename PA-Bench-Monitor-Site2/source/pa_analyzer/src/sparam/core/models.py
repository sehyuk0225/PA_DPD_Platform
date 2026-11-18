"""Data model shared by every loader (CSV, Excel, Touchstone)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

# Display order used everywhere in the UI
PARAMS = ("s21", "s11", "s22", "s12")
PARAM_LABEL = {p: p.upper() for p in PARAMS}
# line style per parameter so S21/S11/... stay distinguishable when files share a colour
PARAM_STYLE = {"s21": "-", "s11": "--", "s22": ":", "s12": "-."}


class LoadError(Exception):
    """User-facing error while reading a file."""


@dataclass
class SParamData:
    name: str                                   # shown in legend / file list
    path: str
    freq_hz: np.ndarray
    db: Dict[str, np.ndarray] = field(default_factory=dict)      # 's21' -> dB
    phase: Dict[str, np.ndarray] = field(default_factory=dict)   # 's21' -> deg
    kind: str = "csv"                           # csv | excel | touchstone
    z0: Optional[float] = None
    notes: List[str] = field(default_factory=list)

    @property
    def params(self) -> List[str]:
        return [p for p in PARAMS if p in self.db or p in self.phase]

    def values(self, param: str, fmt: str = "db") -> Optional[np.ndarray]:
        return (self.db if fmt == "db" else self.phase).get(param)

    @property
    def f_min(self) -> float:
        return float(np.nanmin(self.freq_hz)) if len(self.freq_hz) else float("nan")

    @property
    def f_max(self) -> float:
        return float(np.nanmax(self.freq_hz)) if len(self.freq_hz) else float("nan")

    def gamma(self, param: str) -> Optional[np.ndarray]:
        """Complex S-parameter (needs magnitude AND phase)."""
        if param not in self.db or param not in self.phase:
            return None
        return 10 ** (self.db[param] / 20.0) * np.exp(1j * np.radians(self.phase[param]))

    def value_at(self, param: str, freq_hz: float, fmt: str = "db") -> float:
        """Linear interpolation inside the measured band, NaN outside."""
        y = self.values(param, fmt)
        if y is None or not len(self.freq_hz) or not (self.f_min <= freq_hz <= self.f_max):
            return float("nan")
        ok = np.isfinite(y)
        if ok.sum() < 1:
            return float("nan")
        return float(np.interp(freq_hz, self.freq_hz[ok], y[ok]))


@dataclass
class Series:
    label: str
    x: np.ndarray              # GHz (rect)  |  Re(Gamma) (smith)
    y: np.ndarray              # dB / deg    |  Im(Gamma)
    color: str
    linestyle: str = "-"
    freq_ghz: Optional[np.ndarray] = None   # always set; used by Smith hover / markers
    z0: float = 50.0
    key: tuple = ()                          # (file uid, param) -> legend rename / quick style
    linewidth: float = 1.6
    alpha: float = 1.0                       # < 1: faded (another file is selected / emphasised)
    zorder: float = 2.0


@dataclass
class PlotRequest:
    title: str
    x_label: str
    y_label: str
    series: List[Series]
    markers_ghz: List[float] = field(default_factory=list)
    xlim: Optional[tuple] = None
    ylim: Optional[tuple] = None
    mode: str = "rect"                      # 'rect' | 'smith'

    def __post_init__(self):
        pass
