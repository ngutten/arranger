"""Validation tests: bad ops must raise before any mutation occurs."""

from __future__ import annotations

import copy
import json
import pytest

from standalone.song_plugins import ops as O
from standalone.song_plugins.apply_ops import apply_ops, OperationError


def _snapshot(state):
    return json.loads(state.to_json())


def _assert_unchanged(state, snap_before):
    assert _snapshot(state) == snap_before


# ---------------------------------------------------------------------------

def test_add_note_bad_pattern(app, fixture_state):
    snap = _snapshot(fixture_state)
    with pytest.raises(OperationError):
        apply_ops(
            [O.AddNote(pattern_id=99999, pitch=60, start=0.0,
                       duration=1.0, velocity=100)],
            app, "x"
        )
    _assert_unchanged(fixture_state, snap)


def test_add_note_bad_pitch(app, fixture_state):
    snap = _snapshot(fixture_state)
    p = fixture_state._test_refs['p1']
    with pytest.raises(OperationError):
        apply_ops(
            [O.AddNote(pattern_id=p.id, pitch=200, start=0.0,
                       duration=1.0, velocity=100)],
            app, "x"
        )
    _assert_unchanged(fixture_state, snap)


def test_add_note_negative_duration(app, fixture_state):
    snap = _snapshot(fixture_state)
    p = fixture_state._test_refs['p1']
    with pytest.raises(OperationError):
        apply_ops(
            [O.AddNote(pattern_id=p.id, pitch=60, start=0.0,
                       duration=-1.0, velocity=100)],
            app, "x"
        )
    _assert_unchanged(fixture_state, snap)


def test_add_note_bad_velocity(app, fixture_state):
    snap = _snapshot(fixture_state)
    p = fixture_state._test_refs['p1']
    with pytest.raises(OperationError):
        apply_ops(
            [O.AddNote(pattern_id=p.id, pitch=60, start=0.0,
                       duration=1.0, velocity=200)],
            app, "x"
        )
    _assert_unchanged(fixture_state, snap)


def test_move_note_bad_note_id(app, fixture_state):
    snap = _snapshot(fixture_state)
    p = fixture_state._test_refs['p1']
    with pytest.raises(OperationError):
        apply_ops(
            [O.MoveNote(pattern_id=p.id, note_id=99999,
                        new_start=0.0, new_pitch=60)],
            app, "x"
        )
    _assert_unchanged(fixture_state, snap)


def test_resize_note_zero(app, fixture_state):
    snap = _snapshot(fixture_state)
    p = fixture_state._test_refs['p1']
    with pytest.raises(OperationError):
        apply_ops(
            [O.ResizeNote(pattern_id=p.id, note_id=p.notes[0].note_id,
                          new_duration=0.0)],
            app, "x"
        )
    _assert_unchanged(fixture_state, snap)


def test_delete_note_bad(app, fixture_state):
    snap = _snapshot(fixture_state)
    p = fixture_state._test_refs['p1']
    with pytest.raises(OperationError):
        apply_ops([O.DeleteNote(pattern_id=p.id, note_id=99999)], app, "x")
    _assert_unchanged(fixture_state, snap)


def test_set_note_velocity_out_of_range(app, fixture_state):
    snap = _snapshot(fixture_state)
    p = fixture_state._test_refs['p1']
    with pytest.raises(OperationError):
        apply_ops(
            [O.SetNoteVelocity(pattern_id=p.id,
                               note_id=p.notes[0].note_id, velocity=200)],
            app, "x"
        )
    _assert_unchanged(fixture_state, snap)


def test_set_note_bend_out_of_range(app, fixture_state):
    snap = _snapshot(fixture_state)
    p = fixture_state._test_refs['p1']
    with pytest.raises(OperationError):
        apply_ops(
            [O.SetNoteBend(pattern_id=p.id,
                           note_id=p.notes[0].note_id,
                           bend=((0.0, 5.0),))],  # 5.0 out of [-2,2]
            app, "x"
        )
    _assert_unchanged(fixture_state, snap)


def test_split_note_bad_offset(app, fixture_state):
    snap = _snapshot(fixture_state)
    p = fixture_state._test_refs['p1']
    with pytest.raises(OperationError):
        apply_ops(
            [O.SplitNote(pattern_id=p.id, note_id=p.notes[0].note_id,
                         split_offset=99.0)],
            app, "x"
        )
    _assert_unchanged(fixture_state, snap)


