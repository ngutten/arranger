"""One test per op: valid inputs → expected mutation."""

from __future__ import annotations

import pytest

from standalone.song_plugins import ops as O
from standalone.song_plugins.apply_ops import apply_ops


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------

def test_add_note(app, fixture_state):
    pat = fixture_state._test_refs['p1']
    before = len(pat.notes)
    res = apply_ops(
        [O.AddNote(pattern_id=pat.id, pitch=65, start=0.5,
                   duration=0.5, velocity=80, lyric="la")],
        app, "add"
    )
    assert len(pat.notes) == before + 1
    new = pat.notes[-1]
    assert new.note_id == res[0]
    assert new.pitch == 65
    assert new.lyric == "la"


def test_move_note(app, fixture_state):
    pat = fixture_state._test_refs['p1']
    n = pat.notes[0]
    apply_ops(
        [O.MoveNote(pattern_id=pat.id, note_id=n.note_id,
                    new_start=1.5, new_pitch=72)],
        app, "move"
    )
    assert n.start == 1.5
    assert n.pitch == 72


def test_resize_note(app, fixture_state):
    pat = fixture_state._test_refs['p1']
    n = pat.notes[0]
    apply_ops(
        [O.ResizeNote(pattern_id=pat.id, note_id=n.note_id,
                      new_duration=2.5)],
        app, "resize"
    )
    assert n.duration == 2.5


def test_delete_note(app, fixture_state):
    pat = fixture_state._test_refs['p1']
    nid = pat.notes[0].note_id
    apply_ops([O.DeleteNote(pattern_id=pat.id, note_id=nid)], app, "del")
    assert all(n.note_id != nid for n in pat.notes)


def test_set_note_velocity(app, fixture_state):
    pat = fixture_state._test_refs['p1']
    n = pat.notes[0]
    apply_ops(
        [O.SetNoteVelocity(pattern_id=pat.id, note_id=n.note_id, velocity=60)],
        app, "vel"
    )
    assert n.velocity == 60


def test_set_note_lyric(app, fixture_state):
    pat = fixture_state._test_refs['p1']
    n = pat.notes[0]
    apply_ops(
        [O.SetNoteLyric(pattern_id=pat.id, note_id=n.note_id, lyric="da")],
        app, "lyr"
    )
    assert n.lyric == "da"


def test_set_note_bend(app, fixture_state):
    pat = fixture_state._test_refs['p1']
    n = pat.notes[0]
    bend = ((0.0, 0.0), (0.5, 1.0), (1.0, 0.0))
    apply_ops(
        [O.SetNoteBend(pattern_id=pat.id, note_id=n.note_id, bend=bend)],
        app, "bend"
    )
    assert len(n.bend) == 3
    assert n.bend[1] == [0.5, 1.0]


def test_split_note(app, fixture_state):
    pat = fixture_state._test_refs['p1']
    n = pat.notes[0]
    orig_dur = n.duration
    apply_ops(
        [O.SplitNote(pattern_id=pat.id, note_id=n.note_id, split_offset=0.25)],
        app, "split"
    )
    assert n.duration == 0.25
    # There should be a new note (the right half) in the pattern.
    rights = [m for m in pat.notes
              if m.note_id != n.note_id and m.pitch == n.pitch
              and abs(m.start - (0.0 + 0.25)) < 1e-9]
    assert len(rights) == 1
    assert abs(rights[0].duration - (orig_dur - 0.25)) < 1e-9


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

def test_create_pattern(app, fixture_state):
    before = len(fixture_state.patterns)
    res = apply_ops([O.CreatePattern(name="New", length=3.0)], app, "cp")
    assert len(fixture_state.patterns) == before + 1
    assert fixture_state.find_pattern(res[0]).name == "New"


def test_rename_pattern(app, fixture_state):
    p = fixture_state._test_refs['p1']
    apply_ops([O.RenamePattern(pattern_id=p.id, name="Renamed")], app, "r")
    assert p.name == "Renamed"


