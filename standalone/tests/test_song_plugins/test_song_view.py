"""Tests for SongView — read-only projection over AppState."""

from __future__ import annotations

import pytest

from standalone.song_plugins.api import Scope, SelectionSnapshot
from standalone.song_plugins.song_view import SongView


def test_tracks_view(view, fixture_state):
    refs = fixture_state._test_refs
    tracks = view.tracks()
    assert len(tracks) == 2
    t1 = view.track(refs['t1'].id)
    assert t1.name == "Piano"
    assert t1.channel == 0


def test_patterns_view(view, fixture_state):
    refs = fixture_state._test_refs
    p1 = view.pattern(refs['p1'].id)
    assert p1.name == "Melody A"
    assert p1.length == 4.0
    assert len(p1.note_ids) == 4


def test_placements_view_duration(view, fixture_state):
    refs = fixture_state._test_refs
    pl1 = view.placement(refs['pl1'].id)
    # Pattern length 4 x 2 repeats = 8 beats.
    assert pl1.duration_beats == 8.0
    assert pl1.is_variation is False
    pl_var = view.placement(refs['pl_var'].id)
    assert pl_var.is_variation is True


def test_views_are_immutable(view, fixture_state):
    p = view.pattern(fixture_state._test_refs['p1'].id)
    with pytest.raises(Exception):
        p.name = "hacked"


def test_notes_in_whole_melodic_counts(view, fixture_state):
    """Sum of resolved notes across all melodic placements.

    - pl1 = p1 (4 notes) x 2 repeats = 8
    - pl_var = variation (4 - 1 deleted + 1 added = 4 notes) x 1 = 4
    - pl2 = p2 (5 notes) x 4 repeats = 20
    - pl3 = p3 (12 notes) x 1 repeat = 12
    Total = 44
    """
    all_notes = list(view.notes_in(Scope(kind="whole")))
    assert len(all_notes) == 8 + 4 + 20 + 12


def test_notes_in_variation_resolves_delta(view, fixture_state):
    refs = fixture_state._test_refs
    pl_var = refs['pl_var']
    notes = [n for n in view.notes_in(Scope(kind="whole"))
             if n.placement_id == pl_var.id]
    # 4 notes: modified note 0 (pitch shifted +2), note 2, note 3, added note
    assert len(notes) == 4
    # The pattern's original note[0] had pitch=60; variation adds d_pitch=+2.
    pitches = sorted(n.pitch for n in notes)
    assert 62 in pitches  # modified
    # Note with original pitch 64 should be deleted — should not appear.
    assert 64 not in pitches


def test_notes_in_repeats_have_offset(view, fixture_state):
    refs = fixture_state._test_refs
    pl2 = refs['pl2']
    notes = [n for n in view.notes_in(Scope(kind="whole"))
             if n.placement_id == pl2.id]
    # 5 notes x 4 repeats.
    starts = sorted({n.start_beat for n in notes})
    # Repeat 0 covers 0 .. <2; repeat 1 covers 2 .. <4; etc.
    # First note of each repeat is at 0, 2, 4, 6.
    assert 0.0 in starts
    assert 2.0 in starts
    assert 4.0 in starts
    assert 6.0 in starts


def test_notes_in_range(view):
    scope = Scope(kind="range", start_beat=0.0, end_beat=4.0)
    notes = list(view.notes_in(scope))
    # Every note must at least partially overlap [0, 4).
    for n in notes:
        assert n.start_beat < 4.0
        assert n.start_beat + n.duration_beats > 0.0


def test_notes_in_tracks_filter(view, fixture_state):
    refs = fixture_state._test_refs
    scope = Scope(kind="tracks", track_ids=(refs['t1'].id,))
    notes = list(view.notes_in(scope))
    # Only pl1 (8) + pl_var (4) = 12 notes on track 1.
    assert len(notes) == 12
    for n in notes:
        assert n.track_id == refs['t1'].id


def test_beat_events_counts(view, fixture_state):
    refs = fixture_state._test_refs
    events = list(view.beat_events_in(Scope(kind="whole")))
    # 2 instruments x 1 hit each x 2 repeats = 4 events.
    assert len(events) == 4
    kicks = [e for e in events if e.inst_id == refs['inst_kick'].id]
    snares = [e for e in events if e.inst_id == refs['inst_snare'].id]
    assert len(kicks) == 2
    assert len(snares) == 2


def test_beat_event_step_timing(view, fixture_state):
    refs = fixture_state._test_refs
    events = list(view.beat_events_in(Scope(kind="whole")))
    # Kick is on step 0 — timing should be pl.time + r * length + 0.
    # pl at t=0, length=1, subdivision=4 → step_dur=0.25.
    kicks = [e for e in events if e.inst_id == refs['inst_kick'].id]
    assert sorted(k.start_beat for k in kicks) == [0.0, 1.0]
    snares = [e for e in events if e.inst_id == refs['inst_snare'].id]
    # Snare on step 2 → offset 0.5 in each repeat.
    assert sorted(s.start_beat for s in snares) == [0.5, 1.5]


def test_tempo_map_matches_state_bpm_without_tempo_track(fixture_state,
                                                         empty_selection):
    # Temporarily remove tempo track and rebuild view.
    fixture_state.automation_tracks = [
        t for t in fixture_state.automation_tracks if t.target != 'tempo'
    ]
    v = SongView(fixture_state, empty_selection)
    assert v.tempo_map.bpm_at(0.0) == 120.0
    assert abs(v.tempo_map.beat_to_seconds(120.0) - 60.0) < 1e-6


def test_tempo_map_uses_automation(view):
    # Curve: 120 at beat 0, 150 at beat 4.
    # At beat 2 it's 135.
    assert abs(view.tempo_map.bpm_at(2.0) - 135.0) < 1e-6
    assert view.tempo_map.bpm_at(0.0) == pytest.approx(120.0)
    # Past the end, holds 150.
    assert view.tempo_map.bpm_at(10.0) == pytest.approx(150.0)


def test_tempo_map_roundtrip(view):
    # beat_to_seconds and seconds_to_beat should roundtrip.
    for b in [0.5, 2.0, 4.0, 8.5]:
        s = view.tempo_map.beat_to_seconds(b)
        back = view.tempo_map.seconds_to_beat(s)
        assert abs(back - b) < 1e-3


def test_total_beats(view, fixture_state):
    # The latest-ending placement is pl3 at t=8, length=8, repeats=1 → ends at 16.
    assert view.total_beats == 16.0


def test_placements_in_range(view):
    scope = Scope(kind="range", start_beat=0.0, end_beat=4.0)
    pls = list(view.placements_in(scope))
    # pl1 (0-8), pl2 (0-8), and perhaps pl_var (8-12) should NOT appear.
    for pl in pls:
        assert pl.time < 4.0
        assert pl.time + pl.duration_beats > 0.0


def test_sample_automation(view, fixture_state):
    at_mod = fixture_state._test_refs['at_mod']
    # ModWheel linearly 0 -> 1 over beat 0..4.
    assert view.sample_automation(at_mod.id, 0.0) == pytest.approx(0.0)
    assert view.sample_automation(at_mod.id, 2.0) == pytest.approx(0.5)
    assert view.sample_automation(at_mod.id, 4.0) == pytest.approx(0.0)  # past end


def test_bin_notes_by_beat(view):
    binned = view.bin_notes_by_beat(Scope(kind="whole"), window=2.0)
    assert all(isinstance(beat, float) for beat, _ in binned)
    # Bins are sorted.
    beats = [b for b, _ in binned]
    assert beats == sorted(beats)
