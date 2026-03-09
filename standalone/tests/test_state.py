"""Tests for state serialization and lookup."""

import json
import pytest
from standalone.state import (
    AppState, Track, Pattern, Note, Placement,
    BeatPattern, BeatInstrument,
)


class TestStateSerialization:
    """Round-trip JSON serialization of AppState."""

    def test_empty_state_roundtrip(self, state):
        text = state.to_json()
        s2 = AppState()
        s2.load_json(text)
        assert s2.bpm == state.bpm
        assert len(s2.tracks) == 0
        assert len(s2.patterns) == 0

    def test_simple_state_roundtrip(self, simple_state):
        text = simple_state.to_json()
        s2 = AppState()
        s2.load_json(text)
        assert s2.bpm == 120
        assert len(s2.tracks) == 1
        assert len(s2.patterns) == 1
        assert len(s2.placements) == 1
        assert s2.tracks[0].name == "Piano"
        assert len(s2.patterns[0].notes) == 2

    def test_note_bend_roundtrip(self, state):
        t = Track(id=state.new_id(), name="T", channel=0)
        state.tracks.append(t)
        bend_data = [[0.0, 1.5], [0.5, -0.5]]
        p = Pattern(
            id=state.new_id(), name="P", length=4.0,
            notes=[Note(pitch=60, start=0, duration=1, velocity=100, bend=bend_data)],
            color="#000", key="C", scale="major",
        )
        state.patterns.append(p)
        text = state.to_json()
        s2 = AppState()
        s2.load_json(text)
        assert s2.patterns[0].notes[0].bend == bend_data


class TestStateLookups:
    """O(1) lookup methods on AppState."""

    def test_find_track(self, simple_state):
        t = simple_state.tracks[0]
        assert simple_state.find_track(t.id) is t
        assert simple_state.find_track(99999) is None

    def test_find_pattern(self, simple_state):
        p = simple_state.patterns[0]
        assert simple_state.find_pattern(p.id) is p
        assert simple_state.find_pattern(99999) is None

    def test_find_placement(self, simple_state):
        pl = simple_state.placements[0]
        assert simple_state.find_placement(pl.id) is pl


class TestNoteDataclass:
    def test_note_defaults(self):
        n = Note(pitch=60, start=0, duration=1)
        assert n.velocity == 100
        assert n.bend == []
        assert n.lyric == ''

    def test_note_dict_roundtrip(self):
        n = Note(pitch=72, start=2.5, duration=0.5, velocity=64, bend=[[0, 1]])
        d = n.to_dict()
        n2 = Note.from_dict(d)
        assert n2.pitch == 72
        assert n2.velocity == 64
        assert n2.bend == [[0, 1]]
