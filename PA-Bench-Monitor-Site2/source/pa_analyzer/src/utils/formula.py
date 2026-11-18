"""Excel-like formulas for the data editor:  =A1+B2*2,  =(C3-C2)/2,  =$A$1*B4 …

Only + - * / , parentheses, numbers and cell references (A1, $A$1, AA10). No eval():
a small parser computes the value. Relative references move when a formula is filled
to another cell (fill handle), $-fixed parts stay.
"""
from __future__ import annotations

import re
from typing import Callable, Dict, Optional, Tuple

REF = re.compile(r"(\$?)([A-Za-z]{1,3})(\$?)(\d+)")


class FormulaError(Exception):
    pass


def col_name(c: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    s = ""
    c += 1
    while c:
        c, r = divmod(c - 1, 26)
        s = chr(65 + r) + s
    return s


def col_index(name: str) -> int:
    n = 0
    for ch in name.upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def ref_name(r: int, c: int) -> str:
    return f"{col_name(c)}{r + 1}"


def is_formula(text) -> bool:
    return isinstance(text, str) and text.strip().startswith("=")


def shift_formula(formula: str, dr: int, dc: int) -> str:
    """Move the relative references of `formula` by dr rows / dc columns ($ parts stay)."""
    def sub(m):
        c_abs, col, r_abs, row = m.groups()
        c = col_index(col) if c_abs else col_index(col) + dc
        r = int(row) - 1 if r_abs else int(row) - 1 + dr
        if c < 0 or r < 0:
            return "#REF!"
        return f"{c_abs}{col_name(c)}{r_abs}{r + 1}"
    return REF.sub(sub, formula)


def _tokens(expr: str):
    i, out = 0, []
    while i < len(expr):
        ch = expr[i]
        if ch.isspace():
            i += 1
            continue
        if ch in "+-*/()":
            out.append(ch)
            i += 1
            continue
        m = REF.match(expr, i)
        if m:
            out.append(("ref", col_index(m.group(2)), int(m.group(4)) - 1))
            i = m.end()
            continue
        m = re.match(r"\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?", expr[i:])
        if m:
            out.append(("num", float(m.group(0))))
            i += len(m.group(0))
            continue
        raise FormulaError(f"알 수 없는 문자 '{ch}'")
    return out


def evaluate(formula: str, value_of: Callable[[int, int], Optional[float]]) -> float:
    """Value of '=...'. value_of(row, col) gives a cell's number (None / text -> error)."""
    toks = _tokens(formula.strip().lstrip("="))
    pos = [0]

    def peek():
        return toks[pos[0]] if pos[0] < len(toks) else None

    def take():
        t = peek()
        pos[0] += 1
        return t

    def atom():
        t = take()
        if t is None:
            raise FormulaError("식이 끝나지 않았습니다")
        if t == "(":
            v = expr()
            if take() != ")":
                raise FormulaError("괄호가 닫히지 않았습니다")
            return v
        if t == "-":
            return -atom()
        if t == "+":
            return atom()
        if isinstance(t, tuple) and t[0] == "num":
            return t[1]
        if isinstance(t, tuple) and t[0] == "ref":
            v = value_of(t[2], t[1])
            if v is None:
                return 0.0                       # empty cell counts as 0 (like Excel)
            if not isinstance(v, (int, float)):
                raise FormulaError(f"{ref_name(t[2], t[1])} 은(는) 숫자가 아닙니다")
            return float(v)
        raise FormulaError("식이 올바르지 않습니다")

    def term():
        v = atom()
        while peek() in ("*", "/"):
            op = take()
            w = atom()
            if op == "/" and w == 0:
                raise FormulaError("0으로 나눌 수 없습니다")
            v = v * w if op == "*" else v / w
        return v

    def expr():
        v = term()
        while peek() in ("+", "-"):
            v = v + term() if take() == "+" else v - term()
        return v

    v = expr()
    if peek() is not None:
        raise FormulaError("식이 올바르지 않습니다")
    return v


def evaluate_all(cells: Dict[Tuple[int, int], object]) -> Dict[Tuple[int, int], object]:
    """cells: (row, col) -> number / text / '=formula'. Returns (row, col) -> value, with
    formulas computed (chains in any order). An error / circular reference -> '#ERR: …' text."""
    values: Dict[Tuple[int, int], object] = {k: v for k, v in cells.items() if not is_formula(v)}
    busy = set()

    def value(rc):
        if rc in values:
            v = values[rc]
            if isinstance(v, str) and v.startswith("#ERR"):
                raise FormulaError(v)
            return v
        f = cells.get(rc)
        if not is_formula(f):
            return None
        if rc in busy:
            raise FormulaError("순환 참조")
        busy.add(rc)
        try:
            v = evaluate(f, lambda r, c: value((r, c)))
        finally:
            busy.discard(rc)
        values[rc] = v
        return v

    for rc, f in cells.items():
        if is_formula(f) and rc not in values:
            try:
                value(rc)
            except FormulaError as exc:
                values[rc] = f"#ERR: {exc}"
            except RecursionError:
                values[rc] = "#ERR: 식이 너무 깁니다"
    return values
