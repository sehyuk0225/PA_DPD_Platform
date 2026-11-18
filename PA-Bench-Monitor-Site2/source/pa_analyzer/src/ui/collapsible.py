"""Collapsible section for the side panel: ▼ Title / ▶ Title."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QSizePolicy, QToolButton, QVBoxLayout, QWidget


class Section(QWidget):
    def __init__(self, title: str, body: QWidget, expanded: bool = True, grow: bool = False,
                 on_toggle=None, parent=None):
        super().__init__(parent)
        self.title = title
        self.body = body
        self.grow = grow                       # take the spare height when expanded (Datasets)
        self._on_toggle = on_toggle or (lambda s: None)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(1)
        self.button = QToolButton()
        self.button.setText(title)
        self.button.setCheckable(True)
        self.button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.button.setStyleSheet("QToolButton { border: none; font-weight: bold; text-align: left; padding: 3px; }"
                                  "QToolButton:hover { background: #e8eef7; }")
        self.button.toggled.connect(self._toggled)
        lay.addWidget(self.button)
        lay.addWidget(body, 1)
        self.expanded = None
        self.button.setChecked(expanded)
        self._apply(expanded)

    def _toggled(self, on):
        self._apply(bool(on))
        self._on_toggle(self)

    def _apply(self, on: bool):
        self.expanded = on
        self.button.setArrowType(Qt.DownArrow if on else Qt.RightArrow)
        self.body.setVisible(on)
        vpol = QSizePolicy.Expanding if (on and self.grow) else QSizePolicy.Maximum
        self.setSizePolicy(QSizePolicy.Preferred, vpol)

    def set_expanded(self, on: bool):
        self.button.setChecked(bool(on))
