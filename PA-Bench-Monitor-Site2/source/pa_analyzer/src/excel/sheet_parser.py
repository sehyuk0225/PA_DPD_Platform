"""Sheet Parser: RawSheet -> Dataset (+ diagnostics).

Pipeline for one sheet
    1. find header row        (row with the most recognised quantity headers)
    2. map columns            (header -> quantity key; or user Column Mapping)
    3. extract table          (numeric conversion, NaN for bad cells)
    4. parse conditions       (key/value cells anywhere near the table,
                               or constant condition columns inside the table)
    5. resolve frequency      (user override > cell/column > sheet name > file name)

Never raises on bad data: problems are reported as `Message`s.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from core.models import Condition, Dataset, Level, Message, Source
from core.quantities import (CONDITION_FIELDS, QUANTITIES, match_condition,
                             match_quantity, unit_hint)
from excel.reader import RawSheet, column_letter
from utils.freq import format_freq, number_to_hz, parse_frequency, same_freq
from utils.numeric import split_value_unit, to_float

HEADER_SCAN_ROWS = 60
REQUIRED = ("pout",)
EXPECTED = ("pae", "gain")


# ----------------------------------------------------------------------------
@dataclass
class ColumnInfo:
    index: int
    header: str

    @property
    def letter(self) -> str:
        return column_letter(self.index)

    @property
    def display(self) -> str:
        return f"{self.letter}: {self.header}" if self.header else f"{self.letter}"


@dataclass
class SheetParseResult:
    sheet_name: str
    raw: RawSheet
    dataset: Optional[Dataset] = None
    header_row: Optional[int] = None                 # 0-based
    mapping: Dict[str, int] = field(default_factory=dict)
    messages: List[Message] = field(default_factory=list)
    user_mapping: bool = False
    freq_override_hz: Optional[float] = None
    display_name: Optional[str] = None      # name shown to the user (may repeat); sheet_name is the unique key
    added_at: Optional[str] = None          # when it came into the program ("2026-10-23 18:19"), see 데이터 정보
    added_how: str = ""                     # 붙여넣기 / 파일 열기: x.xlsx / Ctrl+V 복사 …
    modified_at: Optional[str] = None       # last Column Mapping / FREQ / data-editor change

    @property
    def label(self) -> str:
        return self.display_name or self.sheet_name

    @property
    def ok(self) -> bool:
        return self.dataset is not None

    @property
    def freq_hz(self) -> Optional[float]:
        return self.dataset.freq_hz if self.dataset else None

    def columns(self, header_row: Optional[int] = None) -> List[ColumnInfo]:
        """Columns available for Column Mapping (header texts of `header_row`)."""
        hr = self.header_row if header_row is None else header_row
        n = self.raw.n_cols
        out = []
        for c in range(n):
            h = self.raw.cell(hr, c) if hr is not None else None
            out.append(ColumnInfo(c, "" if h is None else str(h).strip()))
        return out

    def errors(self) -> List[Message]:
        return [m for m in self.messages if m.level == Level.ERROR]


# ----------------------------------------------------------------------------
# 1. Header detection
# ----------------------------------------------------------------------------
def map_header_row(row) -> Dict[str, int]:
    """{quantity: column} for one row; best column wins when several match."""
    from core.quantities import header_priority
    best: Dict[str, Tuple[int, int]] = {}
    for c, v in enumerate(row):
        if not isinstance(v, str):
            continue
        q = match_quantity(v)
        if not q:
            continue
        pr = header_priority(v)
        if q not in best or pr > best[q][1]:
            best[q] = (c, pr)
    return {q: c for q, (c, _) in best.items()}


def find_header_row(raw: RawSheet) -> Tuple[Optional[int], Dict[str, int]]:
    """Return (row index, {quantity: column}) of the most plausible header row."""
    best_row, best_map, best_score = None, {}, 0.0
    for r in range(min(HEADER_SCAN_ROWS, raw.n_rows)):
        mapping = map_header_row(raw.rows[r])
        if not mapping:
            continue
        core = sum(1 for q in mapping if q in ("pout", "pae", "gain", "pin"))
        score = core + 0.2 * (len(mapping) - core) + (1.5 if "pout" in mapping else 0)
        # header must be followed by at least one numeric row in a mapped column
        if not _has_numeric_below(raw, r, mapping.values()):
            score -= 2
        if score > best_score and (core >= 2 or "pout" in mapping):
            best_row, best_map, best_score = r, mapping, score
    return best_row, best_map


def _has_numeric_below(raw: RawSheet, r: int, cols, look: int = 5) -> bool:
    for rr in range(r + 1, min(r + 1 + look, raw.n_rows)):
        if any(not math.isnan(to_float(raw.cell(rr, c))) for c in cols):
            return True
    return False


def condition_columns(raw: RawSheet, header_row: int) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for c, v in enumerate(raw.rows[header_row] if header_row < raw.n_rows else []):
        if isinstance(v, str) and match_quantity(v) is None:
            k = match_condition(v)
            if k and k not in out:
                out[k] = c
    return out


# ----------------------------------------------------------------------------
# 3. Table extraction
# ----------------------------------------------------------------------------
def extract_table(raw: RawSheet, header_row: int, cols: Dict[str, int],
                  messages: List[Message]) -> Tuple[Dict[str, np.ndarray], List[int]]:
    """Read data rows below `header_row` for the given columns.

    Rows without a numeric Pout are skipped (unit rows, comments, ...).
    The table ends at the first completely empty row after data started.
    """
    x_col = cols.get("pout")
    data: Dict[str, list] = {k: [] for k in cols}
    rows_used: List[int] = []
    started = False
    bad_cells = 0
    for r in range(header_row + 1, raw.n_rows):
        vals = {k: raw.cell(r, c) for k, c in cols.items()}
        if all(v in (None, "") for v in vals.values()):
            if started:
                break
            continue
        x = to_float(vals.get("pout")) if x_col is not None else math.nan
        if x_col is not None and math.isnan(x):
            if started and any(v not in (None, "") for v in vals.values()):
                bad_cells += 1
            continue
        started = True
        rows_used.append(r)
        for k in cols:
            v = to_float(vals[k])
            if math.isnan(v) and vals[k] not in (None, ""):
                bad_cells += 1
            data[k].append(v)
    if bad_cells:
        messages.append(Message(Level.WARNING,
                                f"숫자가 아닌 값 {bad_cells}개를 무시했습니다 (빈칸/텍스트/오류값)."))
    return {k: np.asarray(v, dtype=float) for k, v in data.items()}, rows_used


def _apply_unit_conversions(raw: RawSheet, header_row: Optional[int], cols: Dict[str, int],
                            data: Dict[str, np.ndarray], messages: List[Message]) -> None:
    def hdr(k):
        return "" if header_row is None else str(raw.cell(header_row, cols[k]) or "")

    for k in ("pae", "de", "evm"):
        if k in data and np.isfinite(data[k]).any():
            arr = data[k]
            fin = arr[np.isfinite(arr)]
            if "%" not in hdr(k) and fin.max() <= 1.0 and fin.min() >= -0.05:
                data[k] = arr * 100.0
                messages.append(Message(Level.INFO,
                                        f"{QUANTITIES[k].label} 값이 0~1 범위여서 %로 변환했습니다 (x100)."))
    if "pout" in data and unit_hint(hdr("pout")) == "W":
        with np.errstate(divide="ignore", invalid="ignore"):
            data["pout"] = np.where(data["pout"] > 0, 10 * np.log10(data["pout"] * 1000.0), np.nan)
        messages.append(Message(Level.INFO, "Pout 단위가 W여서 dBm으로 변환했습니다."))
    if "idd" in data and unit_hint(hdr("idd")) == "A":
        data["idd"] = data["idd"] * 1000.0


# ----------------------------------------------------------------------------
# 4. Condition parsing
# ----------------------------------------------------------------------------
_KV_SPLIT = re.compile(r"\s*[=:]\s*")


def _condition_value(key: str, value: object, label_text: str = ""):
    f = CONDITION_FIELDS[key]
    if not f.numeric:
        return None if value in (None, "") else str(value).strip()
    if key == "freq":
        if isinstance(value, str):
            hz = parse_frequency(value)
            if hz is None:
                v, unit = split_value_unit(value)
                hz = number_to_hz(v, unit or unit_hint(label_text))
            return hz
        return number_to_hz(value, unit_hint(label_text))
    v, _unit = split_value_unit(value)
    if math.isnan(v):
        return None
    if key in ("vgs_drv", "vgs_ca", "vgs_pk"):
        return -abs(v)            # GaN: gate bias is always negative (a missing '-' in the sheet is added)
    return v


def parse_conditions(raw: RawSheet, header_row: Optional[int], table_cols: Dict[str, int],
                     rows_used: List[int], messages: List[Message]) -> Tuple[Condition, Dict[str, str]]:
    """Find condition values; returns (Condition, {key: 'cell'|'column'})."""
    cond = Condition()
    origin: Dict[str, str] = {}
    table_col_set = set(table_cols.values())

    # (a) condition columns inside the data table (e.g. A: FREQ, B: VGS,CA ...)
    if header_row is not None:
        for key, c in condition_columns(raw, header_row).items():
            vals = [raw.cell(r, c) for r in rows_used] or \
                   [raw.cell(r, c) for r in range(header_row + 1, min(header_row + 4, raw.n_rows))]
            parsed = [_condition_value(key, v, str(raw.cell(header_row, c))) for v in vals]
            parsed = [p for p in parsed if p not in (None, "")]
            if not parsed:
                continue
            cond.set(key, parsed[0])
            origin[key] = "column"
            uniq = {round(p, 6) if isinstance(p, float) else p for p in parsed}
            if len(uniq) > 1:
                messages.append(Message(
                    Level.WARNING,
                    f"{CONDITION_FIELDS[key].label} 값이 행마다 다릅니다 ({len(uniq)}종). "
                    f"첫 번째 값을 사용합니다. (Bias/Freq sweep Sheet는 향후 버전에서 분리 지원)"))
            table_col_set.add(c)

    # (b) key/value cells (anywhere in the scan window, outside the data table)
    limit = min(raw.n_rows, HEADER_SCAN_ROWS if header_row is None else max(header_row + 1, HEADER_SCAN_ROWS))
    for r in range(limit):
        row = raw.rows[r]
        for c, v in enumerate(row):
            if not isinstance(v, str) or not v.strip():
                continue
            if header_row is not None and r >= header_row and c in table_col_set:
                continue  # part of the data table
            text = v.strip().lstrip("#").strip()
            label, inline = text, None
            parts = _KV_SPLIT.split(text, maxsplit=1)
            if len(parts) == 2 and parts[1]:
                label, inline = parts
            key = match_condition(label)
            if key is None or key in cond.values:
                continue
            value = None
            if inline is not None:
                value = _condition_value(key, inline, label)
            else:
                candidates = [raw.cell(r, c + 1), raw.cell(r, c + 2)]
                if header_row is None or r < header_row:
                    candidates.append(raw.cell(r + 1, c))
                for cand in candidates:
                    if cand in (None, ""):
                        continue
                    value = _condition_value(key, cand, label)
                    if value not in (None, ""):
                        break
            if value not in (None, ""):
                cond.set(key, value)
                origin[key] = "cell"
    return cond, origin


# ----------------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------------
def parse_sheet(raw: RawSheet,
                source: Source = Source.MEASUREMENT,
                header_row: Optional[int] = None,
                mapping: Optional[Dict[str, int]] = None,
                freq_override_hz: Optional[float] = None,
                fallback_name: str = "") -> SheetParseResult:
    """Parse one sheet. `header_row`/`mapping` given -> user Column Mapping mode."""
    res = SheetParseResult(raw.name, raw, freq_override_hz=freq_override_hz)
    msgs = res.messages

    if raw.is_empty():
        msgs.append(Message(Level.ERROR, "빈 Sheet입니다."))
        return res

    # 1-2. header + column mapping
    if mapping:
        res.user_mapping = True
        res.header_row = header_row
        cols = {k: c for k, c in mapping.items() if c is not None and c >= 0}
        hr = -1 if header_row is None else header_row
    else:
        hr_found, cols = find_header_row(raw)
        if hr_found is None:
            msgs.append(Message(Level.ERROR,
                                "Pout / PAE / Gain Header를 찾을 수 없습니다. Column Mapping을 확인해주세요."))
            # still try to read conditions so the panel is useful
            cond, _ = parse_conditions(raw, None, {}, [], msgs)
            res.header_row = None
            return res
        res.header_row = hr = hr_found
        dup = _duplicate_quantities(raw, hr)
        for q in dup:
            msgs.append(Message(Level.WARNING,
                                f"{QUANTITIES[q].label} column이 여러 개 있어 첫 번째({column_letter(cols[q])})를 사용합니다."))
    res.mapping = dict(cols)

    for q in REQUIRED:
        if q not in cols:
            msgs.append(Message(Level.ERROR,
                                f"{QUANTITIES[q].label} column을 찾을 수 없습니다. Column Mapping을 확인해주세요."))
            return res
    for q in EXPECTED:
        if q not in cols:
            msgs.append(Message(Level.WARNING,
                                f"{QUANTITIES[q].label} column을 찾을 수 없습니다. Column Mapping을 확인해주세요."))

    # 3. table
    data, rows_used = extract_table(raw, hr, cols, msgs)
    if not rows_used:
        msgs.append(Message(Level.ERROR, "숫자 데이터 행이 없습니다. Header 아래에 Pout 값이 있는지 확인해주세요."))
        return res
    _apply_unit_conversions(raw, hr if hr >= 0 else None, cols, data, msgs)
    for q in EXPECTED:
        if q in data and not np.isfinite(data[q]).any():
            msgs.append(Message(Level.WARNING, f"{QUANTITIES[q].label} column에 유효한 숫자가 없습니다."))

    # 4. conditions
    cond, origin = parse_conditions(raw, hr if hr >= 0 else None, cols, rows_used, msgs)

    # 5. frequency resolution
    name_hz = parse_frequency(raw.name) or (parse_frequency(fallback_name) if fallback_name else None)
    if freq_override_hz:
        cond.set("freq", float(freq_override_hz))
        cond.freq_source = "user"
    elif cond.freq_hz is not None:
        cond.freq_source = origin.get("freq", "cell")
        if name_hz is not None and not same_freq(name_hz, cond.freq_hz):
            msgs.append(Message(Level.WARNING,
                                f"Sheet 이름의 FREQ({format_freq(name_hz)})와 Sheet 내부 FREQ "
                                f"({format_freq(cond.freq_hz)})가 다릅니다. 내부 값을 사용합니다."))
    elif name_hz is not None:
        cond.set("freq", name_hz)
        cond.freq_source = "sheet_name"
    else:
        msgs.append(Message(Level.WARNING,
                            "FREQ 자동 인식에 실패했습니다. [FREQ 지정] 버튼으로 직접 입력해주세요."))

    from core.quantities import DEFAULT_DISPLAY_CONDITIONS
    for k in DEFAULT_DISPLAY_CONDITIONS:
        if k not in cond.values:
            msgs.append(Message(Level.INFO, f"{CONDITION_FIELDS[k].label} 조건을 찾지 못했습니다 (N/A 표시)."))

    res.dataset = Dataset(name=raw.name, source=source, condition=cond, columns=data,
                          origin=f"{raw.source_path}::{raw.name}")
    return res


def _duplicate_quantities(raw: RawSheet, header_row: int) -> List[str]:
    """Quantities with two equally good columns (real ambiguity)."""
    from core.quantities import header_priority
    seen: Dict[str, List[int]] = {}
    for v in raw.rows[header_row]:
        q = match_quantity(v) if isinstance(v, str) else None
        if q in ("pout", "pae", "gain", "pin"):
            seen.setdefault(q, []).append(header_priority(v))
    return [q for q, prs in seen.items() if len(prs) > 1 and prs.count(max(prs)) > 1]
