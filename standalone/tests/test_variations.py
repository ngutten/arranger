"""Tests for the variation system."""

import pytest
from standalone.state import (
    AppState, Track, Pattern, Note, Placement, Variation,
    NoteDelta, AddedNote, SplitOp,
)
from standalone.ops.variations import (
    resolve_variation, resolve_placement_notes,
    create_variation, create_variation_from_repeat,
    flatten_variation, delete_variation, delete_pattern_with_variations,
    variation_modify_note, variation_delete_note, variation_undelete_note,
    variation_add_note, variation_remove_added_note, variation_record_split,
    variation_split_added_note, compute_split_baselines,
    bind_added_note_reference, _find_split_for_note,
)


@pytest.fixture
def var_state():
    """State with one track, one pattern (3 notes with IDs), one placement."""
    s = AppState()
    s.bpm = 120

    t = Track(id=s.new_id(), name="Piano", channel=0)
    s.tracks.append(t)

    n1 = Note(pitch=60, start=0.0, duration=1.0, velocity=100, note_id=s.new_id())
    n2 = Note(pitch=64, start=1.0, duration=1.0, velocity=80, note_id=s.new_id())
    n3 = Note(pitch=67, start=2.0, duration=1.0, velocity=90, note_id=s.new_id())

    p = Pattern(
        id=s.new_id(), name="Pat1", length=4.0,
        notes=[n1, n2, n3], color="#ff0000", key="C", scale="major",
    )
    s.patterns.append(p)

    pl = Placement(id=s.new_id(), track_id=t.id, pattern_id=p.id, time=0.0, repeats=2)
    s.placements.append(pl)

    return s


class TestResolveVariation:
    def test_empty_variation_returns_parent_notes(self, var_state):
        s = var_state
        pat = s.patterns[0]
        var = create_variation(s, pat.id)
        notes = resolve_variation(s, var.id)
        assert len(notes) == 3
        assert [n.pitch for n in notes] == [60, 64, 67]

    def test_modification_applies_delta(self, var_state):
        s = var_state
        pat = s.patterns[0]
        var = create_variation(s, pat.id)
        nid = pat.notes[0].note_id
        variation_modify_note(var, nid, d_pitch=2, d_velocity=10)
        notes = resolve_variation(s, var.id)
        modified = next(n for n in notes if n.note_id == nid)
        assert modified.pitch == 62  # 60 + 2
        assert modified.velocity == 110  # 100 + 10

    def test_deletion_removes_note(self, var_state):
        s = var_state
        pat = s.patterns[0]
        var = create_variation(s, pat.id)
        nid = pat.notes[1].note_id
        variation_delete_note(var, nid)
        notes = resolve_variation(s, var.id)
        assert len(notes) == 2
        assert nid not in {n.note_id for n in notes}

    def test_undelete_restores_note(self, var_state):
        s = var_state
        pat = s.patterns[0]
        var = create_variation(s, pat.id)
        nid = pat.notes[1].note_id
        variation_delete_note(var, nid)
        assert len(resolve_variation(s, var.id)) == 2
        variation_undelete_note(var, nid)
        assert len(resolve_variation(s, var.id)) == 3

    def test_addition_adds_note(self, var_state):
        s = var_state
        pat = s.patterns[0]
        var = create_variation(s, pat.id)
        added = variation_add_note(s, var, pitch=72, start=3.0, duration=0.5, velocity=100)
        notes = resolve_variation(s, var.id)
        assert len(notes) == 4
        assert any(n.note_id == added.note_id and n.pitch == 72 for n in notes)

    def test_split_creates_two_notes(self, var_state):
        s = var_state
        pat = s.patterns[0]
        var = create_variation(s, pat.id)
        nid = pat.notes[0].note_id  # duration=1.0
        sp = variation_record_split(s, var, nid, 0.5)
        notes = resolve_variation(s, var.id)
        # Original 3 notes → split makes 4 (left portion + right portion + 2 others)
        assert len(notes) == 4
        left = next(n for n in notes if n.note_id == nid)
        right = next(n for n in notes if n.note_id == sp.right_note_id)
        assert abs(left.duration - 0.5) < 1e-6
        assert abs(right.start - 0.5) < 1e-6
        assert abs(right.duration - 0.5) < 1e-6

    def test_modification_delta_overrides_bend(self, var_state):
        s = var_state
        pat = s.patterns[0]
        var = create_variation(s, pat.id)
        nid = pat.notes[0].note_id
        variation_modify_note(var, nid, bend=[[0.0, 1.0], [0.5, -1.0]])
        notes = resolve_variation(s, var.id)
        modified = next(n for n in notes if n.note_id == nid)
        assert modified.bend == [[0.0, 1.0], [0.5, -1.0]]

    def test_velocity_clamped(self, var_state):
        s = var_state
        pat = s.patterns[0]
        var = create_variation(s, pat.id)
        nid = pat.notes[0].note_id  # vel=100
        variation_modify_note(var, nid, d_velocity=100)  # would be 200
        notes = resolve_variation(s, var.id)
        modified = next(n for n in notes if n.note_id == nid)
        assert modified.velocity == 127


