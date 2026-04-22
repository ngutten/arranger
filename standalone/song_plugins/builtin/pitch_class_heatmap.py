"""Pitch-class heatmap — grid2d demo plugin."""

from __future__ import annotations

import math
import time
from typing import Dict, List

from ..api import (
    Annotation, PluginManifest, ParamSpec, PluginResult, Progress,
    Scope, SongPlugin,
)


_NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F",
               "F#", "G", "G#", "A", "A#", "B")


class PitchClassHeatmapPlugin(SongPlugin):
    manifest = PluginManifest(
        id="builtin.pitch_class_heatmap",
        name="Pitch-Class Heatmap",
        version="1.0.0",
        description="12 pitch classes × N time bins; cell = note activity.",
        capabilities=("analyze",),
        schemas=("grid2d",),
        params=(
            ParamSpec(
                key="window_beats", type="float",
                label="Window (beats)", default=2.0,
                min=0.25, max=16.0,
            ),
            ParamSpec(
                key="weight", type="enum",
                label="Weight by", default="duration",
                choices=("count", "duration"),
                help="'count' tallies note-ons; 'duration' weighs by note length.",
            ),
            ParamSpec(
                key="colormap", type="enum",
                label="Colormap", default="magma",
                choices=("magma", "viridis", "heat", "gray"),
            ),
        ),
        scopes=("whole",),
        deps=("midi", "structure"),
        live_supported=True,
        persistence_default="transient",
    )

    def run(self, view, params: Dict, progress: Progress) -> PluginResult:
        t0 = time.monotonic()
        window = float(params.get("window_beats", 2.0))
        weight = params.get("weight", "duration")
        cmap = params.get("colormap", "magma")

        # Pitch-class row ordering: place C at the top for readability,
        # but we could let the user flip later. Row 0 = B, Row 11 = C
        # would read like a piano-roll; for now stick with C at top.
        # If you flip the convention, reverse this list and the filler.
        row_pc_order = list(range(12))  # top→bottom: C, C#, D, ...

        progress.phase("scan")
        # First pass — find total extent.
        max_end = 0.0
        notes = []
        for i, n in enumerate(view.notes_in(Scope(kind="whole"))):
            if progress.cancelled:
                break
            notes.append((n.start_beat, max(0.0, n.duration_beats), n.pitch))
            e = n.start_beat + n.duration_beats
            if e > max_end:
                max_end = e
            if i % 512 == 0:
                progress.update(min(0.5, i / max(1, 8192)), None)

        cols = max(1, int(math.ceil(max_end / window)))
        cells = [0.0] * (12 * cols)

        progress.phase("bin")
        notes.sort(key=lambda r: r[0])
        for idx, (s, d, p) in enumerate(notes):
            if progress.cancelled:
                break
            pc = p % 12
            row_idx = row_pc_order.index(pc)
            if weight == "count":
                col = int(math.floor(s / window))
                if 0 <= col < cols:
                    cells[row_idx * cols + col] += 1.0
            else:  # duration
                if d <= 0:
                    col = int(math.floor(s / window))
                    if 0 <= col < cols:
                        cells[row_idx * cols + col] += 0.01
                    continue
                end = s + d
                c0 = max(0, int(math.floor(s / window)))
                c1 = min(cols - 1, int(math.floor((end - 1e-9) / window)))
                for c in range(c0, c1 + 1):
                    w0 = c * window
                    w1 = w0 + window
                    overlap = max(0.0, min(end, w1) - max(s, w0))
                    cells[row_idx * cols + c] += overlap
            if idx % 1024 == 0:
                progress.update(0.5 + 0.5 * idx / max(1, len(notes)), None)

        row_labels = [_NOTE_NAMES[pc] for pc in row_pc_order]
        data = {"rows": 12, "cols": cols, "cells": cells}

        annotation = Annotation(
            id="builtin.pitch_class_heatmap/main",
            plugin_id=self.manifest.id,
            instance_id="main",
            title="Pitch-Class Heatmap",
            schema="grid2d",
            data=data,
            render_hint={
                "colormap": cmap,
                "row_labels": row_labels,
                "x_label": "beat",
                "beat_range": (0.0, cols * window),
            },
            declared_deps=self.manifest.deps,
            status="ok",
            last_run_ms=int((time.monotonic() - t0) * 1000),
        )
        progress.update(1.0, "done")
        return PluginResult(annotation=annotation)


PLUGIN = PitchClassHeatmapPlugin
