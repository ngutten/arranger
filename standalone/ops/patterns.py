"""Pattern and beat pattern create/edit/duplicate/delete operations."""

from ..state import Pattern, BeatPattern, PALETTE


def add_pattern(state):
    """Create a new melodic pattern with defaults.
    
    Returns the new Pattern.
    """
    pat = Pattern(
        id=state.new_id(),
        name=f'Pattern {len(state.patterns) + 1}',
        length=state.ts_num,
        notes=[],
        color=PALETTE[len(state.patterns) % len(PALETTE)],
        key='C',
        scale='major',
    )
    state.patterns.append(pat)
    state.sel_pat = pat.id
    state.sel_beat_pat = None
    state.notify('pattern_dialog')
    return pat


def duplicate_pattern(state, pid):
    """Duplicate a pattern. Returns the new Pattern or None."""
    pat = state.find_pattern(pid)
    if not pat:
        return None
    from ..state import Note
    new_pat = Pattern(
        id=state.new_id(),
        name=f'{pat.name} (copy)',
        length=pat.length,
        notes=[Note(pitch=n.pitch, start=n.start, duration=n.duration,
                    velocity=n.velocity) for n in pat.notes],
        color=pat.color,
        key=pat.key,
        scale=pat.scale,
    )
    state.patterns.append(new_pat)
    state.sel_pat = new_pat.id
    state.notify('duplicate_pattern')
    return new_pat


def delete_pattern(state, pid):
    """Delete a pattern and its placements.
    
    Returns set of deleted placement IDs (caller should clean up
    any UI selection state referencing these).
    """
    deleted_placement_ids = {p.id for p in state.placements if p.pattern_id == pid}
    state.patterns = [p for p in state.patterns if p.id != pid]
    state.placements = [p for p in state.placements if p.pattern_id != pid]
    if state.sel_pat == pid:
        state.sel_pat = state.patterns[0].id if state.patterns else None
    state.notify('delete_pattern')
    return deleted_placement_ids


def add_beat_pattern(state):
    """Create a new beat pattern with defaults.
    
    Returns the new BeatPattern.
    """
    grid = {}
    for inst in state.beat_kit:
        grid[inst.id] = [0] * (state.ts_num * 4)
    pat = BeatPattern(
        id=state.new_id(),
        name=f'Beat {len(state.beat_patterns) + 1}',
        length=state.ts_num,
        subdivision=4,
        color=PALETTE[len(state.beat_patterns) % len(PALETTE)],
        grid=grid,
    )
    state.beat_patterns.append(pat)
    state.sel_beat_pat = pat.id
    state.sel_pat = None
    state.notify('beat_pattern_dialog')
    return pat


def duplicate_beat_pattern(state, pid):
    """Duplicate a beat pattern. Returns the new BeatPattern or None."""
    pat = state.find_beat_pattern(pid)
    if not pat:
        return None
    new_pat = BeatPattern(
        id=state.new_id(),
        name=f'{pat.name} (copy)',
        length=pat.length,
        subdivision=pat.subdivision,
        color=pat.color,
        grid={k: list(v) for k, v in pat.grid.items()},
    )
    state.beat_patterns.append(new_pat)
    state.sel_beat_pat = new_pat.id
    state.notify('duplicate_beat_pattern')
    return new_pat


def delete_beat_pattern(state, pid):
    """Delete a beat pattern and its placements.
    
    Returns set of deleted beat placement IDs.
    """
    deleted_ids = {p.id for p in state.beat_placements if p.pattern_id == pid}
    state.beat_patterns = [p for p in state.beat_patterns if p.id != pid]
    state.beat_placements = [p for p in state.beat_placements if p.pattern_id != pid]
    if state.sel_beat_pat == pid:
        state.sel_beat_pat = (state.beat_patterns[0].id
                              if state.beat_patterns else None)
    state.notify('delete_beat_pattern')
    return deleted_ids