def test_resize_pattern(app, fixture_state):
    p = fixture_state._test_refs['p1']
    apply_ops([O.ResizePattern(pattern_id=p.id, new_length=16.0)], app, "r")
    assert p.length == 16.0


def test_delete_pattern(app, fixture_state):
    p = fixture_state._test_refs['p2']
    apply_ops([O.DeletePattern(pattern_id=p.id)], app, "d")
    assert fixture_state.find_pattern(p.id) is None


def test_duplicate_pattern(app, fixture_state):
    p = fixture_state._test_refs['p1']
    res = apply_ops([O.DuplicatePattern(pattern_id=p.id)], app, "dup")
    assert fixture_state.find_pattern(res[0]) is not None


def test_set_pattern_keyscale(app, fixture_state):
    p = fixture_state._test_refs['p1']
    apply_ops(
        [O.SetPatternKeyScale(pattern_id=p.id, key="G", scale="minor")],
        app, "ks"
    )
    assert p.key == "G"
    assert p.scale == "minor"


# ---------------------------------------------------------------------------
# Placements
# ---------------------------------------------------------------------------

def test_create_placement(app, fixture_state):
    refs = fixture_state._test_refs
    before = len(fixture_state.placements)
    res = apply_ops(
        [O.CreatePlacement(track_id=refs['t1'].id, pattern_id=refs['p2'].id,
                           time=4.0, repeats=2)],
        app, "cpl"
    )
    assert len(fixture_state.placements) == before + 1
    assert fixture_state.find_placement(res[0]).time == 4.0


def test_move_placement(app, fixture_state):
    pl = fixture_state._test_refs['pl1']
    apply_ops([O.MovePlacement(placement_id=pl.id, new_time=12.0)], app, "m")
    assert pl.time == 12.0


def test_set_placement_repeats(app, fixture_state):
    pl = fixture_state._test_refs['pl1']
    apply_ops([O.SetPlacementRepeats(placement_id=pl.id, repeats=4)], app, "r")
    assert pl.repeats == 4


def test_set_placement_transpose(app, fixture_state):
    pl = fixture_state._test_refs['pl1']
    apply_ops([O.SetPlacementTranspose(placement_id=pl.id, transpose=5)], app, "t")
    assert pl.transpose == 5


def test_delete_placement(app, fixture_state):
    pl = fixture_state._test_refs['pl1']
    apply_ops([O.DeletePlacement(placement_id=pl.id)], app, "d")
    assert fixture_state.find_placement(pl.id) is None


# ---------------------------------------------------------------------------
# Variations
# ---------------------------------------------------------------------------

def test_create_variation(app, fixture_state):
    p = fixture_state._test_refs['p1']
    res = apply_ops([O.CreateVariation(parent_pattern_id=p.id)], app, "cv")
    assert fixture_state.find_variation(res[0]) is not None


def test_delete_variation(app, fixture_state):
    var = fixture_state._test_refs['var']
    apply_ops([O.DeleteVariation(variation_id=var.id)], app, "dv")
    assert fixture_state.find_variation(var.id) is None


def test_flatten_variation(app, fixture_state):
    var = fixture_state._test_refs['var']
    res = apply_ops([O.FlattenVariation(variation_id=var.id)], app, "fv")
    assert fixture_state.find_pattern(res[0]) is not None
    assert fixture_state.find_variation(var.id) is None


def test_variation_add_note(app, fixture_state):
    var = fixture_state._test_refs['var']
    before = len(var.additions)
    res = apply_ops(
        [O.VariationAddNote(variation_id=var.id, pitch=70, start=1.0,
                             duration=0.5, velocity=90)],
        app, "va"
    )
    assert len(var.additions) == before + 1
    assert isinstance(res[0], int)


def test_variation_delete_note(app, fixture_state):
    var = fixture_state._test_refs['var']
    p = fixture_state._test_refs['p1']
    # Delete note 2 (not already deleted).
    nid = p.notes[2].note_id
    apply_ops([O.VariationDeleteNote(variation_id=var.id, note_id=nid)],
               app, "vd")
    assert nid in var.deletions


