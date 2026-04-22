"""Per-track note density — multi_curve demo plugin."""

from __future__ import annotations

import math
import time
from typing import Dict

from ..api import (
    Annotation, PluginManifest, ParamSpec, PluginResult, Progress,
    Scope, SongPlugin,
)


class PerTrackDensityPlugin(SongPlugin):
    manifest = PluginManifest(
        id="builtin.per_track_density",
        name="Per-Track Density",
        version="1.0.0",
        description="Notes per beat, binned and broken down by track.",
        capabilities=("analyze",),
        schemas=("multi_curve",),
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
        ),
        scopes=("whole",),
        deps=("midi", "structure", "tracks"),
        live_supported=True,
        persistence_default="transient",
    )

    def run(self, view, params: Dict, progress: Progress) -> PluginResult:
        t0 = time.monotonic()
        window = float(params.get("window_beats", 2.0))
        smoothing = params.get("smoothing", "none")

        # track_id -> display name
        track_name: Dict[int, str] = {}
        for t in view.tracks():
            track_name[t.id] = t.name or f"Track {t.id}"

        progress.phase("count")
        # (track_id, bin_index) -> count
        counts: Dict[tuple, int] = {}
        max_bin = 0
        for i, n in enumerate(view.notes_in(Scope(kind="whole"))):
            if progress.cancelled:
                break
            b = int(math.floor(n.start_beat / window))
            key = (n.track_id, b)
            counts[key] = counts.get(key, 0) + 1
            if b > max_bin:
                max_bin = b
            if i % 256 == 0:
                progress.update(min(1.0, i / max(1, 4096)), None)

        progress.phase("emit")
        beats = []
        if counts:
            for b in range(0, max_bin + 1):
                beats.append(b * window + window * 0.5)

        # Build per-track series; skip tracks with zero notes in the scope.
        track_ids_with_notes = sorted({tid for (tid, _b) in counts.keys()})
        series: Dict[str, list] = {}
        for tid in track_ids_with_notes:
            row = [counts.get((tid, b), 0) / window
                   for b in range(0, max_bin + 1)]
            if smoothing == "ema" and row:
                alpha = 0.5
                ema = [row[0]]
                for v in row[1:]:
                    ema.append(alpha * v + (1.0 - alpha) * ema[-1])
                row = ema
            series[track_name.get(tid, f"Track {tid}")] = row

        annotation = Annotation(
            id="builtin.per_track_density/main",
            plugin_id=self.manifest.id,
            instance_id="main",
            title="Per-Track Density",
            schema="multi_curve",
            data={"beats": beats, "series": series},
            render_hint={"y_label": "notes/beat", "x_label": "beat"},
            declared_deps=self.manifest.deps,
            persistence=self.manifest.persistence_default,
            status="ok",
            last_run_ms=int((time.monotonic() - t0) * 1000),
        )
        progress.update(1.0, "done")
        return PluginResult(annotation=annotation)


PLUGIN = PerTrackDensityPlugin
