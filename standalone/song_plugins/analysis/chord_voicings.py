"""Chord voicing engine for the chordify workflow.

Given a root MIDI pitch, the pattern's key/scale, and a chord "spec"
stored under the ``chord_root`` tag of a Note, this module produces:

- the set of MIDI pitches that realise the chord ("voicing"),
- a human-readable letter label ("Cm7", "F#°7"),
- a roman-numeral label ("ii7", "bVII") via the existing ``romans``
  module.

The spec is a plain dict stored on ``Note.tags['chord_root']``:

    {
        'quality':  str,        # 'diatonic' | 'diatonic7' | <base quality>
        'extras':   list[str],  # alterations, optional
        'inversion': str,       # currently always 'root'
    }

Re-running the chordify plugin must be deterministic, so everything
here is stateless and does no I/O.
"""

from __future__ import annotations

import copy
from typing import Optional

from .romans import roman_numeral


# ---------------------------------------------------------------------------
# Quality vocabulary
# ---------------------------------------------------------------------------

# Base quality name → (intervals-from-root, letter-suffix used when we
# label the chord by its absolute root, e.g. 'Cm7' vs 'ii7').
#
# Quality names match ``chord_regions`` where they overlap so that the
# analysis/labeler machinery can consume specs produced here without
# translation.
QUALITIES: dict[str, tuple[tuple[int, ...], str]] = {
    'maj':     ((0, 4, 7),              ''),
    'min':     ((0, 3, 7),              'm'),
    'dim':     ((0, 3, 6),              '°'),
    'aug':     ((0, 4, 8),              '+'),
    'sus2':    ((0, 2, 7),              'sus2'),
    'sus4':    ((0, 5, 7),              'sus4'),
    'maj7':    ((0, 4, 7, 11),          'maj7'),
    'm7':      ((0, 3, 7, 10),          'm7'),
    'dom7':    ((0, 4, 7, 10),          '7'),
    'm7b5':    ((0, 3, 6, 10),          'ø7'),
    'dim7':    ((0, 3, 6, 9),           '°7'),
    'minMaj7': ((0, 3, 7, 11),          'mMaj7'),
    'maj6':    ((0, 4, 7, 9),           '6'),
    'min6':    ((0, 3, 7, 9),           'm6'),
    'add9':    ((0, 4, 7, 14),          'add9'),
    'madd9':   ((0, 3, 7, 14),          'm(add9)'),
    '9':       ((0, 4, 7, 10, 14),      '9'),
    'maj9':    ((0, 4, 7, 11, 14),      'maj9'),
    'm9':      ((0, 3, 7, 10, 14),      'm9'),
    '11':      ((0, 4, 7, 10, 14, 17),  '11'),
    '13':      ((0, 4, 7, 10, 14, 17, 21), '13'),
}


# Tiered vocabulary — used by the piano-roll middle-click cycle and the
# 'q' popup in the UI layer. Separated here rather than at the UI so
# tests (and any future alternate UI) see the same grouping.
TIER1_CYCLE: tuple[Optional[str], ...] = (
    None,          # not a chord root — un-root option in the cycle
    'diatonic',    # scale-degree-diatonic triad
    'diatonic7',   # scale-degree-diatonic seventh chord
)

TIER2_OVERRIDES: tuple[str, ...] = (
    'maj', 'min', 'dim', 'aug',
    'sus2', 'sus4',
    'maj7', 'm7', 'dom7', 'm7b5', 'dim7',
)

# The full middle-click cycle is tier1 + tier2, in that order, wrapping.
CYCLE_ORDER: tuple[Optional[str], ...] = TIER1_CYCLE + TIER2_OVERRIDES

# Available via the 'q' popup for fuller coverage.
TIER3_EXTENSIONS: tuple[str, ...] = (
    'maj6', 'min6', 'minMaj7',
    'add9', 'madd9', '9', 'maj9', 'm9', '11', '13',
)

TIER4_ALTERATIONS: tuple[str, ...] = ('b5', '#5', 'b9', '#9', '#11')


# ---------------------------------------------------------------------------
# Scale theory
# ---------------------------------------------------------------------------

