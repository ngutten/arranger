"""Scale-conformance hinter — flag notes outside the pattern's key.

Emits a ``regions`` annotation: one small box tightly around each note
whose pitch class is not in the pattern's declared scale. Box color is
a warning red by default. The user sees the offending notes highlighted
behind the grid; chord-tones and passing tones stay uncolored.

This plugin is deliberately minimal — no quantize / snap-to-scale
functionality. A sibling transform plugin could consume the same
analysis and emit ``MoveNote`` operations to fix the flagged notes, but
that's out of scope here.
"""

from __future__ import annotations

import time
from typing import Dict, List

from ...state import scale_set
from ..api import (
    Annotation, ParamSpec, PluginManifest, PluginResult, Progress,
    Scope, SongPlugin,
)


class ScaleConformanceHinterPlugin(SongPlugin):
    manifest = PluginManifest(
        id="builtin.scale_conformance",
        name="Scale Conformance",
        version="1.0.0",
        description=(
            "Highlight notes whose pitch class is not in the pattern's "
            "declared key + scale. Broadcasts to the piano roll as "
            "colored boxes behind the flagged notes."
        ),
        capabilities=("analyze",),
        schemas=("regions",),
        params=(
            ParamSpec(
                key="color", type="enum",
                label="Highlight color", default="red",
                choices=("red", "amber", "magenta"),
            ),
            ParamSpec(
                key="pad_beats", type="float",
                label="Box padding (beats)", default=0.05,
                min=0.0, max=0.5,
                help="Small extra padding around each flagged note so the "
                     "box frames the note instead of sitting flush.",
            ),
        ),
        scopes=("whole",),
        deps=("midi", "structure"),
        live_supported=True,
        persistence_default="transient",
    )

    def run(self, view, params: Dict, progress: Progress) -> PluginResult:
        t0 = time.monotonic()
        color_name = params.get("color", "red")
        pad = float(params.get("pad_beats", 0.05))

        color = {
            "red": "#d04040",
            "amber": "#e0a040",
            "magenta": "#d060c0",
        }.get(color_name, "#d04040")

        progress.phase("scan")
        patterns_with_placements = set()
        for pl in view.placements():
            patterns_with_placements.add(pl.pattern_id)

        regions_out: List[dict] = []

        total = max(1, len(patterns_with_placements))
        for i, pat_id in enumerate(sorted(patterns_with_placements)):
            if progress.cancelled:
                break
            raw_pat = view._state.find_pattern(pat_id)  # noqa: SLF001
            if raw_pat is None or not raw_pat.notes:
                continue
            try:
                in_scale = scale_set(raw_pat.key, raw_pat.scale)
            except Exception:
                in_scale = set()
            if not in_scale:
                continue
            for n in raw_pat.notes:
                if (n.pitch % 12) in in_scale:
                    continue
                regions_out.append({
                    "start_beat": max(0.0, n.start - pad),
                    "end_beat": min(raw_pat.length,
                                    n.start + n.duration + pad),
                    "min_pitch": n.pitch,
                    "max_pitch": n.pitch,
                    "note_ids": (n.note_id,) if n.note_id else (),
                    "pattern_id": pat_id,
                    "color": color,
                    "label": "",
                    "payload": {
                        "reason": "out-of-scale",
                        "key": raw_pat.key,
                        "scale": raw_pat.scale,
                        "pitch": n.pitch,
                    },
                })
            progress.update(i / total, None)

        annotation = Annotation(
            id="builtin.scale_conformance/main",
            plugin_id=self.manifest.id,
            instance_id="main",
            title="Scale Conformance",
            schema="regions",
            data=regions_out,
            render_hint={},
            declared_deps=self.manifest.deps,
            status="ok",
            last_run_ms=int((time.monotonic() - t0) * 1000),
        )
        progress.update(1.0, "done")
        msg = (f"{len(regions_out)} out-of-scale notes"
               if regions_out else "No out-of-scale notes found")
        return PluginResult(annotation=annotation, message=msg)


PLUGIN = ScaleConformanceHinterPlugin
