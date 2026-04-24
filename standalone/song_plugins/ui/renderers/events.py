"""EventsRenderer — paints labeled beat-spans (or ticks) in lanes.

Accepts a list of event dicts. Each event has:

- ``beat``: required, start beat (float).
- ``end_beat`` or ``duration``: optional; absent => zero-length tick.
- ``label``: optional text drawn inside the span.
- ``color``: optional "#rrggbb" override.
- ``lane``: optional string; events with the same lane value stack
  vertically below each other. Absent lane values share a default lane.
- ``payload``: anything; used for tooltip, no influence on layout.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget


_LANE_PALETTE = [
    "#6fa8e0", "#e09070", "#90d080", "#c080d0",
    "#e0c060", "#60d0c0", "#d0d060", "#e070a0",
]


class EventsRenderer(QWidget):
    """Paints a list of beat-indexed events as labeled spans."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._events: List[dict] = []
        self._hint: dict = {}
        self._beat_range: Optional[Tuple[float, float]] = None
        self.setMinimumHeight(40)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)

    # Widget heights below this threshold use a chrome-free layout suited
    # to the broadcast band (no axis labels, no lane gutter, thinner boxes).
    _COMPACT_THRESHOLD = 70

    def update_data(self, data, render_hint: Optional[dict] = None) -> None:
        if not isinstance(data, list):
            self._events = []
        else:
            self._events = [dict(e) for e in data if isinstance(e, dict)]
        self._hint = dict(render_hint or {})
        self.setToolTip("")
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

    # -- Helpers ---------------------------------------------------------

    def _event_span(self, e: dict) -> Tuple[float, float]:
        b = float(e.get("beat", 0.0))
        if "end_beat" in e:
            try:
                eb = float(e["end_beat"])
                if eb > b:
                    return (b, eb)
            except (TypeError, ValueError):
                pass
        if "duration" in e:
            try:
                d = float(e["duration"])
                if d > 0:
                    return (b, b + d)
            except (TypeError, ValueError):
                pass
        return (b, b)  # tick-style

    def _lane_for(self, e: dict) -> str:
        return str(e.get("lane", ""))

    def _lane_order(self) -> List[str]:
        seen: Dict[str, int] = {}
        order: List[str] = []
        for e in self._events:
            k = self._lane_for(e)
            if k not in seen:
                seen[k] = len(order)
                order.append(k)
        return order

    def _color_for(self, e: dict, lane_idx: int) -> QColor:
        c = e.get("color")
        if c:
            try:
                return QColor(str(c))
            except Exception:
                pass
        return QColor(_LANE_PALETTE[lane_idx % len(_LANE_PALETTE)])

    # -- Painting --------------------------------------------------------

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        compact = self.height() < self._COMPACT_THRESHOLD
        margin = 1 if compact else 4
        rect = self.rect().adjusted(margin, margin, -margin, -margin)
        p.fillRect(rect, QColor(28, 32, 50))

        if not self._events:
            p.setPen(QColor(136, 136, 136))
            p.drawText(rect, Qt.AlignCenter, "(no events)")
            p.end()
            return

        axis_left = 0 if compact else 24
        axis_bottom = 0 if compact else 14
        top_pad = 1 if compact else 4
        plot = QRectF(
            rect.left() + axis_left,
            rect.top() + top_pad,
            max(10.0, rect.width() - axis_left - margin),
            max(10.0, rect.height() - axis_bottom - top_pad),
        )

        # Beat range — derive from events if not pinned.
        if self._beat_range is not None:
            x_min, x_max = self._beat_range
        else:
            starts = [float(e.get("beat", 0.0)) for e in self._events]
            ends = [self._event_span(e)[1] for e in self._events]
            x_min = min(starts) if starts else 0.0
            x_max = max(ends) if ends else (x_min + 1.0)
            if x_max <= x_min:
                x_max = x_min + 1.0

        x_span = x_max - x_min

        def to_x(b: float) -> float:
            return (
                plot.left()
                + (0.0 if x_span == 0 else (b - x_min) / x_span) * plot.width()
            )

        # Lane layout.
        lanes = self._lane_order()
        lane_count = max(1, len(lanes))
        lane_min_h = 6.0 if compact else 14.0
        lane_h = max(lane_min_h, plot.height() / lane_count)
        lane_top: Dict[str, float] = {
            name: plot.top() + i * lane_h for i, name in enumerate(lanes)
        }

        font = QFont()
        font.setPointSize(7 if compact else 9)
        p.setFont(font)

        if not compact:
            # Baseline — skipped in compact mode to leave more room.
            pen = QPen(QColor(60, 70, 95))
            pen.setWidth(1)
            p.setPen(pen)
            p.drawLine(plot.left(), plot.bottom(), plot.right(), plot.bottom())
            p.drawLine(plot.left(), plot.top(), plot.left(), plot.bottom())

        # Events.
        pad_y = 1.0 if compact else 2.0
        for i, e in enumerate(self._events):
            b0, b1 = self._event_span(e)
            # Clip to visible range.
            if b1 < x_min or b0 > x_max:
                continue
            b0c = max(b0, x_min)
            b1c = min(b1, x_max)
            x0 = to_x(b0c)
            x1 = to_x(b1c)
            lane = self._lane_for(e)
            lane_idx = lanes.index(lane) if lane in lanes else 0
            if compact:
                # Center a slim box in the lane so vertical neighbours
                # don't touch and nothing reaches the clip edge.
                h = max(4.0, lane_h * 0.6)
                top = lane_top.get(lane, plot.top()) + (lane_h - h) * 0.5
            else:
                h = max(8.0, lane_h - 2 * pad_y)
                top = lane_top.get(lane, plot.top()) + pad_y
            color = self._color_for(e, lane_idx)

            if b1 > b0:  # span
                fill = QColor(color)
                fill.setAlpha(110)
                border = QColor(color)
                border.setAlpha(220)
                p.setBrush(QBrush(fill))
                p.setPen(QPen(border, 1.2))
                span_rect = QRectF(x0, top, max(1.5, x1 - x0), h)
                p.drawRoundedRect(span_rect, 3, 3)
                label = str(e.get("label", ""))
                if label and span_rect.width() > 16:
                    p.setPen(QColor(240, 245, 255))
                    clip_rect = span_rect.adjusted(4, 0, -4, 0)
                    p.drawText(clip_rect, Qt.AlignVCenter | Qt.AlignLeft, label)
            else:  # tick
                p.setBrush(Qt.NoBrush)
                tick_pen = QPen(color, 1.8)
                p.setPen(tick_pen)
                p.drawLine(int(x0), int(top), int(x0), int(top + h))
                label = str(e.get("label", ""))
                if label:
                    p.setPen(QColor(210, 220, 240))
                    p.drawText(
                        QRectF(x0 + 3, top, 80, h),
                        Qt.AlignVCenter | Qt.AlignLeft,
                        label,
                    )

        if not compact:
            # Lane name gutters on the left.
            p.setPen(QColor(130, 140, 165))
            for name, top in lane_top.items():
                if name:
                    p.drawText(
                        QRectF(rect.left(), top, axis_left - 2, lane_h),
                        Qt.AlignRight | Qt.AlignVCenter,
                        name[:6],
                    )

            # Beat axis labels.
            p.setPen(QColor(170, 180, 210))
            x_lab = self._hint.get("x_label", "beat")
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
        p.end()

    # -- Tooltips --------------------------------------------------------

    def mouseMoveEvent(self, ev):
        # Minimal hit-test: show the label + payload for the event under
        # the cursor, if any. Linear scan is fine — event counts are
        # typically small (<1000).
        if not self._events:
            return
        rect = self.rect().adjusted(4, 4, -4, -4)
        axis_left = 24
        axis_bottom = 14
        plot_left = rect.left() + axis_left
        plot_right = rect.right() - 4
        plot_top = rect.top() + 4
        plot_bottom = rect.bottom() - axis_bottom
        plot_w = max(10.0, plot_right - plot_left)
        plot_h = max(10.0, plot_bottom - plot_top)
        if self._beat_range is not None:
            x_min, x_max = self._beat_range
        else:
            starts = [float(e.get("beat", 0.0)) for e in self._events]
            ends = [self._event_span(e)[1] for e in self._events]
            x_min = min(starts) if starts else 0.0
            x_max = max(ends) if ends else (x_min + 1.0)
            if x_max <= x_min:
                x_max = x_min + 1.0
        x_span = x_max - x_min
        if x_span <= 0:
            return

        mx = ev.position().x()
        my = ev.position().y()
        beat_at = x_min + (mx - plot_left) / plot_w * x_span

        lanes = self._lane_order()
        lane_count = max(1, len(lanes))
        lane_h = plot_h / lane_count

        best = None
        for e in self._events:
            b0, b1 = self._event_span(e)
            if b1 == b0:
                if abs(beat_at - b0) > x_span * 0.01:
                    continue
            else:
                if not (b0 <= beat_at <= b1):
                    continue
            lane = self._lane_for(e)
            if lane not in lanes:
                continue
            lane_idx = lanes.index(lane)
            top = plot_top + lane_idx * lane_h
            if not (top <= my <= top + lane_h):
                continue
            best = e
            break

        if best is None:
            self.setToolTip("")
        else:
            parts = []
            if best.get("label"):
                parts.append(str(best["label"]))
            b0, b1 = self._event_span(best)
            if b1 > b0:
                parts.append(f"beats {b0:.2f}–{b1:.2f}")
            else:
                parts.append(f"beat {b0:.2f}")
            if best.get("payload") is not None:
                parts.append(f"{best['payload']}")
            self.setToolTip("\n".join(parts))
