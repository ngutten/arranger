"""Fixtures for song_plugins tests.

Builds a small deterministic AppState covering melodic patterns, a
variation with a modification + deletion + addition, beat content, and
both a normal and a tempo automation track.
"""

from __future__ import annotations

import pytest

from standalone.state import (
    AppState, Track, Pattern, Note, Placement,
    BeatInstrument, BeatPattern, BeatTrack, BeatPlacement,
    AutomationTrack, AutomationPattern, AutomationPoint, AutomationPlacement,
    Variation, NoteDelta, AddedNote,
)
from standalone.undo import UndoStack
from standalone.song_plugins.api import SelectionSnapshot
from standalone.song_plugins.song_view import SongView


# ---------------------------------------------------------------------------
# Headless app shim (avoids importing Qt from app.py)
# ---------------------------------------------------------------------------

from contextlib import contextmanager
from standalone.undo import capture_state


class HeadlessApp:
    """Minimal app shim exposing state + undo_group + undo_stack.

    Mirrors the relevant parts of standalone.app.App without the Qt UI.
    """
    def __init__(self, state):
        self.state = state
        self.undo_stack = UndoStack(max_size=100)
        self._suppress_undo = False

    @contextmanager
    def undo_group(self, label: str):
        prev = self._suppress_undo
        self._suppress_undo = True
        try:
            yield
        finally:
            self._suppress_undo = prev
            if not prev:
                self.undo_stack.push(capture_state(self.state))


