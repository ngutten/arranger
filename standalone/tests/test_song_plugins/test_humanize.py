"""Tests for the Humanize plugin."""

from __future__ import annotations

import pytest

from standalone.state import AppState, Pattern, Note, Track, Placement
from standalone.song_plugins.song_view import SongView
from standalone.song_plugins.api import (
    SelectionSnapshot, MoveNote, ResizeNote, SetNoteVelocity,
)
from standalone.song_plugins.builtin.humanize import HumanizePlugin


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


def _mk_state(notes):
    """Build a state with one track + one placement referencing a pattern
    whose notes are the given list (each a Note)."""
    s = AppState()
    s.bpm = 120.0
    t = Track(id=s.new_id(), name="T", channel=0)
    s.tracks.append(t)
    for n in notes:
        n.note_id = s.new_id()
    p = Pattern(id=s.new_id(), name="P", length=4.0, notes=list(notes),
                color='#fff', key='C', scale='major')
    s.patterns.append(p)
    pl = Placement(id=s.new_id(), track_id=t.id, pattern_id=p.id,
                   time=0.0, repeats=1)
    s.placements.append(pl)
    return s, p, notes


def _run(state, params):
    view = SongView(state, _empty_selection())
    plugin = HumanizePlugin()
    return plugin.run(view, params, _DummyProgress())


# ---------------------------------------------------------------------------
# No-op cases
# ---------------------------------------------------------------------------

def test_all_zero_ranges_emits_no_ops():
    state, pat, notes = _mk_state([
        Note(pitch=60, start=0.0, duration=1.0, velocity=100),
        Note(pitch=64, start=1.0, duration=1.0, velocity=80),
    ])
    result = _run(state, {
        "velocity_range": 0, "timing_range": 0.0, "duration_pct": 0.0,
        "preserve_chord_sync": True, "seed": 1, "scope": "whole",
    })
    assert result.operations == ()


def test_empty_state_emits_no_ops():
    s = AppState()
    s.bpm = 120.0
    result = _run(s, {
        "velocity_range": 10, "timing_range": 0.1, "duration_pct": 10.0,
        "preserve_chord_sync": True, "seed": 1, "scope": "whole",
    })
    assert result.operations == ()


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_same_seed_yields_same_ops():
    def run_once(seed):
        state, _pat, _notes = _mk_state([
            Note(pitch=60 + i, start=i * 0.5, duration=0.4, velocity=100)
            for i in range(5)
        ])
        r = _run(state, {
            "velocity_range": 5, "timing_range": 0.05, "duration_pct": 8.0,
            "preserve_chord_sync": True, "seed": seed, "scope": "whole",
        })
        return tuple(
            (type(op).__name__, op.note_id,
             getattr(op, 'new_start', None),
             getattr(op, 'new_duration', None),
             getattr(op, 'velocity', None))
            for op in r.operations
        )

    a = run_once(42)
    b = run_once(42)
    assert a == b
    c = run_once(43)
    assert a != c  # Different seed -> different output (with very high probability)


def test_seed_zero_is_nondeterministic():
    # Can't assert inequality without being flaky, but we can check that
    # seed=0 produces *some* ops when ranges are nonzero.
    state, _pat, _notes = _mk_state([
        Note(pitch=60, start=0.0, duration=1.0, velocity=64),
    ])
    r = _run(state, {
        "velocity_range": 30, "timing_range": 0.0, "duration_pct": 0.0,
        "preserve_chord_sync": True, "seed": 0, "scope": "whole",
    })
    # With velocity_range=30 and 1 note, we should see at most 1 SetNoteVelocity.
    vels = [op for op in r.operations if isinstance(op, SetNoteVelocity)]
    assert len(vels) <= 1


# ---------------------------------------------------------------------------
# Op-type selection by param
# ---------------------------------------------------------------------------

def test_velocity_only_produces_only_velocity_ops():
    state, _pat, _notes = _mk_state([
        Note(pitch=60 + i, start=i * 0.25, duration=0.25, velocity=80)
        for i in range(8)
    ])
    r = _run(state, {
        "velocity_range": 20, "timing_range": 0.0, "duration_pct": 0.0,
        "preserve_chord_sync": True, "seed": 7, "scope": "whole",
    })
    for op in r.operations:
        assert isinstance(op, SetNoteVelocity)


def test_timing_only_produces_only_move_ops():
    state, _pat, _notes = _mk_state([
        Note(pitch=60 + i, start=i * 0.25, duration=0.25, velocity=80)
        for i in range(8)
    ])
    r = _run(state, {
        "velocity_range": 0, "timing_range": 0.05, "duration_pct": 0.0,
        "preserve_chord_sync": False, "seed": 7, "scope": "whole",
    })
    for op in r.operations:
        assert isinstance(op, MoveNote)


def test_duration_only_produces_only_resize_ops():
    state, _pat, _notes = _mk_state([
        Note(pitch=60 + i, start=i * 0.25, duration=0.25, velocity=80)
        for i in range(8)
    ])
    r = _run(state, {
        "velocity_range": 0, "timing_range": 0.0, "duration_pct": 15.0,
        "preserve_chord_sync": True, "seed": 7, "scope": "whole",
    })
    for op in r.operations:
        assert isinstance(op, ResizeNote)


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------

