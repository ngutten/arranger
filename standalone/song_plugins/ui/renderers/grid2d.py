"""Grid2DRenderer — paints a ``{rows, cols, cells}`` matrix as a heatmap.

``cells`` is a flat sequence of length ``rows * cols``, row-major (top
row first). Values are normalized to [0, 1] against the cells' own min
and max and mapped through a colormap.

Render hint keys:

- ``colormap``: ``"magma"`` (default), ``"viridis"``, ``"gray"``,
  ``"heat"``. Unknown names fall back to ``"magma"``.
- ``row_labels``: sequence of strings, one per row; rendered in a
  gutter on the left.
- ``x_label``: axis label for the bottom (default ``"beat"``).
- ``beat_range``: optional ``(x_min, x_max)`` pair — if present, drawn
  as x-axis tick labels. ``set_beat_range`` overrides this.
- ``vmin`` / ``vmax``: pin the color range instead of autoscaling.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QFont
from PySide6.QtWidgets import QSizePolicy, QWidget


# Tiny lookup-based colormaps. Each is a list of (r, g, b) anchor stops
# at evenly-spaced positions; intermediate values are linearly
# interpolated.
_CMAPS = {
    "magma": [
        (0, 0, 4), (28, 16, 68), (79, 18, 123), (129, 37, 129),
        (181, 54, 122), (229, 80, 100), (251, 135, 97), (254, 194, 135),
        (252, 253, 191),
    ],
    "viridis": [
        (68, 1, 84), (72, 40, 120), (62, 73, 137), (49, 104, 142),
        (38, 130, 142), (31, 158, 137), (53, 183, 121), (109, 205, 89),
        (253, 231, 37),
    ],
    "gray": [
        (0, 0, 0), (32, 32, 32), (64, 64, 64), (96, 96, 96),
        (128, 128, 128), (160, 160, 160), (192, 192, 192), (224, 224, 224),
        (255, 255, 255),
    ],
    "heat": [
        (0, 0, 0), (50, 0, 0), (120, 10, 0), (180, 40, 0),
        (220, 90, 0), (240, 160, 30), (250, 210, 80), (252, 240, 160),
        (255, 255, 220),
    ],
}


def _sample_cmap(name: str, t: float) -> Tuple[int, int, int]:
    stops = _CMAPS.get(name) or _CMAPS["magma"]
    if t <= 0:
        return stops[0]
    if t >= 1:
        return stops[-1]
    pos = t * (len(stops) - 1)
    i = int(pos)
    f = pos - i
    r0, g0, b0 = stops[i]
    r1, g1, b1 = stops[i + 1]
    return (
        int(r0 + (r1 - r0) * f),
        int(g0 + (g1 - g0) * f),
        int(b0 + (b1 - b0) * f),
    )


class Grid2DRenderer(QWidget):
    """Paints a 2D numeric grid as a heatmap image."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = 0
        self._cols = 0
        self._cells: List[float] = []
        self._hint: dict = {}
        self._beat_range: Optional[Tuple[float, float]] = None
        self._image: Optional[QImage] = None
        self.setMinimumHeight(40)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    _COMPACT_THRESHOLD = 70

    def update_data(self, data, render_hint: Optional[dict] = None) -> None:
        if not isinstance(data, dict):
            self._rows = self._cols = 0
            self._cells = []
            self._image = None
            self.update()
            return
        try:
            rows = int(data.get("rows", 0))
            cols = int(data.get("cols", 0))
            cells = list(data.get("cells", []))
        except (TypeError, ValueError):
            self._rows = self._cols = 0
            self._cells = []
            self._image = None
            self.update()
            return
        if rows * cols != len(cells):
            self._rows = self._cols = 0
            self._cells = []
            self._image = None
            self.update()
            return
        self._rows = rows
        self._cols = cols
        self._cells = [float(v) for v in cells]
        self._hint = dict(render_hint or {})
        self._rebuild_image()
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

    # -- Internals -------------------------------------------------------

    def _rebuild_image(self) -> None:
        if self._rows <= 0 or self._cols <= 0:
            self._image = None
            return
        vmin_pin = self._hint.get("vmin")
        vmax_pin = self._hint.get("vmax")
        if vmin_pin is not None and vmax_pin is not None:
            vmin = float(vmin_pin)
            vmax = float(vmax_pin)
        else:
            vmin = min(self._cells) if self._cells else 0.0
            vmax = max(self._cells) if self._cells else 1.0
            if vmax <= vmin:
                vmax = vmin + 1.0
        span = vmax - vmin
        cmap = str(self._hint.get("colormap", "magma"))

        img = QImage(self._cols, self._rows, QImage.Format_RGB32)
        for r in range(self._rows):
            for c in range(self._cols):
                v = self._cells[r * self._cols + c]
                t = (v - vmin) / span
                rr, gg, bb = _sample_cmap(cmap, t)
                img.setPixel(c, r, (0xff << 24) | (rr << 16) | (gg << 8) | bb)
        self._image = img

    # -- Painting --------------------------------------------------------

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform, False)
        compact = self.height() < self._COMPACT_THRESHOLD
        margin = 1 if compact else 4
        rect = self.rect().adjusted(margin, margin, -margin, -margin)
        p.fillRect(rect, QColor(18, 22, 36))

        if self._image is None:
            p.setPen(QColor(136, 136, 136))
            p.drawText(rect, Qt.AlignCenter, "(no data)")
            p.end()
            return

        # Row labels gutter — skipped entirely in compact mode so the
        # heatmap gets the full width for its rows.
        row_labels = list(self._hint.get("row_labels") or [])
        gutter_w = 0.0
        if row_labels and not compact:
            gutter_w = 28.0
        axis_bottom = 0.0 if compact else 14.0
        top_pad = 1 if compact else 4
        plot = QRectF(
            rect.left() + gutter_w,
            rect.top() + top_pad,
            max(10.0, rect.width() - gutter_w - margin),
            max(10.0, rect.height() - axis_bottom - top_pad),
        )

        # Pull the data's own beat extent from the hint; without it we
        # can only scale the whole image across the plot rect, which
        # misaligns columns from arranger beats when the band is
        # showing a zoomed-in viewport.
        full_range: Optional[Tuple[float, float]] = None
        if "beat_range" in self._hint:
            try:
                fmin, fmax = self._hint["beat_range"]
                fmin = float(fmin); fmax = float(fmax)
                if fmax > fmin:
                    full_range = (fmin, fmax)
            except Exception:
                full_range = None

        if self._beat_range is not None and full_range is not None:
            vis_min, vis_max = self._beat_range
            full_min, full_max = full_range
            vis_span = vis_max - vis_min
            full_span = full_max - full_min
            # Intersection of the visible viewport and the data extent.
            b_left = max(vis_min, full_min)
            b_right = min(vis_max, full_max)
            if vis_span > 0 and full_span > 0 and b_right > b_left:
                img_w = float(self._image.width())
                img_h = float(self._image.height())
                sx0 = (b_left - full_min) / full_span * img_w
                sx1 = (b_right - full_min) / full_span * img_w
                tx0 = plot.left() + (b_left - vis_min) / vis_span * plot.width()
                tx1 = plot.left() + (b_right - vis_min) / vis_span * plot.width()
                src = QRectF(sx0, 0.0, sx1 - sx0, img_h)
                tgt = QRectF(tx0, plot.top(), tx1 - tx0, plot.height())
                p.drawImage(tgt, self._image, src)
        else:
            p.drawImage(plot, self._image)

        if not compact:
            # Row label gutter text.
            if row_labels:
                font = QFont()
                font.setPointSize(8)
                p.setFont(font)
                p.setPen(QColor(170, 180, 210))
                rh = plot.height() / max(1, self._rows)
                for r in range(min(self._rows, len(row_labels))):
                    p.drawText(
                        QRectF(rect.left(), plot.top() + r * rh,
                               gutter_w - 2, rh),
                        Qt.AlignRight | Qt.AlignVCenter,
                        str(row_labels[r]),
                    )

            # Beat-axis labels (if range is known).
            x_min = None
            x_max = None
            if self._beat_range is not None:
                x_min, x_max = self._beat_range
            elif "beat_range" in self._hint:
                try:
                    x_min, x_max = self._hint["beat_range"]
                    x_min = float(x_min); x_max = float(x_max)
                except Exception:
                    x_min = x_max = None
            if x_min is not None and x_max is not None and x_max > x_min:
                font = QFont()
                font.setPointSize(8)
                p.setFont(font)
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

            # Frame.
            p.setPen(QPen(QColor(60, 70, 95), 1))
            p.setBrush(Qt.NoBrush)
            p.drawRect(plot)
        p.end()
