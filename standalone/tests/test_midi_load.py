"""Tests for the MIDI loader (core/midi_load.py).

Covers parsing, the flat one-pattern-per-track Pass 1 build, and the
Pass 2 segmentation that detects repeated measure-aligned chunks.
"""

import os
import tempfile

import pytest

from standalone.core.midi import create_midi
from standalone.core.midi_load import (
    parse_midi, midi_to_arrangement, segment_track, import_midi,
)
from standalone.state import AppState, Note


def _build_arr(bpm=120, ts_num=4, ts_den=4, tracks=None):
    return {
        'bpm': bpm, 'tsNum': ts_num, 'tsDen': ts_den,
        'tracks': tracks or [],
    }


def _track(name, channel, program, placements):
    return {
        'name': name, 'channel': channel, 'bank': 0, 'program': program,
        'volume': 100, 'placements': placements,
    }


def _placement(notes, length=4.0, time=0.0, transpose=0, repeats=1):
    return {
        'pattern': {'notes': notes, 'length': length},
        'time': time, 'transpose': transpose, 'repeats': repeats,
    }


def _note(pitch, start, duration, velocity=100):
    return {'pitch': pitch, 'start': start, 'duration': duration,
            'velocity': velocity}


# ---------------------------------------------------------------------------
# Parser sanity
# ---------------------------------------------------------------------------

def test_parse_midi_minimal_header():
    arr = _build_arr(tracks=[_track('Piano', 0, 0, [
        _placement([_note(60, 0, 1)]),
    ])])
    data = create_midi(arr)
    parsed = parse_midi(data)
    assert parsed['format'] == 1
    assert parsed['tpb'] == 480
    # tempo + time-sig track plus one note track
    assert len(parsed['tracks']) == 2


def test_parse_midi_rejects_garbage():
    with pytest.raises(ValueError):
        parse_midi(b'not a midi file')


# ---------------------------------------------------------------------------
# Pass 1: round-trip
# ---------------------------------------------------------------------------

def test_round_trip_single_track():
    arr = _build_arr(bpm=140, tracks=[_track('Piano', 0, 0, [
        _placement([
            _note(60, 0.0, 1.0, 100),
            _note(64, 1.0, 0.5, 80),
            _note(67, 2.0, 2.0, 120),
        ], length=4.0),
    ])])
    parsed = parse_midi(create_midi(arr))
    out = midi_to_arrangement(parsed)
    assert out['bpm'] == 140
    assert len(out['tracks']) == 1
    t = out['tracks'][0]
    assert t['channel'] == 0
    assert t['program'] == 0
    pitches = sorted(n.pitch for n in t['notes'])
    assert pitches == [60, 64, 67]
    by_pitch = {n.pitch: n for n in t['notes']}
    assert by_pitch[60].start == pytest.approx(0.0)
    assert by_pitch[60].duration == pytest.approx(1.0)
    assert by_pitch[60].velocity == 100
    assert by_pitch[64].start == pytest.approx(1.0)
    assert by_pitch[64].duration == pytest.approx(0.5)
    assert by_pitch[67].start == pytest.approx(2.0)
    assert by_pitch[67].duration == pytest.approx(2.0)


def test_round_trip_two_tracks_different_channels():
    arr = _build_arr(tracks=[
        _track('Piano', 0, 0, [_placement([_note(60, 0, 1)])]),
        _track('Bass', 1, 32, [_placement([_note(36, 0, 2)])]),
    ])
    parsed = parse_midi(create_midi(arr))
    out = midi_to_arrangement(parsed)
    assert len(out['tracks']) == 2
    by_ch = {t['channel']: t for t in out['tracks']}
    assert by_ch[0]['program'] == 0
    assert by_ch[1]['program'] == 32
    assert by_ch[1]['notes'][0].pitch == 36


def test_drum_track_named_drums_when_no_meta_name():
    # Our writer emits a track-name meta event for every track. Confirm that
    # a channel-9 track without that meta lands as 'Drums'.
    arr = _build_arr(tracks=[_track('', 9, 0, [_placement([_note(36, 0, 0.25)])])])
    parsed = parse_midi(create_midi(arr))
    out = midi_to_arrangement(parsed)
    assert len(out['tracks']) == 1
    # The writer emits an empty track-name; since stripping yields '', the
    # loader should fall back to 'Drums' for channel 9.
    assert out['tracks'][0]['channel'] == 9
    assert out['tracks'][0]['name'] == 'Drums'


# ---------------------------------------------------------------------------
# Pass 2: segmentation
# ---------------------------------------------------------------------------

def test_segment_finds_two_measure_repeat():
    # Pattern: 4 measures, but identical content every 2 measures.
    notes = []
    for cycle in range(2):
        base = cycle * 8.0  # 2 measures = 8 beats in 4/4
        notes.append(Note(pitch=60, start=base + 0.0, duration=1.0, velocity=100))
        notes.append(Note(pitch=64, start=base + 1.0, duration=1.0, velocity=100))
        notes.append(Note(pitch=67, start=base + 4.0, duration=2.0, velocity=100))
    segments = segment_track(notes, 16.0, ts_num=4)
    assert segments is not None
    # Should collapse to one 8-beat sub-pattern with 2 repeats.
    assert len(segments) == 1
    sub_notes, sub_len, start_beat, repeats = segments[0]
    assert sub_len == 8.0
    assert start_beat == 0.0
    assert repeats == 2
    # The sub-pattern should have 3 notes.
    assert len(sub_notes) == 3


