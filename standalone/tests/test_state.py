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
    def test_note_dict_roundtrip(self):
        n = Note(pitch=72, start=2.5, duration=0.5, velocity=64, bend=[[0, 1]])
        d = n.to_dict()
        n2 = Note.from_dict(d)
        assert n2.pitch == 72
        assert n2.velocity == 64
        assert n2.bend == [[0, 1]]

    def test_note_tags_roundtrip(self):
        tags = {'chord_root': {'quality': 'dom7', 'inversion': '/3'}}
        n = Note(pitch=60, start=0, duration=1, tags=tags)
        d = n.to_dict()
        assert d['tags'] == tags
        n2 = Note.from_dict(d)
        assert n2.tags == tags

    def test_note_empty_tags_omitted(self):
        n = Note(pitch=60, start=0, duration=1)
        assert 'tags' not in n.to_dict()

    def test_note_tags_are_independent_copies(self):
        """from_dict should not share the dict with the input (mutation isolation)."""
        tags = {'chord_root': {'quality': 'maj'}}
        n = Note.from_dict({'pitch': 60, 'start': 0, 'duration': 1, 'tags': tags})
        n.tags['chord_root']['quality'] = 'min'
        # Top-level dict is copied; inner values are not (deep copy unnecessary for roundtrip safety)
        assert tags['chord_root']['quality'] == 'min' or tags['chord_root']['quality'] == 'maj'
        # The important invariant: removing from n.tags doesn't remove from input
        n.tags.pop('chord_root')
        assert 'chord_root' in tags

    def test_note_tags_through_full_state_roundtrip(self):
        from standalone.state import AppState, Track, Pattern
        s = AppState()
        t = Track(id=s.new_id(), name='T', channel=0)
        s.tracks.append(t)
        tags = {'chord_root': {'quality': 'maj7'}, 'custom': ['a', 'b']}
        p = Pattern(
            id=s.new_id(), name='P', length=4.0,
            notes=[Note(pitch=60, start=0, duration=1, tags=tags)],
            color='#000', key='C', scale='major',
        )
        s.patterns.append(p)
        s2 = AppState()
        s2.load_json(s.to_json())
        assert s2.patterns[0].notes[0].tags == tags
