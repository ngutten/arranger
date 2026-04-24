"""ScalarCurveRenderer — paints a 1D curve over beats using QPainter."""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QPolygonF, QFont
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QSizePolicy, QWidget


class ScalarCurveRenderer(QWidget):
    """Paints ``{beats: [...], values: [...]}`` as a filled polyline.

    The two arrays should be the same length. If either is empty, an
    empty-state message is shown.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._beats: List[float] = []
        self._values: List[float] = []
        self._hint: dict = {}
        # When set (both not None), paintEvent uses this range for the
        # x-axis instead of deriving it from the data. See set_beat_range.
        self._beat_range: Optional[tuple] = None
        self.setMinimumHeight(40)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    # Widget heights below this threshold use a chrome-free layout suited
    # to the broadcast band (no axis labels, no y-gutter).
    _COMPACT_THRESHOLD = 70

    def update_data(self, data, render_hint: Optional[dict] = None) -> None:
        try:
            beats = list(data.get("beats", []))
            values = list(data.get("values", []))
        except Exception:
            beats, values = [], []
        n = min(len(beats), len(values))
        self._beats = [float(b) for b in beats[:n]]
        self._values = [float(v) for v in values[:n]]
        self._hint = dict(render_hint or {})
        self.update()

    def set_beat_range(
        self,
        left_beat: Optional[float],
        right_beat: Optional[float],
    ) -> None:
        """Fix the x-axis to ``[left_beat, right_beat]``.

        When both arguments are provided (and ``right_beat > left_beat``),
        the renderer uses this range for the x-axis instead of deriving
        it from the data's own min/max. Passing ``None`` for either value
        reverts to auto-scale.
        """
        if (left_beat is None or right_beat is None
                or right_beat <= left_beat):
            self._beat_range = None
        else:
            self._beat_range = (float(left_beat), float(right_beat))
        self.update()

    # -- Painting --------------------------------------------------------

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        compact = self.height() < self._COMPACT_THRESHOLD
        margin = 1 if compact else 4
        rect = self.rect().adjusted(margin, margin, -margin, -margin)
        # Background
        p.fillRect(rect, QColor(28, 32, 50))

        if not self._beats or not self._values:
            p.setPen(QColor(136, 136, 136))
            p.drawText(rect, Qt.AlignCenter, "(no data)")
            p.end()
            return

        # Leave room for axis labels — skipped in compact mode so the
        # curve gets the full widget width/height.
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
        y_min = min(self._values)
        y_max = max(self._values)
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
            # Gridlines (just a subtle baseline + mid/top)
            pen = QPen(QColor(60, 70, 95))
            pen.setWidth(1)
            p.setPen(pen)
            p.drawLine(plot.left(), plot.bottom(), plot.right(), plot.bottom())
            p.drawLine(plot.left(), plot.top(), plot.left(), plot.bottom())

        # Build polygon for the filled area.
        poly = QPolygonF()
        poly.append(QPointF(plot.left(), plot.bottom()))
        for b, v in zip(self._beats, self._values):
            poly.append(to_xy(b, v))
        poly.append(QPointF(
            to_xy(self._beats[-1], self._values[-1]).x(),
            plot.bottom(),
        ))
        fill_color = QColor(233, 69, 96)
        fill_color.setAlpha(70)
        p.setBrush(QBrush(fill_color))
        p.setPen(Qt.NoPen)
        p.drawPolygon(poly)

        # Polyline on top.
        line_pen = QPen(QColor(240, 120, 140))
        line_pen.setWidthF(1.6)
        p.setPen(line_pen)
        p.setBrush(Qt.NoBrush)
        for i in range(len(self._beats) - 1):
            p.drawLine(
                to_xy(self._beats[i], self._values[i]),
                to_xy(self._beats[i + 1], self._values[i + 1]),
            )

        if not compact:
            # Labels
            font = QFont()
            font.setPointSize(8)
            p.setFont(font)
            p.setPen(QColor(170, 180, 210))
            y_lab = self._hint.get("y_label", "")
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
            if y_lab:
                p.drawText(
                    QRectF(rect.left(), plot.top() + plot.height() / 2 - 8,
                           axis_left - 2, 16),
                    Qt.AlignRight | Qt.AlignVCenter,
                    y_lab,
                )

        p.end()
