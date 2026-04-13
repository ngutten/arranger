"""Fallback renderer for schemas without a dedicated implementation."""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


class PlaceholderRenderer(QWidget):
    def __init__(self, schema: str, parent=None):
        super().__init__(parent)
        self._schema = schema
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        msg = QLabel(f"Renderer for schema '{schema}' not implemented yet.")
        msg.setWordWrap(True)
        msg.setStyleSheet("color: #888; font-style: italic;")
        layout.addWidget(msg)
        self.setMinimumHeight(60)

    def update_data(self, data, render_hint: dict | None = None) -> None:
        # No-op: the placeholder doesn't render data.
        pass
