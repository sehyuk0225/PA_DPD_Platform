"""Line favourites (선1 / 선2): two saved line looks to tell curves apart at a glance.

Default 선1 = normal width, dashed, no dots; 선2 = thick, solid, no dots (the contrast to 선1).
Right-click a button to change its look; it is remembered on this PC.
"""
from __future__ import annotations

import json

DEFAULTS = {1: {"linestyle": "--", "linewidth": 1.2, "marker": ""},
            2: {"linestyle": "-", "linewidth": 2.4, "marker": ""}}
LINESTYLES = (("-", "실선"), ("--", "파선"), (":", "점선"), ("-.", "1점 쇄선"))
WIDTHS = ((0.8, "가는"), (1.2, "보통"), (2.4, "굵은"), (3.6, "아주 굵은"))
MARKERS = (("", "점 없음"), ("o", "● 원"), ("s", "■ 사각"), ("^", "▲ 삼각"))


def load(settings, i: int) -> dict:
    try:
        st = json.loads(str(settings.value(f"line_fav/{i}", "") or "{}"))
    except (ValueError, TypeError):
        st = {}
    out = dict(DEFAULTS[i])
    out.update({k: v for k, v in (st or {}).items() if k in out})
    return out


def save(settings, i: int, st: dict) -> None:
    settings.setValue(f"line_fav/{i}", json.dumps(st))


def reset(settings, i: int) -> None:
    save(settings, i, dict(DEFAULTS[i]))


def describe(st: dict) -> str:
    w = next((t for v, t in WIDTHS if abs(v - float(st.get("linewidth", 1.2))) < 1e-6), f"{st.get('linewidth')}pt")
    ls = dict(LINESTYLES).get(st.get("linestyle"), st.get("linestyle"))
    mk = dict(MARKERS).get(st.get("marker") or "", st.get("marker"))
    return f"{w} {ls} · {mk}"


def glyph(st: dict) -> str:
    base = {"-": "━", "--": "╍", ":": "┈", "-.": "━·"}.get(st.get("linestyle"), "━")
    return base if float(st.get("linewidth", 1.2)) >= 2 else {"━": "─", "╍": "╌", "┈": "┈", "━·": "─·"}[base]
