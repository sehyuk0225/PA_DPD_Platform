"""Graph registry. Adding a graph = adding one GraphSpec.

The UI builds its graph selector from GRAPHS, so new entries appear
automatically. `enabled=False` keeps future graphs defined but hidden.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from core.quantities import QUANTITIES


@dataclass(frozen=True)
class GraphSpec:
    key: str
    x: str                                  # quantity key on X
    y: str                                  # quantity key on Y
    default_xlim: Tuple[float, float] = (20.0, 50.0)
    default_ylim: Optional[Tuple[float, float]] = None   # None -> auto
    enabled: bool = True

    @property
    def title(self) -> str:
        return f"{QUANTITIES[self.y].label} vs. {QUANTITIES[self.x].label}"

    @property
    def short_name(self) -> str:
        return f"{QUANTITIES[self.y].label} vs {QUANTITIES[self.x].label}"

    @property
    def file_stem(self) -> str:
        return f"{QUANTITIES[self.y].label}_vs_{QUANTITIES[self.x].label}".replace(" ", "").replace(".", "").replace("-", "")

    @property
    def x_label(self) -> str:
        return QUANTITIES[self.x].axis_label

    @property
    def y_label(self) -> str:
        return QUANTITIES[self.y].axis_label


GRAPHS: Dict[str, GraphSpec] = {}


def register_graph(g: GraphSpec) -> GraphSpec:
    GRAPHS[g.key] = g
    return g


# ---- MVP ------------------------------------------------------------------
register_graph(GraphSpec("pae_vs_pout", "pout", "pae", (26.0, 54.0), (0.0, 70.0)))
register_graph(GraphSpec("gain_vs_pout", "pout", "gain", (26.0, 52.0), (20.0, 40.0)))

# ---- Future (defined, hidden until validated) -------------------------------
register_graph(GraphSpec("pout_vs_pin", "pin", "pout", (0.0, 40.0), (20.0, 50.0), enabled=False))
register_graph(GraphSpec("de_vs_pout", "pout", "de", (20.0, 50.0), (0.0, 80.0), enabled=False))
register_graph(GraphSpec("idd_vs_pout", "pout", "idd", (20.0, 50.0), None, enabled=False))
register_graph(GraphSpec("aclr_vs_pout", "pout", "aclr", (20.0, 50.0), (-70.0, -20.0), enabled=False))
register_graph(GraphSpec("evm_vs_pout", "pout", "evm", (20.0, 50.0), (0.0, 10.0), enabled=False))
register_graph(GraphSpec("ampm_vs_pout", "pout", "ampm", (20.0, 50.0), None, enabled=False))


def enabled_graphs():
    return [g for g in GRAPHS.values() if g.enabled]
