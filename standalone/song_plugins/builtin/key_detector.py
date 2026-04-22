"""Key detection via Krumhansl-Schmuckler profiles — events demo plugin."""

from __future__ import annotations

import math
import time
from typing import Dict, List, Optional, Tuple

from ..api import (
    Annotation, PluginManifest, ParamSpec, PluginResult, Progress,
    Scope, SongPlugin,
)


# Krumhansl-Kessler key profiles. Indexed as pitch-class (C=0, C#=1, ...).
_KS_MAJOR = (6.35, 2.23, 3.48, 2.33, 4.38, 4.09,
             2.52, 5.19, 2.39, 3.66, 2.29, 2.88)
_KS_MINOR = (6.33, 2.68, 3.52, 5.38, 2.60, 3.53,
             2.54, 4.75, 3.98, 2.69, 3.34, 3.17)

_NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F",
               "F#", "G", "G#", "A", "A#", "B")

# Hue per pitch-class root (HSV-like, picked for visible separation on dark bg).
_ROOT_COLORS = (
    "#e06060", "#e08060", "#e0b060", "#c0d060",
    "#80d080", "#60d0a0", "#60d0d0", "#6090e0",
    "#7060e0", "#a060e0", "#d060c0", "#e06090",
)


def _mean(xs) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _pearson(a: Tuple[float, ...], b: Tuple[float, ...]) -> float:
    ma = _mean(a)
    mb = _mean(b)
    num = sum((ai - ma) * (bi - mb) for ai, bi in zip(a, b))
    da = math.sqrt(sum((ai - ma) ** 2 for ai in a))
    db = math.sqrt(sum((bi - mb) ** 2 for bi in b))
    if da == 0 or db == 0:
        return 0.0
    return num / (da * db)


def _detect(chroma: Tuple[float, ...],
            include_minor: bool,
            include_major: bool) -> Tuple[Optional[Tuple[int, str]], float, float]:
    """Return ((root, mode), best_corr, runner_up_corr).

    ``mode`` is ``"maj"`` or ``"min"``. Returns (None, 0, 0) for an
    all-zero chroma (no notes in window).
    """
    if sum(chroma) == 0.0:
        return (None, 0.0, 0.0)
    candidates: List[Tuple[float, int, str]] = []
    for root in range(12):
        rot = tuple(chroma[(i + root) % 12] for i in range(12))
        if include_major:
            candidates.append((_pearson(_KS_MAJOR, rot), root, "maj"))
        if include_minor:
            candidates.append((_pearson(_KS_MINOR, rot), root, "min"))
    candidates.sort(reverse=True)
    best = candidates[0]
    runner = candidates[1] if len(candidates) > 1 else (0.0, 0, "maj")
    return ((best[1], best[2]), best[0], runner[0])


def _label(root: int, mode: str) -> str:
    name = _NOTE_NAMES[root % 12]
    return f"{name} {mode}"


