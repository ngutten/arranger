"""Track, beat track, and beat instrument create/delete operations."""

from ..state import Track, BeatTrack, BeatInstrument


def add_track(state):
    """Create a new melodic track. Returns it."""
    t = Track(
        id=state.new_id(),
        name=f'Track {len(state.tracks) + 1}',
        channel=len(state.tracks) % 16,
    )
    state.tracks.append(t)
    state.sel_trk = t.id
    state.notify('add_track')
    return t


def delete_track(state, tid):
    """Delete a track and its placements.
    
    Returns set of deleted placement IDs.
    """
    deleted_ids = {p.id for p in state.placements if p.track_id == tid}
    state.tracks = [t for t in state.tracks if t.id != tid]
    state.placements = [p for p in state.placements if p.track_id != tid]
    if state.sel_trk == tid:
        state.sel_trk = state.tracks[0].id if state.tracks else None
    state.notify('delete_track')
    return deleted_ids


def add_beat_track(state):
    """Create a new beat track. Returns it."""
    bt = BeatTrack(
        id=state.new_id(),
        name=f'Beat {len(state.beat_tracks) + 1}',
    )
    state.beat_tracks.append(bt)
    state.sel_beat_trk = bt.id
    state.notify('add_beat_track')
    return bt


def delete_beat_track(state, btid):
    """Delete a beat track and its placements.
    
    Returns set of deleted beat placement IDs.
    """
    deleted_ids = {p.id for p in state.beat_placements if p.track_id == btid}
    state.beat_tracks = [t for t in state.beat_tracks if t.id != btid]
    state.beat_placements = [p for p in state.beat_placements
                             if p.track_id != btid]
    if state.sel_beat_trk == btid:
        state.sel_beat_trk = (state.beat_tracks[0].id
                              if state.beat_tracks else None)
    state.notify('delete_beat_track')
    return deleted_ids


def add_beat_instrument(state):
    """Add an instrument to the beat kit. Returns it."""
    inst = BeatInstrument(
        id=state.new_id(),
        name=f'Inst {len(state.beat_kit) + 1}',
        channel=9,
        pitch=36,
        velocity=100,
    )
    state.beat_kit.append(inst)
    state.notify('beat_kit')
    return inst


def delete_beat_instrument(state, iid):
    """Remove an instrument from the beat kit and clean up grids."""
    state.beat_kit = [i for i in state.beat_kit if i.id != iid]
    for pat in state.beat_patterns:
        if iid in pat.grid:
            del pat.grid[iid]
    state.notify('beat_kit')
