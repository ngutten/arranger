"""Phase 2 tests: per-note `attrs` data model, variation diffs, and schedule
emission of NoteAttr events (the Python side that feeds the C++ transport
verified in test_note_attr.py)."""

from standalone.state import (
    AppState, Track, Pattern, Note, Placement, NoteDelta, AddedNote, Variation,
)
from standalone.core.engine import build_schedule, EVT_NOTE_ATTR, EVT_NOTE_ON
from standalone.core.binding_engine import _build_server_schedule
from standalone.ops.variations import resolve_variation


# --------------------------------------------------------------------------
# Data model: serialization round-trips
# --------------------------------------------------------------------------

def test_note_attrs_roundtrip():
    n = Note(pitch=60, start=0.0, duration=1.0, velocity=90,
             attrs={"attack": 2.0, "excitation": 1.0})
    d = n.to_dict()
    assert d["attrs"] == {"attack": 2.0, "excitation": 1.0}
    n2 = Note.from_dict(d)
    assert n2.attrs == n.attrs
    # absent attrs serialize to nothing and load as {}
    bare = Note(pitch=60, start=0.0, duration=1.0)
    assert "attrs" not in bare.to_dict()
    assert Note.from_dict(bare.to_dict()).attrs == {}


def test_notedelta_attrs_roundtrip():
    dlt = NoteDelta(note_id=5, attrs={"attack": 0.5})
    assert dlt.to_dict()["attrs"] == {"attack": 0.5}
    assert NoteDelta.from_dict(dlt.to_dict()).attrs == {"attack": 0.5}
    # None = inherit, not serialized
    assert "attrs" not in NoteDelta(note_id=5).to_dict()
    assert NoteDelta.from_dict({"noteId": 5}).attrs is None


def test_addednote_attrs_roundtrip():
    a = AddedNote(note_id=9, pitch=62, start=0.0, duration=1.0,
                  attrs={"excitation": 1.0})
    assert a.to_dict()["attrs"] == {"excitation": 1.0}
    assert AddedNote.from_dict(a.to_dict()).attrs == {"excitation": 1.0}


# --------------------------------------------------------------------------
# Variation resolution
# --------------------------------------------------------------------------

def _state_with_variation():
    s = AppState()
    s.bpm = 120
    t = Track(id=s.new_id(), name="Gtr", channel=0, bank=0, program=0, volume=100)
    s.tracks.append(t)
    p = Pattern(id=s.new_id(), name="P", length=4.0, notes=[
        Note(pitch=60, start=0.0, duration=1.0, velocity=100, note_id=1,
             attrs={"attack": 2.0}),
        Note(pitch=64, start=1.0, duration=1.0, velocity=100, note_id=2),
    ], color="#fff", key="C", scale="major")
    s.patterns.append(p)
    return s, t, p


def test_variation_inherits_parent_attrs():
    s, t, p = _state_with_variation()
    var = Variation(id=s.new_id(), parent_id=p.id, name="V", color="#abc")
    s.variations.append(var)
    notes = resolve_variation(s, var.id)
    n60 = next(n for n in notes if n.pitch == 60)
    assert n60.attrs == {"attack": 2.0}, "parent attrs must propagate to variation"


def test_variation_delta_overrides_attrs():
    s, t, p = _state_with_variation()
    var = Variation(id=s.new_id(), parent_id=p.id, name="V", color="#abc",
                    modifications=[NoteDelta(note_id=1, attrs={"attack": 0.25})])
    s.variations.append(var)
    notes = resolve_variation(s, var.id)
    n60 = next(n for n in notes if n.pitch == 60)
    assert n60.attrs == {"attack": 0.25}, "delta attrs must override parent"


def test_variation_added_note_carries_attrs():
    s, t, p = _state_with_variation()
    var = Variation(id=s.new_id(), parent_id=p.id, name="V", color="#abc",
                    additions=[AddedNote(note_id=10, pitch=67, start=2.0,
                                         duration=1.0, attrs={"excitation": 1.0})])
    s.variations.append(var)
    notes = resolve_variation(s, var.id)
    n67 = next(n for n in notes if n.pitch == 67)
    assert n67.attrs == {"excitation": 1.0}


# --------------------------------------------------------------------------
# Schedule emission
# --------------------------------------------------------------------------

def _simple_attr_state():
    s = AppState()
    s.bpm = 120
    t = Track(id=s.new_id(), name="S", channel=0, bank=0, program=0, volume=100)
    s.tracks.append(t)
    p = Pattern(id=s.new_id(), name="P", length=4.0, notes=[
        Note(pitch=60, start=0.0, duration=1.0, velocity=100,
             attrs={"attack": 3.0, "excitation": 1.0}),
    ], color="#fff", key="C", scale="major")
    s.patterns.append(p)
    s.placements.append(Placement(id=s.new_id(), track_id=t.id,
                                  pattern_id=p.id, time=0.0, repeats=1))
    return s


def test_build_schedule_emits_note_attr():
    s = _simple_attr_state()
    events = build_schedule(s)
    attr_evts = [e for e in events if e.event_type == EVT_NOTE_ATTR]
    assert len(attr_evts) == 2, "expected one NoteAttr per attr key"
    by_id = {e.attr: e.value for e in attr_evts}
    assert by_id == {"attack": 3.0, "excitation": 1.0}
    # All attr events share the note's onset beat and pitch
    for e in attr_evts:
        assert e.beat == 0.0 and e.pitch == 60

    # NoteAttr must sort before the NoteOn at the same beat (latch-first).
    idx_attr = max(i for i, e in enumerate(events) if e.event_type == EVT_NOTE_ATTR)
    idx_on = next(i for i, e in enumerate(events)
                  if e.event_type == EVT_NOTE_ON and e.beat == 0.0)
    assert idx_attr < idx_on, "NoteAttr events must precede the NoteOn"


def test_server_schedule_emits_note_attr_json():
    s = _simple_attr_state()
    events = _build_server_schedule(s)
    attr_evts = [e for e in events if e.get("type") == "note_attr"]
    assert len(attr_evts) == 2
    for e in attr_evts:
        assert e["port_id"] in ("attack", "excitation")
        assert e["beat"] == 0.0 and e["pitch"] == 60
        assert "value" in e
    vals = {e["port_id"]: e["value"] for e in attr_evts}
    assert vals == {"attack": 3.0, "excitation": 1.0}


def test_no_attrs_emits_nothing():
    s = AppState()
    s.bpm = 120
    t = Track(id=s.new_id(), name="S", channel=0, bank=0, program=0, volume=100)
    s.tracks.append(t)
    p = Pattern(id=s.new_id(), name="P", length=4.0, notes=[
        Note(pitch=60, start=0.0, duration=1.0, velocity=100),
    ], color="#fff", key="C", scale="major")
    s.patterns.append(p)
    s.placements.append(Placement(id=s.new_id(), track_id=t.id,
                                  pattern_id=p.id, time=0.0, repeats=1))
    assert not [e for e in build_schedule(s) if e.event_type == EVT_NOTE_ATTR]
    assert not [e for e in _build_server_schedule(s) if e.get("type") == "note_attr"]
