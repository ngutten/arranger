"""Tests for the Euclidean Syncopate plugin + Bjorklund helper."""

from __future__ import annotations

import math
import pytest

from standalone.state import AppState, Pattern, Note, Track, Placement
from standalone.song_plugins.song_view import SongView
from standalone.song_plugins.api import SelectionSnapshot, MoveNote, ResizeNote
from standalone.song_plugins.builtin._euclidean import euclidean_pattern
from standalone.song_plugins.builtin.euclidean_syncopate import (
    EuclideanSyncopatePlugin,
)


class _DummyProgress:
    def __init__(self):
        self.cancelled = False

    def phase(self, name):
        pass

    def update(self, fraction, message=None):
        pass


def _empty_selection():
    return SelectionSnapshot(
        notes=frozenset(), placements=frozenset(), primary='none',
        current_pattern_id=None, current_variation_id=None,
        current_beat_pattern_id=None, current_auto_pattern_id=None,
    )


# ---------------------------------------------------------------------------
# Bjorklund correctness
# ---------------------------------------------------------------------------

def _gaps(positions, steps):
    """Return the gap sequence of a pulse pattern wrapping at ``steps``."""
    if not positions:
        return []
    ext = list(positions) + [positions[0] + steps]
    return [ext[i + 1] - ext[i] for i in range(len(positions))]


def test_bjorklund_zero_and_full():
    assert euclidean_pattern(0, 4) == []
    assert euclidean_pattern(4, 4) == [0, 1, 2, 3]
    assert euclidean_pattern(5, 4) == [0, 1, 2, 3]  # pulses >= steps


def test_bjorklund_single_pulse():
    assert euclidean_pattern(1, 4) == [0]


def test_bjorklund_3_in_8():
    # (3, 8) is unambiguous: evenly spaced thirds.
    assert euclidean_pattern(3, 8) == [0, 3, 6]


def test_bjorklund_5_in_8():
    pos = euclidean_pattern(5, 8)
    assert pos[0] == 0
    assert len(pos) == 5
    # 5 pulses across 8 steps -> 5 gaps summing to 8 -> 2 ones, 3 twos.
    g = _gaps(pos, 8)
    assert sorted(g) == [1, 1, 2, 2, 2]


def test_bjorklund_5_in_16_is_maximally_even():
    pos = euclidean_pattern(5, 16)
    assert pos[0] == 0
    assert len(pos) == 5
    # Both [0,3,6,9,12] (gaps 3,3,3,3,4) and [0,3,6,10,13] (gaps 3,3,4,3,3)
    # are maximally-even Bjorklund outputs; accept any rotation of either.
    g = sorted(_gaps(pos, 16))
    assert g == [3, 3, 3, 3, 4]


def test_bjorklund_negative_inputs_safe():
    assert euclidean_pattern(-1, 8) == []
    assert euclidean_pattern(3, 0) == []


# ---------------------------------------------------------------------------
# Plugin fixture and tests
# ---------------------------------------------------------------------------

def _mk_state_with_five_notes(repeats=1, extra_placements=0):
    s = AppState()
    s.bpm = 120.0
    t = Track(id=s.new_id(), name="T", channel=0)
    s.tracks.append(t)
    # 5 notes at 0, 0.5, 1.0, 1.5, 2.0 within a 4-beat pattern.
    notes = [
        Note(pitch=60 + i, start=i * 0.5, duration=0.4,
             velocity=100, note_id=s.new_id())
        for i in range(5)
    ]
    p = Pattern(id=s.new_id(), name="P", length=4.0, notes=notes,
                color='#fff', key='C', scale='major')
    s.patterns.append(p)
    pl = Placement(id=s.new_id(), track_id=t.id, pattern_id=p.id,
                   time=0.0, repeats=repeats)
    s.placements.append(pl)
    for k in range(extra_placements):
        # Add another placement referencing the same pattern at a later time.
        t2 = Track(id=s.new_id(), name=f"T_extra_{k}", channel=0)
        s.tracks.append(t2)
        pl_extra = Placement(id=s.new_id(), track_id=t2.id, pattern_id=p.id,
                             time=(k + 1) * 8.0, repeats=1)
        s.placements.append(pl_extra)
    return s, p, notes


