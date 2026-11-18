"""A splitter bar that is easy to grab: wide, shows a light line, turns blue under the mouse."""
from __future__ import annotations

HANDLE_PX = 12
CSS = ("QSplitter::handle:horizontal { background: qlineargradient(x1:0, y1:0, x2:1, y2:0,"
       " stop:0 transparent, stop:0.42 transparent, stop:0.43 #c9d3e0, stop:0.57 #c9d3e0,"
       " stop:0.58 transparent, stop:1 transparent); }"
       "QSplitter::handle:horizontal:hover { background: #cfe3ff; }"
       "QSplitter::handle:horizontal:pressed { background: #9cc3f5; }")


def wide_handle(splitter) -> None:
    """Wider bar between the Datasets / settings panel and the plots (drag anywhere near it)."""
    try:
        splitter.setHandleWidth(HANDLE_PX)
        splitter.setStyleSheet(CSS)
        from PySide6.QtCore import Qt
        for i in range(1, splitter.count()):
            h = splitter.handle(i)
            h.setCursor(Qt.SplitHCursor)
            h.setToolTip("끌어서 왼쪽 메뉴 / Plot 폭 조절")
    except Exception:
        pass
