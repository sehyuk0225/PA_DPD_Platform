"""File access: detect the real format and turn text / Excel into cell grids."""
from __future__ import annotations

import csv
import os
from typing import List, Tuple

from sparam.core.models import LoadError

SAVE_AS_HINT = "Excel에서 열어 [다른 이름으로 저장 → CSV 또는 Excel 통합 문서(*.xlsx)]로 저장한 후 다시 추가해주세요."
_DRM = (b"FASOO", b"SCDSA", b"MarkAny", b"DOCUMENTSAFER", b"DRMONE", b"<## ")


def detect_format(path: str) -> str:
    """'xlsx' | 'ole' | 'drm' | 'text' | 'unknown' from the file content."""
    with open(path, "rb") as f:
        head = f.read(4096)
    if head.startswith(b"PK\x03\x04"):
        return "xlsx"
    if any(m in head[:512] for m in _DRM):
        return "drm"
    if head.startswith(b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1"):
        return "ole"
    for enc in ("utf-8", "cp949"):
        try:
            text = head.decode(enc)
            break
        except UnicodeDecodeError:
            text = None
    if not text:
        return "text" if not head else "unknown"
    printable = sum(ch.isprintable() or ch in "\r\n\t" for ch in text)
    return "text" if printable / len(text) > 0.97 else "unknown"


def read_text(path: str) -> str:
    for enc in ("utf-8-sig", "cp949", "latin-1"):
        try:
            with open(path, "r", encoding=enc, newline="") as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    raise LoadError(f"파일을 읽을 수 없습니다: {os.path.basename(path)}")


def guess_delimiter(raw: str) -> str:
    lines = [l for l in raw.splitlines()[:200] if l.strip() and not l.lstrip().startswith(("#", "!"))]
    if not lines:
        return ","
    share = {d: sum(d in l for l in lines) / len(lines) for d in ("\t", ";", ",")}
    if share["\t"] >= 0.5:
        return "\t"
    if share[";"] >= 0.5 and share[";"] >= share[","]:
        return ";"
    if share[","] >= 0.5:
        return ","
    return " "  # whitespace separated


def _merge_parens(cells: list, sep: str) -> list:
    """ADS-style headers 'dB(S(2,1))' contain the delimiter: glue the pieces back."""
    out, buf = [], None
    for c in cells:
        piece = "" if c is None else str(c)
        buf = piece if buf is None else buf + sep + piece
        if buf.count("(") <= buf.count(")"):
            out.append(buf.strip() or None)
            buf = None
    if buf is not None:
        out.append(buf.strip() or None)
    return out


def text_rows(raw: str) -> List[list]:
    d = guess_delimiter(raw)
    if d == " ":
        return [l.split() for l in raw.splitlines()]
    rows = []
    for r in csv.reader(raw.splitlines(), delimiter=d):
        if any("(" in c for c in r):
            r = _merge_parens(r, d)
        rows.append([c.strip() if c and c.strip() != "" else None for c in r])
    return rows


def excel_sheets(path: str) -> List[Tuple[str, List[list]]]:
    try:
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except PermissionError:
        raise LoadError("파일이 다른 프로그램에서 잠겨 있습니다. Excel에서 닫은 후 다시 시도해주세요.")
    except Exception as exc:
        raise LoadError(f"Excel 파일을 열 수 없습니다: {exc}")
    out = []
    try:
        for ws in wb.worksheets:
            rows = []
            for i, r in enumerate(ws.iter_rows(values_only=True)):
                if i > 100000:
                    break
                rows.append(list(r))
            out.append((ws.title, rows))
    finally:
        wb.close()
    return out


def check_readable(kind: str) -> None:
    if kind == "drm":
        raise LoadError("회사 보안(DRM)으로 암호화된 파일이라 읽을 수 없습니다. DRM이 해제된 사본이 필요합니다.")
    if kind == "ole":
        raise LoadError("구 Excel(.xls), 암호가 걸린 파일, 또는 DRM 파일입니다.\n" + SAVE_AS_HINT)
    if kind == "unknown":
        raise LoadError("지원하지 않거나 손상된 파일입니다.\n" + SAVE_AS_HINT)
