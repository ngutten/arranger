"""Variation operations — resolve, create, flatten, delete."""

import copy
from ..state import (
    Note, Pattern, Variation, NoteDelta, AddedNote, SplitOp, Placement, PALETTE,
)


# ---------------------------------------------------------------------------
# Resolution: Variation → concrete note list
# ---------------------------------------------------------------------------

def resolve_variation(state, variation_id) -> list:
    """Resolve a variation to a concrete list of Notes.

    Steps:
    1. Deep-copy parent pattern notes (keyed by note_id)
    2. Apply split operations
    3. Apply modifications (deltas)
    4. Remove deleted notes
    5. Add new notes with reference-note positioning
    """
    var = state.find_variation(variation_id)
    if not var:
        return []
    pat = state.find_pattern(var.parent_id)
    if not pat:
        return []

    # 1. Deep-copy parent notes keyed by note_id
    notes_by_id = {}
    for n in pat.notes:
        notes_by_id[n.note_id] = Note(
            pitch=n.pitch, start=n.start, duration=n.duration,
            velocity=n.velocity,
            bend=[list(p) for p in n.bend] if n.bend else [],
            lyric=n.lyric, note_id=n.note_id,
            tags=copy.deepcopy(n.tags) if n.tags else {},
            attrs=copy.deepcopy(n.attrs) if n.attrs else {},
        )

    # Keep original parent notes for reference-note positioning
    parent_notes = {n.note_id: n for n in pat.notes}

    # 2. Apply split operations
    for sp in var.splits:
        orig = notes_by_id.get(sp.note_id)
        if not orig:
            continue
        # Left portion keeps original note_id
        left_dur = sp.split_offset
        right_dur = orig.duration - sp.split_offset
        if left_dur <= 0 or right_dur <= 0:
            continue

        right_start = orig.start + sp.split_offset
        right_id = sp.right_note_id

        # Create right note
        right_note = Note(
            pitch=orig.pitch, start=right_start, duration=right_dur,
            velocity=orig.velocity, bend=[], lyric='',
            note_id=right_id,
        )
        # Shorten left note
        orig.duration = left_dur
        orig.bend = []  # split clears bends

        # Apply left/right deltas if present
        if sp.left_delta:
            _apply_delta(orig, sp.left_delta)
        if sp.right_delta:
            _apply_delta(right_note, sp.right_delta)

        notes_by_id[right_id] = right_note

    # 3. Apply modifications
    for mod in var.modifications:
        note = notes_by_id.get(mod.note_id)
        if note:
            _apply_delta(note, mod)

    # 4. Remove deleted notes
    for nid in var.deletions:
        notes_by_id.pop(nid, None)

    # 5. Add new notes with reference-note positioning
    result = list(notes_by_id.values())

    for added in var.additions:
        note = Note(
            pitch=added.pitch, start=added.start, duration=added.duration,
            velocity=added.velocity,
            bend=[list(p) for p in added.bend] if added.bend else [],
            lyric=added.lyric, note_id=added.note_id,
            tags=copy.deepcopy(added.tags) if added.tags else {},
            attrs=copy.deepcopy(added.attrs) if added.attrs else {},
        )

        if added.ref_note_id:
            parent_ref = parent_notes.get(added.ref_note_id)
            resolved_ref = notes_by_id.get(added.ref_note_id)

            if resolved_ref and parent_ref:
                # Use stored offsets; fall back to computing from current parent for legacy data
                p_off = added.ref_pitch_offset if added.ref_pitch_offset is not None else (added.pitch - parent_ref.pitch)
                note.pitch = resolved_ref.pitch + p_off
                if added.ref_bind == 'full':
                    s_off = added.ref_start_offset if added.ref_start_offset is not None else (added.start - parent_ref.start)
                    d_off = added.ref_dur_offset if added.ref_dur_offset is not None else (added.duration - parent_ref.duration)
                    note.start = resolved_ref.start + s_off
                    note.duration = resolved_ref.duration + d_off

        result.append(note)

    # Sort by start time for consistency
    result.sort(key=lambda n: (n.start, n.pitch))
    return result


