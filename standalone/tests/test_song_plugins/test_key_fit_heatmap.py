"""Tests for the Key Fit Heatmap plugin."""

from __future__ import annotations

import pytest

from standalone.state import AppState, Pattern, Note, Track, Placement
from standalone.song_plugins.song_view import SongView
from standalone.song_plugins.api import SelectionSnapshot
from standalone.song_plugins.builtin.key_fit_heatmap import (
    KeyFitHeatmapPlugin, _ROW_ORDER,
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


def test_row_order_has_24_unique_keys():
    # Every key appears exactly once.
    assert len(_ROW_ORDER) == 24
    assert len(set(_ROW_ORDER)) == 24
    # 12 majors + 12 minors.
    majs = [k for k in _ROW_ORDER if k[1] == "maj"]
    mins = [k for k in _ROW_ORDER if k[1] == "min"]
    assert len(majs) == 12 and len(mins) == 12


def test_row_order_starts_with_c_major_a_minor():
    # The interleaving starts at C major + A minor (relative minor pair).
    assert _ROW_ORDER[0] == (0, "maj")
    assert _ROW_ORDER[1] == (9, "min")


def test_empty_state_emits_empty_grid():
    s = AppState()
    s.bpm = 120.0
    view = SongView(s, _empty_selection())
    plugin = KeyFitHeatmapPlugin()
    result = plugin.run(view, {}, _DummyProgress())
    ann = result.annotation
    assert ann is not None
    # No notes → no chromas → no columns.
    assert ann.data["rows"] == 24
    assert ann.data["cols"] == 0
    assert ann.data["cells"] == []


def test_c_major_content_brightest_row_is_c_major():
    s = AppState()
    s.bpm = 120.0
    t = Track(id=s.new_id(), name="T", channel=0)
    s.tracks.append(t)
    # Long-sustained C major triad.
    notes = [
        Note(pitch=60, start=0.0, duration=4.0, velocity=100, note_id=s.new_id()),
        Note(pitch=64, start=0.0, duration=4.0, velocity=100, note_id=s.new_id()),
        Note(pitch=67, start=0.0, duration=4.0, velocity=100, note_id=s.new_id()),
    ]
    p = Pattern(id=s.new_id(), name="P", length=4.0, notes=notes,
                color='#fff', key='C', scale='major')
    s.patterns.append(p)
    s.placements.append(Placement(id=s.new_id(), track_id=t.id,
                                  pattern_id=p.id, time=0.0, repeats=1))
    view = SongView(s, _empty_selection())
    plugin = KeyFitHeatmapPlugin()
    result = plugin.run(view, {
        "window_beats": 2.0, "hop_beats": 2.0,
        "colormap": "magma", "clip_negative": True,
    }, _DummyProgress())
    ann = result.annotation
    assert ann.data["rows"] == 24
    assert ann.data["cols"] >= 1
    cells = ann.data["cells"]
    cols = ann.data["cols"]
    # Across all columns, the winning row for each should be either
    # C-major (row 0 of _ROW_ORDER) or A-minor (row 1). C-major triad
    # alone is ambiguous between these but shouldn't pick anything far away.
    for c in range(cols):
        best_row = max(range(24), key=lambda r: cells[r * cols + c])
        assert _ROW_ORDER[best_row] in ((0, "maj"), (9, "min"))


def test_clip_negative_pins_vmin_vmax():
    s = AppState()
    s.bpm = 120.0
    t = Track(id=s.new_id(), name="T", channel=0)
    s.tracks.append(t)
    notes = [Note(pitch=60, start=0.0, duration=1.0, velocity=100,
                  note_id=s.new_id())]
    p = Pattern(id=s.new_id(), name="P", length=1.0, notes=notes,
                color='#fff', key='C', scale='major')
    s.patterns.append(p)
    s.placements.append(Placement(id=s.new_id(), track_id=t.id,
                                  pattern_id=p.id, time=0.0, repeats=1))
    view = SongView(s, _empty_selection())
    plugin = KeyFitHeatmapPlugin()
    result = plugin.run(view, {"clip_negative": True}, _DummyProgress())
    hint = result.annotation.render_hint
    assert hint["vmin"] == 0.0
    assert hint["vmax"] == 1.0
    for v in result.annotation.data["cells"]:
        assert v >= 0.0


def test_row_labels_match_order():
    s = AppState()
    s.bpm = 120.0
    t = Track(id=s.new_id(), name="T", channel=0)
    s.tracks.append(t)
    notes = [Note(pitch=60, start=0.0, duration=2.0, velocity=100,
                  note_id=s.new_id())]
    p = Pattern(id=s.new_id(), name="P", length=2.0, notes=notes,
                color='#fff', key='C', scale='major')
    s.patterns.append(p)
    s.placements.append(Placement(id=s.new_id(), track_id=t.id,
                                  pattern_id=p.id, time=0.0, repeats=1))
    view = SongView(s, _empty_selection())
    plugin = KeyFitHeatmapPlugin()
    result = plugin.run(view, {}, _DummyProgress())
    labels = result.annotation.render_hint["row_labels"]
    assert len(labels) == 24
    # First two labels match the fixed ordering.
    assert labels[0].startswith("C ") and "maj" in labels[0]
    assert labels[1].startswith("A ") and "min" in labels[1]