class TestResolveplacementNotes:
    def test_regular_placement(self, var_state):
        s = var_state
        pl = s.placements[0]
        notes, length, key, scale = resolve_placement_notes(s, pl)
        assert notes is not None
        assert len(notes) == 3
        assert length == 4.0

    def test_variation_placement(self, var_state):
        s = var_state
        pat = s.patterns[0]
        var = create_variation(s, pat.id)
        variation_delete_note(var, pat.notes[0].note_id)
        pl = Placement(id=s.new_id(), track_id=s.tracks[0].id,
                        pattern_id=var.id, time=0.0, repeats=1,
                        is_variation=True)
        s.placements.append(pl)
        notes, length, key, scale = resolve_placement_notes(s, pl)
        assert notes is not None
        assert len(notes) == 2  # one deleted
        assert length == 4.0

    def test_missing_pattern(self, var_state):
        s = var_state
        pl = Placement(id=s.new_id(), track_id=s.tracks[0].id,
                        pattern_id=9999, time=0.0, repeats=1)
        notes, length, key, scale = resolve_placement_notes(s, pl)
        assert notes is None


class TestCreateVariation:
    def test_creates_empty_variation(self, var_state):
        s = var_state
        pat = s.patterns[0]
        var = create_variation(s, pat.id)
        assert var is not None
        assert var.parent_id == pat.id
        assert var.name == 'Variation 1'
        assert len(var.modifications) == 0
        assert len(var.deletions) == 0
        assert len(var.additions) == 0
        assert len(s.variations) == 1
        # Second variation should auto-number
        var2 = create_variation(s, pat.id)
        assert var2.name == 'Variation 2'
        assert len(s.variations) == 2

    def test_ensures_note_ids(self, var_state):
        s = var_state
        pat = s.patterns[0]
        # Clear note IDs
        for n in pat.notes:
            n.note_id = 0
        create_variation(s, pat.id)
        for n in pat.notes:
            assert n.note_id != 0


class TestCreateVariationFromRepeat:
    def test_splits_placement(self, var_state):
        s = var_state
        pl = s.placements[0]
        assert pl.repeats == 2
        var = create_variation_from_repeat(s, pl.id, 1)
        assert var is not None
        regular_pls = [p for p in s.placements if not p.is_variation and p.pattern_id == s.patterns[0].id]
        var_pls = [p for p in s.placements if p.is_variation]
        assert len(var_pls) == 1
        assert var_pls[0].pattern_id == var.id
        assert len(regular_pls) == 1
        assert regular_pls[0].repeats == 1

    def test_splits_into_three_parts(self, var_state):
        s = var_state
        pl = s.placements[0]
        pl.repeats = 4
        var = create_variation_from_repeat(s, pl.id, 2)
        assert var is not None
        regular_pls = [p for p in s.placements if not p.is_variation and p.pattern_id == s.patterns[0].id]
        var_pls = [p for p in s.placements if p.is_variation]
        assert len(var_pls) == 1
        assert len(regular_pls) == 2  # left + right
        left = min(regular_pls, key=lambda p: p.time)
        right = max(regular_pls, key=lambda p: p.time)
        assert left.repeats == 2
        assert right.repeats == 1


