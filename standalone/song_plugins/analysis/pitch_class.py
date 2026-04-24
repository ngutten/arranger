"""Windowed pitch-class (chroma) histograms.

Given an iterable of notes (anything with ``pitch``, ``start_beat``,
``duration_beats``, and ``velocity``), produce a sequence of 12-bin
chroma vectors over fixed-size beat windows.

Contribution of each note to a window is weighted by duration-in-window
times velocity, so longer / louder notes dominate the chroma as the ear
would expect. Notes that span multiple windows contribute to each.

The vectors are not normalized — downstream consumers decide whether to
normalize (for Pearson correlation with key profiles, for example).
"""

from __future__ import annotations

import math
from typing import Iterable, List, Sequence, Tuple


PITCH_CLASS_NAMES: Tuple[str, ...] = (
    "C", "C#", "D", "D#", "E", "F",
    "F#", "G", "G#", "A", "A#", "B",
)


def chroma_for_notes(notes: Iterable) -> List[float]:
    """Return a single 12-bin chroma over all notes, weighted by
    ``duration_beats * velocity``. Velocity is treated as 0..127."""
    chroma = [0.0] * 12
    for n in notes:
        w = max(0.0, float(n.duration_beats)) * max(0.0, float(n.velocity))
        chroma[int(n.pitch) % 12] += w
    return chroma


def windowed_chroma(
    notes: Iterable,
    *,
    window_beats: float,
    total_beats: float,
    hop_beats: float = 0.0,
) -> Tuple[List[float], List[List[float]]]:
    """Return ``(window_starts, chromas)`` over ``[0, total_beats]``.

    Parameters
    ----------
    notes: iterable of note-likes with pitch/start_beat/duration_beats/velocity.
    window_beats: width of each analysis window in beats. Must be > 0.
    total_beats: upper limit of the analysis span. The last window starts
        at ``total_beats - window_beats`` unless ``total_beats`` is zero.
    hop_beats: distance between window starts. Defaults to ``window_beats``
        (non-overlapping). Must be positive.

    The returned chromas are per-window 12-element vectors (duration *
    velocity weighted). No normalization — the caller decides.
    """
    if window_beats <= 0:
        raise ValueError("window_beats must be > 0")
    if hop_beats <= 0:
        hop_beats = window_beats
    if total_beats <= 0:
        return [], []

    note_list = list(notes)

    # Number of windows so the last covers the end of the span. For
    # non-overlapping windows this is ceil(total / window).
    n_windows = max(1, int(math.ceil(total_beats / hop_beats)))
    starts = [i * hop_beats for i in range(n_windows)]

    chromas: List[List[float]] = [[0.0] * 12 for _ in range(n_windows)]
    for n in note_list:
        pc = int(n.pitch) % 12
        vel = max(0.0, float(n.velocity))
        n_start = float(n.start_beat)
        n_end = n_start + max(0.0, float(n.duration_beats))
        if n_end <= 0.0 or n_start >= total_beats:
            continue
        # First and last windows this note touches.
        first = max(0, int(math.floor(n_start / hop_beats)))
        last = min(n_windows - 1, int(math.floor((n_end - 1e-9) / hop_beats)))
        for w in range(first, last + 1):
            w_start = starts[w]
            w_end = w_start + window_beats
            overlap = max(0.0, min(w_end, n_end) - max(w_start, n_start))
            if overlap > 0:
                chromas[w][pc] += overlap * vel

    return starts, chromas
