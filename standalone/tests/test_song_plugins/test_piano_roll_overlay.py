"""Tests for PianoRollOverlay (data holder; no Qt painting involved)."""

from __future__ import annotations

import pytest

from standalone.song_plugins.api import Annotation
from standalone.song_plugins.ui.piano_roll_overlay import (
    PianoRollOverlay, tag_label, tag_color,
)


def _mk_ann(schema, data, **kw):
    return Annotation(
        id="test/1", plugin_id="test", instance_id="1",
        title="t", schema=schema, data=data,
        render_hint=kw.pop("render_hint", {}),
        **kw,
    )


# ---------------------------------------------------------------------------
# Regions indexing
# ---------------------------------------------------------------------------

def test_regions_indexed_by_pattern_id(qtbot):
    ov = PianoRollOverlay()
    ann = _mk_ann("regions", [
        {"start_beat": 0.0, "end_beat": 1.0, "pattern_id": 10, "label": "I"},
        {"start_beat": 1.0, "end_beat": 2.0, "pattern_id": 10, "label": "V"},
        {"start_beat": 0.0, "end_beat": 1.0, "pattern_id": 20, "label": "i"},
    ])
    ov.set_broadcaster(None, ann)
    r10 = ov.regions_for_pattern(10)
    assert len(r10) == 2
    assert {r["label"] for r in r10} == {"I", "V"}
    r20 = ov.regions_for_pattern(20)
    assert len(r20) == 1 and r20[0]["label"] == "i"
    r99 = ov.regions_for_pattern(99)
    assert r99 == []


def test_regions_global_appear_for_every_pattern(qtbot):
    ov = PianoRollOverlay()
    ann = _mk_ann("regions", [
        {"start_beat": 0.0, "end_beat": 1.0, "label": "global"},
        {"start_beat": 0.0, "end_beat": 1.0, "pattern_id": 10,
         "label": "local"},
    ])
    ov.set_broadcaster(None, ann)
    r10 = ov.regions_for_pattern(10)
    labels = {r["label"] for r in r10}
    assert labels == {"global", "local"}
    r99 = ov.regions_for_pattern(99)
    assert [r["label"] for r in r99] == ["global"]


def test_regions_variation_scoped(qtbot):
    ov = PianoRollOverlay()
    ann = _mk_ann("regions", [
        {"start_beat": 0.0, "end_beat": 1.0, "variation_id": 7, "label": "V"},
    ])
    ov.set_broadcaster(None, ann)
    # Asking by pattern alone doesn't surface the variation region.
    assert ov.regions_for_pattern(99) == []
    # Passing variation_id does.
    labels = [r["label"] for r in ov.regions_for_pattern(99, variation_id=7)]
    assert labels == ["V"]


# ---------------------------------------------------------------------------
# Note tags / placement tags
# ---------------------------------------------------------------------------

def test_note_tags_round_trip(qtbot):
    ov = PianoRollOverlay()
    ann = _mk_ann("note_tags", {1: "V7", 2: {"label": "chord-tone", "color": "#0f0"}})
    ov.set_broadcaster(None, ann)
    assert ov.has_note_tags()
    assert ov.note_tag_for(1) == "V7"
    tag_two = ov.note_tag_for(2)
    assert tag_label(tag_two) == "chord-tone"
    assert tag_color(tag_two) == "#0f0"
    assert ov.note_tag_for(99) is None


def test_placement_tags_round_trip(qtbot):
    ov = PianoRollOverlay()
    ann = _mk_ann("placement_tags", {5: "phrase-A"})
    ov.set_broadcaster(None, ann)
    assert ov.has_placement_tags()
    assert ov.placement_tag_for(5) == "phrase-A"


# ---------------------------------------------------------------------------
# Schema switches clear previous state
# ---------------------------------------------------------------------------

def test_switching_schema_replaces_data(qtbot):
    ov = PianoRollOverlay()
    ov.set_broadcaster(None, _mk_ann("note_tags", {1: "a"}))
    assert ov.note_tag_for(1) == "a"
    ov.set_broadcaster(None, _mk_ann("regions", [
        {"start_beat": 0.0, "end_beat": 1.0, "pattern_id": 42, "label": "X"},
    ]))
    assert ov.note_tag_for(1) is None   # tags cleared
    assert ov.has_regions()
    assert [r["label"] for r in ov.regions_for_pattern(42)] == ["X"]


def test_clear_resets_everything(qtbot):
    ov = PianoRollOverlay()
    ov.set_broadcaster(None, _mk_ann("regions", [
        {"start_beat": 0.0, "end_beat": 1.0, "pattern_id": 1, "label": "X"},
    ]))
    ov.clear()
    assert not ov.has_regions()
    assert ov.regions_for_pattern(1) == []
    assert ov.current_schema() is None


# ---------------------------------------------------------------------------
# Signals fire on mutations
# ---------------------------------------------------------------------------

def test_changed_emits_on_set_update_clear(qtbot):
    ov = PianoRollOverlay()
    calls = []
    ov.changed.connect(lambda: calls.append(1))
    ov.set_broadcaster(None, _mk_ann("regions", []))
    ov.update_annotation(_mk_ann("regions", [
        {"start_beat": 0.0, "end_beat": 1.0, "label": "x"},
    ]))
    ov.clear()
    assert len(calls) == 3


# ---------------------------------------------------------------------------
# Tag helpers
# ---------------------------------------------------------------------------

def test_tag_helpers():
    assert tag_label("hello") == "hello"
    assert tag_label({"label": "foo", "color": "#fff"}) == "foo"
    assert tag_label({"no_label": True}) == ""
    assert tag_color("hello") is None
    assert tag_color({"label": "foo", "color": "#abc"}) == "#abc"
    assert tag_color("hello", default="#000") == "#000"
