"""Compact stereo master level meter for the top bar.

Shows L/R peak as two horizontal bars on a dB scale, a clip indicator that
latches red when a channel hits 0 dBFS, and a thin gain-reduction tick that
lights when the master limiter is working.
"""

import math

from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QPainter, QColor, QLinearGradient


# Visible meter range: -48 dBFS (left/empty) .. 0 dBFS (right/full).
_MIN_DB = -48.0


def _lin_to_norm(lin: float) -> float:
    """Linear amplitude → 0..1 position on the dB scale."""
    if lin <= 1e-6:
        return 0.0
    db = 20.0 * math.log10(lin)
    if db <= _MIN_DB:
        return 0.0
    if db >= 0.0:
        return 1.0
    return (db - _MIN_DB) / (0.0 - _MIN_DB)


class MasterMeter(QWidget):
    def __init__(self, parent=None, compact=False):
        super().__init__(parent)
        self._pk_l = 0.0
        self._pk_r = 0.0
        self._gr = 1.0
        self._clip = False
        self._compact = compact   # compact: no clip block / GR tick, smaller
        if compact:
            self.setMinimumSize(40, 16)
            self.setToolTip('Track level (L/R peak, dBFS). Red = clipping.')
        else:
            self.setMinimumSize(96, 24)
            self.setToolTip('Master output level (L/R peak, dBFS). '
                            'Red = clipping; bottom tick = limiter gain reduction.')

    def sizeHint(self) -> QSize:
        return QSize(48, 18) if self._compact else QSize(110, 24)

    def set_levels(self, peak_l: float, peak_r: float, gr: float = 1.0):
        self._pk_l = peak_l
        self._pk_r = peak_r
        self._gr = gr
        # Latch clip until reset (click to clear).
        if peak_l >= 0.999 or peak_r >= 0.999:
            self._clip = True
        self.update()

    def mousePressEvent(self, ev):
        self._clip = False          # click clears the clip latch
        self.update()

    def _draw_bar(self, p: QPainter, x, y, w, h, norm):
        # Track
        p.fillRect(x, y, w, h, QColor('#15151f'))
        if norm <= 0.0:
            return
        fill_w = max(1, int(w * norm))
        grad = QLinearGradient(x, 0, x + w, 0)
        grad.setColorAt(0.0, QColor('#3fb950'))    # green
        grad.setColorAt(0.72, QColor('#d6c244'))   # yellow (~-13 dB)
        grad.setColorAt(0.90, QColor('#e09b3a'))   # amber (~-5 dB)
        grad.setColorAt(1.0, QColor('#e94560'))    # red (0 dB)
        p.fillRect(x, y, fill_w, h, grad)

    def paintEvent(self, ev):
        p = QPainter(self)
        w = self.width()
        h = self.height()

        if self._compact:
            # Two stacked L/R bars, no clip block / GR tick.
            gap = 1
            bar_h = (h - gap) // 2
            self._draw_bar(p, 0, 0, w, bar_h, _lin_to_norm(self._pk_l))
            self._draw_bar(p, 0, bar_h + gap, w, h - bar_h - gap,
                           _lin_to_norm(self._pk_r))
            if self._clip:
                p.fillRect(w - 3, 0, 3, h, QColor('#e94560'))
            p.end()
            return

        clip_w = 8
        gr_h = 3
        gap = 2
        bar_x = 0
        bar_w = w - clip_w - gap
        bar_area_h = h - gr_h - 1
        bar_h = (bar_area_h - gap) // 2

        self._draw_bar(p, bar_x, 0, bar_w, bar_h, _lin_to_norm(self._pk_l))
        self._draw_bar(p, bar_x, bar_h + gap, bar_w, bar_h, _lin_to_norm(self._pk_r))

        # Gain-reduction tick along the bottom (grows leftward as gr drops).
        gr_norm = max(0.0, min(1.0, 1.0 - self._gr))
        p.fillRect(bar_x, h - gr_h, bar_w, gr_h, QColor('#15151f'))
        if gr_norm > 0.001:
            p.fillRect(bar_x, h - gr_h, max(1, int(bar_w * gr_norm)), gr_h,
                       QColor('#5aa0ff'))

        # Clip indicator block on the right.
        cx = w - clip_w
        p.fillRect(cx, 0, clip_w, h, QColor('#e94560') if self._clip
                   else QColor('#2a2a38'))
        p.end()
