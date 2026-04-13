"""Minimal renderer showing a summary of pending operations.

Used by transform/generate plugin blocks to preview ``PluginResult.operations``
before the user commits them via Apply.
"""

from __future__ import annotations

from collections import Counter
from typing import Iterable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


class OperationsPreview(QWidget):
    """Compact preview widget: op count + breakdown by op type."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(3)

        self._header = QLabel("")
        self._header.setAlignment(Qt.AlignLeft)
        self._header.setStyleSheet("color: #c0c8e0; font-weight: bold;")
        layout.addWidget(self._header)

        self._breakdown = QLabel("")
        self._breakdown.setAlignment(Qt.AlignLeft)
        self._breakdown.setWordWrap(True)
        self._breakdown.setStyleSheet("color: #8a94b0; font-size: 11px;")
        layout.addWidget(self._breakdown)

        layout.addStretch()
        self.setMinimumHeight(60)

    def update_data(self, data, render_hint: dict | None = None) -> None:
        """``data`` is an iterable of Operation dataclasses."""
        ops = tuple(data or ())
        n = len(ops)
        if n == 0:
            self._header.setText("No operations")
            self._breakdown.setText("")
            return
        self._header.setText(f"{n} operation{'s' if n != 1 else ''}")
        self._breakdown.setText(_format_breakdown(ops))


def _format_breakdown(ops: Iterable) -> str:
    counts = Counter(type(op).__name__ for op in ops)
    # Stable ordering: highest count first, then alphabetic.
    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return " \u00b7 ".join(f"{name} \u00d7 {cnt}" for name, cnt in items)
