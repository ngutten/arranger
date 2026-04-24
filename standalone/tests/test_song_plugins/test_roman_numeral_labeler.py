"""Tests for the Roman Numeral Labeler plugin."""

from __future__ import annotations

import pytest

from standalone.state import AppState, Pattern, Note, Track, Placement
from standalone.song_plugins.song_view import SongView
from standalone.song_plugins.api import SelectionSnapshot
from standalone.song_plugins.builtin.roman_numeral_labeler import (
    RomanNumeralLabelerPlugin, _parse_key,
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


def _mk_state_with_pattern(notes, key="C", scale="major", pat_len=4.0):
    s = AppState()
    s.bpm = 120.0
    t = Track(id=s.new_id(), name="T", channel=0)
    s.tracks.append(t)
    for n in notes:
        if n.note_id == 0:
            n.note_id = s.new_id()
    p = Pattern(id=s.new_id(), name="P", length=pat_len, notes=list(notes),
                color='#fff', key=key, scale=scale)
    s.patterns.append(p)
    pl = Placement(id=s.new_id(), track_id=t.id, pattern_id=p.id,
                   time=0.0, repeats=1)
    s.placements.append(pl)
    return s, p


# ---------------------------------------------------------------------------
# _parse_key helper
# ---------------------------------------------------------------------------

def test_parse_key_major():
    assert _parse_key("C", "major") == (0, "maj")
    assert _parse_key("D", "major") == (2, "maj")
    assert _parse_key("F#", "major") == (6, "maj")


def test_parse_key_minor():
    assert _parse_key("A", "minor") == (9, "min")
    assert _parse_key("A", "natural minor") == (9, "min")
    assert _parse_key("C", "harmonic minor") == (0, "min")


def test_parse_key_enharmonic():
    assert _parse_key("Db", "major") == (1, "maj")
    assert _parse_key("Eb", "minor") == (3, "min")


def test_parse_key_unknown_returns_none():
    assert _parse_key("???", "major") is None


# ---------------------------------------------------------------------------
# Labeler end-to-end
# ---------------------------------------------------------------------------

def test_c_major_chord_in_c_major_labeled_I():
    # Sustained C major triad in a pattern declared as C major.
    notes = [
        Note(pitch=60, start=0.0, duration=4.0, velocity=100),
        Note(pitch=64, start=0.0, duration=4.0, velocity=100),
        Note(pitch=67, start=0.0, duration=4.0, velocity=100),
    ]
    s, _pat = _mk_state_with_pattern(notes, key="C", scale="major")
    view = SongView(s, _empty_selection())
    plugin = RomanNumeralLabelerPlugin()
    result = plugin.run(view, {
        "window_beats": 1.0, "min_confidence": 0.0, "show_symbols": True,
    }, _DummyProgress())
    ann = result.annotation
    assert ann.schema == "regions"
    assert ann.data  # non-empty
    # At least one region with roman "I" in its label.
    romans = [r["payload"]["quality"] for r in ann.data
              if r["payload"]["key"] == (0, "maj")]
    # Somewhere in the output a major-quality chord labeled I or similar.
    assert any(q == "maj" for q in romans)
    # The label field should contain "I" for the tonic chord.
    labels = [r["label"] for r in ann.data]
    assert any("I" in lbl for lbl in labels)


def test_g_major_chord_in_c_major_labeled_V():
    # Sustained G major triad in C major pattern -> V.
    notes = [
        Note(pitch=55, start=0.0, duration=4.0, velocity=100),  # G
        Note(pitch=59, start=0.0, duration=4.0, velocity=100),  # B
        Note(pitch=62, start=0.0, duration=4.0, velocity=100),  # D
    ]
    s, _pat = _mk_state_with_pattern(notes, key="C", scale="major")
    view = SongView(s, _empty_selection())
    plugin = RomanNumeralLabelerPlugin()
    result = plugin.run(view, {
        "window_beats": 2.0, "min_confidence": 0.0, "show_symbols": False,
    }, _DummyProgress())
    # show_symbols=False so label is *just* the roman numeral.
    labels = [r["label"] for r in result.annotation.data]
    assert any(lbl == "V" for lbl in labels)


def test_regions_carry_pattern_id():
    notes = [
        Note(pitch=60, start=0.0, duration=4.0, velocity=100),
        Note(pitch=64, start=0.0, duration=4.0, velocity=100),
        Note(pitch=67, start=0.0, duration=4.0, velocity=100),
    ]
    s, pat = _mk_state_with_pattern(notes)
    view = SongView(s, _empty_selection())
    plugin = RomanNumeralLabelerPlugin()
    result = plugin.run(view, {}, _DummyProgress())
    for r in result.annotation.data:
        assert r["pattern_id"] == pat.id


def test_no_placements_emits_no_regions():
    # Pattern exists but nothing references it — nothing to label.
    s = AppState()
    s.bpm = 120.0
    t = Track(id=s.new_id(), name="T", channel=0)
    s.tracks.append(t)
    p = Pattern(id=s.new_id(), name="P", length=4.0, notes=[
        Note(pitch=60, start=0.0, duration=4.0, velocity=100,
             note_id=s.new_id()),
    ], color='#fff', key='C', scale='major')
    s.patterns.append(p)
    view = SongView(s, _empty_selection())
    plugin = RomanNumeralLabelerPlugin()
    result = plugin.run(view, {}, _DummyProgress())
    assert result.annotation.data == []


def test_unknown_key_still_labels_with_chord_symbol_only():
    # A pattern with a malformed key string still gets chord symbols
    # when show_symbols=True (no roman numeral possible).
    notes = [
        Note(pitch=60, start=0.0, duration=4.0, velocity=100),
        Note(pitch=64, start=0.0, duration=4.0, velocity=100),
        Note(pitch=67, start=0.0, duration=4.0, velocity=100),
    ]
    s, _pat = _mk_state_with_pattern(notes, key="???", scale="major")
    view = SongView(s, _empty_selection())
    plugin = RomanNumeralLabelerPlugin()
    result = plugin.run(view, {"show_symbols": True}, _DummyProgress())
    labels = [r["label"] for r in result.annotation.data]
    assert any("C" in lbl for lbl in labels)


def test_schema_validator_accepts_output():
    from standalone.song_plugins.schemas import validate
    notes = [
        Note(pitch=60, start=0.0, duration=4.0, velocity=100),
        Note(pitch=64, start=0.0, duration=4.0, velocity=100),
        Note(pitch=67, start=0.0, duration=4.0, velocity=100),
    ]
    s, _pat = _mk_state_with_pattern(notes)
    view = SongView(s, _empty_selection())
    plugin = RomanNumeralLabelerPlugin()
    result = plugin.run(view, {}, _DummyProgress())
    ok, why = validate("regions", result.annotation.data)
    assert ok, why
