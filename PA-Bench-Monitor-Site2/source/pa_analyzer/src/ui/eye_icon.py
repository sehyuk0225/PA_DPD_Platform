"""List icon = colour swatch + eye (open = shown normally, crossed = always hidden)."""
from __future__ import annotations

ICON_W, ICON_H = 24, 11


def swatch_eye_icon(color: str, muted: bool = False):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
    pm = QPixmap(ICON_W, ICON_H)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    try:
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(0, 1, 9, 9, QColor("#dddddd" if muted else color))
        eye = QColor("#b0b0b0" if muted else "#444444")
        pen = QPen(eye)
        pen.setWidthF(1.1)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(12, 3, 11, 6)                     # eye outline
        p.setBrush(eye)
        p.drawEllipse(16, 4, 4, 4)                      # pupil
        if muted:
            pen = QPen(QColor("#c62828"))
            pen.setWidthF(1.5)
            p.setPen(pen)
            p.drawLine(12, 10, 23, 1)                   # crossed out
    finally:
        p.end()
    return QIcon(pm)
