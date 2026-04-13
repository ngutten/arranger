"""Map state-change source strings to MetaDep sets.

Live-mode plugins declare the domains they depend on (``manifest.deps``):
``midi``, ``structure``, ``tempo``, ``tracks``, ``automation``, ``beat``.
When ``App.notify(source)`` fires, we translate ``source`` into the set of
MetaDeps it affects; any active block whose deps intersect the set is
marked stale and (if live) scheduled for a rerun.
"""

from __future__ import annotations

from typing import FrozenSet

# All known state-change source strings we care about.
# Sources not listed here fall through to frozenset() — no known deps.
SOURCE_TO_DEPS: dict[str, FrozenSet[str]] = {
    # Pattern editing / dialogs
    "pattern_dialog":        frozenset({"midi", "structure"}),
    "edit_pattern":          frozenset({"midi", "structure"}),
    "beat_pattern_dialog":   frozenset({"beat", "structure"}),
    "edit_beat_pattern":     frozenset({"beat", "structure"}),

    # Arrangement structure — placements live/arrange changes
    "placement_edit":        frozenset({"structure"}),
    "beat_placement_edit":   frozenset({"structure"}),
    "del_pl":                frozenset({"structure", "midi"}),
    "del_beat_pl":           frozenset({"structure", "beat"}),
    "placement_added":       frozenset({"structure"}),
    "beat_placement_added":  frozenset({"structure"}),
    "cut_placements":        frozenset({"structure", "midi", "beat"}),
    "paste_placements":      frozenset({"structure", "midi", "beat"}),
    "delete_placements":     frozenset({"structure", "midi", "beat"}),

    # Piano roll / beat grid note edits
    "note_edit":             frozenset({"midi"}),
    "note_add":              frozenset({"midi"}),
    "piano_roll_edit":       frozenset({"midi"}),
    "beat_grid_edit":        frozenset({"beat"}),

    # Tracks
    "track_deleted":         frozenset({"tracks", "structure"}),
    "beat_track_deleted":    frozenset({"tracks", "structure", "beat"}),

    # Transport / global
    "ts":                    frozenset({"tempo", "structure"}),

    # Automation
    "automation_edit":               frozenset({"automation"}),
    "automation_pattern_dialog":     frozenset({"automation"}),
    "automation_track_dialog":       frozenset({"tracks", "automation"}),
    "del_auto_pat":                  frozenset({"automation"}),
    "dup_auto_pat":                  frozenset({"automation"}),
    "automation_placement_edit":     frozenset({"automation", "structure"}),
    "automation_placement_added":    frozenset({"automation", "structure"}),

    # Plugin-applied operations: broad invalidation since ops may touch any domain.
    "plugin_apply": frozenset({"midi", "structure", "tempo", "tracks",
                                "automation", "beat"}),
}


def classify(source) -> FrozenSet[str]:
    """Return the MetaDep set affected by ``source``.

    Unknown or None sources yield ``frozenset()``.
    """
    if source is None:
        return frozenset()
    return SOURCE_TO_DEPS.get(source, frozenset())