class TestFlattenVariation:
    def test_creates_pattern_and_updates_placements(self, var_state):
        s = var_state
        pat = s.patterns[0]
        var = create_variation(s, pat.id)
        variation_modify_note(var, pat.notes[0].note_id, d_pitch=5)
        var_pl = Placement(id=s.new_id(), track_id=s.tracks[0].id,
                           pattern_id=var.id, time=4.0, repeats=1,
                           is_variation=True)
        s.placements.append(var_pl)
        new_pat = flatten_variation(s, var.id)
        assert new_pat is not None
        assert len(s.variations) == 0
        updated_pl = s.find_placement(var_pl.id)
        assert updated_pl.pattern_id == new_pat.id
        assert not updated_pl.is_variation
        assert any(n.pitch == 65 for n in new_pat.notes)


class TestDeleteVariation:
    def test_removes_variation_and_placements(self, var_state):
        s = var_state
        pat = s.patterns[0]
        var = create_variation(s, pat.id)
        var_pl = Placement(id=s.new_id(), track_id=s.tracks[0].id,
                           pattern_id=var.id, time=4.0, repeats=1,
                           is_variation=True)
        s.placements.append(var_pl)
        s.sel_variation = var.id
        delete_variation(s, var.id)
        assert len(s.variations) == 0
        assert s.sel_variation is None
        assert not any(p.is_variation for p in s.placements)


class TestDeletePatternWithVariations:
    def test_delete_all_mode(self, var_state):
        s = var_state
        pat = s.patterns[0]
        var = create_variation(s, pat.id)
        var_pl = Placement(id=s.new_id(), track_id=s.tracks[0].id,
                           pattern_id=var.id, time=4.0, repeats=1,
                           is_variation=True)
        s.placements.append(var_pl)
        delete_pattern_with_variations(s, pat.id, 'delete_all')
        assert len(s.patterns) == 0
        assert len(s.variations) == 0
        assert len(s.placements) == 0

    def test_make_unique_mode(self, var_state):
        s = var_state
        pat = s.patterns[0]
        var = create_variation(s, pat.id)
        variation_modify_note(var, pat.notes[0].note_id, d_pitch=3)
        var_pl = Placement(id=s.new_id(), track_id=s.tracks[0].id,
                           pattern_id=var.id, time=4.0, repeats=1,
                           is_variation=True)
        s.placements.append(var_pl)
        delete_pattern_with_variations(s, pat.id, 'make_unique')
        assert len(s.patterns) == 1
        assert len(s.variations) == 0


