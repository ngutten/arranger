"""Tests for the Chordify plugin."""

from __future__ import annotations

from standalone.state import (
    AppState, Pattern, Note, Track, Placement, Variation,
)
from standalone.ops.variations import variation_add_note
from standalone.song_plugins.song_view import SongView
from standalone.song_plugins.api import (
    SelectionSnapshot, AddNote, DeleteNote,
    VariationAddNote, VariationDeleteNote,
)
from standalone.song_plugins.builtin.chordify import (
    ChordifyPlugin, CHORD_ROOT_TAG, CHORD_VOICING_TAG,
)
from standalone.song_plugins.apply_ops import apply_ops as _apply_ops


def apply(app, ops):
    return _apply_ops(ops, app, 'test')


class _DummyProgress:
    def __init__(self):
        self.cancelled = False
    def phase(self, name): pass
    def update(self, fraction, message=None): pass


def _empty_selection():
    return SelectionSnapshot(
        notes=frozenset(), placements=frozenset(), primary='none',
        current_pattern_id=None, current_variation_id=None,
        current_beat_pattern_id=None, current_auto_pattern_id=None,
    )


def _mk_state(notes, *, key='C', scale='major'):
    s = AppState()
    s.bpm = 120.0
    t = Track(id=s.new_id(), name='T', channel=0)
    s.tracks.append(t)
    for n in notes:
        if n.note_id == 0:
            n.note_id = s.new_id()
    p = Pattern(id=s.new_id(), name='P', length=4.0, notes=list(notes),
                color='#fff', key=key, scale=scale)
    s.patterns.append(p)
    pl = Placement(id=s.new_id(), track_id=t.id, pattern_id=p.id,
                   time=0.0, repeats=1)
    s.placements.append(pl)
    return s, p


def _run(state, params=None):
    view = SongView(state, _empty_selection())
    plugin = ChordifyPlugin()
    return plugin.run(view, params or {}, _DummyProgress())


# ---------------------------------------------------------------------------
# Basic chordification
# ---------------------------------------------------------------------------

def test_no_roots_no_ops():
    state, _pat = _mk_state([
        Note(pitch=60, start=0.0, duration=1.0, velocity=100),
    ])
    result = _run(state)
    assert result.operations == ()


def test_single_root_generates_triad():
    n = Note(pitch=60, start=0.0, duration=1.0, velocity=100)
    n.tags = {CHORD_ROOT_TAG: {'quality': 'maj'}}
    state, pat = _mk_state([n])
    result = _run(state)
    # 2 voicings (E, G); root is kept in place (not duplicated).
    adds = [op for op in result.operations if isinstance(op, AddNote)]
    assert len(adds) == 2
    assert {op.pitch for op in adds} == {64, 67}
    # Each voicing tagged with the root's note_id.
    for op in adds:
        assert op.tags[CHORD_VOICING_TAG]['root_id'] == n.note_id


def test_diatonic_default_uses_scale_context():
    # ii in C major should be a minor triad
    n = Note(pitch=62, start=0.0, duration=1.0, velocity=100)
    n.tags = {CHORD_ROOT_TAG: {'quality': 'diatonic'}}
    state, pat = _mk_state([n])
    result = _run(state)
    adds = sorted([op.pitch for op in result.operations if isinstance(op, AddNote)])
    assert adds == [65, 69]  # D minor: D F A


def test_include_root_false_omits_root_from_voicing():
    n = Note(pitch=60, start=0.0, duration=1.0, velocity=100)
    n.tags = {CHORD_ROOT_TAG: {'quality': 'maj'}}
    state, pat = _mk_state([n])
    result = _run(state, {'include_root': False})
    adds = [op.pitch for op in result.operations if isinstance(op, AddNote)]
    # Even with include_root=False the *user's root note* is never
    # duplicated; the omission controls only whether we would add a
    # root-pitch voicing on top. Here still no 60 (user's note).
    assert 60 not in adds
    assert sorted(adds) == [64, 67]


def test_voicing_carries_root_duration_and_velocity():
    n = Note(pitch=60, start=1.5, duration=2.0, velocity=80)
    n.tags = {CHORD_ROOT_TAG: {'quality': 'maj'}}
    state, pat = _mk_state([n])
    result = _run(state)
    adds = [op for op in result.operations if isinstance(op, AddNote)]
    assert all(op.duration == 2.0 for op in adds)
    assert all(op.velocity == 80 for op in adds)
    assert all(op.start == 1.5 for op in adds)


