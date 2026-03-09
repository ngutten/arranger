"""Tests for engine.py scheduling logic."""

import pytest
from standalone.core.engine import (
    build_schedule, compute_arrangement_length,
    EVT_NOTE_ON, EVT_NOTE_OFF, EVT_PROGRAM, EVT_VOLUME, EVT_NOTE_TUNE,
    SchedEvent,
)
from standalone.state import (
    AppState, Track, Pattern, Note, Placement,
    BeatPattern, BeatTrack, BeatPlacement, BeatInstrument,
)


class TestBuildSchedule:
    """Test schedule generation from AppState."""

    def test_empty_state(self, state):
        events = build_schedule(state)
        assert events == []

    def test_simple_notes(self, simple_state):
        events = build_schedule(simple_state)
        # Should have: 1 program + 1 volume (setup) + 2 note-on + 2 note-off
        setup = [e for e in events if e.beat < 0]
        notes_on = [e for e in events if e.event_type == EVT_NOTE_ON]
        notes_off = [e for e in events if e.event_type == EVT_NOTE_OFF]
        assert len(setup) == 2  # program + volume
        assert len(notes_on) == 2
        assert len(notes_off) == 2

    def test_schedule_sorted(self, simple_state):
        events = build_schedule(simple_state)
        beats = [e.beat for e in events]
        assert beats == sorted(beats)

    def test_note_off_before_note_on_at_same_beat(self):
        """At the same beat position, note-offs should come before note-ons."""
        s = AppState()
        t = Track(id=s.new_id(), name="T", channel=0)
        s.tracks.append(t)
        p = Pattern(
            id=s.new_id(), name="P", length=4.0,
            notes=[
                Note(pitch=60, start=0, duration=1, velocity=100),
                Note(pitch=64, start=1, duration=1, velocity=100),
            ],
            color="#000", key="C", scale="major",
        )
        s.patterns.append(p)
        s.placements.append(
            Placement(id=s.new_id(), track_id=t.id, pattern_id=p.id, time=0)
        )
        events = build_schedule(s)
        # At beat 1.0: note-off for pitch 60 should come before note-on for pitch 64
        at_beat_1 = [e for e in events if e.beat == 1.0]
        types = [e.event_type for e in at_beat_1]
        assert types.index(EVT_NOTE_OFF) < types.index(EVT_NOTE_ON)

    def test_bend_generates_note_tune_events(self):
        """Notes with bend data should generate EVT_NOTE_TUNE events."""
        s = AppState()
        t = Track(id=s.new_id(), name="T", channel=0)
        s.tracks.append(t)
        p = Pattern(
            id=s.new_id(), name="P", length=4.0,
            notes=[
                Note(pitch=60, start=0, duration=2, velocity=100,
                     bend=[[0.0, 0.0], [1.0, 2.0]]),
            ],
            color="#000", key="C", scale="major",
        )
        s.patterns.append(p)
        s.placements.append(
            Placement(id=s.new_id(), track_id=t.id, pattern_id=p.id, time=0)
        )
        events = build_schedule(s)
        tune_events = [e for e in events if e.event_type == EVT_NOTE_TUNE]
        assert len(tune_events) > 0
        # All tune events should reference pitch 60
        for e in tune_events:
            assert e.pitch == 60


class TestArrangementLength:
    def test_empty(self, state):
        assert compute_arrangement_length(state) == 0.0

    def test_single_placement(self, simple_state):
        length = compute_arrangement_length(simple_state)
        assert length == 4.0  # pattern length=4, repeats=1, time=0

    def test_repeated_placement(self):
        s = AppState()
        t = Track(id=s.new_id(), name="T", channel=0)
        s.tracks.append(t)
        p = Pattern(id=s.new_id(), name="P", length=4.0, notes=[], color="#000")
        s.patterns.append(p)
        s.placements.append(
            Placement(id=s.new_id(), track_id=t.id, pattern_id=p.id,
                      time=2.0, repeats=3)
        )
        assert compute_arrangement_length(s) == 2.0 + 4.0 * 3  # 14.0
