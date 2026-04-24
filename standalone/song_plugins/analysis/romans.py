"""Chord + key → roman numeral label.

Given a chord's root pitch-class and quality, plus a key (tonic pc and
mode), return the roman numeral for the chord's function in that key.

Conventions
-----------
- Major-key romans: upper-case triads (I, II, III, IV, V, VI, VII);
  minor-quality triads are lower-case (ii, iii, vi). Sevenths append to
  the numeral (V7, ii7, vii°7).
- Minor-key romans: the natural-minor diatonic is lower-case tonic
  (i, iv), upper-case III, VI, VII. Dominant V is common (major V7 in
  harmonic minor) — we treat an actual V major triad in a minor key as
  "V" without special-casing harmonic vs natural.
- Non-diatonic roots: accidentals are surfaced as flat / sharp (bII,
  #iv). We always label via the *chromatic* scale degree; the choice
  of flat vs sharp tracks the key's mode and the chord's quality.
"""

from __future__ import annotations

from typing import List, Optional, Tuple


# Pitch-class distance from tonic → (major_rel, minor_rel) label prefix.
# Entries give the preferred accidental for each chromatic step in a
# major vs minor key.
# Keys: 0..11 (semitones above tonic). Values: (maj-key-label, min-key-label).
_SCALE_DEGREE_LABELS: Tuple[Tuple[str, str], ...] = (
    ("I",   "i"),    # 0
    ("bII", "bii"),  # 1
    ("II",  "ii"),   # 2
    ("bIII","III"),  # 3 (minor-key bIII = III)
    ("III", "iii"),  # 4 (raised-3 in minor, i.e., major III)
    ("IV",  "iv"),   # 5
    ("bV",  "bv"),   # 6
    ("V",   "v"),    # 7
    ("bVI", "VI"),   # 8 (minor-key bVI = VI)
    ("VI",  "vi"),   # 9
    ("bVII","VII"),  # 10 (minor-key bVII = VII)
    ("VII", "vii"),  # 11
)

# Diatonic pitch-class offsets (from tonic) per key mode. Used to decide
# case (upper vs lower) for triads whose quality is unspecified.
_DIATONIC_MAJOR = (0, 2, 4, 5, 7, 9, 11)
_DIATONIC_MINOR = (0, 2, 3, 5, 7, 8, 10)  # natural minor


def _quality_suffix(quality: str) -> str:
    return {
        "maj": "",
        "min": "",
        "dom7": "7",
        "maj7": "maj7",
        "m7": "7",
        "minMaj7": "(maj7)",
        "maj6": "6",
        "min6": "6",
        "m7b5": "ø",  # half-diminished
        "dim": "°",   # diminished
        "aug": "+",
        "sus4": "sus4",
        "sus2": "sus2",
    }.get(quality, "")


def _is_minor_quality(quality: str) -> bool:
    return quality in ("min", "m7", "minMaj7", "min6", "m7b5", "dim")


def roman_numeral(
    chord_root_pc: int,
    chord_quality: str,
    key_tonic_pc: int,
    key_mode: str,
) -> str:
    """Return the roman-numeral label for ``chord`` in ``key``.

    ``key_mode`` is ``"maj"`` or ``"min"``; ``chord_quality`` is one of
    the strings from ``chord_regions.chord_quality_templates()``.

    The numeral's case reflects the actual chord quality (lowercase for
    minor/dim quality, uppercase for major/dom/aug quality), not the
    key's diatonic default — so a major chord on the 2nd degree in C
    minor is rendered "II" rather than "ii".
    """
    mode = "min" if str(key_mode).startswith("min") else "maj"
    degree = (int(chord_root_pc) - int(key_tonic_pc)) % 12
    mode_idx = 1 if mode == "min" else 0
    base = _SCALE_DEGREE_LABELS[degree][mode_idx]
    # Case follows chord quality, not key.
    if _is_minor_quality(chord_quality):
        base = base.lower()
    else:
        base = base.upper()
    # Preserve any leading flat/sharp accidentals in the original casing.
    # _SCALE_DEGREE_LABELS uses 'b' for flats; lower()ing keeps it, upper()
    # turns it to 'B' which is wrong — fix by re-lowercasing the accidental.
    if base and base[0] == "B" and len(base) > 1 and base[1:] and \
            base[1:][0] in ("I", "V"):
        base = "b" + base[1:]
    return base + _quality_suffix(chord_quality)


def roman_numeral_with_alternates(
    chord_root_pc: int,
    chord_quality: str,
    candidate_keys: List[Tuple[int, str]],
    *,
    max_alternates: int = 3,
) -> Tuple[str, List[Tuple[Tuple[int, str], str]]]:
    """Label the chord in its primary key, plus readings in neighbours.

    ``candidate_keys`` is a ranked list of ``(tonic_pc, mode)`` tuples
    — the first is the primary key, the rest are alternates. Returns
    ``(primary_label, [((tonic, mode), label), ...])`` where the
    alternates list excludes the primary and is truncated to
    ``max_alternates`` entries.
    """
    if not candidate_keys:
        return (chord_quality, [])
    primary = candidate_keys[0]
    primary_label = roman_numeral(
        chord_root_pc, chord_quality, primary[0], primary[1])
    alternates: List[Tuple[Tuple[int, str], str]] = []
    for key in candidate_keys[1:]:
        if len(alternates) >= max_alternates:
            break
        alt = roman_numeral(
            chord_root_pc, chord_quality, key[0], key[1])
        alternates.append((key, alt))
    return (primary_label, alternates)