# ---------------------------------------------------------------------------
# Idempotency / re-run
# ---------------------------------------------------------------------------

def test_rerun_idempotent_after_apply(simple_app):
    """Running twice produces no net change to the pattern."""
    app = simple_app
    # First pass: root only.
    state = app.state
    pat = state.patterns[0]
    pat.notes[0].tags = {CHORD_ROOT_TAG: {'quality': 'maj'}}

    ops1 = _run(state).operations
    apply(app, ops1)
    pitches_after_first = sorted(n.pitch for n in pat.notes)
    assert pitches_after_first == [60, 64, 67]

    # Second pass: should delete the two voicings and re-add them.
    ops2 = _run(state).operations
    apply(app, ops2)
    pitches_after_second = sorted(n.pitch for n in pat.notes)
    assert pitches_after_second == [60, 64, 67]


def test_changing_root_quality_regenerates_voicings(simple_app):
    app = simple_app
    state = app.state
    pat = state.patterns[0]
    root = pat.notes[0]
    root.tags = {CHORD_ROOT_TAG: {'quality': 'maj'}}
    apply(app, _run(state).operations)
    assert sorted(n.pitch for n in pat.notes) == [60, 64, 67]

    # Change to minor — next run should swap E for Eb.
    root.tags = {CHORD_ROOT_TAG: {'quality': 'min'}}
    apply(app, _run(state).operations)
    assert sorted(n.pitch for n in pat.notes) == [60, 63, 67]


def test_moving_root_regenerates_at_new_location(simple_app):
    app = simple_app
    state = app.state
    pat = state.patterns[0]
    root = pat.notes[0]
    root.tags = {CHORD_ROOT_TAG: {'quality': 'maj'}}
    apply(app, _run(state).operations)
    # Move root up a 4th: C -> F
    root.pitch = 65
    apply(app, _run(state).operations)
    assert sorted(n.pitch for n in pat.notes) == [65, 69, 72]


# ---------------------------------------------------------------------------
# Edit-detection: user-edited voicings are preserved on re-run
# ---------------------------------------------------------------------------

def test_untouched_voicing_carries_generated_geometry(simple_app):
    """Newly generated voicings record their gen_* geometry."""
    app = simple_app
    state = app.state
    pat = state.patterns[0]
    pat.notes[0].tags = {CHORD_ROOT_TAG: {'quality': 'maj'}}
    apply(app, _run(state).operations)
    voicings = [n for n in pat.notes if CHORD_VOICING_TAG in n.tags]
    assert len(voicings) == 2
    for v in voicings:
        tag = v.tags[CHORD_VOICING_TAG]
        assert tag['gen_pitch'] == v.pitch
        assert tag['gen_start'] == v.start
        assert tag['gen_dur'] == v.duration
        assert tag['gen_vel'] == v.velocity


def test_edited_voicing_preserved_on_rerun(simple_app):
    """If a user moves a voicing, re-run leaves it alone."""
    app = simple_app
    state = app.state
    pat = state.patterns[0]
    pat.notes[0].tags = {CHORD_ROOT_TAG: {'quality': 'maj'}}
    apply(app, _run(state).operations)

    # Find the E voicing (pitch 64) and move it up an octave.
    e_voicing = next(n for n in pat.notes if n.pitch == 64)
    e_voicing.pitch = 76  # user pulls it up an octave

    apply(app, _run(state).operations)
    # E at 64 should NOT be regenerated because its corresponding voicing
    # still exists at 76 (marked as edited). G (67) stays; the edited
    # note at 76 survives.
    pitches = sorted(n.pitch for n in pat.notes)
    assert 64 not in pitches
    assert 67 in pitches      # G untouched
    assert 76 in pitches      # user-edited voicing preserved
    assert 60 in pitches      # root


def test_humanized_voicings_frozen_as_set(simple_app):
    """Simulate humanize: every voicing geometry changes -> all frozen."""
    app = simple_app
    state = app.state
    pat = state.patterns[0]
    pat.notes[0].tags = {CHORD_ROOT_TAG: {'quality': 'maj'}}
    apply(app, _run(state).operations)

    for n in pat.notes:
        if CHORD_VOICING_TAG in n.tags:
            n.velocity = max(1, n.velocity - 5)  # small jitter

    before = [(n.pitch, n.velocity) for n in pat.notes]
    apply(app, _run(state).operations)
    after = [(n.pitch, n.velocity) for n in pat.notes]
    assert sorted(before) == sorted(after)


