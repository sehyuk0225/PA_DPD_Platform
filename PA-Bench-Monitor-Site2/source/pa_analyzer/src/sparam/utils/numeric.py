"""Robust cell value -> float conversion for messy lab spreadsheets."""
from __future__ import annotations

import math
import re
from typing import Optional, Tuple

_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_NA_TOKENS = {"", "NA", "N/A", "NAN", "NONE", "NULL", "-", "--", "—", "#N/A", "#DIV/0!",
              "#VALUE!", "#REF!", "#NUM!", "#NAME?", "INF", "-INF"}


def to_float(value: object) -> float:
    """Return a float, or NaN when the cell is not a usable number.

    Accepts numbers, numeric strings and strings with a trailing unit
    ("-1.20 V", "54.2 %", "42.3dBm"). Booleans are rejected.
    """
    if value is None or isinstance(value, bool):
        return math.nan
    if isinstance(value, (int, float)):
        v = float(value)
        return v if math.isfinite(v) else math.nan
    s = str(value).strip().replace(",", ".") if _looks_decimal_comma(value) else str(value).strip()
    if s.upper() in _NA_TOKENS:
        return math.nan
    m = _NUM.match(s.replace(" ", ""))
    if not m:
        return math.nan
    rest = s.replace(" ", "")[m.end():]
    # reject things like "12abc34" but allow units / percent
    if rest and not re.fullmatch(r"[A-Za-z%°/µΩ]*", rest):
        return math.nan
    try:
        v = float(m.group(0))
    except ValueError:
        return math.nan
    return v if math.isfinite(v) else math.nan


def split_value_unit(value: object) -> Tuple[float, str]:
    """'-1.20 V' -> (-1.2, 'V');  3.6 -> (3.6, '')."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value), ""
    s = "" if value is None else str(value).strip()
    m = _NUM.search(s)
    if not m:
        return math.nan, ""
    return to_float(m.group(0)), s[m.end():].strip()


def is_number(value: object) -> bool:
    return not math.isnan(to_float(value))


def _looks_decimal_comma(value: object) -> bool:
    """European '3,6' style -> treat comma as decimal separator (only one comma, no dot)."""
    if not isinstance(value, str):
        return False
    s = value.strip()
    return s.count(",") == 1 and "." not in s and bool(re.fullmatch(r"[-+]?\d+,\d+\s*[A-Za-z%]*", s))


def nice_bounds(lo: float, hi: float, pad_frac: float = 0.05) -> Tuple[float, float]:
    """Padded bounds for auto-scale."""
    if not (math.isfinite(lo) and math.isfinite(hi)):
        return 0.0, 1.0
    if hi < lo:
        lo, hi = hi, lo
    span = hi - lo
    if span == 0:
        span = abs(hi) * 0.1 or 1.0
    return lo - span * pad_frac, hi + span * pad_frac


def fmt_num(v: Optional[float], digits: int = 2) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    return f"{v:.{digits}f}".rstrip("0").rstrip(".") if digits > 0 else f"{v:.0f}"