class TestReferenceBinding:
    def test_auto_binds_to_overlapping_note(self, var_state):
        s = var_state
        pat = s.patterns[0]
        var = create_variation(s, pat.id)
        added = variation_add_note(s, var, pitch=64, start=0.0, duration=1.0, velocity=100)
        assert added.ref_note_id == pat.notes[0].note_id
        assert added.ref_bind == 'full'

    def test_auto_binds_pitch_only(self, var_state):
        s = var_state
        pat = s.patterns[0]
        var = create_variation(s, pat.id)
        added = variation_add_note(s, var, pitch=65, start=0.5, duration=0.5, velocity=100)
        assert added.ref_note_id == pat.notes[0].note_id
        assert added.ref_bind == 'pitch'

    def test_full_bind_follows_parent_edits(self, var_state):
        """Moving/resizing a parent note should move/resize the bound added note."""
        s = var_state
        pat = s.patterns[0]
        var = create_variation(s, pat.id)
        added = variation_add_note(s, var, pitch=67, start=0.0, duration=1.0, velocity=100)
        assert added.ref_bind == 'full'

        pat.notes[0].start = 2.0
        pat.notes[0].duration = 2.0
        notes = resolve_variation(s, var.id)
        added_resolved = next(n for n in notes if n.note_id == added.note_id)
        assert abs(added_resolved.start - 2.0) < 1e-6
        assert abs(added_resolved.duration - 2.0) < 1e-6
        assert added_resolved.pitch == 67

    def test_full_bind_follows_parent_pitch_edit(self, var_state):
        """Transposing parent note should transpose the bound added note by same amount."""
        s = var_state
        pat = s.patterns[0]
        var = create_variation(s, pat.id)
        added = variation_add_note(s, var, pitch=67, start=0.0, duration=1.0, velocity=100)

        pat.notes[0].pitch = 62
        notes = resolve_variation(s, var.id)
        added_resolved = next(n for n in notes if n.note_id == added.note_id)
        assert added_resolved.pitch == 69  # 62 + 7

    def test_pitch_bind_follows_pitch_only(self, var_state):
        """Pitch-only binding should follow pitch but not start/duration."""
        s = var_state
        pat = s.patterns[0]
        var = create_variation(s, pat.id)
        added = variation_add_note(s, var, pitch=65, start=0.5, duration=0.5, velocity=100)
        assert added.ref_bind == 'pitch'

        pat.notes[0].start = 2.0
        pat.notes[0].duration = 3.0
        pat.notes[0].pitch = 62
        notes = resolve_variation(s, var.id)
        added_resolved = next(n for n in notes if n.note_id == added.note_id)
        assert added_resolved.pitch == 67  # 62 + 5
        assert abs(added_resolved.start - 0.5) < 1e-6   # unchanged
        assert abs(added_resolved.duration - 0.5) < 1e-6  # unchanged

    def test_add_note_after_modifying_ref_resolves_at_click_position(self, var_state):
        """Adding a note after moving its reference should place it at the clicked position.

        Regression test: offsets were computed against parent positions but applied
        against resolved positions, causing displacement equal to the ref delta.
        """
        s = var_state
        pat = s.patterns[0]
        var = create_variation(s, pat.id)

        # Move parent note n1 (pitch=60, start=0) via variation delta
        variation_modify_note(var, pat.notes[0].note_id, d_pitch=2, d_start=0.5)

        # Add a new note at pitch 65, start 0.75 (where the user clicks)
        added = variation_add_note(s, var, pitch=65, start=0.75, duration=0.5, velocity=100)

        # Resolve and verify the added note appears at the clicked position
        notes = resolve_variation(s, var.id)
        added_resolved = next(n for n in notes if n.note_id == added.note_id)
        assert added_resolved.pitch == 65, f"expected pitch 65, got {added_resolved.pitch}"
        assert abs(added_resolved.start - 0.75) < 1e-6, \
            f"expected start 0.75, got {added_resolved.start}"

    def test_binding_offsets_serialization_roundtrip(self, var_state):
        """Binding offsets should survive serialization and still work."""
        s = var_state
        pat = s.patterns[0]
        var = create_variation(s, pat.id)
        added = variation_add_note(s, var, pitch=67, start=0.0, duration=1.0, velocity=100)

        json_str = s.to_json()
        s2 = AppState()
        s2.load_json(json_str)

        s2.patterns[0].notes[0].start = 3.0
        notes = resolve_variation(s2, s2.variations[0].id)
        added_resolved = next(n for n in notes if n.note_id == s2.variations[0].additions[0].note_id)
        assert abs(added_resolved.start - 3.0) < 1e-6


class TestSerialization:
    def test_note_id_serialization(self):
        n = Note(pitch=60, start=0.0, duration=1.0, note_id=42)
        d = n.to_dict()
        assert d['noteId'] == 42
        n2 = Note.from_dict(d)
        assert n2.note_id == 42

    def test_variation_serialization(self):
        var = Variation(
            id=1, name='var1', parent_id=2, color='#ff0000',
            modifications=[NoteDelta(note_id=10, d_pitch=3)],
            deletions=[20],
            additions=[AddedNote(note_id=30, pitch=60, start=0.0, duration=1.0,
                                  ref_note_id=10, ref_bind='full')],
            splits=[SplitOp(note_id=40, split_offset=0.5, right_note_id=41)],
        )
        d = var.to_dict()
        var2 = Variation.from_dict(d)
        assert var2.parent_id == 2
        assert var2.modifications[0].d_pitch == 3
        assert var2.deletions == [20]
        assert var2.additions[0].ref_bind == 'full'
        assert var2.splits[0].split_offset == 0.5

    def test_appstate_roundtrip(self, var_state):
        s = var_state
        pat = s.patterns[0]
        var = create_variation(s, pat.id)
        variation_modify_note(var, pat.notes[0].note_id, d_pitch=2)
        json_str = s.to_json()

        s2 = AppState()
        s2.load_json(json_str)
        assert len(s2.variations) == 1
        assert s2.variations[0].modifications[0].d_pitch == 2


