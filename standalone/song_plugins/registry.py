"""Plugin discovery and manifest validation."""

from __future__ import annotations

import importlib
import importlib.util
import logging
from pathlib import Path
from typing import Dict, Optional, Type

from .api import (
    PluginManifest, SongPlugin, SelectionSnapshot, Scope,
    SelectionMismatch, SelectionEmpty,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Broadcast-band eligibility
# ---------------------------------------------------------------------------

#: Schemas that carry a beat-time axis and are therefore eligible to claim
#: the broadcast band near the arranger.
BAND_ELIGIBLE_SCHEMAS: frozenset = frozenset({
    "scalar_curve", "multi_curve", "events", "grid2d",
})

#: Schemas that target the piano-roll overlay (painted behind notes).
OVERLAY_ELIGIBLE_SCHEMAS: frozenset = frozenset({
    "regions", "note_tags", "placement_tags",
})

#: Schemas that any broadcaster can claim — i.e. band ∪ overlay.
#: Retained under the old name for source compatibility with existing
#: callers.
BROADCAST_ELIGIBLE_SCHEMAS: frozenset = (
    BAND_ELIGIBLE_SCHEMAS | OVERLAY_ELIGIBLE_SCHEMAS
)


def broadcast_target_for_schema(schema: str) -> Optional[str]:
    """Return ``"band"``, ``"overlay"``, or ``None`` for a schema name.

    Used by :class:`PluginHost` to route an active broadcaster's annotation
    to the appropriate surface. Schemas outside both sets return ``None``
    (not broadcastable).
    """
    if schema in BAND_ELIGIBLE_SCHEMAS:
        return "band"
    if schema in OVERLAY_ELIGIBLE_SCHEMAS:
        return "overlay"
    return None


def is_broadcast_eligible(manifest: PluginManifest) -> bool:
    """Return True if plugin is eligible to claim the broadcast band.

    Honors ``manifest.broadcast_eligible`` when non-None; otherwise derives
    from the intersection of ``manifest.schemas`` with time-axis schemas
    (see :data:`BROADCAST_ELIGIBLE_SCHEMAS`).
    """
    if manifest.broadcast_eligible is not None:
        return bool(manifest.broadcast_eligible)
    return bool(set(manifest.schemas or ()) & BROADCAST_ELIGIBLE_SCHEMAS)


# ---------------------------------------------------------------------------
# Manifest validation
# ---------------------------------------------------------------------------

def validate_manifest(m: PluginManifest) -> None:
    if not m.id:
        raise ValueError("manifest: id must be non-empty")
    if not m.capabilities:
        raise ValueError(f"manifest {m.id!r}: capabilities must be non-empty")
    for c in m.capabilities:
        if c not in ('analyze', 'transform', 'generate'):
            raise ValueError(f"manifest {m.id!r}: unknown capability {c!r}")
    for s in m.schemas:
        if s not in ('scalar_curve', 'multi_curve', 'grid2d', 'events',
                     'note_tags', 'placement_tags', 'regions',
                     'stats', 'custom'):
            raise ValueError(f"manifest {m.id!r}: unknown schema {s!r}")
    for sc in m.scopes:
        if sc not in ('whole', 'range', 'tracks', 'selection'):
            raise ValueError(f"manifest {m.id!r}: unknown scope {sc!r}")
    if 'selection' in m.scopes and not m.selection_kinds:
        raise ValueError(
            f"manifest {m.id!r}: 'selection' scope requires selection_kinds")
    for sk in m.selection_kinds:
        if sk not in ('notes', 'placements'):
            raise ValueError(f"manifest {m.id!r}: unknown selection_kind {sk!r}")
    if m.persistence_default not in ('transient', 'cached', 'authoritative'):
        raise ValueError(
            f"manifest {m.id!r}: bad persistence_default {m.persistence_default!r}")
    for p in m.params:
        if p.type not in ('int', 'float', 'bool', 'enum', 'string',
                          'beat_range', 'track_select'):
            raise ValueError(
                f"manifest {m.id!r}: param {p.key!r} has unknown type {p.type!r}")
        if p.type == 'enum' and not p.choices:
            raise ValueError(
                f"manifest {m.id!r}: enum param {p.key!r} requires choices")


# ---------------------------------------------------------------------------
# Selection scope resolution
# ---------------------------------------------------------------------------

def resolve_selection_scope(manifest: PluginManifest,
                            sel: SelectionSnapshot) -> Scope:
    """Map a SelectionSnapshot into a concrete Scope for a plugin.

    Rules:
      - If the plugin accepts 'notes' only: require selected notes, else mismatch.
      - If the plugin accepts 'placements' only: require selected placements,
        else mismatch.
      - If both are accepted: use snapshot.primary to disambiguate;
        fall back to whichever side is populated.
      - If nothing is selected: SelectionEmpty.
    """
    if not sel.notes and not sel.placements:
        raise SelectionEmpty("no selection")

    accepts_notes = 'notes' in manifest.selection_kinds
    accepts_places = 'placements' in manifest.selection_kinds

    have_notes = bool(sel.notes)
    have_places = bool(sel.placements)

    if accepts_notes and not accepts_places:
        if not have_notes:
            raise SelectionMismatch(
                f"plugin {manifest.id!r} needs notes selected")
        return Scope(kind="notes", note_ids=tuple(sorted(sel.notes)))

    if accepts_places and not accepts_notes:
        if not have_places:
            raise SelectionMismatch(
                f"plugin {manifest.id!r} needs placements selected")
        return Scope(kind="placements",
                     placement_ids=tuple(sorted(sel.placements)))

    if accepts_notes and accepts_places:
        # Both accepted — let primary decide, but honour one-sided selections.
        if have_notes and not have_places:
            return Scope(kind="notes", note_ids=tuple(sorted(sel.notes)))
        if have_places and not have_notes:
            return Scope(kind="placements",
                         placement_ids=tuple(sorted(sel.placements)))
        if sel.primary == 'notes':
            return Scope(kind="notes", note_ids=tuple(sorted(sel.notes)))
        return Scope(kind="placements",
                     placement_ids=tuple(sorted(sel.placements)))

    raise SelectionMismatch(
        f"plugin {manifest.id!r} declares no selection_kinds")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def load_plugins_from_dir(path: Path) -> Dict[str, Type[SongPlugin]]:
    """Import every .py under ``path`` and collect PLUGIN classes.

    Each module must expose ``PLUGIN: type[SongPlugin]`` (a class, not an
    instance). Modules that fail to import are logged and skipped.
    Duplicate ids: the first wins; subsequent duplicates are logged.
    """
    path = Path(path)
    result: Dict[str, Type[SongPlugin]] = {}
    if not path.is_dir():
        return result

    for py_file in sorted(path.glob("*.py")):
        if py_file.name.startswith('_'):
            continue
        mod_name = f"_song_plugin_{py_file.stem}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, py_file)
            if spec is None or spec.loader is None:
                logger.warning("Cannot load spec for %s", py_file)
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as exc:
            logger.warning("Failed to import plugin %s: %s", py_file, exc)
            continue

        plugin_cls = getattr(module, 'PLUGIN', None)
        if plugin_cls is None:
            logger.warning("Plugin file %s has no PLUGIN attribute", py_file)
            continue
        if not isinstance(plugin_cls, type) or not issubclass(plugin_cls, SongPlugin):
            logger.warning("%s.PLUGIN is not a SongPlugin subclass", py_file)
            continue

        manifest = getattr(plugin_cls, 'manifest', None)
        if manifest is None:
            logger.warning("Plugin %s has no manifest", py_file)
            continue
        try:
            validate_manifest(manifest)
        except ValueError as exc:
            logger.warning("Invalid manifest in %s: %s", py_file, exc)
            continue

        if manifest.id in result:
            logger.warning("Duplicate plugin id %r; skipping %s",
                           manifest.id, py_file)
            continue
        result[manifest.id] = plugin_cls
    return result


def load_builtin_plugins() -> Dict[str, Type[SongPlugin]]:
    """Load plugins from the ``standalone.song_plugins.builtin`` package.

    Unlike external plugins, these are loaded via the normal import
    system so their relative imports work.
    """
    builtin_dir = Path(__file__).parent / 'builtin'
    result: Dict[str, Type[SongPlugin]] = {}
    if not builtin_dir.is_dir():
        return result
    for py_file in sorted(builtin_dir.glob("*.py")):
        if py_file.name.startswith('_'):
            continue
        mod_name = f"standalone.song_plugins.builtin.{py_file.stem}"
        try:
            module = importlib.import_module(mod_name)
        except Exception as exc:
            logger.warning("Failed to import builtin plugin %s: %s",
                           mod_name, exc)
            continue
        plugin_cls = getattr(module, 'PLUGIN', None)
        if plugin_cls is None:
            continue
        if not isinstance(plugin_cls, type) or not issubclass(plugin_cls, SongPlugin):
            continue
        manifest = getattr(plugin_cls, 'manifest', None)
        if manifest is None:
            continue
        try:
            validate_manifest(manifest)
        except ValueError as exc:
            logger.warning("Invalid manifest in %s: %s", mod_name, exc)
            continue
        if manifest.id in result:
            logger.warning("Duplicate builtin plugin id %r", manifest.id)
            continue
        result[manifest.id] = plugin_cls
    return result
