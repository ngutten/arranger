"""Tests for the Scale Conformance Hinter plugin."""

from __future__ import annotations

import pytest

from standalone.state import AppState, Pattern, Note, Track, Placement
from standalone.song_plugins.song_view import SongView
from standalone.song_plugins.api import SelectionSnapshot
from standalone.song_plugins.builtin.scale_conformance import (
    ScaleConformanceHinterPlugin,
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


def _mk_state(notes, key="C", scale="major"):
    s = AppState()
    s.bpm = 120.0
    t = Track(id=s.new_id(), name="T", channel=0)
    s.tracks.append(t)
    for n in notes:
        if n.note_id == 0:
            n.note_id = s.new_id()
    p = Pattern(id=s.new_id(), name="P", length=4.0, notes=list(notes),
                color='#fff', key=key, scale=scale)
    s.patterns.append(p)
    pl = Placement(id=s.new_id(), track_id=t.id, pattern_id=p.id,
                   time=0.0, repeats=1)
    s.placements.append(pl)
    return s, p


def _run(state):
    view = SongView(state, _empty_selection())
    plugin = ScaleConformanceHinterPlugin()
    return plugin.run(view, {}, _DummyProgress())


# ---------------------------------------------------------------------------
# Core behavior
# ---------------------------------------------------------------------------

def test_all_in_scale_emits_no_regions():
    # C major scale notes only.
    notes = [
        Note(pitch=60, start=0.0, duration=0.5, velocity=100),  # C
        Note(pitch=62, start=0.5, duration=0.5, velocity=100),  # D
        Note(pitch=64, start=1.0, duration=0.5, velocity=100),  # E
        Note(pitch=65, start=1.5, duration=0.5, velocity=100),  # F
        Note(pitch=67, start=2.0, duration=0.5, velocity=100),  # G
    ]
    s, _ = _mk_state(notes)
    result = _run(s)
    assert result.annotation.data == []


def test_one_offender_flagged():
    # C major scale + one Eb (out of scale).
    notes = [
        Note(pitch=60, start=0.0, duration=1.0, velocity=100),  # C (in)
        Note(pitch=63, start=1.0, duration=1.0, velocity=100),  # Eb (out)
        Note(pitch=67, start=2.0, duration=1.0, velocity=100),  # G (in)
    ]
    s, pat = _mk_state(notes)
    result = _run(s)
    regs = result.annotation.data
    assert len(regs) == 1
    r = regs[0]
    assert r["pattern_id"] == pat.id
    assert r["min_pitch"] == 63 and r["max_pitch"] == 63
    # Start is padded on both sides.
    assert r["start_beat"] < 1.0
    assert r["end_beat"] > 2.0


def test_regions_tight_to_each_note():
    # Two separate out-of-scale notes → two regions (not merged).
    notes = [
        Note(pitch=61, start=0.0, duration=0.5, velocity=100),  # C# (out)
        Note(pitch=66, start=2.0, duration=0.5, velocity=100),  # F# (out)
    ]
    s, _ = _mk_state(notes)
    result = _run(s)
    regs = result.annotation.data
    assert len(regs) == 2
    assert {r["min_pitch"] for r in regs} == {61, 66}


def test_no_placements_emits_nothing():
    s = AppState()
    s.bpm = 120.0
    t = Track(id=s.new_id(), name="T", channel=0)
    s.tracks.append(t)
    # Pattern with out-of-scale note but no placement.
    p = Pattern(id=s.new_id(), name="P", length=4.0, notes=[
        Note(pitch=61, start=0.0, duration=1.0, velocity=100,
             note_id=s.new_id()),
    ], color='#fff', key='C', scale='major')
    s.patterns.append(p)
    result = _run(s)
    assert result.annotation.data == []


def test_minor_key_flags_out_of_scale_notes():
    # A natural minor: A B C D E F G
    notes = [
        Note(pitch=69, start=0.0, duration=1.0, velocity=100),  # A (in)
        Note(pitch=70, start=1.0, duration=1.0, velocity=100),  # Bb (out)
    ]
    s, _ = _mk_state(notes, key="A", scale="minor")
    result = _run(s)
    regs = result.annotation.data
    assert len(regs) == 1
    assert regs[0]["min_pitch"] == 70


def test_note_ids_carried_through():
    n = Note(pitch=61, start=0.0, duration=1.0, velocity=100)
    s, _ = _mk_state([n])
    # note_id was assigned by _mk_state.
    assert n.note_id != 0
    result = _run(s)
    regs = result.annotation.data
    assert len(regs) == 1
    assert regs[0]["note_ids"] == (n.note_id,)


def test_schema_validator_accepts_output():
    from standalone.song_plugins.schemas import validate
    notes = [Note(pitch=61, start=0.0, duration=1.0, velocity=100)]
    s, _ = _mk_state(notes)
    result = _run(s)
    ok, why = validate("regions", result.annotation.data)
    assert ok, why
