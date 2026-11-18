"""Split a 'long format' sheet (one FREQ column with several values) into
one RawSheet per frequency. Used for simulation CSVs today and ready for
multi-frequency measurement sheets later."""
from __future__ import annotations

from typing import List

from excel.reader import RawSheet
from excel.sheet_parser import _condition_value, condition_columns, find_header_row
from utils.freq import freq_tag


def split_by_frequency(raw: RawSheet) -> List[RawSheet]:
    hr, _ = find_header_row(raw)
    if hr is None:
        return [raw]
    fcol = condition_columns(raw, hr).get("freq")
    if fcol is None:
        return [raw]
    header_txt = str(raw.cell(hr, fcol) or "")
    groups = {}
    order = []
    for r in range(hr + 1, raw.n_rows):
        hz = _condition_value("freq", raw.cell(r, fcol), header_txt)
        if hz is None:
            continue
        key = round(hz / 1e3)  # kHz bucket
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(raw.rows[r])
    if len(groups) <= 1:
        return [raw]
    out = []
    for key in order:
        rows = [list(x) for x in raw.rows[: hr + 1]] + groups[key]
        out.append(RawSheet(f"{raw.name}@{freq_tag(key * 1e3)}", rows, raw.source_path))
    return out


def split_side_by_side(raw: RawSheet) -> List[RawSheet]:
    """Several 'Freq | Pout | PAE | Gain' groups next to each other (Excel copied to Notepad):

        Freq Po_dBm PAE Gain   Freq Po_dBm PAE Gain   Freq Po_dBm PAE Gain ...
        3.4  20.0   5.4 32.2   3.5  20.0   5.5 32.0   3.6  ...

    -> one RawSheet per group. A sheet with a single group is returned unchanged.
    Groups may have different lengths (empty cells are ignored)."""
    from core.quantities import match_condition, match_quantity
    hr, _ = find_header_row(raw)
    if hr is None:
        return [raw]
    header = raw.rows[hr]
    pout_cols = [c for c, v in enumerate(header) if isinstance(v, str) and match_quantity(v) == "pout"]
    if len(pout_cols) < 2:
        return [raw]
    starts, prev_end = [], 0
    for p in pout_cols:
        start = p
        for c in range(p - 1, prev_end - 1, -1):          # include the group's Freq column
            v = header[c] if c < len(header) else None
            if isinstance(v, str) and match_condition(v) == "freq":
                start = c
                break
            if isinstance(v, str) and match_quantity(v) is not None:
                break
        starts.append(start)
        prev_end = p + 1
    starts.append(max(len(r) for r in raw.rows))
    out = []
    for i in range(len(pout_cols)):
        a, b = starts[i], starts[i + 1]
        rows = [list(r[a:b]) for r in raw.rows]
        rows[:hr] = [list(r) for r in raw.rows[:hr]]       # keep any condition lines above the header
        out.append(RawSheet(f"{raw.name}#{i + 1}", rows, raw.source_path))
    return out
