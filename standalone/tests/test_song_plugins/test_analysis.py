"""Tests for song_plugins.analysis primitives."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from standalone.song_plugins.analysis import (
    PITCH_CLASS_NAMES,
    chroma_for_notes, windowed_chroma,
    KS_MAJOR, KS_MINOR, key_fit_scores, best_key,
    ChordRegion, detect_chord_regions, chord_quality_templates,
    roman_numeral, roman_numeral_with_alternates,
)


@dataclass
class N:
    """Minimal note-like for tests."""
    pitch: int
    start_beat: float
    duration_beats: float
    velocity: int = 100


# ---------------------------------------------------------------------------
# pitch_class
# ---------------------------------------------------------------------------

def test_chroma_for_notes_sums_weights():
    # 2 copies of C, 1 copy of E. Each note is 1 beat, vel=100.
    notes = [N(60, 0.0, 1.0, 100), N(72, 0.0, 1.0, 100), N(64, 0.0, 1.0, 100)]
    c = chroma_for_notes(notes)
    assert len(c) == 12
    assert c[0] == 200.0   # C pitch-class: 2 notes
    assert c[4] == 100.0   # E pitch-class: 1 note
    assert sum(c[i] for i in range(12) if i not in (0, 4)) == 0.0


def test_windowed_chroma_splits_by_window():
    # Two C-major notes: C at beat 0, E at beat 2. Window=1 beat.
    notes = [N(60, 0.0, 1.0, 100), N(64, 2.0, 1.0, 100)]
    starts, chromas = windowed_chroma(
        notes, window_beats=1.0, total_beats=4.0,
    )
    assert starts == [0.0, 1.0, 2.0, 3.0]
    assert len(chromas) == 4
    assert chromas[0][0] > 0 and chromas[0][4] == 0  # C only
    assert chromas[1] == [0.0] * 12                   # silence
    assert chromas[2][4] > 0 and chromas[2][0] == 0  # E only
    assert chromas[3] == [0.0] * 12


def test_windowed_chroma_spanning_note():
    # One note from beat 0.5 to 2.5 — spans 3 windows (0, 1, 2).
    notes = [N(60, 0.5, 2.0, 100)]
    _, chromas = windowed_chroma(
        notes, window_beats=1.0, total_beats=4.0,
    )
    # window 0 sees 0.5 beats of overlap, windows 1 full beat, window 2 0.5.
    assert chromas[0][0] == pytest.approx(0.5 * 100)
    assert chromas[1][0] == pytest.approx(1.0 * 100)
    assert chromas[2][0] == pytest.approx(0.5 * 100)
    assert chromas[3][0] == 0.0


def test_windowed_chroma_rejects_bad_window():
    with pytest.raises(ValueError):
        windowed_chroma([], window_beats=0.0, total_beats=4.0)


def test_windowed_chroma_empty_total():
    starts, chromas = windowed_chroma(
        [N(60, 0.0, 1.0, 100)], window_beats=1.0, total_beats=0.0,
    )
    assert starts == [] and chromas == []


# ---------------------------------------------------------------------------
# key_fit
# ---------------------------------------------------------------------------

def test_key_fit_all_zero_chroma_returns_zeros():
    scores = key_fit_scores([0.0] * 12)
    assert all(v == 0.0 for v in scores.values())
    assert len(scores) == 24


def test_key_fit_c_major_chord_picks_c_major():
    # Pure C major triad (C, E, G) should pick C major.
    chroma = [0.0] * 12
    chroma[0] = chroma[4] = chroma[7] = 1.0
    key, best, _ = best_key(chroma)
    assert key == (0, "maj")
    assert best > 0


def test_key_fit_a_minor_chord_picks_a_minor_or_c_major():
    # A minor chord (A, C, E). Could fit both A minor and C major roughly
    # equally; require the winner to be one of them.
    chroma = [0.0] * 12
    chroma[9] = chroma[0] = chroma[4] = 1.0
    key, _best, _runner = best_key(chroma)
    assert key in ((9, "min"), (0, "maj"))


def test_key_fit_scores_has_24_entries():
    scores = key_fit_scores([1.0] * 12)
    assert len(scores) == 24
    for root in range(12):
        assert (root, "maj") in scores
        assert (root, "min") in scores


# ---------------------------------------------------------------------------
# chord_regions
# ---------------------------------------------------------------------------

def test_chord_quality_templates_contains_expected():
    t = chord_quality_templates()
    for q in ("maj", "min", "dom7", "maj7", "m7", "m7b5", "dim", "aug",
              "sus4", "sus2"):
        assert q in t
        assert len(t[q]) == 12


def test_detect_single_c_major_region():
    # Long sustained C major triad covering 4 beats.
    notes = [
        N(60, 0.0, 4.0, 100),  # C
        N(64, 0.0, 4.0, 100),  # E
        N(67, 0.0, 4.0, 100),  # G
    ]
    regions = detect_chord_regions(notes, total_beats=4.0, window_beats=1.0)
    assert len(regions) == 1
    r = regions[0]
    assert r.root_pc == 0 and r.quality == "maj"
    assert r.start_beat == 0.0
    assert r.end_beat == pytest.approx(4.0)
    assert r.label == "C"


def test_detect_alternating_chords():
    # Beat 0-2: C major. Beat 2-4: G major.
    notes = [
        N(60, 0.0, 2.0, 100), N(64, 0.0, 2.0, 100), N(67, 0.0, 2.0, 100),
        N(67, 2.0, 2.0, 100), N(71, 2.0, 2.0, 100), N(74, 2.0, 2.0, 100),
    ]
    regions = detect_chord_regions(notes, total_beats=4.0, window_beats=1.0)
    assert len(regions) == 2
    assert (regions[0].root_pc, regions[0].quality) == (0, "maj")
    assert (regions[1].root_pc, regions[1].quality) == (7, "maj")
    assert regions[0].end_beat == pytest.approx(regions[1].start_beat)


def test_detect_minor_quality():
    # A minor triad.
    notes = [
        N(57, 0.0, 2.0, 100),  # A
        N(60, 0.0, 2.0, 100),  # C
        N(64, 0.0, 2.0, 100),  # E
    ]
    regions = detect_chord_regions(notes, total_beats=2.0, window_beats=1.0)
    assert len(regions) >= 1
    # Best match could be "min" (A minor) — check it's a reasonable
    # triad quality, not some distant alternative.
    assert regions[0].quality in ("min", "m7", "maj")
    if regions[0].quality == "min":
        assert regions[0].root_pc == 9


def test_detect_empty_returns_empty():
    assert detect_chord_regions([], total_beats=4.0) == []
    assert detect_chord_regions([N(60, 0.0, 1.0, 100)], total_beats=0.0) == []


def test_chord_region_label_formatting():
    assert ChordRegion(0, 1, 0, "maj", 0.1).label == "C"
    assert ChordRegion(0, 1, 9, "min", 0.1).label == "Am"
    assert ChordRegion(0, 1, 7, "dom7", 0.1).label == "G7"
    assert ChordRegion(0, 1, 2, "m7b5", 0.1).label == "Dm7b5"
    assert ChordRegion(0, 1, 5, "maj7", 0.1).label == "Fmaj7"
    assert ChordRegion(0, 1, 4, "minMaj7", 0.1).label == "Em(maj7)"
    assert ChordRegion(0, 1, 0, "maj6", 0.1).label == "C6"
    assert ChordRegion(0, 1, 9, "min6", 0.1).label == "Am6"


def test_detect_minMaj7_with_extension():
    # User-reported case: E-G-B-D#-F#. Em(maj7) triad plus 9th extension.
    # Without the minMaj7 template four 3-note templates tied at 0.77,
    # leaving margin-based confidence near zero and dropping the region.
    notes = [
        N(64, 0.0, 1.0, 100),  # E
        N(67, 0.0, 1.0, 100),  # G
        N(71, 0.0, 1.0, 100),  # B
        N(75, 0.0, 1.0, 100),  # D#
        N(78, 0.0, 1.0, 100),  # F#
    ]
    regions = detect_chord_regions(
        notes, total_beats=1.0, window_beats=1.0,
        min_confidence=0.05,
    )
    assert len(regions) == 1
    r = regions[0]
    assert r.root_pc == 4 and r.quality == "minMaj7"
    assert r.confidence > 0.05  # clear winner, not a tie


def test_detect_maj6_or_enharmonic():
    # C6 and Am7 share the same 4 pitch classes, so from chord tones
    # alone the root is ambiguous. Accept either reading — both are
    # valid in different contexts.
    notes = [
        N(60, 0.0, 2.0, 100), N(64, 0.0, 2.0, 100),
        N(67, 0.0, 2.0, 100), N(69, 0.0, 2.0, 100),
    ]
    regions = detect_chord_regions(
        notes, total_beats=2.0, window_beats=1.0, min_confidence=0.05)
    assert any(
        (r.root_pc, r.quality) in ((0, "maj6"), (9, "m7"))
        for r in regions
    )


def test_detect_min6_or_enharmonic():
    # Am6 and F#m7b5 share the same 4 pitch classes. Accept either.
    notes = [
        N(57, 0.0, 2.0, 100), N(60, 0.0, 2.0, 100),
        N(64, 0.0, 2.0, 100), N(66, 0.0, 2.0, 100),
    ]
    regions = detect_chord_regions(
        notes, total_beats=2.0, window_beats=1.0, min_confidence=0.05)
    assert any(
        (r.root_pc, r.quality) in ((9, "min6"), (6, "m7b5"))
        for r in regions
    )


def test_min_confidence_drops_ambiguous_windows():
    # Single isolated note: best cosine against any triad template is
    # ~0.577 (one note fits three-note template). A threshold above
    # that drops the window; a zero threshold keeps it.
    notes = [N(60, 0.0, 1.0, 100)]
    kept = detect_chord_regions(
        notes, total_beats=1.0, window_beats=1.0, min_confidence=0.0,
    )
    dropped = detect_chord_regions(
        notes, total_beats=1.0, window_beats=1.0, min_confidence=0.7,
    )
    assert len(kept) >= 1  # weak match still classified
    assert len(dropped) == 0


# ---------------------------------------------------------------------------
# romans
# ---------------------------------------------------------------------------

def test_roman_diatonic_major_key():
    # In C major: C=I, Dm=ii, Em=iii, F=IV, G=V, Am=vi, Bdim=vii°
    cases = [
        (0, "maj", "I"),
        (2, "min", "ii"),
        (4, "min", "iii"),
        (5, "maj", "IV"),
        (7, "maj", "V"),
        (9, "min", "vi"),
        (11, "dim", "vii°"),
    ]
    for root, quality, expected in cases:
        got = roman_numeral(root, quality, 0, "maj")
        assert got == expected, f"{root}/{quality} → got {got!r}, want {expected!r}"


def test_roman_diatonic_minor_key():
    # In A minor (natural): Am=i, Bdim=ii°, C=III, Dm=iv, Em=v (or E=V),
    # F=VI, G=VII.
    cases = [
        (9, "min", "i"),
        (11, "dim", "ii°"),
        (0, "maj", "III"),
        (2, "min", "iv"),
        (4, "min", "v"),
        (5, "maj", "VI"),
        (7, "maj", "VII"),
    ]
    for root, quality, expected in cases:
        got = roman_numeral(root, quality, 9, "min")
        assert got == expected, f"{root}/{quality} → got {got!r}, want {expected!r}"


def test_roman_dom7_labels():
    # V7 in C major = G7.
    assert roman_numeral(7, "dom7", 0, "maj") == "V7"
    # In A minor, V7 is E7.
    assert roman_numeral(4, "dom7", 9, "min") == "V7"


def test_roman_extended_qualities():
    # i(maj7) in E minor — iconic Em(maj7) functions as tonic-with-
    # leading-tone-tension. Case follows the minor triad root.
    assert roman_numeral(4, "minMaj7", 4, "min") == "i(maj7)"
    # IV6 in C major: F6.
    assert roman_numeral(5, "maj6", 0, "maj") == "IV6"
    # vi6 in C major: Am6.
    assert roman_numeral(9, "min6", 0, "maj") == "vi6"


def test_roman_chromatic_flat_two():
    # Db major in C major -> bII (Neapolitan).
    assert roman_numeral(1, "maj", 0, "maj") == "bII"
    # Db minor in C major -> bii.
    assert roman_numeral(1, "min", 0, "maj") == "bii"


def test_roman_tracks_chord_quality_case():
    # II chord in C major: Dmaj = "II", Dmin = "ii". Case follows quality.
    assert roman_numeral(2, "maj", 0, "maj") == "II"
    assert roman_numeral(2, "min", 0, "maj") == "ii"


def test_roman_with_alternates():
    # C major chord, candidates: C-major (primary), G-major, F-major.
    primary, alts = roman_numeral_with_alternates(
        0, "maj", [(0, "maj"), (7, "maj"), (5, "maj")],
    )
    assert primary == "I"
    labels = [lbl for (_k, lbl) in alts]
    assert labels == ["IV", "V"]


def test_roman_with_alternates_truncates():
    primary, alts = roman_numeral_with_alternates(
        0, "maj", [(0, "maj"), (7, "maj"), (5, "maj"), (2, "min"), (9, "min")],
        max_alternates=2,
    )
    assert primary == "I"
    assert len(alts) == 2


def test_roman_with_alternates_empty_input():
    primary, alts = roman_numeral_with_alternates(0, "maj", [])
    assert alts == []