def test_segment_returns_none_when_no_repeats():
    # Distinct notes in every measure; nothing should segment.
    notes = [
        Note(pitch=60, start=0.0, duration=1.0),
        Note(pitch=62, start=4.0, duration=1.0),
        Note(pitch=64, start=8.0, duration=1.0),
        Note(pitch=65, start=12.0, duration=1.0),
    ]
    assert segment_track(notes, 16.0, ts_num=4) is None


def test_segment_handles_gap_between_repeats():
    # Pattern A at measures 0-1, silence in measure 2-3, pattern A again at 4-5.
    notes = []
    for start_measure in (0, 4):
        base = start_measure * 4.0
        notes.append(Note(pitch=60, start=base, duration=1.0))
        notes.append(Note(pitch=64, start=base + 1.0, duration=1.0))
    segments = segment_track(notes, 24.0, ts_num=4)  # 6 measures
    assert segments is not None
    # Empty chunks are dropped; non-empty A appears at 0 and 16, but with
    # 1 measure chunks A may be 1 measure long; with 2 measure chunks A is 2.
    # Either way: total of 2 placements (or one with reps=1 each).
    total_placements = sum(reps for _, _, _, reps in segments)
    assert total_placements == 2


def test_segment_skips_chunks_with_overrunning_notes():
    # A 3-beat sustain inside a 4-beat measure is fine, but a 5-beat sustain
    # would prevent 1-measure segmentation. Segmentation should still find
    # the 2-measure pattern that holds the sustain inside it.
    notes = []
    for cycle in range(2):
        base = cycle * 8.0
        notes.append(Note(pitch=60, start=base, duration=5.0))
        notes.append(Note(pitch=64, start=base + 6.0, duration=1.0))
    segments = segment_track(notes, 16.0, ts_num=4)
    assert segments is not None
    sub_notes, sub_len, _, repeats = segments[0]
    assert sub_len == 8.0
    assert repeats == 2


# ---------------------------------------------------------------------------
# import_midi end-to-end
# ---------------------------------------------------------------------------

def _write_temp_midi(arr):
    fd, path = tempfile.mkstemp(suffix='.mid')
    os.close(fd)
    with open(path, 'wb') as f:
        f.write(create_midi(arr))
    return path


def test_import_midi_populates_state():
    arr = _build_arr(tracks=[
        _track('Piano', 0, 0, [_placement([
            _note(60, 0, 1), _note(64, 1, 1)
        ], length=4.0)]),
        _track('Bass', 1, 32, [_placement([
            _note(36, 0, 2)
        ], length=4.0)]),
    ])
    path = _write_temp_midi(arr)
    try:
        s = AppState()
        stats = import_midi(s, path, segment=False)
    finally:
        os.unlink(path)
    assert stats['tracks'] == 2
    assert stats['patterns'] == 2
    assert stats['placements'] == 2
    assert {t.name for t in s.tracks} == {'Piano', 'Bass'}
    # Each track gets one placement at time 0
    times = sorted(p.time for p in s.placements)
    assert times == [0.0, 0.0]


def test_import_midi_with_segmentation_collapses_repeats():
    # 4-bar piano pattern that's actually 2x a 2-bar idea.
    repeated_notes = []
    for cycle in range(2):
        base = cycle * 8.0
        repeated_notes.append(_note(60, base, 1))
        repeated_notes.append(_note(64, base + 4, 1))
    arr = _build_arr(tracks=[
        _track('Piano', 0, 0, [_placement(repeated_notes, length=16.0)]),
    ])
    path = _write_temp_midi(arr)
    try:
        s = AppState()
        stats = import_midi(s, path, segment=True)
    finally:
        os.unlink(path)
    assert stats['tracks'] == 1
    assert stats['segmented_tracks'] == 1
    # One pattern, one placement with repeats=2
    assert len(s.patterns) == 1
    assert len(s.placements) == 1
    assert s.placements[0].repeats == 2
    assert s.patterns[0].length == 8.0


def test_import_midi_replaces_existing_state():
    # First import seeds state, second import replaces it.
    a = _build_arr(tracks=[_track('Piano', 0, 0, [_placement([_note(60, 0, 1)])])])
    b = _build_arr(tracks=[
        _track('Strings', 2, 48, [_placement([_note(72, 0, 0.5)])]),
        _track('Brass', 3, 56, [_placement([_note(48, 0, 1)])]),
    ])
    pa = _write_temp_midi(a)
    pb = _write_temp_midi(b)
    try:
        s = AppState()
        import_midi(s, pa, segment=False)
        assert len(s.tracks) == 1
        import_midi(s, pb, segment=False)
    finally:
        os.unlink(pa); os.unlink(pb)
    assert len(s.tracks) == 2
    assert {t.name for t in s.tracks} == {'Strings', 'Brass'}
