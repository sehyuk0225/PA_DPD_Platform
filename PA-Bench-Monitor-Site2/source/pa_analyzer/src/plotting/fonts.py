"""Plot fonts: Arial Narrow for text, a Korean font for Hangul (dataset names like '보드A').

matplotlib only falls back glyph-by-glyph across fonts when the font FAMILY is a list of real
font names (a generic 'sans-serif' resolves to ONE font). So the family is set to
[Arial Narrow (or replacement), Korean font, DejaVu Sans] - only fonts that exist on this PC.
"""
from __future__ import annotations

import matplotlib
from matplotlib import font_manager as fm

LATIN = ("Arial Narrow", "Liberation Sans Narrow", "Arial", "Liberation Sans")
KOREAN = ("Malgun Gothic", "맑은 고딕", "NanumGothic", "Noto Sans CJK KR", "Noto Sans KR", "AppleGothic",
          "Noto Serif CJK KR", "Gulim", "Dotum", "Batang")


def _exists(name: str) -> bool:
    try:
        fm.findfont(fm.FontProperties(family=name), fallback_to_default=False)
        return True
    except Exception:
        return False


def _first(names):
    return next((n for n in names if _exists(n)), None)


LATIN_FONT = _first(LATIN) or "DejaVu Sans"
KOREAN_FONT = _first(KOREAN)
FAMILY = [LATIN_FONT] + ([KOREAN_FONT] if KOREAN_FONT else []) + (["DejaVu Sans"] if LATIN_FONT != "DejaVu Sans" else [])


def setup() -> None:
    matplotlib.rcParams["font.family"] = list(FAMILY)
    matplotlib.rcParams["font.sans-serif"] = list(FAMILY) + [f for f in matplotlib.rcParams["font.sans-serif"]
                                                             if f not in FAMILY]
    matplotlib.rcParams["axes.unicode_minus"] = False       # plain '-' : every font has it


setup()
