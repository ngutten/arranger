"""Key-fit heatmap — 24 keys × N windows, cells = Krumhansl correlation.

Uses the shared ``analysis.key_fit`` primitive. Rows are ordered so that
musically-related keys are visually adjacent: descending the circle of
fifths, interleaving each major key with its relative minor. This makes
modulations read as lateral movements across one or two rows rather
than visual jumps across the display.

Designed to be broadcast into the BroadcastBand below the arranger so
the user can scrub over time and see the detected key change shape.
"""

from __future__ import annotations

import math
import time
from typing import Dict, List, Tuple

from ..api import (
    Annotation, PluginManifest, ParamSpec, PluginResult, Progress,
    Scope, SongPlugin,
)
from ..analysis import (
    PITCH_CLASS_NAMES, windowed_chroma, key_fit_scores,
)


# Circle-of-fifths row ordering with majors and relative minors
# interleaved. Each entry is ``(pitch_class, mode)``.
# Start at C major, then its relative minor A, then go clockwise around
# the circle (+7 semitones each step for majors, and each major's
# relative minor is +9 semitones / -3).
def _build_row_order() -> List[Tuple[int, str]]:
    rows: List[Tuple[int, str]] = []
    pc = 0
    for _ in range(12):
        rows.append((pc, "maj"))
        rows.append(((pc + 9) % 12, "min"))
        pc = (pc + 7) % 12
    return rows


_ROW_ORDER = _build_row_order()


def _row_label(pc: int, mode: str) -> str:
    name = PITCH_CLASS_NAMES[pc % 12]
    return f"{name} {'maj' if mode == 'maj' else 'min'}"


class KeyFitHeatmapPlugin(SongPlugin):
    manifest = PluginManifest(
        id="builtin.key_fit_heatmap",
        name="Key Fit Heatmap",
        version="1.0.0",
        description=(
            "Krumhansl-Kessler fit of every window against all 24 keys. "
            "Rows follow circle-of-fifths with relative minors interleaved "
            "so neighbouring keys are adjacent; modulations and pivot "
            "regions show as handoffs between rows."
        ),
        capabilities=("analyze",),
        schemas=("grid2d",),
        params=(
            ParamSpec(
                key="window_beats", type="float",
                label="Window (beats)", default=2.0,
                min=0.5, max=16.0,
                help="Width of each analysis window. Smaller = more "
                     "temporal resolution but noisier fits.",
            ),
            ParamSpec(
                key="hop_beats", type="float",
                label="Hop (beats)", default=1.0,
                min=0.25, max=16.0,
                help="Distance between window centers. Smaller = "
                     "smoother horizontal shading.",
            ),
            ParamSpec(
                key="colormap", type="enum",
                label="Colormap", default="magma",
                choices=("magma", "viridis", "heat", "gray"),
            ),
            ParamSpec(
                key="clip_negative", type="bool",
                label="Clip negative fits to zero", default=True,
                help="Negative correlations indicate anti-fit. Clipping "
                     "keeps the colormap focused on positive matches.",
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
        hop = float(params.get("hop_beats", 1.0))
        cmap = params.get("colormap", "magma")
        clip_negative = bool(params.get("clip_negative", True))

        total = float(view.total_beats)

        progress.phase("chroma")
        if total <= 0.0:
            starts, chromas = [], []
        else:
            starts, chromas = windowed_chroma(
                view.notes_in(Scope(kind="whole")),
                window_beats=window,
                total_beats=total,
                hop_beats=hop,
            )
        if not chromas:
            # Still emit an empty annotation so the UI can clear cleanly.
            return PluginResult(annotation=Annotation(
                id="builtin.key_fit_heatmap/main",
                plugin_id=self.manifest.id,
                instance_id="main",
                title="Key Fit Heatmap",
                schema="grid2d",
                data={"rows": 24, "cols": 0, "cells": []},
                render_hint={"colormap": cmap,
                             "row_labels": [_row_label(*k) for k in _ROW_ORDER],
                             "x_label": "beat",
                             "beat_range": (0.0, total)},
                declared_deps=self.manifest.deps,
                status="ok",
                last_run_ms=int((time.monotonic() - t0) * 1000),
            ))

        progress.phase("fit")
        cols = len(chromas)
        cells = [0.0] * (24 * cols)
        for c, chroma in enumerate(chromas):
            if progress.cancelled:
                break
            scores = key_fit_scores(chroma)
            for r, (pc, mode) in enumerate(_ROW_ORDER):
                v = scores[(pc, mode)]
                if clip_negative and v < 0:
                    v = 0.0
                cells[r * cols + c] = v
            if c % 64 == 0:
                progress.update(c / max(1, cols), None)

        data = {"rows": 24, "cols": cols, "cells": cells}
        hint = {
            "colormap": cmap,
            "row_labels": [_row_label(*k) for k in _ROW_ORDER],
            "x_label": "beat",
            # Data extent in beats — last window's right edge.
            "beat_range": (0.0, starts[-1] + window if starts else total),
        }
        if clip_negative:
            hint["vmin"] = 0.0
            hint["vmax"] = 1.0

        annotation = Annotation(
            id="builtin.key_fit_heatmap/main",
            plugin_id=self.manifest.id,
            instance_id="main",
            title="Key Fit Heatmap",
            schema="grid2d",
            data=data,
            render_hint=hint,
            declared_deps=self.manifest.deps,
            status="ok",
            last_run_ms=int((time.monotonic() - t0) * 1000),
        )
        progress.update(1.0, "done")
        return PluginResult(annotation=annotation)


PLUGIN = KeyFitHeatmapPlugin
