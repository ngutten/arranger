"""Reference plugin: builtin.note_density happy path."""

from __future__ import annotations

import pytest

from standalone.state import AppState, Track, Pattern, Note, Placement
from standalone.song_plugins.api import SelectionSnapshot
from standalone.song_plugins.song_view import SongView
from standalone.song_plugins.builtin.note_density import NoteDensityPlugin


class _Progress:
    def phase(self, name): pass
    def update(self, fraction, message=None): pass
    @property
    def cancelled(self): return False


def _simple_state_with_notes_at(beats):
    s = AppState()
    t = Track(id=s.new_id(), name="T", channel=0)
    s.tracks.append(t)
    notes = [
        Note(pitch=60, start=b, duration=0.1, velocity=100,
             note_id=s.new_id())
        for b in beats
    ]
    p = Pattern(id=s.new_id(), name="P",
                length=max(beats) + 1 if beats else 1.0,
                notes=notes, color="#fff")
    s.patterns.append(p)
    pl = Placement(id=s.new_id(), track_id=t.id, pattern_id=p.id,
                   time=0.0, repeats=1)
    s.placements.append(pl)
    return s


def test_note_density_counts_bins():
    # Notes at 0, 1, 1.5, 4, 4.5, 4.75 — windows of size 2.
    # Bin 0 [0..2): 3 notes → 1.5 notes/beat
    # Bin 1 [2..4): 0 notes → 0
    # Bin 2 [4..6): 3 notes → 1.5 notes/beat
    state = _simple_state_with_notes_at([0.0, 1.0, 1.5, 4.0, 4.5, 4.75])
    sel = SelectionSnapshot(
        notes=frozenset(), placements=frozenset(), primary='none',
        current_pattern_id=None, current_variation_id=None,
        current_beat_pattern_id=None, current_auto_pattern_id=None,
    )
    view = SongView(state, sel)
    plugin = NoteDensityPlugin()
    result = plugin.run(view, {"window_beats": 2.0, "smoothing": "none",
                               "scope": "whole"}, _Progress())
    ann = result.annotation
    assert ann is not None
    assert ann.schema == "scalar_curve"
    values = ann.data["values"]
    beats = ann.data["beats"]
    assert len(values) == len(beats)
    # Expect bins 0, 1, 2 to be present.
    assert len(values) >= 3
    assert values[0] == pytest.approx(1.5)
    assert values[1] == pytest.approx(0.0)
    assert values[2] == pytest.approx(1.5)
