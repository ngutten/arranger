"""Clipboard and marquee selection for arrangement and piano roll views.

Handles marquee selection, copy, cut, paste of placements and notes.
Maintains separate clipboards for arrangement and piano roll.
"""

import copy
from dataclasses import dataclass
from typing import Optional, List, Tuple
from PySide6.QtCore import QRectF, QPointF


# ============================================================================
# ARRANGEMENT CLIPBOARD
# ============================================================================

@dataclass
class ClipboardData:
    """Data structure for clipboard contents."""
    placements: List[dict]  # Serialized Placement objects
    beat_placements: List[dict]  # Serialized BeatPlacement objects
    min_time: float  # For relative positioning on paste
    track_count: int  # How many unique tracks
    beat_track_count: int


class ArrangementClipboard:
    """Manages clipboard operations for the arrangement view."""
    
    def __init__(self):
        self.data: Optional[ClipboardData] = None
        
    def copy(self, placements, beat_placements, state):
        """Copy placements to clipboard."""
        if not placements and not beat_placements:
            return
            
        # Serialize the placements
        pl_dicts = [p.to_dict() for p in placements]
        bp_dicts = [bp.to_dict() for bp in beat_placements]
        
        # Find min time for relative positioning
        min_time = float('inf')
        if placements:
            min_time = min(min_time, min(p.time for p in placements))
        if beat_placements:
            min_time = min(min_time, min(bp.time for bp in beat_placements))
        if min_time == float('inf'):
            min_time = 0
            
        # Count unique tracks
        track_ids = set(p.track_id for p in placements)
        beat_track_ids = set(bp.track_id for bp in beat_placements)
        
        self.data = ClipboardData(
            placements=pl_dicts,
            beat_placements=bp_dicts,
            min_time=min_time,
            track_count=len(track_ids),
            beat_track_count=len(beat_track_ids),
        )
        
        print(f"[CLIPBOARD] Copied {len(placements)} placements, {len(beat_placements)} beat placements")
        
    def paste(self, at_time: float, state):
        """Paste clipboard contents at specified time.
        
        Returns (new_placements, new_beat_placements).
        """
        if not self.data:
            print("[CLIPBOARD] Nothing to paste")
            return [], []
            
        # Import here to avoid circular imports
        from .state import Placement, BeatPlacement
        
        # Calculate time offset
        time_offset = at_time - self.data.min_time
        
        # Create new placements with offset times and new IDs
        new_placements = []
        for p_dict in self.data.placements:
            pl = Placement.from_dict(p_dict)
            pl.id = state.new_id()
            pl.time += time_offset
            # Ensure track still exists
            if state.find_track(pl.track_id):
                new_placements.append(pl)
            else:
                print(f"[CLIPBOARD] Skipping placement - track {pl.track_id} not found")
                
        new_beat_placements = []
        for bp_dict in self.data.beat_placements:
            bp = BeatPlacement.from_dict(bp_dict)
            bp.id = state.new_id()
            bp.time += time_offset
            # Ensure track still exists
            if state.find_beat_track(bp.track_id):
                new_beat_placements.append(bp)
            else:
                print(f"[CLIPBOARD] Skipping beat placement - track {bp.track_id} not found")
                
        print(f"[CLIPBOARD] Pasted {len(new_placements)} placements, {len(new_beat_placements)} beat placements at time {at_time}")
        return new_placements, new_beat_placements
        
    def has_data(self) -> bool:
        """Check if clipboard has data."""
        return self.data is not None


