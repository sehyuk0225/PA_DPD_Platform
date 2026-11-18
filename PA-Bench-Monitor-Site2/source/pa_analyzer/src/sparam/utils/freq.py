"""Frequency helpers."""
from __future__ import annotations

import re
from typing import Optional

UNIT = {"HZ": 1.0, "KHZ": 1e3, "MHZ": 1e6, "GHZ": 1e9}


def unit_from_text(text: object) -> Optional[float]:
    """'Freq (MHz)' -> 1e6 ; None if no unit written."""
    s = str(text or "").upper()
    for u in ("GHZ", "MHZ", "KHZ"):
        if u in s:
            return UNIT[u]
    if re.search(r"(?<![A-Z])HZ\b|\(HZ\)|\[HZ\]", s):
        return 1.0
    return None


def guess_scale(max_value: float) -> float:
    """Unit-less frequency column: 3.6 -> GHz, 3600 -> MHz, 3.6e9 -> Hz."""
    if max_value >= 1e5:
        return 1.0
    if max_value >= 100:
        return 1e6
    return 1e9


def parse_freq_input(text: str, default_unit: float = 1e9) -> Optional[float]:
    """User input '3.6', '3.6 GHz', '3600MHz' -> Hz."""
    m = re.fullmatch(r"\s*([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*([kKmMgG]?[hH][zZ]|[kKmMgG])?\s*", text or "")
    if not m:
        return None
    v = float(m.group(1))
    u = (m.group(2) or "").upper()
    if not u:
        return v * default_unit
    u = u if u.endswith("HZ") else u + "HZ"
    return v * UNIT.get(u, default_unit)


def fmt_ghz(hz: Optional[float], digits: int = 3) -> str:
    if hz is None:
        return "N/A"
    s = f"{hz / 1e9:.{digits}f}".rstrip("0").rstrip(".")
    return f"{s} GHz"
