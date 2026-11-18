"""Paths for the S-Parameter part: shared with the PA Analyzer (one program, one bundle)."""
from __future__ import annotations

import os

from utils.paths import bundle_root, is_frozen, user_root  # noqa: F401


def sparam_example_dir() -> str:
    return os.path.join(bundle_root(), "data", "example", "sparam")