class KeyDetectorPlugin(SongPlugin):
    manifest = PluginManifest(
        id="builtin.key_detector",
        name="Key Detector",
        version="1.0.0",
        description="Krumhansl-Schmuckler key detection in sliding windows.",
        capabilities=("analyze",),
        schemas=("events",),
        params=(
            ParamSpec(
                key="window_beats", type="float",
                label="Window (beats)", default=4.0,
                min=1.0, max=32.0,
                help="Analysis window length.",
            ),
            ParamSpec(
                key="hop_beats", type="float",
                label="Hop (beats)", default=2.0,
                min=0.25, max=16.0,
                help="Distance between successive windows.",
            ),
            ParamSpec(
                key="mode", type="enum",
                label="Modes", default="both",
                choices=("both", "major", "minor"),
            ),
            ParamSpec(
                key="min_confidence", type="float",
                label="Min corr", default=0.55,
                min=0.0, max=1.0,
                help="Drop windows whose best correlation is below this.",
            ),
        ),
        scopes=("whole",),
        deps=("midi", "structure"),
        live_supported=True,
        persistence_default="transient",
    )

    def run(self, view, params: Dict, progress: Progress) -> PluginResult:
        t0 = time.monotonic()
        window = float(params.get("window_beats", 4.0))
        hop = max(0.01, float(params.get("hop_beats", 2.0)))
        mode_choice = params.get("mode", "both")
        min_conf = float(params.get("min_confidence", 0.55))
        include_major = mode_choice in ("both", "major")
        include_minor = mode_choice in ("both", "minor")

        progress.phase("collect")
        # Collect (start_beat, duration, pitch) tuples up front so we can
        # iterate them multiple times without rebuilding the view.
        notes: List[Tuple[float, float, int]] = []
        total_end = 0.0
        for i, n in enumerate(view.notes_in(Scope(kind="whole"))):
            if progress.cancelled:
                break
            notes.append((n.start_beat, max(0.0, n.duration_beats), n.pitch))
            e = n.start_beat + n.duration_beats
            if e > total_end:
                total_end = e
            if i % 512 == 0:
                progress.update(min(0.5, i / max(1, 8192)), None)

        if not notes:
            return PluginResult(
                annotation=Annotation(
                    id="builtin.key_detector/main",
                    plugin_id=self.manifest.id,
                    instance_id="main",
                    title="Key Detector",
                    schema="events",
                    data=[],
                    render_hint={"x_label": "beat"},
                    declared_deps=self.manifest.deps,
                    status="ok",
                    last_run_ms=int((time.monotonic() - t0) * 1000),
                ),
                message="No notes to analyze.",
            )

        progress.phase("detect")
        window_results: List[Tuple[float, float, Optional[Tuple[int, str]],
                                   float]] = []
        n_windows = max(1, int(math.ceil((total_end) / hop)))
        # Sort notes by start for a light early-exit in the inner loop.
        notes.sort(key=lambda r: r[0])

        for w in range(n_windows):
            if progress.cancelled:
                break
            w0 = w * hop
            w1 = w0 + window
            chroma = [0.0] * 12
            for (s, d, pitch) in notes:
                if s >= w1:
                    break
                e = s + d
                if e <= w0:
                    continue
                # Overlap duration — weights notes by how much of them
                # sits inside the window.
                overlap = max(0.0, min(e, w1) - max(s, w0))
                if overlap <= 0.0:
                    continue
                chroma[pitch % 12] += overlap
            key, best, runner = _detect(
                tuple(chroma), include_minor, include_major,
            )
            window_results.append((w0, w1, key, best))
            if w % 32 == 0:
                progress.update(0.5 + 0.5 * w / n_windows, None)

        progress.phase("merge")
        events: List[dict] = []
        cur_key: Optional[Tuple[int, str]] = None
        cur_start = 0.0
        cur_end = 0.0
        cur_conf = 0.0

        def flush():
            if cur_key is None:
                return
            root, mode = cur_key
            events.append({
                "beat": cur_start,
                "end_beat": cur_end,
                "label": _label(root, mode),
                "color": _ROOT_COLORS[root % 12],
                "lane": mode,
                "payload": f"confidence {cur_conf:.2f}",
            })

        for (w0, w1, key, conf) in window_results:
            # Ignore low-confidence or keyless windows (break the run).
            if key is None or conf < min_conf:
                flush()
                cur_key = None
                continue
            if cur_key == key:
                cur_end = w1
                if conf > cur_conf:
                    cur_conf = conf
                continue
            # Key changed — emit prior run, start fresh.
            flush()
            cur_key = key
            cur_start = w0
            cur_end = w1
            cur_conf = conf
        flush()

        annotation = Annotation(
            id="builtin.key_detector/main",
            plugin_id=self.manifest.id,
            instance_id="main",
            title="Key Detector",
            schema="events",
            data=events,
            render_hint={"x_label": "beat"},
            declared_deps=self.manifest.deps,
            status="ok",
            last_run_ms=int((time.monotonic() - t0) * 1000),
        )
        progress.update(1.0, f"{len(events)} regions")
        return PluginResult(
            annotation=annotation,
            message=f"{len(events)} key region{'s' if len(events) != 1 else ''}.",
        )


PLUGIN = KeyDetectorPlugin