# Semitone offsets from tonic for each supported scale mode.
SCALE_INTERVALS: dict[str, tuple[int, ...]] = {
    'major':          (0, 2, 4, 5, 7, 9, 11),
    'minor':          (0, 2, 3, 5, 7, 8, 10),
    'harmonic_minor': (0, 2, 3, 5, 7, 8, 11),
    'melodic_minor':  (0, 2, 3, 5, 7, 9, 11),
    'dorian':         (0, 2, 3, 5, 7, 9, 10),
    'phrygian':       (0, 1, 3, 5, 7, 8, 10),
    'lydian':         (0, 2, 4, 6, 7, 9, 11),
    'mixolydian':     (0, 2, 4, 5, 7, 9, 10),
    'locrian':        (0, 1, 3, 5, 6, 8, 10),
}


# Diatonic triads per scale (index 0..6 → base quality).
DIATONIC_TRIADS: dict[str, tuple[str, ...]] = {
    'major':          ('maj', 'min', 'min', 'maj', 'maj', 'min', 'dim'),
    'minor':          ('min', 'dim', 'maj', 'min', 'min', 'maj', 'maj'),
    'harmonic_minor': ('min', 'dim', 'aug', 'min', 'maj', 'maj', 'dim'),
    'melodic_minor':  ('min', 'min', 'aug', 'maj', 'maj', 'dim', 'dim'),
    'dorian':         ('min', 'min', 'maj', 'maj', 'min', 'dim', 'maj'),
    'phrygian':       ('min', 'maj', 'maj', 'min', 'dim', 'maj', 'min'),
    'lydian':         ('maj', 'maj', 'min', 'dim', 'maj', 'min', 'min'),
    'mixolydian':     ('maj', 'min', 'dim', 'maj', 'min', 'min', 'maj'),
    'locrian':        ('dim', 'maj', 'min', 'min', 'maj', 'maj', 'min'),
}


# Diatonic seventh chords — major and natural minor only; other modes
# fall through to DIATONIC_TRIADS with 'dom7'-ish naive handling.
DIATONIC_SEVENTHS: dict[str, tuple[str, ...]] = {
    'major':          ('maj7', 'm7', 'm7', 'maj7', 'dom7', 'm7', 'm7b5'),
    'minor':          ('m7', 'm7b5', 'maj7', 'm7', 'm7', 'maj7', 'dom7'),
    'harmonic_minor': ('minMaj7', 'm7b5', 'maj7', 'm7', 'dom7', 'maj7', 'dim7'),
    'dorian':         ('m7', 'm7', 'maj7', 'dom7', 'm7', 'm7b5', 'maj7'),
    'mixolydian':     ('dom7', 'm7', 'm7b5', 'maj7', 'm7', 'm7', 'maj7'),
}


KEY_PC: dict[str, int] = {
    'C': 0, 'C#': 1, 'Db': 1, 'D': 2, 'D#': 3, 'Eb': 3, 'E': 4,
    'F': 5, 'F#': 6, 'Gb': 6, 'G': 7, 'G#': 8, 'Ab': 8, 'A': 9,
    'A#': 10, 'Bb': 10, 'B': 11,
}


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def key_pc(key: str) -> Optional[int]:
    return KEY_PC.get(key)


def scale_degree(root_pitch: int, key: str, scale: str) -> Optional[int]:
    """Return 0..6 scale degree of root_pitch, or None if non-diatonic."""
    kpc = key_pc(key)
    if kpc is None:
        return None
    intervals = SCALE_INTERVALS.get(scale)
    if not intervals:
        return None
    offset = (root_pitch - kpc) % 12
    if offset in intervals:
        return intervals.index(offset)
    return None


def resolve_quality(spec: dict, root_pitch: int, key: str, scale: str) -> tuple[str, tuple[int, ...], str]:
    """Resolve a spec to a concrete (base_quality_name, intervals, letter_suffix).

    ``spec['quality']`` of ``'diatonic'`` / ``'diatonic7'`` looks up the
    scale-degree default; anything else is taken verbatim. Non-diatonic
    roots fall back to ``'maj'`` / ``'maj7'``.
    """
    q = spec.get('quality', 'diatonic')
    if q == 'diatonic':
        deg = scale_degree(root_pitch, key, scale)
        table = DIATONIC_TRIADS.get(scale, DIATONIC_TRIADS['major'])
        q = table[deg] if deg is not None else 'maj'
    elif q == 'diatonic7':
        deg = scale_degree(root_pitch, key, scale)
        table = DIATONIC_SEVENTHS.get(scale)
        if table is None:
            # Mode without a curated 7ths table — fall back to the triad's
            # family plus a minor 7th.
            tri = DIATONIC_TRIADS.get(scale, DIATONIC_TRIADS['major'])
            base = tri[deg] if deg is not None else 'maj'
            q = {'maj': 'maj7', 'min': 'm7', 'dim': 'm7b5', 'aug': 'maj7'}.get(base, 'maj7')
        else:
            q = table[deg] if deg is not None else 'maj7'
    if q not in QUALITIES:
        q = 'maj'
    intervals, suffix = QUALITIES[q]
    return q, intervals, suffix