def test_variation_modify_note(app, fixture_state):
    var = fixture_state._test_refs['var']
    p = fixture_state._test_refs['p1']
    nid = p.notes[2].note_id
    apply_ops(
        [O.VariationModifyNote(variation_id=var.id, note_id=nid,
                                d_pitch=3)],
        app, "vm"
    )
    assert any(m.note_id == nid and m.d_pitch == 3 for m in var.modifications)


def test_variation_split_note(app, fixture_state):
    var = fixture_state._test_refs['var']
    p = fixture_state._test_refs['p1']
    nid = p.notes[2].note_id
    res = apply_ops(
        [O.VariationSplitNote(variation_id=var.id, note_id=nid,
                               split_offset=0.25)],
        app, "vs"
    )
    assert isinstance(res[0], int)
    assert any(s.note_id == nid for s in var.splits)


# ---------------------------------------------------------------------------
# Tracks
# ---------------------------------------------------------------------------

def test_create_track(app, fixture_state):
    before = len(fixture_state.tracks)
    res = apply_ops([O.CreateTrack(name="Violin")], app, "ct")
    assert len(fixture_state.tracks) == before + 1
    assert fixture_state.find_track(res[0]).name == "Violin"


def test_rename_track(app, fixture_state):
    t = fixture_state._test_refs['t1']
    apply_ops([O.RenameTrack(track_id=t.id, name="Rhodes")], app, "rt")
    assert t.name == "Rhodes"


def test_delete_track(app, fixture_state):
    t = fixture_state._test_refs['t2']
    apply_ops([O.DeleteTrack(track_id=t.id)], app, "dt")
    assert fixture_state.find_track(t.id) is None


def test_set_track_instrument(app, fixture_state):
    t = fixture_state._test_refs['t1']
    apply_ops(
        [O.SetTrackInstrument(track_id=t.id, bank=0, program=40)],
        app, "si"
    )
    assert t.program == 40


def test_set_track_volume(app, fixture_state):
    t = fixture_state._test_refs['t1']
    apply_ops([O.SetTrackVolume(track_id=t.id, volume=55)], app, "sv")
    assert t.volume == 55


# ---------------------------------------------------------------------------
# Automation
# ---------------------------------------------------------------------------

def test_create_automation_track(app, fixture_state):
    res = apply_ops([O.CreateAutomationTrack(name="Filter")], app, "cat")
    assert fixture_state.find_automation_track(res[0]) is not None


def test_delete_automation_track(app, fixture_state):
    at = fixture_state._test_refs['at_mod']
    apply_ops([O.DeleteAutomationTrack(track_id=at.id)], app, "dat")
    assert fixture_state.find_automation_track(at.id) is None


def test_rename_automation_track(app, fixture_state):
    at = fixture_state._test_refs['at_mod']
    apply_ops([O.RenameAutomationTrack(track_id=at.id, name="LFO")], app, "rat")
    assert at.name == "LFO"


def test_create_automation_pattern(app, fixture_state):
    res = apply_ops([O.CreateAutomationPattern(name="Curve")], app, "cap")
    assert fixture_state.find_automation_pattern(res[0]) is not None


def test_delete_automation_pattern(app, fixture_state):
    p = fixture_state._test_refs['apat']
    apply_ops([O.DeleteAutomationPattern(pattern_id=p.id)], app, "dap")
    assert fixture_state.find_automation_pattern(p.id) is None


def test_resize_automation_pattern(app, fixture_state):
    p = fixture_state._test_refs['apat']
    apply_ops([O.ResizeAutomationPattern(pattern_id=p.id, new_length=8.0)],
               app, "rap")
    assert p.length == 8.0


