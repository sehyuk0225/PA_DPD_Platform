"""Touchstone v1 (.s1p .s2p .s3p .s4p) reader.

Option line:  # <Hz|kHz|MHz|GHz> S <MA|DB|RI> R <Z0>     (default: # GHz S MA R 50)
2-port data order is  f  S11 S21 S12 S22  (each as a pair), n>2 ports are row-major.
Noise parameters after the S-data (frequency restarts) are ignored.
"""
from __future__ import annotations

import os
import re

import numpy as np

from sparam.core.models import PARAMS, LoadError, SParamData
from sparam.utils.freq import UNIT


def n_ports_from_ext(path: str) -> int:
    m = re.search(r"\.s(\d+)p$", path.lower())
    return int(m.group(1)) if m else 2


def parse_touchstone(text: str, name: str, path: str = "", n_ports: int = 2) -> SParamData:
    unit, fmt, z0 = 1e9, "MA", 50.0
    numbers = []
    notes = []
    for raw in text.splitlines():
        line = raw.split("!", 1)[0].strip()
        if not line:
            continue
        if line.startswith("#"):
            tok = line[1:].upper().split()
            for i, t in enumerate(tok):
                if t in UNIT:
                    unit = UNIT[t]
                elif t in ("MA", "DB", "RI"):
                    fmt = t
                elif t == "R" and i + 1 < len(tok):
                    try:
                        z0 = float(tok[i + 1])
                    except ValueError:
                        pass
                elif t in ("Y", "Z", "H", "G"):
                    raise LoadError(f"{name}: {t}-parameter Touchstone은 지원하지 않습니다 (S-parameter만 지원).")
            continue
        if line.startswith("["):  # Touchstone 2.0 keyword
            if line.upper().startswith("[NUMBER OF PORTS]"):
                try:
                    n_ports = int(line.split("]")[1])
                except (ValueError, IndexError):
                    pass
            continue
        try:
            numbers.extend(float(x) for x in line.replace(",", " ").split())
        except ValueError:
            raise LoadError(f"{name}: Touchstone 데이터 줄을 해석할 수 없습니다: {raw.strip()[:60]}")

    width = 1 + 2 * n_ports * n_ports
    n = len(numbers) // width
    if n < 1:
        raise LoadError(f"{name}: Touchstone 데이터가 없습니다.")
    arr = np.array(numbers[: n * width]).reshape(n, width)
    # stop where frequency stops increasing (noise data block)
    f = arr[:, 0]
    stop = n
    for i in range(1, n):
        if f[i] <= f[i - 1]:
            stop = i
            notes.append("Noise parameter 구간은 무시했습니다.")
            break
    arr = arr[:stop]
    freq_hz = arr[:, 0] * unit

    def pair_index(i, j):  # 1-based ports -> column of the first number of the pair
        if n_ports == 2:
            k = {(1, 1): 0, (2, 1): 1, (1, 2): 2, (2, 2): 3}[(i, j)]
        else:
            k = (i - 1) * n_ports + (j - 1)
        return 1 + 2 * k

    db, phase = {}, {}
    for key in PARAMS:
        i, j = int(key[1]), int(key[2])
        if i > n_ports or j > n_ports:
            continue
        c = pair_index(i, j)
        a, b = arr[:, c], arr[:, c + 1]
        if fmt == "RI":
            z = a + 1j * b
            with np.errstate(divide="ignore"):
                db[key] = 20 * np.log10(np.abs(z))
            phase[key] = np.degrees(np.angle(z))
        elif fmt == "DB":
            db[key], phase[key] = a, b
        else:  # MA
            with np.errstate(divide="ignore"):
                db[key] = 20 * np.log10(np.abs(a))
            phase[key] = b
    return SParamData(name=name, path=path, freq_hz=freq_hz, db=db, phase=phase,
                      kind="touchstone", z0=z0, notes=notes)
