"""Column Mapping favourites ('맵핑1', '맵핑2' …): remember a mapping once, apply it to other data.

A preset stores the header row and, per quantity, the column's HEADER TEXT and its index.
Applying it looks the header text up in the data's header row first (columns may have moved),
and falls back to the stored column index.
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional, Tuple

KEY = "mapping_presets"


def _norm(t) -> str:
    return " ".join(str(t or "").split()).lower()


def make_preset(name: str, result, header_row: Optional[int], mapping: Dict[str, int]) -> dict:
    return {"name": name, "header_row": header_row,
            "index": {q: int(c) for q, c in mapping.items() if c is not None and c >= 0},
            "text": {q: (str(result.raw.cell(header_row, c)).strip() if header_row is not None
                         and result.raw.cell(header_row, c) is not None else "")
                     for q, c in mapping.items() if c is not None and c >= 0}}


def resolve(preset: dict, result) -> Tuple[Optional[int], Dict[str, int]]:
    """(header_row, mapping) of `preset` for this dataset's cells."""
    hr = preset.get("header_row")
    n = result.raw.n_cols
    headers = {}
    if hr is not None:
        for c in range(n):
            t = _norm(result.raw.cell(hr, c))
            if t and t not in headers:
                headers[t] = c
    out = {}
    for q, idx in (preset.get("index") or {}).items():
        t = _norm((preset.get("text") or {}).get(q))
        if t and t in headers:
            out[q] = headers[t]                     # same header text -> that column (even if it moved)
        elif 0 <= int(idx) < max(n, 1):
            out[q] = int(idx)
    return hr, out


def load(settings) -> List[dict]:
    try:
        return list(json.loads(str(settings.value(KEY, "") or "[]")))
    except (ValueError, TypeError):
        return []


def save(settings, presets: List[dict]) -> None:
    settings.setValue(KEY, json.dumps(presets, ensure_ascii=False))


def next_name(presets: List[dict]) -> str:
    names = {p.get("name") for p in presets}
    i = 1
    while f"맵핑{i}" in names:
        i += 1
    return f"맵핑{i}"


def upsert(presets: List[dict], preset: dict) -> List[dict]:
    return [p for p in presets if p.get("name") != preset["name"]] + [preset]
