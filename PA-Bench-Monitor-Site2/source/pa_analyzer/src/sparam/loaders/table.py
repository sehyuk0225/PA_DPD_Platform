"""Column table (CSV / Excel sheet) -> SParamData.

Default column order (no header needed):
    1: Frequency   2: S21   3: S11   4: S22   5: S12      (values in dB)
If a header row names the columns (e.g. 'Freq(GHz)', 'dB(S(2,1))', 'S11 phase'),
the names win over the position, so other column orders also work.
"""
from __future__ import annotations

import math
import re
from typing import Dict, List, Optional, Tuple

import numpy as np

from sparam.core.models import PARAMS, LoadError, SParamData
from sparam.utils.freq import guess_scale, unit_from_text
from sparam.utils.numeric import to_float

DEFAULT_ORDER = ("freq", "s21", "s11", "s22", "s12")
_S = re.compile(r"S\s*\(?\s*([1-4])\s*[,_ ]?\s*([1-4])\s*\)?")


def classify_header(text: object) -> Optional[Tuple[str, str]]:
    """'dB(S(2,1))' -> ('s21','db');  'S11 Phase' -> ('s11','phase');  'Freq (GHz)' -> ('freq','')."""
    if text is None:
        return None
    s = str(text).strip().upper()
    if not s:
        return None
    if re.match(r"^(FREQ|FREQUENCY|F\b|F\s*\(|FRQ)", s):
        return ("freq", "")
    m = _S.search(s)
    if not m:
        return None
    key = f"s{m.group(1)}{m.group(2)}"
    if key not in PARAMS:
        return None
    if re.search(r"PHASE|ANG|DEG|∠|ARG", s):
        return (key, "phase")
    if re.search(r"REAL|IMAG|\bRE\b|\bIM\b", s):
        return (key, "skip")
    if re.search(r"MAG|LIN|ABS", s) and "DB" not in s:
        return (key, "lin")
    return (key, "db")


def _first_data_row(rows: List[list]) -> Optional[int]:
    for i, r in enumerate(rows):
        nums = [to_float(v) for v in r if v not in (None, "")]
        if len(nums) >= 2 and all(not math.isnan(x) for x in nums[:2]) and not math.isnan(to_float(r[0] if r else None)):
            return i
    return None


def parse_table(rows: List[list], name: str, path: str = "", kind: str = "csv") -> SParamData:
    rows = [r for r in rows if r is not None]
    start = _first_data_row(rows)
    if start is None:
        raise LoadError(f"{name}: 숫자 데이터를 찾을 수 없습니다 (1열=Frequency, 2열=S21, 3열=S11, 4열=S22, 5열=S12).")

    # header = last non-empty row above the data
    header = None
    for r in reversed(rows[:start]):
        if any(v not in (None, "") for v in r):
            header = r
            break

    notes: List[str] = []
    cols: Dict[int, Tuple[str, str]] = {}
    if header:
        named = {i: classify_header(h) for i, h in enumerate(header)}
        named = {i: c for i, c in named.items() if c and c[1] != "skip"}
        if sum(1 for c in named.values() if c[0] != "freq") >= 1:
            cols = named
            if not any(c[0] == "freq" for c in cols.values()):
                cols[0] = ("freq", "")
            notes.append("Header 이름으로 Column을 인식했습니다.")
    if not cols:
        width = max(len(r) for r in rows[start:start + 20])
        cols = {i: (k, "db" if k != "freq" else "") for i, k in enumerate(DEFAULT_ORDER[:width])}

    data_rows = rows[start:]
    f_col = next(i for i, c in cols.items() if c[0] == "freq")
    freq = np.array([to_float(r[f_col]) if f_col < len(r) else math.nan for r in data_rows])
    keep = np.isfinite(freq)
    if keep.sum() < 2:
        raise LoadError(f"{name}: Frequency 데이터가 2개 미만입니다.")

    scale = unit_from_text(header[f_col]) if header and f_col < len(header) else None
    if scale is None:
        scale = guess_scale(float(np.nanmax(np.abs(freq[keep]))))
    freq_hz = freq[keep] * scale

    db: Dict[str, np.ndarray] = {}
    phase: Dict[str, np.ndarray] = {}
    bad = 0
    for i, (key, fmt) in cols.items():
        if key == "freq":
            continue
        vals = np.array([to_float(r[i]) if i < len(r) else math.nan for r in data_rows])[keep]
        bad += int(np.sum(~np.isfinite(vals)))
        if not np.isfinite(vals).any():
            continue
        if fmt == "phase":
            phase[key] = vals
        elif fmt == "lin":
            with np.errstate(divide="ignore", invalid="ignore"):
                db[key] = 20 * np.log10(np.abs(vals))
        else:
            db.setdefault(key, vals)
    if not db and not phase:
        raise LoadError(f"{name}: S-parameter Column이 없습니다.")
    if bad:
        notes.append(f"빈칸/숫자가 아닌 값 {bad}개를 건너뛰었습니다.")

    order = np.argsort(freq_hz, kind="stable")
    return SParamData(name=name, path=path, freq_hz=freq_hz[order],
                      db={k: v[order] for k, v in db.items()},
                      phase={k: v[order] for k, v in phase.items()}, kind=kind, notes=notes)
