"""Build a :class:`SelectionSnapshot` from the live App state + widgets."""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QEvent, QObject

from ..api import SelectionSnapshot

logger = logging.getLogger(__name__)


class SelectionProvider:
    """Queries the app for the current selection on demand.

    Also tracks which editor widget was most recently focused so that
    :attr:`SelectionSnapshot.primary` can be populated correctly.
    """

    def __init__(self, app):
        self.app = app
        self._last_focused: str = "none"  # 'notes' / 'placements' / 'beat_placements' / 'automation_placements' / 'none'
        self._focus_filter: Optional[FocusFilter] = None

    # -- Focus tracking --------------------------------------------------

    def install_focus_tracker(self) -> None:
        """Install Qt event filters on the relevant editor widgets."""
        if self._focus_filter is not None:
            return
        self._focus_filter = FocusFilter(self)
        app = self.app
        # Note: some of these widgets have inner canvas children. Install on
        # the widget itself and on its canvas child where present.
        targets = []
        if hasattr(app, 'piano_roll'):
            targets.append((app.piano_roll, 'notes'))
            gw = getattr(app.piano_roll, 'grid_widget', None)
            if gw is not None:
                targets.append((gw, 'notes'))
        if hasattr(app, 'arrangement'):
            # The arrangement canvas covers placements, beats, auto — we
            # disambiguate at query time using which selection list is
            # non-empty. The focus tracker records 'placements' as the
            # coarse category.
            targets.append((app.arrangement, 'placements'))
            cw = getattr(app.arrangement, 'canvas_widget', None)
            if cw is not None:
                targets.append((cw, 'placements'))
        if hasattr(app, 'beat_grid'):
            targets.append((app.beat_grid, 'beat_placements'))
        if hasattr(app, 'automation_curve'):
            targets.append((app.automation_curve, 'automation_placements'))

        for widget, kind in targets:
            try:
                widget.installEventFilter(self._focus_filter)
                self._focus_filter.register(widget, kind)
            except Exception as exc:
                logger.debug("failed to install focus filter on %r: %s",
                             widget, exc)

    def _on_focus_in(self, kind: str) -> None:
        self._last_focused = kind

    # -- Snapshot build --------------------------------------------------

    def snapshot(self) -> SelectionSnapshot:
        app = self.app
        state = app.state

        # ---- notes ----
        note_ids = set()
        pr = getattr(app, 'piano_roll', None)
        pat = None
        current_pattern_id = state.sel_pat
        current_variation_id = state.sel_variation
        if pr is not None and getattr(pr, '_selected', None):
            if current_variation_id:
                var = state.find_variation(current_variation_id)
                pat = state.find_pattern(var.parent_id) if var else None
            else:
                pat = state.find_pattern(current_pattern_id)
            if pat is not None:
                for idx in pr._selected:
                    if 0 <= idx < len(pat.notes):
                        nid = pat.notes[idx].note_id
                        if nid:
                            note_ids.add(int(nid))

        # ---- placements (melodic + beat + automation) ----
        placements = set()
        arr = getattr(app, 'arrangement', None)
        have_places = False
        have_beat = False
        have_auto = False
        if arr is not None:
            for pl in getattr(arr, 'selected_placements', []) or []:
                if getattr(pl, 'id', None) is not None:
                    placements.add(int(pl.id))
                    have_places = True
            for bp in getattr(arr, 'selected_beat_placements', []) or []:
                if getattr(bp, 'id', None) is not None:
                    placements.add(int(bp.id))
                    have_beat = True
            for ap in getattr(arr, 'selected_automation_placements', []) or []:
                if getattr(ap, 'id', None) is not None:
                    placements.add(int(ap.id))
                    have_auto = True

        # ---- primary ----
        primary = "none"
        if note_ids and not placements:
            primary = "notes"
        elif placements and not note_ids:
            if have_beat and not have_places and not have_auto:
                primary = "beat_placements"
            elif have_auto and not have_places and not have_beat:
                primary = "automation_placements"
            else:
                primary = "placements"
        elif note_ids and placements:
            # Both selected — use focus hint.
            lf = self._last_focused
            if lf in ("notes", "placements", "beat_placements",
                      "automation_placements"):
                primary = lf
            else:
                primary = "notes"

        return SelectionSnapshot(
            notes=frozenset(note_ids),
            placements=frozenset(placements),
            primary=primary,
            current_pattern_id=current_pattern_id,
            current_variation_id=current_variation_id,
            current_beat_pattern_id=state.sel_beat_pat,
            current_auto_pattern_id=state.sel_auto_pat,
        )


class FocusFilter(QObject):
    """Qt event filter that records the most recently focused editor."""

    def __init__(self, provider: SelectionProvider):
        super().__init__(provider.app)
        self._provider = provider
        self._widgets: dict = {}  # id(widget) -> kind

    def register(self, widget, kind: str) -> None:
        self._widgets[id(widget)] = kind

    def eventFilter(self, obj, event):
        if event.type() == QEvent.FocusIn:
            kind = self._widgets.get(id(obj))
            if kind:
                self._provider._on_focus_in(kind)
        return False  # don't consume
