"""Metric Velocity Pattern — apply a per-position velocity weight pattern.

Velocity is shaped by a short weight cycle indexed by the note's metric
position. Two modes: ``absolute`` rewrites each velocity to a scaled version
of its weight, ``relative`` scales the existing velocity by the ratio of the
weight to the pattern's average.
"""

from __future__ import annotations

import math
from typing import Dict, List, Tuple

from ..api import (
    ParamSpec, PluginManifest, PluginResult, Progress, Scope,
    SetNoteVelocity, SongPlugin,
)


def _parse_weights(raw: str) -> List[float]:
    """Parse a space-separated weight string. Raises ValueError on bad input."""
    tokens = [t for t in str(raw).split() if t]
    if not tokens:
        raise ValueError("pattern is empty")
    out: List[float] = []
    for tok in tokens:
        out.append(float(tok))  # ValueError on non-numeric
    return out


class MetricVelocityPlugin(SongPlugin):
    manifest = PluginManifest(
        id="builtin.metric_velocity",
        name="Metric Velocity Pattern",
        version="1.0.0",
        description="Apply a per-position velocity weight pattern to notes.",
        capabilities=("transform",),
        schemas=(),
        params=(
            ParamSpec(
                key="subdivision", type="enum",
                label="Positions per beat", default="4",
                choices=("1", "2", "3", "4", "6", "8"),
            ),
            ParamSpec(
                key="pattern", type="string",
                label="Weights", default="5 1 3 1",
                help="Space-separated numeric weights; one per position in the cycle.",
            ),
            ParamSpec(
                key="mode", type="enum",
                label="Mode", default="absolute",
                choices=("absolute", "relative"),
                help="Absolute: velocity = weight/max * 127. "
                     "Relative: velocity = original * weight/avg.",
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
        subdivision = int(params.get("subdivision", "4"))
        weights = _parse_weights(params.get("pattern", "5 1 3 1"))
        mode = params.get("mode", "absolute")
        scope_kind = params.get("scope", "whole")

        scope = self._resolve_scope(view, scope_kind)

        max_w = max(weights)
        avg_w = sum(weights) / len(weights) if weights else 0.0
        cycle = len(weights)

        ops: List[SetNoteVelocity] = []
        progress.phase("scan")
        for n in view.notes_in(scope):
            if progress.cancelled:
                break
            if n.source_kind != "pattern":
                # Variations are not targeted by this PR.
                continue
            if n.note_id == 0:
                continue  # legacy / unassigned note

            pos = int(math.floor(n.start_beat * subdivision)) % cycle
            weight = weights[pos]
            if mode == "relative":
                if avg_w <= 0:
                    continue
                new_vel = int(round(n.velocity * weight / avg_w))
            else:
                if max_w <= 0:
                    continue
                new_vel = int(round(weight / max_w * 127))
            new_vel = max(1, min(127, new_vel))
            if new_vel == n.velocity:
                continue
            ops.append(SetNoteVelocity(
                pattern_id=n.source_id,
                note_id=n.note_id,
                velocity=new_vel,
            ))

        progress.update(1.0, "done")
        return PluginResult(
            operations=tuple(ops),
            message=f"{len(ops)} velocity changes",
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


PLUGIN = MetricVelocityPlugin
