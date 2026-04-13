"""Tests for the source-string → MetaDep mapping."""

from standalone.song_plugins.ui.dep_watcher import SOURCE_TO_DEPS, classify


def test_midi_sources():
    assert "midi" in classify("note_edit")
    assert "midi" in classify("note_add")
    assert "midi" in classify("piano_roll_edit")


def test_structure_sources():
    assert "structure" in classify("placement_added")
    assert "structure" in classify("placement_edit")
    assert "structure" in classify("del_pl")


def test_beat_sources():
    assert "beat" in classify("beat_grid_edit")
    assert "beat" in classify("beat_pattern_dialog")
    assert "beat" in classify("del_beat_pl")


def test_automation_sources():
    assert "automation" in classify("automation_edit")
    assert "automation" in classify("automation_pattern_dialog")
    assert "automation" in classify("automation_placement_edit")


def test_tempo_source():
    assert "tempo" in classify("ts")


def test_track_sources():
    assert "tracks" in classify("track_deleted")
    assert "tracks" in classify("beat_track_deleted")


def test_unknown_source_empty():
    assert classify("this_does_not_exist") == frozenset()
    assert classify(None) == frozenset()
    assert classify("") == frozenset()


def test_all_returned_sets_are_frozenset():
    for source, deps in SOURCE_TO_DEPS.items():
        assert isinstance(deps, frozenset), source
        # Only contains known MetaDeps
        for d in deps:
            assert d in {"midi", "structure", "tempo", "tracks",
                         "automation", "beat"}, (source, d)


def test_app_undo_trigger_coverage():
    """Every source listed in App._undo_triggers should have a mapping
    (or be a transport-only source we can safely ignore)."""
    # A minimal copy of the undo-trigger list from app.py. If any are
    # missing from SOURCE_TO_DEPS, add them (or explicitly add them to
    # the ignore set below).
    undo_triggers = {
        'pattern_dialog', 'beat_pattern_dialog',
        'placement_edit', 'beat_placement_edit',
        'del_pl', 'del_beat_pl',
        'placement_added', 'beat_placement_added',
        'note_edit', 'note_add',
        'piano_roll_edit', 'beat_grid_edit',
        'track_deleted', 'beat_track_deleted',
        'ts', 'cut_placements', 'paste_placements', 'delete_placements',
        'automation_edit', 'automation_pattern_dialog',
        'automation_track_dialog', 'del_auto_pat', 'dup_auto_pat',
        'automation_placement_edit', 'automation_placement_added',
    }
    ignore = set()
    missing = undo_triggers - set(SOURCE_TO_DEPS.keys()) - ignore
    assert not missing, (
        f"{len(missing)} undo-triggers lack MetaDep classification: {missing}"
    )
