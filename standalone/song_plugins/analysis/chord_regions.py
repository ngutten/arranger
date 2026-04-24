"""Chord region detection via template matching.

For each analysis window we compute the cosine similarity between the
chroma vector and a set of chord templates (major, minor, dominant 7,
minor 7, half-diminished, diminished, augmented, sus4 — see
``chord_quality_templates``) at each of 12 roots. The highest-scoring
``(root, quality)`` labels the window. Adjacent windows sharing a label
are merged into a ``ChordRegion``.

Output regions carry a confidence score equal to the winning template's
absolute cosine similarity to the window's chroma — i.e. "how well does
the best-matching chord template fit this window." Downstream consumers
can threshold on this to drop weak matches (passing tones, silence,
very dense clusters).

An earlier version used the margin between the winner and the runner-up
as the confidence. That read was too strict: perfectly valid chords
with enharmonic ties (e.g. C6 ↔ Am7 share the same four pitch classes)
produced margin ≈ 0 and were dropped even though either label is a
correct reading. Absolute cosine handles this gracefully.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .pitch_class import windowed_chroma


# ---------------------------------------------------------------------------
# Chord templates
# ---------------------------------------------------------------------------

def _template(intervals: Sequence[int]) -> Tuple[float, ...]:
    """Build a 12-bin template with 1.0 at each interval, 0.0 elsewhere.

    Root is 0; intervals are semitones from the root.
    """
    v = [0.0] * 12
    for iv in intervals:
        v[iv % 12] = 1.0
    return tuple(v)


# Quality name -> template vector (root at 0). Keep order stable; we
# iterate this as the canonical qualities list.
#
# The 4-note extended-triad templates (maj7/dom7/m7/minMaj7/maj6/min6)
# matter even when the input chord has *more* notes — cosine similarity
# accommodates extras gracefully. Without minMaj7 in particular, a
# common chord like EmMaj7 hits three 3-note templates at identical
# scores (because an augmented triad is a subset of minMaj7), and the
# margin-based confidence gate drops the region rather than labeling it.
_QUALITY_INTERVALS: Tuple[Tuple[str, Tuple[int, ...]], ...] = (
    ("maj",     (0, 4, 7)),
    ("min",     (0, 3, 7)),
    ("dom7",    (0, 4, 7, 10)),
    ("maj7",    (0, 4, 7, 11)),
    ("m7",      (0, 3, 7, 10)),
    ("minMaj7", (0, 3, 7, 11)),
    ("maj6",    (0, 4, 7, 9)),
    ("min6",    (0, 3, 7, 9)),
    ("m7b5",    (0, 3, 6, 10)),  # half-diminished
    ("dim",     (0, 3, 6)),
    ("aug",     (0, 4, 8)),
    ("sus4",    (0, 5, 7)),
    ("sus2",    (0, 2, 7)),
)


def chord_quality_templates() -> Dict[str, Tuple[float, ...]]:
    """Return a fresh dict of ``{quality_name: 12-vector}`` templates."""
    return {name: _template(ivs) for name, ivs in _QUALITY_INTERVALS}


def _rotate(v: Sequence[float], n: int) -> Tuple[float, ...]:
    n = n % 12
    return tuple(v[(i - n) % 12] for i in range(12))


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    num = 0.0
    da = 0.0
    db = 0.0
    for ai, bi in zip(a, b):
        num += ai * bi
        da += ai * ai
        db += bi * bi
    if da == 0.0 or db == 0.0:
        return 0.0
    return num / math.sqrt(da * db)


# ---------------------------------------------------------------------------
# Region dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ChordRegion:
    start_beat: float
    end_beat: float
    root_pc: int           # 0..11, C = 0
    quality: str           # one of the keys from chord_quality_templates()
    confidence: float      # winning template's cosine similarity, in [0, 1]

    @property
    def label(self) -> str:
        """Short chord symbol like ``"C"``, ``"Am"``, ``"G7"``."""
        from .pitch_class import PITCH_CLASS_NAMES
        name = PITCH_CLASS_NAMES[self.root_pc % 12]
        suffix = {
            "maj": "", "min": "m",
            "dom7": "7", "maj7": "maj7", "m7": "m7",
            "minMaj7": "m(maj7)",
            "maj6": "6", "min6": "m6",
            "m7b5": "m7b5", "dim": "dim", "aug": "aug",
            "sus4": "sus4", "sus2": "sus2",
        }.get(self.quality, self.quality)
        return f"{name}{suffix}"


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def _classify_chroma(
    chroma: Sequence[float],
    templates: Dict[str, Tuple[float, ...]],
) -> Tuple[Optional[Tuple[int, str]], float]:
    """Best-matching (root, quality) plus absolute cosine fit.

    Returns ``(None, 0)`` for an all-zero chroma. Ties break by the
    iteration order of ``templates`` (i.e. the declaration order of
    ``_QUALITY_INTERVALS``) — put preferred qualities earlier if you
    care about disambiguating enharmonics.
    """
    if sum(chroma) == 0.0:
        return (None, 0.0)
    best_score = -1.0
    best_root = 0
    best_quality = ""
    for quality, tmpl in templates.items():
        for root in range(12):
            rot = _rotate(tmpl, root)
            score = _cosine(chroma, rot)
            if score > best_score:
                best_score = score
                best_root = root
                best_quality = quality
    if best_score < 0:
        return (None, 0.0)
    return ((best_root, best_quality), best_score)


def detect_chord_regions(
    notes: Iterable,
    *,
    total_beats: float,
    window_beats: float = 1.0,
    hop_beats: Optional[float] = None,
    min_confidence: float = 0.0,
    merge_adjacent: bool = True,
) -> List[ChordRegion]:
    """Scan ``notes`` and return a list of ``ChordRegion`` segments.

    Parameters
    ----------
    notes: iterable of objects with ``pitch``, ``start_beat``,
        ``duration_beats``, ``velocity`` — typically ``ResolvedNote``s.
    total_beats: length of the analysis span.
    window_beats: analysis window width (default 1 beat).
    hop_beats: hop size; defaults to ``window_beats`` (non-overlapping).
    min_confidence: windows whose best-template cosine falls below this
        are dropped (treated as "no chord"). ``0.0`` keeps everything.
        A lone note scores around 0.58 against triad templates; clean
        triads score ≥ 0.85; perfect matches hit 1.0.
    merge_adjacent: when True, contiguous windows with the same
        ``(root, quality)`` collapse into one region (confidence becomes
        the mean of the merged windows).
    """
    if total_beats <= 0 or window_beats <= 0:
        return []
    hop = hop_beats if hop_beats and hop_beats > 0 else window_beats

    starts, chromas = windowed_chroma(
        notes, window_beats=window_beats,
        total_beats=total_beats, hop_beats=hop,
    )
    if not chromas:
        return []

    templates = chord_quality_templates()

    # Per-window classification. ``None`` for empty or low-confidence.
    per_window: List[Optional[Tuple[int, str, float]]] = []
    for chroma in chromas:
        label, best = _classify_chroma(chroma, templates)
        if label is None or best < min_confidence:
            per_window.append(None)
        else:
            per_window.append((label[0], label[1], best))

    # Collapse into regions.
    regions: List[ChordRegion] = []
    if not merge_adjacent:
        for i, entry in enumerate(per_window):
            if entry is None:
                continue
            root, quality, conf = entry
            s = starts[i]
            e = min(total_beats, s + window_beats)
            regions.append(ChordRegion(s, e, root, quality, conf))
        return regions

    i = 0
    n = len(per_window)
    while i < n:
        entry = per_window[i]
        if entry is None:
            i += 1
            continue
        root, quality, conf = entry
        j = i + 1
        conf_sum = conf
        conf_count = 1
        while j < n and per_window[j] is not None and \
                per_window[j][0] == root and per_window[j][1] == quality:
            conf_sum += per_window[j][2]
            conf_count += 1
            j += 1
        s = starts[i]
        # End at the start of the next classified window or at total_beats.
        e = starts[j] if j < n else min(total_beats, s + window_beats * (j - i))
        e = min(e, total_beats)
        regions.append(ChordRegion(
            start_beat=s, end_beat=e,
            root_pc=root, quality=quality,
            confidence=conf_sum / conf_count,
        ))
        i = j
    return regions