class MarqueeSelection:
    """Handles marquee (rectangular) selection in arrangement view."""
    
    def __init__(self):
        self.is_active = False
        self.start_x = 0
        self.start_y = 0
        self.current_x = 0
        self.current_y = 0
        
    def start(self, x: float, y: float):
        """Start marquee selection."""
        self.is_active = True
        self.start_x = x
        self.start_y = y
        self.current_x = x
        self.current_y = y
        
    def update(self, x: float, y: float):
        """Update marquee selection."""
        if self.is_active:
            self.current_x = x
            self.current_y = y
            
    def finish(self) -> QRectF:
        """Finish marquee and return selection rectangle."""
        if not self.is_active:
            return QRectF()
            
        self.is_active = False
        
        # Build normalized rectangle
        x1 = min(self.start_x, self.current_x)
        y1 = min(self.start_y, self.current_y)
        x2 = max(self.start_x, self.current_x)
        y2 = max(self.start_y, self.current_y)
        
        return QRectF(x1, y1, x2 - x1, y2 - y1)
        
    def cancel(self):
        """Cancel marquee selection."""
        self.is_active = False
        
    def get_rect(self) -> QRectF:
        """Get current selection rectangle (for drawing)."""
        if not self.is_active:
            return QRectF()
            
        x1 = min(self.start_x, self.current_x)
        y1 = min(self.start_y, self.current_y)
        x2 = max(self.start_x, self.current_x)
        y2 = max(self.start_y, self.current_y)
        
        return QRectF(x1, y1, x2 - x1, y2 - y1)


def select_placements_in_rect(rect: QRectF, state, bw: float, th: float) -> Tuple[List, List]:
    """Find all placements that intersect with the selection rectangle.
    
    Args:
        rect: Selection rectangle in pixels
        state: AppState
        bw: Beat width in pixels
        th: Track height in pixels
        
    Returns:
        (selected_placements, selected_beat_placements)
    """
    selected_pls = []
    selected_bps = []
    
    # Convert rect to beat coordinates
    t1 = rect.left() / bw
    t2 = rect.right() / bw
    track1 = int(rect.top() / th)
    track2 = int(rect.bottom() / th)
    
    # Check melodic placements
    for i, track in enumerate(state.tracks):
        if i < track1 or i > track2:
            continue
            
        for pl in state.placements:
            if pl.track_id != track.id:
                continue
                
            pat = state.find_pattern(pl.pattern_id)
            if not pat:
                continue
                
            pl_start = pl.time
            pl_end = pl.time + pat.length * (pl.repeats or 1)
            
            # Check if placement intersects selection time range
            if pl_end > t1 and pl_start < t2:
                selected_pls.append(pl)
                
    # Check beat placements
    beat_track_offset = len(state.tracks)
    for i, track in enumerate(state.beat_tracks):
        track_idx = beat_track_offset + i
        if track_idx < track1 or track_idx > track2:
            continue
            
        for bp in state.beat_placements:
            if bp.track_id != track.id:
                continue
                
            pat = state.find_beat_pattern(bp.pattern_id)
            if not pat:
                continue
                
            bp_start = bp.time
            bp_end = bp.time + pat.length * (bp.repeats or 1)
            
            if bp_end > t1 and bp_start < t2:
                selected_bps.append(bp)
                
    return selected_pls, selected_bps


# ============================================================================
# PIANO ROLL NOTE CLIPBOARD
# ============================================================================

class NoteClipboard:
    """Manages clipboard operations for piano roll notes.
    
    Separate from ArrangementClipboard to allow independent copy/paste
    of notes and arrangement placements.
    """
    
    def __init__(self):
        self.notes: List = []  # List of Note objects
        
    def copy(self, notes):
        """Copy notes to clipboard.
        
        Args:
            notes: List of Note objects to copy
        """
        if not notes:
            return
            
        # Import here to avoid circular imports
        from .state import Note
        
        self.notes = [
            Note(
                pitch=n.pitch,
                start=n.start,
                duration=n.duration,
                velocity=n.velocity
            ) for n in notes
        ]
        
        print(f"[NOTE CLIPBOARD] Copied {len(self.notes)} notes")
        
    def paste(self):
        """Get clipboard contents as new Note objects.
        
        Returns:
            List of Note objects (copies of clipboard contents)
        """
        if not self.notes:
            print("[NOTE CLIPBOARD] Nothing to paste")
            return []
            
        # Import here to avoid circular imports
        from .state import Note
        
        # Return copies so clipboard is not modified
        copied = [
            Note(
                pitch=n.pitch,
                start=n.start,
                duration=n.duration,
                velocity=n.velocity
            ) for n in self.notes
        ]
        
        print(f"[NOTE CLIPBOARD] Pasted {len(copied)} notes")
        return copied
        
    def has_data(self) -> bool:
        """Check if clipboard has notes."""
        return len(self.notes) > 0
        
    def clear(self):
        """Clear clipboard."""
        self.notes = []

