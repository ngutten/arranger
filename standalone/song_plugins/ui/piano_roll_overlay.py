"""PianoRollOverlay — stores region/tag annotations for the piano roll.

Not a widget. The piano-roll widget queries this object during its
``paintEvent`` to paint colored boxes behind notes (for the ``regions``
schema) and per-note badges (for ``note_tags``).

Lifecycle parallels :class:`BroadcastBand`: ``set_broadcaster`` installs
an annotation, ``update_annotation`` refreshes it, ``clear`` hides it.
The overlay emits :pyattr:`changed` after every mutation so the piano
roll can schedule a repaint.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, Signal

from ..api import Annotation

logger = logging.getLogger(__name__)


class PianoRollOverlay(QObject):
    """Read-side cache of the currently-broadcasting piano-roll schema.

    Hosts a single annotation at a time (radio-button behaviour). The
    piano roll reads it by asking :meth:`regions_for_pattern` or
    :meth:`note_tag_for`.
    """

    #: Emitted after any state change; piano-roll widgets should connect
    #: this to their ``update()`` slot.
    changed = Signal()

    #: Emitted when the user dismisses the overlay from its own close
    #: affordance (if a close button is added later). Mirrors
    #: :pyattr:`BroadcastBand.cleared`.
    cleared = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._block = None
        self._schema: Optional[str] = None
        self._data: Any = None
        self._render_hint: dict = {}

        # Derived indexes built by :meth:`_reindex`.
        # pattern_id -> list of region dicts (or None => global regions)
        self._regions_by_pattern: Dict[Optional[int], List[dict]] = {}
        self._variation_regions: Dict[int, List[dict]] = {}
        # note_id -> tag value (str or dict)
        self._note_tag: Dict[int, Any] = {}
        # placement_id -> tag value
        self._placement_tag: Dict[int, Any] = {}

    # -- API mirrored from BroadcastBand --------------------------------

    def set_broadcaster(self, block, annotation: Optional[Annotation]) -> None:
        self._block = block
        if annotation is None:
            self._schema = None
            self._data = None
            self._render_hint = {}
        else:
            self._apply(annotation)
        self._reindex()
        self.changed.emit()

    def update_annotation(self, annotation: Annotation) -> None:
        if annotation is None:
            return
        self._apply(annotation)
        self._reindex()
        self.changed.emit()

    def clear(self) -> None:
        self._block = None
        self._schema = None
        self._data = None
        self._render_hint = {}
        self._reindex()
        self.changed.emit()

    # -- Queries used by the piano roll ---------------------------------

    def has_regions(self) -> bool:
        return self._schema == "regions" and bool(self._data)

    def has_note_tags(self) -> bool:
        return self._schema == "note_tags" and bool(self._data)

    def has_placement_tags(self) -> bool:
        return self._schema == "placement_tags" and bool(self._data)

    def regions_for_pattern(self, pattern_id: int,
                            variation_id: Optional[int] = None
                            ) -> List[dict]:
        """Regions whose coordinates are pattern-local and bound to this
        pattern (or variation). Regions without a ``pattern_id`` field
        are returned under the ``None`` key and treated as pattern-agnostic.
        """
        if not self.has_regions():
            return []
        out: List[dict] = []
        out.extend(self._regions_by_pattern.get(pattern_id, ()))
        if variation_id is not None:
            out.extend(self._variation_regions.get(variation_id, ()))
        # Global / pattern-agnostic regions — caller can decide whether
        # to render these (typically yes for song-level analyses).
        out.extend(self._regions_by_pattern.get(None, ()))
        return out

    def note_tag_for(self, note_id: int) -> Optional[Any]:
        if not self.has_note_tags():
            return None
        return self._note_tag.get(note_id)

    def placement_tag_for(self, placement_id: int) -> Optional[Any]:
        if not self.has_placement_tags():
            return None
        return self._placement_tag.get(placement_id)

    def current_schema(self) -> Optional[str]:
        return self._schema

    def current_block(self):
        return self._block

    # -- Internals ------------------------------------------------------

    def _apply(self, ann: Annotation) -> None:
        self._schema = ann.schema
        self._data = ann.data
        self._render_hint = dict(ann.render_hint or {})

    def _reindex(self) -> None:
        self._regions_by_pattern = {}
        self._variation_regions = {}
        self._note_tag = {}
        self._placement_tag = {}

        if self._schema == "regions" and isinstance(self._data, list):
            for r in self._data:
                if not isinstance(r, dict):
                    continue
                pid = r.get("pattern_id")
                vid = r.get("variation_id")
                if vid is not None:
                    self._variation_regions.setdefault(vid, []).append(r)
                else:
                    self._regions_by_pattern.setdefault(pid, []).append(r)
        elif self._schema == "note_tags" and isinstance(self._data, dict):
            for k, v in self._data.items():
                if isinstance(k, int):
                    self._note_tag[k] = v
        elif self._schema == "placement_tags" and isinstance(self._data, dict):
            for k, v in self._data.items():
                if isinstance(k, int):
                    self._placement_tag[k] = v


def tag_label(tag: Any) -> str:
    """Extract a short label from a tag value (str or dict)."""
    if isinstance(tag, str):
        return tag
    if isinstance(tag, dict):
        return str(tag.get("label", ""))
    return ""


def tag_color(tag: Any, default: Optional[str] = None) -> Optional[str]:
    """Extract a color ("#rrggbb") from a tag value, or ``default``."""
    if isinstance(tag, dict):
        c = tag.get("color")
        if isinstance(c, str):
            return c
    return default


__all__ = ["PianoRollOverlay", "tag_label", "tag_color"]