def test_set_automation_points(app, fixture_state):
    p = fixture_state._test_refs['apat']
    new_points = ((0.0, 0.0, 'linear'), (2.0, 0.5, 'step'),
                  (4.0, 1.0, 'linear'))
    apply_ops([O.SetAutomationPoints(pattern_id=p.id, points=new_points)],
               app, "sap")
    assert len(p.points) == 3
    assert p.points[1].curve == 'step'


def test_create_automation_placement(app, fixture_state):
    refs = fixture_state._test_refs
    res = apply_ops(
        [O.CreateAutomationPlacement(track_id=refs['at_mod'].id,
                                     pattern_id=refs['apat'].id,
                                     time=8.0)],
        app, "capl"
    )
    assert fixture_state.find_automation_placement(res[0]).time == 8.0


def test_move_automation_placement(app, fixture_state):
    apl = fixture_state._test_refs['apl']
    apply_ops([O.MoveAutomationPlacement(placement_id=apl.id, new_time=4.0)],
               app, "mapl")
    assert apl.time == 4.0


def test_delete_automation_placement(app, fixture_state):
    apl = fixture_state._test_refs['apl']
    apply_ops([O.DeleteAutomationPlacement(placement_id=apl.id)], app, "dapl")
    assert fixture_state.find_automation_placement(apl.id) is None


# ---------------------------------------------------------------------------
# Beat
# ---------------------------------------------------------------------------

def test_create_beat_pattern(app, fixture_state):
    res = apply_ops([O.CreateBeatPattern(name="B", length=2.0)], app, "cbp")
    assert fixture_state.find_beat_pattern(res[0]) is not None


def test_delete_beat_pattern(app, fixture_state):
    bpat = fixture_state._test_refs['bpat']
    apply_ops([O.DeleteBeatPattern(pattern_id=bpat.id)], app, "dbp")
    assert fixture_state.find_beat_pattern(bpat.id) is None


def test_resize_beat_pattern(app, fixture_state):
    bpat = fixture_state._test_refs['bpat']
    apply_ops([O.ResizeBeatPattern(pattern_id=bpat.id, new_length=2.0)],
               app, "rbp")
    assert bpat.length == 2.0


def test_set_beat_step(app, fixture_state):
    refs = fixture_state._test_refs
    apply_ops(
        [O.SetBeatStep(pattern_id=refs['bpat'].id,
                       inst_id=refs['inst_snare'].id, step=1, velocity=80)],
        app, "sbs"
    )
    assert refs['bpat'].grid[refs['inst_snare'].id][1] == 80


def test_set_beat_row(app, fixture_state):
    refs = fixture_state._test_refs
    new_row = (100, 0, 80, 0)
    apply_ops(
        [O.SetBeatRow(pattern_id=refs['bpat'].id,
                      inst_id=refs['inst_snare'].id, velocities=new_row)],
        app, "sbr"
    )
    assert refs['bpat'].grid[refs['inst_snare'].id] == list(new_row)


def test_create_beat_placement(app, fixture_state):
    refs = fixture_state._test_refs
    res = apply_ops(
        [O.CreateBeatPlacement(track_id=refs['btrack'].id,
                               pattern_id=refs['bpat'].id, time=4.0)],
        app, "cbpl"
    )
    assert fixture_state.find_beat_placement(res[0]).time == 4.0


def test_move_beat_placement(app, fixture_state):
    bpl = fixture_state._test_refs['bpl']
    apply_ops([O.MoveBeatPlacement(placement_id=bpl.id, new_time=2.0)],
               app, "mbpl")
    assert bpl.time == 2.0


def test_set_beat_placement_repeats(app, fixture_state):
    bpl = fixture_state._test_refs['bpl']
    apply_ops([O.SetBeatPlacementRepeats(placement_id=bpl.id, repeats=8)],
               app, "sbpr")
    assert bpl.repeats == 8


def test_delete_beat_placement(app, fixture_state):
    bpl = fixture_state._test_refs['bpl']
    apply_ops([O.DeleteBeatPlacement(placement_id=bpl.id)], app, "dbpl")
    assert fixture_state.find_beat_placement(bpl.id) is None
