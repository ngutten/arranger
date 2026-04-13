"""Euclidean Syncopate — redistribute notes using Bjorklund's algorithm.

Each phrase (of length ``phrase_beats`` beats) has its notes moved to the
positions of the maximally-even E(N, M) distribution, where N is the number
of notes and M = ``slots_per_phrase`` is the number of slots in the phrase.
"""

from __future__ import annotations

import math
from typing import Dict, List, Tuple

from ..api import (
    MoveNote, ParamSpec, PluginManifest, PluginResult, Progress,
    ResizeNote, Scope, SongPlugin,
)
from ._euclidean import euclidean_pattern


class EuclideanSyncopatePlugin(SongPlugin):
    manifest = PluginManifest(
        id="builtin.euclidean_syncopate",
        name="Euclidean Syncopate",
        version="1.0.0",
        description=(
            "Redistribute notes across each phrase using Bjorklund's "
            "maximally-even algorithm (N notes into M slots)."
        ),
        capabilities=("transform",),
        schemas=(),
        params=(
            ParamSpec(
                key="phrase_beats", type="float",
                label="Phrase length (beats)", default=4.0,
                min=0.5, max=16.0,
                help=(
                    "Each phrase is this many beats long. Notes within "
                    "the phrase are redistributed."
                ),
            ),
            ParamSpec(
                key="slots_per_phrase", type="int",
                label="Slots per phrase (M)", default=16,
                min=2, max=64,
                help=(
                    "Euclidean redistribution places N notes at the "
                    "maximally-even positions across M slots. M = this "
                    "value; N is the count of existing notes in each phrase."
                ),
            ),
            ParamSpec(
                key="preserve_duration", type="bool",
                label="Preserve original durations", default=True,
            ),
            ParamSpec(
                key="scope", type="enum",
                label="Scope", default="whole",
                choices=("whole", "selection"),
            ),
        ),
        scopes=("whole", "selection"),
        selection_kinds=("notes", "placements"),
        deps=("midi", "structure"),
        live_supported=False,
        persistence_default="transient",
    )

    def run(self, view, params: Dict, progress: Progress) -> PluginResult:
        phrase_beats = float(params.get("phrase_beats", 4.0))
        slots_per_phrase = int(params.get("slots_per_phrase", 16))
        preserve_duration = bool(params.get("preserve_duration", True))
        scope_kind = params.get("scope", "whole")

        if phrase_beats <= 0:
            raise ValueError("phrase_beats must be > 0")
        if slots_per_phrase < 2:
            raise ValueError("slots_per_phrase must be >= 2")

        # Internal aliases for the algorithm. slot_beats is the duration
        # (in beats) of one euclidean slot within the phrase.
        group_length = phrase_beats
        slot_beats = phrase_beats / slots_per_phrase

        scope = self._resolve_scope(view, scope_kind)

        # Look up patterns and placements for beat->pattern-local conversion.
        patterns_by_id = {p.id: p for p in view.patterns()}
        placements_by_id = {pl.id: pl for pl in view.placements()}

        progress.phase("scan")
        # Collect notes grouped by phrase index. Notes in a pattern can be
        # referenced by multiple placements/repeats; dedupe on
        # (pattern_id, note_id) — moving a pattern note moves it for every
        # placement. A future "unique copy first" mode would avoid this
        # cross-placement coupling, but it's out of scope for this PR.
        seen: set = set()
        # (pattern_id, note_id) -> (phrase_idx, absolute_start, note_record)
        collected: Dict[Tuple[int, int], Tuple[int, float, object]] = {}
        for n in view.notes_in(scope):
            if progress.cancelled:
                break
            if n.source_kind != "pattern":
                continue
            if n.note_id == 0:
                continue
            key = (n.source_id, n.note_id)
            if key in seen:
                continue
            seen.add(key)
            phrase_idx = int(math.floor(n.start_beat / group_length))
            collected[key] = (phrase_idx, n.start_beat, n)

        # Bucket by phrase.
        phrases: Dict[int, List[Tuple[Tuple[int, int], float, object]]] = {}
        for key, (phrase_idx, start, n) in collected.items():
            phrases.setdefault(phrase_idx, []).append((key, start, n))

        ops: List = []
        group_count = 0
        progress.phase("redistribute")
        for phrase_idx, entries in phrases.items():
            if not entries:
                continue
            M = slots_per_phrase
            N = len(entries)
            if N == 0 or N >= M:
                continue
            slots = euclidean_pattern(N, M)
            if not slots:
                continue
            entries.sort(key=lambda item: item[1])  # by original start
            group_start_beat = phrase_idx * group_length

            # Compute planned new-start per entry, used later for duration
            # re-fitting when preserve_duration is False.
            planned: List[Tuple[Tuple[int, int], object, float]] = []
            for (key, _start, n), slot in zip(entries, slots):
                new_local = slot * slot_beats
                pl = placements_by_id.get(n.placement_id)
                pat = patterns_by_id.get(n.source_id)
                if pl is None or pat is None:
                    continue
                pat_length = pat.length
                new_absolute = group_start_beat + new_local
                pattern_local = new_absolute - (pl.time + n.repeat_index * pat_length)
                if pattern_local < 0 or pattern_local >= pat_length:
                    continue  # placement straddles the phrase boundary awkwardly
                original_pattern_local = n.start_beat - (
                    pl.time + n.repeat_index * pat_length
                )
                if abs(pattern_local - original_pattern_local) < 1e-6:
                    continue
                planned.append((key, n, pattern_local))

            if not planned:
                continue

            group_count += 1

            # Emit MoveNote ops (pitch preserved).
            for key, n, pattern_local in planned:
                ops.append(MoveNote(
                    pattern_id=key[0], note_id=key[1],
                    new_start=pattern_local, new_pitch=n.pitch,
                ))

            if not preserve_duration:
                # Derive new durations as the gap to the next placed note
                # in this group (or to the end of the group for the last).
                # Use the planned positions sorted by new local start.
                ordered = sorted(planned, key=lambda item: item[2])
                group_end_local = group_length
                for i, (key, n, local_start) in enumerate(ordered):
                    if i + 1 < len(ordered):
                        next_local = ordered[i + 1][2]
                    else:
                        # Convert group end to the same pattern-local frame.
                        pl = placements_by_id[n.placement_id]
                        pat = patterns_by_id[n.source_id]
                        next_local = min(
                            group_end_local + (phrase_idx * group_length)
                            - (pl.time + n.repeat_index * pat.length),
                            pat.length,
                        )
                    new_dur = max(1e-3, next_local - local_start)
                    if abs(new_dur - n.duration_beats) < 1e-6:
                        continue
                    ops.append(ResizeNote(
                        pattern_id=key[0], note_id=key[1],
                        new_duration=new_dur,
                    ))

        progress.update(1.0, "done")
        return PluginResult(
            operations=tuple(ops),
            message=f"{len(ops)} operations across {group_count} groups",
        )

    @staticmethod
    def _resolve_scope(view, scope_kind: str) -> Scope:
        if scope_kind != "selection":
            return Scope(kind="whole")
        sel = view.selection()
        if sel.primary == "placements" and sel.placements:
            return Scope(
                kind="placements",
                placement_ids=tuple(sorted(sel.placements)),
            )
        if sel.notes:
            return Scope(
                kind="notes",
                note_ids=tuple(sorted(sel.notes)),
            )
        if sel.placements:
            return Scope(
                kind="placements",
                placement_ids=tuple(sorted(sel.placements)),
            )
        return Scope(kind="whole")


PLUGIN = EuclideanSyncopatePlugin