def test_five_notes_redistribute_to_euclidean_positions():
    state, pat, notes = _mk_state_with_five_notes()
    view = SongView(state, _empty_selection())
    plugin = EuclideanSyncopatePlugin()
    result = plugin.run(view, {
        "phrase_beats": 4.0, "slots_per_phrase": 16,
        "preserve_duration": True, "scope": "whole",
    }, _DummyProgress())

    # All 5 notes fall in phrase 0 (beats 0..4). E(5, 16) with subdivision=4
    # yields pattern-local beats [0, 0.75, 1.5, 2.25, 3.0] for our impl
    # (slot positions [0,3,6,9,12] / 4). Any rotation of the canonical
    # Bjorklund output is accepted.
    moves = [op for op in result.operations if isinstance(op, MoveNote)]
    # The first note stays at 0 (no-op suppressed), so only 4 MoveNote ops.
    assert 4 <= len(moves) <= 5
    note_by_id = {n.note_id: n for n in notes}
    # Reconstruct the full set of target pattern-local starts: moved notes
    # from the ops + unchanged notes at their original start.
    moved_ids = {op.note_id for op in moves}
    starts = [op.new_start for op in moves]
    for n in notes:
        if n.note_id not in moved_ids:
            starts.append(n.start)
    starts.sort()
    assert len(starts) == 5
    # Compare gaps — rotation-independent check of Euclidean-ness.
    gaps = [starts[i + 1] - starts[i] for i in range(len(starts) - 1)]
    gaps.append(4.0 - starts[-1] + starts[0])  # wrap gap
    gaps_rounded = sorted(round(g * 4) for g in gaps)  # in slot units
    assert gaps_rounded == [3, 3, 3, 3, 4]


def test_dedupe_across_placements():
    # Pattern referenced by 3 placements: same note should emit exactly one op.
    state, pat, notes = _mk_state_with_five_notes(extra_placements=2)
    view = SongView(state, _empty_selection())
    plugin = EuclideanSyncopatePlugin()
    result = plugin.run(view, {
        "phrase_beats": 4.0, "slots_per_phrase": 16,
        "preserve_duration": True, "scope": "whole",
    }, _DummyProgress())
    moves = [op for op in result.operations if isinstance(op, MoveNote)]
    # At most one MoveNote per (pattern_id, note_id).
    keys = [(op.pattern_id, op.note_id) for op in moves]
    assert len(keys) == len(set(keys))


def test_preserve_duration_false_emits_resizes():
    state, pat, notes = _mk_state_with_five_notes()
    view = SongView(state, _empty_selection())
    plugin = EuclideanSyncopatePlugin()
    result = plugin.run(view, {
        "phrase_beats": 4.0, "slots_per_phrase": 16,
        "preserve_duration": False, "scope": "whole",
    }, _DummyProgress())
    resizes = [op for op in result.operations if isinstance(op, ResizeNote)]
    assert len(resizes) > 0
    for op in resizes:
        assert op.new_duration > 0


def test_preserve_duration_true_no_resize():
    state, pat, notes = _mk_state_with_five_notes()
    view = SongView(state, _empty_selection())
    plugin = EuclideanSyncopatePlugin()
    result = plugin.run(view, {
        "phrase_beats": 4.0, "slots_per_phrase": 16,
        "preserve_duration": True, "scope": "whole",
    }, _DummyProgress())
    resizes = [op for op in result.operations if isinstance(op, ResizeNote)]
    assert resizes == []


def test_variations_skipped(fixture_state):
    view = SongView(fixture_state, _empty_selection())
    plugin = EuclideanSyncopatePlugin()
    result = plugin.run(view, {
        "phrase_beats": 4.0, "slots_per_phrase": 16,
        "preserve_duration": True, "scope": "whole",
    }, _DummyProgress())
    var_id = fixture_state._test_refs['var'].id
    for op in result.operations:
        assert op.pattern_id != var_id