def test_create_pattern_zero_length(app, fixture_state):
    snap = _snapshot(fixture_state)
    with pytest.raises(OperationError):
        apply_ops([O.CreatePattern(name="P", length=0.0)], app, "x")
    _assert_unchanged(fixture_state, snap)


def test_rename_pattern_bad(app, fixture_state):
    snap = _snapshot(fixture_state)
    with pytest.raises(OperationError):
        apply_ops([O.RenamePattern(pattern_id=99999, name="X")], app, "x")
    _assert_unchanged(fixture_state, snap)


def test_resize_pattern_bad(app, fixture_state):
    snap = _snapshot(fixture_state)
    p = fixture_state._test_refs['p1']
    with pytest.raises(OperationError):
        apply_ops([O.ResizePattern(pattern_id=p.id, new_length=-1.0)],
                   app, "x")
    _assert_unchanged(fixture_state, snap)


def test_delete_pattern_bad(app, fixture_state):
    snap = _snapshot(fixture_state)
    with pytest.raises(OperationError):
        apply_ops([O.DeletePattern(pattern_id=99999)], app, "x")
    _assert_unchanged(fixture_state, snap)


def test_duplicate_pattern_bad(app, fixture_state):
    snap = _snapshot(fixture_state)
    with pytest.raises(OperationError):
        apply_ops([O.DuplicatePattern(pattern_id=99999)], app, "x")
    _assert_unchanged(fixture_state, snap)


def test_set_pattern_keyscale_bad(app, fixture_state):
    snap = _snapshot(fixture_state)
    with pytest.raises(OperationError):
        apply_ops(
            [O.SetPatternKeyScale(pattern_id=99999, key="C", scale="major")],
            app, "x"
        )
    _assert_unchanged(fixture_state, snap)


def test_create_placement_bad_track(app, fixture_state):
    snap = _snapshot(fixture_state)
    p = fixture_state._test_refs['p1']
    with pytest.raises(OperationError):
        apply_ops(
            [O.CreatePlacement(track_id=99999, pattern_id=p.id, time=0.0)],
            app, "x"
        )
    _assert_unchanged(fixture_state, snap)


def test_create_placement_bad_repeats(app, fixture_state):
    snap = _snapshot(fixture_state)
    refs = fixture_state._test_refs
    with pytest.raises(OperationError):
        apply_ops(
            [O.CreatePlacement(track_id=refs['t1'].id,
                               pattern_id=refs['p1'].id,
                               time=0.0, repeats=0)],
            app, "x"
        )
    _assert_unchanged(fixture_state, snap)


def test_move_placement_bad(app, fixture_state):
    snap = _snapshot(fixture_state)
    with pytest.raises(OperationError):
        apply_ops([O.MovePlacement(placement_id=99999, new_time=0.0)], app, "x")
    _assert_unchanged(fixture_state, snap)


def test_set_placement_repeats_bad(app, fixture_state):
    snap = _snapshot(fixture_state)
    pl = fixture_state._test_refs['pl1']
    with pytest.raises(OperationError):
        apply_ops([O.SetPlacementRepeats(placement_id=pl.id, repeats=0)],
                   app, "x")
    _assert_unchanged(fixture_state, snap)


def test_set_placement_transpose_bad_id(app, fixture_state):
    snap = _snapshot(fixture_state)
    with pytest.raises(OperationError):
        apply_ops([O.SetPlacementTranspose(placement_id=99999, transpose=0)],
                   app, "x")
    _assert_unchanged(fixture_state, snap)


def test_delete_placement_bad(app, fixture_state):
    snap = _snapshot(fixture_state)
    with pytest.raises(OperationError):
        apply_ops([O.DeletePlacement(placement_id=99999)], app, "x")
    _assert_unchanged(fixture_state, snap)


def test_create_variation_bad(app, fixture_state):
    snap = _snapshot(fixture_state)
    with pytest.raises(OperationError):
        apply_ops([O.CreateVariation(parent_pattern_id=99999)], app, "x")
    _assert_unchanged(fixture_state, snap)


def test_delete_variation_bad(app, fixture_state):
    snap = _snapshot(fixture_state)
    with pytest.raises(OperationError):
        apply_ops([O.DeleteVariation(variation_id=99999)], app, "x")
    _assert_unchanged(fixture_state, snap)


def test_flatten_variation_bad(app, fixture_state):
    snap = _snapshot(fixture_state)
    with pytest.raises(OperationError):
        apply_ops([O.FlattenVariation(variation_id=99999)], app, "x")
    _assert_unchanged(fixture_state, snap)


