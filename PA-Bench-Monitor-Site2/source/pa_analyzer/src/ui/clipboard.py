"""Put a PNG on the system clipboard (PowerPoint / Excel / Word / Teams / Outlook).

Offers three representations at once:
  * the standard bitmap (CF_DIB / CF_DIBV5 on Windows) via setImageData
  * the Windows-registered "PNG" format (Office prefers it: lossless, no gray fringe)
  * "image/png" for other platforms / apps
"""
from __future__ import annotations

from PySide6.QtCore import QByteArray, QMimeData
from PySide6.QtGui import QGuiApplication, QImage


def copy_png_to_clipboard(png: bytes) -> bool:
    img = QImage.fromData(QByteArray(png), "PNG")
    if img.isNull():
        return False
    mime = QMimeData()
    mime.setImageData(img)
    data = QByteArray(png)
    mime.setData("image/png", data)
    mime.setData('application/x-qt-windows-mime;value="PNG"', data)
    QGuiApplication.clipboard().setMimeData(mime)
    return True
