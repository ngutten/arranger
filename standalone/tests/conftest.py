"""Shared fixtures for arranger standalone tests."""

import pytest
from standalone.state import (
    AppState, Track, Pattern, Note, Placement,
    BeatPattern, BeatTrack, BeatPlacement, BeatInstrument,
)


@pytest.fixture
def state():
    """A fresh AppState with no tracks/patterns."""
    return AppState()


@pytest.fixture
def simple_state():
    """An AppState with one track, one pattern (2 notes), and one placement."""
    s = AppState()
    s.bpm = 120

    t = Track(id=s.new_id(), name="Piano", channel=0, bank=0, program=0, volume=100)
    s.tracks.append(t)

    p = Pattern(
        id=s.new_id(), name="Pat1", length=4.0,
        notes=[
            Note(pitch=60, start=0.0, duration=1.0, velocity=100),
            Note(pitch=64, start=1.0, duration=1.0, velocity=80),
        ],
        color="#ff0000", key="C", scale="major",
    )
    s.patterns.append(p)

    pl = Placement(id=s.new_id(), track_id=t.id, pattern_id=p.id, time=0.0, repeats=1)
    s.placements.append(pl)

    return s
