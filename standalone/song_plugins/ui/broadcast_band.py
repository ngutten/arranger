"""BroadcastBand — shared visualization strip near the arranger.

One plugin block at a time may "broadcast" its annotation into this
strip by checking its Broadcast toggle. The band swaps in a core
renderer for the current annotation schema. Hidden when no broadcaster
is active.

The band's horizontal view tracks the arranger's scroll position and
viewport width so that a vertical line drawn at beat ``b`` on the
arranger canvas aligns with the same beat in the band. See
:meth:`set_arrangement_view`.
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QEvent, QObject, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QPushButton, QSizePolicy, QWidget,
)

from ..api import Annotation
from .renderers import make_renderer
from .renderers.scalar_curve import ScalarCurveRenderer

logger = logging.getLogger(__name__)


# Matches arrangement track-labels column width; imported lazily from
# ArrangementView where possible but kept as a fallback constant in
# case the arrangement module is unavailable (headless tests).
_LABEL_COL_WIDTH = 150


class BroadcastBand(QFrame):
    """Strip-shaped widget hosting a single plugin's annotation at a time.

    Hidden by default; revealed when :meth:`set_broadcaster` is called and
    collapsed again on :meth:`clear`.

    Signals
    -------
    cleared
        Emitted when the user clicks the band's close button. The
        :class:`PluginHost` listens for this so it can un-check the
        broadcasting block's Broadcast toggle.
    """

    cleared = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("BroadcastBand")
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet(
            "QFrame#BroadcastBand { background-color: #1a1d33; }"
        )
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        # Compact strip: just enough for a single row of plot + a few
        # pixels of padding. Adjust if renderers need more room.
        self.setMinimumHeight(48)
        self.setMaximumHeight(52)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Left spacer matches the arranger's track-labels column so the
        # content widget's x=0 aligns with canvas x=0.
        self._spacer = QWidget(self)
        self._spacer.setFixedWidth(_LABEL_COL_WIDTH)
        root.addWidget(self._spacer)

        # Content widget hosts the renderer. It expands to fill the
        # remaining horizontal space and clips to the arranger's
        # visible beat range.
        self._content_holder = QWidget(self)
        self._content_holder.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding,
        )
        self._content_layout = QHBoxLayout(self._content_holder)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(0)
        root.addWidget(self._content_holder, 1)

        # Close button — small, plain, far right.
        self._close_btn = QPushButton("\u00d7", self)
        self._close_btn.setFixedSize(18, 18)
        self._close_btn.setToolTip("Clear broadcast band")
        self._close_btn.clicked.connect(self._on_close_clicked)
        root.addWidget(self._close_btn)

        self._renderer: Optional[QWidget] = None
        self._renderer_schema: Optional[str] = None
        self._block = None  # type: ignore[assignment]

        # Arrangement-sync state. Populated via set_arrangement_view.
        self._arr = None
        self._arr_scrollbar = None
        self._viewport_filter: Optional[_ViewportResizeFilter] = None

        self.hide()  # hidden until a broadcaster sets us

    # -- Arrangement sync ------------------------------------------------

    def set_arrangement_view(self, arr) -> None:
        """Wire up horizontal-view sync with ``arr`` (an ArrangementView).

        May be called multiple times (late wiring is fine); subsequent
        calls disconnect the previous binding first.
        """
        # Detach from a previous arrangement, if any.
        if self._arr is not None:
            try:
                if self._arr_scrollbar is not None:
                    self._arr_scrollbar.valueChanged.disconnect(
                        self._on_arr_view_changed
                    )
            except (RuntimeError, TypeError):
                pass
            if self._viewport_filter is not None:
                try:
                    vp = self._arr.scroll_area.viewport()
                    vp.removeEventFilter(self._viewport_filter)
                except Exception:  # noqa: BLE001
                    pass
                self._viewport_filter = None
        self._arr = arr
        self._arr_scrollbar = None
        if arr is None:
            return
        try:
            sa = arr.scroll_area
            self._arr_scrollbar = sa.horizontalScrollBar()
            self._arr_scrollbar.valueChanged.connect(
                self._on_arr_view_changed
            )
            # Event-filter the viewport so we re-sync on resize (splitter
            # drag, window resize, etc.).
            self._viewport_filter = _ViewportResizeFilter(self)
            sa.viewport().installEventFilter(self._viewport_filter)
        except Exception as exc:  # noqa: BLE001
            logger.warning("arrangement-view sync wiring failed: %s", exc)
        # Apply an initial sync if we already have a scalar_curve renderer.
        self._sync_renderer_range()

    def _on_arr_view_changed(self, _value: int = 0) -> None:
        self._sync_renderer_range()

    def _compute_visible_range(self) -> Optional[tuple]:
        """Return ``(left_beat, right_beat)`` of the arranger viewport,
        or ``None`` if no arrangement is wired."""
        if self._arr is None or self._arr_scrollbar is None:
            return None
        try:
            bw = float(self._bw())
            left_px = float(self._arr_scrollbar.value())
            vp_w = float(self._arr.scroll_area.viewport().width())
        except Exception:  # noqa: BLE001
            return None
        if bw <= 0.0:
            return None
        left_beat = left_px / bw
        right_beat = (left_px + vp_w) / bw
        return (left_beat, right_beat)

    def _bw(self) -> float:
        # Pull BW from ArrangementView so the two stay in lockstep even
        # if the constant ever changes.
        if self._arr is not None:
            return float(getattr(self._arr, "BW", 30))
        try:
            from ...ui.arrangement import ArrangementView
            return float(ArrangementView.BW)
        except Exception:  # noqa: BLE001
            return 30.0

    def _sync_renderer_range(self) -> None:
        """If the current renderer supports fixed-range, push the
        arranger's current visible beat range into it."""
        if not isinstance(self._renderer, ScalarCurveRenderer):
            return
        rng = self._compute_visible_range()
        if rng is None:
            self._renderer.set_beat_range(None, None)
        else:
            self._renderer.set_beat_range(rng[0], rng[1])

    # -- API -------------------------------------------------------------

    def set_broadcaster(self, block, annotation: Optional[Annotation]) -> None:
        """Display ``annotation`` from ``block`` in the band.

        If ``annotation`` is None, the band still becomes visible —
        but with no renderer. We tolerate None defensively.
        """
        self._block = block
        if annotation is None:
            self._teardown_renderer()
            self.show()
            return
        self._apply_annotation(annotation)
        self.show()

    def update_annotation(self, annotation: Annotation) -> None:
        """Refresh an already-shown annotation's data."""
        if annotation is None:
            return
        self._apply_annotation(annotation)
        self.show()

    def clear(self) -> None:
        """Hide the band and detach any renderer."""
        self._teardown_renderer()
        self._block = None
        self.hide()

    # -- Internals -------------------------------------------------------

    def _apply_annotation(self, ann: Annotation) -> None:
        schema = ann.schema
        if self._renderer is None or self._renderer_schema != schema:
            self._teardown_renderer()
            widget = make_renderer(schema, self._content_holder)
            self._content_layout.addWidget(widget)
            self._renderer = widget
            self._renderer_schema = schema
        try:
            self._renderer.update_data(ann.data, ann.render_hint)
        except Exception as exc:  # noqa: BLE001
            logger.exception("broadcast renderer update_data failed: %s", exc)
        # After data arrives, push the current arranger view range in.
        self._sync_renderer_range()

    def _teardown_renderer(self) -> None:
        if self._renderer is not None:
            self._renderer.setParent(None)
            self._renderer.deleteLater()
        self._renderer = None
        self._renderer_schema = None

    def _on_close_clicked(self) -> None:
        # Emit the signal; the host clears the actual state (this in turn
        # calls clear() on us via its clear_broadcaster path).
        self.cleared.emit()


class _ViewportResizeFilter(QObject):
    """Event filter that triggers a range re-sync whenever the
    arranger's scroll-area viewport is resized."""

    def __init__(self, band: BroadcastBand):
        super().__init__(band)
        self._band = band

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt API)
        if event.type() == QEvent.Resize:
            try:
                self._band._sync_renderer_range()
            except Exception:  # noqa: BLE001
                pass
        return False  # never consume — let Qt keep processing


__all__ = ["BroadcastBand"]
