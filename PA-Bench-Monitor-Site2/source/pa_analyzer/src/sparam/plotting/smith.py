"""Smith chart grid (impedance chart, normalised to Z0) drawn on a normal Axes."""
from __future__ import annotations

import numpy as np

R_VALUES = (0.2, 0.5, 1.0, 2.0, 5.0)
X_VALUES = (0.2, 0.5, 1.0, 2.0, 5.0)
GRID = dict(color="#b8b8b8", linewidth=0.7, zorder=0, label="_grid")
GRID_MAIN = dict(color="#7a7a7a", linewidth=1.0, zorder=0, label="_grid")


def draw_smith_grid(ax) -> None:
    t = np.linspace(0, 2 * np.pi, 721)
    ax.plot(np.cos(t), np.sin(t), **GRID_MAIN)                 # |Gamma| = 1
    ax.plot([-1, 1], [0, 0], **GRID)                            # real axis
    for r in R_VALUES:                                          # constant resistance
        c, rad = r / (1 + r), 1 / (1 + r)
        ax.plot(c + rad * np.cos(t), rad * np.sin(t), **(GRID_MAIN if r == 1.0 else GRID))
        ax.text(c - rad, 0.012, f"{r:g}", fontsize=7.5, color="#666", ha="right", va="bottom", zorder=1)
    for x in X_VALUES:                                          # constant reactance arcs
        for sx in (x, -x):
            g = _x_arc(sx)
            ax.plot(g.real, g.imag, **(GRID_MAIN if x == 1.0 else GRID))
            end = g[np.argmax(np.abs(g))] if len(g) else None
            if end is not None:
                ax.text(end.real * 1.06, end.imag * 1.06, f"{sx:+g}j", fontsize=7.5, color="#666",
                        ha="center", va="center", zorder=1)
    ax.text(-1.0, -0.06, "0", fontsize=7.5, color="#666", ha="center")
    ax.plot([1], [0], marker="o", markersize=2.5, color="#7a7a7a", label="_grid")
    ax.set_aspect("equal", adjustable="box")
    ax.set_xticks([]); ax.set_yticks([])
    for sp in ax.spines.values():
        sp.set_visible(False)


def _x_arc(x: float) -> np.ndarray:
    """Points of normalised reactance x for r = 0..inf, mapped to Gamma."""
    r = np.concatenate([np.linspace(0, 1, 120), np.logspace(0, 3, 120)])
    z = r + 1j * x
    return (z - 1) / (z + 1)


def gamma_to_z(g: complex, z0: float = 50.0) -> complex:
    return z0 * (1 + g) / (1 - g) if abs(1 - g) > 1e-12 else complex(np.inf)