class TestParentEditPropagation:
    def test_parent_edit_propagates_through_variation(self, var_state):
        """Editing the parent pattern should be visible in the variation's resolved notes."""
        s = var_state
        pat = s.patterns[0]
        var = create_variation(s, pat.id)
        variation_modify_note(var, pat.notes[1].note_id, d_pitch=1)

        pat.notes[0].pitch = 62  # was 60
        notes = resolve_variation(s, var.id)
        n0 = next(n for n in notes if n.note_id == pat.notes[0].note_id)
        assert n0.pitch == 62
        n1 = next(n for n in notes if n.note_id == pat.notes[1].note_id)
        assert n1.pitch == 65  # 64 + 1


class TestSplitModificationInteraction:
    """Tests for the interaction between splits and modifications."""

    def test_modify_then_split_both_halves_retain_pitch(self, var_state):
        """Modifying a note then splitting should preserve pitch on both halves
        and absorb the modification into split deltas."""
        s = var_state
        pat = s.patterns[0]
        var = create_variation(s, pat.id)
        nid = pat.notes[0].note_id

        variation_modify_note(var, nid, d_pitch=2, d_velocity=10)
        assert len(var.modifications) == 1
        sp = variation_record_split(s, var, nid, 0.5)

        # Modification absorbed — no longer in var.modifications
        assert len(var.modifications) == 0
        assert sp.left_delta.d_pitch == 2
        assert sp.left_delta.d_velocity == 10
        # Geometry fields NOT transferred
        assert sp.left_delta.d_start == 0.0
        assert sp.left_delta.d_duration == 0.0
        assert sp.right_delta.d_pitch == 2

        notes = resolve_variation(s, var.id)
        left = next(n for n in notes if n.note_id == nid)
        right = next(n for n in notes if n.note_id == sp.right_note_id)
        assert left.pitch == 62
        assert right.pitch == 62
        assert abs(left.duration - 0.5) < 1e-6
        assert abs(right.duration - 0.5) < 1e-6

    def test_split_then_modify_right_half(self, var_state):
        """Modifying the right half of a split should update right_delta, not var.modifications."""
        s = var_state
        pat = s.patterns[0]
        var = create_variation(s, pat.id)
        nid = pat.notes[0].note_id

        sp = variation_record_split(s, var, nid, 0.5)
        right_id = sp.right_note_id
        variation_modify_note(var, right_id, d_pitch=5)

        assert len(var.modifications) == 0
        assert sp.right_delta.d_pitch == 5

        notes = resolve_variation(s, var.id)
        right = next(n for n in notes if n.note_id == right_id)
        assert right.pitch == 65

    def test_split_geometry_not_double_counted(self, var_state):
        """Split geometry should not be double-counted when re-resolving."""
        s = var_state
        pat = s.patterns[0]
        var = create_variation(s, pat.id)
        nid = pat.notes[0].note_id

        sp = variation_record_split(s, var, nid, 0.5)

        notes1 = resolve_variation(s, var.id)
        notes2 = resolve_variation(s, var.id)

        left1 = next(n for n in notes1 if n.note_id == nid)
        left2 = next(n for n in notes2 if n.note_id == nid)
        right1 = next(n for n in notes1 if n.note_id == sp.right_note_id)
        right2 = next(n for n in notes2 if n.note_id == sp.right_note_id)

        assert abs(left1.duration - left2.duration) < 1e-6
        assert abs(right1.start - right2.start) < 1e-6
        assert abs(right1.duration - right2.duration) < 1e-6

    def test_delete_split_right_half_clears_right_delta(self, var_state):
        s = var_state
        pat = s.patterns[0]
        var = create_variation(s, pat.id)
        nid = pat.notes[0].note_id

        sp = variation_record_split(s, var, nid, 0.5)
        right_id = sp.right_note_id
        variation_modify_note(var, right_id, d_pitch=3)
        assert sp.right_delta is not None

        variation_delete_note(var, right_id)
        assert sp.right_delta is None
        assert right_id in var.deletions

    def test_find_split_for_note_helper(self, var_state):
        s = var_state
        pat = s.patterns[0]
        var = create_variation(s, pat.id)
        nid = pat.notes[0].note_id

        assert _find_split_for_note(var, nid) == (None, None)

        sp = variation_record_split(s, var, nid, 0.5)

        assert _find_split_for_note(var, nid) == (sp, 'left')
        assert _find_split_for_note(var, sp.right_note_id) == (sp, 'right')
        assert _find_split_for_note(var, pat.notes[1].note_id) == (None, None)

    def test_serialization_roundtrip_with_split_deltas(self, var_state):
        s = var_state
        pat = s.patterns[0]
        var = create_variation(s, pat.id)
        nid = pat.notes[0].note_id

        variation_modify_note(var, nid, d_pitch=4, d_velocity=-10)
        sp = variation_record_split(s, var, nid, 0.5)
        variation_modify_note(var, sp.right_note_id, d_pitch=7)

        json_str = s.to_json()
        s2 = AppState()
        s2.load_json(json_str)

        var2 = s2.variations[0]
        sp2 = var2.splits[0]
        assert sp2.left_delta.d_pitch == 4
        assert sp2.left_delta.d_velocity == -10
        assert sp2.right_delta.d_pitch == 7
        assert sp2.right_delta.d_velocity == -10

        notes1 = resolve_variation(s, var.id)
        notes2 = resolve_variation(s2, var2.id)
        assert len(notes1) == len(notes2)
        for n1, n2 in zip(notes1, notes2):
            assert n1.pitch == n2.pitch
            assert abs(n1.start - n2.start) < 1e-6
            assert abs(n1.duration - n2.duration) < 1e-6


