"""Tests for the chord voicing engine used by the chordify plugin."""

import pytest

from standalone.song_plugins.analysis.chord_voicings import (
    QUALITIES, CYCLE_ORDER,
    scale_degree, resolve_quality, build_voicing,
    chord_label, roman_label, cycle_quality,
    default_spec, with_quality,
)


# ---------------------------------------------------------------------------
# Scale degree / resolve_quality
# ---------------------------------------------------------------------------

class TestScaleDegree:
    def test_c_major_tonic(self):
        assert scale_degree(60, 'C', 'major') == 0  # C4 -> I

    def test_c_major_fifth(self):
        assert scale_degree(67, 'C', 'major') == 4  # G4 -> V

    def test_c_major_leading_tone(self):
        assert scale_degree(71, 'C', 'major') == 6  # B4 -> vii°

    def test_c_major_non_diatonic(self):
        assert scale_degree(61, 'C', 'major') is None  # C#4 is not diatonic

    def test_a_minor_tonic(self):
        assert scale_degree(69, 'A', 'minor') == 0

    def test_a_minor_leading_tone_absent(self):
        # Natural minor doesn't include the leading tone (G#)
        assert scale_degree(68, 'A', 'minor') is None

    def test_octave_insensitive(self):
        assert scale_degree(48, 'C', 'major') == 0  # C3
        assert scale_degree(72, 'C', 'major') == 0  # C5

    def test_unknown_key_returns_none(self):
        assert scale_degree(60, 'H', 'major') is None

    def test_unknown_scale_returns_none(self):
        assert scale_degree(60, 'C', 'klingon') is None


class TestResolveQuality:
    def test_diatonic_in_c_major(self):
        # IV of C major is F major
        q, iv, _ = resolve_quality({'quality': 'diatonic'}, 65, 'C', 'major')
        assert q == 'maj'
        assert iv == (0, 4, 7)

    def test_diatonic_ii_in_c_major_is_minor(self):
        q, _, _ = resolve_quality({'quality': 'diatonic'}, 62, 'C', 'major')
        assert q == 'min'

    def test_diatonic7_v_in_c_major_is_dom7(self):
        q, iv, _ = resolve_quality({'quality': 'diatonic7'}, 67, 'C', 'major')
        assert q == 'dom7'
        assert iv == (0, 4, 7, 10)

    def test_diatonic7_i_in_c_major_is_maj7(self):
        q, _, _ = resolve_quality({'quality': 'diatonic7'}, 60, 'C', 'major')
        assert q == 'maj7'

    def test_diatonic_non_scale_falls_back_to_maj(self):
        # F# is not in C major
        q, _, _ = resolve_quality({'quality': 'diatonic'}, 66, 'C', 'major')
        assert q == 'maj'

    def test_explicit_quality_used_verbatim(self):
        q, iv, _ = resolve_quality({'quality': 'sus4'}, 60, 'C', 'major')
        assert q == 'sus4'
        assert iv == (0, 5, 7)

    def test_unknown_quality_falls_back_to_maj(self):
        q, _, _ = resolve_quality({'quality': 'nonsense'}, 60, 'C', 'major')
        assert q == 'maj'


# ---------------------------------------------------------------------------
# Voicing pitches
# ---------------------------------------------------------------------------

class TestBuildVoicing:
    def test_basic_c_major(self):
        assert build_voicing(60, 'C', 'major', {'quality': 'maj'}) == [60, 64, 67]

    def test_c_minor_triad(self):
        assert build_voicing(60, 'C', 'major', {'quality': 'min'}) == [60, 63, 67]

    def test_c_dom7(self):
        assert build_voicing(60, 'C', 'major', {'quality': 'dom7'}) == [60, 64, 67, 70]

    def test_diatonic_routes_via_scale(self):
        # ii in C major: D min
        assert build_voicing(62, 'C', 'major', {'quality': 'diatonic'}) == [62, 65, 69]

    def test_diatonic7_v_is_dom7(self):
        # V7 in C: G B D F -> [67,71,74,77]
        assert build_voicing(67, 'C', 'major', {'quality': 'diatonic7'}) == [67, 71, 74, 77]

    def test_include_root_false_omits_root(self):
        notes = build_voicing(60, 'C', 'major', {'quality': 'maj'}, include_root=False)
        assert 60 not in notes
        assert notes == [64, 67]

    def test_none_spec_returns_empty(self):
        assert build_voicing(60, 'C', 'major', None) == []

    def test_spec_without_quality_returns_empty(self):
        assert build_voicing(60, 'C', 'major', {}) == []

    def test_alteration_b5_flattens_fifth(self):
        notes = build_voicing(60, 'C', 'major',
                              {'quality': 'dom7', 'extras': ['b5']})
        # [0, 4, 6, 10] → [60, 64, 66, 70]
        assert notes == [60, 64, 66, 70]

    def test_alteration_sharp_nine_adds_note(self):
        notes = build_voicing(60, 'C', 'major',
                              {'quality': 'dom7', 'extras': ['#9']})
        # [0,4,7,10] + 15 → [60, 64, 67, 70, 75]
        assert 75 in notes
        assert 70 in notes

    def test_dim7_symmetric(self):
        assert build_voicing(60, 'C', 'major', {'quality': 'dim7'}) == [60, 63, 66, 69]

    def test_maj9_full_chord(self):
        # Cmaj9 = C E G B D
        assert build_voicing(60, 'C', 'major', {'quality': 'maj9'}) == [60, 64, 67, 71, 74]

    def test_high_root_preserves_shape(self):
        assert build_voicing(72, 'C', 'major', {'quality': 'maj'}) == [72, 76, 79]


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------

