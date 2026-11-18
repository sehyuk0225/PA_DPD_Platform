"""Cell values <-> text for the data editor (no Qt here -> unit-testable)."""
from __future__ import annotations


def to_cell(text: str):
    """Table text -> cell value: number if it is one, '' -> None, else the text."""
    t = (text or "").strip()
    if not t:
        return None
    try:
        return float(t.replace(",", ""))
    except ValueError:
        return t


def fmt_cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        if v != v:
            return ""
        t = repr(v)                       # shortest text that reads back as exactly the same number
        return t[:-2] if t.endswith(".0") else t
    return str(v)
