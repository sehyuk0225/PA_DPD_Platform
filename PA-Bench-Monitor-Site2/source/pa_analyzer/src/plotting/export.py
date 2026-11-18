"""PNG export (file + bytes for clipboard).

The exported image is rendered from the *same* Figure that is on screen
(same size, axis ranges, legend and condition header). Only the transient
hover tooltip is hidden. `scale` raises the resolution without changing
the layout (2.0 -> crisp when pasted into PowerPoint).
"""
from __future__ import annotations

import io
import os
import re
from contextlib import contextmanager

from matplotlib.figure import Figure

from utils.freq import freq_tag

DEFAULT_SCALE = 2.0


@contextmanager
def _hover_hidden(fig: Figure):
    hidden = []
    for ax in fig.axes:
        for child in ax.get_children():
            if child.get_gid() == "hover" and child.get_visible():
                child.set_visible(False)
                hidden.append(child)
    try:
        yield
    finally:
        for c in hidden:
            c.set_visible(True)


def figure_png_bytes(fig: Figure, scale: float = DEFAULT_SCALE) -> bytes:
    buf = io.BytesIO()
    with _hover_hidden(fig):
        # On HiDPI screens the Qt canvas multiplies fig.dpi by the device pixel
        # ratio; use the original dpi so the PNG size does not depend on the monitor.
        base_dpi = getattr(fig, "_original_dpi", fig.dpi)
        fig.savefig(buf, format="png", dpi=base_dpi * scale, facecolor="white")
    return buf.getvalue()


def save_png(fig: Figure, path: str, scale: float = DEFAULT_SCALE) -> str:
    if not path.lower().endswith(".png"):
        path += ".png"
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    with open(path, "wb") as f:
        f.write(figure_png_bytes(fig, scale))
    return path


def default_png_name(graph_file_stem: str, freq_hz=None, sheet_name: str = "", suffix: str = "") -> str:
    """e.g. 'PAE_vs_Pout_3p6GHz.png'."""
    tag = freq_tag(freq_hz) if freq_hz else (_safe(sheet_name) or "FREQunknown")
    extra = f"_{_safe(suffix)}" if suffix else ""
    return f"{graph_file_stem}_{tag}{extra}.png"


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text or "").strip("_")
