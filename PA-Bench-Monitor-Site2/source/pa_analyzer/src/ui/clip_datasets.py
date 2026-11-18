"""Copy / paste whole datasets between Datasets lists - also into ANOTHER running copy of the program.

Ctrl+C on the list puts the selected datasets on the Windows clipboard twice:
  * in the program's own format (exact data, name, FREQ, column mapping, colour / line style)
  * as plain tab-separated text (so pasting into Excel still gives the numbers)
Ctrl+V in a Datasets list adds them back exactly (no questions asked).
"""
from __future__ import annotations

import json
from typing import Optional

MIME_PA = "application/x-rf-pa-analyzer-pa-datasets"
MIME_SP = "application/x-rf-pa-analyzer-sparam-files"


def put(mime_type: str, payload: dict, text: str) -> None:
    from PySide6.QtCore import QByteArray, QMimeData
    from PySide6.QtGui import QGuiApplication
    md = QMimeData()
    md.setData(mime_type, QByteArray(json.dumps(payload, ensure_ascii=False, allow_nan=True).encode("utf-8")))
    md.setText(text)
    QGuiApplication.clipboard().setMimeData(md)


def get(mime_type: str) -> Optional[dict]:
    """The copied datasets if the clipboard holds them (from this or another copy of the program)."""
    from PySide6.QtGui import QGuiApplication
    md = QGuiApplication.clipboard().mimeData()
    try:
        if md is None or not md.hasFormat(mime_type):
            return None
        raw = md.data(mime_type)
        return json.loads(bytes(raw).decode("utf-8"))
    except (ValueError, TypeError, AttributeError):
        return None


def table_text(blocks) -> str:
    """[(title, rows)] -> TSV text: each dataset under its name, separated by an empty line."""
    out = []
    for title, rows in blocks:
        out.append(str(title))
        for r in rows:
            out.append("\t".join("" if v is None else (repr(v)[:-2] if isinstance(v, float) and repr(v).endswith(".0")
                                                      else str(v)) for v in r))
        out.append("")
    return "\n".join(out).rstrip("\n")
