"""StatsRenderer — dispatches on ``data.render`` to show text / table / bars.

The schema only requires ``data`` be a dict, but by convention plugins
pass ``{"render": "<kind>", "data": ...}``. Supported kinds:

- ``"text"``: ``data`` is a string; shown in a scrolling label.
- ``"table"``: ``data`` is ``{"columns": [str,...], "rows": [[..],..]}``.
- ``"bars"``: ``data`` is ``[{"label": str, "value": float,
  "color"?: "#rrggbb"},...]``.
- ``"ranked_list"``: same shape as ``"bars"`` but values drawn as a
  right-aligned number and the bar is a subtle background fill.

If ``render`` is absent, the renderer falls back to a simple
key/value table built from the dict itself.
"""

from __future__ import annotations

from typing import Any, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget,
)
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QPainter, QPen, QBrush, QFont


class _BarsWidget(QWidget):
    """Horizontal bar chart. Each row is (label, value, optional color).

    When ``ranked`` is True, bars become subtle background fills and the
    value is written right-aligned in the row.
    """

    def __init__(self, ranked: bool = False, parent=None):
        super().__init__(parent)
        self._rows: List[dict] = []
        self._ranked = bool(ranked)
        self.setMinimumHeight(80)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_rows(self, rows: List[dict]) -> None:
        self._rows = list(rows or [])
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        rect = self.rect().adjusted(6, 4, -6, -4)
        p.fillRect(rect, QColor(28, 32, 50))

        if not self._rows:
            p.setPen(QColor(136, 136, 136))
            p.drawText(rect, Qt.AlignCenter, "(no data)")
            p.end()
            return

        vmax = 0.0
        for r in self._rows:
            try:
                v = float(r.get("value", 0.0))
                if v > vmax:
                    vmax = v
            except (TypeError, ValueError):
                continue
        if vmax <= 0.0:
            vmax = 1.0

        n = len(self._rows)
        row_h = max(14.0, rect.height() / n)
        label_w = min(120.0, rect.width() * 0.35)
        bar_left = rect.left() + label_w + 6
        bar_right = rect.right() - 4
        bar_w = max(10.0, bar_right - bar_left)

        font = QFont()
        font.setPointSize(9)
        p.setFont(font)

        for i, r in enumerate(self._rows):
            y = rect.top() + i * row_h
            label = str(r.get("label", ""))
            try:
                v = float(r.get("value", 0.0))
            except (TypeError, ValueError):
                v = 0.0
            color_hex = r.get("color") or "#6fa8e0"
            color = QColor(str(color_hex))
            bar_px = bar_w * max(0.0, min(1.0, v / vmax))

            p.setPen(QColor(210, 220, 240))
            p.drawText(
                QRectF(rect.left(), y, label_w, row_h),
                Qt.AlignVCenter | Qt.AlignLeft,
                label,
            )

            if self._ranked:
                fill = QColor(color)
                fill.setAlpha(60)
                p.setBrush(QBrush(fill))
                p.setPen(Qt.NoPen)
                p.drawRoundedRect(
                    QRectF(bar_left, y + 2, bar_px, row_h - 4), 3, 3,
                )
                p.setPen(QColor(230, 235, 245))
                p.drawText(
                    QRectF(bar_left, y, bar_w, row_h),
                    Qt.AlignVCenter | Qt.AlignRight,
                    f"{v:g}",
                )
            else:
                p.setBrush(QBrush(color))
                p.setPen(Qt.NoPen)
                p.drawRoundedRect(
                    QRectF(bar_left, y + 3, bar_px, row_h - 6), 2, 2,
                )
                if bar_px > 26:
                    p.setPen(QColor(20, 24, 40))
                    p.drawText(
                        QRectF(bar_left + 4, y, bar_px - 8, row_h),
                        Qt.AlignVCenter | Qt.AlignLeft,
                        f"{v:g}",
                    )
                else:
                    p.setPen(QColor(200, 210, 230))
                    p.drawText(
                        QRectF(bar_left + bar_px + 4, y, 80, row_h),
                        Qt.AlignVCenter | Qt.AlignLeft,
                        f"{v:g}",
                    )

        p.end()


