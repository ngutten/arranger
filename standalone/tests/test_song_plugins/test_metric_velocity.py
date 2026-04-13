"""Tests for the Metric Velocity Pattern plugin."""

from __future__ import annotations

import pytest

from standalone.state import AppState, Pattern, Note, Track, Placement
from standalone.song_plugins.song_view import SongView
from standalone.song_plugins.api import SelectionSnapshot
from standalone.song_plugins.builtin.metric_velocity import (
    MetricVelocityPlugin, _parse_weights,
)


class _DummyProgress:
    def __init__(self):
        self.cancelled = False
        self.phases = []

    def phase(self, name):
        self.phases.append(name)

    def update(self, fraction, message=None):
        pass


def _empty_selection():
    return SelectionSnapshot(
        notes=frozenset(), placements=frozenset(), primary='none',
        current_pattern_id=None, current_variation_id=None,
        current_beat_pattern_id=None, current_auto_pattern_id=None,
    )


def _mk_state_with_four_notes():
    s = AppState()
    s.bpm = 120.0
    t = Track(id=s.new_id(), name="T1", channel=0)
    s.tracks.append(t)
    notes = [
        Note(pitch=60, start=0.0, duration=0.25, velocity=80, note_id=s.new_id()),
        Note(pitch=62, start=0.25, duration=0.25, velocity=80, note_id=s.new_id()),
        Note(pitch=64, start=0.5, duration=0.25, velocity=80, note_id=s.new_id()),
        Note(pitch=65, start=0.75, duration=0.25, velocity=80, note_id=s.new_id()),
    ]
    p = Pattern(id=s.new_id(), name="P", length=1.0, notes=notes,
                color='#fff', key='C', scale='major')
    s.patterns.append(p)
    pl = Placement(id=s.new_id(), track_id=t.id, pattern_id=p.id,
                   time=0.0, repeats=1)
    s.placements.append(pl)
    return s, p, notes


# ---------------------------------------------------------------------------

def test_parse_weights_good():
    assert _parse_weights("5 1 3 1") == [5.0, 1.0, 3.0, 1.0]
    assert _parse_weights("  2.5   0.5 ") == [2.5, 0.5]


def test_parse_weights_raises_on_garbage():
    with pytest.raises(ValueError):
        _parse_weights("foo bar")
    with pytest.raises(ValueError):
        _parse_weights("")


def test_absolute_mode_velocities_follow_weights():
    state, pat, notes = _mk_state_with_four_notes()
    view = SongView(state, _empty_selection())
    plugin = MetricVelocityPlugin()
    result = plugin.run(view, {
        "subdivision": "4", "pattern": "5 1 3 1",
        "mode": "absolute", "scope": "whole",
    }, _DummyProgress())

    ops = result.operations
    assert ops is not None
    by_id = {op.note_id: op.velocity for op in ops}
    # With subdivision=4, weights=[5,1,3,1], note positions 0..3 land in pattern
    # indices 0..3. Absolute: weight/5 * 127 -> [127, 25, 76, 25].
    expected = [127, 25, 76, 25]
    for n, want in zip(notes, expected):
        if n.velocity == want:
            # no-op ops are skipped
            assert n.note_id not in by_id
        else:
            assert by_id[n.note_id] == want


def test_relative_mode_scales_original():
    state, pat, notes = _mk_state_with_four_notes()
    view = SongView(state, _empty_selection())
    plugin = MetricVelocityPlugin()
    # Pattern "2 0.5" with avg = 1.25, so a weight of 0.5 with original 80
    # gives 80 * 0.5 / 1.25 = 32. Weight of 2 gives 80 * 2 / 1.25 = 128 -> clamp to 127.
    result = plugin.run(view, {
        "subdivision": "4", "pattern": "2 0.5",
        "mode": "relative", "scope": "whole",
    }, _DummyProgress())
    by_id = {op.note_id: op.velocity for op in result.operations}
    # positions 0,1,2,3 -> cycle indices 0,1,0,1 -> weights 2,0.5,2,0.5
    assert by_id[notes[0].note_id] == 127  # clamped
    assert by_id[notes[1].note_id] == 32
    assert by_id[notes[2].note_id] == 127
    assert by_id[notes[3].note_id] == 32


def test_relative_half_ratio():
    # Weight pattern "1 1" has avg=1, so ratio 1.0 -> no change when original=80.
    # Use "0.5 0.5" to get ratio 1.0 also (no change). Instead use "1 0" where
    # avg=0.5 and weight=0.0 -> new_vel=0 -> clamped to 1.
    state, pat, notes = _mk_state_with_four_notes()
    view = SongView(state, _empty_selection())
    plugin = MetricVelocityPlugin()
    # Construct a pattern whose weight ratio is exactly 0.5:
    # pattern "1 2" -> avg 1.5, weight at pos0 = 1 -> 80 * 1/1.5 ~= 53.3 -> 53
    # pattern "1 1" -> avg 1, ratio 1 -> no change; avoid.
    # Use "0.5 1.5" -> avg 1.0; weight 0.5 -> 40, weight 1.5 -> 120.
    result = plugin.run(view, {
        "subdivision": "4", "pattern": "0.5 1.5",
        "mode": "relative", "scope": "whole",
    }, _DummyProgress())
    by_id = {op.note_id: op.velocity for op in result.operations}
    assert by_id[notes[0].note_id] == 40
    assert by_id[notes[1].note_id] == 120


def test_bad_pattern_raises_in_run():
    state, pat, notes = _mk_state_with_four_notes()
    view = SongView(state, _empty_selection())
    plugin = MetricVelocityPlugin()
    with pytest.raises(ValueError):
        plugin.run(view, {
            "subdivision": "4", "pattern": "foo bar",
            "mode": "absolute", "scope": "whole",
        }, _DummyProgress())


def test_variations_skipped(fixture_state):
    """Variation placements yield source_kind=='variation' notes that are skipped."""
    view = SongView(fixture_state, _empty_selection())
    plugin = MetricVelocityPlugin()
    result = plugin.run(view, {
        "subdivision": "4", "pattern": "5 1 3 1",
        "mode": "absolute", "scope": "whole",
    }, _DummyProgress())

    var_id = fixture_state._test_refs['var'].id
    # No op should target the variation id.
    for op in result.operations:
        assert op.pattern_id != var_id


def test_note_id_zero_skipped():
    """Legacy notes with note_id==0 must not be touched."""
    s = AppState()
    t = Track(id=s.new_id(), name="T")
    s.tracks.append(t)
    n0 = Note(pitch=60, start=0.0, duration=0.5, velocity=80, note_id=0)
    n1 = Note(pitch=62, start=0.5, duration=0.5, velocity=80, note_id=s.new_id())
    p = Pattern(id=s.new_id(), name="P", length=1.0, notes=[n0, n1],
                color='#fff', key='C', scale='major')
    s.patterns.append(p)
    pl = Placement(id=s.new_id(), track_id=t.id, pattern_id=p.id,
                   time=0.0, repeats=1)
    s.placements.append(pl)

    view = SongView(s, _empty_selection())
    plugin = MetricVelocityPlugin()
    result = plugin.run(view, {
        "subdivision": "4", "pattern": "5 1 3 1",
        "mode": "absolute", "scope": "whole",
    }, _DummyProgress())
    for op in result.operations:
        assert op.note_id != 0
