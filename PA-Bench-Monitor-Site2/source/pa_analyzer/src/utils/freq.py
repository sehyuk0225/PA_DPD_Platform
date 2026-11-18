"""Frequency parsing / formatting.

Handles naming conventions found in RF lab files:

    "3.6GHz", "3.6 GHz", "3600MHz", "3600 MHz", "FREQ_3P6G", "FREQ_3600",
    "3p6GHz", "F3.6G", "3.6G", "3600M", "3.6e9", 3.6 (cell value), 3600 ...

Unit-less numbers use a heuristic:  < 100 -> GHz,  < 1e5 -> MHz,  else Hz.
"""
from __future__ import annotations

import re
from typing import Optional

_UNIT_SCALE = {"GHZ": 1e9, "G": 1e9, "MHZ": 1e6, "M": 1e6, "KHZ": 1e3, "K": 1e3, "HZ": 1.0}

# number (with optional exponent) followed by optional frequency unit
_NUM_UNIT = re.compile(
    r"(?<![0-9.])(\d+(?:\.\d+)?(?:E[+-]?\d+)?)\s*(GHZ|MHZ|KHZ|HZ|G|M|K)?(?![A-Z0-9])"
)
# "3P6" -> "3.6" (decimal point written as P/p, common in sheet names)
_P_DECIMAL = re.compile(r"(?<=\d)P(?=\d)")
_FREQ_WORDS = {"FREQ", "FRQ", "F", "FC", "RF", "FREQUENCY", "FO", "CF"}


def _heuristic_scale(value: float) -> float:
    if value < 100:
        return 1e9   # GHz
    if value < 1e5:
        return 1e6   # MHz
    return 1.0       # Hz


def number_to_hz(value: float, unit: str = "") -> Optional[float]:
    """Convert a numeric cell value to Hz using an explicit unit or heuristic."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v or v <= 0:  # NaN or non-positive
        return None
    u = (unit or "").upper().replace(" ", "")
    if u in _UNIT_SCALE:
        return v * _UNIT_SCALE[u]
    return v * _heuristic_scale(v)


def parse_frequency(text: object) -> Optional[float]:
    """Extract a frequency in Hz from free text (sheet name, cell, file name).

    Returns None when nothing plausible (10 MHz .. 300 GHz) is found.
    """
    if text is None:
        return None
    if isinstance(text, (int, float)):
        hz = number_to_hz(text)
        return hz if hz and 1e7 <= hz <= 3e11 else None

    s = str(text).strip().upper()
    if not s:
        return None
    s = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", s)     # "3,600 MHz" (Excel thousands separator)
    s = _P_DECIMAL.sub(".", s)
    s = s.replace("_", " ").replace("-", " ")

    # Unit-less numbers are only trusted when the text is just a number or
    # carries a frequency keyword ("FREQ_3600"), so "Sheet1" or "Summary 2024"
    # are NOT mistaken for 1 GHz / 2.024 GHz.
    tokens = set(re.findall(r"[A-Z]+", re.sub(r"\d+(\.\d+)?(E[+-]?\d+)?", " ", s)))
    tokens -= set(_UNIT_SCALE)
    unitless_ok = (not tokens) or bool(tokens & _FREQ_WORDS)

    candidates = []
    for m in _NUM_UNIT.finditer(s):
        num, unit = m.group(1), m.group(2)
        try:
            v = float(num)
        except ValueError:
            continue
        if not unit and not unitless_ok:
            continue
        hz = v * _UNIT_SCALE[unit] if unit else v * _heuristic_scale(v)
        if 1e7 <= hz <= 3e11:
            candidates.append((unit is not None, hz))
    if not candidates:
        return None
    # Prefer a number that carries an explicit unit
    with_unit = [hz for has_u, hz in candidates if has_u]
    return with_unit[0] if with_unit else candidates[0][1]


def format_freq(hz: Optional[float], unit: str = "GHz", digits: int = 2) -> str:
    if hz is None:
        return "N/A"
    if unit == "MHz":
        return f"{hz / 1e6:.{max(digits - 2, 0)}f} MHz"
    return f"{hz / 1e9:.{digits}f} GHz"


def freq_tag(hz: Optional[float]) -> str:
    """File-name friendly tag: 3.6e9 -> '3p6GHz', 3.45e9 -> '3p45GHz'."""
    if hz is None:
        return "FREQunknown"
    ghz = f"{hz / 1e9:.4f}".rstrip("0").rstrip(".")
    return ghz.replace(".", "p") + "GHz"


def same_freq(a: Optional[float], b: Optional[float], tol_hz: float = 1e6) -> bool:
    return a is not None and b is not None and abs(a - b) <= tol_hz