def test_deleted_voicing_regenerates(simple_app):
    """Deleting a voicing releases it back to regenerable state."""
    app = simple_app
    state = app.state
    pat = state.patterns[0]
    pat.notes[0].tags = {CHORD_ROOT_TAG: {'quality': 'maj'}}
    apply(app, _run(state).operations)

    # Delete G voicing.
    pat.notes = [n for n in pat.notes if n.pitch != 67]
    assert sorted(n.pitch for n in pat.notes) == [60, 64]

    apply(app, _run(state).operations)
    # G should come back.
    assert sorted(n.pitch for n in pat.notes) == [60, 64, 67]


# ---------------------------------------------------------------------------
# Orphan / stale voicing cleanup
# ---------------------------------------------------------------------------

def test_orphan_voicing_cleaned_up(simple_app):
    app = simple_app
    state = app.state
    pat = state.patterns[0]
    root = pat.notes[0]
    root.tags = {CHORD_ROOT_TAG: {'quality': 'maj'}}
    apply(app, _run(state).operations)
    assert len(pat.notes) == 3

    # Simulate deleting the root note: voicings now reference a gone root.
    pat.notes = [n for n in pat.notes if n.note_id != root.note_id]
    assert len(pat.notes) == 2  # two orphan voicings

    # Re-run — plugin should detect orphans and delete them.
    ops = _run(state).operations
    dels = [op for op in ops if isinstance(op, DeleteNote)]
    assert len(dels) == 2
    apply(app, ops)
    assert len(pat.notes) == 0


def test_voicings_for_other_roots_left_alone():
    """Deletes only voicings whose root is in scope."""
    root = Note(pitch=60, start=0.0, duration=1.0, velocity=100)
    root.tags = {CHORD_ROOT_TAG: {'quality': 'maj'}}
    melody = Note(pitch=62, start=1.0, duration=1.0, velocity=100)
    state, pat = _mk_state([root, melody])

    # A stray voicing-tagged note that points to melody's id — melody
    # isn't a chord root, but it still exists in the pattern, so the
    # voicing should be considered "alive but not in scope" and left
    # untouched by this run.
    stray = Note(pitch=99, start=1.0, duration=1.0, velocity=90,
                 note_id=state.new_id())
    stray.tags = {CHORD_VOICING_TAG: {'root_id': melody.note_id}}
    pat.notes.append(stray)

    ops = _run(state).operations
    dels = [op for op in ops if isinstance(op, DeleteNote)]
    assert all(op.note_id != stray.note_id for op in dels)


# ---------------------------------------------------------------------------
# Variation support
# ---------------------------------------------------------------------------

def test_root_in_variation_emits_variation_ops():
    n = Note(pitch=60, start=0.0, duration=1.0, velocity=100)
    state, pat = _mk_state([n])
    var = Variation(id=state.new_id(), name='V', parent_id=pat.id,
                    color='#f00')
    state.variations.append(var)
    added = variation_add_note(state, var, pitch=62, start=1.0,
                               duration=1.0, velocity=100)
    added.tags = {CHORD_ROOT_TAG: {'quality': 'maj'}}
    # Add placement for the variation (resolved notes only yield via
    # placements).
    pl_var = Placement(id=state.new_id(),
                       track_id=state.tracks[0].id,
                       pattern_id=var.id, time=0.0, repeats=1,
                       is_variation=True)
    state.placements.append(pl_var)

    result = _run(state)
    adds = [op for op in result.operations if isinstance(op, VariationAddNote)]
    assert len(adds) == 2  # D maj: F# and A
    assert all(op.variation_id == var.id for op in adds)
    assert all(op.tags[CHORD_VOICING_TAG]['root_id'] == added.note_id
               for op in adds)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

import pytest


class _MinimalApp:
    def __init__(self, state):
        self.state = state


@pytest.fixture
def simple_app():
    """One-pattern, one-placement state wrapped in a minimal app shim."""
    n = Note(pitch=60, start=0.0, duration=1.0, velocity=100)
    state, _pat = _mk_state([n])
    return _MinimalApp(state)