# ---------------------------------------------------------------------------
# Deterministic state fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def fixture_state():
    s = AppState()
    s.bpm = 120.0
    s.ts_num = 4
    s.ts_den = 4

    # -- Tracks (melodic) --
    t1 = Track(id=s.new_id(), name="Piano", channel=0, program=0, volume=100)
    t2 = Track(id=s.new_id(), name="Bass",  channel=1, program=33, volume=100)
    s.tracks.append(t1)
    s.tracks.append(t2)

    # -- Patterns --
    # Pattern 1 — 4 beats, 4 notes.
    p1_notes = [
        Note(pitch=60, start=0.0, duration=1.0, velocity=100, note_id=s.new_id()),
        Note(pitch=64, start=1.0, duration=1.0, velocity=90,  note_id=s.new_id()),
        Note(pitch=67, start=2.0, duration=1.0, velocity=90,  note_id=s.new_id()),
        Note(pitch=72, start=3.0, duration=1.0, velocity=100, note_id=s.new_id()),
    ]
    p1 = Pattern(id=s.new_id(), name="Melody A", length=4.0, notes=p1_notes,
                 color="#ff0000", key="C", scale="major")
    s.patterns.append(p1)

    # Pattern 2 — 2 beats, 5 notes.
    p2_notes = [
        Note(pitch=48 + (i % 7), start=i * 0.25, duration=0.25,
             velocity=80, note_id=s.new_id())
        for i in range(5)
    ]
    p2 = Pattern(id=s.new_id(), name="Bass A", length=2.0, notes=p2_notes,
                 color="#00ff00", key="C", scale="major")
    s.patterns.append(p2)

    # Pattern 3 — 8 beats, 12 notes.
    p3_notes = [
        Note(pitch=60 + (i * 2) % 12, start=i * 0.5, duration=0.4,
             velocity=70 + i * 2, note_id=s.new_id())
        for i in range(12)
    ]
    p3 = Pattern(id=s.new_id(), name="Melody B", length=8.0, notes=p3_notes,
                 color="#0000ff", key="C", scale="major")
    s.patterns.append(p3)

    # -- Variation on p1: modify note[0] pitch, delete note[1], add one note --
    var = Variation(
        id=s.new_id(), name="Var A", parent_id=p1.id, color="#ffaa00",
        modifications=[
            NoteDelta(note_id=p1_notes[0].note_id, d_pitch=2),  # 60 -> 62
        ],
        deletions=[p1_notes[1].note_id],
        additions=[
            AddedNote(
                note_id=s.new_id(),
                pitch=76, start=2.5, duration=0.5,
                velocity=110,
            ),
        ],
        splits=[],
    )
    s.variations.append(var)

    # -- Placements --
    # Regular placement of p1 on t1 at t=0, repeats=2 (covers beats 0-8).
    pl1 = Placement(id=s.new_id(), track_id=t1.id, pattern_id=p1.id,
                    time=0.0, repeats=2)
    s.placements.append(pl1)
    # Variation placement on t1 at t=8 (beats 8-12).
    pl_var = Placement(id=s.new_id(), track_id=t1.id, pattern_id=var.id,
                       time=8.0, repeats=1, is_variation=True)
    s.placements.append(pl_var)
    # Regular placement of p2 on t2 at t=0, repeats=4 (covers 0-8).
    pl2 = Placement(id=s.new_id(), track_id=t2.id, pattern_id=p2.id,
                    time=0.0, repeats=4)
    s.placements.append(pl2)
    # Regular placement of p3 on t2 at t=8, repeats=1 (covers 8-16).
    pl3 = Placement(id=s.new_id(), track_id=t2.id, pattern_id=p3.id,
                    time=8.0, repeats=1)
    s.placements.append(pl3)

    # -- Beat kit / pattern / track / placement --
    inst_kick = BeatInstrument(id=s.new_id(), name="Kick", channel=9, pitch=36)
    inst_snare = BeatInstrument(id=s.new_id(), name="Snare", channel=9, pitch=38)
    s.beat_kit.append(inst_kick)
    s.beat_kit.append(inst_snare)

    bpat = BeatPattern(
        id=s.new_id(), name="Beat 1", length=1.0, subdivision=4,
        color="#888888",
        grid={
            inst_kick.id:  [100, 0, 0, 0],  # kick on 1
            inst_snare.id: [0, 0, 100, 0],  # snare on 3
        },
    )
    s.beat_patterns.append(bpat)

    btrack = BeatTrack(id=s.new_id(), name="Drums")
    s.beat_tracks.append(btrack)

    bpl = BeatPlacement(id=s.new_id(), track_id=btrack.id,
                        pattern_id=bpat.id, time=0.0, repeats=2)
    s.beat_placements.append(bpl)

    # -- Normal automation (not a tempo track) --
    at_mod = AutomationTrack(id=s.new_id(), name="ModWheel")
    s.automation_tracks.append(at_mod)
    apat = AutomationPattern(
        id=s.new_id(), name="Sweep", length=4.0,
        points=[
            AutomationPoint(time=0.0, value=0.0, curve='linear'),
            AutomationPoint(time=4.0, value=1.0, curve='linear'),
        ],
        min_value=0.0, max_value=1.0,
    )
    s.automation_patterns.append(apat)
    apl = AutomationPlacement(id=s.new_id(), track_id=at_mod.id,
                              pattern_id=apat.id, time=0.0, repeats=1)
    s.automation_placements.append(apl)

    # -- Tempo automation track --
    at_tempo = AutomationTrack(id=s.new_id(), name="Tempo", target="tempo")
    s.automation_tracks.append(at_tempo)
    tpat = AutomationPattern(
        id=s.new_id(), name="Tempo curve", length=4.0,
        points=[
            AutomationPoint(time=0.0, value=120.0, curve='linear'),
            AutomationPoint(time=4.0, value=150.0, curve='linear'),
        ],
        min_value=60.0, max_value=200.0,
    )
    s.automation_patterns.append(tpat)
    tpl = AutomationPlacement(id=s.new_id(), track_id=at_tempo.id,
                              pattern_id=tpat.id, time=0.0, repeats=1)
    s.automation_placements.append(tpl)

    # Attach named handles for tests.
    s._test_refs = {
        't1': t1, 't2': t2,
        'p1': p1, 'p2': p2, 'p3': p3,
        'pl1': pl1, 'pl_var': pl_var, 'pl2': pl2, 'pl3': pl3,
        'var': var,
        'inst_kick': inst_kick, 'inst_snare': inst_snare,
        'bpat': bpat, 'btrack': btrack, 'bpl': bpl,
        'at_mod': at_mod, 'apat': apat, 'apl': apl,
        'at_tempo': at_tempo, 'tpat': tpat, 'tpl': tpl,
    }
    return s


@pytest.fixture
def empty_selection():
    return SelectionSnapshot(
        notes=frozenset(), placements=frozenset(), primary='none',
        current_pattern_id=None, current_variation_id=None,
        current_beat_pattern_id=None, current_auto_pattern_id=None,
    )


@pytest.fixture
def view(fixture_state, empty_selection):
    return SongView(fixture_state, empty_selection)


@pytest.fixture
def app(fixture_state):
    return HeadlessApp(fixture_state)
