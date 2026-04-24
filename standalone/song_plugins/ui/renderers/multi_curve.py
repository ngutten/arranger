"""MultiCurveRenderer — paints one beats-axis with several named series."""

from __future__ import annotations

from typing import Dict, List, Optional

from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget


# Distinct hues cycled through for series colors when render_hint.colors
# does not pin a color for a given series. Chosen for decent separability
# on the ~#1c2032 background used by the scalar_curve renderer.
_DEFAULT_COLORS = [
    "#f07890", "#7ad0ff", "#ffd070", "#90e090",
    "#c090ff", "#ff9f70", "#60d0c0", "#d090c0",
]


class MultiCurveRenderer(QWidget):
    """Paints ``{beats: [...], series: {name: [...], ...}}`` as overlaid lines.

    Each series draws as a polyline of its own color. A compact legend is
    rendered at the top-right. Empty input shows a "(no data)" message.

    ``render_hint`` keys honored:
      - ``colors``: ``{series_name: "#rrggbb"}`` — override per-series color.
      - ``y_label`` / ``x_label``: axis labels (default ``"beat"`` for x).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._beats: List[float] = []
        self._series: Dict[str, List[float]] = {}
        self._hint: dict = {}
        self._beat_range: Optional[tuple] = None
        self.setMinimumHeight(40)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    _COMPACT_THRESHOLD = 70

    def update_data(self, data, render_hint: Optional[dict] = None) -> None:
        try:
            beats = list(data.get("beats", []))
            series_map = data.get("series", {}) or {}
        except Exception:
            beats, series_map = [], {}
        self._beats = [float(b) for b in beats]
        self._series = {}
        for name, vs in series_map.items():
            try:
                self._series[str(name)] = [float(v) for v in vs]
            except Exception:
                continue
        self._hint = dict(render_hint or {})
        self.update()

    def set_beat_range(
        self,
        left_beat: Optional[float],
        right_beat: Optional[float],
    ) -> None:
        if (left_beat is None or right_beat is None
                or right_beat <= left_beat):
            self._beat_range = None
        else:
            self._beat_range = (float(left_beat), float(right_beat))
        self.update()

    # -- Painting --------------------------------------------------------

    def _color_for(self, name: str, i: int) -> QColor:
        overrides = self._hint.get("colors") or {}
        if name in overrides:
            return QColor(str(overrides[name]))
        return QColor(_DEFAULT_COLORS[i % len(_DEFAULT_COLORS)])

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        compact = self.height() < self._COMPACT_THRESHOLD
        margin = 1 if compact else 4
        rect = self.rect().adjusted(margin, margin, -margin, -margin)
        p.fillRect(rect, QColor(28, 32, 50))

        if not self._beats or not self._series:
            p.setPen(QColor(136, 136, 136))
            p.drawText(rect, Qt.AlignCenter, "(no data)")
            p.end()
            return

        axis_left = 0 if compact else 36
        axis_bottom = 0 if compact else 14
        top_pad = 1 if compact else 4
        plot = QRectF(
            rect.left() + axis_left,
            rect.top() + top_pad,
            max(10.0, rect.width() - axis_left - margin),
            max(10.0, rect.height() - axis_bottom - top_pad),
        )

        if self._beat_range is not None:
            x_min, x_max = self._beat_range
        else:
            x_min = self._beats[0]
            x_max = self._beats[-1] if self._beats[-1] > x_min else (x_min + 1.0)

        # Global y-range across all series.
        y_min = float("inf")
        y_max = float("-inf")
        n_beats = len(self._beats)
        for vs in self._series.values():
            n = min(n_beats, len(vs))
            for i in range(n):
                v = vs[i]
                if v < y_min:
                    y_min = v
                if v > y_max:
                    y_max = v
        if y_min == float("inf"):
            y_min, y_max = 0.0, 1.0
        if y_max <= y_min:
            y_max = y_min + 1.0

        x_span = x_max - x_min
        y_span = y_max - y_min

        def to_xy(b, v):
            fx = 0.0 if x_span == 0 else (b - x_min) / x_span
            fy = 0.0 if y_span == 0 else (v - y_min) / y_span
            return QPointF(
                plot.left() + fx * plot.width(),
                plot.bottom() - fy * plot.height(),
            )

        if not compact:
            # Axis lines.
            pen = QPen(QColor(60, 70, 95))
            pen.setWidth(1)
            p.setPen(pen)
            p.drawLine(plot.left(), plot.bottom(), plot.right(), plot.bottom())
            p.drawLine(plot.left(), plot.top(), plot.left(), plot.bottom())

        # Series polylines.
        series_items = list(self._series.items())
        for idx, (name, vs) in enumerate(series_items):
            if not vs:
                continue
            color = self._color_for(name, idx)
            line_pen = QPen(color)
            line_pen.setWidthF(1.6)
            p.setPen(line_pen)
            n = min(n_beats, len(vs))
            for i in range(n - 1):
                p.drawLine(
                    to_xy(self._beats[i], vs[i]),
                    to_xy(self._beats[i + 1], vs[i + 1]),
                )

        if not compact:
            # Axis labels.
            font = QFont()
            font.setPointSize(8)
            p.setFont(font)
            p.setPen(QColor(170, 180, 210))
            x_lab = self._hint.get("x_label", "beat")
            p.drawText(
                QRectF(rect.left(), plot.top(), axis_left - 2, 12),
                Qt.AlignRight | Qt.AlignTop,
                f"{y_max:.2f}",
            )
            p.drawText(
                QRectF(rect.left(), plot.bottom() - 12, axis_left - 2, 12),
                Qt.AlignRight | Qt.AlignBottom,
                f"{y_min:.2f}",
            )
            p.drawText(
                QRectF(plot.left(), rect.bottom() - 12, plot.width(), 12),
                Qt.AlignLeft | Qt.AlignBottom,
                f"{x_min:.1f} {x_lab}",
            )
            p.drawText(
                QRectF(plot.left(), rect.bottom() - 12, plot.width(), 12),
                Qt.AlignRight | Qt.AlignBottom,
                f"{x_max:.1f}",
            )

            # Legend — top-right, inside plot rect, one line per series.
            legend_x = plot.right() - 8
            legend_y = plot.top() + 2
            for idx, (name, _vs) in enumerate(series_items):
                color = self._color_for(name, idx)
                line_pen = QPen(color)
                line_pen.setWidthF(2.0)
                p.setPen(line_pen)
                p.drawLine(
                    QPointF(legend_x - 16, legend_y + 6),
                    QPointF(legend_x - 4, legend_y + 6),
                )
                p.setPen(QColor(210, 220, 240))
                p.drawText(
                    QRectF(legend_x - 120, legend_y, 100, 12),
                    Qt.AlignRight | Qt.AlignVCenter,
                    str(name),
                )
                legend_y += 12
                if legend_y > plot.top() + plot.height() - 14:
                    break

        p.end()
