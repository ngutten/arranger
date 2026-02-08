"""Undo/redo system for the arranger.

Captures snapshots of AppState and allows undo/redo navigation.
"""

import copy
from typing import Optional


class UndoStack:
    """Manages undo/redo history with snapshots."""
    
    def __init__(self, max_size: int = 100):
        self.max_size = max_size
        self.stack = []  # List of state snapshots
        self.pointer = -1  # Current position in stack (-1 = empty)
        
    def can_undo(self) -> bool:
        """Check if undo is available."""
        return self.pointer > 0
        
    def can_redo(self) -> bool:
        """Check if redo is available."""
        return self.pointer < len(self.stack) - 1
        
    def push(self, snapshot: dict):
        """Push a new snapshot onto the stack."""
        # Remove anything after current pointer (we branched)
        self.stack = self.stack[:self.pointer + 1]
        
        # Add new snapshot
        self.stack.append(snapshot)
        
        # Enforce max size
        if len(self.stack) > self.max_size:
            self.stack.pop(0)
        else:
            self.pointer += 1
            
    def undo(self) -> Optional[dict]:
        """Move back one step and return that snapshot."""
        if not self.can_undo():
            return None
        self.pointer -= 1
        return self.stack[self.pointer]
        
    def redo(self) -> Optional[dict]:
        """Move forward one step and return that snapshot."""
        if not self.can_redo():
            return None
        self.pointer += 1
        return self.stack[self.pointer]
        
    def clear(self):
        """Clear all history."""
        self.stack = []
        self.pointer = -1


def capture_state(state) -> dict:
    """Capture a serializable snapshot of AppState.
    
    Only captures the parts we want to undo/redo:
    - patterns, tracks, placements
    - beat_kit, beat_patterns, beat_tracks, beat_placements
    - bpm, snap, ts_num, ts_den
    
    Does NOT capture:
    - selection state (sel_pat, sel_trk, etc.)
    - playback state (playing, playhead, etc.)
    - sf2 (too large, handled separately)
    """
    return {
        'bpm': state.bpm,
        'snap': state.snap,
        'ts_num': state.ts_num,
        'ts_den': state.ts_den,
        'patterns': copy.deepcopy([p.to_dict() for p in state.patterns]),
        'tracks': copy.deepcopy([t.to_dict() for t in state.tracks]),
        'placements': copy.deepcopy([p.to_dict() for p in state.placements]),
        'beat_kit': copy.deepcopy([i.to_dict() for i in state.beat_kit]),
        'beat_patterns': copy.deepcopy([p.to_dict() for p in state.beat_patterns]),
        'beat_tracks': copy.deepcopy([t.to_dict() for t in state.beat_tracks]),
        'beat_placements': copy.deepcopy([p.to_dict() for p in state.beat_placements]),
        '_next_id': state._next_id,
    }


def restore_state(state, snapshot: dict):
    """Restore AppState from a snapshot.
    
    Preserves selection and playback state.
    """
    from .state import (Pattern, Track, Placement, BeatInstrument, 
                       BeatPattern, BeatTrack, BeatPlacement)
    
    state.bpm = snapshot['bpm']
    state.snap = snapshot['snap']
    state.ts_num = snapshot['ts_num']
    state.ts_den = snapshot['ts_den']
    
    state.patterns = [Pattern.from_dict(p) for p in snapshot['patterns']]
    state.tracks = [Track.from_dict(t) for t in snapshot['tracks']]
    state.placements = [Placement.from_dict(p) for p in snapshot['placements']]
    
    state.beat_kit = [BeatInstrument.from_dict(i) for i in snapshot['beat_kit']]
    state.beat_patterns = [BeatPattern.from_dict(p) for p in snapshot['beat_patterns']]
    state.beat_tracks = [BeatTrack.from_dict(t) for t in snapshot['beat_tracks']]
    state.beat_placements = [BeatPlacement.from_dict(p) for p in snapshot['beat_placements']]
    
    state._next_id = snapshot['_next_id']
    
    # Clear selections that reference deleted objects
    if state.sel_pat and not state.find_pattern(state.sel_pat):
        state.sel_pat = None
    if state.sel_trk and not state.find_track(state.sel_trk):
        state.sel_trk = None
    if state.sel_pl and not state.find_placement(state.sel_pl):
        state.sel_pl = None
    if state.sel_beat_pat and not state.find_beat_pattern(state.sel_beat_pat):
        state.sel_beat_pat = None
    if state.sel_beat_trk and not state.find_beat_track(state.sel_beat_trk):
        state.sel_beat_trk = None
    if state.sel_beat_pl and not state.find_beat_placement(state.sel_beat_pl):
        state.sel_beat_pl = None