class TestChordLabel:
    def test_c_major(self):
        assert chord_label(60, 'C', 'major', {'quality': 'maj'}) == 'C'

    def test_c_minor(self):
        assert chord_label(60, 'C', 'major', {'quality': 'min'}) == 'Cm'

    def test_c_dom7(self):
        assert chord_label(60, 'C', 'major', {'quality': 'dom7'}) == 'C7'

    def test_g7sus4(self):
        assert chord_label(67, 'C', 'major',
                           {'quality': 'sus4', 'extras': []}) == 'Gsus4'

    def test_diminished_uses_symbol(self):
        assert chord_label(71, 'C', 'major', {'quality': 'dim'}) == 'B°'

    def test_diatonic_routes_v_to_7(self):
        # In C major, diatonic7 on G yields Gdom7 -> 'G7'
        assert chord_label(67, 'C', 'major', {'quality': 'diatonic7'}) == 'G7'

    def test_flat_spelling_in_flat_key(self):
        # In F major (1 flat), Bb is the IV — should spell 'Bb' not 'A#'
        assert chord_label(70, 'F', 'major', {'quality': 'maj'}) == 'Bb'

    def test_sharp_spelling_in_sharp_key(self):
        # In G major (1 sharp), F# is the vii° — should spell 'F#'
        assert chord_label(66, 'G', 'major', {'quality': 'dim'}) == 'F#°'

    def test_empty_spec_returns_empty(self):
        assert chord_label(60, 'C', 'major', None) == ''
        assert chord_label(60, 'C', 'major', {}) == ''

    def test_extras_appended(self):
        label = chord_label(60, 'C', 'major',
                            {'quality': 'dom7', 'extras': ['b9']})
        assert label == 'C7b9'


class TestRomanLabel:
    def test_i_in_c_major(self):
        assert roman_label(60, 'C', 'major', {'quality': 'maj'}) == 'I'

    def test_v7_in_c_major(self):
        assert roman_label(67, 'C', 'major', {'quality': 'dom7'}) == 'V7'

    def test_ii_is_minor_in_c_major(self):
        assert roman_label(62, 'C', 'major', {'quality': 'min'}) == 'ii'

    def test_vii_diminished_in_c_major(self):
        # vii° in C major at pitch 71 (B)
        label = roman_label(71, 'C', 'major', {'quality': 'dim'})
        assert label == 'vii°'

    def test_non_diatonic_uses_chromatic(self):
        # F# in C major has no scale degree; expect '#iv' or 'bV' style
        label = roman_label(66, 'C', 'major', {'quality': 'maj'})
        # bV or #IV depending on convention — romans.py uses bV label for degree 6
        assert 'V' in label or 'IV' in label

    def test_empty_spec_returns_empty(self):
        assert roman_label(60, 'C', 'major', None) == ''


# ---------------------------------------------------------------------------
# Cycle UX
# ---------------------------------------------------------------------------

class TestCycleQuality:
    def test_cycle_from_none_forward(self):
        assert cycle_quality(None, 1) == CYCLE_ORDER[1]

    def test_cycle_from_none_backward(self):
        assert cycle_quality(None, -1) == CYCLE_ORDER[-1]

    def test_cycle_wraps(self):
        last = CYCLE_ORDER[-1]
        assert cycle_quality(last, 1) == CYCLE_ORDER[0]

    def test_unknown_quality_starts_at_beginning(self):
        # Non-cycle qualities (e.g. '13', 'b5') put us at idx=0 before stepping.
        assert cycle_quality('13', 1) == CYCLE_ORDER[1]

    def test_cycle_includes_none(self):
        assert None in CYCLE_ORDER


class TestWithQuality:
    def test_none_means_un_root(self):
        assert with_quality({'quality': 'maj'}, None) is None

    def test_sets_quality_preserving_extras(self):
        s = with_quality({'quality': 'maj', 'extras': ['b5']}, 'min')
        assert s['quality'] == 'min'
        assert s['extras'] == ['b5']

    def test_from_none_spec_uses_default(self):
        s = with_quality(None, 'maj7')
        assert s['quality'] == 'maj7'
        assert s['extras'] == []
        assert s['inversion'] == 'root'

    def test_default_spec_roundtrip(self):
        s = default_spec()
        assert s == {'quality': 'diatonic', 'extras': [], 'inversion': 'root'}


# ---------------------------------------------------------------------------
# Quality vocabulary invariants
# ---------------------------------------------------------------------------

class TestQualityVocab:
    def test_all_intervals_start_with_root(self):
        for name, (iv, _) in QUALITIES.items():
            assert iv[0] == 0, f'{name} missing root'

    def test_all_intervals_sorted(self):
        for name, (iv, _) in QUALITIES.items():
            assert list(iv) == sorted(iv), f'{name} unsorted: {iv}'

    def test_cycle_order_all_valid(self):
        for q in CYCLE_ORDER:
            if q is None or q in ('diatonic', 'diatonic7'):
                continue
            assert q in QUALITIES, f'cycle quality {q!r} missing from QUALITIES'
