"""Project file (*.pam): the analyzer's own save format.

Stores the raw cell grids of every dataset (opened sheets and pasted data)
plus the user's corrections (FREQ override, Column Mapping), so reopening a
project gives exactly the same plots - without needing the original Excel.
Plain UTF-8 JSON, written by this program.
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import os
from typing import Optional

from core.models import Level, Message, Source
from excel.reader import RawSheet, ReaderError
from excel.sheet_parser import parse_sheet
from measurement.loader import MeasurementFile

EXT = ".pam"
FORMAT = "PA-Measurement-Analyzer project"
VERSION = 1


def _cell(v):
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, (int, float, str)) or v is None:
        return v
    return str(v)  # dates etc.


def save_project(mf: MeasurementFile, path: str, extra: Optional[dict] = None) -> str:
    if not path.lower().endswith(EXT):
        path += EXT
    doc = {
        "format": FORMAT, "version": VERSION,
        "saved": _dt.datetime.now().isoformat(timespec="seconds"),
        "source_file": mf.path if not mf.path.lower().endswith(EXT) else "",
        "sheets": [{
            "name": s.sheet_name,
            "display": s.display_name,
            "rows": [[_cell(v) for v in row] for row in s.raw.rows],
            "freq_override_hz": s.freq_override_hz,
            "user_mapping": s.user_mapping,
            "header_row": s.header_row if s.user_mapping else None,
            "mapping": s.mapping if s.user_mapping else None,
            "added_at": getattr(s, "added_at", None), "added_how": getattr(s, "added_how", ""),
            "modified_at": getattr(s, "modified_at", None),
        } for s in mf.sheets],
        "settings": extra or {},
    }
    _write_safely(path, doc)
    check = verify_project(path, [s.sheet_name for s in mf.sheets], [s.display_name for s in mf.sheets])
    if check:
        raise ProjectSaveError(check)
    mf.path = path
    mf.dirty = False
    return path


class ProjectSaveError(Exception):
    """The project could not be written (or what is on disk is not what we wrote)."""


def _write_safely(path: str, doc: dict) -> None:
    """Write tmp -> flush to disk -> swap in. Company PCs (DRM / antivirus / cloud sync) sometimes
    lock the old file for a moment: retry, then fall back to writing the file directly.
    The previous version is kept as <name>.pam.bak."""
    import shutil
    import time
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    data = json.dumps(doc, ensure_ascii=False, default=_cell)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    if os.path.isfile(path):
        try:
            shutil.copy2(path, path + ".bak")
        except OSError:
            pass
    last = None
    for _ in range(10):                          # ~2 s in total
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:           # locked for a moment
            last = exc
            time.sleep(0.2)
    try:                                         # still locked for renames: overwrite in place
        with open(path, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.remove(tmp)
        except OSError:
            pass
    except OSError as exc:
        raise ProjectSaveError(f"파일에 쓸 수 없습니다 ({exc or last}).\n"
                               f"다른 프로그램이 파일을 사용 중이거나 쓰기 권한이 없는 위치일 수 있습니다.")


def verify_project(path: str, names, displays) -> str:
    """Read the saved file back: '' if every dataset (and its name) is there, else what is wrong."""
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as exc:
        return f"저장한 파일을 다시 읽을 수 없습니다: {exc}"
    sheets = doc.get("sheets", [])
    if [sh.get("name") for sh in sheets] != list(names):
        return f"저장 확인 실패: Dataset {len(names)}개를 저장했는데 파일에는 {len(sheets)}개가 있습니다."
    if [sh.get("display") for sh in sheets] != list(displays):
        return "저장 확인 실패: Dataset 이름이 파일에 그대로 저장되지 않았습니다."
    return ""


def load_project(path: str) -> MeasurementFile:
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as exc:
        raise ReaderError(f"프로젝트 파일을 열 수 없습니다: {exc}")
    if doc.get("format") != FORMAT:
        raise ReaderError("PA Measurement Analyzer 프로젝트 파일이 아닙니다.")
    mf = MeasurementFile(path)
    for sh in doc.get("sheets", []):
        raw = RawSheet(sh.get("name", "Sheet"), sh.get("rows", []), path)
        mapping = {k: int(v) for k, v in (sh.get("mapping") or {}).items()} if sh.get("user_mapping") else None
        res = parse_sheet(raw, Source.MEASUREMENT, sh.get("header_row") if mapping else None, mapping,
                          sh.get("freq_override_hz"))
        res.display_name = sh.get("display")
        res.added_at, res.added_how = sh.get("added_at"), sh.get("added_how") or ""
        res.modified_at = sh.get("modified_at")
        mf.sheets.append(res)
    mf.messages.append(Message(Level.INFO, f"프로젝트 {os.path.basename(path)}: Dataset {len(mf.datasets)}개"))
    mf.settings = doc.get("settings", {})
    return mf
