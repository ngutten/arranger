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
from ..registry import broadcast_target_for_schema, load_builtin_plugins
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
        # Broadcast state — wired up after construction by the app.
        # A broadcaster is a single block at a time (radio-button).
        # Its annotation is routed to either the band or the piano-roll
        # overlay based on schema — see ``broadcast_target_for_schema``.
        self._broadcaster = None
        self._broadcast_band = None
        self._piano_roll_overlay = None
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

    def set_piano_roll_overlay(self, overlay) -> None:
        """Wire up the :class:`PianoRollOverlay` data holder.

        Analogous to :meth:`set_broadcast_band`: installed once by the
        app at startup, reachable via the current broadcaster routing.
        """
        if self._piano_roll_overlay is not None:
            try:
                self._piano_roll_overlay.cleared.disconnect(
                    self.clear_broadcaster
                )
            except (RuntimeError, TypeError):
                pass
        self._piano_roll_overlay = overlay
        if overlay is not None:
            try:
                overlay.cleared.connect(self.clear_broadcaster)
            except Exception as exc:  # noqa: BLE001
                logger.warning("failed to connect overlay.cleared: %s", exc)

    def current_broadcaster(self):
        """Return the currently broadcasting block, or None."""
        return self._broadcaster

    def _target_for_annotation(self, annotation):
        """Return the target widget (band or overlay) for an annotation's
        schema. Returns ``None`` if no target is wired for the schema.
        """
        if annotation is None:
            return None
        target = broadcast_target_for_schema(annotation.schema)
        if target == "band":
            return self._broadcast_band
        if target == "overlay":
            return self._piano_roll_overlay
        return None

    def _clear_all_targets(self) -> None:
        """Tell both broadcast surfaces to drop their state. Called on
        broadcaster handoff so we don't leave stale data in the target
        that the new broadcaster isn't routing to.
        """
        for t in (self._broadcast_band, self._piano_roll_overlay):
            if t is None:
                continue
            try:
                t.clear()
            except Exception as exc:  # noqa: BLE001
                logger.exception("target clear failed: %s", exc)

    def set_broadcaster(self, block) -> None:
        """Install ``block`` as the current broadcaster (radio-button).

        Unchecks the previous broadcaster's Broadcast checkbox (via the
        block's ``_on_broadcast_released`` hook) so only one block is
        active at a time. Routes the block's latest annotation to either
        the band or the piano-roll overlay based on the schema.
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
        # Clear any target the previous broadcaster populated — the new
        # block may route to a different surface.
        self._clear_all_targets()
        ann = getattr(block, "_last_annotation", None)
        target = self._target_for_annotation(ann)
        if target is None:
            return
        try:
            target.set_broadcaster(block, ann)
        except Exception as exc:  # noqa: BLE001
            logger.exception("broadcast target set_broadcaster failed: %s", exc)

    def clear_broadcaster(self) -> None:
        """Clear the current broadcaster and hide both targets."""
        prev = self._broadcaster
        self._broadcaster = None
        if prev is not None:
            try:
                prev._on_broadcast_released()
            except Exception as exc:  # noqa: BLE001
                logger.exception("broadcast-release hook failed: %s", exc)
        self._clear_all_targets()

    def broadcaster_annotation_updated(self, block, annotation) -> None:
        """Called by a block when a fresh run produces a new annotation.

        Only forwards if ``block`` is the current broadcaster. Routing
        is recomputed each time so a block whose schema changes between
        runs ends up on the right surface.
        """
        if block is not self._broadcaster:
            return
        target = self._target_for_annotation(annotation)
        # If the target changes between runs, clear the previous one.
        other = (self._piano_roll_overlay if target is self._broadcast_band
                 else self._broadcast_band)
        if target is not None and other is not None:
            try:
                other.clear()
            except Exception as exc:  # noqa: BLE001
                logger.exception("other-target clear failed: %s", exc)
        if target is None:
            return
        try:
            target.update_annotation(annotation)
        except Exception as exc:  # noqa: BLE001
            logger.exception("broadcast target update failed: %s", exc)

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
        # If the outgoing block was our broadcaster, drop both targets.
        if block is self._broadcaster:
            self._broadcaster = None
            self._clear_all_targets()

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
