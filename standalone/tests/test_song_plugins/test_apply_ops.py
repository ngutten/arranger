"""Apply-ops executor: batching, atomicity, undo_group integration."""

from __future__ import annotations

import pytest
import copy

from standalone.song_plugins import ops as O
from standalone.song_plugins.apply_ops import apply_ops, OperationError


def test_single_op_applies(app, fixture_state):
    refs = fixture_state._test_refs
    results = apply_ops([O.SetTrackVolume(track_id=refs['t1'].id, volume=64)],
                        app, "set volume")
    assert results == [None]
    assert fixture_state.find_track(refs['t1'].id).volume == 64


def test_create_returns_new_id(app, fixture_state):
    results = apply_ops([O.CreateTrack(name="New")], app, "create")
    assert len(results) == 1
    nid = results[0]
    assert isinstance(nid, int)
    assert fixture_state.find_track(nid) is not None


def test_batch_applies_all(app, fixture_state):
    refs = fixture_state._test_refs
    ops = [
        O.SetTrackVolume(track_id=refs['t1'].id, volume=50),
        O.SetTrackVolume(track_id=refs['t2'].id, volume=60),
        O.RenameTrack(track_id=refs['t1'].id, name="NewPiano"),
    ]
    apply_ops(ops, app, "batch")
    assert fixture_state.find_track(refs['t1'].id).volume == 50
    assert fixture_state.find_track(refs['t1'].id).name == "NewPiano"
    assert fixture_state.find_track(refs['t2'].id).volume == 60


def test_invalid_op_rejects_whole_batch(app, fixture_state):
    """If any op fails validation, none apply."""
    refs = fixture_state._test_refs
    before = copy.deepcopy(fixture_state.to_json())
    ops = [
        O.SetTrackVolume(track_id=refs['t1'].id, volume=50),
        O.SetTrackVolume(track_id=99999, volume=60),   # bad id
        O.RenameTrack(track_id=refs['t1'].id, name="X"),
    ]
    with pytest.raises(OperationError):
        apply_ops(ops, app, "bad batch")
    after = fixture_state.to_json()
    # _next_id may have differed if anything partial happened; the collections
    # themselves must match. Compare parsed JSONs to ignore nextId drift.
    import json
    before_d = json.loads(before)
    after_d = json.loads(after)
    # nothing mutated except possibly _next_id (should also match since
    # validation is pre-mutation).
    assert before_d == after_d


def test_single_undo_entry_per_batch(app, fixture_state):
    refs = fixture_state._test_refs
    before_len = len(app.undo_stack.stack)
    ops = [
        O.SetTrackVolume(track_id=refs['t1'].id, volume=10),
        O.SetTrackVolume(track_id=refs['t1'].id, volume=20),
        O.SetTrackVolume(track_id=refs['t1'].id, volume=30),
    ]
    apply_ops(ops, app, "thrice")
    assert len(app.undo_stack.stack) == before_len + 1


def test_undo_group_nesting(app, fixture_state):
    """A nested undo_group doesn't produce a second snapshot."""
    refs = fixture_state._test_refs
    before = len(app.undo_stack.stack)
    with app.undo_group("outer"):
        apply_ops([O.SetTrackVolume(track_id=refs['t1'].id, volume=42)],
                   app, "inner")
    # Only outer should push a snapshot.
    assert len(app.undo_stack.stack) == before + 1


def test_create_pattern_then_add_note_in_same_batch(app, fixture_state):
    """Operations that depend on IDs created earlier in the same batch:
    since create ops run first and return real IDs, plugins must split
    such work across two apply_ops calls. This test documents that limit.
    """
    # CreatePattern is processed; subsequent ops in the same batch cannot
    # reference its id. We verify the create alone works and that the
    # returned id is the real id.
    res = apply_ops([O.CreatePattern(name="P", length=4.0)], app, "p")
    new_pid = res[0]
    assert fixture_state.find_pattern(new_pid) is not None
    # Now use it.
    res2 = apply_ops(
        [O.AddNote(pattern_id=new_pid, pitch=60, start=0.0,
                   duration=1.0, velocity=100)],
        app, "add"
    )
    assert isinstance(res2[0], int)
