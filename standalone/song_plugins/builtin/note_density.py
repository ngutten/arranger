"""Note density analyzer — counts notes per beat in windows."""

from __future__ import annotations

import math
import time
from typing import Dict

from ..api import (
    Annotation, PluginManifest, ParamSpec, PluginResult, Progress,
    Scope, SongPlugin,
)


class NoteDensityPlugin(SongPlugin):
    manifest = PluginManifest(
        id="builtin.note_density",
        name="Note Density",
        version="1.0.0",
        description="Notes per beat, binned into fixed windows.",
        capabilities=("analyze",),
        schemas=("scalar_curve",),
        params=(
            ParamSpec(
                key="window_beats", type="float",
                label="Window (beats)", default=2.0,
                min=0.25, max=16.0,
                help="Width of each density bin, in beats.",
            ),
            ParamSpec(
                key="smoothing", type="enum",
                label="Smoothing", default="none",
                choices=("none", "ema"),
            ),
            ParamSpec(
                key="scope", type="enum",
                label="Scope", default="whole",
                choices=("whole", "selection"),
            ),
        ),
        scopes=("whole", "selection"),
        selection_kinds=("placements",),
        deps=("midi", "structure"),
        live_supported=True,
        persistence_default="transient",
    )

    def run(self, view, params: Dict, progress: Progress) -> PluginResult:
        t0 = time.monotonic()
        window = float(params.get("window_beats", 2.0))
        smoothing = params.get("smoothing", "none")
        scope_kind = params.get("scope", "whole")

        if scope_kind == "selection":
            sel = view.selection()
            if sel.placements:
                scope = Scope(
                    kind="placements",
                    placement_ids=tuple(sorted(sel.placements)),
                )
            else:
                scope = Scope(kind="whole")
        else:
            scope = Scope(kind="whole")

        progress.phase("count")
        counts: Dict[int, int] = {}
        max_bin = 0
        for i, n in enumerate(view.notes_in(scope)):
            if progress.cancelled:
                break
            b = int(math.floor(n.start_beat / window))
            counts[b] = counts.get(b, 0) + 1
            if b > max_bin:
                max_bin = b
            if i % 256 == 0:
                # Cheap, bounded progress feedback.
                progress.update(min(1.0, i / max(1, 4096)), None)

        progress.phase("emit")
        beats = []
        values = []
        if counts:
            for b in range(0, max_bin + 1):
                beats.append(b * window + window * 0.5)
                values.append(counts.get(b, 0) / window)

        if smoothing == "ema" and values:
            alpha = 0.5
            ema = [values[0]]
            for v in values[1:]:
                ema.append(alpha * v + (1.0 - alpha) * ema[-1])
            values = ema

        annotation = Annotation(
            id="builtin.note_density/main",
            plugin_id=self.manifest.id,
            instance_id="main",
            title="Note Density",
            schema="scalar_curve",
            data={"beats": beats, "values": values},
            render_hint={"y_label": "notes/beat", "x_label": "beat"},
            declared_deps=self.manifest.deps,
            persistence=self.manifest.persistence_default,
            status="ok",
            last_run_ms=int((time.monotonic() - t0) * 1000),
        )
        progress.update(1.0, "done")
        return PluginResult(annotation=annotation)


PLUGIN = NoteDensityPlugin
