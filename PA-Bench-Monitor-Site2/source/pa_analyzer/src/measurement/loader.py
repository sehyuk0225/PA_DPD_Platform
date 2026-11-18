"""Measurement File: one workbook, one Dataset per sheet."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from core.models import Dataset, Level, Message, Source
from excel.reader import RawSheet, ReaderError, read_workbook
from excel.sheet_parser import SheetParseResult, parse_sheet
from utils.freq import format_freq


@dataclass
class MeasurementFile:
    path: str
    sheets: List[SheetParseResult] = field(default_factory=list)
    messages: List[Message] = field(default_factory=list)
    dirty: bool = False          # pasted / edited since the last project save

    @property
    def file_name(self) -> str:
        return os.path.basename(self.path)

    # ---- queries ---------------------------------------------------------
    def sheet(self, name: str) -> Optional[SheetParseResult]:
        """Dataset by key. Indexed (hundreds of datasets: this is called per curve, per redraw)."""
        idx = self.__dict__.get("_idx")
        if idx is None or idx[0] is not self.sheets or idx[1] != len(self.sheets):
            idx = self._reindex()
        s = idx[2].get(name)
        if s is not None and s.sheet_name == name:
            return s
        idx = self._reindex()                       # renamed / replaced since: look again
        return idx[2].get(name)

    def _reindex(self):
        d = {}
        for s in self.sheets:
            d.setdefault(s.sheet_name, s)
        idx = (self.sheets, len(self.sheets), d)
        self.__dict__["_idx"] = idx
        return idx

    @property
    def datasets(self) -> List[Dataset]:
        return [s.dataset for s in self.sheets if s.dataset is not None]

    def sheet_label(self, name: str) -> str:
        """Label for the Frequency selector, e.g. '3.60 GHz   [3.6GHz]'."""
        s = self.sheet(name)
        if s is None:
            return name
        shown = s.label
        if not s.ok:
            return f"⚠ {shown}   (데이터 없음)"
        hz = s.freq_hz
        if hz is None:
            return f"? FREQ 미지정   [{shown}]"
        return f"{format_freq(hz)}   [{shown}]"

    def ordered_sheet_names(self) -> List[str]:
        """Valid sheets sorted by frequency first, then the rest in workbook order."""
        valid = [s for s in self.sheets if s.ok and s.freq_hz is not None]
        valid.sort(key=lambda s: s.freq_hz)
        rest = [s for s in self.sheets if s not in valid]
        return [s.sheet_name for s in valid + rest]

    def default_sheet_name(self) -> Optional[str]:
        names = self.ordered_sheet_names()
        for n in names:
            if self.sheet(n).ok:
                return n
        return names[0] if names else None

    # ---- user corrections -----------------------------------------------
    def reparse(self, name: str, header_row: Optional[int] = None,
                mapping: Optional[Dict[str, int]] = None,
                freq_override_hz: Optional[float] = None,
                keep_mapping: bool = True) -> SheetParseResult:
        old = self.sheet(name)
        if old is None:
            raise KeyError(name)
        if keep_mapping and mapping is None and old.user_mapping:
            header_row, mapping = old.header_row, old.mapping
        if freq_override_hz is None:
            freq_override_hz = old.freq_override_hz
        new = parse_sheet(old.raw, Source.MEASUREMENT, header_row, mapping, freq_override_hz,
                          fallback_name=self.file_name)
        new.display_name = old.display_name
        _carry_info(old, new)
        self.sheets[self.sheets.index(old)] = new
        self.__dict__.pop("_idx", None)
        return new

    def replace_rows(self, name: str, rows) -> SheetParseResult:
        """New cell grid for one dataset (data editor). A NEW RawSheet so undo can bring the old one back."""
        from excel.reader import RawSheet
        old = self.sheet(name)
        if old is None:
            raise KeyError(name)
        raw = RawSheet(old.raw.name, [list(r) for r in rows], old.raw.source_path)
        header_row, mapping = (old.header_row, old.mapping) if old.user_mapping else (None, None)
        new = parse_sheet(raw, Source.MEASUREMENT, header_row, mapping, old.freq_override_hz,
                          fallback_name=self.file_name)
        new.display_name = old.display_name
        _carry_info(old, new)
        self.sheets[self.sheets.index(old)] = new
        self.__dict__.pop("_idx", None)
        self.dirty = True
        return new

    def set_frequency(self, name: str, hz: float) -> SheetParseResult:
        self.dirty = True
        return self.reparse(name, freq_override_hz=hz)

    def unique_name(self, base: str) -> str:
        names = {s.sheet_name for s in self.sheets}
        if base not in names:
            return base
        i = 2
        while f"{base} ({i})" in names:
            i += 1
        return f"{base} ({i})"

    def add_raw_sheet(self, raw: RawSheet, freq_override_hz: Optional[float] = None,
                      display_name: Optional[str] = None, how: str = "붙여넣기 (Excel 등에서 복사)") -> SheetParseResult:
        """Add pasted data as a new dataset. The internal key is made unique; the
        display name may repeat (e.g. several pastes called 'Board7')."""
        wanted = display_name or raw.name
        raw.name = self.unique_name(raw.name)
        res = parse_sheet(raw, Source.MEASUREMENT, freq_override_hz=freq_override_hz)
        res.display_name = wanted if wanted != raw.name else None
        res.added_at, res.added_how = now_text(), how
        self.sheets.append(res)
        self.dirty = True
        return res

    def remove_sheet(self, name: str) -> None:
        self.sheets = [s for s in self.sheets if s.sheet_name != name]
        self.dirty = True

    def rename_sheet(self, old: str, new: str) -> str:
        r = self.sheet(old)
        new = (new or "").strip()
        if r is None or not new:
            return old
        r.display_name = None if new == r.sheet_name else new     # duplicates allowed
        self.dirty = True
        return old

    def apply_mapping(self, name: str, header_row: Optional[int], mapping: Dict[str, int]) -> SheetParseResult:
        self.dirty = True
        return self.reparse(name, header_row=header_row, mapping=mapping, keep_mapping=False)


def now_text() -> str:
    import datetime as _dt
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def _carry_info(old, new) -> None:
    """Re-parsed (mapping / FREQ / edited data): keep when and how it was added, note the change."""
    new.added_at, new.added_how = getattr(old, "added_at", None), getattr(old, "added_how", "")
    new.modified_at = now_text()


def load_measurement_file(path: str) -> MeasurementFile:
    """Read a workbook and parse every sheet. Raises ReaderError only if the file can't be opened."""
    raws: List[RawSheet] = read_workbook(path)
    mf = MeasurementFile(path)
    for raw in raws:
        try:
            res = parse_sheet(raw, Source.MEASUREMENT,
                              fallback_name=os.path.basename(path) if len(raws) == 1 else "")
        except Exception as exc:  # defensive: never crash the app on one bad sheet
            res = SheetParseResult(raw.name, raw)
            res.messages.append(Message(Level.ERROR, f"Sheet 분석 중 오류: {exc}"))
        res.added_at, res.added_how = now_text(), f"파일 열기: {os.path.basename(path)} · Sheet '{raw.name}'"
        mf.sheets.append(res)

    n_ok = sum(1 for s in mf.sheets if s.ok)
    mf.messages.append(Message(Level.INFO, f"{mf.file_name}: Sheet {len(mf.sheets)}개 중 "
                                           f"{n_ok}개를 Measurement Dataset으로 인식했습니다."))
    if n_ok == 0:
        mf.messages.append(Message(Level.ERROR, "인식 가능한 Measurement 데이터가 없습니다. "
                                                "Column Mapping으로 직접 지정해주세요."))
    freqs = [s.freq_hz for s in mf.sheets if s.ok and s.freq_hz is not None]
    if len(freqs) != len(set(round(f / 1e6, 3) for f in freqs)):
        mf.messages.append(Message(Level.WARNING, "동일한 FREQ를 가진 Sheet가 여러 개 있습니다."))
    return mf


__all__ = ["MeasurementFile", "load_measurement_file", "ReaderError"]
