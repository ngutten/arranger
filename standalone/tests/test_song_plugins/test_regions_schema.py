"""Tests for the regions schema validator and tag-value contract."""

from __future__ import annotations

import pytest

from standalone.song_plugins.schemas import validate


# ---------------------------------------------------------------------------
# regions validator
# ---------------------------------------------------------------------------

def test_regions_accepts_minimal_box():
    data = [{"start_beat": 0.0, "end_beat": 2.0}]
    ok, why = validate("regions", data)
    assert ok, why


def test_regions_accepts_full_box():
    data = [{
        "start_beat": 0.0, "end_beat": 2.0,
        "min_pitch": 60, "max_pitch": 72,
        "note_ids": (1, 2, 3),
        "color": "#ff0000",
        "label": "V7",
        "payload": {"foo": "bar"},
        "pattern_id": 42,
    }]
    ok, why = validate("regions", data)
    assert ok, why


def test_regions_rejects_non_list():
    ok, why = validate("regions", {"start_beat": 0.0})
    assert not ok


def test_regions_rejects_missing_start():
    ok, why = validate("regions", [{"end_beat": 2.0}])
    assert not ok
    assert "start_beat" in why


def test_regions_rejects_missing_end():
    ok, why = validate("regions", [{"start_beat": 0.0}])
    assert not ok


def test_regions_rejects_end_before_start():
    ok, why = validate("regions", [{"start_beat": 2.0, "end_beat": 1.0}])
    assert not ok


def test_regions_rejects_non_int_pitch():
    ok, why = validate("regions", [{
        "start_beat": 0.0, "end_beat": 1.0, "min_pitch": 60.5,
    }])
    assert not ok


def test_regions_accepts_null_pitch():
    ok, why = validate("regions", [{
        "start_beat": 0.0, "end_beat": 1.0,
        "min_pitch": None, "max_pitch": None,
    }])
    assert ok


def test_regions_rejects_non_int_note_ids():
    ok, why = validate("regions", [{
        "start_beat": 0.0, "end_beat": 1.0, "note_ids": [1, "bad"],
    }])
    assert not ok


def test_regions_rejects_non_int_pattern_id():
    ok, why = validate("regions", [{
        "start_beat": 0.0, "end_beat": 1.0, "pattern_id": "pat"
    }])
    assert not ok


def test_regions_accepts_empty_list():
    ok, _ = validate("regions", [])
    assert ok


# ---------------------------------------------------------------------------
# note_tags / placement_tags — str-or-dict contract
# ---------------------------------------------------------------------------

def test_note_tags_accepts_string_values():
    ok, _ = validate("note_tags", {1: "V7", 2: "chord-tone"})
    assert ok


def test_note_tags_accepts_dict_values():
    ok, why = validate("note_tags", {
        1: {"label": "V7", "color": "#ff0000", "priority": 5,
            "payload": {"notes": "dominant-seventh"}},
    })
    assert ok, why


def test_note_tags_rejects_dict_without_label():
    ok, why = validate("note_tags", {1: {"color": "#ff0000"}})
    assert not ok
    assert "label" in why


def test_note_tags_rejects_non_string_label():
    ok, _ = validate("note_tags", {1: {"label": 123}})
    assert not ok


def test_note_tags_rejects_non_string_color():
    ok, _ = validate("note_tags", {1: {"label": "V7", "color": 0xff0000}})
    assert not ok


def test_note_tags_rejects_non_int_priority():
    ok, _ = validate("note_tags", {1: {"label": "V7", "priority": "high"}})
    assert not ok


def test_note_tags_rejects_non_int_keys():
    ok, _ = validate("note_tags", {"one": "V7"})
    assert not ok


def test_note_tags_rejects_bad_value_type():
    ok, _ = validate("note_tags", {1: 42})
    assert not ok


def test_placement_tags_validates_values_per_entry():
    # Ensure the loop actually iterates (prior bug: check outside loop).
    ok, _ = validate("placement_tags", {1: "ok", 2: {"no_label": True}})
    assert not ok