class TestChainedSplits:
    """Tests for splitting a note that was already produced by a split."""

    def test_double_split_produces_three_notes(self, var_state):
        """Splitting A→A+B then B→B+C should produce 3 notes from 1."""
        s = var_state
        pat = s.patterns[0]
        var = create_variation(s, pat.id)
        nid = pat.notes[0].note_id  # start=0, dur=1.0

        sp1 = variation_record_split(s, var, nid, 0.5)
        sp2 = variation_record_split(s, var, sp1.right_note_id, 0.25)

        notes = resolve_variation(s, var.id)
        assert len(notes) == 5  # 3 original - 1 split into 3 = 5

        left = next(n for n in notes if n.note_id == nid)
        mid = next(n for n in notes if n.note_id == sp1.right_note_id)
        right = next(n for n in notes if n.note_id == sp2.right_note_id)

        assert abs(left.start - 0.0) < 1e-6
        assert abs(left.duration - 0.5) < 1e-6
        assert abs(mid.start - 0.5) < 1e-6
        assert abs(mid.duration - 0.25) < 1e-6
        assert abs(right.start - 0.75) < 1e-6
        assert abs(right.duration - 0.25) < 1e-6

    def test_find_split_prefers_left_over_right(self, var_state):
        """When a note is both right of split1 and left of split2, prefer split2."""
        s = var_state
        pat = s.patterns[0]
        var = create_variation(s, pat.id)
        nid = pat.notes[0].note_id

        sp1 = variation_record_split(s, var, nid, 0.5)
        b_id = sp1.right_note_id
        sp2 = variation_record_split(s, var, b_id, 0.25)

        found, side = _find_split_for_note(var, b_id)
        assert found is sp2
        assert side == 'left'

    def test_modify_chained_note_independent(self, var_state):
        """Moving a note in a chained split should not affect siblings."""
        s = var_state
        pat = s.patterns[0]
        var = create_variation(s, pat.id)
        nid = pat.notes[0].note_id  # start=0, dur=1.0

        sp1 = variation_record_split(s, var, nid, 0.5)
        b_id = sp1.right_note_id
        sp2 = variation_record_split(s, var, b_id, 0.25)
        c_id = sp2.right_note_id

        variation_modify_note(var, b_id, d_start=0.1)

        notes = resolve_variation(s, var.id)
        mid = next(n for n in notes if n.note_id == b_id)
        right = next(n for n in notes if n.note_id == c_id)

        assert abs(mid.start - 0.6) < 1e-6   # 0.5 + 0.1
        assert abs(right.start - 0.75) < 1e-6  # unchanged

    def test_chained_split_transfers_content_from_previous_delta(self, var_state):
        """Splitting a note with an existing delta on a previous split transfers content."""
        s = var_state
        pat = s.patterns[0]
        var = create_variation(s, pat.id)
        nid = pat.notes[0].note_id

        sp1 = variation_record_split(s, var, nid, 0.5)
        b_id = sp1.right_note_id

        variation_modify_note(var, b_id, d_pitch=5)
        assert sp1.right_delta.d_pitch == 5

        sp2 = variation_record_split(s, var, b_id, 0.25)

        assert sp2.left_delta.d_pitch == 5
        assert sp2.right_delta.d_pitch == 5
        if sp1.right_delta:
            assert sp1.right_delta.d_pitch == 0

        notes = resolve_variation(s, var.id)
        mid = next(n for n in notes if n.note_id == b_id)
        right = next(n for n in notes if n.note_id == sp2.right_note_id)
        assert mid.pitch == 65
        assert right.pitch == 65

    def test_chained_split_preserves_geometry_on_previous_delta(self, var_state):
        """When splitting a moved note, the previous delta's geometry should persist."""
        s = var_state
        pat = s.patterns[0]
        var = create_variation(s, pat.id)
        nid = pat.notes[0].note_id  # start=0, dur=1.0

        sp1 = variation_record_split(s, var, nid, 0.5)
        b_id = sp1.right_note_id

        variation_modify_note(var, b_id, d_start=0.2)
        sp2 = variation_record_split(s, var, b_id, 0.25)

        assert sp1.right_delta is not None
        assert abs(sp1.right_delta.d_start - 0.2) < 1e-6

        notes = resolve_variation(s, var.id)
        mid = next(n for n in notes if n.note_id == b_id)
        right = next(n for n in notes if n.note_id == sp2.right_note_id)
        assert abs(mid.start - 0.7) < 1e-6
        assert abs(mid.duration - 0.25) < 1e-6
        assert abs(right.start - 0.95) < 1e-6
        assert abs(right.duration - 0.25) < 1e-6


