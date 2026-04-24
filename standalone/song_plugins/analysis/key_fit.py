"""Key-fit scores using Krumhansl-Kessler profiles.

For each chroma vector, computes 24 Pearson correlations (12 majors, 12
minors). The result is either the per-window 24-vector (for heatmap
display) or a single best-pick (for the Key Detector plugin).

Keys are indexed by ``(root_pc, mode)`` where ``mode`` is ``"maj"`` or
``"min"`` and ``root_pc`` is 0..11 with 0=C.

This module is a deliberate thin layer — the same math lives in
``key_detector.py`` today. Plugins migrating over should import from
here; the duplicate in the detector will be removed once all consumers
are ported.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple


# Krumhansl-Kessler profiles (Temperley-refined weights also common but
# these are the classic values and match existing key_detector output).
KS_MAJOR: Tuple[float, ...] = (
    6.35, 2.23, 3.48, 2.33, 4.38, 4.09,
    2.52, 5.19, 2.39, 3.66, 2.29, 2.88,
)
KS_MINOR: Tuple[float, ...] = (
    6.33, 2.68, 3.52, 5.38, 2.60, 3.53,
    2.54, 4.75, 3.98, 2.69, 3.34, 3.17,
)


def _pearson(a: Sequence[float], b: Sequence[float]) -> float:
    n = len(a)
    if n == 0:
        return 0.0
    ma = sum(a) / n
    mb = sum(b) / n
    num = sum((ai - ma) * (bi - mb) for ai, bi in zip(a, b))
    da = math.sqrt(sum((ai - ma) ** 2 for ai in a))
    db = math.sqrt(sum((bi - mb) ** 2 for bi in b))
    if da == 0.0 or db == 0.0:
        return 0.0
    return num / (da * db)


def key_fit_scores(
    chroma: Sequence[float],
) -> Dict[Tuple[int, str], float]:
    """Return ``{(root, mode): correlation}`` for all 24 keys.

    An all-zero chroma returns zeros across the board.
    """
    out: Dict[Tuple[int, str], float] = {}
    if sum(chroma) == 0.0:
        for root in range(12):
            out[(root, "maj")] = 0.0
            out[(root, "min")] = 0.0
        return out
    for root in range(12):
        rot = tuple(chroma[(i + root) % 12] for i in range(12))
        out[(root, "maj")] = _pearson(KS_MAJOR, rot)
        out[(root, "min")] = _pearson(KS_MINOR, rot)
    return out


def best_key(
    chroma: Sequence[float],
    *,
    include_major: bool = True,
    include_minor: bool = True,
) -> Tuple[Optional[Tuple[int, str]], float, float]:
    """Return ``((root, mode), best_corr, runner_up_corr)``.

    The second value is the winning correlation, the third is the next
    best (used for confidence gating). Returns ``(None, 0, 0)`` on
    empty chroma.
    """
    if sum(chroma) == 0.0:
        return (None, 0.0, 0.0)
    cands: List[Tuple[float, int, str]] = []
    for root in range(12):
        rot = tuple(chroma[(i + root) % 12] for i in range(12))
        if include_major:
            cands.append((_pearson(KS_MAJOR, rot), root, "maj"))
        if include_minor:
            cands.append((_pearson(KS_MINOR, rot), root, "min"))
    cands.sort(reverse=True)
    if not cands:
        return (None, 0.0, 0.0)
    best = cands[0]
    runner = cands[1] if len(cands) > 1 else (0.0, 0, "maj")
    return ((best[1], best[2]), best[0], runner[0])
