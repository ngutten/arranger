"""PluginHost — thin controller owning plugins, selection, and dep-watcher.

The host:

* Loads and stores the plugin registry (``{plugin_id: SongPlugin class}``).
* Owns a :class:`SelectionProvider` that can snapshot the UI's current
  selection at any time.
* Tracks the set of active :class:`PluginBlock` instances and lets them
  subscribe to source-string notifications coming from ``App._on_state_change``.
* Is explicitly *not* a Qt widget — just a plain object the dock and
  blocks talk to.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional, Type

from ..api import SelectionSnapshot, SongPlugin
from ..registry import load_builtin_plugins
from ..song_view import SongView
from . import dep_watcher
from .selection_provider import SelectionProvider

logger = logging.getLogger(__name__)


class PluginHost:
    """Controller object shared by the dock and all active blocks."""

    def __init__(self, app):
        self.app = app
        self.selection_provider = SelectionProvider(app)
        self.plugins: Dict[str, Type[SongPlugin]] = {}
        # Blocks register themselves so the host can broadcast state changes.
        self._blocks: List = []
        # Broadcast band state — wired up after construction by the app.
        self._broadcaster = None
        self._broadcast_band = None
        self._reload_registry()

    # -- Broadcast band --------------------------------------------------

    def set_broadcast_band(self, band) -> None:
        """Wire up the shared :class:`BroadcastBand` widget.

        The app creates the band once at startup and hands it here. The
        band's ``cleared`` signal is connected to :meth:`clear_broadcaster`
        so the user can dismiss the band from its own close button.
        """
        # Disconnect any previous band.
        if self._broadcast_band is not None:
            try:
                self._broadcast_band.cleared.disconnect(self.clear_broadcaster)
            except (RuntimeError, TypeError):
                pass
        self._broadcast_band = band
        if band is not None:
            try:
                band.cleared.connect(self.clear_broadcaster)
            except Exception as exc:  # noqa: BLE001
                logger.warning("failed to connect band.cleared: %s", exc)

    def current_broadcaster(self):
        """Return the currently broadcasting block, or None."""
        return self._broadcaster

    def set_broadcaster(self, block) -> None:
        """Install ``block`` as the current broadcaster (radio-button).

        Unchecks the previous broadcaster's Broadcast checkbox (via the
        block's ``_on_broadcast_released`` hook) so only one block is
        active at a time. If the block already has an annotation, pushes
        it to the band; otherwise just updates the band header.
        """
        if block is self._broadcaster:
            return
        prev = self._broadcaster
        self._broadcaster = block
        if prev is not None and prev is not block:
            try:
                prev._on_broadcast_released()
            except Exception as exc:  # noqa: BLE001
                logger.exception("broadcast-release hook failed: %s", exc)
        if self._broadcast_band is None:
            return
        ann = getattr(block, "_last_annotation", None)
        try:
            self._broadcast_band.set_broadcaster(block, ann)
        except Exception as exc:  # noqa: BLE001
            logger.exception("broadcast band set_broadcaster failed: %s", exc)

    def clear_broadcaster(self) -> None:
        """Clear the current broadcaster and hide the band."""
        if self._broadcaster is None:
            if self._broadcast_band is not None:
                self._broadcast_band.clear()
            return
        prev = self._broadcaster
        self._broadcaster = None
        try:
            prev._on_broadcast_released()
        except Exception as exc:  # noqa: BLE001
            logger.exception("broadcast-release hook failed: %s", exc)
        if self._broadcast_band is not None:
            self._broadcast_band.clear()

    def broadcaster_annotation_updated(self, block, annotation) -> None:
        """Called by a block when a fresh run produces a new annotation.

        Only forwards to the band if ``block`` is the current broadcaster.
        """
        if block is not self._broadcaster or self._broadcast_band is None:
            return
        try:
            self._broadcast_band.update_annotation(annotation)
        except Exception as exc:  # noqa: BLE001
            logger.exception("broadcast band update failed: %s", exc)

    def _reload_registry(self) -> None:
        try:
            self.plugins = load_builtin_plugins()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load builtin plugins: %s", exc)
            self.plugins = {}

    # -- Selection -------------------------------------------------------

    def selection_snapshot(self) -> SelectionSnapshot:
        return self.selection_provider.snapshot()

    def build_song_view(self) -> SongView:
        return SongView(self.app.state, self.selection_snapshot())

    # -- Block lifecycle -------------------------------------------------

    def register_block(self, block) -> None:
        if block not in self._blocks:
            self._blocks.append(block)

    def unregister_block(self, block) -> None:
        if block in self._blocks:
            self._blocks.remove(block)
        # If the outgoing block was our broadcaster, drop the band.
        if block is self._broadcaster:
            self._broadcaster = None
            if self._broadcast_band is not None:
                try:
                    self._broadcast_band.clear()
                except Exception as exc:  # noqa: BLE001
                    logger.exception("band clear on unregister failed: %s", exc)

    def active_blocks(self) -> List:
        return list(self._blocks)

    # -- State-change broadcast -----------------------------------------

    def on_state_change(self, source) -> None:
        """Called by App._on_state_change after it performs its own work."""
        deps = dep_watcher.classify(source)
        if not deps:
            return
        for block in list(self._blocks):
            try:
                block.handle_state_change(deps)
            except Exception as exc:  # noqa: BLE001
                logger.exception("block dep-handler failed: %s", exc)
