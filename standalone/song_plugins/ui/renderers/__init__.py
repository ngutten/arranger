"""Schema-to-renderer dispatch."""

from __future__ import annotations

from PySide6.QtWidgets import QWidget

from .events import EventsRenderer
from .grid2d import Grid2DRenderer
from .multi_curve import MultiCurveRenderer
from .placeholder import PlaceholderRenderer
from .scalar_curve import ScalarCurveRenderer
from .stats import StatsRenderer


def make_renderer(schema: str, parent: QWidget | None = None) -> QWidget:
    """Return a renderer widget for the given schema.

    The widget exposes ``update_data(data, render_hint)``. Unknown schemas
    fall back to a placeholder that renders a "not implemented" message
    rather than crashing.
    """
    if schema == "scalar_curve":
        return ScalarCurveRenderer(parent)
    if schema == "multi_curve":
        return MultiCurveRenderer(parent)
    if schema == "events":
        return EventsRenderer(parent)
    if schema == "grid2d":
        return Grid2DRenderer(parent)
    if schema == "stats":
        return StatsRenderer(parent)
    return PlaceholderRenderer(schema, parent)


__all__ = [
    "make_renderer",
    "ScalarCurveRenderer", "MultiCurveRenderer", "EventsRenderer",
    "Grid2DRenderer", "StatsRenderer", "PlaceholderRenderer",
]
