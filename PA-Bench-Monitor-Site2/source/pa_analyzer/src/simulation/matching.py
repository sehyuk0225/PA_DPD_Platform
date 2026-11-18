"""Frequency Matching: pick the simulation Dataset for a measurement FREQ."""
from __future__ import annotations

from typing import Iterable, Optional

from core.models import Dataset

DEFAULT_TOL_HZ = 1e6  # 1 MHz


def match_by_frequency(freq_hz: Optional[float], datasets: Iterable[Dataset],
                       tol_hz: float = DEFAULT_TOL_HZ) -> Optional[Dataset]:
    """Nearest dataset whose FREQ is within `tol_hz`; None if nothing matches."""
    if freq_hz is None:
        return None
    best, best_err = None, None
    for ds in datasets:
        f = ds.freq_hz
        if f is None:
            continue
        err = abs(f - freq_hz)
        if err <= tol_hz and (best_err is None or err < best_err):
            best, best_err = ds, err
    return best
