"""Schema-to-renderer dispatch."""

from __future__ import annotations

from PySide6.QtWidgets import QWidget

from .placeholder import PlaceholderRenderer
from .scalar_curve import ScalarCurveRenderer


def make_renderer(schema: str, parent: QWidget | None = None) -> QWidget:
    """Return a renderer widget for the given schema.

    The widget exposes ``update_data(data, render_hint)``. Unknown schemas
    fall back to a placeholder that renders a "not implemented" message
    rather than crashing.
    """
    if schema == "scalar_curve":
        return ScalarCurveRenderer(parent)
    return PlaceholderRenderer(schema, parent)


__all__ = ["make_renderer", "ScalarCurveRenderer", "PlaceholderRenderer"]