def compute_split_baselines(var, pat):
    """Compute delta-free baselines for all notes produced by splits.

    Walks the split chain in order, simulating resolution but capturing
    each note's geometry *before* its own delta is applied.

    Returns dict: note_id → (base_start, base_dur, base_pitch, base_vel)
    """
    baselines = {}
    # Running state: geometry of each note after deltas (for subsequent splits)
    geom = {}
    for n in pat.notes:
        geom[n.note_id] = (n.start, n.duration, n.pitch, n.velocity)

    for sp in var.splits:
        src = geom.get(sp.note_id)
        if not src:
            continue
        src_start, src_dur, src_pitch, src_vel = src

        # Left baseline (before left_delta)
        left_start = src_start
        left_dur = sp.split_offset
        baselines[sp.note_id] = (left_start, left_dur, src_pitch, src_vel)

        # Right baseline (before right_delta)
        right_start = src_start + sp.split_offset
        right_dur = src_dur - sp.split_offset
        baselines[sp.right_note_id] = (right_start, right_dur, src_pitch, src_vel)

        # Update running state WITH deltas for subsequent splits
        ls, ld, lp, lv = left_start, left_dur, src_pitch, src_vel
        if sp.left_delta:
            ls += sp.left_delta.d_start
            ld += sp.left_delta.d_duration
            lp += sp.left_delta.d_pitch
            lv += sp.left_delta.d_velocity
        geom[sp.note_id] = (ls, ld, lp, lv)

        rs, rd, rp, rv = right_start, right_dur, src_pitch, src_vel
        if sp.right_delta:
            rs += sp.right_delta.d_start
            rd += sp.right_delta.d_duration
            rp += sp.right_delta.d_pitch
            rv += sp.right_delta.d_velocity
        geom[sp.right_note_id] = (rs, rd, rp, rv)

    return baselines


def _apply_delta(note, delta):
    """Apply a NoteDelta to a Note in-place."""
    note.start += delta.d_start
    note.duration += delta.d_duration
    note.pitch += delta.d_pitch
    note.velocity += delta.d_velocity
    note.velocity = max(1, min(127, note.velocity))
    if delta.bend is not None:
        note.bend = [list(p) for p in delta.bend] if delta.bend else []
    if delta.lyric is not None:
        note.lyric = delta.lyric
    if delta.tags is not None:
        note.tags = copy.deepcopy(delta.tags)
    if delta.attrs is not None:
        note.attrs = copy.deepcopy(delta.attrs)


# ---------------------------------------------------------------------------
# Helper for schedule builders
# ---------------------------------------------------------------------------

def resolve_placement_notes(state, pl):
    """Returns (notes, pat_length, pat_key, pat_scale) for a placement.

    For regular placements, returns the pattern's notes directly.
    For variation placements, resolves the variation.
    Returns (None, 0, 'C', 'major') if the pattern/variation is not found.
    """
    if pl.is_variation:
        var = state.find_variation(pl.pattern_id)
        if not var:
            return (None, 0, 'C', 'major')
        pat = state.find_pattern(var.parent_id)
        if not pat:
            return (None, 0, 'C', 'major')
        notes = resolve_variation(state, var.id)
        return (notes, pat.length, pat.key, pat.scale)
    else:
        pat = state.find_pattern(pl.pattern_id)
        if not pat:
            return (None, 0, 'C', 'major')
        return (pat.notes, pat.length, pat.key, pat.scale)


# ---------------------------------------------------------------------------
# Create / flatten / delete
# ---------------------------------------------------------------------------

def create_variation(state, parent_id) -> Variation:
    """Create an empty variation from a parent pattern."""
    pat = state.find_pattern(parent_id)
    if not pat:
        return None
    # Ensure parent notes have IDs
    pat.ensure_note_ids(state.new_id)

    # Auto-number: count existing variations for this parent
    existing = state.variations_of(parent_id)
    num = len(existing) + 1
    var = Variation(
        id=state.new_id(),
        name=f'Variation {num}',
        parent_id=parent_id,
        color=pat.color,
    )
    state.variations.append(var)
    return var


