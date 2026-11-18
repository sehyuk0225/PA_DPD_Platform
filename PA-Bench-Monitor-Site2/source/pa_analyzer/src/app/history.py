"""Undo / Redo (Ctrl+Z / Ctrl+Y) for the PA tab.

After every change the window records a snapshot of what is on the plot (datasets shown, names,
colours / styles, order, deleted data, markers, axis ranges, back-off, plot count …).
Undo steps back to the previous snapshot. Merely selecting / highlighting is not a step.
"""
from __future__ import annotations

from typing import List, Optional


class History:
    def __init__(self, limit: int = 80):
        self.limit = limit
        self.undo_stack: List[dict] = []
        self.redo_stack: List[dict] = []
        self.current: Optional[dict] = None

    def note(self, snap: dict) -> bool:
        """Record `snap` if it differs from the current state. True = a new step was added."""
        if self.current is None:
            self.current = snap
            return False
        if snap["key"] == self.current["key"]:
            self.current = snap                     # same state: keep the newest copy (e.g. selection)
            return False
        self.undo_stack.append(self.current)
        del self.undo_stack[:-self.limit]
        self.current = snap
        self.redo_stack.clear()
        return True

    def undo(self) -> Optional[dict]:
        if not self.undo_stack:
            return None
        self.redo_stack.append(self.current)
        self.current = self.undo_stack.pop()
        return self.current

    def redo(self) -> Optional[dict]:
        if not self.redo_stack:
            return None
        self.undo_stack.append(self.current)
        self.current = self.redo_stack.pop()
        return self.current

    def reset(self, snap: Optional[dict] = None) -> None:
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.current = snap