def _apply_alterations(intervals: tuple[int, ...], extras: Optional[list[str]]) -> list[int]:
    iv = list(intervals)
    for e in extras or ():
        if e == 'b5':
            iv = [i if i != 7 else 6 for i in iv]
        elif e == '#5':
            iv = [i if i != 7 else 8 for i in iv]
        elif e == 'b9':
            if 13 not in iv:
                iv.append(13)
        elif e == '#9':
            if 15 not in iv:
                iv.append(15)
        elif e == '#11':
            if 18 not in iv:
                iv.append(18)
    return sorted(set(iv))


def build_voicing(
    root_pitch: int, key: str, scale: str, spec: Optional[dict],
    *, include_root: bool = True,
) -> list[int]:
    """Return MIDI pitches for the chord, sorted ascending.

    Returns [] if spec is None or if it lacks a quality.
    """
    if not spec or not spec.get('quality'):
        return []
    _, intervals, _ = resolve_quality(spec, root_pitch, key, scale)
    intervals = _apply_alterations(intervals, spec.get('extras'))
    pitches = [root_pitch + i for i in intervals]
    if not include_root:
        pitches = [p for p in pitches if p != root_pitch]
    return sorted(set(pitches))


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------

_FLAT_NAMES  = ('C', 'Db', 'D', 'Eb', 'E', 'F', 'Gb', 'G', 'Ab', 'A', 'Bb', 'B')
_SHARP_NAMES = ('C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B')
_FLAT_KEYS = {'F', 'Bb', 'Eb', 'Ab', 'Db', 'Gb', 'Cb'}


def _letter_for(root_pitch: int, key: str) -> str:
    pc = root_pitch % 12
    use_flats = key in _FLAT_KEYS or (len(key) > 1 and key.endswith('b'))
    return _FLAT_NAMES[pc] if use_flats else _SHARP_NAMES[pc]


def chord_label(root_pitch: int, key: str, scale: str, spec: Optional[dict]) -> str:
    """Human-readable letter label: ``'Cm7'``, ``'F#°7'``, ``'G7sus4'``."""
    if not spec or not spec.get('quality'):
        return ''
    _, _, suffix = resolve_quality(spec, root_pitch, key, scale)
    extras = ''.join(spec.get('extras') or ())
    return f'{_letter_for(root_pitch, key)}{suffix}{extras}'


def roman_label(root_pitch: int, key: str, scale: str, spec: Optional[dict]) -> str:
    """Roman-numeral label: ``'ii7'``, ``'V'``, ``'bVII'``.

    Non-diatonic roots are labelled by chromatic degree (bII, #iv, ...);
    the shape of the numeral's casing follows the resolved chord quality.
    """
    if not spec or not spec.get('quality'):
        return ''
    kpc = key_pc(key)
    if kpc is None:
        return chord_label(root_pitch, key, scale, spec)
    q, _, _ = resolve_quality(spec, root_pitch, key, scale)
    mode = 'min' if scale.startswith('minor') or scale == 'harmonic_minor' else 'maj'
    base = roman_numeral(root_pitch % 12, q, kpc, mode)
    extras = ''.join(spec.get('extras') or ())
    return f'{base}{extras}' if extras else base


# ---------------------------------------------------------------------------
# Cycle helpers (UI-facing)
# ---------------------------------------------------------------------------

def cycle_quality(current: Optional[str], direction: int = 1) -> Optional[str]:
    """Advance through CYCLE_ORDER. ``current=None`` means "not a root"."""
    try:
        idx = CYCLE_ORDER.index(current)
    except ValueError:
        idx = 0
    idx = (idx + direction) % len(CYCLE_ORDER)
    return CYCLE_ORDER[idx]


def default_spec() -> dict:
    """A spec representing the baseline 'just-root'd' state."""
    return {'quality': 'diatonic', 'extras': [], 'inversion': 'root'}


def with_quality(spec: Optional[dict], quality: Optional[str]) -> Optional[dict]:
    """Return a new spec with ``quality`` set (or None to clear root-hood)."""
    if quality is None:
        return None
    s = copy.deepcopy(spec) if spec else default_spec()
    s['quality'] = quality
    return s