def create_variation_from_repeat(state, placement_id, repeat_index) -> Variation:
    """Create a variation from a specific repeat of a placement.

    Splits the placement into up to 3 parts:
    - Left repeats (before the variation)
    - Variation placement (1 repeat)
    - Right repeats (after the variation)
    Removes zero-repeat placements.
    """
    pl = state.find_placement(placement_id)
    if not pl or pl.is_variation:
        return None
    pat = state.find_pattern(pl.pattern_id)
    if not pat:
        return None

    reps = pl.repeats or 1
    if repeat_index < 0 or repeat_index >= reps:
        return None

    # Ensure parent notes have IDs
    pat.ensure_note_ids(state.new_id)

    # Create the variation
    var = create_variation(state, pl.pattern_id)
    if not var:
        return None

    left_reps = repeat_index
    right_reps = reps - repeat_index - 1
    var_time = pl.time + repeat_index * pat.length

    # Modify original placement to cover left repeats
    if left_reps > 0:
        pl.repeats = left_reps
    else:
        # Remove original if no left repeats
        state.placements = [p for p in state.placements if p.id != pl.id]

    # Create variation placement
    var_pl = Placement(
        id=state.new_id(),
        track_id=pl.track_id,
        pattern_id=var.id,
        time=var_time,
        transpose=pl.transpose,
        repeats=1,
        target_key=pl.target_key,
        target_scale=pl.target_scale,
        is_variation=True,
    )
    state.placements.append(var_pl)

    # Create right repeats placement if needed
    if right_reps > 0:
        right_pl = Placement(
            id=state.new_id(),
            track_id=pl.track_id,
            pattern_id=pl.pattern_id,
            time=var_time + pat.length,
            transpose=pl.transpose,
            repeats=right_reps,
            target_key=pl.target_key,
            target_scale=pl.target_scale,
        )
        state.placements.append(right_pl)

    return var


def flatten_variation(state, variation_id) -> Pattern:
    """Resolve a variation to a concrete pattern and update referencing placements."""
    var = state.find_variation(variation_id)
    if not var:
        return None
    parent = state.find_pattern(var.parent_id)
    if not parent:
        return None

    notes = resolve_variation(state, variation_id)
    new_pat = Pattern(
        id=state.new_id(),
        name=var.name,
        length=parent.length,
        notes=notes,
        color=var.color,
        key=parent.key,
        scale=parent.scale,
        preview_mode=parent.preview_mode,
        overlay_mode=parent.overlay_mode,
    )
    state.patterns.append(new_pat)

    # Update placements that reference this variation
    for pl in state.placements:
        if pl.is_variation and pl.pattern_id == variation_id:
            pl.pattern_id = new_pat.id
            pl.is_variation = False

    # Remove the variation
    state.variations = [v for v in state.variations if v.id != variation_id]
    return new_pat


def delete_variation(state, variation_id):
    """Remove a variation and its placements."""
    state.placements = [
        p for p in state.placements
        if not (p.is_variation and p.pattern_id == variation_id)
    ]
    state.variations = [v for v in state.variations if v.id != variation_id]
    if state.sel_variation == variation_id:
        state.sel_variation = None


def delete_pattern_with_variations(state, pattern_id, mode='delete_all'):
    """Delete a pattern that has child variations.

    mode:
      'delete_all' — delete pattern + all variations + all placements
      'make_unique' — flatten all variations first, then delete pattern
      'abort' — do nothing
    """
    if mode == 'abort':
        return

    child_vars = state.variations_of(pattern_id)

    if mode == 'make_unique':
        for var in child_vars:
            flatten_variation(state, var.id)
        # Now delete the original pattern (no more variations)
        from .patterns import delete_pattern
        delete_pattern(state, pattern_id)
    elif mode == 'delete_all':
        # Delete all child variations and their placements
        var_ids = {v.id for v in child_vars}
        state.placements = [
            p for p in state.placements
            if not (p.is_variation and p.pattern_id in var_ids)
        ]
        state.variations = [v for v in state.variations if v.parent_id != pattern_id]
        if state.sel_variation and state.find_variation(state.sel_variation) is None:
            state.sel_variation = None
        # Delete the pattern itself
        from .patterns import delete_pattern
        delete_pattern(state, pattern_id)


# ---------------------------------------------------------------------------
# Note-level variation operations
# ---------------------------------------------------------------------------

def _find_split_for_note(var, note_id):
    """Find a SplitOp that owns *note_id* (left or right half).

    When a note appears in multiple splits (e.g. produced by one split
    and then split again), prefer the split where it is ``note_id``
    (left / input) over ``right_note_id`` (right / product).  This
    ensures modifications route to the most-direct owning split.

    Returns (SplitOp, 'left'|'right') or (None, None).
    """
    right_match = None
    for sp in var.splits:
        if sp.note_id == note_id:
            return sp, 'left'
        if sp.right_note_id == note_id and right_match is None:
            right_match = sp
    if right_match is not None:
        return right_match, 'right'
    return None, None


