"""Project paths — works from source and from the PyInstaller .exe.

bundle_root(): read-only files shipped with the program (example data, icon).
user_root():   folder the user sees — next to PA-Measurement-Analyzer.exe when
               frozen, the repository root when run from source. Private data
               (local_data/simulation) is looked up here.
"""
from __future__ import annotations

import os
import sys


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> str:
    if is_frozen():
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def user_root() -> str:
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return bundle_root()


project_root = bundle_root  # backwards compatible name


def example_measurement_path() -> str:
    return os.path.join(bundle_root(), "data", "example", "Measurement_Example.xlsx")


def example_simulation_root() -> str:
    return os.path.join(bundle_root(), "data", "example", "simulation")


def default_simulation_root() -> str:
    """local_data/simulation or simulation/ next to the program, else the bundled example."""
    for cand in (os.path.join(user_root(), "sim_data"),
                 os.path.join(user_root(), "local_data", "simulation"),
                 os.path.join(user_root(), "simulation")):
        if os.path.isdir(cand):
            return cand
    return example_simulation_root()