def test_variation_add_note_bad(app, fixture_state):
    snap = _snapshot(fixture_state)
    with pytest.raises(OperationError):
        apply_ops(
            [O.VariationAddNote(variation_id=99999, pitch=60, start=0,
                                 duration=1, velocity=100)],
            app, "x"
        )
    _assert_unchanged(fixture_state, snap)


def test_variation_delete_note_bad(app, fixture_state):
    snap = _snapshot(fixture_state)
    with pytest.raises(OperationError):
        apply_ops([O.VariationDeleteNote(variation_id=99999, note_id=1)],
                   app, "x")
    _assert_unchanged(fixture_state, snap)


def test_variation_modify_note_bad(app, fixture_state):
    snap = _snapshot(fixture_state)
    with pytest.raises(OperationError):
        apply_ops([O.VariationModifyNote(variation_id=99999, note_id=1)],
                   app, "x")
    _assert_unchanged(fixture_state, snap)


def test_variation_split_note_bad_offset(app, fixture_state):
    snap = _snapshot(fixture_state)
    var = fixture_state._test_refs['var']
    with pytest.raises(OperationError):
        apply_ops(
            [O.VariationSplitNote(variation_id=var.id, note_id=1,
                                   split_offset=0.0)],
            app, "x"
        )
    _assert_unchanged(fixture_state, snap)


def test_create_track_bad_channel(app, fixture_state):
    snap = _snapshot(fixture_state)
    with pytest.raises(OperationError):
        apply_ops([O.CreateTrack(name="X", channel=99)], app, "x")
    _assert_unchanged(fixture_state, snap)


def test_rename_track_bad(app, fixture_state):
    snap = _snapshot(fixture_state)
    with pytest.raises(OperationError):
        apply_ops([O.RenameTrack(track_id=99999, name="X")], app, "x")
    _assert_unchanged(fixture_state, snap)


def test_delete_track_bad(app, fixture_state):
    snap = _snapshot(fixture_state)
    with pytest.raises(OperationError):
        apply_ops([O.DeleteTrack(track_id=99999)], app, "x")
    _assert_unchanged(fixture_state, snap)


def test_set_track_instrument_bad(app, fixture_state):
    snap = _snapshot(fixture_state)
    t = fixture_state._test_refs['t1']
    with pytest.raises(OperationError):
        apply_ops([O.SetTrackInstrument(track_id=t.id, bank=0, program=200)],
                   app, "x")
    _assert_unchanged(fixture_state, snap)


def test_set_track_volume_bad(app, fixture_state):
    snap = _snapshot(fixture_state)
    t = fixture_state._test_refs['t1']
    with pytest.raises(OperationError):
        apply_ops([O.SetTrackVolume(track_id=t.id, volume=200)], app, "x")
    _assert_unchanged(fixture_state, snap)


def test_duplicate_tempo_track_rejected(app, fixture_state):
    snap = _snapshot(fixture_state)
    # The fixture already has a tempo track.
    with pytest.raises(OperationError):
        apply_ops([O.CreateAutomationTrack(name="X", target="tempo")],
                   app, "x")
    _assert_unchanged(fixture_state, snap)


def test_delete_auto_track_bad(app, fixture_state):
    snap = _snapshot(fixture_state)
    with pytest.raises(OperationError):
        apply_ops([O.DeleteAutomationTrack(track_id=99999)], app, "x")
    _assert_unchanged(fixture_state, snap)


def test_rename_auto_track_bad(app, fixture_state):
    snap = _snapshot(fixture_state)
    with pytest.raises(OperationError):
        apply_ops([O.RenameAutomationTrack(track_id=99999, name="X")],
                   app, "x")
    _assert_unchanged(fixture_state, snap)


def test_create_auto_pattern_bad(app, fixture_state):
    snap = _snapshot(fixture_state)
    with pytest.raises(OperationError):
        apply_ops([O.CreateAutomationPattern(name="X", length=0.0)], app, "x")
    _assert_unchanged(fixture_state, snap)


def test_delete_auto_pattern_bad(app, fixture_state):
    snap = _snapshot(fixture_state)
    with pytest.raises(OperationError):
        apply_ops([O.DeleteAutomationPattern(pattern_id=99999)], app, "x")
    _assert_unchanged(fixture_state, snap)


def test_resize_auto_pattern_bad(app, fixture_state):
    snap = _snapshot(fixture_state)
    with pytest.raises(OperationError):
        apply_ops([O.ResizeAutomationPattern(pattern_id=99999, new_length=1)],
                   app, "x")
    _assert_unchanged(fixture_state, snap)