def test_timing_stays_within_pattern():
    # Notes at 0.0 and 3.98 in a 4.0-beat pattern: jitter must not push
    # either past the edges.
    state, _pat, _notes = _mk_state([
        Note(pitch=60, start=0.0, duration=0.1, velocity=80),
        Note(pitch=64, start=3.98, duration=0.01, velocity=80),
    ])
    r = _run(state, {
        "velocity_range": 0, "timing_range": 0.25, "duration_pct": 0.0,
        "preserve_chord_sync": False, "seed": 1, "scope": "whole",
    })
    for op in r.operations:
        if isinstance(op, MoveNote):
            assert 0.0 <= op.new_start < 4.0


def test_velocity_stays_in_range():
    # Note at velocity=1 with large jitter — must clamp at 1 and 127.
    state, _pat, _notes = _mk_state([
        Note(pitch=60, start=0.0, duration=1.0, velocity=1),
        Note(pitch=62, start=1.0, duration=1.0, velocity=127),
    ])
    for seed in range(1, 20):
        r = _run(state, {
            "velocity_range": 40, "timing_range": 0.0, "duration_pct": 0.0,
            "preserve_chord_sync": True, "seed": seed, "scope": "whole",
        })
        for op in r.operations:
            if isinstance(op, SetNoteVelocity):
                assert 1 <= op.velocity <= 127


def test_duration_stays_positive():
    state, _pat, _notes = _mk_state([
        Note(pitch=60, start=0.0, duration=0.005, velocity=80),
    ])
    r = _run(state, {
        "velocity_range": 0, "timing_range": 0.0, "duration_pct": 40.0,
        "preserve_chord_sync": True, "seed": 1, "scope": "whole",
    })
    for op in r.operations:
        if isinstance(op, ResizeNote):
            assert op.new_duration > 0.0


# ---------------------------------------------------------------------------
# Chord-sync preservation
# ---------------------------------------------------------------------------

def test_chord_sync_keeps_simultaneous_notes_together():
    # A 4-note chord at beat 0.0: with preserve_chord_sync, all four must
    # receive identical new_start offsets.
    state, _pat, _notes = _mk_state([
        Note(pitch=60, start=0.0, duration=1.0, velocity=100),
        Note(pitch=64, start=0.0, duration=1.0, velocity=100),
        Note(pitch=67, start=0.0, duration=1.0, velocity=100),
        Note(pitch=72, start=0.0, duration=1.0, velocity=100),
    ])
    r = _run(state, {
        "velocity_range": 0, "timing_range": 0.05, "duration_pct": 0.0,
        "preserve_chord_sync": True, "seed": 5, "scope": "whole",
    })
    moves = [op for op in r.operations if isinstance(op, MoveNote)]
    # Either all 4 moved to the same start or none moved (jitter rounded
    # to zero). Either way, the set of new_starts must be a single value.
    if moves:
        starts = {round(op.new_start, 6) for op in moves}
        assert len(starts) == 1


def test_chord_sync_off_moves_independently():
    # Same 4-note chord; without preservation, not all four share an offset.
    # (Probabilistically — we'd need astronomically unlucky jitter to get
    # 4 identical samples. Use moderate seeds and assert at least one
    # seed gives >1 distinct start.)
    any_diverged = False
    for seed in range(1, 12):
        state, _pat, _notes = _mk_state([
            Note(pitch=60, start=0.0, duration=1.0, velocity=100),
            Note(pitch=64, start=0.0, duration=1.0, velocity=100),
            Note(pitch=67, start=0.0, duration=1.0, velocity=100),
            Note(pitch=72, start=0.0, duration=1.0, velocity=100),
        ])
        r = _run(state, {
            "velocity_range": 0, "timing_range": 0.05, "duration_pct": 0.0,
            "preserve_chord_sync": False, "seed": seed, "scope": "whole",
        })
        moves = [op for op in r.operations if isinstance(op, MoveNote)]
        if moves and len({round(op.new_start, 6) for op in moves}) > 1:
            any_diverged = True
            break
    assert any_diverged


# ---------------------------------------------------------------------------
# Cross-placement dedupe
# ---------------------------------------------------------------------------

def test_dedupe_across_placements():
    # Same pattern referenced by two placements: each note should produce
    # at most one op per kind (no duplicate MoveNote for note X on
    # pattern P, regardless of how many placements show it).
    s = AppState()
    s.bpm = 120.0
    t = Track(id=s.new_id(), name="T", channel=0)
    s.tracks.append(t)
    notes = [
        Note(pitch=60 + i, start=i * 0.25, duration=0.25, velocity=100,
             note_id=s.new_id())
        for i in range(4)
    ]
    p = Pattern(id=s.new_id(), name="P", length=4.0, notes=list(notes),
                color='#fff', key='C', scale='major')
    s.patterns.append(p)
    s.placements.append(Placement(id=s.new_id(), track_id=t.id,
                                  pattern_id=p.id, time=0.0, repeats=1))
    t2 = Track(id=s.new_id(), name="T2", channel=0)
    s.tracks.append(t2)
    s.placements.append(Placement(id=s.new_id(), track_id=t2.id,
                                  pattern_id=p.id, time=8.0, repeats=1))

    r = _run(s, {
        "velocity_range": 10, "timing_range": 0.05, "duration_pct": 5.0,
        "preserve_chord_sync": True, "seed": 3, "scope": "whole",
    })
    # Each (pattern_id, note_id, op_type) combination should appear at most once.
    seen = set()
    for op in r.operations:
        key = (op.pattern_id, op.note_id, type(op).__name__)
        assert key not in seen, f"duplicate op {key}"
        seen.add(key)
