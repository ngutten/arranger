"""Humanize — jitter velocity, start time, and duration of selected notes.

Follows the same scope/undo/selection semantics as
``euclidean_syncopate``: emits operations that the app's executor applies
atomically, so undo is one step regardless of how many notes were touched.

Notes that share an identical start time within the same source pattern
are treated as a chord when ``preserve_chord_sync`` is set — the same
timing jitter is applied to all members so they stay aligned.
"""

from __future__ import annotations

import random
from typing import Dict, List, Tuple

from ..api import (
    MoveNote, ParamSpec, PluginManifest, PluginResult, Progress,
    ResizeNote, Scope, SetNoteVelocity, SongPlugin,
)


class HumanizePlugin(SongPlugin):
    manifest = PluginManifest(
        id="builtin.humanize",
        name="Humanize",
        version="1.0.0",
        description=(
            "Add subtle random variation to velocity, timing, and duration "
            "to make quantized input feel less mechanical."
        ),
        capabilities=("transform",),
        schemas=(),
        params=(
            ParamSpec(
                key="velocity_range", type="int",
                label="Velocity jitter (±)", default=6,
                min=0, max=40,
                help="Each note's velocity is shifted by a random value "
                     "in [-range, +range].",
            ),
            ParamSpec(
                key="timing_range", type="float",
                label="Timing jitter (± beats)", default=0.02,
                min=0.0, max=0.25,
                help="Each note's start is shifted by a random offset in "
                     "[-range, +range] beats. Notes remain non-overlapping "
                     "with pattern boundaries.",
            ),
            ParamSpec(
                key="duration_pct", type="float",
                label="Duration jitter (±%)", default=5.0,
                min=0.0, max=40.0,
                help="Each note's duration is scaled by 1 + r/100 where r "
                     "is random in [-pct, +pct].",
            ),
            ParamSpec(
                key="preserve_chord_sync", type="bool",
                label="Preserve chord sync", default=True,
                help="Notes that start at the same pattern-local time "
                     "receive identical timing jitter so chords stay "
                     "aligned.",
            ),
            ParamSpec(
                key="seed", type="int",
                label="Seed (0 = random)", default=0,
                min=0, max=2**31 - 1,
                help="Non-zero values produce deterministic output — "
                     "re-running with the same seed yields the same ops.",
            ),
            ParamSpec(
                key="scope", type="enum",
                label="Scope", default="whole",
                choices=("whole", "selection"),
            ),
        ),
        scopes=("whole", "selection"),
        selection_kinds=("notes", "placements"),
        deps=("midi",),
        live_supported=False,
        persistence_default="transient",
    )

    def run(self, view, params: Dict, progress: Progress) -> PluginResult:
        vel_range = int(params.get("velocity_range", 6))
        timing_range = float(params.get("timing_range", 0.02))
        dur_pct = float(params.get("duration_pct", 5.0))
        preserve_chord = bool(params.get("preserve_chord_sync", True))
        seed = int(params.get("seed", 0))
        scope_kind = params.get("scope", "whole")

        if vel_range < 0 or timing_range < 0 or dur_pct < 0:
            raise ValueError("ranges must be non-negative")
        if vel_range == 0 and timing_range == 0 and dur_pct == 0:
            return PluginResult(
                operations=(),
                message="All jitter ranges are zero — nothing to do.",
            )

        rng = random.Random(seed) if seed else random.Random()

        scope = self._resolve_scope(view, scope_kind)

        patterns_by_id = {p.id: p for p in view.patterns()}
        placements_by_id = {pl.id: pl for pl in view.placements()}

        progress.phase("scan")
        # Dedupe cross-placement: moving a pattern note moves it for every
        # placement that references it. Key is (source_id, note_id).
        # Value holds the canonical note record plus the *pattern-local*
        # start we'll base the jitter against.
        seen: Dict[Tuple[int, int], Dict] = {}
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
            pl = placements_by_id.get(n.placement_id)
            pat = patterns_by_id.get(n.source_id)
            if pl is None or pat is None:
                continue
            pat_len = pat.length
            local_start = n.start_beat - (pl.time + n.repeat_index * pat_len)
            seen[key] = {
                "note": n,
                "pattern_id": n.source_id,
                "note_id": n.note_id,
                "local_start": local_start,
                "pat_len": pat_len,
            }

        # Bucket by (pattern_id, quantized local_start) for chord detection.
        # Quantize to 1e-4 beats to absorb float noise.
        chord_offsets: Dict[Tuple[int, int], float] = {}
        if preserve_chord and timing_range > 0:
            for entry in seen.values():
                k = (entry["pattern_id"],
                     int(round(entry["local_start"] * 10_000)))
                if k not in chord_offsets:
                    chord_offsets[k] = rng.uniform(-timing_range, timing_range)

        progress.phase("generate")
        ops: List = []
        for entry in seen.values():
            n = entry["note"]
            pat_len = entry["pat_len"]
            local_start = entry["local_start"]

            # Timing.
            if timing_range > 0:
                if preserve_chord:
                    k = (entry["pattern_id"],
                         int(round(local_start * 10_000)))
                    dt = chord_offsets.get(k, 0.0)
                else:
                    dt = rng.uniform(-timing_range, timing_range)
                new_local = local_start + dt
                # Clamp to pattern bounds — keep a hair of room so the
                # note doesn't sit exactly on the boundary.
                eps = 1e-4
                new_local = max(0.0, min(pat_len - eps, new_local))
                if abs(new_local - local_start) > 1e-6:
                    ops.append(MoveNote(
                        pattern_id=entry["pattern_id"],
                        note_id=entry["note_id"],
                        new_start=new_local,
                        new_pitch=n.pitch,
                    ))

            # Duration.
            if dur_pct > 0:
                scale = 1.0 + rng.uniform(-dur_pct, dur_pct) / 100.0
                new_dur = max(1e-3, n.duration_beats * scale)
                if abs(new_dur - n.duration_beats) > 1e-6:
                    ops.append(ResizeNote(
                        pattern_id=entry["pattern_id"],
                        note_id=entry["note_id"],
                        new_duration=new_dur,
                    ))

            # Velocity.
            if vel_range > 0:
                delta = rng.randint(-vel_range, vel_range)
                new_vel = max(1, min(127, n.velocity + delta))
                if new_vel != n.velocity:
                    ops.append(SetNoteVelocity(
                        pattern_id=entry["pattern_id"],
                        note_id=entry["note_id"],
                        velocity=new_vel,
                    ))

        progress.update(1.0, "done")
        return PluginResult(
            operations=tuple(ops),
            message=f"{len(ops)} operations across {len(seen)} notes",
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


PLUGIN = HumanizePlugin
