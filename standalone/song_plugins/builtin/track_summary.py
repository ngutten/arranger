"""Track summary — stats demo plugin.

A single block that can render four different shapes via the ``view``
param:

- ``"summary"``     → text (whole-song stats)
- ``"per_track"``   → table (one row per track)
- ``"track_bars"``  → bars (note count per track)
- ``"top_pitches"`` → ranked_list (most-used pitches)
"""

from __future__ import annotations

import time
from typing import Dict, List

from ..api import (
    Annotation, PluginManifest, ParamSpec, PluginResult, Progress,
    Scope, SongPlugin,
)


_NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F",
               "F#", "G", "G#", "A", "A#", "B")


def _pitch_name(p: int) -> str:
    octave = (p // 12) - 1
    return f"{_NOTE_NAMES[p % 12]}{octave}"


class TrackSummaryPlugin(SongPlugin):
    manifest = PluginManifest(
        id="builtin.track_summary",
        name="Track Summary",
        version="1.0.0",
        description="Per-track note counts, ranges, top pitches.",
        capabilities=("analyze",),
        schemas=("stats",),
        params=(
            ParamSpec(
                key="view", type="enum",
                label="View", default="summary",
                choices=("summary", "per_track", "track_bars", "top_pitches"),
            ),
            ParamSpec(
                key="top_n", type="int",
                label="Top N", default=12,
                min=1, max=50,
                visible_when={"view": "top_pitches"},
            ),
        ),
        scopes=("whole",),
        deps=("midi", "structure", "tracks"),
        live_supported=True,
        persistence_default="transient",
    )

    def run(self, view, params: Dict, progress: Progress) -> PluginResult:
        t0 = time.monotonic()
        which = params.get("view", "summary")
        top_n = int(params.get("top_n", 12))

        # Gather track names once.
        track_name: Dict[int, str] = {}
        for t in view.tracks():
            track_name[t.id] = t.name or f"Track {t.id}"

        progress.phase("scan")
        total = 0
        pitch_total: Dict[int, int] = {}
        per_track_count: Dict[int, int] = {}
        per_track_min: Dict[int, int] = {}
        per_track_max: Dict[int, int] = {}
        per_track_dur: Dict[int, float] = {}
        end_beat = 0.0

        for i, n in enumerate(view.notes_in(Scope(kind="whole"))):
            if progress.cancelled:
                break
            total += 1
            pitch_total[n.pitch] = pitch_total.get(n.pitch, 0) + 1
            per_track_count[n.track_id] = per_track_count.get(n.track_id, 0) + 1
            per_track_dur[n.track_id] = per_track_dur.get(n.track_id, 0.0) + n.duration_beats
            lo = per_track_min.get(n.track_id)
            hi = per_track_max.get(n.track_id)
            per_track_min[n.track_id] = n.pitch if lo is None else min(lo, n.pitch)
            per_track_max[n.track_id] = n.pitch if hi is None else max(hi, n.pitch)
            e = n.start_beat + n.duration_beats
            if e > end_beat:
                end_beat = e
            if i % 512 == 0:
                progress.update(min(1.0, i / max(1, 4096)), None)

        # Build the requested annotation payload.
        if which == "summary":
            tracks_with_notes = len(per_track_count)
            if total == 0:
                text = "No notes in this project."
            else:
                lines = [
                    f"Notes: {total}",
                    f"Tracks with notes: {tracks_with_notes}",
                    f"Song length (from notes): {end_beat:.2f} beats",
                    f"Distinct pitches used: {len(pitch_total)}",
                    f"Project BPM: {view.bpm:.2f}",
                ]
                text = "\n".join(lines)
            data = {"render": "text", "data": text}

        elif which == "per_track":
            cols = ["Track", "Notes", "Range", "Total dur (beats)"]
            rows: List[list] = []
            for tid in sorted(per_track_count.keys(),
                              key=lambda x: -per_track_count[x]):
                nm = track_name.get(tid, f"Track {tid}")
                cnt = per_track_count[tid]
                lo = per_track_min[tid]
                hi = per_track_max[tid]
                rng = f"{_pitch_name(lo)} – {_pitch_name(hi)}"
                dur = per_track_dur.get(tid, 0.0)
                rows.append([nm, cnt, rng, f"{dur:.1f}"])
            data = {
                "render": "table",
                "data": {"columns": cols, "rows": rows},
            }

        elif which == "track_bars":
            rows = []
            for tid in sorted(per_track_count.keys(),
                              key=lambda x: -per_track_count[x]):
                rows.append({
                    "label": track_name.get(tid, f"Track {tid}"),
                    "value": per_track_count[tid],
                })
            data = {"render": "bars", "data": rows}

        else:  # top_pitches
            ranked = sorted(pitch_total.items(), key=lambda kv: -kv[1])
            rows = [
                {"label": _pitch_name(p), "value": c}
                for p, c in ranked[:top_n]
            ]
            data = {"render": "ranked_list", "data": rows}

        annotation = Annotation(
            id="builtin.track_summary/main",
            plugin_id=self.manifest.id,
            instance_id="main",
            title="Track Summary",
            schema="stats",
            data=data,
            render_hint={},
            declared_deps=self.manifest.deps,
            status="ok",
            last_run_ms=int((time.monotonic() - t0) * 1000),
        )
        progress.update(1.0, "done")
        return PluginResult(annotation=annotation)


PLUGIN = TrackSummaryPlugin