class TestComputeSplitBaselines:
    """Tests for compute_split_baselines helper."""

    def test_chained_split_baselines(self, var_state):
        s = var_state
        pat = s.patterns[0]
        var = create_variation(s, pat.id)
        nid = pat.notes[0].note_id  # start=0, dur=1.0

        sp1 = variation_record_split(s, var, nid, 0.5)
        b_id = sp1.right_note_id

        variation_modify_note(var, b_id, d_start=0.2)
        sp2 = variation_record_split(s, var, b_id, 0.25)

        baselines = compute_split_baselines(var, pat)

        # B (left of sp2) baseline includes the d_start from sp1.right_delta
        bs, bd, bp, bv = baselines[b_id]
        assert abs(bs - 0.7) < 1e-6  # 0.5 + 0.2
        assert abs(bd - 0.25) < 1e-6  # sp2.split_offset

        # C (right of sp2)
        c_id = sp2.right_note_id
        bs, bd, bp, bv = baselines[c_id]
        assert abs(bs - 0.95) < 1e-6  # 0.7 + 0.25
        assert abs(bd - 0.25) < 1e-6  # 0.5 - 0.25


class TestSplitAddedNote:
    """Tests for splitting added notes in variations."""

    def test_split_added_note_creates_two(self, var_state):
        s = var_state
        pat = s.patterns[0]
        var = create_variation(s, pat.id)
        added = variation_add_note(s, var, pitch=72, start=0.0, duration=1.0, velocity=100)

        right = variation_split_added_note(s, var, added.note_id, 0.5)

        assert right is not None
        assert len(var.additions) == 2
        assert abs(added.duration - 0.5) < 1e-6
        assert right.pitch == 72
        assert abs(right.start - 0.5) < 1e-6
        assert abs(right.duration - 0.5) < 1e-6
        assert right.velocity == 100

    def test_split_added_note_preserves_binding(self, var_state):
        s = var_state
        pat = s.patterns[0]
        var = create_variation(s, pat.id)
        added = variation_add_note(s, var, pitch=67, start=0.0, duration=1.0, velocity=100)
        assert added.ref_note_id == pat.notes[0].note_id

        right = variation_split_added_note(s, var, added.note_id, 0.5)

        assert added.ref_note_id == pat.notes[0].note_id
        assert right.ref_note_id == pat.notes[0].note_id

    def test_split_added_note_invalid_offset(self, var_state):
        s = var_state
        pat = s.patterns[0]
        var = create_variation(s, pat.id)
        added = variation_add_note(s, var, pitch=72, start=0.0, duration=1.0)

        assert variation_split_added_note(s, var, added.note_id, 0.0) is None
        assert variation_split_added_note(s, var, added.note_id, 1.0) is None
        assert len(var.additions) == 1
