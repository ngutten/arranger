"""Selection scope resolution — the six cases."""

from __future__ import annotations

import pytest

from standalone.song_plugins.api import (
    PluginManifest, SelectionSnapshot,
    SelectionMismatch, SelectionEmpty,
)
from standalone.song_plugins.registry import resolve_selection_scope


def _mk_manifest(selection_kinds):
    return PluginManifest(
        id="p", name="p", version="1", description="",
        capabilities=("analyze",),
        scopes=("selection",),
        selection_kinds=selection_kinds,
    )


def _mk_sel(notes=(), placements=(), primary='none'):
    return SelectionSnapshot(
        notes=frozenset(notes),
        placements=frozenset(placements),
        primary=primary,
        current_pattern_id=None, current_variation_id=None,
        current_beat_pattern_id=None, current_auto_pattern_id=None,
    )


def test_notes_only_with_notes_plugin():
    m = _mk_manifest(("notes",))
    sel = _mk_sel(notes={1, 2, 3}, primary='notes')
    scope = resolve_selection_scope(m, sel)
    assert scope.kind == "notes"
    assert set(scope.note_ids) == {1, 2, 3}


def test_placements_only_with_notes_plugin_mismatch():
    m = _mk_manifest(("notes",))
    sel = _mk_sel(placements={10}, primary='placements')
    with pytest.raises(SelectionMismatch):
        resolve_selection_scope(m, sel)


def test_both_accepted_primary_notes():
    m = _mk_manifest(("notes", "placements"))
    sel = _mk_sel(notes={1}, placements={10}, primary='notes')
    scope = resolve_selection_scope(m, sel)
    assert scope.kind == "notes"


def test_both_accepted_primary_placements():
    m = _mk_manifest(("notes", "placements"))
    sel = _mk_sel(notes={1}, placements={10}, primary='placements')
    scope = resolve_selection_scope(m, sel)
    assert scope.kind == "placements"


def test_both_selected_but_plugin_accepts_only_placements():
    m = _mk_manifest(("placements",))
    sel = _mk_sel(notes={1}, placements={10}, primary='notes')
    scope = resolve_selection_scope(m, sel)
    # Plugin narrows to placements regardless of primary.
    assert scope.kind == "placements"
    assert set(scope.placement_ids) == {10}


def test_nothing_selected_raises_empty():
    m = _mk_manifest(("notes", "placements"))
    sel = _mk_sel()
    with pytest.raises(SelectionEmpty):
        resolve_selection_scope(m, sel)
