"""Execute a batch of operations as a single undo step."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, List, Sequence

from ..state import (
    Note, Pattern, Track, Placement, BeatPattern, BeatTrack, BeatPlacement,
    AutomationPattern, AutomationTrack, AutomationPlacement, AutomationPoint,
    Variation, PALETTE,
)
from ..ops import patterns as pat_ops
from ..ops import tracks as trk_ops
from ..ops import variations as var_ops
from . import ops as O


class OperationError(Exception):
    def __init__(self, index: int, op, reason: str):
        super().__init__(f"op[{index}] {type(op).__name__}: {reason}")
        self.index = index
        self.op = op
        self.reason = reason


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _check_pattern(state, pid, op, i):
    if state.find_pattern(pid) is None:
        raise OperationError(i, op, f"pattern {pid} not found")


def _check_track(state, tid, op, i):
    if state.find_track(tid) is None:
        raise OperationError(i, op, f"track {tid} not found")


def _check_placement(state, plid, op, i):
    if state.find_placement(plid) is None:
        raise OperationError(i, op, f"placement {plid} not found")


def _check_variation(state, vid, op, i):
    if state.find_variation(vid) is None:
        raise OperationError(i, op, f"variation {vid} not found")


def _check_beat_pattern(state, pid, op, i):
    if state.find_beat_pattern(pid) is None:
        raise OperationError(i, op, f"beat pattern {pid} not found")


def _check_beat_placement(state, plid, op, i):
    if state.find_beat_placement(plid) is None:
        raise OperationError(i, op, f"beat placement {plid} not found")


def _check_auto_track(state, tid, op, i):
    if state.find_automation_track(tid) is None:
        raise OperationError(i, op, f"automation track {tid} not found")


def _check_auto_pattern(state, pid, op, i):
    if state.find_automation_pattern(pid) is None:
        raise OperationError(i, op, f"automation pattern {pid} not found")


def _check_auto_placement(state, plid, op, i):
    if state.find_automation_placement(plid) is None:
        raise OperationError(i, op, f"automation placement {plid} not found")


def _check_note_in_pattern(state, pid, nid, op, i):
    pat = state.find_pattern(pid)
    if pat is None:
        raise OperationError(i, op, f"pattern {pid} not found")
    for n in pat.notes:
        if n.note_id == nid:
            return n
    raise OperationError(i, op, f"note {nid} not in pattern {pid}")


def _validate(state, ops: Sequence) -> None:
    for i, op in enumerate(ops):
        if not isinstance(op, O.ALL_OP_TYPES):
            raise OperationError(i, op, f"unknown op type {type(op).__name__}")
        _VALIDATORS[type(op)](state, op, i)


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

def _v_add_note(s, op, i):
    _check_pattern(s, op.pattern_id, op, i)
    if not 0 <= op.pitch <= 127:
        raise OperationError(i, op, f"pitch {op.pitch} out of range")
    if op.duration <= 0:
        raise OperationError(i, op, "duration must be > 0")
    if op.start < 0:
        raise OperationError(i, op, "start must be >= 0")
    if not 1 <= op.velocity <= 127:
        raise OperationError(i, op, f"velocity {op.velocity} out of range")


def _v_move_note(s, op, i):
    _check_note_in_pattern(s, op.pattern_id, op.note_id, op, i)
    if not 0 <= op.new_pitch <= 127:
        raise OperationError(i, op, f"pitch {op.new_pitch} out of range")
    if op.new_start < 0:
        raise OperationError(i, op, "start must be >= 0")


def _v_resize_note(s, op, i):
    _check_note_in_pattern(s, op.pattern_id, op.note_id, op, i)
    if op.new_duration <= 0:
        raise OperationError(i, op, "duration must be > 0")


def _v_delete_note(s, op, i):
    _check_note_in_pattern(s, op.pattern_id, op.note_id, op, i)


def _v_set_velocity(s, op, i):
    _check_note_in_pattern(s, op.pattern_id, op.note_id, op, i)
    if not 1 <= op.velocity <= 127:
        raise OperationError(i, op, f"velocity {op.velocity} out of range")


def _v_set_lyric(s, op, i):
    _check_note_in_pattern(s, op.pattern_id, op.note_id, op, i)


def _v_set_bend(s, op, i):
    _check_note_in_pattern(s, op.pattern_id, op.note_id, op, i)
    for pt in op.bend:
        if len(pt) != 2:
            raise OperationError(i, op, "bend points must be (offset, semis)")
        o, sv = pt
        if not -2.0 <= sv <= 2.0:
            raise OperationError(i, op, "bend semis out of [-2, 2]")


def _v_split_note(s, op, i):
    n = _check_note_in_pattern(s, op.pattern_id, op.note_id, op, i)
    if op.split_offset <= 0 or op.split_offset >= n.duration:
        raise OperationError(i, op, "split_offset must be within the note")


def _v_create_pattern(s, op, i):
    if op.length <= 0:
        raise OperationError(i, op, "length must be > 0")


def _v_rename_pattern(s, op, i):
    _check_pattern(s, op.pattern_id, op, i)


def _v_resize_pattern(s, op, i):
    _check_pattern(s, op.pattern_id, op, i)
    if op.new_length <= 0:
        raise OperationError(i, op, "length must be > 0")


def _v_delete_pattern(s, op, i):
    _check_pattern(s, op.pattern_id, op, i)


def _v_duplicate_pattern(s, op, i):
    _check_pattern(s, op.pattern_id, op, i)


def _v_set_pat_keyscale(s, op, i):
    _check_pattern(s, op.pattern_id, op, i)


def _v_create_placement(s, op, i):
    _check_track(s, op.track_id, op, i)
    if op.is_variation:
        _check_variation(s, op.pattern_id, op, i)
    else:
        _check_pattern(s, op.pattern_id, op, i)
    if op.time < 0:
        raise OperationError(i, op, "time must be >= 0")
    if op.repeats < 1:
        raise OperationError(i, op, "repeats must be >= 1")


def _v_move_placement(s, op, i):
    _check_placement(s, op.placement_id, op, i)
    if op.new_track_id is not None:
        _check_track(s, op.new_track_id, op, i)
    if op.new_time < 0:
        raise OperationError(i, op, "time must be >= 0")


def _v_set_pl_repeats(s, op, i):
    _check_placement(s, op.placement_id, op, i)
    if op.repeats < 1:
        raise OperationError(i, op, "repeats must be >= 1")


def _v_set_pl_transpose(s, op, i):
    _check_placement(s, op.placement_id, op, i)


def _v_delete_placement(s, op, i):
    _check_placement(s, op.placement_id, op, i)


def _v_create_variation(s, op, i):
    _check_pattern(s, op.parent_pattern_id, op, i)


def _v_delete_variation(s, op, i):
    _check_variation(s, op.variation_id, op, i)


def _v_flatten_variation(s, op, i):
    _check_variation(s, op.variation_id, op, i)


def _v_var_add_note(s, op, i):
    _check_variation(s, op.variation_id, op, i)
    if not 0 <= op.pitch <= 127:
        raise OperationError(i, op, "pitch out of range")
    if op.duration <= 0:
        raise OperationError(i, op, "duration must be > 0")


def _v_var_delete_note(s, op, i):
    _check_variation(s, op.variation_id, op, i)


def _v_var_modify_note(s, op, i):
    _check_variation(s, op.variation_id, op, i)


def _v_var_split_note(s, op, i):
    _check_variation(s, op.variation_id, op, i)
    if op.split_offset <= 0:
        raise OperationError(i, op, "split_offset must be > 0")


def _v_create_track(s, op, i):
    if not 0 <= op.channel <= 15:
        raise OperationError(i, op, "channel out of range")
    if not 0 <= op.volume <= 127:
        raise OperationError(i, op, "volume out of range")


def _v_rename_track(s, op, i):
    _check_track(s, op.track_id, op, i)


def _v_delete_track(s, op, i):
    _check_track(s, op.track_id, op, i)


def _v_set_track_inst(s, op, i):
    _check_track(s, op.track_id, op, i)
    if not 0 <= op.bank <= 16383:
        raise OperationError(i, op, "bank out of range")
    if not 0 <= op.program <= 127:
        raise OperationError(i, op, "program out of range")


def _v_set_track_vol(s, op, i):
    _check_track(s, op.track_id, op, i)
    if not 0 <= op.volume <= 127:
        raise OperationError(i, op, "volume out of range")


def _v_create_auto_track(s, op, i):
    if op.target == 'tempo' and s.find_tempo_track() is not None:
        raise OperationError(i, op, "a tempo automation track already exists")
    if op.target and op.target.startswith('output_gain:'):
        for t in s.automation_tracks:
            if t.target == op.target:
                raise OperationError(
                    i, op,
                    f"another automation track already targets {op.target}")


def _v_delete_auto_track(s, op, i):
    _check_auto_track(s, op.track_id, op, i)


def _v_rename_auto_track(s, op, i):
    _check_auto_track(s, op.track_id, op, i)


def _v_create_auto_pattern(s, op, i):
    if op.length <= 0:
        raise OperationError(i, op, "length must be > 0")


def _v_delete_auto_pattern(s, op, i):
    _check_auto_pattern(s, op.pattern_id, op, i)


def _v_resize_auto_pattern(s, op, i):
    _check_auto_pattern(s, op.pattern_id, op, i)
    if op.new_length <= 0:
        raise OperationError(i, op, "length must be > 0")


def _v_set_auto_points(s, op, i):
    _check_auto_pattern(s, op.pattern_id, op, i)
    for pt in op.points:
        if len(pt) != 3:
            raise OperationError(i, op, "points must be (time, value, curve)")
        t, _v, c = pt
        if t < 0:
            raise OperationError(i, op, "point time must be >= 0")
        if c not in ('linear', 'step', 'smooth'):
            raise OperationError(i, op, f"unknown curve {c}")


def _v_create_auto_pl(s, op, i):
    _check_auto_track(s, op.track_id, op, i)
    _check_auto_pattern(s, op.pattern_id, op, i)
    if op.time < 0:
        raise OperationError(i, op, "time must be >= 0")
    if op.repeats < 1:
        raise OperationError(i, op, "repeats must be >= 1")


def _v_move_auto_pl(s, op, i):
    _check_auto_placement(s, op.placement_id, op, i)
    if op.new_track_id is not None:
        _check_auto_track(s, op.new_track_id, op, i)
    if op.new_time < 0:
        raise OperationError(i, op, "time must be >= 0")


def _v_delete_auto_pl(s, op, i):
    _check_auto_placement(s, op.placement_id, op, i)


def _v_create_beat_pat(s, op, i):
    if op.length <= 0:
        raise OperationError(i, op, "length must be > 0")
    if op.subdivision < 1:
        raise OperationError(i, op, "subdivision must be >= 1")


def _v_delete_beat_pat(s, op, i):
    _check_beat_pattern(s, op.pattern_id, op, i)


def _v_resize_beat_pat(s, op, i):
    _check_beat_pattern(s, op.pattern_id, op, i)
    if op.new_length <= 0:
        raise OperationError(i, op, "length must be > 0")


def _v_set_beat_step(s, op, i):
    pat = s.find_beat_pattern(op.pattern_id)
    if pat is None:
        raise OperationError(i, op, f"beat pattern {op.pattern_id} not found")
    if s.find_beat_instrument(op.inst_id) is None:
        raise OperationError(i, op, f"beat instrument {op.inst_id} not found")
    if not 0 <= op.velocity <= 127:
        raise OperationError(i, op, "velocity out of range")
    if op.step < 0:
        raise OperationError(i, op, "step must be >= 0")


def _v_set_beat_row(s, op, i):
    pat = s.find_beat_pattern(op.pattern_id)
    if pat is None:
        raise OperationError(i, op, f"beat pattern {op.pattern_id} not found")
    if s.find_beat_instrument(op.inst_id) is None:
        raise OperationError(i, op, f"beat instrument {op.inst_id} not found")
    for v in op.velocities:
        if not 0 <= v <= 127:
            raise OperationError(i, op, "velocity out of range")


def _v_create_beat_pl(s, op, i):
    if s.find_beat_track(op.track_id) is None:
        raise OperationError(i, op, f"beat track {op.track_id} not found")
    _check_beat_pattern(s, op.pattern_id, op, i)
    if op.time < 0:
        raise OperationError(i, op, "time must be >= 0")
    if op.repeats < 1:
        raise OperationError(i, op, "repeats must be >= 1")


def _v_move_beat_pl(s, op, i):
    _check_beat_placement(s, op.placement_id, op, i)
    if op.new_track_id is not None:
        if s.find_beat_track(op.new_track_id) is None:
            raise OperationError(i, op, f"beat track {op.new_track_id} not found")
    if op.new_time < 0:
        raise OperationError(i, op, "time must be >= 0")


def _v_set_beat_pl_repeats(s, op, i):
    _check_beat_placement(s, op.placement_id, op, i)
    if op.repeats < 1:
        raise OperationError(i, op, "repeats must be >= 1")


def _v_delete_beat_pl(s, op, i):
    _check_beat_placement(s, op.placement_id, op, i)


_VALIDATORS = {
    O.AddNote: _v_add_note, O.MoveNote: _v_move_note,
    O.ResizeNote: _v_resize_note, O.DeleteNote: _v_delete_note,
    O.SetNoteVelocity: _v_set_velocity, O.SetNoteLyric: _v_set_lyric,
    O.SetNoteBend: _v_set_bend, O.SplitNote: _v_split_note,

    O.CreatePattern: _v_create_pattern, O.RenamePattern: _v_rename_pattern,
    O.ResizePattern: _v_resize_pattern, O.DeletePattern: _v_delete_pattern,
    O.DuplicatePattern: _v_duplicate_pattern,
    O.SetPatternKeyScale: _v_set_pat_keyscale,

    O.CreatePlacement: _v_create_placement,
    O.MovePlacement: _v_move_placement,
    O.SetPlacementRepeats: _v_set_pl_repeats,
    O.SetPlacementTranspose: _v_set_pl_transpose,
    O.DeletePlacement: _v_delete_placement,

    O.CreateVariation: _v_create_variation,
    O.DeleteVariation: _v_delete_variation,
    O.FlattenVariation: _v_flatten_variation,
    O.VariationAddNote: _v_var_add_note,
    O.VariationDeleteNote: _v_var_delete_note,
    O.VariationModifyNote: _v_var_modify_note,
    O.VariationSplitNote: _v_var_split_note,

    O.CreateTrack: _v_create_track, O.RenameTrack: _v_rename_track,
    O.DeleteTrack: _v_delete_track,
    O.SetTrackInstrument: _v_set_track_inst,
    O.SetTrackVolume: _v_set_track_vol,

    O.CreateAutomationTrack: _v_create_auto_track,
    O.DeleteAutomationTrack: _v_delete_auto_track,
    O.RenameAutomationTrack: _v_rename_auto_track,
    O.CreateAutomationPattern: _v_create_auto_pattern,
    O.DeleteAutomationPattern: _v_delete_auto_pattern,
    O.ResizeAutomationPattern: _v_resize_auto_pattern,
    O.SetAutomationPoints: _v_set_auto_points,
    O.CreateAutomationPlacement: _v_create_auto_pl,
    O.MoveAutomationPlacement: _v_move_auto_pl,
    O.DeleteAutomationPlacement: _v_delete_auto_pl,

    O.CreateBeatPattern: _v_create_beat_pat,
    O.DeleteBeatPattern: _v_delete_beat_pat,
    O.ResizeBeatPattern: _v_resize_beat_pat,
    O.SetBeatStep: _v_set_beat_step,
    O.SetBeatRow: _v_set_beat_row,
    O.CreateBeatPlacement: _v_create_beat_pl,
    O.MoveBeatPlacement: _v_move_beat_pl,
    O.SetBeatPlacementRepeats: _v_set_beat_pl_repeats,
    O.DeleteBeatPlacement: _v_delete_beat_pl,
}


# ---------------------------------------------------------------------------
# Handlers (mutators). Each takes (app, op) and returns the new id (or None).
# ---------------------------------------------------------------------------

def _get_note(pat, nid):
    for n in pat.notes:
        if n.note_id == nid:
            return n
    return None


def _h_add_note(app, op):
    pat = app.state.find_pattern(op.pattern_id)
    n = Note(
        pitch=op.pitch, start=op.start, duration=op.duration,
        velocity=op.velocity, lyric=op.lyric,
        bend=[list(b) for b in op.bend],
        note_id=app.state.new_id(),
        tags=dict(op.tags) if op.tags else {},
    )
    pat.notes.append(n)
    return n.note_id


def _h_move_note(app, op):
    pat = app.state.find_pattern(op.pattern_id)
    n = _get_note(pat, op.note_id)
    n.start = op.new_start
    n.pitch = op.new_pitch
    return None


def _h_resize_note(app, op):
    pat = app.state.find_pattern(op.pattern_id)
    n = _get_note(pat, op.note_id)
    n.duration = op.new_duration
    return None


def _h_delete_note(app, op):
    pat = app.state.find_pattern(op.pattern_id)
    pat.notes = [n for n in pat.notes if n.note_id != op.note_id]
    return None


def _h_set_velocity(app, op):
    pat = app.state.find_pattern(op.pattern_id)
    n = _get_note(pat, op.note_id)
    n.velocity = op.velocity
    return None


def _h_set_lyric(app, op):
    pat = app.state.find_pattern(op.pattern_id)
    n = _get_note(pat, op.note_id)
    n.lyric = op.lyric
    return None


def _h_set_bend(app, op):
    pat = app.state.find_pattern(op.pattern_id)
    n = _get_note(pat, op.note_id)
    n.bend = [list(b) for b in op.bend]
    return None


def _h_split_note(app, op):
    pat = app.state.find_pattern(op.pattern_id)
    n = _get_note(pat, op.note_id)
    right = Note(
        pitch=n.pitch, start=n.start + op.split_offset,
        duration=n.duration - op.split_offset,
        velocity=n.velocity, lyric='',
        bend=[], note_id=app.state.new_id(),
    )
    n.duration = op.split_offset
    n.bend = []
    pat.notes.append(right)
    return right.note_id


def _h_create_pattern(app, op):
    color = op.color or PALETTE[len(app.state.patterns) % len(PALETTE)]
    pat = Pattern(
        id=app.state.new_id(), name=op.name, length=op.length,
        notes=[], color=color, key=op.key, scale=op.scale,
    )
    app.state.patterns.append(pat)
    return pat.id


def _h_rename_pattern(app, op):
    app.state.find_pattern(op.pattern_id).name = op.name
    return None


def _h_resize_pattern(app, op):
    app.state.find_pattern(op.pattern_id).length = op.new_length
    return None


def _h_delete_pattern(app, op):
    pat_ops.delete_pattern(app.state, op.pattern_id)
    return None


def _h_duplicate_pattern(app, op):
    new = pat_ops.duplicate_pattern(app.state, op.pattern_id)
    return new.id if new else None


def _h_set_pat_keyscale(app, op):
    pat = app.state.find_pattern(op.pattern_id)
    pat.key = op.key
    pat.scale = op.scale
    return None


def _h_create_placement(app, op):
    pl = Placement(
        id=app.state.new_id(), track_id=op.track_id,
        pattern_id=op.pattern_id, time=op.time, repeats=op.repeats,
        transpose=op.transpose, target_key=op.target_key,
        target_scale=op.target_scale, is_variation=op.is_variation,
    )
    app.state.placements.append(pl)
    return pl.id


def _h_move_placement(app, op):
    pl = app.state.find_placement(op.placement_id)
    pl.time = op.new_time
    if op.new_track_id is not None:
        pl.track_id = op.new_track_id
    return None


def _h_set_pl_repeats(app, op):
    app.state.find_placement(op.placement_id).repeats = op.repeats
    return None


def _h_set_pl_transpose(app, op):
    app.state.find_placement(op.placement_id).transpose = op.transpose
    return None


def _h_delete_placement(app, op):
    app.state.placements = [
        p for p in app.state.placements if p.id != op.placement_id
    ]
    return None


def _h_create_variation(app, op):
    var = var_ops.create_variation(app.state, op.parent_pattern_id)
    if var is None:
        return None
    if op.name:
        var.name = op.name
    return var.id


def _h_delete_variation(app, op):
    var_ops.delete_variation(app.state, op.variation_id)
    return None


def _h_flatten_variation(app, op):
    new = var_ops.flatten_variation(app.state, op.variation_id)
    return new.id if new else None


def _h_var_add_note(app, op):
    var = app.state.find_variation(op.variation_id)
    added = var_ops.variation_add_note(
        app.state, var, pitch=op.pitch, start=op.start,
        duration=op.duration, velocity=op.velocity, lyric=op.lyric,
    )
    if op.tags:
        added.tags = dict(op.tags)
    return added.note_id


def _h_var_delete_note(app, op):
    var = app.state.find_variation(op.variation_id)
    # Handle either parent-note deletion or removing an added note.
    if any(a.note_id == op.note_id for a in var.additions):
        var_ops.variation_remove_added_note(var, op.note_id)
    else:
        var_ops.variation_delete_note(var, op.note_id)
    return None


def _h_var_modify_note(app, op):
    var = app.state.find_variation(op.variation_id)
    kwargs = {}
    if op.d_start: kwargs['d_start'] = op.d_start
    if op.d_duration: kwargs['d_duration'] = op.d_duration
    if op.d_pitch: kwargs['d_pitch'] = op.d_pitch
    if op.d_velocity: kwargs['d_velocity'] = op.d_velocity
    var_ops.variation_modify_note(var, op.note_id, **kwargs)
    return None


def _h_var_split_note(app, op):
    var = app.state.find_variation(op.variation_id)
    if any(a.note_id == op.note_id for a in var.additions):
        right = var_ops.variation_split_added_note(
            app.state, var, op.note_id, op.split_offset)
        return right.note_id if right else None
    sp = var_ops.variation_record_split(
        app.state, var, op.note_id, op.split_offset)
    return sp.right_note_id if sp else None


def _h_create_track(app, op):
    t = Track(
        id=app.state.new_id(), name=op.name, channel=op.channel,
        bank=op.bank, program=op.program, volume=op.volume,
    )
    app.state.tracks.append(t)
    return t.id


def _h_rename_track(app, op):
    app.state.find_track(op.track_id).name = op.name
    return None


def _h_delete_track(app, op):
    trk_ops.delete_track(app.state, op.track_id)
    return None


def _h_set_track_inst(app, op):
    t = app.state.find_track(op.track_id)
    t.bank = op.bank
    t.program = op.program
    return None


def _h_set_track_vol(app, op):
    app.state.find_track(op.track_id).volume = op.volume
    return None


def _h_create_auto_track(app, op):
    t = AutomationTrack(id=app.state.new_id(), name=op.name, target=op.target)
    app.state.automation_tracks.append(t)
    return t.id


def _h_delete_auto_track(app, op):
    app.state.automation_tracks = [
        t for t in app.state.automation_tracks if t.id != op.track_id
    ]
    app.state.automation_placements = [
        p for p in app.state.automation_placements if p.track_id != op.track_id
    ]
    return None


def _h_rename_auto_track(app, op):
    app.state.find_automation_track(op.track_id).name = op.name
    return None


def _h_create_auto_pattern(app, op):
    p = AutomationPattern(
        id=app.state.new_id(), name=op.name, length=op.length,
        color=op.color, min_value=op.min_value, max_value=op.max_value,
    )
    app.state.automation_patterns.append(p)
    return p.id


def _h_delete_auto_pattern(app, op):
    app.state.automation_patterns = [
        p for p in app.state.automation_patterns if p.id != op.pattern_id
    ]
    app.state.automation_placements = [
        p for p in app.state.automation_placements if p.pattern_id != op.pattern_id
    ]
    return None


def _h_resize_auto_pattern(app, op):
    app.state.find_automation_pattern(op.pattern_id).length = op.new_length
    return None


def _h_set_auto_points(app, op):
    pat = app.state.find_automation_pattern(op.pattern_id)
    pat.points = [AutomationPoint(time=t, value=v, curve=c)
                  for (t, v, c) in op.points]
    return None


def _h_create_auto_pl(app, op):
    pl = AutomationPlacement(
        id=app.state.new_id(), track_id=op.track_id,
        pattern_id=op.pattern_id, time=op.time, repeats=op.repeats,
    )
    app.state.automation_placements.append(pl)
    return pl.id


def _h_move_auto_pl(app, op):
    pl = app.state.find_automation_placement(op.placement_id)
    pl.time = op.new_time
    if op.new_track_id is not None:
        pl.track_id = op.new_track_id
    return None


def _h_delete_auto_pl(app, op):
    app.state.automation_placements = [
        p for p in app.state.automation_placements if p.id != op.placement_id
    ]
    return None


def _h_create_beat_pat(app, op):
    color = op.color or PALETTE[len(app.state.beat_patterns) % len(PALETTE)]
    grid = {inst.id: [0] * (int(op.length) * op.subdivision)
            for inst in app.state.beat_kit}
    p = BeatPattern(
        id=app.state.new_id(), name=op.name, length=op.length,
        subdivision=op.subdivision, color=color, grid=grid,
    )
    app.state.beat_patterns.append(p)
    return p.id


def _h_delete_beat_pat(app, op):
    pat_ops.delete_beat_pattern(app.state, op.pattern_id)
    return None


def _h_resize_beat_pat(app, op):
    app.state.find_beat_pattern(op.pattern_id).length = op.new_length
    return None


def _h_set_beat_step(app, op):
    pat = app.state.find_beat_pattern(op.pattern_id)
    row = pat.grid.setdefault(op.inst_id,
                              [0] * int(pat.length * pat.subdivision))
    if op.step >= len(row):
        row.extend([0] * (op.step + 1 - len(row)))
    row[op.step] = op.velocity
    return None


def _h_set_beat_row(app, op):
    pat = app.state.find_beat_pattern(op.pattern_id)
    pat.grid[op.inst_id] = list(op.velocities)
    return None


def _h_create_beat_pl(app, op):
    pl = BeatPlacement(
        id=app.state.new_id(), track_id=op.track_id,
        pattern_id=op.pattern_id, time=op.time, repeats=op.repeats,
    )
    app.state.beat_placements.append(pl)
    return pl.id


def _h_move_beat_pl(app, op):
    pl = app.state.find_beat_placement(op.placement_id)
    pl.time = op.new_time
    if op.new_track_id is not None:
        pl.track_id = op.new_track_id
    return None


def _h_set_beat_pl_repeats(app, op):
    app.state.find_beat_placement(op.placement_id).repeats = op.repeats
    return None


def _h_delete_beat_pl(app, op):
    app.state.beat_placements = [
        p for p in app.state.beat_placements if p.id != op.placement_id
    ]
    return None


_HANDLERS = {
    O.AddNote: _h_add_note, O.MoveNote: _h_move_note,
    O.ResizeNote: _h_resize_note, O.DeleteNote: _h_delete_note,
    O.SetNoteVelocity: _h_set_velocity, O.SetNoteLyric: _h_set_lyric,
    O.SetNoteBend: _h_set_bend, O.SplitNote: _h_split_note,

    O.CreatePattern: _h_create_pattern, O.RenamePattern: _h_rename_pattern,
    O.ResizePattern: _h_resize_pattern, O.DeletePattern: _h_delete_pattern,
    O.DuplicatePattern: _h_duplicate_pattern,
    O.SetPatternKeyScale: _h_set_pat_keyscale,

    O.CreatePlacement: _h_create_placement,
    O.MovePlacement: _h_move_placement,
    O.SetPlacementRepeats: _h_set_pl_repeats,
    O.SetPlacementTranspose: _h_set_pl_transpose,
    O.DeletePlacement: _h_delete_placement,

    O.CreateVariation: _h_create_variation,
    O.DeleteVariation: _h_delete_variation,
    O.FlattenVariation: _h_flatten_variation,
    O.VariationAddNote: _h_var_add_note,
    O.VariationDeleteNote: _h_var_delete_note,
    O.VariationModifyNote: _h_var_modify_note,
    O.VariationSplitNote: _h_var_split_note,

    O.CreateTrack: _h_create_track, O.RenameTrack: _h_rename_track,
    O.DeleteTrack: _h_delete_track,
    O.SetTrackInstrument: _h_set_track_inst,
    O.SetTrackVolume: _h_set_track_vol,

    O.CreateAutomationTrack: _h_create_auto_track,
    O.DeleteAutomationTrack: _h_delete_auto_track,
    O.RenameAutomationTrack: _h_rename_auto_track,
    O.CreateAutomationPattern: _h_create_auto_pattern,
    O.DeleteAutomationPattern: _h_delete_auto_pattern,
    O.ResizeAutomationPattern: _h_resize_auto_pattern,
    O.SetAutomationPoints: _h_set_auto_points,
    O.CreateAutomationPlacement: _h_create_auto_pl,
    O.MoveAutomationPlacement: _h_move_auto_pl,
    O.DeleteAutomationPlacement: _h_delete_auto_pl,

    O.CreateBeatPattern: _h_create_beat_pat,
    O.DeleteBeatPattern: _h_delete_beat_pat,
    O.ResizeBeatPattern: _h_resize_beat_pat,
    O.SetBeatStep: _h_set_beat_step,
    O.SetBeatRow: _h_set_beat_row,
    O.CreateBeatPlacement: _h_create_beat_pl,
    O.MoveBeatPlacement: _h_move_beat_pl,
    O.SetBeatPlacementRepeats: _h_set_beat_pl_repeats,
    O.DeleteBeatPlacement: _h_delete_beat_pl,
}


# ---------------------------------------------------------------------------
# Fallback undo_group for tests / headless use
# ---------------------------------------------------------------------------

@contextmanager
def _fallback_undo_group(app, label: str):
    """Used when the app lacks undo_group (e.g. a bare state or test shim).

    Captures one snapshot after the batch if the app has an undo_stack.
    """
    if hasattr(app, 'undo_stack'):
        from ..undo import capture_state
        yield
        app.undo_stack.push(capture_state(app.state))
    else:
        yield


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def apply_ops(ops: Sequence, app, label: str) -> List[Any]:
    """Apply a list of operations as one undo step.

    Two phases. Phase 1 validates every op against ``app.state`` without
    mutating; on any failure raises ``OperationError`` with the op's
    index. Phase 2 mutates inside a single ``undo_group(label)`` so the
    whole batch becomes one undo entry.

    Returns a list parallel to ``ops``: each element is the newly
    allocated integer id for Create* ops, and ``None`` for all others.
    """
    if not isinstance(ops, (list, tuple)):
        ops = list(ops)

    state = app.state
    _validate(state, ops)

    # Acquire undo group — prefer the app's own context manager.
    undo_cm = getattr(app, 'undo_group', None)
    if undo_cm is None:
        undo_cm = lambda lbl: _fallback_undo_group(app, lbl)

    results: List[Any] = []
    with undo_cm(label):
        for op in ops:
            handler = _HANDLERS[type(op)]
            results.append(handler(app, op))
    # Ops mutate state dicts directly; fire one notify so the app's UI
    # (arranger, piano roll, panels) refreshes. Source 'plugin_apply' is
    # outside _undo_triggers — the undo snapshot was already pushed by
    # undo_group — but _on_state_change still schedules a UI refresh.
    notify = getattr(state, 'notify', None)
    if callable(notify):
        notify(source='plugin_apply')
    return results
