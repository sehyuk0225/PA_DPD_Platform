"""Excel Reader: file -> list of RawSheet (plain 2-D cell grids).

Nothing here knows about Pout / PAE; it only turns files into grids so the
Sheet Parser can work identically on .xlsx sheets and .csv files.
"""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from typing import List

MAX_ROWS = 20000       # safety limit per sheet
MAX_COLS = 200


class ReaderError(Exception):
    """User-facing error while opening a file."""


@dataclass
class RawSheet:
    name: str
    rows: List[list] = field(default_factory=list)
    source_path: str = ""

    @property
    def n_rows(self) -> int:
        return len(self.rows)

    @property
    def n_cols(self) -> int:
        return max((len(r) for r in self.rows), default=0)

    def cell(self, r: int, c: int):
        if 0 <= r < len(self.rows) and 0 <= c < len(self.rows[r]):
            return self.rows[r][c]
        return None

    def is_empty(self) -> bool:
        return not any(v not in (None, "") for row in self.rows for v in row)


def column_letter(idx: int) -> str:
    """0 -> 'A', 25 -> 'Z', 26 -> 'AA'."""
    s, n = "", idx + 1
    while n:
        n, rem = divmod(n - 1, 26)
        s = chr(65 + rem) + s
    return s


def _trim(rows: List[list]) -> List[list]:
    # strip trailing empty cells / rows to keep grids small
    out = []
    for r in rows:
        r = list(r[:MAX_COLS])
        while r and r[-1] in (None, ""):
            r.pop()
        out.append(r)
    while out and not out[-1]:
        out.pop()
    return out


_DRM_MARKERS = (b"FASOO", b"FSD", b"SCDSA", b"MarkAny", b"DOCUMENTSAFER", b"DRMONE", b"<## ")

SAVE_AS_HINT = ("Excel에서 파일을 연 뒤 [파일 → 다른 이름으로 저장 → 'Excel 통합 문서 (*.xlsx)']로 "
                "저장하고 다시 열어주세요.")