class _TableWidget(QWidget):
    """Header row + data rows. No sort, no selection. Just paints."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._columns: List[str] = []
        self._rows: List[List[Any]] = []
        self.setMinimumHeight(80)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_data(self, columns: List[str], rows: List[List[Any]]) -> None:
        self._columns = [str(c) for c in (columns or [])]
        self._rows = [[c for c in r] for r in (rows or [])]
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        rect = self.rect().adjusted(6, 4, -6, -4)
        p.fillRect(rect, QColor(28, 32, 50))

        if not self._columns:
            p.setPen(QColor(136, 136, 136))
            p.drawText(rect, Qt.AlignCenter, "(no table)")
            p.end()
            return

        ncols = len(self._columns)
        nrows = len(self._rows)
        row_h = 18.0
        header_h = 20.0
        col_w = rect.width() / ncols

        font = QFont()
        font.setPointSize(9)
        p.setFont(font)

        # Header.
        p.fillRect(
            QRectF(rect.left(), rect.top(), rect.width(), header_h),
            QColor(40, 46, 70),
        )
        p.setPen(QColor(220, 225, 240))
        hf = QFont(font)
        hf.setBold(True)
        p.setFont(hf)
        for ci, name in enumerate(self._columns):
            p.drawText(
                QRectF(rect.left() + ci * col_w + 4, rect.top(),
                       col_w - 6, header_h),
                Qt.AlignVCenter | Qt.AlignLeft,
                name,
            )

        p.setFont(font)
        for ri in range(nrows):
            y = rect.top() + header_h + ri * row_h
            if y + row_h > rect.bottom():
                break
            if ri % 2 == 0:
                p.fillRect(
                    QRectF(rect.left(), y, rect.width(), row_h),
                    QColor(32, 36, 56),
                )
            row = self._rows[ri]
            p.setPen(QColor(200, 210, 230))
            for ci in range(min(ncols, len(row))):
                p.drawText(
                    QRectF(rect.left() + ci * col_w + 4, y, col_w - 6, row_h),
                    Qt.AlignVCenter | Qt.AlignLeft,
                    str(row[ci]),
                )

        # Separator grid (subtle).
        p.setPen(QPen(QColor(50, 58, 85), 1))
        for ci in range(1, ncols):
            x = rect.left() + ci * col_w
            p.drawLine(int(x), int(rect.top()),
                       int(x), int(rect.bottom()))
        p.end()


class StatsRenderer(QWidget):
    """Dispatch-container for text / table / bars / ranked_list."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self._inner: Optional[QWidget] = None
        self._kind: Optional[str] = None
        self.setMinimumHeight(80)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def _reset_to(self, kind: str) -> QWidget:
        if self._kind == kind and self._inner is not None:
            return self._inner
        if self._inner is not None:
            self._inner.setParent(None)
            self._inner.deleteLater()
            self._inner = None
        if kind == "text":
            w = QLabel("")
            w.setWordWrap(True)
            w.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            w.setStyleSheet(
                "background-color: #1c2032; color: #d0dce8; padding: 6px;"
            )
        elif kind == "table":
            w = _TableWidget()
        elif kind == "bars":
            w = _BarsWidget(ranked=False)
        elif kind == "ranked_list":
            w = _BarsWidget(ranked=True)
        else:
            # Generic dict key/value fallback.
            w = _TableWidget()
            kind = "table"
        self._layout.addWidget(w)
        self._inner = w
        self._kind = kind
        return w

    def update_data(self, data, render_hint: Optional[dict] = None) -> None:
        if not isinstance(data, dict):
            # Schema validator should have caught this, but be defensive.
            w = self._reset_to("text")
            w.setText("(invalid stats payload)")
            return

        render = data.get("render")
        inner = data.get("data")

        if render == "text":
            w = self._reset_to("text")
            w.setText(str(inner if inner is not None else ""))
            return

        if render == "table":
            w = self._reset_to("table")
            cols = []
            rows = []
            if isinstance(inner, dict):
                cols = list(inner.get("columns", []))
                rows = list(inner.get("rows", []))
            w.set_data(cols, rows)
            return

        if render == "bars":
            w = self._reset_to("bars")
            rows = list(inner) if isinstance(inner, list) else []
            w.set_rows(rows)
            return

        if render == "ranked_list":
            w = self._reset_to("ranked_list")
            rows = list(inner) if isinstance(inner, list) else []
            w.set_rows(rows)
            return

        # No ``render`` key — present the dict as a 2-column table.
        w = self._reset_to("table")
        rows = [[str(k), str(v)] for k, v in data.items()]
        w.set_data(["key", "value"], rows)
