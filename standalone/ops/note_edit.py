"""Note editing operations for the piano roll.

Pure functions that operate on pattern notes and selection state.
The PianoRoll widget owns the selection set and ghost notes list;
these functions take them as arguments and return results rather
than mutating widget state directly.
"""

from ..state import Note


def get_selected_notes(pat, selected):
    """Get Note objects for a set of selected indices.
    
    Returns list of (index, note) pairs, sorted by index.
    """
    if not pat:
        return []
    return [(i, pat.notes[i]) for i in sorted(selected)
            if 0 <= i < len(pat.notes)]


def delete_selected(pat, selected):
    """Delete notes at selected indices from pattern.
    
    Mutates pat.notes in place. Returns new (empty) selection set.
    """
    for idx in sorted(selected, reverse=True):
        if 0 <= idx < len(pat.notes):
            pat.notes.pop(idx)
    return set()


def delete_note_at(pat, index, selected):
    """Delete a single note and fix up selection indices.
    
    Returns updated selection set.
    """
    if 0 <= index < len(pat.notes):
        pat.notes.pop(index)
        selected.discard(index)
        return {idx - 1 if idx > index else idx for idx in selected}
    return selected


def duplicate_notes(pat, selected, clipboard_notes, offset_beats):
    """Duplicate notes by adding copies at an offset.
    
    Args:
        pat: Pattern to add notes to
        selected: Current selection set (unused notes, for extent calc)
        clipboard_notes: List of Note objects to duplicate
        offset_beats: Beat offset for duplicated notes
        
    Returns new selection set pointing to the duplicated notes.
    """
    new_indices = []
    for note in clipboard_notes:
        new_note = Note(
            pitch=note.pitch,
            start=note.start + offset_beats,
            duration=note.duration,
            velocity=note.velocity,
            bend=[list(p) for p in note.bend] if note.bend else [],
        )
        pat.notes.append(new_note)
        new_indices.append(len(pat.notes) - 1)
    return set(new_indices)


def commit_ghost_notes(pat, ghost_notes, beat, pitch, snap_fn,
                       lo_pitch, hi_pitch):
    """Place ghost notes into a pattern at the given position.
    
    Args:
        pat: Target pattern
        ghost_notes: List of Note objects to place
        beat: Mouse beat position
        pitch: Mouse pitch position
        snap_fn: Snap function (beat -> snapped_beat)
        lo_pitch, hi_pitch: Valid pitch range
        
    Returns new selection set for the placed notes.
    """
    if not ghost_notes:
        return set()
    
    min_start = min(n.start for n in ghost_notes)
    min_pitch = min(n.pitch for n in ghost_notes)
    
    beat_offset = snap_fn(beat) - min_start
    pitch_offset = pitch - min_pitch
    
    new_indices = []
    for note in ghost_notes:
        new_note = Note(
            pitch=max(lo_pitch, min(hi_pitch, note.pitch + pitch_offset)),
            start=max(0, note.start + beat_offset),
            duration=note.duration,
            velocity=note.velocity,
            bend=[list(p) for p in note.bend] if note.bend else [],
        )
        pat.notes.append(new_note)
        new_indices.append(len(pat.notes) - 1)
    
    return set(new_indices)


def merge_notes(pat, selected):
    """Merge two selected notes at the same pitch.
    
    Requires exactly 2 selected notes at the same pitch.
    Returns updated selection set, or None if merge not possible.
    """
    if len(selected) != 2:
        return None
    
    indices = sorted(selected)
    idx1, idx2 = indices
    
    if idx1 >= len(pat.notes) or idx2 >= len(pat.notes):
        return None
    
    n1, n2 = pat.notes[idx1], pat.notes[idx2]
    
    if n1.pitch != n2.pitch:
        return None
    
    # Ensure n1 is the earlier note
    if n1.start > n2.start:
        n1, n2 = n2, n1
        idx1, idx2 = idx2, idx1
    
    # Extend n1 to cover both
    end1 = n1.start + n1.duration
    end2 = n2.start + n2.duration
    n1.duration = max(end1, end2) - n1.start
    # Bend curves from two different notes can't be meaningfully combined — strip them
    n1.bend = []

    # Delete n2
    pat.notes.pop(idx2)

    return {idx1}


def marquee_select(pat, start_point, end_point, bw, nh, hi_pitch):
    """Find notes within a marquee rectangle.
    
    Args:
        pat: Pattern with notes
        start_point: (x, y) start of marquee
        end_point: (x, y) end of marquee
        bw: Pixels per beat
        nh: Note row height in pixels
        hi_pitch: Highest displayed pitch
        
    Returns set of selected note indices.
    """
    min_x = min(start_point[0], end_point[0])
    max_x = max(start_point[0], end_point[0])
    min_y = min(start_point[1], end_point[1])
    max_y = max(start_point[1], end_point[1])
    
    selected = set()
    for i, n in enumerate(pat.notes):
        note_x = n.start * bw
        note_y = (hi_pitch - n.pitch) * nh
        note_w = n.duration * bw
        note_h = nh
        
        if (note_x < max_x and note_x + note_w > min_x and
                note_y < max_y and note_y + note_h > min_y):
            selected.add(i)
    
    return selected