def detect_format(path: str) -> str:
    """Look at the file's real content (not its extension).

    Returns 'xlsx' | 'ole' (old .xls, password-protected or DRM-wrapped Office file)
            | 'drm' | 'html' | 'text' | 'unknown'
    """
    with open(path, "rb") as f:
        head = f.read(4096)
    if head.startswith(b"PK\x03\x04"):
        return "xlsx"
    if any(m in head[:512] for m in _DRM_MARKERS):
        return "drm"
    if head.startswith(b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1"):
        return "ole"
    stripped = head.lstrip(b"\xef\xbb\xbf\xff\xfe\r\n\t ").lower()
    if stripped.startswith((b"<html", b"<!doctype", b"<table", b"<?xml", b"<meta")):
        return "html"
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = head.decode("cp949")
        except UnicodeDecodeError:
            return "unknown"
    printable = sum(ch.isprintable() or ch in "\r\n\t" for ch in text)
    return "text" if text and printable / len(text) > 0.97 else "unknown"


def read_workbook(path: str) -> List[RawSheet]:
    """Read every worksheet of an .xlsx/.xlsm file (cached values, not formulas).

    The real content decides how the file is read, so instrument exports that
    are really CSV/TSV text saved with an .xls/.xlsx extension still work.
    """
    if not os.path.isfile(path):
        raise ReaderError(f"파일을 찾을 수 없습니다: {path}")
    ext = os.path.splitext(path)[1].lower()
    if ext in (".csv", ".txt", ".tsv", ".dat"):
        return [read_csv(path)]
    if ext not in (".xlsx", ".xlsm", ".xls", ""):
        raise ReaderError(f"지원하지 않는 파일 형식입니다: {ext} (.xlsx / .xlsm / .xls / .csv / .txt 지원)")
    try:
        kind = detect_format(path)
    except OSError as exc:
        raise ReaderError(f"파일에 접근할 수 없습니다 (다른 프로그램에서 열려 있는지 확인): {exc}")
    if kind == "text":
        return [read_csv(path)]  # e.g. tab-separated text from measurement software
    if kind == "drm":
        raise ReaderError("회사 보안(DRM)으로 암호화된 파일이라 Excel 외의 프로그램에서는 읽을 수 없습니다.\n"
                          "DRM이 해제된 사본이 필요합니다 (보안 담당 부서의 반출/해제 절차 확인).")
    if kind == "ole":
        raise ReaderError("이 파일은 실제 .xlsx 형식이 아닙니다. 다음 중 하나입니다.\n"
                          " • 구 Excel 형식(.xls)\n"
                          " • 암호가 걸린 Excel 파일\n"
                          " • 회사 보안(DRM)으로 암호화된 파일\n\n"
                          + SAVE_AS_HINT + "\n(DRM 파일이면 저장한 사본도 암호화될 수 있어 보안 해제가 필요합니다.)")
    if kind == "html":
        raise ReaderError("이 파일은 Excel 모양의 HTML/XML 파일입니다 (측정 장비/웹 내보내기에서 흔함).\n" + SAVE_AS_HINT)
    if kind == "unknown":
        raise ReaderError("Excel(.xlsx) 파일이 아니거나 손상/암호화된 파일입니다.\n" + SAVE_AS_HINT)

    try:
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except PermissionError:
        raise ReaderError("파일에 접근할 수 없습니다. 다른 프로그램(Excel 등)에서 잠겨 있는지 확인해주세요.")
    except Exception as exc:  # corrupt file, wrong format, ...
        raise ReaderError(f"Excel 파일을 열 수 없습니다: {exc}")

    sheets: List[RawSheet] = []
    try:
        for ws in wb.worksheets:
            rows = []
            try:
                for i, row in enumerate(ws.iter_rows(values_only=True)):
                    if i >= MAX_ROWS:
                        break
                    rows.append(list(row))
            except Exception:
                rows = []  # unreadable sheet (chart sheet etc.) -> treated as empty
            sheets.append(RawSheet(ws.title, _trim(rows), path))
    finally:
        wb.close()
    if not sheets:
        raise ReaderError("Workbook에 Sheet가 없습니다.")
    return sheets


def _guess_delimiter(raw: str) -> str:
    """Tab if most lines have tabs (instrument exports), else ';' or ','.
    (csv.Sniffer is fooled by labels like 'VGS,CA' inside tab-separated files.)"""
    lines = [l for l in raw.splitlines()[:200] if l.strip() and not l.lstrip().startswith("#")]
    if not lines:
        return ","
    share = {d: sum(d in l for l in lines) / len(lines) for d in ("\t", ";", ",")}
    if share["\t"] >= 0.5:
        return "\t"
    if share[";"] >= 0.5 and share[";"] >= share[","]:
        return ";"
    if share[","] >= 0.5:
        return ","
    return " "          # Notepad files aligned with spaces


def _split_rows(raw: str):
    d = _guess_delimiter(raw)
    lines = raw.splitlines()
    if d == " ":
        return [l.split() for l in lines]
    return [list(r) for r in csv.reader(lines, delimiter=d)]


def read_csv(path: str, name: str = "") -> RawSheet:
    """Read a CSV (comma / semicolon / tab auto-detected; '#' lines kept as cells)."""
    raw = None
    for enc in ("utf-8-sig", "cp949", "latin-1"):
        try:
            with open(path, "r", encoding=enc, newline="") as f:
                raw = f.read()
            break
        except UnicodeDecodeError:
            continue
    if raw is None:
        raise ReaderError(f"CSV 파일을 읽을 수 없습니다: {path}")
    rows = _split_rows(raw)
    rows = [[(c if c.strip() != "" else None) for c in r] for r in rows]
    return RawSheet(name or os.path.splitext(os.path.basename(path))[0], _trim(rows), path)


def read_clipboard_text(text: str, name: str) -> RawSheet:
    """Cells copied from Excel arrive as tab-separated text (CRLF lines)."""
    if not text or not text.strip():
        raise ReaderError("Clipboard가 비어 있습니다. Excel에서 데이터 범위를 복사(Ctrl+C)한 후 다시 붙여넣어 주세요.")
    raw = text.replace("\r\n", "\n").replace("\r", "\n")
    rows = [list(r) for r in csv.reader(raw.split("\n"), delimiter=_guess_delimiter(raw))]
    rows = [[(c.strip() if c.strip() != "" else None) for c in r] for r in rows]
    sheet = RawSheet(name, _trim(rows), "")
    if sheet.is_empty():
        raise ReaderError("붙여넣은 내용에 데이터가 없습니다.")
    return sheet
