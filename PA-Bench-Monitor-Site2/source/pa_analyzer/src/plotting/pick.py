"""Find the plotted curve under the mouse (pixel distance to the drawn polyline)."""
from __future__ import annotations

from typing import Optional

import numpy as np


def _dist_to_polyline(px: np.ndarray, x: float, y: float) -> float:
    if len(px) == 0:
        return float("inf")
    if len(px) == 1:
        return float(np.hypot(px[0, 0] - x, px[0, 1] - y))
    a, b = px[:-1], px[1:]
    ab = b - a
    ap = np.array([x, y]) - a
    den = (ab ** 2).sum(axis=1)
    t = np.clip(np.where(den > 0, (ap * ab).sum(axis=1) / np.where(den > 0, den, 1), 0), 0, 1)
    proj = a + ab * t[:, None]
    return float(np.min(np.hypot(proj[:, 0] - x, proj[:, 1] - y)))


def curve_at(ax, x: float, y: float, tol_px: float = 7.0) -> Optional[str]:
    """gid of the nearest data curve within tol_px of the display point (x, y), else None."""
    best, best_d = None, tol_px
    for line in ax.get_lines():
        gid = line.get_gid()
        if not gid or not line.get_visible():
            continue
        xd, yd = (np.asarray(v, dtype=float) for v in line.get_data())
        ok = np.isfinite(xd) & np.isfinite(yd)
        if not ok.any():
            continue
        px = ax.transData.transform(np.column_stack([xd[ok], yd[ok]]))
        d = _dist_to_polyline(px, x, y)
        if line.get_linestyle() in ("None", "none", "", " "):     # markers only: vertices count
            d = float(np.min(np.hypot(px[:, 0] - x, px[:, 1] - y)))
        if d <= best_d:
            best, best_d = gid, d
    return best