def variation_modify_note(var, note_id, **kwargs):
    """Create or update a NoteDelta for a note in a variation.

    If *note_id* belongs to a split, the delta is stored on the SplitOp
    (left_delta or right_delta) instead of var.modifications.

    kwargs can include: d_start, d_duration, d_pitch, d_velocity, bend,
    lyric, tags, attrs.
    """
    sp, side = _find_split_for_note(var, note_id)
    if sp is not None:
        # Store delta on the SplitOp
        attr = 'left_delta' if side == 'left' else 'right_delta'
        delta = getattr(sp, attr)
        if delta is None:
            delta = NoteDelta(note_id=note_id, **kwargs)
            setattr(sp, attr, delta)
        else:
            for k, v in kwargs.items():
                setattr(delta, k, v)
        return delta

    # Not a split note — use var.modifications
    for mod in var.modifications:
        if mod.note_id == note_id:
            for k, v in kwargs.items():
                setattr(mod, k, v)
            return mod
    # Create new
    delta = NoteDelta(note_id=note_id, **kwargs)
    var.modifications.append(delta)
    return delta


def variation_delete_note(var, note_id):
    """Mark a parent note as deleted in a variation."""
    if note_id not in var.deletions:
        var.deletions.append(note_id)
    # Remove any modification for this note
    var.modifications = [m for m in var.modifications if m.note_id != note_id]
    # Clear delta on split if this is a split half
    sp, side = _find_split_for_note(var, note_id)
    if sp is not None:
        if side == 'left':
            sp.left_delta = None
        else:
            sp.right_delta = None


def variation_undelete_note(var, note_id):
    """Restore a deleted parent note in a variation."""
    var.deletions = [nid for nid in var.deletions if nid != note_id]


def variation_add_note(state, var, pitch, start, duration, velocity=100, bend=None, lyric=''):
    """Add a new note to a variation. Auto-binds to nearest parent note."""
    added = AddedNote(
        note_id=state.new_id(),
        pitch=pitch, start=start, duration=duration,
        velocity=velocity,
        bend=bend or [], lyric=lyric,
    )
    bind_added_note_reference(state, var, added)
    var.additions.append(added)
    return added


def variation_remove_added_note(var, note_id):
    """Remove an added note from a variation."""
    var.additions = [a for a in var.additions if a.note_id != note_id]


def variation_split_added_note(state, var, note_id, split_offset):
    """Split an added note into two added notes.

    Unlike parent-note splits (SplitOp), added notes store absolute
    positions, so splitting is straightforward: shorten the original
    and create a new AddedNote for the right portion.

    Returns the new right-side AddedNote, or None if the note wasn't found.
    """
    orig = None
    for a in var.additions:
        if a.note_id == note_id:
            orig = a
            break
    if not orig:
        return None

    right_dur = orig.duration - split_offset
    if split_offset <= 0 or right_dur <= 0:
        return None

    right = AddedNote(
        note_id=state.new_id(),
        pitch=orig.pitch,
        start=orig.start + split_offset,
        duration=right_dur,
        velocity=orig.velocity,
        bend=[],
        lyric='',
        ref_note_id=orig.ref_note_id,
        ref_bind=orig.ref_bind,
    )
    # Update reference offsets for the right note
    if right.ref_note_id:
        pat = state.find_pattern(var.parent_id)
        if pat:
            ref = next((n for n in pat.notes if n.note_id == right.ref_note_id), None)
            if ref:
                right.ref_pitch_offset = right.pitch - ref.pitch
                right.ref_start_offset = right.start - ref.start
                right.ref_dur_offset = right.duration - ref.duration

    # Shorten the original (left portion)
    orig.duration = split_offset
    orig.bend = []
    # Update reference offsets for the shortened original
    if orig.ref_note_id:
        pat = state.find_pattern(var.parent_id)
        if pat:
            ref = next((n for n in pat.notes if n.note_id == orig.ref_note_id), None)
            if ref:
                orig.ref_dur_offset = orig.duration - ref.duration

    var.additions.append(right)
    return right