def test_set_auto_points_bad_curve(app, fixture_state):
    snap = _snapshot(fixture_state)
    p = fixture_state._test_refs['apat']
    with pytest.raises(OperationError):
        apply_ops(
            [O.SetAutomationPoints(pattern_id=p.id,
                                   points=((0.0, 0.0, 'bogus'),))],
            app, "x"
        )
    _assert_unchanged(fixture_state, snap)


def test_create_auto_placement_bad(app, fixture_state):
    snap = _snapshot(fixture_state)
    refs = fixture_state._test_refs
    with pytest.raises(OperationError):
        apply_ops(
            [O.CreateAutomationPlacement(track_id=99999,
                                         pattern_id=refs['apat'].id,
                                         time=0.0)],
            app, "x"
        )
    _assert_unchanged(fixture_state, snap)


def test_move_auto_placement_bad(app, fixture_state):
    snap = _snapshot(fixture_state)
    with pytest.raises(OperationError):
        apply_ops([O.MoveAutomationPlacement(placement_id=99999, new_time=0)],
                   app, "x")
    _assert_unchanged(fixture_state, snap)


def test_delete_auto_placement_bad(app, fixture_state):
    snap = _snapshot(fixture_state)
    with pytest.raises(OperationError):
        apply_ops([O.DeleteAutomationPlacement(placement_id=99999)], app, "x")
    _assert_unchanged(fixture_state, snap)


def test_create_beat_pattern_bad(app, fixture_state):
    snap = _snapshot(fixture_state)
    with pytest.raises(OperationError):
        apply_ops([O.CreateBeatPattern(name="X", length=-1.0)], app, "x")
    _assert_unchanged(fixture_state, snap)


def test_delete_beat_pattern_bad(app, fixture_state):
    snap = _snapshot(fixture_state)
    with pytest.raises(OperationError):
        apply_ops([O.DeleteBeatPattern(pattern_id=99999)], app, "x")
    _assert_unchanged(fixture_state, snap)


def test_resize_beat_pattern_bad(app, fixture_state):
    snap = _snapshot(fixture_state)
    with pytest.raises(OperationError):
        apply_ops([O.ResizeBeatPattern(pattern_id=99999, new_length=1.0)],
                   app, "x")
    _assert_unchanged(fixture_state, snap)


def test_set_beat_step_bad_vel(app, fixture_state):
    snap = _snapshot(fixture_state)
    refs = fixture_state._test_refs
    with pytest.raises(OperationError):
        apply_ops(
            [O.SetBeatStep(pattern_id=refs['bpat'].id,
                           inst_id=refs['inst_kick'].id,
                           step=0, velocity=999)],
            app, "x"
        )
    _assert_unchanged(fixture_state, snap)


def test_set_beat_row_bad_vel(app, fixture_state):
    snap = _snapshot(fixture_state)
    refs = fixture_state._test_refs
    with pytest.raises(OperationError):
        apply_ops(
            [O.SetBeatRow(pattern_id=refs['bpat'].id,
                          inst_id=refs['inst_kick'].id,
                          velocities=(100, 999))],
            app, "x"
        )
    _assert_unchanged(fixture_state, snap)


def test_create_beat_placement_bad(app, fixture_state):
    snap = _snapshot(fixture_state)
    refs = fixture_state._test_refs
    with pytest.raises(OperationError):
        apply_ops(
            [O.CreateBeatPlacement(track_id=99999,
                                   pattern_id=refs['bpat'].id, time=0)],
            app, "x"
        )
    _assert_unchanged(fixture_state, snap)


def test_move_beat_placement_bad(app, fixture_state):
    snap = _snapshot(fixture_state)
    with pytest.raises(OperationError):
        apply_ops([O.MoveBeatPlacement(placement_id=99999, new_time=0)],
                   app, "x")
    _assert_unchanged(fixture_state, snap)


def test_set_beat_placement_repeats_bad(app, fixture_state):
    snap = _snapshot(fixture_state)
    bpl = fixture_state._test_refs['bpl']
    with pytest.raises(OperationError):
        apply_ops([O.SetBeatPlacementRepeats(placement_id=bpl.id, repeats=0)],
                   app, "x")
    _assert_unchanged(fixture_state, snap)


def test_delete_beat_placement_bad(app, fixture_state):
    snap = _snapshot(fixture_state)
    with pytest.raises(OperationError):
        apply_ops([O.DeleteBeatPlacement(placement_id=99999)], app, "x")
    _assert_unchanged(fixture_state, snap)
