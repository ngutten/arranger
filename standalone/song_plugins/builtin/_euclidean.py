"""Bjorklund's algorithm for maximally-even rhythms.

Implementation based on the classic Toussaint-Demaine recursion. The output
is the canonical form starting with a pulse at position 0.
"""

from __future__ import annotations

from typing import List


def euclidean_pattern(pulses: int, steps: int) -> List[int]:
    """Return sorted indices (0-based) of positions where pulses land in a
    maximally-even distribution over ``steps`` slots.

    Edge cases:
      - ``pulses <= 0`` or ``steps <= 0`` returns ``[]``.
      - ``pulses >= steps`` fills every slot.
    """
    if pulses <= 0 or steps <= 0:
        return []
    if pulses >= steps:
        return list(range(steps))

    # Classic Bjorklund: seed with ``pulses`` ones and ``steps - pulses`` zeros,
    # repeatedly fold the shorter run onto the longer until the shorter run
    # has length <= 1.
    groups: List[List[int]] = (
        [[1] for _ in range(pulses)]
        + [[0] for _ in range(steps - pulses)]
    )
    while True:
        last = groups[-1]
        tail_count = 0
        for g in reversed(groups):
            if g == last:
                tail_count += 1
            else:
                break
        head_count = len(groups) - tail_count
        if head_count <= 1 or tail_count <= 1:
            break
        merges = min(head_count, tail_count)
        new_groups: List[List[int]] = []
        for i in range(merges):
            new_groups.append(groups[i] + groups[-(merges - i)])
        if head_count > tail_count:
            new_groups.extend(groups[merges:head_count])
        elif tail_count > head_count:
            new_groups.extend(groups[head_count:tail_count])
        groups = new_groups

    flat = [x for g in groups for x in g]
    positions = [i for i, v in enumerate(flat) if v == 1]
    # Rotate so a pulse sits at position 0 (canonical form).
    if positions and positions[0] != 0:
        shift = positions[0]
        positions = sorted((p - shift) % steps for p in positions)
    return positions