def variation_record_split(state, var, note_id, split_offset):
    """Record a split of a note in a variation.

    If the note already has a modification — either in var.modifications or
    as a delta on a previous SplitOp — the content fields (d_pitch,
    d_velocity, bend, lyric) are transferred to both left_delta and
    right_delta so they aren't lost.  Geometry fields (d_start, d_duration)
    are NOT transferred — the split redefines geometry.
    """
    right_id = state.new_id()

    # Collect content fields from any existing delta for this note.
    content = {}

    # Check var.modifications first
    existing_mod = None
    for mod in var.modifications:
        if mod.note_id == note_id:
            existing_mod = mod
            break

    if existing_mod:
        if existing_mod.d_pitch:
            content['d_pitch'] = existing_mod.d_pitch
        if existing_mod.d_velocity:
            content['d_velocity'] = existing_mod.d_velocity
        if existing_mod.bend is not None:
            content['bend'] = existing_mod.bend
        if existing_mod.lyric is not None:
            content['lyric'] = existing_mod.lyric
        var.modifications = [m for m in var.modifications if m.note_id != note_id]
    else:
        # Check if this note has a delta on a previous SplitOp
        prev_sp, prev_side = _find_split_for_note(var, note_id)
        if prev_sp is not None:
            prev_delta = (prev_sp.left_delta if prev_side == 'left'
                          else prev_sp.right_delta)
            if prev_delta:
                if prev_delta.d_pitch:
                    content['d_pitch'] = prev_delta.d_pitch
                if prev_delta.d_velocity:
                    content['d_velocity'] = prev_delta.d_velocity
                if prev_delta.bend is not None:
                    content['bend'] = prev_delta.bend
                if prev_delta.lyric is not None:
                    content['lyric'] = prev_delta.lyric
                # Clear content fields from the previous delta (geometry stays)
                prev_delta.d_pitch = 0
                prev_delta.d_velocity = 0
                prev_delta.bend = None
                prev_delta.lyric = None
                # If the previous delta is now empty, remove it
                if (abs(prev_delta.d_start) < 1e-9 and
                        abs(prev_delta.d_duration) < 1e-9):
                    if prev_side == 'left':
                        prev_sp.left_delta = None
                    else:
                        prev_sp.right_delta = None

    left_delta = None
    right_delta = None
    if content:
        left_delta = NoteDelta(note_id=note_id, **content)
        right_delta = NoteDelta(note_id=right_id, **content)

    sp = SplitOp(
        note_id=note_id,
        split_offset=split_offset,
        right_note_id=right_id,
        left_delta=left_delta,
        right_delta=right_delta,
    )
    var.splits.append(sp)
    return sp


# ---------------------------------------------------------------------------
# Reference note binding
# ---------------------------------------------------------------------------

def bind_added_note_reference(state, var, added_note):
    """Auto-bind an added note to the nearest parent note.

    Offsets are computed against *resolved* reference positions so that
    resolution (which applies offsets to resolved notes) reproduces the
    exact position the user clicked.
    """
    pat = state.find_pattern(var.parent_id)
    if not pat or not pat.notes:
        return

    # Resolve current variation to get on-screen positions of reference notes
    resolved = resolve_variation(state, var.id)
    resolved_by_id = {n.note_id: n for n in resolved}

    best_note = None
    best_score = float('inf')

    for pn in pat.notes:
        if pn.note_id in var.deletions:
            continue
        # Use resolved position for overlap/distance (matches what user sees)
        rn = resolved_by_id.get(pn.note_id, pn)
        overlap = _overlap(added_note.start, added_note.duration, rn.start, rn.duration)
        if overlap <= 0:
            continue  # only bind to overlapping notes
        pitch_dist = abs(added_note.pitch - rn.pitch)
        octave_dist = pitch_dist % 12
        octave_penalty = pitch_dist // 12

        score = -overlap * 10 + octave_dist + octave_penalty * 12
        if score < best_score:
            best_score = score
            best_note = pn

    if best_note:
        added_note.ref_note_id = best_note.note_id
        # Compute offsets against RESOLVED position (resolution applies them to resolved ref)
        rn = resolved_by_id.get(best_note.note_id, best_note)
        added_note.ref_pitch_offset = added_note.pitch - rn.pitch
        added_note.ref_start_offset = added_note.start - rn.start
        added_note.ref_dur_offset = added_note.duration - rn.duration
        # Exact match in time+duration → full bind (chord member)
        if (abs(added_note.start - rn.start) < 1e-6 and
                abs(added_note.duration - rn.duration) < 1e-6):
            added_note.ref_bind = 'full'
        else:
            added_note.ref_bind = 'pitch'


def _overlap(s1, d1, s2, d2):
    """Compute overlap in beats between two intervals."""
    return max(0, min(s1 + d1, s2 + d2) - max(s1, s2))
