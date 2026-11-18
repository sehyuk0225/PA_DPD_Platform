"""load_file(path) -> list[SParamData]  (Excel workbooks give one entry per sheet)."""
from __future__ import annotations

import os
import re
from typing import List

from sparam.core.models import LoadError, SParamData
from sparam.loaders.files import check_readable, detect_format, excel_sheets, read_text, text_rows
from sparam.loaders.table import parse_table
from sparam.loaders.touchstone import n_ports_from_ext, parse_touchstone

SUPPORTED = (".csv", ".txt", ".tsv", ".dat", ".xlsx", ".xlsm", ".xls")
FILE_FILTER = ("S-parameter files (*.csv *.txt *.tsv *.dat *.xlsx *.xlsm *.xls *.s1p *.s2p *.s3p *.s4p *.snp);;"
               "All files (*.*)")


def is_supported(path: str) -> bool:
    p = path.lower()
    return p.endswith(SUPPORTED) or bool(re.search(r"\.s\d+p$", p))


def load_file(path: str) -> List[SParamData]:
    if not os.path.isfile(path):
        raise LoadError(f"파일을 찾을 수 없습니다: {path}")
    base = os.path.splitext(os.path.basename(path))[0]
    low = path.lower()
    try:
        kind = detect_format(path)
    except OSError as exc:
        raise LoadError(f"파일에 접근할 수 없습니다: {exc}")

    if re.search(r"\.s\d+p$", low) or low.endswith(".snp"):
        check_readable(kind if kind != "xlsx" else "unknown")
        return [parse_touchstone(read_text(path), base, path, n_ports_from_ext(path))]
    if not is_supported(path):
        raise LoadError(f"지원하지 않는 형식입니다: {os.path.basename(path)}")
    if kind == "xlsx":
        out, errors = [], []
        sheets = excel_sheets(path)
        for title, rows in sheets:
            name = base if len(sheets) == 1 else f"{base} [{title}]"
            try:
                out.append(parse_table(rows, name, path, kind="excel"))
            except LoadError as exc:
                errors.append(str(exc))
        if not out:
            raise LoadError("\n".join(errors) or "데이터가 있는 Sheet가 없습니다.")
        return out
    check_readable(kind)
    return [parse_table(text_rows(read_text(path)), base, path, kind="csv")]
