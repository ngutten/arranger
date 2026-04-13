"""Tests for SelectionProvider snapshot building.

These avoid spinning up a real Qt app — they stub the widgets with plain
objects exposing the attributes SelectionProvider reads.
"""

from types import SimpleNamespace

import pytest

from standalone.song_plugins.ui.selection_provider import SelectionProvider


class _Placement:
    def __init__(self, pid):
        self.id = pid


def _make_app(state, note_indices=None, placement_ids=None,
              beat_placement_ids=None, auto_placement_ids=None):
    piano_roll = SimpleNamespace(_selected=set(note_indices or ()))
    arrangement = SimpleNamespace(
        selected_placements=[_Placement(i) for i in (placement_ids or ())],
        selected_beat_placements=[_Placement(i) for i in (beat_placement_ids or ())],
        selected_automation_placements=[_Placement(i) for i in (auto_placement_ids or ())],
    )
    return SimpleNamespace(state=state, piano_roll=piano_roll,
                           arrangement=arrangement)


def test_empty_snapshot(fixture_state):
    state = fixture_state
    state.sel_pat = state._test_refs['p1'].id
    app = _make_app(state)
    prov = SelectionProvider(app)
    snap = prov.snapshot()
    assert snap.notes == frozenset()
    assert snap.placements == frozenset()
    assert snap.primary == 'none'
    assert snap.current_pattern_id == state.sel_pat


def test_note_indices_translate_to_note_ids(fixture_state):
    state = fixture_state
    p1 = state._test_refs['p1']
    state.sel_pat = p1.id
    state.sel_variation = None
    # Select indices 0 and 2 in pattern p1 (4 notes).
    app = _make_app(state, note_indices=[0, 2])
    prov = SelectionProvider(app)
    snap = prov.snapshot()
    expected = {p1.notes[0].note_id, p1.notes[2].note_id}
    assert snap.notes == frozenset(expected)
    assert snap.primary == 'notes'


def test_placement_ids_collected(fixture_state):
    state = fixture_state
    pl1 = state._test_refs['pl1']
    bpl = state._test_refs['bpl']
    apl = state._test_refs['apl']
    app = _make_app(state, placement_ids=[pl1.id],
                    beat_placement_ids=[bpl.id],
                    auto_placement_ids=[apl.id])
    prov = SelectionProvider(app)
    snap = prov.snapshot()
    assert snap.placements == frozenset({pl1.id, bpl.id, apl.id})
    assert snap.primary == 'placements'


def test_primary_beat_only(fixture_state):
    state = fixture_state
    bpl = state._test_refs['bpl']
    app = _make_app(state, beat_placement_ids=[bpl.id])
    prov = SelectionProvider(app)
    snap = prov.snapshot()
    assert snap.primary == 'beat_placements'


def test_primary_auto_only(fixture_state):
    state = fixture_state
    apl = state._test_refs['apl']
    app = _make_app(state, auto_placement_ids=[apl.id])
    prov = SelectionProvider(app)
    snap = prov.snapshot()
    assert snap.primary == 'automation_placements'


def test_primary_focus_break_ties(fixture_state):
    state = fixture_state
    p1 = state._test_refs['p1']
    pl1 = state._test_refs['pl1']
    state.sel_pat = p1.id
    app = _make_app(state, note_indices=[0], placement_ids=[pl1.id])
    prov = SelectionProvider(app)

    # No focus yet → default to notes.
    snap = prov.snapshot()
    assert snap.primary == 'notes'

    # Simulate focusing the arrangement.
    prov._on_focus_in('placements')
    snap = prov.snapshot()
    assert snap.primary == 'placements'

    # Back to piano roll.
    prov._on_focus_in('notes')
    snap = prov.snapshot()
    assert snap.primary == 'notes'


def test_variation_pattern_used_for_note_lookup(fixture_state):
    state = fixture_state
    var = state._test_refs['var']
    p1 = state._test_refs['p1']
    state.sel_pat = None
    state.sel_variation = var.id
    # Indices map against the parent pattern.
    app = _make_app(state, note_indices=[0])
    prov = SelectionProvider(app)
    snap = prov.snapshot()
    # Parent-pattern note[0] is what we should find.
    assert p1.notes[0].note_id in snap.notes
    assert snap.current_variation_id == var.id
